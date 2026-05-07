"""Tests for runbook_exec.executor module.

All external I/O (classifier, shell, approval, LLM, audit) is mocked.
Tests cover the two-phase execution loop, direction handling, and LLM decisions.
"""

from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from runbook_exec.approval import ApprovalResult, FailureDirection
from runbook_exec.executor import LLMDecision, llm_decision, run_runbook
from runbook_exec.models import (
    ActionType,
    Config,
    ExecutionResult,
    ExecutionSummary,
    RiskLevel,
    Step,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_exec_result(exit_code: int = 0, stdout: str = "ok", stderr: str = "", timed_out: bool = False) -> ExecutionResult:
    return ExecutionResult(
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        duration_seconds=0.1,
        timed_out=timed_out,
    )


def _make_step(
    index: int = 1,
    text: str = "Test step",
    command: str | None = "echo test",
    risk_level: RiskLevel = RiskLevel.READ_ONLY,
) -> Step:
    return Step(
        index=index,
        section=None,
        text=text,
        command=command,
        risk_level=risk_level,
        classification_reasoning="test reasoning",
    )


def _make_config(**kwargs) -> Config:
    defaults = dict(
        llm_model="claude-sonnet-4-5",
        slack_channel="#test",
        slack_bot_token="xoxb-test",
        slack_app_token="xapp-test",
        timeout_seconds=10,
        auto_approve_level=None,
        audit_log_dir="./test-audit-logs",
        no_llm_context=False,
        dry_run=False,
        incident_id=None,
    )
    defaults.update(kwargs)
    return Config(**defaults)


def _make_mock_audit_log(tmp_path: Path) -> MagicMock:
    """Create a mock AuditLog that records appended entries."""
    mock_log = MagicMock()
    mock_log.path = tmp_path / "test-audit.json"
    mock_log.__enter__ = MagicMock(return_value=mock_log)
    mock_log.__exit__ = MagicMock(return_value=False)
    return mock_log


# ---------------------------------------------------------------------------
# Test 1: read_only step executes without approval
# ---------------------------------------------------------------------------


def test_read_only_step_executes_without_approval(tmp_path: Path) -> None:
    """A read_only step should run directly without calling request_approval."""
    step = _make_step(risk_level=RiskLevel.READ_ONLY)
    config = _make_config(no_llm_context=True)
    runbook_path = tmp_path / "runbook.md"

    mock_log = _make_mock_audit_log(tmp_path)

    with (
        patch("runbook_exec.executor.parser.parse_runbook", return_value=[step]),
        patch("runbook_exec.executor.classifier.classify_step", return_value=step),
        patch("runbook_exec.executor.create_audit_log", return_value=mock_log),
        patch("runbook_exec.executor.shell.run_command", return_value=_make_exec_result()) as mock_shell,
        patch("runbook_exec.executor.approval.request_approval") as mock_approval,
        patch("runbook_exec.executor.display.show_classification"),
        patch("runbook_exec.executor.display.show_step_running"),
        patch("runbook_exec.executor.display.show_step_success"),
        patch("runbook_exec.executor.display.show_summary"),
    ):
        summary = run_runbook(runbook_path, config)

    mock_approval.assert_not_called()
    mock_shell.assert_called_once_with(step.command, config.timeout_seconds)
    assert summary.successful == 1
    assert summary.failed == 0


# ---------------------------------------------------------------------------
# Test 2: modifying step triggers approval — approved → executes
# ---------------------------------------------------------------------------


def test_modifying_step_triggers_approval_approved(tmp_path: Path) -> None:
    """A modifying step should trigger approval; when approved, the command runs."""
    step = _make_step(risk_level=RiskLevel.MODIFYING)
    config = _make_config(no_llm_context=True)
    runbook_path = tmp_path / "runbook.md"

    mock_log = _make_mock_audit_log(tmp_path)
    approval_result = ApprovalResult(approved=True, approver_slack_id="U_TEST")

    with (
        patch("runbook_exec.executor.parser.parse_runbook", return_value=[step]),
        patch("runbook_exec.executor.classifier.classify_step", return_value=step),
        patch("runbook_exec.executor.create_audit_log", return_value=mock_log),
        patch("runbook_exec.executor.shell.run_command", return_value=_make_exec_result()) as mock_shell,
        patch("runbook_exec.executor.approval.request_approval", return_value=approval_result) as mock_approval,
        patch("runbook_exec.executor.display.show_classification"),
        patch("runbook_exec.executor.display.show_step_running"),
        patch("runbook_exec.executor.display.show_step_success"),
        patch("runbook_exec.executor.display.show_summary"),
    ):
        summary = run_runbook(runbook_path, config)

    mock_approval.assert_called_once_with(step, config)
    mock_shell.assert_called_once_with(step.command, config.timeout_seconds)
    assert summary.successful == 1
    assert summary.failed == 0


# ---------------------------------------------------------------------------
# Test 3: modifying step triggers approval — denied → direction prompt called
# ---------------------------------------------------------------------------


def test_modifying_step_triggers_approval_denied_then_direction(tmp_path: Path) -> None:
    """When a modifying step is denied, the failure direction prompt is called."""
    step = _make_step(risk_level=RiskLevel.MODIFYING)
    config = _make_config(no_llm_context=True)
    runbook_path = tmp_path / "runbook.md"

    mock_log = _make_mock_audit_log(tmp_path)
    denied_result = ApprovalResult(approved=False, approver_slack_id="U_DENIER")

    with (
        patch("runbook_exec.executor.parser.parse_runbook", return_value=[step]),
        patch("runbook_exec.executor.classifier.classify_step", return_value=step),
        patch("runbook_exec.executor.create_audit_log", return_value=mock_log),
        patch("runbook_exec.executor.shell.run_command") as mock_shell,
        patch("runbook_exec.executor.approval.request_approval", return_value=denied_result),
        patch(
            "runbook_exec.executor.approval.request_failure_direction",
            return_value=FailureDirection.SKIP,
        ) as mock_direction,
        patch("runbook_exec.executor.display.show_classification"),
        patch("runbook_exec.executor.display.show_summary"),
    ):
        summary = run_runbook(runbook_path, config)

    mock_direction.assert_called_once_with(
        step, "Step was denied", config, include_retry_warning=False
    )
    mock_shell.assert_not_called()
    assert summary.skipped == 1


# ---------------------------------------------------------------------------
# Test 4: dry-run — no shell calls, no Slack calls, all steps shown
# ---------------------------------------------------------------------------


def test_dry_run_no_shell_calls(tmp_path: Path) -> None:
    """In dry-run mode, no shell commands or Slack calls should be made."""
    steps = [
        _make_step(index=1, risk_level=RiskLevel.READ_ONLY),
        _make_step(index=2, risk_level=RiskLevel.MODIFYING),
        _make_step(index=3, risk_level=RiskLevel.DESTRUCTIVE),
    ]
    config = _make_config(dry_run=True, no_llm_context=True)
    runbook_path = tmp_path / "runbook.md"

    mock_log = _make_mock_audit_log(tmp_path)

    with (
        patch("runbook_exec.executor.parser.parse_runbook", return_value=steps),
        patch("runbook_exec.executor.classifier.classify_step", side_effect=steps),
        patch("runbook_exec.executor.create_audit_log", return_value=mock_log),
        patch("runbook_exec.executor.shell.run_command") as mock_shell,
        patch("runbook_exec.executor.approval.request_approval") as mock_approval,
        patch("runbook_exec.executor.display.show_classification"),
        patch("runbook_exec.executor.display.show_dry_run_step") as mock_dry_run,
        patch("runbook_exec.executor.display.show_summary"),
    ):
        summary = run_runbook(runbook_path, config)

    mock_shell.assert_not_called()
    mock_approval.assert_not_called()
    assert mock_dry_run.call_count == 3
    assert summary.total_steps == 3


# ---------------------------------------------------------------------------
# Test 5: dry-run still writes audit log (with mode=dry_run)
# ---------------------------------------------------------------------------


def test_dry_run_audit_log_written(tmp_path: Path) -> None:
    """Dry-run mode should still write audit log entries with mode='dry_run'."""
    step = _make_step(risk_level=RiskLevel.READ_ONLY)
    config = _make_config(dry_run=True, no_llm_context=True)
    runbook_path = tmp_path / "runbook.md"

    mock_log = _make_mock_audit_log(tmp_path)

    with (
        patch("runbook_exec.executor.parser.parse_runbook", return_value=[step]),
        patch("runbook_exec.executor.classifier.classify_step", return_value=step),
        patch("runbook_exec.executor.create_audit_log", return_value=mock_log),
        patch("runbook_exec.executor.display.show_classification"),
        patch("runbook_exec.executor.display.show_dry_run_step"),
        patch("runbook_exec.executor.display.show_summary"),
    ):
        run_runbook(runbook_path, config)

    # Audit log should have been used (PARSE + CLASSIFY + SUMMARY entries at minimum)
    assert mock_log.append.call_count >= 3

    # All entries should have mode=dry_run
    for call_args in mock_log.append.call_args_list:
        entry_data = call_args[0][0]
        assert entry_data.get("mode") == "dry_run", (
            f"Expected mode='dry_run' but got {entry_data.get('mode')} for action {entry_data.get('action')}"
        )


# ---------------------------------------------------------------------------
# Test 6: step failure triggers direction prompt
# ---------------------------------------------------------------------------


def test_step_failure_triggers_direction_prompt(tmp_path: Path) -> None:
    """A step that exits with non-zero code should trigger the failure direction prompt."""
    step = _make_step(risk_level=RiskLevel.READ_ONLY)
    config = _make_config(no_llm_context=True)
    runbook_path = tmp_path / "runbook.md"

    mock_log = _make_mock_audit_log(tmp_path)
    failed_result = _make_exec_result(exit_code=1, stderr="error output")

    with (
        patch("runbook_exec.executor.parser.parse_runbook", return_value=[step]),
        patch("runbook_exec.executor.classifier.classify_step", return_value=step),
        patch("runbook_exec.executor.create_audit_log", return_value=mock_log),
        patch("runbook_exec.executor.shell.run_command", return_value=failed_result),
        patch(
            "runbook_exec.executor.approval.request_failure_direction",
            return_value=FailureDirection.CONTINUE,
        ) as mock_direction,
        patch("runbook_exec.executor.display.show_classification"),
        patch("runbook_exec.executor.display.show_step_running"),
        patch("runbook_exec.executor.display.show_step_failure"),
        patch("runbook_exec.executor.display.show_summary"),
    ):
        summary = run_runbook(runbook_path, config)

    mock_direction.assert_called_once()
    # failed + continued = still counted as failed
    assert summary.failed == 1


# ---------------------------------------------------------------------------
# Test 7: step failure retry warning for destructive step
# ---------------------------------------------------------------------------


def test_step_failure_retry_warning_for_destructive(tmp_path: Path) -> None:
    """A destructive step failure should include include_retry_warning=True."""
    step = _make_step(risk_level=RiskLevel.DESTRUCTIVE)
    config = _make_config(no_llm_context=True, auto_approve_level=RiskLevel.DESTRUCTIVE)
    runbook_path = tmp_path / "runbook.md"

    mock_log = _make_mock_audit_log(tmp_path)
    failed_result = _make_exec_result(exit_code=1)

    with (
        patch("runbook_exec.executor.parser.parse_runbook", return_value=[step]),
        patch("runbook_exec.executor.classifier.classify_step", return_value=step),
        patch("runbook_exec.executor.create_audit_log", return_value=mock_log),
        patch("runbook_exec.executor.shell.run_command", return_value=failed_result),
        patch(
            "runbook_exec.executor.approval.request_failure_direction",
            return_value=FailureDirection.ABORT,
        ) as mock_direction,
        patch("runbook_exec.executor.display.show_classification"),
        patch("runbook_exec.executor.display.show_step_running"),
        patch("runbook_exec.executor.display.show_step_failure"),
        patch("runbook_exec.executor.display.show_summary"),
    ):
        summary = run_runbook(runbook_path, config)

    # include_retry_warning should be True for destructive steps
    mock_direction.assert_called_once()
    _, kwargs = mock_direction.call_args
    assert kwargs.get("include_retry_warning") is True or mock_direction.call_args[0][3] is True
    assert summary.aborted is True


# ---------------------------------------------------------------------------
# Test 8: LLM decision abort halts loop and writes ABORT entry
# ---------------------------------------------------------------------------


def test_llm_decision_abort_halts_loop(tmp_path: Path) -> None:
    """When the LLM decides to abort, execution halts and an ABORT entry is written."""
    steps = [
        _make_step(index=1, risk_level=RiskLevel.READ_ONLY),
        _make_step(index=2, risk_level=RiskLevel.READ_ONLY),
    ]
    config = _make_config(no_llm_context=False)
    runbook_path = tmp_path / "runbook.md"

    mock_log = _make_mock_audit_log(tmp_path)
    abort_decision = LLMDecision(action="abort", reasoning="System is in bad state")

    with (
        patch("runbook_exec.executor.parser.parse_runbook", return_value=steps),
        patch("runbook_exec.executor.classifier.classify_step", side_effect=steps),
        patch("runbook_exec.executor.create_audit_log", return_value=mock_log),
        patch("runbook_exec.executor.shell.run_command", return_value=_make_exec_result()),
        patch("runbook_exec.executor.llm_decision", return_value=abort_decision),
        patch("runbook_exec.executor.display.show_classification"),
        patch("runbook_exec.executor.display.show_step_running"),
        patch("runbook_exec.executor.display.show_step_success"),
        patch("runbook_exec.executor.display.show_summary"),
    ):
        summary = run_runbook(runbook_path, config)

    # Only step 1 should have run; step 2 should not
    assert summary.successful == 1
    assert summary.aborted is True

    # Verify ABORT entry was written
    abort_calls = [
        c for c in mock_log.append.call_args_list
        if c[0][0].get("action") == ActionType.ABORT
    ]
    assert len(abort_calls) == 1
    assert abort_calls[0][0][0]["reasoning"] == "System is in bad state"


# ---------------------------------------------------------------------------
# Test 9: LLM decision skip advances index and writes SKIP entries
# ---------------------------------------------------------------------------


def test_llm_decision_skip_advances_index(tmp_path: Path) -> None:
    """When the LLM decides to skip N steps, those steps are skipped with audit entries."""
    steps = [
        _make_step(index=1, risk_level=RiskLevel.READ_ONLY),
        _make_step(index=2, risk_level=RiskLevel.READ_ONLY),
        _make_step(index=3, risk_level=RiskLevel.READ_ONLY),
    ]
    config = _make_config(no_llm_context=False)
    runbook_path = tmp_path / "runbook.md"

    mock_log = _make_mock_audit_log(tmp_path)
    skip_decision = LLMDecision(action="skip", skip_count=2, reasoning="Steps 2 and 3 not needed")
    continue_decision = LLMDecision(action="continue", reasoning="All good")

    # First call (after step 1) returns skip; subsequent calls return continue
    llm_side_effects = [skip_decision, continue_decision]

    with (
        patch("runbook_exec.executor.parser.parse_runbook", return_value=steps),
        patch("runbook_exec.executor.classifier.classify_step", side_effect=steps),
        patch("runbook_exec.executor.create_audit_log", return_value=mock_log),
        patch("runbook_exec.executor.shell.run_command", return_value=_make_exec_result()),
        patch("runbook_exec.executor.llm_decision", side_effect=llm_side_effects),
        patch("runbook_exec.executor.display.show_classification"),
        patch("runbook_exec.executor.display.show_step_running"),
        patch("runbook_exec.executor.display.show_step_success"),
        patch("runbook_exec.executor.display.show_summary"),
    ):
        summary = run_runbook(runbook_path, config)

    assert summary.successful == 1
    assert summary.skipped == 2
    assert summary.aborted is False

    # Verify SKIP entries were written for steps 2 and 3
    skip_calls = [
        c for c in mock_log.append.call_args_list
        if c[0][0].get("action") == ActionType.SKIP
    ]
    assert len(skip_calls) == 2
    skipped_indices = {c[0][0]["step_index"] for c in skip_calls}
    assert skipped_indices == {2, 3}


# ---------------------------------------------------------------------------
# Test 10: no_llm_context=True skips post-step LLM call
# ---------------------------------------------------------------------------


def test_no_llm_context_skips_post_step_llm_call(tmp_path: Path) -> None:
    """When no_llm_context=True, the post-step LLM decision call is skipped."""
    step = _make_step(risk_level=RiskLevel.READ_ONLY)
    config = _make_config(no_llm_context=True)
    runbook_path = tmp_path / "runbook.md"

    mock_log = _make_mock_audit_log(tmp_path)

    with (
        patch("runbook_exec.executor.parser.parse_runbook", return_value=[step]),
        patch("runbook_exec.executor.classifier.classify_step", return_value=step),
        patch("runbook_exec.executor.create_audit_log", return_value=mock_log),
        patch("runbook_exec.executor.shell.run_command", return_value=_make_exec_result()),
        patch("runbook_exec.executor.llm_decision") as mock_llm,
        patch("runbook_exec.executor.display.show_classification"),
        patch("runbook_exec.executor.display.show_step_running"),
        patch("runbook_exec.executor.display.show_step_success"),
        patch("runbook_exec.executor.display.show_summary"),
    ):
        summary = run_runbook(runbook_path, config)

    mock_llm.assert_not_called()
    assert summary.successful == 1


# ---------------------------------------------------------------------------
# Test 11: second consecutive retry failure returns to direction prompt
# ---------------------------------------------------------------------------


def test_second_consecutive_retry_failure_returns_to_direction_prompt(tmp_path: Path) -> None:
    """After a retry also fails, the direction prompt is called again (not infinite loop)."""
    step = _make_step(risk_level=RiskLevel.READ_ONLY)
    config = _make_config(no_llm_context=True)
    runbook_path = tmp_path / "runbook.md"

    mock_log = _make_mock_audit_log(tmp_path)
    failed_result = _make_exec_result(exit_code=1)

    # First direction call: RETRY; second direction call: ABORT
    direction_side_effects = [FailureDirection.RETRY, FailureDirection.ABORT]

    with (
        patch("runbook_exec.executor.parser.parse_runbook", return_value=[step]),
        patch("runbook_exec.executor.classifier.classify_step", return_value=step),
        patch("runbook_exec.executor.create_audit_log", return_value=mock_log),
        patch("runbook_exec.executor.shell.run_command", return_value=failed_result),
        patch(
            "runbook_exec.executor.approval.request_failure_direction",
            side_effect=direction_side_effects,
        ) as mock_direction,
        patch("runbook_exec.executor.display.show_classification"),
        patch("runbook_exec.executor.display.show_step_running"),
        patch("runbook_exec.executor.display.show_step_failure"),
        patch("runbook_exec.executor.display.show_summary"),
    ):
        summary = run_runbook(runbook_path, config)

    # Direction prompt should have been called twice (initial failure + retry failure)
    assert mock_direction.call_count == 2
    assert summary.aborted is True

    # Second call should include "(retry also failed)" in the reason
    second_call_args = mock_direction.call_args_list[1]
    failure_reason = second_call_args[0][1]
    assert "retry also failed" in failure_reason


# ---------------------------------------------------------------------------
# Test 12: step with no command is skipped with audit entry
# ---------------------------------------------------------------------------


def test_step_with_no_command_is_skipped(tmp_path: Path) -> None:
    """A step with command=None should be skipped silently with a SKIP audit entry."""
    step = _make_step(command=None, risk_level=RiskLevel.READ_ONLY)
    config = _make_config(no_llm_context=True)
    runbook_path = tmp_path / "runbook.md"

    mock_log = _make_mock_audit_log(tmp_path)

    with (
        patch("runbook_exec.executor.parser.parse_runbook", return_value=[step]),
        patch("runbook_exec.executor.classifier.classify_step", return_value=step),
        patch("runbook_exec.executor.create_audit_log", return_value=mock_log),
        patch("runbook_exec.executor.shell.run_command") as mock_shell,
        patch("runbook_exec.executor.approval.request_approval") as mock_approval,
        patch("runbook_exec.executor.display.show_classification"),
        patch("runbook_exec.executor.display.show_step_skipped") as mock_skipped,
        patch("runbook_exec.executor.display.show_summary"),
    ):
        summary = run_runbook(runbook_path, config)

    mock_shell.assert_not_called()
    mock_approval.assert_not_called()
    mock_skipped.assert_called_once_with(step, reason="no command")
    assert summary.skipped == 1
    assert summary.successful == 0

    # Verify SKIP audit entry was written with reasoning="no command"
    skip_calls = [
        c for c in mock_log.append.call_args_list
        if c[0][0].get("action") == ActionType.SKIP
    ]
    assert len(skip_calls) == 1
    assert skip_calls[0][0][0]["reasoning"] == "no command"


# ---------------------------------------------------------------------------
# Test 13: SIGINT mid-execution writes ABORT entry
# ---------------------------------------------------------------------------


def test_sigint_mid_execution_writes_abort_entry(tmp_path: Path) -> None:
    """SIGINT mid-execution writes ABORT entry with reasoning='interrupted by SIGINT'."""
    import runbook_exec.interrupt as interrupt

    steps = [
        _make_step(index=1, risk_level=RiskLevel.READ_ONLY),
        _make_step(index=2, risk_level=RiskLevel.READ_ONLY),
    ]
    config = _make_config(no_llm_context=True)
    runbook_path = tmp_path / "runbook.md"

    mock_log = _make_mock_audit_log(tmp_path)

    # Set the interrupt flag before the second step runs by having shell.run_command
    # set it as a side effect on the first call.
    def run_command_and_interrupt(command, timeout):
        interrupt.set_interrupted()
        return _make_exec_result()

    try:
        with (
            patch("runbook_exec.executor.parser.parse_runbook", return_value=steps),
            patch("runbook_exec.executor.classifier.classify_step", side_effect=steps),
            patch("runbook_exec.executor.create_audit_log", return_value=mock_log),
            patch("runbook_exec.executor.shell.run_command", side_effect=run_command_and_interrupt),
            patch("runbook_exec.executor.display.show_classification"),
            patch("runbook_exec.executor.display.show_step_running"),
            patch("runbook_exec.executor.display.show_step_success"),
            patch("runbook_exec.executor.display.show_summary"),
        ):
            summary = run_runbook(runbook_path, config)
    finally:
        interrupt.reset()

    # Step 1 ran successfully; step 2 was aborted due to SIGINT
    assert summary.successful == 1
    assert summary.aborted is True

    # Verify ABORT entry was written with the correct reasoning
    abort_calls = [
        c for c in mock_log.append.call_args_list
        if c[0][0].get("action") == ActionType.ABORT
    ]
    assert len(abort_calls) == 1
    assert abort_calls[0][0][0]["reasoning"] == "interrupted by SIGINT"


# ---------------------------------------------------------------------------
# Test 14: SIGINT during subprocess — shell returns exit_code=-1
# ---------------------------------------------------------------------------


def test_sigint_during_subprocess_shell_returns_exit_code_minus_one(tmp_path: Path) -> None:
    """SIGINT during subprocess: shell.run_command returns exit_code=-1 on KeyboardInterrupt."""
    import subprocess
    import time
    from runbook_exec.shell import run_command
    from runbook_exec.models import ExecutionResult

    # Patch Popen so that proc.communicate() raises KeyboardInterrupt
    mock_proc = MagicMock()
    mock_proc.communicate.side_effect = [KeyboardInterrupt, ("", "")]
    mock_proc.returncode = -1

    with patch("subprocess.Popen", return_value=mock_proc):
        result = run_command("echo test", timeout_seconds=10)

    assert result.exit_code == -1
    assert result.timed_out is False


# ---------------------------------------------------------------------------
# Test 15: llm_decision function — direct call with mocked LLM
# ---------------------------------------------------------------------------


def test_llm_decision_returns_continue_on_success(tmp_path: Path) -> None:
    """llm_decision returns a continue decision when LLM responds with continue."""
    from runbook_exec.executor import llm_decision

    step = _make_step(risk_level=RiskLevel.READ_ONLY)
    exec_result = _make_exec_result(exit_code=0, stdout="ok")
    config = _make_config()
    runbook_path = tmp_path / "runbook.md"

    with patch(
        "runbook_exec.executor.call_llm",
        return_value='{"action": "continue", "skip_count": null, "reasoning": "All good"}',
    ):
        decision = llm_decision(step, exec_result, [], config, runbook_path)

    assert decision.action == "continue"
    assert decision.reasoning == "All good"


def test_llm_decision_returns_abort(tmp_path: Path) -> None:
    """llm_decision returns an abort decision when LLM responds with abort."""
    from runbook_exec.executor import llm_decision

    step = _make_step(risk_level=RiskLevel.READ_ONLY)
    exec_result = _make_exec_result(exit_code=0)
    config = _make_config()
    runbook_path = tmp_path / "runbook.md"

    with patch(
        "runbook_exec.executor.call_llm",
        return_value='{"action": "abort", "skip_count": null, "reasoning": "System unstable"}',
    ):
        decision = llm_decision(step, exec_result, [], config, runbook_path)

    assert decision.action == "abort"
    assert decision.reasoning == "System unstable"


def test_llm_decision_returns_skip_with_count(tmp_path: Path) -> None:
    """llm_decision returns a skip decision with skip_count when LLM responds with skip."""
    from runbook_exec.executor import llm_decision

    step = _make_step(risk_level=RiskLevel.READ_ONLY)
    exec_result = _make_exec_result(exit_code=0)
    remaining = [_make_step(index=2), _make_step(index=3)]
    config = _make_config()
    runbook_path = tmp_path / "runbook.md"

    with patch(
        "runbook_exec.executor.call_llm",
        return_value='{"action": "skip", "skip_count": 2, "reasoning": "Steps not needed"}',
    ):
        decision = llm_decision(step, exec_result, remaining, config, runbook_path)

    assert decision.action == "skip"
    assert decision.skip_count == 2


def test_llm_decision_defaults_to_continue_on_unparseable_response(tmp_path: Path) -> None:
    """llm_decision defaults to continue when LLM returns unparseable JSON."""
    from runbook_exec.executor import llm_decision

    step = _make_step(risk_level=RiskLevel.READ_ONLY)
    exec_result = _make_exec_result(exit_code=0)
    config = _make_config()
    runbook_path = tmp_path / "runbook.md"

    with patch(
        "runbook_exec.executor.call_llm",
        return_value="not valid json at all",
    ):
        decision = llm_decision(step, exec_result, [], config, runbook_path)

    assert decision.action == "continue"
    assert "unparseable" in decision.reasoning.lower() or "default" in decision.reasoning.lower()


def test_llm_decision_defaults_to_continue_on_llm_error(tmp_path: Path) -> None:
    """llm_decision defaults to continue when call_llm raises an exception."""
    from runbook_exec.executor import llm_decision
    from runbook_exec.exceptions import LLMError

    step = _make_step(risk_level=RiskLevel.READ_ONLY)
    exec_result = _make_exec_result(exit_code=0)
    config = _make_config()
    runbook_path = tmp_path / "runbook.md"

    with patch(
        "runbook_exec.executor.call_llm",
        side_effect=LLMError("API failure"),
    ):
        decision = llm_decision(step, exec_result, [], config, runbook_path)

    assert decision.action == "continue"


# ---------------------------------------------------------------------------
# Test 16: approval timeout — retry path
# ---------------------------------------------------------------------------


def test_approval_timeout_retry_then_abort(tmp_path: Path) -> None:
    """Approval timeout with retry direction re-runs the step; second timeout aborts."""
    step = _make_step(risk_level=RiskLevel.MODIFYING)
    config = _make_config(no_llm_context=True)
    runbook_path = tmp_path / "runbook.md"

    mock_log = _make_mock_audit_log(tmp_path)
    timed_out_result = ApprovalResult(approved=False, approver_slack_id=None, timed_out=True)

    # First timeout → RETRY; second timeout → ABORT
    direction_side_effects = [FailureDirection.RETRY, FailureDirection.ABORT]

    with (
        patch("runbook_exec.executor.parser.parse_runbook", return_value=[step]),
        patch("runbook_exec.executor.classifier.classify_step", return_value=step),
        patch("runbook_exec.executor.create_audit_log", return_value=mock_log),
        patch("runbook_exec.executor.shell.run_command") as mock_shell,
        patch("runbook_exec.executor.approval.request_approval", return_value=timed_out_result),
        patch(
            "runbook_exec.executor.approval.request_failure_direction",
            side_effect=direction_side_effects,
        ) as mock_direction,
        patch("runbook_exec.executor.display.show_classification"),
        patch("runbook_exec.executor.display.show_summary"),
    ):
        summary = run_runbook(runbook_path, config)

    assert mock_direction.call_count == 2
    mock_shell.assert_not_called()
    assert summary.aborted is True


def test_approval_timeout_skip_direction(tmp_path: Path) -> None:
    """Approval timeout with skip direction skips the step and continues."""
    step = _make_step(risk_level=RiskLevel.MODIFYING)
    config = _make_config(no_llm_context=True)
    runbook_path = tmp_path / "runbook.md"

    mock_log = _make_mock_audit_log(tmp_path)
    timed_out_result = ApprovalResult(approved=False, approver_slack_id=None, timed_out=True)

    with (
        patch("runbook_exec.executor.parser.parse_runbook", return_value=[step]),
        patch("runbook_exec.executor.classifier.classify_step", return_value=step),
        patch("runbook_exec.executor.create_audit_log", return_value=mock_log),
        patch("runbook_exec.executor.shell.run_command") as mock_shell,
        patch("runbook_exec.executor.approval.request_approval", return_value=timed_out_result),
        patch(
            "runbook_exec.executor.approval.request_failure_direction",
            return_value=FailureDirection.SKIP,
        ),
        patch("runbook_exec.executor.display.show_classification"),
        patch("runbook_exec.executor.display.show_summary"),
    ):
        summary = run_runbook(runbook_path, config)

    mock_shell.assert_not_called()
    assert summary.skipped == 1
    assert summary.aborted is False


def test_approval_timeout_continue_direction(tmp_path: Path) -> None:
    """Approval timeout with continue direction counts as failed and continues."""
    step = _make_step(risk_level=RiskLevel.MODIFYING)
    config = _make_config(no_llm_context=True)
    runbook_path = tmp_path / "runbook.md"

    mock_log = _make_mock_audit_log(tmp_path)
    timed_out_result = ApprovalResult(approved=False, approver_slack_id=None, timed_out=True)

    with (
        patch("runbook_exec.executor.parser.parse_runbook", return_value=[step]),
        patch("runbook_exec.executor.classifier.classify_step", return_value=step),
        patch("runbook_exec.executor.create_audit_log", return_value=mock_log),
        patch("runbook_exec.executor.shell.run_command") as mock_shell,
        patch("runbook_exec.executor.approval.request_approval", return_value=timed_out_result),
        patch(
            "runbook_exec.executor.approval.request_failure_direction",
            return_value=FailureDirection.CONTINUE,
        ),
        patch("runbook_exec.executor.display.show_classification"),
        patch("runbook_exec.executor.display.show_summary"),
    ):
        summary = run_runbook(runbook_path, config)

    mock_shell.assert_not_called()
    assert summary.failed == 1
    assert summary.aborted is False


# ---------------------------------------------------------------------------
# Test 17: denied approval — retry and continue paths
# ---------------------------------------------------------------------------


def test_denied_approval_retry_then_approved(tmp_path: Path) -> None:
    """Denied approval with retry direction re-runs the approval; second time approved."""
    step = _make_step(risk_level=RiskLevel.MODIFYING)
    config = _make_config(no_llm_context=True)
    runbook_path = tmp_path / "runbook.md"

    mock_log = _make_mock_audit_log(tmp_path)
    denied_result = ApprovalResult(approved=False, approver_slack_id="U_DENIER")
    approved_result = ApprovalResult(approved=True, approver_slack_id="U_APPROVER")

    # First approval: denied; second: approved
    approval_side_effects = [denied_result, approved_result]

    with (
        patch("runbook_exec.executor.parser.parse_runbook", return_value=[step]),
        patch("runbook_exec.executor.classifier.classify_step", return_value=step),
        patch("runbook_exec.executor.create_audit_log", return_value=mock_log),
        patch("runbook_exec.executor.shell.run_command", return_value=_make_exec_result()),
        patch("runbook_exec.executor.approval.request_approval", side_effect=approval_side_effects),
        patch(
            "runbook_exec.executor.approval.request_failure_direction",
            return_value=FailureDirection.RETRY,
        ),
        patch("runbook_exec.executor.display.show_classification"),
        patch("runbook_exec.executor.display.show_step_running"),
        patch("runbook_exec.executor.display.show_step_success"),
        patch("runbook_exec.executor.display.show_summary"),
    ):
        summary = run_runbook(runbook_path, config)

    assert summary.successful == 1
    assert summary.failed == 0


def test_denied_approval_continue_direction_counts_as_failed(tmp_path: Path) -> None:
    """Denied approval with continue direction counts the step as failed."""
    step = _make_step(risk_level=RiskLevel.MODIFYING)
    config = _make_config(no_llm_context=True)
    runbook_path = tmp_path / "runbook.md"

    mock_log = _make_mock_audit_log(tmp_path)
    denied_result = ApprovalResult(approved=False, approver_slack_id="U_DENIER")

    with (
        patch("runbook_exec.executor.parser.parse_runbook", return_value=[step]),
        patch("runbook_exec.executor.classifier.classify_step", return_value=step),
        patch("runbook_exec.executor.create_audit_log", return_value=mock_log),
        patch("runbook_exec.executor.shell.run_command") as mock_shell,
        patch("runbook_exec.executor.approval.request_approval", return_value=denied_result),
        patch(
            "runbook_exec.executor.approval.request_failure_direction",
            return_value=FailureDirection.CONTINUE,
        ),
        patch("runbook_exec.executor.display.show_classification"),
        patch("runbook_exec.executor.display.show_summary"),
    ):
        summary = run_runbook(runbook_path, config)

    mock_shell.assert_not_called()
    assert summary.failed == 1
    assert summary.aborted is False


# ---------------------------------------------------------------------------
# Test 18: step failure retry path (i -= 1 branch)
# ---------------------------------------------------------------------------


def test_step_failure_retry_reruns_step(tmp_path: Path) -> None:
    """After a step failure with RETRY direction, the same step is re-run."""
    step = _make_step(risk_level=RiskLevel.READ_ONLY)
    config = _make_config(no_llm_context=True)
    runbook_path = tmp_path / "runbook.md"

    mock_log = _make_mock_audit_log(tmp_path)
    failed_result = _make_exec_result(exit_code=1)
    success_result = _make_exec_result(exit_code=0)

    # First run: fail; second run: succeed
    shell_side_effects = [failed_result, success_result]
    # First direction: RETRY; no second direction needed (step succeeds)
    direction_side_effects = [FailureDirection.RETRY]

    with (
        patch("runbook_exec.executor.parser.parse_runbook", return_value=[step]),
        patch("runbook_exec.executor.classifier.classify_step", return_value=step),
        patch("runbook_exec.executor.create_audit_log", return_value=mock_log),
        patch("runbook_exec.executor.shell.run_command", side_effect=shell_side_effects),
        patch(
            "runbook_exec.executor.approval.request_failure_direction",
            side_effect=direction_side_effects,
        ),
        patch("runbook_exec.executor.display.show_classification"),
        patch("runbook_exec.executor.display.show_step_running"),
        patch("runbook_exec.executor.display.show_step_success"),
        patch("runbook_exec.executor.display.show_step_failure"),
        patch("runbook_exec.executor.display.show_summary"),
    ):
        summary = run_runbook(runbook_path, config)

    assert summary.successful == 1
    assert summary.failed == 0


# ---------------------------------------------------------------------------
# Test 19: denied approval — skip and failed (continue) paths
# ---------------------------------------------------------------------------


def test_denied_approval_skip_direction(tmp_path: Path) -> None:
    """Denied approval with skip direction increments skipped count."""
    step = _make_step(risk_level=RiskLevel.MODIFYING)
    config = _make_config(no_llm_context=True)
    runbook_path = tmp_path / "runbook.md"

    mock_log = _make_mock_audit_log(tmp_path)
    denied_result = ApprovalResult(approved=False, approver_slack_id="U_DENIER")

    with (
        patch("runbook_exec.executor.parser.parse_runbook", return_value=[step]),
        patch("runbook_exec.executor.classifier.classify_step", return_value=step),
        patch("runbook_exec.executor.create_audit_log", return_value=mock_log),
        patch("runbook_exec.executor.shell.run_command") as mock_shell,
        patch("runbook_exec.executor.approval.request_approval", return_value=denied_result),
        patch(
            "runbook_exec.executor.approval.request_failure_direction",
            return_value=FailureDirection.SKIP,
        ),
        patch("runbook_exec.executor.display.show_classification"),
        patch("runbook_exec.executor.display.show_summary"),
    ):
        summary = run_runbook(runbook_path, config)

    mock_shell.assert_not_called()
    assert summary.skipped == 1
    assert summary.failed == 0
    assert summary.aborted is False


# ---------------------------------------------------------------------------
# Test 20: step failure skip direction (failed -= 1, skipped += 1)
# ---------------------------------------------------------------------------


def test_step_failure_skip_direction_adjusts_counts(tmp_path: Path) -> None:
    """Step failure with SKIP direction: failed count decremented, skipped incremented."""
    step = _make_step(risk_level=RiskLevel.READ_ONLY)
    config = _make_config(no_llm_context=True)
    runbook_path = tmp_path / "runbook.md"

    mock_log = _make_mock_audit_log(tmp_path)
    failed_result = _make_exec_result(exit_code=1)

    with (
        patch("runbook_exec.executor.parser.parse_runbook", return_value=[step]),
        patch("runbook_exec.executor.classifier.classify_step", return_value=step),
        patch("runbook_exec.executor.create_audit_log", return_value=mock_log),
        patch("runbook_exec.executor.shell.run_command", return_value=failed_result),
        patch(
            "runbook_exec.executor.approval.request_failure_direction",
            return_value=FailureDirection.SKIP,
        ),
        patch("runbook_exec.executor.display.show_classification"),
        patch("runbook_exec.executor.display.show_step_running"),
        patch("runbook_exec.executor.display.show_step_failure"),
        patch("runbook_exec.executor.display.show_summary"),
    ):
        summary = run_runbook(runbook_path, config)

    assert summary.failed == 0
    assert summary.skipped == 1
    assert summary.aborted is False


# ---------------------------------------------------------------------------
# Test 21: denied approval — abort path
# ---------------------------------------------------------------------------


def test_denied_approval_abort_direction(tmp_path: Path) -> None:
    """Denied approval with abort direction sets aborted=True and breaks the loop."""
    step = _make_step(risk_level=RiskLevel.MODIFYING)
    config = _make_config(no_llm_context=True)
    runbook_path = tmp_path / "runbook.md"

    mock_log = _make_mock_audit_log(tmp_path)
    denied_result = ApprovalResult(approved=False, approver_slack_id="U_DENIER")

    with (
        patch("runbook_exec.executor.parser.parse_runbook", return_value=[step]),
        patch("runbook_exec.executor.classifier.classify_step", return_value=step),
        patch("runbook_exec.executor.create_audit_log", return_value=mock_log),
        patch("runbook_exec.executor.shell.run_command") as mock_shell,
        patch("runbook_exec.executor.approval.request_approval", return_value=denied_result),
        patch(
            "runbook_exec.executor.approval.request_failure_direction",
            return_value=FailureDirection.ABORT,
        ),
        patch("runbook_exec.executor.display.show_classification"),
        patch("runbook_exec.executor.display.show_summary"),
    ):
        summary = run_runbook(runbook_path, config)

    mock_shell.assert_not_called()
    assert summary.aborted is True


# ---------------------------------------------------------------------------
# Approval mode banner tests
# ---------------------------------------------------------------------------


def test_approval_mode_banner_shown_with_slack(tmp_path: Path) -> None:
    """When Slack is configured, the Slack approval banner is shown at startup."""
    step = _make_step(risk_level=RiskLevel.READ_ONLY)
    config = _make_config(no_llm_context=True)  # has slack tokens
    runbook_path = tmp_path / "runbook.md"
    mock_log = _make_mock_audit_log(tmp_path)

    with (
        patch("runbook_exec.executor.parser.parse_runbook", return_value=[step]),
        patch("runbook_exec.executor.classifier.classify_step", return_value=step),
        patch("runbook_exec.executor.create_audit_log", return_value=mock_log),
        patch("runbook_exec.executor.shell.run_command", return_value=_make_exec_result()),
        patch("runbook_exec.executor.display.show_classification"),
        patch("runbook_exec.executor.display.show_step_running"),
        patch("runbook_exec.executor.display.show_step_success"),
        patch("runbook_exec.executor.display.show_summary"),
        patch("runbook_exec.executor.display.show_approval_mode_banner") as mock_banner,
    ):
        run_runbook(runbook_path, config)

    mock_banner.assert_called_once_with(True)


def test_approval_mode_banner_shown_without_slack(tmp_path: Path) -> None:
    """When Slack is not configured, the terminal fallback banner is shown at startup."""
    step = _make_step(risk_level=RiskLevel.READ_ONLY)
    config = _make_config(no_llm_context=True, slack_bot_token="", slack_app_token="")
    runbook_path = tmp_path / "runbook.md"
    mock_log = _make_mock_audit_log(tmp_path)

    with (
        patch("runbook_exec.executor.parser.parse_runbook", return_value=[step]),
        patch("runbook_exec.executor.classifier.classify_step", return_value=step),
        patch("runbook_exec.executor.create_audit_log", return_value=mock_log),
        patch("runbook_exec.executor.shell.run_command", return_value=_make_exec_result()),
        patch("runbook_exec.executor.display.show_classification"),
        patch("runbook_exec.executor.display.show_step_running"),
        patch("runbook_exec.executor.display.show_step_success"),
        patch("runbook_exec.executor.display.show_summary"),
        patch("runbook_exec.executor.display.show_approval_mode_banner") as mock_banner,
    ):
        run_runbook(runbook_path, config)

    mock_banner.assert_called_once_with(False)


def test_banner_not_shown_in_dry_run(tmp_path: Path) -> None:
    """The approval mode banner is suppressed in dry-run mode."""
    step = _make_step(risk_level=RiskLevel.READ_ONLY)
    config = _make_config(dry_run=True, no_llm_context=True)
    runbook_path = tmp_path / "runbook.md"
    mock_log = _make_mock_audit_log(tmp_path)

    with (
        patch("runbook_exec.executor.parser.parse_runbook", return_value=[step]),
        patch("runbook_exec.executor.classifier.classify_step", return_value=step),
        patch("runbook_exec.executor.create_audit_log", return_value=mock_log),
        patch("runbook_exec.executor.display.show_classification"),
        patch("runbook_exec.executor.display.show_dry_run_step"),
        patch("runbook_exec.executor.display.show_summary"),
        patch("runbook_exec.executor.display.show_approval_mode_banner") as mock_banner,
    ):
        run_runbook(runbook_path, config)

    mock_banner.assert_not_called()


def test_failing_step_uses_terminal_fallback_without_slack(tmp_path: Path) -> None:
    """Without Slack tokens, a failing step uses terminal prompts instead of crashing."""
    step = _make_step(risk_level=RiskLevel.READ_ONLY)
    config = _make_config(no_llm_context=True, slack_bot_token="", slack_app_token="")
    runbook_path = tmp_path / "runbook.md"
    mock_log = _make_mock_audit_log(tmp_path)
    failed_result = _make_exec_result(exit_code=1, stderr="disk full")

    with (
        patch("runbook_exec.executor.parser.parse_runbook", return_value=[step]),
        patch("runbook_exec.executor.classifier.classify_step", return_value=step),
        patch("runbook_exec.executor.create_audit_log", return_value=mock_log),
        patch("runbook_exec.executor.shell.run_command", return_value=failed_result),
        patch("runbook_exec.executor.display.show_classification"),
        patch("runbook_exec.executor.display.show_step_running"),
        patch("runbook_exec.executor.display.show_step_failure"),
        patch("runbook_exec.executor.display.show_summary"),
        patch("runbook_exec.executor.display.show_approval_mode_banner"),
        # Terminal fallback: operator chooses "skip"
        patch("runbook_exec.approval.Prompt.ask", return_value="skip"),
    ):
        summary = run_runbook(runbook_path, config)

    # Should have skipped cleanly — no crash, no Slack call
    assert summary.skipped == 1
    assert summary.failed == 0


# ---------------------------------------------------------------------------
# Post-step LLM decision: markdown fence stripping
# ---------------------------------------------------------------------------


def test_llm_decision_parses_fenced_json_with_language_tag(tmp_path: Path) -> None:
    """Post-step LLM response wrapped in ```json...``` fences is parsed correctly."""
    from runbook_exec.executor import llm_decision

    step = _make_step(risk_level=RiskLevel.READ_ONLY)
    exec_result = _make_exec_result(exit_code=0)
    config = _make_config()
    runbook_path = tmp_path / "runbook.md"

    fenced_response = '```json\n{"action": "abort", "skip_count": null, "reasoning": "System unstable"}\n```'

    with patch("runbook_exec.executor.call_llm", return_value=fenced_response):
        decision = llm_decision(step, exec_result, [], config, runbook_path)

    # Must parse correctly — NOT fall back to "continue"
    assert decision.action == "abort"
    assert decision.reasoning == "System unstable"


def test_llm_decision_parses_fenced_json_without_language_tag(tmp_path: Path) -> None:
    """Post-step LLM response wrapped in plain ```...``` fences is parsed correctly."""
    from runbook_exec.executor import llm_decision

    step = _make_step(risk_level=RiskLevel.READ_ONLY)
    exec_result = _make_exec_result(exit_code=0)
    config = _make_config()
    runbook_path = tmp_path / "runbook.md"

    fenced_response = '```\n{"action": "skip", "skip_count": 2, "reasoning": "Steps not needed"}\n```'

    with patch("runbook_exec.executor.call_llm", return_value=fenced_response):
        decision = llm_decision(step, exec_result, [], config, runbook_path)

    assert decision.action == "skip"
    assert decision.skip_count == 2
    assert decision.reasoning == "Steps not needed"


def test_llm_decision_fallback_only_on_genuinely_malformed(tmp_path: Path) -> None:
    """The 'defaulting to continue' fallback only fires for truly unparseable responses."""
    from runbook_exec.executor import llm_decision

    step = _make_step(risk_level=RiskLevel.READ_ONLY)
    exec_result = _make_exec_result(exit_code=0)
    config = _make_config()
    runbook_path = tmp_path / "runbook.md"

    # Genuinely malformed — not JSON at all, not fenced JSON
    with patch("runbook_exec.executor.call_llm", return_value="Sure! I think you should continue."):
        decision = llm_decision(step, exec_result, [], config, runbook_path)

    assert decision.action == "continue"
    assert "unparseable" in decision.reasoning.lower() or "default" in decision.reasoning.lower()
