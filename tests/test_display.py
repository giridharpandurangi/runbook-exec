"""Tests for the display module.

Uses rich's Console(file=StringIO()) to capture terminal output without
actually writing to stdout, so assertions can be made on the rendered text.
"""

from io import StringIO

from runbook_exec.display import (
    _risk_badge,
    show_classification,
    show_dry_run_step,
    show_error,
    show_replay,
    show_step_failure,
    show_step_running,
    show_step_skipped,
    show_step_success,
    show_summary,
    show_warning,
)
from runbook_exec.models import (
    ActionType,
    AuditEntry,
    ExecutionResult,
    ExecutionSummary,
    RiskLevel,
    Step,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _capture(fn, *args, **kwargs) -> str:
    """Run a display function with a StringIO-backed Console and return output."""
    import runbook_exec.display as display_module

    buf = StringIO()
    from rich.console import Console

    original_console = display_module.console
    display_module.console = Console(file=buf, highlight=False, markup=True)
    try:
        fn(*args, **kwargs)
    finally:
        display_module.console = original_console
    return buf.getvalue()


def _make_step(
    index: int = 1,
    text: str = "Check disk space",
    command: str | None = "df -h",
    risk_level: RiskLevel | None = RiskLevel.READ_ONLY,
    section: str | None = None,
    classification_reasoning: str | None = None,
) -> Step:
    return Step(
        index=index,
        text=text,
        command=command,
        risk_level=risk_level,
        section=section,
        classification_reasoning=classification_reasoning,
    )


def _make_result(
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
    duration_seconds: float = 0.5,
    timed_out: bool = False,
) -> ExecutionResult:
    return ExecutionResult(
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        duration_seconds=duration_seconds,
        timed_out=timed_out,
    )


def _make_audit_entry(
    seq: int,
    action: ActionType = ActionType.EXECUTE,
    step_index: int | None = 1,
    step_text: str | None = "Check disk space",
    command: str | None = "df -h",
    risk_level: RiskLevel | None = RiskLevel.READ_ONLY,
    exit_code: int | None = 0,
    reasoning: str | None = None,
    prev_hash: str | None = None,
    hash_val: str = "abc123",
) -> AuditEntry:
    return AuditEntry(
        seq=seq,
        action=action,
        timestamp="2024-01-15T03:00:00Z",
        step_index=step_index,
        step_text=step_text,
        command=command,
        risk_level=risk_level,
        exit_code=exit_code,
        reasoning=reasoning,
        prev_hash=prev_hash,
        hash=hash_val,
    )


# ---------------------------------------------------------------------------
# _risk_badge — color styling
# ---------------------------------------------------------------------------


def test_risk_badge_read_only_is_green():
    badge = _risk_badge(RiskLevel.READ_ONLY)
    assert "green" in badge
    assert "read_only" in badge


def test_risk_badge_modifying_is_yellow():
    badge = _risk_badge(RiskLevel.MODIFYING)
    assert "yellow" in badge
    assert "modifying" in badge


def test_risk_badge_destructive_is_red_bold():
    badge = _risk_badge(RiskLevel.DESTRUCTIVE)
    assert "red" in badge
    assert "bold" in badge
    assert "destructive" in badge


def test_risk_badge_none_returns_unclassified():
    badge = _risk_badge(None)
    assert "unclassified" in badge


# ---------------------------------------------------------------------------
# show_step_running
# ---------------------------------------------------------------------------


def test_show_step_running_contains_step_text():
    step = _make_step(index=2, text="Restart the service")
    output = _capture(show_step_running, step)
    assert "Restart the service" in output
    assert "Step 2" in output


def test_show_step_running_shows_command():
    step = _make_step(command="systemctl restart nginx")
    output = _capture(show_step_running, step)
    assert "systemctl restart nginx" in output


def test_show_step_running_no_command():
    step = _make_step(command=None)
    output = _capture(show_step_running, step)
    # Should not crash and should still show step text
    assert "Check disk space" in output


# ---------------------------------------------------------------------------
# show_step_success
# ---------------------------------------------------------------------------


def test_show_step_success_contains_step_text():
    step = _make_step(index=1, text="Check disk space")
    result = _make_result(stdout="Filesystem 100G", exit_code=0)
    output = _capture(show_step_success, step, result)
    assert "Check disk space" in output


def test_show_step_success_shows_duration():
    step = _make_step()
    result = _make_result(duration_seconds=1.23)
    output = _capture(show_step_success, step, result)
    assert "1.23s" in output


def test_show_step_success_shows_stdout_when_present():
    step = _make_step()
    result = _make_result(stdout="some output here")
    output = _capture(show_step_success, step, result)
    assert "some output here" in output


def test_show_step_success_no_stdout_no_crash():
    step = _make_step()
    result = _make_result(stdout="")
    output = _capture(show_step_success, step, result)
    assert "Check disk space" in output


# ---------------------------------------------------------------------------
# show_step_failure
# ---------------------------------------------------------------------------


def test_show_step_failure_contains_step_text():
    step = _make_step(text="Delete old logs")
    result = _make_result(exit_code=1, stderr="Permission denied")
    output = _capture(show_step_failure, step, result)
    assert "Delete old logs" in output


def test_show_step_failure_shows_exit_code():
    step = _make_step()
    result = _make_result(exit_code=127)
    output = _capture(show_step_failure, step, result)
    assert "127" in output


def test_show_step_failure_shows_timed_out():
    step = _make_step()
    result = _make_result(exit_code=1, timed_out=True)
    output = _capture(show_step_failure, step, result)
    assert "timed out" in output


def test_show_step_failure_shows_stderr():
    step = _make_step()
    result = _make_result(exit_code=1, stderr="command not found")
    output = _capture(show_step_failure, step, result)
    assert "command not found" in output


# ---------------------------------------------------------------------------
# show_step_skipped
# ---------------------------------------------------------------------------


def test_show_step_skipped_contains_step_text():
    step = _make_step(text="Optional cleanup")
    output = _capture(show_step_skipped, step, "no command")
    assert "Optional cleanup" in output


def test_show_step_skipped_contains_reason():
    step = _make_step()
    output = _capture(show_step_skipped, step, "operator skipped")
    assert "operator skipped" in output


# ---------------------------------------------------------------------------
# show_dry_run_step
# ---------------------------------------------------------------------------


def test_show_dry_run_step_contains_step_text():
    step = _make_step(text="Drop the database")
    output = _capture(show_dry_run_step, step)
    assert "Drop the database" in output


def test_show_dry_run_step_shows_dry_run_label():
    step = _make_step()
    output = _capture(show_dry_run_step, step)
    assert "dry-run" in output.lower()


def test_show_dry_run_step_shows_command():
    step = _make_step(command="rm -rf /tmp/cache")
    output = _capture(show_dry_run_step, step)
    assert "rm -rf /tmp/cache" in output


# ---------------------------------------------------------------------------
# show_classification
# ---------------------------------------------------------------------------


def test_show_classification_shows_risk_level():
    step = _make_step(risk_level=RiskLevel.DESTRUCTIVE)
    output = _capture(show_classification, step)
    assert "destructive" in output


def test_show_classification_shows_reasoning():
    step = _make_step(
        risk_level=RiskLevel.MODIFYING,
        classification_reasoning="Restarts a service",
    )
    output = _capture(show_classification, step)
    assert "Restarts a service" in output


def test_show_classification_no_reasoning():
    step = _make_step(risk_level=RiskLevel.READ_ONLY, classification_reasoning=None)
    output = _capture(show_classification, step)
    assert "read_only" in output


# ---------------------------------------------------------------------------
# show_summary
# ---------------------------------------------------------------------------


def test_show_summary_includes_successful_count():
    summary = ExecutionSummary(total_steps=5, successful=3, failed=1, skipped=1)
    output = _capture(show_summary, summary)
    assert "3" in output


def test_show_summary_includes_failed_count():
    summary = ExecutionSummary(total_steps=5, successful=3, failed=2, skipped=0)
    output = _capture(show_summary, summary)
    assert "2" in output


def test_show_summary_includes_skipped_count():
    summary = ExecutionSummary(total_steps=5, successful=2, failed=1, skipped=2)
    output = _capture(show_summary, summary)
    assert "2" in output


def test_show_summary_shows_aborted_status():
    summary = ExecutionSummary(total_steps=3, successful=1, failed=1, skipped=0, aborted=True)
    output = _capture(show_summary, summary)
    assert "ABORTED" in output


def test_show_summary_shows_completed_status():
    summary = ExecutionSummary(total_steps=3, successful=3, failed=0, skipped=0, aborted=False)
    output = _capture(show_summary, summary)
    assert "COMPLETED" in output


def test_show_summary_shows_audit_log_path():
    summary = ExecutionSummary(
        total_steps=2,
        successful=2,
        failed=0,
        skipped=0,
        audit_log_path="/tmp/test-audit.json",
    )
    output = _capture(show_summary, summary)
    assert "/tmp/test-audit.json" in output


# ---------------------------------------------------------------------------
# show_warning
# ---------------------------------------------------------------------------


def test_show_warning_contains_message():
    output = _capture(show_warning, "This is a test warning")
    assert "This is a test warning" in output


def test_show_warning_contains_warning_label():
    output = _capture(show_warning, "something")
    assert "WARNING" in output


# ---------------------------------------------------------------------------
# show_error
# ---------------------------------------------------------------------------


def test_show_error_contains_message():
    output = _capture(show_error, "Something went wrong")
    assert "Something went wrong" in output


def test_show_error_contains_error_label():
    output = _capture(show_error, "oops")
    assert "ERROR" in output


# ---------------------------------------------------------------------------
# show_replay — intact chain
# ---------------------------------------------------------------------------


def test_show_replay_intact_chain_shows_hash_chain_intact():
    entries = [
        _make_audit_entry(seq=1, prev_hash=None, hash_val="aaa"),
        _make_audit_entry(seq=2, prev_hash="aaa", hash_val="bbb"),
    ]
    output = _capture(show_replay, entries, [])
    assert "Hash chain intact" in output


def test_show_replay_intact_chain_no_integrity_warning():
    entries = [_make_audit_entry(seq=1, prev_hash=None, hash_val="aaa")]
    output = _capture(show_replay, entries, [])
    assert "INTEGRITY WARNING" not in output


def test_show_replay_intact_chain_shows_entries():
    entries = [
        _make_audit_entry(seq=1, action=ActionType.PARSE, step_index=None, hash_val="aaa"),
        _make_audit_entry(seq=2, action=ActionType.EXECUTE, step_index=1, hash_val="bbb"),
    ]
    output = _capture(show_replay, entries, [])
    assert "PARSE" in output
    assert "EXECUTE" in output


# ---------------------------------------------------------------------------
# show_replay — broken chain
# ---------------------------------------------------------------------------


def test_show_replay_broken_chain_shows_integrity_warning():
    entries = [
        _make_audit_entry(seq=1, prev_hash=None, hash_val="aaa"),
        _make_audit_entry(seq=2, prev_hash="WRONG", hash_val="bbb"),
    ]
    output = _capture(show_replay, entries, [2])
    assert "INTEGRITY WARNING" in output


def test_show_replay_broken_chain_shows_broken_status():
    entries = [
        _make_audit_entry(seq=1, prev_hash=None, hash_val="aaa"),
        _make_audit_entry(seq=2, prev_hash="WRONG", hash_val="bbb"),
    ]
    output = _capture(show_replay, entries, [2])
    assert "Hash chain broken" in output


def test_show_replay_broken_chain_shows_unverified_prefix():
    entries = [
        _make_audit_entry(seq=1, prev_hash=None, hash_val="aaa"),
        _make_audit_entry(seq=2, prev_hash="WRONG", hash_val="bbb"),
        _make_audit_entry(seq=3, prev_hash="bbb", hash_val="ccc"),
    ]
    output = _capture(show_replay, entries, [2])
    assert "UNVERIFIED" in output


def test_show_replay_broken_chain_entries_before_break_not_unverified():
    entries = [
        _make_audit_entry(seq=1, prev_hash=None, hash_val="aaa"),
        _make_audit_entry(seq=2, prev_hash="WRONG", hash_val="bbb"),
    ]
    output = _capture(show_replay, entries, [2])
    lines = output.splitlines()
    # The first entry line should not have [UNVERIFIED]
    entry_lines = [line for line in lines if "#   1" in line or "#1" in line]
    for line in entry_lines:
        assert "UNVERIFIED" not in line


def test_show_replay_broken_chain_includes_seq_in_final_line():
    entries = [
        _make_audit_entry(seq=1, prev_hash=None, hash_val="aaa"),
        _make_audit_entry(seq=2, prev_hash="WRONG", hash_val="bbb"),
    ]
    output = _capture(show_replay, entries, [2])
    assert "#2" in output


def test_show_replay_empty_entries():
    output = _capture(show_replay, [], [])
    assert "Hash chain intact" in output


def test_show_replay_multiple_breaks():
    entries = [
        _make_audit_entry(seq=1, prev_hash=None, hash_val="aaa"),
        _make_audit_entry(seq=2, prev_hash="WRONG", hash_val="bbb"),
        _make_audit_entry(seq=3, prev_hash="WRONG2", hash_val="ccc"),
    ]
    output = _capture(show_replay, entries, [2, 3])
    assert "INTEGRITY WARNING" in output
    assert "#2" in output
    assert "#3" in output


def test_show_step_failure_shows_stdout_when_no_stderr():
    """show_step_failure shows stdout (dim) when stderr is empty but stdout has content."""
    step = _make_step()
    result = _make_result(exit_code=1, stdout="some output here", stderr="")
    output = _capture(show_step_failure, step, result)
    assert "some output here" in output
