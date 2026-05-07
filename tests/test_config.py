"""Tests for runbook_exec.config — load_config()."""

import os
from pathlib import Path

import pytest

from runbook_exec.config import load_config
from runbook_exec.exceptions import ConfigError
from runbook_exec.models import RiskLevel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_toml(tmp_path: Path, content: str) -> Path:
    """Write a .runbook-exec.toml file in tmp_path and return its path."""
    p = tmp_path / ".runbook-exec.toml"
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# Missing ANTHROPIC_API_KEY raises ConfigError
# ---------------------------------------------------------------------------

def test_missing_api_key_raises_config_error(monkeypatch, tmp_path):
    """ConfigError is raised with an actionable message when ANTHROPIC_API_KEY is absent."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        load_config()


def test_empty_api_key_raises_config_error(monkeypatch, tmp_path):
    """An empty string for ANTHROPIC_API_KEY is treated as missing."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")

    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        load_config()


# ---------------------------------------------------------------------------
# Missing config file uses built-in defaults without error
# ---------------------------------------------------------------------------

def test_no_config_file_uses_defaults(monkeypatch, tmp_path):
    """When .runbook-exec.toml is absent, built-in defaults are used without error."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)

    config = load_config()

    assert config.llm_model == "claude-sonnet-4-5"
    assert config.slack_channel == ""
    assert config.slack_bot_token == ""
    assert config.slack_app_token == ""
    assert config.timeout_seconds == 300
    assert config.auto_approve_level is None
    assert config.audit_log_dir == "./runbook-exec-logs"
    assert config.no_llm_context is False
    assert config.dry_run is False
    assert config.incident_id is None


def test_audit_log_dir_default(monkeypatch, tmp_path):
    """audit_log_dir default is './runbook-exec-logs'."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    config = load_config()

    assert config.audit_log_dir == "./runbook-exec-logs"


def test_timeout_seconds_default(monkeypatch, tmp_path):
    """timeout_seconds default is 300."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    config = load_config()

    assert config.timeout_seconds == 300


# ---------------------------------------------------------------------------
# File values override defaults
# ---------------------------------------------------------------------------

def test_file_overrides_defaults(monkeypatch, tmp_path):
    """Values in .runbook-exec.toml override built-in defaults."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    _write_toml(
        tmp_path,
        """
llm_model = "claude-opus-4"
slack_channel = "#incidents"
timeout_seconds = 600
audit_log_dir = "./custom-logs"
auto_approve_level = "read_only"
""",
    )

    config = load_config()

    assert config.llm_model == "claude-opus-4"
    assert config.slack_channel == "#incidents"
    assert config.timeout_seconds == 600
    assert config.audit_log_dir == "./custom-logs"
    assert config.auto_approve_level == RiskLevel.READ_ONLY


def test_file_partial_override(monkeypatch, tmp_path):
    """Only keys present in the TOML file override defaults; others keep defaults."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    _write_toml(tmp_path, 'slack_channel = "#ops"\n')

    config = load_config()

    assert config.slack_channel == "#ops"
    assert config.timeout_seconds == 300  # default unchanged
    assert config.audit_log_dir == "./runbook-exec-logs"  # default unchanged


# ---------------------------------------------------------------------------
# CLI values override file values
# ---------------------------------------------------------------------------

def test_cli_overrides_file(monkeypatch, tmp_path):
    """CLI kwargs override values from .runbook-exec.toml."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    _write_toml(tmp_path, 'timeout_seconds = 600\nslack_channel = "#incidents"\n')

    config = load_config(timeout_seconds=30, slack_channel="#override")

    assert config.timeout_seconds == 30
    assert config.slack_channel == "#override"


def test_cli_overrides_defaults(monkeypatch, tmp_path):
    """CLI kwargs override built-in defaults even without a config file."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    config = load_config(timeout_seconds=60, dry_run=True, audit_log_dir="./my-logs")

    assert config.timeout_seconds == 60
    assert config.dry_run is True
    assert config.audit_log_dir == "./my-logs"


def test_cli_none_values_do_not_override(monkeypatch, tmp_path):
    """CLI kwargs with None values are ignored — they don't override lower-priority sources."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    _write_toml(tmp_path, "timeout_seconds = 600\n")

    config = load_config(timeout_seconds=None, slack_channel=None)

    assert config.timeout_seconds == 600  # file value preserved
    assert config.slack_channel == ""  # default preserved


# ---------------------------------------------------------------------------
# Environment variable handling
# ---------------------------------------------------------------------------

def test_slack_tokens_read_from_env(monkeypatch, tmp_path):
    """SLACK_BOT_TOKEN and SLACK_APP_TOKEN are read from environment."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-bot-token")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-app-token")

    config = load_config()

    assert config.slack_bot_token == "xoxb-bot-token"
    assert config.slack_app_token == "xapp-app-token"


def test_slack_tokens_default_to_empty_when_absent(monkeypatch, tmp_path):
    """Missing Slack tokens default to empty strings (not an error)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)

    config = load_config()

    assert config.slack_bot_token == ""
    assert config.slack_app_token == ""


def test_cli_overrides_env_slack_token(monkeypatch, tmp_path):
    """CLI override for slack_bot_token takes precedence over env var."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-from-env")

    config = load_config(slack_bot_token="xoxb-from-cli")

    assert config.slack_bot_token == "xoxb-from-cli"


# ---------------------------------------------------------------------------
# Priority ordering: defaults < file < CLI (full stack)
# ---------------------------------------------------------------------------

def test_full_priority_stack(monkeypatch, tmp_path):
    """Verify all three layers interact correctly: defaults < file < CLI."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    _write_toml(
        tmp_path,
        """
timeout_seconds = 600
slack_channel = "#from-file"
audit_log_dir = "./file-logs"
""",
    )

    # CLI overrides timeout and slack_channel but not audit_log_dir
    config = load_config(timeout_seconds=10, slack_channel="#from-cli")

    assert config.timeout_seconds == 10          # CLI wins
    assert config.slack_channel == "#from-cli"   # CLI wins
    assert config.audit_log_dir == "./file-logs"  # file wins over default
    assert config.llm_model == "claude-sonnet-4-5"  # default (not in file or CLI)
