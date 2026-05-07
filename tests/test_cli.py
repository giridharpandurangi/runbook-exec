"""Tests for runbook_exec.cli module.

Uses Typer's CliRunner to invoke subcommands end-to-end with all external
dependencies mocked.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from runbook_exec.cli import app
from runbook_exec.exceptions import ParseError, RunbookExecError
from runbook_exec.models import (
    AuditEntry,
    ActionType,
    Config,
    ExecutionSummary,
    RiskLevel,
    Step,
)

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _make_step(
    index: int = 1,
    text: str = "Check disk space",
    command: str | None = "df -h",
    risk_level: RiskLevel = RiskLevel.READ_ONLY,
) -> Step:
    return Step(
        index=index,
        section=None,
        text=text,
        command=command,
        risk_level=risk_level,
        classification_reasoning="Read-only command",
    )


def _make_audit_entry(seq: int = 1) -> AuditEntry:
    """Build a minimal valid AuditEntry for replay tests."""
    import hashlib, json
    entry_data = {
        "seq": seq,
        "action": ActionType.EXECUTE,
        "timestamp": "2024-01-15T03:00:00Z",
        "step_index": 1,
        "step_text": "Check disk space",
        "command": "df -h",
        "risk_level": RiskLevel.READ_ONLY,
        "output": "Filesystem 100%",
        "stdout": "Filesystem 100%",
        "stderr": None,
        "exit_code": 0,
        "duration_seconds": 0.1,
        "approver_slack_id": None,
        "reasoning": None,
        "mode": "live",
        "prev_hash": None,
        "hash": "",
    }
    # Compute a real hash so verify_chain passes
    data_for_hash = {k: v for k, v in entry_data.items() if k != "hash"}
    canonical = json.dumps(data_for_hash, sort_keys=True, separators=(",", ":"), default=str)
    entry_data["hash"] = hashlib.sha256(canonical.encode()).hexdigest()
    return AuditEntry(**entry_data)


# ---------------------------------------------------------------------------
# Test 1: run with valid runbook invokes executor
# ---------------------------------------------------------------------------


def test_run_with_valid_runbook_invokes_executor(tmp_path: Path) -> None:
    """The run subcommand should invoke executor.run_runbook with the runbook path."""
    runbook = tmp_path / "runbook.md"
    runbook.write_text("# Test\n\n1. Check disk\n\n   ```\n   df -h\n   ```\n")

    config = _make_config()
    summary = ExecutionSummary(
        total_steps=1, successful=1, failed=0, skipped=0, aborted=False
    )

    with (
        patch("runbook_exec.cli.config_module.load_config", return_value=config),
        patch("runbook_exec.cli.executor.run_runbook", return_value=summary) as mock_run,
    ):
        result = runner.invoke(app, ["run", str(runbook)])

    assert result.exit_code == 0
    mock_run.assert_called_once_with(runbook, config)


# ---------------------------------------------------------------------------
# Test 2: run with missing file exits 1 with error message
# ---------------------------------------------------------------------------


def test_run_with_missing_file_exits_1(tmp_path: Path) -> None:
    """The run subcommand should exit 1 and show an error when the runbook is missing."""
    missing = tmp_path / "nonexistent.md"

    config = _make_config()

    with (
        patch("runbook_exec.cli.config_module.load_config", return_value=config),
        patch(
            "runbook_exec.cli.executor.run_runbook",
            side_effect=ParseError("File not found"),
        ),
    ):
        result = runner.invoke(app, ["run", str(missing)])

    assert result.exit_code == 1
    assert "File not found" in result.output


# ---------------------------------------------------------------------------
# Test 3: validate displays risk levels, exits 0, writes no audit log
# ---------------------------------------------------------------------------


def test_validate_displays_risk_levels_exits_0(tmp_path: Path) -> None:
    """validate should display risk levels for each step and exit 0 without writing an audit log."""
    runbook = tmp_path / "runbook.md"
    runbook.write_text("# Test\n\n1. Check disk\n\n   ```\n   df -h\n   ```\n")

    config = _make_config()
    step = _make_step(risk_level=RiskLevel.READ_ONLY)

    with (
        patch("runbook_exec.cli.config_module.load_config", return_value=config),
        patch("runbook_exec.cli.parser.parse_runbook", return_value=[step]),
        patch("runbook_exec.cli.classifier.classify_step", return_value=step),
        patch("runbook_exec.cli.audit_module.create_audit_log") as mock_audit,
    ):
        result = runner.invoke(app, ["validate", str(runbook)])

    assert result.exit_code == 0
    # No audit log should be created
    mock_audit.assert_not_called()
    # Output should contain the risk level
    assert "read_only" in result.output


def test_validate_shows_approval_warning_for_modifying_step(tmp_path: Path) -> None:
    """validate should warn when a step would require approval."""
    runbook = tmp_path / "runbook.md"
    runbook.write_text("# Test\n\n1. Restart service\n\n   ```\n   systemctl restart nginx\n   ```\n")

    config = _make_config()
    step = _make_step(risk_level=RiskLevel.MODIFYING)

    with (
        patch("runbook_exec.cli.config_module.load_config", return_value=config),
        patch("runbook_exec.cli.parser.parse_runbook", return_value=[step]),
        patch("runbook_exec.cli.classifier.classify_step", return_value=step),
    ):
        result = runner.invoke(app, ["validate", str(runbook)])

    assert result.exit_code == 0
    assert "would require approval" in result.output


# ---------------------------------------------------------------------------
# Test 4: replay displays entries and chain status
# ---------------------------------------------------------------------------


def test_replay_displays_entries_and_chain_status(tmp_path: Path) -> None:
    """replay should display audit entries and the chain integrity status."""
    audit_log = tmp_path / "audit.json"
    audit_log.write_text("")  # file must exist for the path argument

    entry = _make_audit_entry(seq=1)

    with (
        patch("runbook_exec.cli.audit_module.load_log", return_value=[entry]),
        patch("runbook_exec.cli.audit_module.verify_chain", return_value=[]),
    ):
        result = runner.invoke(app, ["replay", str(audit_log)])

    assert result.exit_code == 0
    # Output should contain chain status
    assert "Hash chain intact" in result.output


# ---------------------------------------------------------------------------
# Test 5: --help on run shows usage
# ---------------------------------------------------------------------------


def test_run_help_shows_usage() -> None:
    """--help on the run subcommand should show usage information."""
    result = runner.invoke(app, ["run", "--help"])

    assert result.exit_code == 0
    assert "runbook" in result.output.lower()
    assert "--dry-run" in result.output


# ---------------------------------------------------------------------------
# Test 6: --help on validate shows usage
# ---------------------------------------------------------------------------


def test_validate_help_shows_usage() -> None:
    """--help on the validate subcommand should show usage information."""
    result = runner.invoke(app, ["validate", "--help"])

    assert result.exit_code == 0
    assert "runbook" in result.output.lower()


# ---------------------------------------------------------------------------
# Test 7: --help on replay shows usage
# ---------------------------------------------------------------------------


def test_replay_help_shows_usage() -> None:
    """--help on the replay subcommand should show usage information."""
    result = runner.invoke(app, ["replay", "--help"])

    assert result.exit_code == 0
    assert "audit" in result.output.lower()


# ---------------------------------------------------------------------------
# Test 8: --no-llm-context flag shows warning at startup
# ---------------------------------------------------------------------------


def test_no_llm_context_flag_shows_warning(tmp_path: Path) -> None:
    """--no-llm-context should display a warning before execution begins."""
    runbook = tmp_path / "runbook.md"
    runbook.write_text("# Test\n\n1. Check disk\n\n   ```\n   df -h\n   ```\n")

    config = _make_config(no_llm_context=True)
    summary = ExecutionSummary(
        total_steps=1, successful=1, failed=0, skipped=0, aborted=False
    )

    with (
        patch("runbook_exec.cli.config_module.load_config", return_value=config),
        patch("runbook_exec.cli.executor.run_runbook", return_value=summary),
    ):
        result = runner.invoke(app, ["run", str(runbook), "--no-llm-context"])

    assert result.exit_code == 0
    assert "no-llm-context" in result.output or "LLM" in result.output or "disabled" in result.output


# ---------------------------------------------------------------------------
# Test 9: RunbookExecError → exit 1 with error message
# ---------------------------------------------------------------------------


def test_run_runbook_exec_error_exits_1(tmp_path: Path) -> None:
    """Any RunbookExecError raised during run should exit 1 and display the error."""
    runbook = tmp_path / "runbook.md"
    runbook.write_text("# Test\n\n1. Check disk\n")

    config = _make_config()

    with (
        patch("runbook_exec.cli.config_module.load_config", return_value=config),
        patch(
            "runbook_exec.cli.executor.run_runbook",
            side_effect=RunbookExecError("Something went wrong"),
        ),
    ):
        result = runner.invoke(app, ["run", str(runbook)])

    assert result.exit_code == 1
    assert "Something went wrong" in result.output


# ---------------------------------------------------------------------------
# Coverage gap tests
# ---------------------------------------------------------------------------


def test_sigint_handler_sets_interrupt_flag() -> None:
    """_sigint_handler calls interrupt.set_interrupted() when invoked."""
    import runbook_exec.interrupt as interrupt_module
    from runbook_exec.cli import _sigint_handler

    interrupt_module.reset()
    try:
        _sigint_handler(2, None)
        assert interrupt_module.is_interrupted() is True
    finally:
        interrupt_module.reset()


def test_run_keyboard_interrupt_exits_130(tmp_path: Path) -> None:
    """KeyboardInterrupt during run exits with code 130."""
    runbook = tmp_path / "runbook.md"
    runbook.write_text("# Test\n\n1. Check disk\n")

    config = _make_config()

    with (
        patch("runbook_exec.cli.config_module.load_config", return_value=config),
        patch(
            "runbook_exec.cli.executor.run_runbook",
            side_effect=KeyboardInterrupt,
        ),
    ):
        result = runner.invoke(app, ["run", str(runbook)])

    assert result.exit_code == 130


def test_validate_runbook_exec_error_exits_1(tmp_path: Path) -> None:
    """RunbookExecError during validate exits 1 with error message."""
    runbook = tmp_path / "runbook.md"
    runbook.write_text("# Test\n\n1. Check disk\n")

    config = _make_config()

    with (
        patch("runbook_exec.cli.config_module.load_config", return_value=config),
        patch(
            "runbook_exec.cli.parser.parse_runbook",
            side_effect=RunbookExecError("parse failed"),
        ),
    ):
        result = runner.invoke(app, ["validate", str(runbook)])

    assert result.exit_code == 1
    assert "parse failed" in result.output


def test_replay_runbook_exec_error_exits_1(tmp_path: Path) -> None:
    """RunbookExecError during replay exits 1 with error message."""
    audit_log = tmp_path / "audit.json"
    audit_log.write_text("")

    with patch(
        "runbook_exec.cli.audit_module.load_log",
        side_effect=RunbookExecError("log corrupted"),
    ):
        result = runner.invoke(app, ["replay", str(audit_log)])

    assert result.exit_code == 1
    assert "log corrupted" in result.output


def test_version_flag_prints_version() -> None:
    """--version prints the package version and exits 0."""
    from runbook_exec import __version__

    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert __version__ in result.output
    assert "0.1.0" in result.output
