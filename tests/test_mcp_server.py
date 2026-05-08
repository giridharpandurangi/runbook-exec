"""Tests for runbook_exec.mcp_server.

All external I/O (parser, classifier, shell, audit) is mocked.
No real LLM calls, no real filesystem writes, no real subprocess execution.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from runbook_exec.models import ActionType, Config, ExecutionResult, RiskLevel, Step


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_step(
    index: int = 1,
    text: str = "Check disk usage",
    command: str | None = "df -h",
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


def _make_exec_result(exit_code: int = 0, stdout: str = "ok", stderr: str = "") -> ExecutionResult:
    return ExecutionResult(
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        duration_seconds=0.1,
        timed_out=False,
    )


def _make_config(slack_bot_token: str = "", slack_app_token: str = "", **kwargs) -> Config:
    defaults = dict(
        llm_model="claude-sonnet-4-5",
        slack_channel="#test",
        slack_bot_token=slack_bot_token,
        slack_app_token=slack_app_token,
        timeout_seconds=10,
        audit_log_dir="./test-audit-logs",
    )
    defaults.update(kwargs)
    return Config(**defaults)


def _make_mock_audit_log(tmp_path: Path) -> MagicMock:
    mock_log = MagicMock()
    mock_log.path = tmp_path / "test-audit.json"
    mock_log.__enter__ = MagicMock(return_value=mock_log)
    mock_log.__exit__ = MagicMock(return_value=False)
    return mock_log


# ---------------------------------------------------------------------------
# Test: server registers exactly 3 tools
# ---------------------------------------------------------------------------


def test_server_registers_three_tools() -> None:
    """The MCP server exposes exactly 3 tools with the correct names."""
    import asyncio
    from runbook_exec.mcp_server import mcp

    tools = asyncio.run(mcp.list_tools())
    tool_names = {t.name for t in tools}
    assert tool_names == {"validate_runbook", "execute_runbook", "replay_audit_log"}


# ---------------------------------------------------------------------------
# Test: validate_runbook
# ---------------------------------------------------------------------------


def test_validate_runbook_returns_step_list(tmp_path: Path) -> None:
    """validate_runbook returns a dict with classified steps."""
    runbook = tmp_path / "runbook.md"
    runbook.write_text("1. Check disk\n\n   ```bash\n   df -h\n   ```\n")

    step = _make_step(risk_level=RiskLevel.READ_ONLY)
    config = _make_config()

    with (
        patch("runbook_exec.mcp_server._load_config_for_mcp", return_value=config),
        patch("runbook_exec.mcp_server.parser.parse_runbook", return_value=[step]),
        patch("runbook_exec.mcp_server.classifier.classify_step", return_value=step),
    ):
        from runbook_exec.mcp_server import validate_runbook
        result = validate_runbook(str(runbook))

    assert result["status"] == "ok"
    assert result["total_steps"] == 1
    assert result["steps"][0]["index"] == 1
    assert result["steps"][0]["risk_level"] == "read_only"


def test_validate_runbook_missing_api_key_returns_error(tmp_path: Path) -> None:
    """Missing ANTHROPIC_API_KEY returns a clear error dict, not an exception."""
    runbook = tmp_path / "runbook.md"
    runbook.write_text("1. Check disk\n")

    with patch(
        "runbook_exec.mcp_server._load_config_for_mcp",
        return_value={"error": "ANTHROPIC_API_KEY is not set."},
    ):
        from runbook_exec.mcp_server import validate_runbook
        result = validate_runbook(str(runbook))

    assert "error" in result
    assert "ANTHROPIC_API_KEY" in result["error"]


# ---------------------------------------------------------------------------
# Test: execute_runbook — dry_run=True by default
# ---------------------------------------------------------------------------


def test_execute_runbook_defaults_to_dry_run(tmp_path: Path) -> None:
    """execute_runbook must default to dry_run=True — verified explicitly."""
    import inspect
    from runbook_exec.mcp_server import execute_runbook

    sig = inspect.signature(execute_runbook)
    assert sig.parameters["dry_run"].default is True, (
        "dry_run must default to True to prevent accidental real execution"
    )


def test_execute_runbook_dry_run_returns_preview(tmp_path: Path) -> None:
    """execute_runbook with dry_run=True returns a preview without executing."""
    runbook = tmp_path / "runbook.md"
    runbook.write_text("1. Check disk\n\n   ```bash\n   df -h\n   ```\n")

    steps = [_make_step(risk_level=RiskLevel.READ_ONLY)]
    config = _make_config(dry_run=True)

    with (
        patch("runbook_exec.mcp_server._load_config_for_mcp", return_value=config),
        patch("runbook_exec.mcp_server.parser.parse_runbook", return_value=steps),
        patch("runbook_exec.mcp_server.classifier.classify_step", side_effect=steps),
    ):
        from runbook_exec.mcp_server import execute_runbook
        result = execute_runbook(str(runbook), dry_run=True)

    assert result["status"] == "dry_run"
    assert result["total_steps"] == 1
    assert "dry_run=False" in result["message"]


# ---------------------------------------------------------------------------
# Test: execute_runbook — no Slack, approval_required on modifying step
# ---------------------------------------------------------------------------


def test_execute_runbook_no_slack_returns_approval_required(tmp_path: Path) -> None:
    """Without Slack, a modifying step triggers approval_required response."""
    runbook = tmp_path / "runbook.md"
    runbook.write_text("1. Restart nginx\n\n   ```bash\n   systemctl restart nginx\n   ```\n")

    step = _make_step(
        index=1,
        text="Restart nginx",
        command="systemctl restart nginx",
        risk_level=RiskLevel.MODIFYING,
    )
    # No Slack tokens → slack_enabled=False
    config = _make_config(slack_bot_token="", slack_app_token="", dry_run=False)
    mock_log = _make_mock_audit_log(tmp_path)

    with (
        patch("runbook_exec.mcp_server._load_config_for_mcp", return_value=config),
        patch("runbook_exec.mcp_server.parser.parse_runbook", return_value=[step]),
        patch("runbook_exec.mcp_server.classifier.classify_step", return_value=step),
        patch("runbook_exec.mcp_server.create_audit_log", return_value=mock_log),
    ):
        from runbook_exec.mcp_server import execute_runbook
        result = execute_runbook(str(runbook), dry_run=False)

    assert result["status"] == "approval_required"
    assert result["pending_step"]["index"] == 1
    assert result["pending_step"]["risk_level"] == "modifying"
    assert "resume_from_step=1" in result["message"]
    assert "approved=True" in result["message"]


# ---------------------------------------------------------------------------
# Test: execute_runbook — resume with approved=True
# ---------------------------------------------------------------------------


def test_execute_runbook_resume_approved_true_executes_step(tmp_path: Path) -> None:
    """Resuming with approved=True executes the step and returns completed."""
    runbook = tmp_path / "runbook.md"
    runbook.write_text("1. Restart nginx\n\n   ```bash\n   systemctl restart nginx\n   ```\n")

    step = _make_step(
        index=1,
        text="Restart nginx",
        command="systemctl restart nginx",
        risk_level=RiskLevel.MODIFYING,
    )
    config = _make_config(slack_bot_token="", slack_app_token="", dry_run=False)
    mock_log = _make_mock_audit_log(tmp_path)

    with (
        patch("runbook_exec.mcp_server._load_config_for_mcp", return_value=config),
        patch("runbook_exec.mcp_server.parser.parse_runbook", return_value=[step]),
        patch("runbook_exec.mcp_server.classifier.classify_step", return_value=step),
        patch("runbook_exec.mcp_server.create_audit_log", return_value=mock_log),
        patch("runbook_exec.mcp_server.shell.run_command", return_value=_make_exec_result()),
    ):
        from runbook_exec.mcp_server import execute_runbook
        result = execute_runbook(
            str(runbook), dry_run=False, resume_from_step=1, approved=True
        )

    assert result["status"] == "completed"
    assert result["summary"]["successful"] == 1
    assert result["summary"]["failed"] == 0


# ---------------------------------------------------------------------------
# Test: execute_runbook — resume with approved=False skips the step
# ---------------------------------------------------------------------------


def test_execute_runbook_resume_approved_false_skips_step(tmp_path: Path) -> None:
    """Resuming with approved=False skips the step and returns completed."""
    runbook = tmp_path / "runbook.md"
    runbook.write_text("1. Restart nginx\n\n   ```bash\n   systemctl restart nginx\n   ```\n")

    step = _make_step(
        index=1,
        text="Restart nginx",
        command="systemctl restart nginx",
        risk_level=RiskLevel.MODIFYING,
    )
    config = _make_config(slack_bot_token="", slack_app_token="", dry_run=False)
    mock_log = _make_mock_audit_log(tmp_path)

    with (
        patch("runbook_exec.mcp_server._load_config_for_mcp", return_value=config),
        patch("runbook_exec.mcp_server.parser.parse_runbook", return_value=[step]),
        patch("runbook_exec.mcp_server.classifier.classify_step", return_value=step),
        patch("runbook_exec.mcp_server.create_audit_log", return_value=mock_log),
        patch("runbook_exec.mcp_server.shell.run_command") as mock_shell,
    ):
        from runbook_exec.mcp_server import execute_runbook
        result = execute_runbook(
            str(runbook), dry_run=False, resume_from_step=1, approved=False
        )

    mock_shell.assert_not_called()
    assert result["status"] == "completed"
    assert result["summary"]["skipped"] == 1


# ---------------------------------------------------------------------------
# Test: replay_audit_log
# ---------------------------------------------------------------------------


def test_replay_audit_log_returns_entries_and_chain_status(tmp_path: Path) -> None:
    """replay_audit_log returns entries and chain integrity status."""
    from runbook_exec.audit import AuditLog, create_audit_log
    from runbook_exec.models import ActionType

    config = _make_config(audit_log_dir=str(tmp_path))
    runbook_path = tmp_path / "runbook.md"
    runbook_path.write_text("1. Check disk\n")

    # Write a real audit log with one entry
    with create_audit_log(config, runbook_path) as log:
        log.append({
            "action": ActionType.PARSE,
            "reasoning": "test",
            "mode": "live",
        })
        log_path = log.path

    from runbook_exec.mcp_server import replay_audit_log
    result = replay_audit_log(str(log_path))

    assert result["status"] == "ok"
    assert result["total_entries"] == 1
    assert result["chain_intact"] is True
    assert result["chain_breaks"] == []
    assert result["entries"][0]["action"] == "parse"


def test_replay_audit_log_missing_file_returns_error(tmp_path: Path) -> None:
    """replay_audit_log returns an error dict for a missing file."""
    from runbook_exec.mcp_server import replay_audit_log

    result = replay_audit_log(str(tmp_path / "nonexistent.json"))

    assert "error" in result


# ---------------------------------------------------------------------------
# Test: mcp-server CLI subcommand
# ---------------------------------------------------------------------------


def test_mcp_server_cli_subcommand_starts_without_crashing() -> None:
    """The mcp-server CLI subcommand calls mcp.run() without crashing."""
    from typer.testing import CliRunner
    from runbook_exec.cli import app

    runner = CliRunner()

    with patch("runbook_exec.mcp_server.mcp.run") as mock_run:
        result = runner.invoke(app, ["mcp-server"])

    mock_run.assert_called_once()
    assert result.exit_code == 0
