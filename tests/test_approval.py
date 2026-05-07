"""Tests for the Slack approval workflow (approval.py).

All Slack SDK calls are mocked — no real API calls are made.
Tests simulate button clicks by directly invoking the registered
socket_mode_request_listeners handler with a mock SocketModeRequest.
"""

from unittest.mock import MagicMock, Mock, patch

import pytest

from runbook_exec.approval import (
    FailureDirection,
    request_approval,
    request_failure_direction,
)
from runbook_exec.exceptions import ApprovalError
from runbook_exec.models import RiskLevel

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_block_action_request(action_id: str, user_id: str, message_ts: str) -> Mock:
    """Build a mock SocketModeRequest that looks like a block_actions event."""
    req = Mock()
    req.envelope_id = "env-001"
    req.payload = {
        "type": "block_actions",
        "user": {"id": user_id},
        "container": {"message_ts": message_ts},
        "actions": [{"action_id": action_id}],
    }
    return req


def _setup_socket_mock(
    mock_socket_class: Mock, action_id_template: str, user_id: str, message_ts: str
) -> Mock:
    """Configure the SocketModeClient mock to fire a button click when connect() is called.

    The action_id_template uses '{ts}' as a placeholder that gets replaced with message_ts.
    """
    mock_socket_instance = MagicMock()
    mock_socket_instance.socket_mode_request_listeners = []

    def fire_click_on_connect():
        """Simulate a button click immediately after connect."""
        action_id = action_id_template.format(ts=message_ts)
        req = _make_block_action_request(action_id, user_id, message_ts)
        for listener in mock_socket_instance.socket_mode_request_listeners:
            listener(mock_socket_instance, req)

    mock_socket_instance.connect.side_effect = fire_click_on_connect
    mock_socket_class.return_value = mock_socket_instance
    return mock_socket_instance


# ---------------------------------------------------------------------------
# request_approval tests
# ---------------------------------------------------------------------------


class TestRequestApproval:
    """Tests for request_approval()."""

    def test_approve_button_click(self, make_step, make_config):
        """Approve button click returns ApprovalResult(approved=True, approver_slack_id='U_TEST')."""
        step = make_step(risk_level=RiskLevel.MODIFYING)
        config = make_config()
        message_ts = "1234567890.123456"

        with (
            patch("slack_sdk.WebClient") as mock_web_class,
            patch("slack_sdk.socket_mode.SocketModeClient") as mock_socket_class,
        ):
            mock_web = MagicMock()
            mock_web.chat_postMessage.return_value = {"ts": message_ts, "ok": True}
            mock_web.chat_update.return_value = {"ok": True}
            mock_web_class.return_value = mock_web

            _setup_socket_mock(mock_socket_class, "approve_{ts}", "U_TEST", message_ts)

            result = request_approval(step, config)

        assert result.approved is True
        assert result.approver_slack_id == "U_TEST"
        assert result.timed_out is False

    def test_deny_button_click(self, make_step, make_config):
        """Deny button click returns ApprovalResult(approved=False, approver_slack_id='U_TEST')."""
        step = make_step(risk_level=RiskLevel.DESTRUCTIVE)
        config = make_config()
        message_ts = "1234567890.654321"

        with (
            patch("slack_sdk.WebClient") as mock_web_class,
            patch("slack_sdk.socket_mode.SocketModeClient") as mock_socket_class,
        ):
            mock_web = MagicMock()
            mock_web.chat_postMessage.return_value = {"ts": message_ts, "ok": True}
            mock_web.chat_update.return_value = {"ok": True}
            mock_web_class.return_value = mock_web

            _setup_socket_mock(mock_socket_class, "deny_{ts}", "U_TEST", message_ts)

            result = request_approval(step, config)

        assert result.approved is False
        assert result.approver_slack_id == "U_TEST"
        assert result.timed_out is False

    def test_approval_timeout(self, make_step, make_config):
        """No response within timeout returns ApprovalResult(timed_out=True)."""
        step = make_step(risk_level=RiskLevel.MODIFYING)
        config = make_config(timeout_seconds=0)  # Immediate timeout
        message_ts = "1234567890.000001"

        with (
            patch("slack_sdk.WebClient") as mock_web_class,
            patch("slack_sdk.socket_mode.SocketModeClient") as mock_socket_class,
        ):
            mock_web = MagicMock()
            mock_web.chat_postMessage.return_value = {"ts": message_ts, "ok": True}
            mock_web.chat_update.return_value = {"ok": True}
            mock_web_class.return_value = mock_web

            # Socket that never fires any events
            mock_socket_instance = MagicMock()
            mock_socket_instance.socket_mode_request_listeners = []
            mock_socket_instance.connect.return_value = None
            mock_socket_class.return_value = mock_socket_instance

            result = request_approval(step, config)

        assert result.timed_out is True
        assert result.approved is False

    def test_slack_api_failure_raises_approval_error(self, make_step, make_config):
        """Slack API failure raises ApprovalError."""
        step = make_step(risk_level=RiskLevel.MODIFYING)
        config = make_config()

        with patch("slack_sdk.WebClient") as mock_web_class:
            mock_web = MagicMock()
            mock_web.chat_postMessage.side_effect = Exception("Slack API error")
            mock_web_class.return_value = mock_web

            with pytest.raises(ApprovalError, match="Slack API error"):
                request_approval(step, config)

    def test_approve_updates_slack_message(self, make_step, make_config):
        """After approval, the Slack message is updated to show the decision."""
        step = make_step(risk_level=RiskLevel.MODIFYING)
        config = make_config()
        message_ts = "1234567890.111111"

        with (
            patch("slack_sdk.WebClient") as mock_web_class,
            patch("slack_sdk.socket_mode.SocketModeClient") as mock_socket_class,
        ):
            mock_web = MagicMock()
            mock_web.chat_postMessage.return_value = {"ts": message_ts, "ok": True}
            mock_web.chat_update.return_value = {"ok": True}
            mock_web_class.return_value = mock_web

            _setup_socket_mock(mock_socket_class, "approve_{ts}", "U_APPROVER", message_ts)

            request_approval(step, config)

        # chat_update should have been called at least twice:
        # once to set real action IDs, once to show the decision
        assert mock_web.chat_update.call_count >= 2

    def test_stale_message_ts_ignored(self, make_step, make_config):
        """block_actions events for a different message ts are ignored."""
        step = make_step(risk_level=RiskLevel.MODIFYING)
        config = make_config(timeout_seconds=0)
        message_ts = "1234567890.999999"
        stale_ts = "0000000000.000000"

        with (
            patch("slack_sdk.WebClient") as mock_web_class,
            patch("slack_sdk.socket_mode.SocketModeClient") as mock_socket_class,
        ):
            mock_web = MagicMock()
            mock_web.chat_postMessage.return_value = {"ts": message_ts, "ok": True}
            mock_web.chat_update.return_value = {"ok": True}
            mock_web_class.return_value = mock_web

            # Fire a click for a DIFFERENT message ts (stale)
            _setup_socket_mock(mock_socket_class, "approve_{ts}", "U_TEST", stale_ts)

            result = request_approval(step, config)

        # Should time out because the stale event was ignored
        assert result.timed_out is True

    def test_keyboard_interrupt_updates_slack_message(self, make_step, make_config):
        """KeyboardInterrupt during wait updates Slack message to interrupted text and closes connection."""
        step = make_step(risk_level=RiskLevel.MODIFYING)
        config = make_config(timeout_seconds=30)
        message_ts = "1234567890.interrupt"

        with (
            patch("slack_sdk.WebClient") as mock_web_class,
            patch("slack_sdk.socket_mode.SocketModeClient") as mock_socket_class,
        ):
            mock_web = MagicMock()
            mock_web.chat_postMessage.return_value = {"ts": message_ts, "ok": True}
            mock_web.chat_update.return_value = {"ok": True}
            mock_web_class.return_value = mock_web

            mock_socket_instance = MagicMock()
            mock_socket_instance.socket_mode_request_listeners = []
            mock_socket_instance.connect.side_effect = KeyboardInterrupt
            mock_socket_class.return_value = mock_socket_instance

            with pytest.raises(KeyboardInterrupt):
                request_approval(step, config)

        # The message should have been updated with the interrupted text
        update_calls = mock_web.chat_update.call_args_list
        interrupted_texts = [
            c
            for c in update_calls
            if "interrupted" in str(c).lower() or "no action taken" in str(c).lower()
        ]
        assert len(interrupted_texts) >= 1

        # Socket should have been closed
        mock_socket_instance.close.assert_called()


# ---------------------------------------------------------------------------
# request_failure_direction tests
# ---------------------------------------------------------------------------


class TestRequestFailureDirection:
    """Tests for request_failure_direction()."""

    def _run_direction_test(
        self,
        make_step,
        make_config,
        action_template: str,
        expected_direction: FailureDirection,
        include_retry_warning: bool = False,
    ) -> FailureDirection:
        """Helper to run a failure direction test for a given button."""
        step = make_step(risk_level=RiskLevel.MODIFYING)
        config = make_config()
        message_ts = "9876543210.123456"

        with (
            patch("slack_sdk.WebClient") as mock_web_class,
            patch("slack_sdk.socket_mode.SocketModeClient") as mock_socket_class,
        ):
            mock_web = MagicMock()
            mock_web.chat_postMessage.return_value = {"ts": message_ts, "ok": True}
            mock_web.chat_update.return_value = {"ok": True}
            mock_web_class.return_value = mock_web

            _setup_socket_mock(mock_socket_class, action_template, "U_TEST", message_ts)

            result = request_failure_direction(
                step,
                "exit code 1",
                config,
                include_retry_warning=include_retry_warning,
            )

        assert result == expected_direction
        return result

    def test_failure_direction_continue(self, make_step, make_config):
        """Continue button returns FailureDirection.CONTINUE."""
        self._run_direction_test(make_step, make_config, "continue_{ts}", FailureDirection.CONTINUE)

    def test_failure_direction_retry(self, make_step, make_config):
        """Retry button returns FailureDirection.RETRY."""
        self._run_direction_test(make_step, make_config, "retry_{ts}", FailureDirection.RETRY)

    def test_failure_direction_skip(self, make_step, make_config):
        """Skip button returns FailureDirection.SKIP."""
        self._run_direction_test(make_step, make_config, "skip_{ts}", FailureDirection.SKIP)

    def test_failure_direction_abort(self, make_step, make_config):
        """Abort button returns FailureDirection.ABORT."""
        self._run_direction_test(make_step, make_config, "abort_{ts}", FailureDirection.ABORT)

    def test_failure_direction_timeout_returns_abort(self, make_step, make_config):
        """No response within timeout returns FailureDirection.ABORT (safe default)."""
        step = make_step(risk_level=RiskLevel.MODIFYING)
        config = make_config(timeout_seconds=0)
        message_ts = "9876543210.timeout"

        with (
            patch("slack_sdk.WebClient") as mock_web_class,
            patch("slack_sdk.socket_mode.SocketModeClient") as mock_socket_class,
        ):
            mock_web = MagicMock()
            mock_web.chat_postMessage.return_value = {"ts": message_ts, "ok": True}
            mock_web.chat_update.return_value = {"ok": True}
            mock_web_class.return_value = mock_web

            mock_socket_instance = MagicMock()
            mock_socket_instance.socket_mode_request_listeners = []
            mock_socket_instance.connect.return_value = None
            mock_socket_class.return_value = mock_socket_instance

            result = request_failure_direction(step, "exit code 1", config)

        assert result == FailureDirection.ABORT

    def test_include_retry_warning_true(self, make_step, make_config):
        """include_retry_warning=True includes warning text in the posted message."""
        step = make_step(risk_level=RiskLevel.DESTRUCTIVE)
        config = make_config(timeout_seconds=0)
        message_ts = "9876543210.warning"

        with (
            patch("slack_sdk.WebClient") as mock_web_class,
            patch("slack_sdk.socket_mode.SocketModeClient") as mock_socket_class,
        ):
            mock_web = MagicMock()
            mock_web.chat_postMessage.return_value = {"ts": message_ts, "ok": True}
            mock_web.chat_update.return_value = {"ok": True}
            mock_web_class.return_value = mock_web

            mock_socket_instance = MagicMock()
            mock_socket_instance.socket_mode_request_listeners = []
            mock_socket_instance.connect.return_value = None
            mock_socket_class.return_value = mock_socket_instance

            request_failure_direction(step, "exit code 1", config, include_retry_warning=True)

        # Inspect the blocks passed to chat_update (the second call with real ts)
        update_calls = mock_web.chat_update.call_args_list
        # Find the call that has blocks with the warning text
        all_blocks_text = str(update_calls)
        assert "WARNING" in all_blocks_text
        assert "Retry may have unintended side effects" in all_blocks_text

    def test_include_retry_warning_false(self, make_step, make_config):
        """include_retry_warning=False does not include warning text in the posted message."""
        step = make_step(risk_level=RiskLevel.MODIFYING)
        config = make_config(timeout_seconds=0)
        message_ts = "9876543210.nowarning"

        with (
            patch("slack_sdk.WebClient") as mock_web_class,
            patch("slack_sdk.socket_mode.SocketModeClient") as mock_socket_class,
        ):
            mock_web = MagicMock()
            mock_web.chat_postMessage.return_value = {"ts": message_ts, "ok": True}
            mock_web.chat_update.return_value = {"ok": True}
            mock_web_class.return_value = mock_web

            mock_socket_instance = MagicMock()
            mock_socket_instance.socket_mode_request_listeners = []
            mock_socket_instance.connect.return_value = None
            mock_socket_class.return_value = mock_socket_instance

            request_failure_direction(step, "exit code 1", config, include_retry_warning=False)

        update_calls = mock_web.chat_update.call_args_list
        all_blocks_text = str(update_calls)
        assert "Retry may have unintended side effects" not in all_blocks_text

    def test_slack_api_failure_raises_approval_error(self, make_step, make_config):
        """Slack API failure raises ApprovalError."""
        step = make_step(risk_level=RiskLevel.MODIFYING)
        config = make_config()

        with patch("slack_sdk.WebClient") as mock_web_class:
            mock_web = MagicMock()
            mock_web.chat_postMessage.side_effect = Exception("Connection refused")
            mock_web_class.return_value = mock_web

            with pytest.raises(ApprovalError, match="Connection refused"):
                request_failure_direction(step, "exit code 1", config)

    def test_keyboard_interrupt_updates_slack_message(self, make_step, make_config):
        """KeyboardInterrupt during wait updates Slack message to interrupted text and closes connection."""
        step = make_step(risk_level=RiskLevel.MODIFYING)
        config = make_config(timeout_seconds=30)
        message_ts = "9876543210.interrupt"

        with (
            patch("slack_sdk.WebClient") as mock_web_class,
            patch("slack_sdk.socket_mode.SocketModeClient") as mock_socket_class,
        ):
            mock_web = MagicMock()
            mock_web.chat_postMessage.return_value = {"ts": message_ts, "ok": True}
            mock_web.chat_update.return_value = {"ok": True}
            mock_web_class.return_value = mock_web

            mock_socket_instance = MagicMock()
            mock_socket_instance.socket_mode_request_listeners = []
            mock_socket_instance.connect.side_effect = KeyboardInterrupt
            mock_socket_class.return_value = mock_socket_instance

            with pytest.raises(KeyboardInterrupt):
                request_failure_direction(step, "exit code 1", config)

        # The message should have been updated with the interrupted text
        update_calls = mock_web.chat_update.call_args_list
        interrupted_texts = [
            c
            for c in update_calls
            if "interrupted" in str(c).lower() or "no action taken" in str(c).lower()
        ]
        assert len(interrupted_texts) >= 1

        # Socket should have been closed
        mock_socket_instance.close.assert_called()


# ---------------------------------------------------------------------------
# Coverage gap tests — error branches and edge cases
# ---------------------------------------------------------------------------


class TestApprovalCoverageGaps:
    """Tests targeting previously uncovered branches in approval.py."""

    def test_update_message_resolved_swallows_exception(self, make_step, make_config):
        """_update_message_resolved logs a warning but does not raise on chat_update failure."""
        from runbook_exec.approval import _update_message_resolved
        import logging

        mock_web = MagicMock()
        mock_web.chat_update.side_effect = Exception("network error")

        # Should not raise — exception is swallowed with a warning
        with patch("runbook_exec.approval.logger") as mock_logger:
            _update_message_resolved(mock_web, "#channel", "123.456", "decision text")

        mock_logger.warning.assert_called_once()
        assert "network error" in str(mock_logger.warning.call_args)

    def test_chat_update_failure_after_post_raises_approval_error(self, make_step, make_config):
        """ApprovalError is raised when chat_update (to set real action IDs) fails."""
        step = make_step(risk_level=RiskLevel.MODIFYING)
        config = make_config()
        message_ts = "1234567890.update_fail"

        with patch("slack_sdk.WebClient") as mock_web_class:
            mock_web = MagicMock()
            mock_web.chat_postMessage.return_value = {"ts": message_ts, "ok": True}
            mock_web.chat_update.side_effect = Exception("update failed")
            mock_web_class.return_value = mock_web

            with pytest.raises(ApprovalError, match="update failed"):
                request_approval(step, config)

    def test_non_block_actions_event_ignored_in_approval(self, make_step, make_config):
        """Socket events with type != 'block_actions' are silently ignored."""
        step = make_step(risk_level=RiskLevel.MODIFYING)
        config = make_config(timeout_seconds=0)
        message_ts = "1234567890.non_block"

        with (
            patch("slack_sdk.WebClient") as mock_web_class,
            patch("slack_sdk.socket_mode.SocketModeClient") as mock_socket_class,
        ):
            mock_web = MagicMock()
            mock_web.chat_postMessage.return_value = {"ts": message_ts, "ok": True}
            mock_web.chat_update.return_value = {"ok": True}
            mock_web_class.return_value = mock_web

            mock_socket_instance = MagicMock()
            mock_socket_instance.socket_mode_request_listeners = []

            def fire_non_block_action():
                # Fire a non-block_actions event — should be ignored
                req = Mock()
                req.envelope_id = "env-002"
                req.payload = {"type": "message", "text": "hello"}
                for listener in mock_socket_instance.socket_mode_request_listeners:
                    listener(mock_socket_instance, req)

            mock_socket_instance.connect.side_effect = fire_non_block_action
            mock_socket_class.return_value = mock_socket_instance

            result = request_approval(step, config)

        # Should time out since the non-block event was ignored
        assert result.timed_out is True

    def test_empty_actions_list_ignored_in_approval(self, make_step, make_config):
        """block_actions events with an empty actions list are silently ignored."""
        step = make_step(risk_level=RiskLevel.MODIFYING)
        config = make_config(timeout_seconds=0)
        message_ts = "1234567890.empty_actions"

        with (
            patch("slack_sdk.WebClient") as mock_web_class,
            patch("slack_sdk.socket_mode.SocketModeClient") as mock_socket_class,
        ):
            mock_web = MagicMock()
            mock_web.chat_postMessage.return_value = {"ts": message_ts, "ok": True}
            mock_web.chat_update.return_value = {"ok": True}
            mock_web_class.return_value = mock_web

            mock_socket_instance = MagicMock()
            mock_socket_instance.socket_mode_request_listeners = []

            def fire_empty_actions():
                req = Mock()
                req.envelope_id = "env-003"
                req.payload = {
                    "type": "block_actions",
                    "user": {"id": "U_TEST"},
                    "container": {"message_ts": message_ts},
                    "actions": [],  # empty!
                }
                for listener in mock_socket_instance.socket_mode_request_listeners:
                    listener(mock_socket_instance, req)

            mock_socket_instance.connect.side_effect = fire_empty_actions
            mock_socket_class.return_value = mock_socket_instance

            result = request_approval(step, config)

        assert result.timed_out is True

    def test_failure_direction_chat_update_failure_raises_approval_error(
        self, make_step, make_config
    ):
        """ApprovalError is raised when chat_update fails in request_failure_direction."""
        step = make_step(risk_level=RiskLevel.MODIFYING)
        config = make_config()
        message_ts = "9876543210.update_fail"

        with patch("slack_sdk.WebClient") as mock_web_class:
            mock_web = MagicMock()
            mock_web.chat_postMessage.return_value = {"ts": message_ts, "ok": True}
            mock_web.chat_update.side_effect = Exception("direction update failed")
            mock_web_class.return_value = mock_web

            with pytest.raises(ApprovalError, match="direction update failed"):
                request_failure_direction(step, "exit code 1", config)

    def test_non_block_actions_event_ignored_in_failure_direction(self, make_step, make_config):
        """Non-block_actions events are ignored in request_failure_direction."""
        step = make_step(risk_level=RiskLevel.MODIFYING)
        config = make_config(timeout_seconds=0)
        message_ts = "9876543210.non_block"

        with (
            patch("slack_sdk.WebClient") as mock_web_class,
            patch("slack_sdk.socket_mode.SocketModeClient") as mock_socket_class,
        ):
            mock_web = MagicMock()
            mock_web.chat_postMessage.return_value = {"ts": message_ts, "ok": True}
            mock_web.chat_update.return_value = {"ok": True}
            mock_web_class.return_value = mock_web

            mock_socket_instance = MagicMock()
            mock_socket_instance.socket_mode_request_listeners = []

            def fire_non_block():
                req = Mock()
                req.envelope_id = "env-004"
                req.payload = {"type": "shortcut", "callback_id": "my_shortcut"}
                for listener in mock_socket_instance.socket_mode_request_listeners:
                    listener(mock_socket_instance, req)

            mock_socket_instance.connect.side_effect = fire_non_block
            mock_socket_class.return_value = mock_socket_instance

            result = request_failure_direction(step, "exit code 1", config)

        assert result == FailureDirection.ABORT  # timeout default

    def test_empty_actions_ignored_in_failure_direction(self, make_step, make_config):
        """Empty actions list is ignored in request_failure_direction.

        Tests the inner handler directly to ensure the 'if not actions: return'
        branch is covered regardless of closure timing.
        """
        from runbook_exec.approval import _build_failure_direction_blocks

        step = make_step(risk_level=RiskLevel.MODIFYING)
        config = make_config(timeout_seconds=0)
        message_ts = "9876543210.empty_actions"

        captured_handler = []

        with (
            patch("slack_sdk.WebClient") as mock_web_class,
            patch("slack_sdk.socket_mode.SocketModeClient") as mock_socket_class,
        ):
            mock_web = MagicMock()
            mock_web.chat_postMessage.return_value = {"ts": message_ts, "ok": True}
            mock_web.chat_update.return_value = {"ok": True}
            mock_web_class.return_value = mock_web

            mock_socket_instance = MagicMock()
            mock_socket_instance.socket_mode_request_listeners = []

            def capture_and_fire():
                # Fire with empty actions using the real ts
                req = Mock()
                req.envelope_id = "env-005"
                req.payload = {
                    "type": "block_actions",
                    "user": {"id": "U_TEST"},
                    "container": {"message_ts": message_ts},
                    "actions": [],
                }
                for listener in mock_socket_instance.socket_mode_request_listeners:
                    listener(mock_socket_instance, req)

            mock_socket_instance.connect.side_effect = capture_and_fire
            mock_socket_class.return_value = mock_socket_instance

            result = request_failure_direction(step, "exit code 1", config)

        assert result == FailureDirection.ABORT

    def test_unknown_action_id_ignored_in_failure_direction(self, make_step, make_config):
        """An unrecognised action_id is silently ignored (direction stays None)."""
        step = make_step(risk_level=RiskLevel.MODIFYING)
        config = make_config(timeout_seconds=0)
        message_ts = "9876543210.unknown_action"

        with (
            patch("slack_sdk.WebClient") as mock_web_class,
            patch("slack_sdk.socket_mode.SocketModeClient") as mock_socket_class,
        ):
            mock_web = MagicMock()
            mock_web.chat_postMessage.return_value = {"ts": message_ts, "ok": True}
            mock_web.chat_update.return_value = {"ok": True}
            mock_web_class.return_value = mock_web

            mock_socket_instance = MagicMock()
            mock_socket_instance.socket_mode_request_listeners = []

            def fire_unknown_action():
                req = Mock()
                req.envelope_id = "env-006"
                req.payload = {
                    "type": "block_actions",
                    "user": {"id": "U_TEST"},
                    "container": {"message_ts": message_ts},
                    "actions": [{"action_id": "unknown_action_xyz"}],
                }
                for listener in mock_socket_instance.socket_mode_request_listeners:
                    listener(mock_socket_instance, req)

            mock_socket_instance.connect.side_effect = fire_unknown_action
            mock_socket_class.return_value = mock_socket_instance

            result = request_failure_direction(step, "exit code 1", config)

        # Unknown action → no result set → timeout → ABORT
        assert result == FailureDirection.ABORT

    def test_stale_message_ts_ignored_in_failure_direction(self, make_step, make_config):
        """block_actions events for a different message ts are ignored in failure direction."""
        step = make_step(risk_level=RiskLevel.MODIFYING)
        config = make_config(timeout_seconds=0)
        message_ts = "9876543210.stale_ts"
        stale_ts = "0000000000.000000"

        with (
            patch("slack_sdk.WebClient") as mock_web_class,
            patch("slack_sdk.socket_mode.SocketModeClient") as mock_socket_class,
        ):
            mock_web = MagicMock()
            mock_web.chat_postMessage.return_value = {"ts": message_ts, "ok": True}
            mock_web.chat_update.return_value = {"ok": True}
            mock_web_class.return_value = mock_web

            # Fire a click for a DIFFERENT message ts (stale)
            _setup_socket_mock(mock_socket_class, "continue_{ts}", "U_TEST", stale_ts)

            result = request_failure_direction(step, "exit code 1", config)

        # Should time out because the stale event was ignored
        assert result == FailureDirection.ABORT


# ---------------------------------------------------------------------------
# Terminal fallback tests (no Slack configured)
# ---------------------------------------------------------------------------


class TestTerminalFallback:
    """Tests for terminal-mode approval and failure direction (no Slack tokens)."""

    def test_terminal_approval_approved(self, make_step, make_config):
        """Without Slack, request_approval prompts in terminal; 'y' → approved=True."""
        step = make_step(risk_level=RiskLevel.MODIFYING)
        config = make_config(slack_bot_token="", slack_app_token="")

        with patch("runbook_exec.approval.Confirm.ask", return_value=True):
            result = request_approval(step, config)

        assert result.approved is True
        assert result.approver_slack_id is None
        assert result.timed_out is False

    def test_terminal_approval_denied(self, make_step, make_config):
        """Without Slack, request_approval prompts in terminal; 'n' → approved=False."""
        step = make_step(risk_level=RiskLevel.DESTRUCTIVE)
        config = make_config(slack_bot_token="", slack_app_token="")

        with patch("runbook_exec.approval.Confirm.ask", return_value=False):
            result = request_approval(step, config)

        assert result.approved is False
        assert result.timed_out is False

    def test_terminal_failure_direction_continue(self, make_step, make_config):
        """Without Slack, failure direction 'continue' → FailureDirection.CONTINUE."""
        step = make_step(risk_level=RiskLevel.READ_ONLY)
        config = make_config(slack_bot_token="", slack_app_token="")

        with patch("runbook_exec.approval.Prompt.ask", return_value="continue"):
            result = request_failure_direction(step, "exit code 1", config)

        assert result == FailureDirection.CONTINUE

    def test_terminal_failure_direction_retry(self, make_step, make_config):
        """Without Slack, failure direction 'retry' → FailureDirection.RETRY."""
        step = make_step(risk_level=RiskLevel.MODIFYING)
        config = make_config(slack_bot_token="", slack_app_token="")

        with patch("runbook_exec.approval.Prompt.ask", return_value="retry"):
            result = request_failure_direction(step, "exit code 1", config)

        assert result == FailureDirection.RETRY

    def test_terminal_failure_direction_skip(self, make_step, make_config):
        """Without Slack, failure direction 'skip' → FailureDirection.SKIP."""
        step = make_step(risk_level=RiskLevel.MODIFYING)
        config = make_config(slack_bot_token="", slack_app_token="")

        with patch("runbook_exec.approval.Prompt.ask", return_value="skip"):
            result = request_failure_direction(step, "exit code 1", config)

        assert result == FailureDirection.SKIP

    def test_terminal_failure_direction_abort(self, make_step, make_config):
        """Without Slack, failure direction 'abort' → FailureDirection.ABORT."""
        step = make_step(risk_level=RiskLevel.DESTRUCTIVE)
        config = make_config(slack_bot_token="", slack_app_token="")

        with patch("runbook_exec.approval.Prompt.ask", return_value="abort"):
            result = request_failure_direction(step, "exit code 1", config)

        assert result == FailureDirection.ABORT

    def test_terminal_failure_direction_shows_retry_warning(self, make_step, make_config):
        """Without Slack, include_retry_warning=True prints a warning before prompting."""
        step = make_step(risk_level=RiskLevel.DESTRUCTIVE)
        config = make_config(slack_bot_token="", slack_app_token="")

        with (
            patch("runbook_exec.approval.Prompt.ask", return_value="abort"),
            patch("runbook_exec.display.show_warning") as mock_warn,
        ):
            request_failure_direction(
                step, "exit code 1", config, include_retry_warning=True
            )

        warning_texts = " ".join(str(c) for c in mock_warn.call_args_list)
        assert "destructive" in warning_texts.lower() or "retry" in warning_texts.lower()

    def test_slack_path_still_used_when_configured(self, make_step, make_config):
        """When Slack tokens are present, the Slack path is used (not terminal)."""
        step = make_step(risk_level=RiskLevel.MODIFYING)
        config = make_config()  # has slack tokens by default
        message_ts = "1111111111.111111"

        with (
            patch("slack_sdk.WebClient") as mock_web_class,
            patch("slack_sdk.socket_mode.SocketModeClient") as mock_socket_class,
            patch("runbook_exec.approval.Confirm.ask") as mock_confirm,
        ):
            mock_web = MagicMock()
            mock_web.chat_postMessage.return_value = {"ts": message_ts, "ok": True}
            mock_web.chat_update.return_value = {"ok": True}
            mock_web_class.return_value = mock_web

            _setup_socket_mock(mock_socket_class, "approve_{ts}", "U_TEST", message_ts)

            result = request_approval(step, config)

        # Terminal prompt should NOT have been called
        mock_confirm.assert_not_called()
        assert result.approved is True
        assert result.approver_slack_id == "U_TEST"
