"""MCP server for runbook-exec.

Exposes three tools to Claude Desktop and other MCP clients:
  - validate_runbook   — parse + classify without executing
  - execute_runbook    — execute with safety gates and audit trail
  - replay_audit_log   — read and verify a previous execution log

Approval handling
-----------------
When Slack is configured (SLACK_BOT_TOKEN + SLACK_APP_TOKEN), approvals go
through Slack as normal.

When Slack is NOT configured, the MCP server uses a two-phase pattern because
rich.prompt.Confirm cannot be used in a background subprocess with no TTY:

  Phase 1 — execute_runbook runs until a step needs approval, then returns:
    {"status": "approval_required", "pending_step": {...}, ...}

  Phase 2 — the MCP client (Claude) shows the user the pending step, asks
    for approval, then calls execute_runbook again with:
      resume_from_step=N, approved=True|False

This lets Claude handle the approval conversation naturally.
"""

import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from runbook_exec import audit as audit_module
from runbook_exec import classifier, parser, shell
from runbook_exec._json_utils import strip_markdown_fences
from runbook_exec.audit import create_audit_log
from runbook_exec.exceptions import RunbookExecError
from runbook_exec.models import Config, RiskLevel, needs_approval

mcp = FastMCP("runbook-exec")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_config_for_mcp(**overrides: Any) -> Config | dict:
    """Load config, returning an error dict if ANTHROPIC_API_KEY is missing."""
    from runbook_exec.config import load_config
    from runbook_exec.exceptions import ConfigError

    try:
        return load_config(**overrides)
    except ConfigError as exc:
        return {"error": str(exc)}


def _step_to_dict(step: Any) -> dict:
    """Serialise a Step to a plain dict for MCP responses."""
    return {
        "index": step.index,
        "section": step.section,
        "text": step.text,
        "command": step.command,
        "risk_level": step.risk_level.value if step.risk_level else None,
        "classification_reasoning": step.classification_reasoning,
    }


def _exec_result_to_dict(result: Any) -> dict:
    """Serialise an ExecutionResult to a plain dict."""
    return {
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration_seconds": round(result.duration_seconds, 3),
        "timed_out": result.timed_out,
    }


# ---------------------------------------------------------------------------
# Tool: validate_runbook
# ---------------------------------------------------------------------------


@mcp.tool()
def validate_runbook(runbook_path: str) -> dict:
    """Parse and classify a runbook without executing any commands.

    Returns each step with its risk level and classification reasoning.
    Safe to call on any machine — no commands are executed.

    Args:
        runbook_path: Absolute or relative path to the Markdown runbook file.

    Returns:
        Dict with 'steps' list and 'total_steps' count, or 'error' on failure.
    """
    config_or_err = _load_config_for_mcp()
    if isinstance(config_or_err, dict):
        return config_or_err
    config: Config = config_or_err

    try:
        path = Path(runbook_path)
        steps = parser.parse_runbook(path)
        classified = []
        for step in steps:
            step = classifier.classify_step(step, config)
            classified.append(_step_to_dict(step))

        return {
            "status": "ok",
            "total_steps": len(classified),
            "steps": classified,
        }
    except RunbookExecError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return {"error": f"Unexpected error: {exc}"}


# ---------------------------------------------------------------------------
# Tool: execute_runbook
# ---------------------------------------------------------------------------


@mcp.tool()
def execute_runbook(
    runbook_path: str,
    dry_run: bool = True,
    incident_id: str | None = None,
    resume_from_step: int = 0,
    approved: bool | None = None,
) -> dict:
    """Execute a runbook with safety gates and a tamper-evident audit trail.

    SAFETY: dry_run=True by default. Pass dry_run=False only when the user
    explicitly confirms they want real execution.

    MCP approval flow (when Slack is not configured):
    - First call runs until a step needs approval, then returns
      status='approval_required' with details of the pending step.
    - Resume by calling again with resume_from_step=N and approved=True
      (to allow the step) or approved=False (to skip it).
    - If Slack IS configured, Slack handles approvals automatically and
      this function runs to completion.

    Args:
        runbook_path: Path to the Markdown runbook file.
        dry_run: When True (default), simulate without running commands.
        incident_id: Optional identifier used in the audit log filename.
        resume_from_step: Step index to resume from (used in Phase 2).
        approved: Approval decision for the pending step (used in Phase 2).

    Returns:
        On completion: {'status': 'completed', 'summary': {...}}
        On approval needed: {'status': 'approval_required', 'pending_step': {...}, ...}
        On error: {'error': '...'}
    """
    config_or_err = _load_config_for_mcp(
        dry_run=dry_run,
        incident_id=incident_id,
    )
    if isinstance(config_or_err, dict):
        return config_or_err
    config: Config = config_or_err

    try:
        path = Path(runbook_path)

        # Phase 1: parse + classify all steps
        steps = parser.parse_runbook(path)
        classified_steps = []
        for step in steps:
            step = classifier.classify_step(step, config)
            classified_steps.append(step)

        # Dry-run: return preview without executing
        if config.dry_run:
            return {
                "status": "dry_run",
                "total_steps": len(classified_steps),
                "steps": [_step_to_dict(s) for s in classified_steps],
                "message": (
                    "Dry-run complete. No commands were executed. "
                    "Call again with dry_run=False to execute for real."
                ),
            }

        # Slack path: delegate entirely to the existing executor
        if config.slack_enabled:
            from runbook_exec.executor import run_runbook
            summary = run_runbook(path, config)
            return {
                "status": "completed",
                "summary": {
                    "total_steps": summary.total_steps,
                    "successful": summary.successful,
                    "failed": summary.failed,
                    "skipped": summary.skipped,
                    "aborted": summary.aborted,
                    "audit_log_path": summary.audit_log_path,
                },
            }

        # No-Slack path: two-phase execution
        return _execute_no_slack(
            classified_steps=classified_steps,
            config=config,
            path=path,
            resume_from_step=resume_from_step,
            approved=approved,
        )

    except RunbookExecError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return {"error": f"Unexpected error: {exc}"}


def _execute_no_slack(
    classified_steps: list,
    config: Config,
    path: Path,
    resume_from_step: int,
    approved: bool | None,
) -> dict:
    """Two-phase execution for the no-Slack case.

    Runs steps sequentially. When a step needs approval:
    - If this is Phase 1 (no resume_from_step / approved), pause and return
      approval_required with the pending step details.
    - If this is Phase 2 (resume_from_step matches, approved is set), apply
      the decision and continue.

    Args:
        classified_steps: All steps, already classified.
        config: Runtime configuration.
        path: Runbook path (for audit log naming).
        resume_from_step: Step index to resume from (0 = start from beginning).
        approved: Approval decision for the step at resume_from_step.

    Returns:
        MCP response dict.
    """
    from runbook_exec.models import ActionType

    completed: list[dict] = []
    successful = 0
    failed = 0
    skipped = 0

    with create_audit_log(config, path) as audit_log:
        # Write PARSE entry
        audit_log.append({
            "action": ActionType.PARSE,
            "reasoning": f"Parsed {len(classified_steps)} steps from {path} (MCP)",
            "mode": "live",
        })

        for step in classified_steps:
            # Write CLASSIFY entry
            audit_log.append({
                "action": ActionType.CLASSIFY,
                "step_index": step.index,
                "step_text": step.text,
                "command": step.command,
                "risk_level": step.risk_level,
                "reasoning": step.classification_reasoning,
                "mode": "live",
            })

        for step in classified_steps:
            # Skip steps before the resume point
            if step.index < resume_from_step:
                skipped += 1
                completed.append({
                    "index": step.index,
                    "text": step.text,
                    "status": "skipped_before_resume",
                })
                continue

            # Steps with no command are skipped silently
            if step.command is None:
                skipped += 1
                audit_log.append({
                    "action": ActionType.SKIP,
                    "step_index": step.index,
                    "step_text": step.text,
                    "reasoning": "no command",
                    "mode": "live",
                })
                completed.append({
                    "index": step.index,
                    "text": step.text,
                    "status": "skipped",
                    "reason": "no command",
                })
                continue

            # Approval gate
            if needs_approval(step, config):
                if step.index == resume_from_step and approved is not None:
                    # Phase 2: apply the decision
                    if not approved:
                        skipped += 1
                        audit_log.append({
                            "action": ActionType.SKIP,
                            "step_index": step.index,
                            "step_text": step.text,
                            "reasoning": "denied by MCP client",
                            "mode": "live",
                        })
                        completed.append({
                            "index": step.index,
                            "text": step.text,
                            "status": "skipped",
                            "reason": "denied by operator",
                        })
                        continue
                    # approved=True: log and fall through to execution
                    audit_log.append({
                        "action": ActionType.APPROVE,
                        "step_index": step.index,
                        "step_text": step.text,
                        "command": step.command,
                        "risk_level": step.risk_level,
                        "reasoning": "approved by MCP client",
                        "mode": "live",
                    })
                else:
                    # Phase 1: pause and ask for approval
                    audit_log.append({
                        "action": ActionType.SUMMARY,
                        "reasoning": (
                            f"Paused at step {step.index} awaiting MCP approval"
                        ),
                        "mode": "live",
                    })
                    return {
                        "status": "approval_required",
                        "completed_steps": completed,
                        "pending_step": {
                            "index": step.index,
                            "text": step.text,
                            "command": step.command,
                            "risk_level": step.risk_level.value if step.risk_level else None,
                        },
                        "message": (
                            f"Step {step.index} requires approval before executing. "
                            f"Call execute_runbook again with "
                            f"resume_from_step={step.index} and approved=True to continue, "
                            f"or approved=False to skip this step."
                        ),
                    }

            # Execute the command
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

            step_result = _exec_result_to_dict(exec_result)
            if exec_result.exit_code == 0 and not exec_result.timed_out:
                successful += 1
                completed.append({
                    "index": step.index,
                    "text": step.text,
                    "status": "success",
                    "result": step_result,
                })
            else:
                failed += 1
                reason = "timed out" if exec_result.timed_out else f"exit code {exec_result.exit_code}"
                completed.append({
                    "index": step.index,
                    "text": step.text,
                    "status": "failed",
                    "reason": reason,
                    "result": step_result,
                })

        # Write summary
        audit_log.append({
            "action": ActionType.SUMMARY,
            "reasoning": (
                f"MCP execution complete: {successful} succeeded, "
                f"{failed} failed, {skipped} skipped"
            ),
            "mode": "live",
        })

    return {
        "status": "completed",
        "summary": {
            "total_steps": len(classified_steps),
            "successful": successful,
            "failed": failed,
            "skipped": skipped,
            "aborted": False,
            "audit_log_path": str(audit_log.path),
        },
        "completed_steps": completed,
    }


# ---------------------------------------------------------------------------
# Tool: replay_audit_log
# ---------------------------------------------------------------------------


@mcp.tool()
def replay_audit_log(audit_log_path: str) -> dict:
    """Read a previous execution audit log and verify its integrity.

    Returns all audit entries in chronological order plus hash chain status.

    Args:
        audit_log_path: Path to the NDJSON audit log file.

    Returns:
        Dict with 'entries', 'chain_intact', and 'chain_breaks' list.
    """
    try:
        path = Path(audit_log_path)
        entries = audit_module.load_log(path)
        chain_breaks = audit_module.verify_chain(entries)

        serialised = []
        for entry in entries:
            serialised.append({
                "seq": entry.seq,
                "action": entry.action.value,
                "timestamp": entry.timestamp,
                "step_index": entry.step_index,
                "step_text": entry.step_text,
                "command": entry.command,
                "risk_level": entry.risk_level.value if entry.risk_level else None,
                "exit_code": entry.exit_code,
                "duration_seconds": entry.duration_seconds,
                "approver_slack_id": entry.approver_slack_id,
                "reasoning": entry.reasoning,
                "mode": entry.mode,
            })

        return {
            "status": "ok",
            "total_entries": len(serialised),
            "chain_intact": len(chain_breaks) == 0,
            "chain_breaks": chain_breaks,
            "entries": serialised,
        }
    except RunbookExecError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return {"error": f"Unexpected error: {exc}"}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Start the runbook-exec MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
