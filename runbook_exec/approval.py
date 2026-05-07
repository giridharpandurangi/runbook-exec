"""Slack-based approval workflow for runbook-exec.

Manages approval requests and failure direction prompts via Slack Block Kit
messages and Socket Mode for real-time interaction without a public webhook.

When Slack is not configured (config.slack_enabled is False), both functions
fall back to interactive terminal prompts using rich.prompt.
"""

import logging
import threading
from enum import Enum

import slack_sdk
import slack_sdk.socket_mode
from pydantic import BaseModel
from rich.prompt import Confirm, Prompt
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

from runbook_exec.exceptions import ApprovalError
from runbook_exec.models import Config, Step

logger = logging.getLogger(__name__)


class ApprovalResult(BaseModel):
    """Result of an approval request."""

    approved: bool
    approver_slack_id: str | None = None
    timed_out: bool = False


class FailureDirection(str, Enum):
    """Direction chosen by the operator after a step failure."""

    CONTINUE = "continue"
    RETRY = "retry"
    ABORT = "abort"
    SKIP = "skip"


def _build_approval_blocks(step: Step, ts: str) -> list[dict]:
    """Build Block Kit blocks for an approval request message."""
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "⚠️ Approval Required"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Step:*\n{step.text}"},
                {"type": "mrkdwn", "text": f"*Risk Level:*\n{step.risk_level}"},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Command:*\n```{step.command}```"},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Approve"},
                    "style": "primary",
                    "action_id": f"approve_{ts}",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "❌ Deny"},
                    "style": "danger",
                    "action_id": f"deny_{ts}",
                },
            ],
        },
    ]


def _build_failure_direction_blocks(
    step: Step,
    failure_reason: str,
    ts: str,
    include_retry_warning: bool = False,
) -> list[dict]:
    """Build Block Kit blocks for a failure direction request message."""
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "⚠️ Step Failed — Choose Direction"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Step:*\n{step.text}"},
                {"type": "mrkdwn", "text": f"*Risk Level:*\n{step.risk_level}"},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Command:*\n```{step.command}```"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Failure Reason:*\n{failure_reason}"},
        },
    ]

    if include_retry_warning:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"⚠️ WARNING: This step is {step.risk_level}. "
                        "Retry may have unintended side effects."
                    ),
                },
            }
        )

    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Continue"},
                    "action_id": f"continue_{ts}",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Retry"},
                    "action_id": f"retry_{ts}",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Skip"},
                    "action_id": f"skip_{ts}",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Abort"},
                    "style": "danger",
                    "action_id": f"abort_{ts}",
                },
            ],
        }
    )

    return blocks


def _update_message_resolved(
    web_client: slack_sdk.WebClient,
    channel: str,
    ts: str,
    decision_text: str,
) -> None:
    """Update the Slack message to show the decision that was made."""
    try:
        web_client.chat_update(
            channel=channel,
            ts=ts,
            text=decision_text,
            blocks=[
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": decision_text},
                }
            ],
        )
    except Exception as exc:
        logger.warning(f"Failed to update Slack message after resolution: {exc}")


def _terminal_request_approval(step: Step, config: Config) -> ApprovalResult:
    """Prompt for approval in the terminal when Slack is not configured.

    Args:
        step: The step requiring approval.
        config: Runtime configuration.

    Returns:
        ApprovalResult based on the operator's terminal input.
    """
    from runbook_exec import display  # local import to avoid circular dependency

    display.show_warning(
        f"Step {step.index} requires approval [{step.risk_level.value if step.risk_level else 'unknown'}]"
    )
    if step.command:
        display.console.print(f"  Command: [bold]{step.command}[/bold]")

    approved = Confirm.ask("  Approve this step?", default=False)
    return ApprovalResult(approved=approved, approver_slack_id=None)


def _terminal_request_failure_direction(
    step: Step,
    failure_reason: str,
    config: Config,
    include_retry_warning: bool = False,
) -> FailureDirection:
    """Prompt for failure direction in the terminal when Slack is not configured.

    Args:
        step: The step that failed.
        failure_reason: Human-readable description of the failure.
        config: Runtime configuration.
        include_retry_warning: When True, prints a warning about retry side effects.

    Returns:
        FailureDirection chosen by the operator.
    """
    from runbook_exec import display  # local import to avoid circular dependency

    display.show_warning(f"Step {step.index} failed: {failure_reason}")
    if include_retry_warning and step.risk_level:
        display.show_warning(
            f"This step is {step.risk_level.value}. Retry may have unintended side effects."
        )

    choice = Prompt.ask(
        "  Choose direction",
        choices=["continue", "retry", "skip", "abort"],
        default="abort",
    )
    return FailureDirection(choice)


def request_approval(step: Step, config: Config) -> ApprovalResult:
    """Request approval for a step, using Slack or terminal depending on config.

    Routes to the Slack workflow when both SLACK_BOT_TOKEN and SLACK_APP_TOKEN
    are set (config.slack_enabled is True). Falls back to an interactive terminal
    prompt otherwise.

    Args:
        step: The step requiring approval.
        config: Runtime configuration with Slack tokens and channel.

    Returns:
        ApprovalResult with approved status, approver ID, and timeout flag.

    Raises:
        ApprovalError: If the Slack API call fails (Slack path only).
    """
    if not config.slack_enabled:
        return _terminal_request_approval(step, config)
    return _slack_request_approval(step, config)


def request_failure_direction(
    step: Step,
    failure_reason: str,
    config: Config,
    include_retry_warning: bool = False,
) -> FailureDirection:
    """Request failure direction, using Slack or terminal depending on config.

    Routes to the Slack workflow when both SLACK_BOT_TOKEN and SLACK_APP_TOKEN
    are set (config.slack_enabled is True). Falls back to an interactive terminal
    prompt otherwise.

    Args:
        step: The step that failed.
        failure_reason: Human-readable description of the failure.
        config: Runtime configuration with Slack tokens and channel.
        include_retry_warning: When True, includes a warning about retry side effects.

    Returns:
        FailureDirection enum value matching the operator's choice, or ABORT on timeout.

    Raises:
        ApprovalError: If the Slack API call fails (Slack path only).
    """
    if not config.slack_enabled:
        return _terminal_request_failure_direction(
            step, failure_reason, config, include_retry_warning
        )
    return _slack_request_failure_direction(step, failure_reason, config, include_retry_warning)


def _slack_request_approval(step: Step, config: Config) -> ApprovalResult:
    """Post an approval request to Slack and wait for a button click.

    Posts a Block Kit message to config.slack_channel with Approve/Deny buttons,
    opens a Socket Mode connection, and waits for a response up to
    config.timeout_seconds.

    Args:
        step: The step requiring approval.
        config: Runtime configuration with Slack tokens and channel.

    Returns:
        ApprovalResult with approved status, approver ID, and timeout flag.

    Raises:
        ApprovalError: If the Slack API call fails.
    """
    try:
        web_client = slack_sdk.WebClient(token=config.slack_bot_token)
        response = web_client.chat_postMessage(
            channel=config.slack_channel,
            text=f"⚠️ Approval Required for step: {step.text}",
            blocks=_build_approval_blocks(step, "placeholder"),
        )
    except Exception as exc:
        raise ApprovalError(f"Failed to post approval request to Slack: {exc}") from exc

    ts: str = response["ts"]

    # Rebuild blocks with the real ts now that we have it
    try:
        web_client.chat_update(
            channel=config.slack_channel,
            ts=ts,
            text=f"⚠️ Approval Required for step: {step.text}",
            blocks=_build_approval_blocks(step, ts),
        )
    except Exception as exc:
        raise ApprovalError(f"Failed to update approval message with action IDs: {exc}") from exc

    result_holder: dict = {}
    done_event = threading.Event()

    def handle_socket_request(
        socket_client: slack_sdk.socket_mode.SocketModeClient, req: SocketModeRequest
    ) -> None:
        """Handle incoming Socket Mode requests."""
        if req.payload.get("type") != "block_actions":
            return

        # Acknowledge the interaction immediately
        socket_client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))

        # Only handle actions for our posted message
        container = req.payload.get("container", {})
        message_ts = container.get("message_ts", "")
        if message_ts != ts:
            return

        actions = req.payload.get("actions", [])
        if not actions:
            return

        action_id: str = actions[0].get("action_id", "")
        user_id: str = req.payload.get("user", {}).get("id", "")

        if action_id == f"approve_{ts}":
            result_holder["result"] = ApprovalResult(
                approved=True,
                approver_slack_id=user_id,
            )
            _update_message_resolved(
                web_client,
                config.slack_channel,
                ts,
                f"✅ Approved by <@{user_id}>",
            )
            done_event.set()
        elif action_id == f"deny_{ts}":
            result_holder["result"] = ApprovalResult(
                approved=False,
                approver_slack_id=user_id,
            )
            _update_message_resolved(
                web_client,
                config.slack_channel,
                ts,
                f"❌ Denied by <@{user_id}>",
            )
            done_event.set()

    socket_client = slack_sdk.socket_mode.SocketModeClient(
        app_token=config.slack_app_token,
        web_client=web_client,
    )
    socket_client.socket_mode_request_listeners.append(handle_socket_request)

    try:
        socket_client.connect()
        timed_out = not done_event.wait(timeout=config.timeout_seconds)
    except KeyboardInterrupt:
        _update_message_resolved(
            web_client,
            config.slack_channel,
            ts,
            "⚠️ Execution interrupted — no action taken",
        )
        socket_client.close()
        raise
    finally:
        socket_client.close()

    if timed_out:
        return ApprovalResult(approved=False, timed_out=True)

    return result_holder["result"]


def _slack_request_failure_direction(
    step: Step,
    failure_reason: str,
    config: Config,
    include_retry_warning: bool = False,
) -> FailureDirection:
    """Post a failure direction prompt to Slack and wait for a choice.

    Posts a Block Kit message with Continue/Retry/Skip/Abort buttons,
    opens a Socket Mode connection, and waits for a response up to
    config.timeout_seconds. On timeout, returns FailureDirection.ABORT
    as the safe default.

    Args:
        step: The step that failed.
        failure_reason: Human-readable description of the failure.
        config: Runtime configuration with Slack tokens and channel.
        include_retry_warning: When True, includes a warning about retry
            side effects for modifying/destructive steps.

    Returns:
        FailureDirection enum value matching the button clicked, or ABORT on timeout.

    Raises:
        ApprovalError: If the Slack API call fails.
    """
    try:
        web_client = slack_sdk.WebClient(token=config.slack_bot_token)
        response = web_client.chat_postMessage(
            channel=config.slack_channel,
            text=f"⚠️ Step failed: {step.text} — {failure_reason}",
            blocks=_build_failure_direction_blocks(
                step, failure_reason, "placeholder", include_retry_warning
            ),
        )
    except Exception as exc:
        raise ApprovalError(f"Failed to post failure direction request to Slack: {exc}") from exc

    ts: str = response["ts"]

    # Rebuild blocks with the real ts now that we have it
    try:
        web_client.chat_update(
            channel=config.slack_channel,
            ts=ts,
            text=f"⚠️ Step failed: {step.text} — {failure_reason}",
            blocks=_build_failure_direction_blocks(step, failure_reason, ts, include_retry_warning),
        )
    except Exception as exc:
        raise ApprovalError(
            f"Failed to update failure direction message with action IDs: {exc}"
        ) from exc

    result_holder: dict = {}
    done_event = threading.Event()

    _action_to_direction = {
        f"continue_{ts}": FailureDirection.CONTINUE,
        f"retry_{ts}": FailureDirection.RETRY,
        f"skip_{ts}": FailureDirection.SKIP,
        f"abort_{ts}": FailureDirection.ABORT,
    }

    def handle_socket_request(
        socket_client: slack_sdk.socket_mode.SocketModeClient, req: SocketModeRequest
    ) -> None:
        """Handle incoming Socket Mode requests."""
        if req.payload.get("type") != "block_actions":
            return

        # Acknowledge the interaction immediately
        socket_client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))

        # Only handle actions for our posted message
        container = req.payload.get("container", {})
        message_ts = container.get("message_ts", "")
        if message_ts != ts:
            return

        actions = req.payload.get("actions", [])
        if not actions:
            return

        action_id: str = actions[0].get("action_id", "")
        user_id: str = req.payload.get("user", {}).get("id", "")

        direction = _action_to_direction.get(action_id)
        if direction is not None:
            result_holder["result"] = direction
            _update_message_resolved(
                web_client,
                config.slack_channel,
                ts,
                f"Direction chosen by <@{user_id}>: *{direction.value}*",
            )
            done_event.set()

    socket_client = slack_sdk.socket_mode.SocketModeClient(
        app_token=config.slack_app_token,
        web_client=web_client,
    )
    socket_client.socket_mode_request_listeners.append(handle_socket_request)

    try:
        socket_client.connect()
        timed_out = not done_event.wait(timeout=config.timeout_seconds)
    except KeyboardInterrupt:
        _update_message_resolved(
            web_client,
            config.slack_channel,
            ts,
            "⚠️ Execution interrupted — no action taken",
        )
        socket_client.close()
        raise
    finally:
        socket_client.close()

    if timed_out:
        return FailureDirection.ABORT

    return result_holder["result"]
