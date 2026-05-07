"""Custom exception hierarchy for runbook-exec.

All exceptions inherit from RunbookExecError so callers can catch the
base class for a broad handler or a specific subclass for targeted handling.
"""


class RunbookExecError(Exception):
    """Base exception for all runbook-exec errors."""


class ConfigError(RunbookExecError):
    """Raised when configuration is invalid or required values are missing.

    Examples: missing ANTHROPIC_API_KEY, invalid auto_approve_level value.
    """


class ParseError(RunbookExecError):
    """Raised when a Markdown runbook cannot be parsed.

    Should include the file path and line number in the message where possible.
    """


class ClassificationError(RunbookExecError):
    """Raised when the LLM returns an unparseable or invalid classification response.

    Triggered after the retry attempt also fails to produce valid JSON.
    """


class LLMError(RunbookExecError):
    """Raised when all LLM API retries are exhausted.

    The message includes the last exception encountered.
    """


class ShellError(RunbookExecError):
    """Reserved for shell execution errors.

    Note: shell.py always returns an ExecutionResult and never raises this
    exception directly. It exists in the hierarchy for completeness and
    potential future use.
    """


class ApprovalError(RunbookExecError):
    """Raised when the Slack API call fails during the approval workflow."""


class AuditError(RunbookExecError):
    """Raised when the audit log file cannot be created or written to.

    Examples: file collision on exclusive create, I/O failure during append.
    """
