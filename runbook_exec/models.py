"""Data models for runbook-exec.

All shared types live here. No other module defines domain types.
"""

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class RiskLevel(str, Enum):
    """Risk classification for runbook steps."""

    READ_ONLY = "read_only"
    MODIFYING = "modifying"
    DESTRUCTIVE = "destructive"


class Step(BaseModel):
    """A single step in a runbook."""

    index: int = Field(..., description="1-based position in the runbook")
    section: str | None = Field(None, description="Nearest preceding heading, or None")
    text: str = Field(..., description="Full step text (prose + command)")
    command: str | None = Field(None, description="Extracted shell command, or None")
    risk_level: RiskLevel | None = Field(None, description="Set after classification")
    classification_reasoning: str | None = Field(None, description="LLM explanation")


class ExecutionResult(BaseModel):
    """Result of executing a shell command."""

    stdout: str
    stderr: str
    exit_code: int
    duration_seconds: float
    timed_out: bool = False


class ActionType(str, Enum):
    """Types of actions recorded in the audit log."""

    PARSE = "parse"
    CLASSIFY = "classify"
    EXECUTE = "execute"
    APPROVE = "approve"
    DENY = "deny"
    SKIP = "skip"
    ABORT = "abort"
    FAILURE = "failure"
    SUMMARY = "summary"


class AuditEntry(BaseModel):
    """A single record in the audit log with hash chain."""

    seq: int = Field(..., description="Monotonically increasing sequence number")
    action: ActionType
    timestamp: str = Field(..., description="ISO 8601 UTC")
    step_index: int | None = None
    step_text: str | None = None
    command: str | None = None
    risk_level: RiskLevel | None = None
    output: str | None = Field(None, description="Combined stdout+stderr for readability")
    stdout: str | None = None
    stderr: str | None = None
    exit_code: int | None = None
    duration_seconds: float | None = None
    approver_slack_id: str | None = None
    reasoning: str | None = None
    mode: Literal["live", "dry_run"] = "live"
    prev_hash: str | None = Field(None, description="SHA-256 of previous entry's canonical JSON")
    hash: str = Field(..., description="SHA-256 of this entry's canonical JSON (excl. hash field)")


class Config(BaseModel):
    """Runtime configuration for runbook-exec."""

    llm_model: str = "claude-sonnet-4-5"
    slack_channel: str = ""
    slack_bot_token: str = ""  # from SLACK_BOT_TOKEN env var
    slack_app_token: str = ""  # from SLACK_APP_TOKEN env var (Socket Mode)
    timeout_seconds: int = 300  # 5 minutes default
    auto_approve_level: RiskLevel | None = None
    audit_log_dir: str = "./runbook-exec-logs"
    no_llm_context: bool = False
    dry_run: bool = False
    incident_id: str | None = None

    @property
    def slack_enabled(self) -> bool:
        """True when both Slack tokens are present and non-empty."""
        return bool(self.slack_bot_token and self.slack_app_token)


class ExecutionSummary(BaseModel):
    """Summary of a runbook execution."""

    total_steps: int
    successful: int
    failed: int
    skipped: int
    aborted: bool = False
    audit_log_path: str | None = None


# Risk level ordering for approval logic
RISK_ORDER = {
    RiskLevel.READ_ONLY: 0,
    RiskLevel.MODIFYING: 1,
    RiskLevel.DESTRUCTIVE: 2,
}


def needs_approval(step: Step, config: Config) -> bool:
    """Determine if a step requires approval based on risk level and config.

    A step requires approval iff its risk level is strictly higher than auto_approve_level.

    Args:
        step: The step to check
        config: Runtime configuration

    Returns:
        True if approval is required, False otherwise
    """
    if step.risk_level is None:
        # Unclassified steps should not reach execution, but if they do, require approval
        return True

    if config.auto_approve_level is None:
        # Default: only read_only steps auto-run
        return step.risk_level != RiskLevel.READ_ONLY

    return RISK_ORDER[step.risk_level] > RISK_ORDER[config.auto_approve_level]
