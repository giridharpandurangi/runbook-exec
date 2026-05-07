"""Shared test fixtures for runbook-exec test suite.

Provides factory fixtures for creating test data and mocked external dependencies.
"""

from unittest.mock import MagicMock, Mock, patch

import pytest

from runbook_exec.models import Config, RiskLevel, Step


@pytest.fixture
def make_step():
    """Factory fixture that returns a Step with sensible defaults.

    Accepts keyword overrides for any Step field.

    Example:
        step = make_step(index=1, text="Check disk space", command="df -h")
        step = make_step(index=2, risk_level=RiskLevel.DESTRUCTIVE)
    """

    def _make_step(
        index: int = 1,
        section: str | None = None,
        text: str = "Test step",
        command: str | None = "echo test",
        risk_level: RiskLevel | None = None,
        classification_reasoning: str | None = None,
    ) -> Step:
        return Step(
            index=index,
            section=section,
            text=text,
            command=command,
            risk_level=risk_level,
            classification_reasoning=classification_reasoning,
        )

    return _make_step


@pytest.fixture
def make_config():
    """Factory fixture that returns a Config with test-safe defaults.

    Accepts keyword overrides for any Config field.

    Test-safe defaults:
    - No real tokens (empty strings)
    - Short timeout (10 seconds instead of 300)
    - dry_run=False (allows testing live execution paths)
    - audit_log_dir points to a test directory

    Example:
        config = make_config()
        config = make_config(dry_run=True, timeout_seconds=5)
        config = make_config(auto_approve_level=RiskLevel.MODIFYING)
    """

    def _make_config(
        llm_model: str = "claude-sonnet-4-5",
        slack_channel: str = "#test-channel",
        slack_bot_token: str = "xoxb-test-token",
        slack_app_token: str = "xapp-test-token",
        timeout_seconds: int = 10,  # Short timeout for tests
        auto_approve_level: RiskLevel | None = None,
        audit_log_dir: str = "./test-audit-logs",
        no_llm_context: bool = False,
        dry_run: bool = False,
        incident_id: str | None = None,
    ) -> Config:
        return Config(
            llm_model=llm_model,
            slack_channel=slack_channel,
            slack_bot_token=slack_bot_token,
            slack_app_token=slack_app_token,
            timeout_seconds=timeout_seconds,
            auto_approve_level=auto_approve_level,
            audit_log_dir=audit_log_dir,
            no_llm_context=no_llm_context,
            dry_run=dry_run,
            incident_id=incident_id,
        )

    return _make_config


@pytest.fixture
def mock_anthropic_client():
    """Fixture that patches anthropic.Anthropic and returns a configurable mock.

    The mock's messages.create method returns a valid classification JSON by default.

    Usage:
        def test_classification(mock_anthropic_client):
            mock_client = mock_anthropic_client
            # Default response is valid classification JSON
            result = classifier.classify_step(step, config)

            # Configure custom response:
            mock_client.messages.create.return_value.content = [
                Mock(text='{"risk_level": "destructive", "reasoning": "Uses rm -rf"}')
            ]

    Returns:
        Mock: The mocked Anthropic client instance with configurable messages.create
    """
    with patch("anthropic.Anthropic") as mock_anthropic_class:
        # Create the mock client instance
        mock_client = MagicMock()

        # Configure messages.create to return a valid classification response
        mock_response = Mock()
        mock_response.content = [
            Mock(
                text='{"risk_level": "read_only", "reasoning": "Read-only command with no side effects"}'
            )
        ]
        mock_client.messages.create.return_value = mock_response

        # Make the Anthropic class constructor return our mock client
        mock_anthropic_class.return_value = mock_client

        yield mock_client


@pytest.fixture
def mock_slack_client():
    """Fixture that patches slack_sdk clients with immediate Approve behavior.

    Patches both WebClient (for posting messages) and SocketModeClient (for receiving interactions).
    Default behavior simulates an immediate Approve button click from user U_TEST.

    Usage:
        def test_approval(mock_slack_client):
            web_client, socket_client = mock_slack_client
            # Default: immediate approval from U_TEST
            result = approval.request_approval(step, config)
            assert result.approved is True
            assert result.approver_slack_id == "U_TEST"

            # Configure custom behavior:
            socket_client.configure_response(approved=False, user_id="U_ADMIN")

    Returns:
        tuple: (mock_web_client, mock_socket_client) with configurable behavior
    """
    with patch("slack_sdk.WebClient") as mock_web_class, patch(
        "slack_sdk.socket_mode.SocketModeClient"
    ) as mock_socket_class:
        # Create mock WebClient
        mock_web_client = MagicMock()
        mock_web_client.chat_postMessage.return_value = {"ts": "1234567890.123456", "ok": True}
        mock_web_client.chat_update.return_value = {"ok": True}
        mock_web_class.return_value = mock_web_client

        # Create mock SocketModeClient
        mock_socket_client = MagicMock()

        # Default behavior: immediate Approve click from U_TEST
        def default_connect_handler():
            """Simulate immediate approval when socket connects."""
            # This will be called by the approval workflow
            # The actual event handling is set up by the code under test
            pass

        mock_socket_client.connect = Mock(side_effect=default_connect_handler)
        mock_socket_client.close = Mock()

        # Helper method to configure response behavior
        def configure_response(
            approved: bool = True,
            user_id: str = "U_TEST",
            timeout: bool = False,
            action: str = "approve",
        ):
            """Configure the mock to simulate a specific user interaction.

            Args:
                approved: Whether the approval was granted (for approval requests)
                user_id: Slack user ID of the responder
                timeout: Whether to simulate a timeout (no response)
                action: Action taken (approve, deny, continue, retry, skip, abort)
            """
            mock_socket_client._test_approved = approved
            mock_socket_client._test_user_id = user_id
            mock_socket_client._test_timeout = timeout
            mock_socket_client._test_action = action

        # Attach configuration helper to the mock
        mock_socket_client.configure_response = configure_response

        # Set default configuration
        configure_response(approved=True, user_id="U_TEST")

        mock_socket_class.return_value = mock_socket_client

        yield (mock_web_client, mock_socket_client)
