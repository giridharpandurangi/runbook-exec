"""Executor module for runbook-exec.

Orchestrates the two-phase parse+classify → execute pipeline.
Wires together parser, classifier, shell, approval, audit, display, and llm modules.
"""

import json
import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

import runbook_exec.interrupt as interrupt
from runbook_exec import approval, classifier, display, parser, shell
from runbook_exec._json_utils import strip_markdown_fences
from runbook_exec.approval import FailureDirection
from runbook_exec.audit import create_audit_log
from runbook_exec.llm import call_llm
from runbook_exec.models import (
    ActionType,
    Config,
    ExecutionResult,
    ExecutionSummary,
    RiskLevel,
    Step,
    needs_approval,
)

logger = logging.getLogger(__name__)

_LLM_SYSTEM_PROMPT = (
    "You are an execution controller for an automated runbook. "
    "After each step, decide whether to continue, skip steps, or abort.\n"
    "Return ONLY the JSON object. Do not wrap it in markdown code fences. "
    "Do not include any prose before or after.\n"
    'Respond with JSON only: {"action": "continue"|"skip"|"abort", '
    '"skip_count": <int or null>, "reasoning": "<one sentence>"}'
)


class LLMDecision(BaseModel):
    """Decision returned by the LLM after a step completes."""

    action: Literal["continue", "skip", "abort"]
    skip_count: int | None = None
    reasoning: str


def llm_decision(
    step: Step,
    exec_result: ExecutionResult,
    remaining_steps: list[Step],
    config: Config,
    runbook_path: Path,
) -> LLMDecision:
    """Call the LLM with the post-step decision prompt and return a parsed decision.

    Args:
        step: The step that just completed.
        exec_result: The execution result for that step.
        remaining_steps: Steps that have not yet been executed.
        config: Runtime configuration.
        runbook_path: Path to the runbook file (used as context in the prompt).

    Returns:
        LLMDecision with action, optional skip_count, and reasoning.
        Defaults to continue if the LLM response is unparseable.
    """
    remaining_texts = "\n".join(
        f"  {s.index}. {s.text}" for s in remaining_steps
    )

    user_prompt = (
        f"Runbook context: {runbook_path}\n"
        f"Completed step {step.index}: {step.text}\n"
        f"Command: {step.command}\n"
        f"Exit code: {exec_result.exit_code}\n"
        f"Output:\n{exec_result.stdout}\n{exec_result.stderr}\n"
        f"Remaining steps: {remaining_texts}"
    )

    try:
        response_text = call_llm(
            prompt=user_prompt,
            system=_LLM_SYSTEM_PROMPT,
            model=config.llm_model,
        )
        data = json.loads(strip_markdown_fences(response_text))
        return LLMDecision(
            action=data["action"],
            skip_count=data.get("skip_count"),
            reasoning=data["reasoning"],
        )
    except Exception:
        logger.warning("LLM post-step decision response unparseable, defaulting to continue")
        return LLMDecision(
            action="continue",
            reasoning="LLM response unparseable, defaulting to continue",
        )


def _handle_direction(
    direction: FailureDirection,
    step: Step,
    audit_log,
    config: Config,
) -> str:
    """Handle a failure direction choice and write the appropriate audit entry.

    Args:
        direction: The FailureDirection chosen by the operator.
        step: The step that failed or was denied.
        audit_log: The open AuditLog to append entries to.
        config: Runtime configuration.

    Returns:
        One of "abort", "retry", "skip", or "continue".
    """
    if direction == FailureDirection.ABORT:
        audit_log.append({
            "action": ActionType.ABORT,
            "step_index": step.index,
            "step_text": step.text,
            "reasoning": "operator aborted",
            "mode": "live",
        })
        return "abort"
    elif direction == FailureDirection.RETRY:
        return "retry"
    elif direction == FailureDirection.SKIP:
        audit_log.append({
            "action": ActionType.SKIP,
            "step_index": step.index,
            "step_text": step.text,
            "reasoning": "operator skipped",
            "mode": "live",
        })
        return "skip"
    else:  # CONTINUE
        audit_log.append({
            "action": ActionType.FAILURE,
            "step_index": step.index,
            "step_text": step.text,
            "reasoning": "operator continued after failure",
            "mode": "live",
        })
        return "continue"


def _execute_steps(
    steps: list[Step],
    audit_log,
    runbook_path: Path,
    config: Config,
) -> ExecutionSummary:
    """Execute all steps in the execution phase (Phase 2).

    Args:
        steps: Classified steps to execute.
        audit_log: Open AuditLog for writing entries.
        runbook_path: Path to the runbook (used in LLM prompts).
        config: Runtime configuration.

    Returns:
        ExecutionSummary with counts and audit log path.
    """
    i = 0
    successful = 0
    failed = 0
    skipped = 0
    aborted = False
    consecutive_retries = 0

    while i < len(steps):
        # Check for SIGINT before processing each step
        if interrupt.is_interrupted():
            audit_log.append({
                "action": ActionType.ABORT,
                "step_index": steps[i].index,
                "step_text": steps[i].text,
                "reasoning": "interrupted by SIGINT",
                "mode": "live",
            })
            aborted = True
            break

        step = steps[i]

        # Dry-run: never touches Slack or subprocess
        if config.dry_run:
            display.show_dry_run_step(step)
            i += 1
            continue

        # Steps with no extractable command are skipped silently
        if step.command is None:
            display.show_step_skipped(step, reason="no command")
            audit_log.append({
                "action": ActionType.SKIP,
                "step_index": step.index,
                "step_text": step.text,
                "command": None,
                "risk_level": step.risk_level,
                "reasoning": "no command",
                "mode": "live",
            })
            skipped += 1
            i += 1
            continue

        # Approval gate (only reached in live mode, only for steps with commands)
        if needs_approval(step, config):
            approval_result = approval.request_approval(step, config)

            if approval_result.timed_out:
                audit_log.append({
                    "action": ActionType.FAILURE,
                    "step_index": step.index,
                    "step_text": step.text,
                    "command": step.command,
                    "risk_level": step.risk_level,
                    "reasoning": "approval timed out",
                    "mode": "live",
                })
                failed += 1
                direction = approval.request_failure_direction(
                    step, "approval timed out", config, include_retry_warning=False
                )
                result = _handle_direction(direction, step, audit_log, config)
                if result == "abort":
                    aborted = True
                    break
                elif result == "retry":
                    consecutive_retries += 1
                    failed -= 1  # Will be re-counted if it fails again
                    # Don't increment i — retry same step
                    continue
                else:  # continue or skip
                    if result == "skip":
                        failed -= 1
                        skipped += 1
                    # else: already counted as failed
                    consecutive_retries = 0
                    i += 1
                    continue
            elif approval_result.approved:
                audit_log.append({
                    "action": ActionType.APPROVE,
                    "step_index": step.index,
                    "step_text": step.text,
                    "command": step.command,
                    "risk_level": step.risk_level,
                    "approver_slack_id": approval_result.approver_slack_id,
                    "mode": "live",
                })
            else:
                # Denied
                audit_log.append({
                    "action": ActionType.DENY,
                    "step_index": step.index,
                    "step_text": step.text,
                    "command": step.command,
                    "risk_level": step.risk_level,
                    "approver_slack_id": approval_result.approver_slack_id,
                    "mode": "live",
                })
                direction = approval.request_failure_direction(
                    step, "Step was denied", config, include_retry_warning=False
                )
                result = _handle_direction(direction, step, audit_log, config)
                if result == "abort":
                    aborted = True
                    break
                elif result == "retry":
                    consecutive_retries += 1
                    continue
                else:
                    if result == "skip":
                        skipped += 1
                    else:
                        failed += 1
                    consecutive_retries = 0
                    i += 1
                    continue

        # Execute the command
        display.show_step_running(step)
        exec_result = shell.run_command(step.command, config.timeout_seconds)

        audit_log.append({
            "action": ActionType.EXECUTE,
            "step_index": step.index,
            "step_text": step.text,
            "command": step.command,
            "risk_level": step.risk_level,
            "output": (exec_result.stdout + exec_result.stderr).strip() or None,
            "stdout": exec_result.stdout or None,
            "stderr": exec_result.stderr or None,
            "exit_code": exec_result.exit_code,
            "duration_seconds": exec_result.duration_seconds,
            "mode": "live",
        })

        if exec_result.exit_code == 0 and not exec_result.timed_out:
            display.show_step_success(step, exec_result)
            successful += 1
            consecutive_retries = 0
        else:
            display.show_step_failure(step, exec_result)
            failed += 1
            reason = "timed out" if exec_result.timed_out else f"exit code {exec_result.exit_code}"

            # Check if this is a second consecutive retry failure
            if consecutive_retries >= 1:
                # Already retried once — go back to direction prompt
                direction = approval.request_failure_direction(
                    step,
                    f"{reason} (retry also failed)",
                    config,
                    include_retry_warning=(step.risk_level != RiskLevel.READ_ONLY),
                )
            else:
                direction = approval.request_failure_direction(
                    step,
                    reason,
                    config,
                    include_retry_warning=(step.risk_level != RiskLevel.READ_ONLY),
                )

            result = _handle_direction(direction, step, audit_log, config)
            if result == "abort":
                aborted = True
                break
            elif result == "retry":
                consecutive_retries += 1
                failed -= 1  # Will be re-counted if it fails again
                i -= 1  # Will be incremented at end of loop
            else:
                if result == "skip":
                    failed -= 1  # Was counted as failed, now it's skipped
                    skipped += 1
                consecutive_retries = 0

        # LLM post-step decision (only for successful steps in live mode)
        if (
            not config.no_llm_context
            and exec_result.exit_code == 0
            and not exec_result.timed_out
        ):
            remaining = steps[i + 1 :]
            decision = llm_decision(step, exec_result, remaining, config, runbook_path)

            if decision.action == "abort":
                audit_log.append({
                    "action": ActionType.ABORT,
                    "step_index": step.index,
                    "step_text": step.text,
                    "command": step.command,
                    "risk_level": step.risk_level,
                    "reasoning": decision.reasoning,
                    "mode": "live",
                })
                aborted = True
                break

            if decision.action == "skip" and decision.skip_count:
                for skipped_step in steps[i + 1 : i + 1 + decision.skip_count]:
                    audit_log.append({
                        "action": ActionType.SKIP,
                        "step_index": skipped_step.index,
                        "step_text": skipped_step.text,
                        "command": skipped_step.command,
                        "risk_level": skipped_step.risk_level,
                        "reasoning": decision.reasoning,
                        "mode": "live",
                    })
                    skipped += 1
                i += decision.skip_count

        i += 1

    # Write summary entry
    total = len(steps)
    audit_log.append({
        "action": ActionType.SUMMARY,
        "reasoning": f"Execution complete: {successful} succeeded, {failed} failed, {skipped} skipped",
        "mode": "dry_run" if config.dry_run else "live",
    })

    summary = ExecutionSummary(
        total_steps=total,
        successful=successful,
        failed=failed,
        skipped=skipped,
        aborted=aborted,
        audit_log_path=str(audit_log.path),
    )
    display.show_summary(summary)
    return summary


def run_runbook(runbook_path: Path, config: Config) -> ExecutionSummary:
    """Parse, classify, and execute a runbook. Returns a summary.

    Phase 1: Parse all steps and classify them (all before any execution).
    Phase 2: Execute steps in order with approval gates, shell execution,
             and LLM post-step decisions.

    Args:
        runbook_path: Path to the Markdown runbook file.
        config: Runtime configuration.

    Returns:
        ExecutionSummary with counts and audit log path.

    Raises:
        ParseError: If the runbook cannot be parsed.
        ClassificationError: If a step cannot be classified.
        AuditError: If the audit log cannot be created or written.
    """
    steps = parser.parse_runbook(runbook_path)

    # Show approval mode banner so the operator knows what to expect
    if not config.dry_run:
        display.show_approval_mode_banner(config.slack_enabled)

    with create_audit_log(config, runbook_path) as audit_log:
        audit_log.append({
            "action": ActionType.PARSE,
            "step_index": None,
            "step_text": None,
            "command": None,
            "reasoning": f"Parsed {len(steps)} steps from {runbook_path}",
            "mode": "dry_run" if config.dry_run else "live",
        })

        for idx, step in enumerate(steps):
            step = classifier.classify_step(step, config)
            steps[idx] = step
            display.show_classification(step)
            audit_log.append({
                "action": ActionType.CLASSIFY,
                "step_index": step.index,
                "step_text": step.text,
                "command": step.command,
                "risk_level": step.risk_level,
                "reasoning": step.classification_reasoning,
                "mode": "dry_run" if config.dry_run else "live",
            })

        # Phase 2 — Execute
        summary = _execute_steps(steps, audit_log, runbook_path, config)

    return summary
