"""Tests for runbook_exec/llm.py — Anthropic API adapter with retry logic."""

from unittest.mock import MagicMock, Mock, call, patch

import pytest

from runbook_exec.exceptions import LLMError
from runbook_exec.llm import call_llm

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_response(text: str) -> Mock:
    """Build a minimal mock that looks like an anthropic Messages response."""
    response = Mock()
    response.content = [Mock(text=text)]
    return response


# ---------------------------------------------------------------------------
# Successful call
# ---------------------------------------------------------------------------

def test_call_llm_success(mock_anthropic_client):
    """Successful call returns the text from the first content block."""
    mock_anthropic_client.messages.create.return_value = _make_response("hello world")

    result = call_llm(
        prompt="Say hello",
        system="You are helpful.",
        model="claude-sonnet-4-5",
        max_tokens=256,
    )

    assert result == "hello world"
    mock_anthropic_client.messages.create.assert_called_once()


# ---------------------------------------------------------------------------
# Retry: first call fails, second succeeds
# ---------------------------------------------------------------------------

def test_call_llm_retries_on_first_failure(mock_anthropic_client):
    """When the first attempt raises, the second attempt succeeds and its text is returned."""
    mock_anthropic_client.messages.create.side_effect = [
        RuntimeError("transient error"),
        _make_response("recovered"),
    ]

    with patch("runbook_exec.llm.time.sleep") as mock_sleep:
        result = call_llm(
            prompt="Do something",
            system="System prompt",
            model="claude-sonnet-4-5",
        )
    assert mock_anthropic_client.messages.create.call_count == 2
    # Should have slept once (after the first failure) with the first backoff delay
    mock_sleep.assert_called_once_with(1)


# ---------------------------------------------------------------------------
# All retries exhausted → LLMError
# ---------------------------------------------------------------------------

def test_call_llm_raises_llm_error_after_all_retries(mock_anthropic_client):
    """When all 3 attempts fail, LLMError is raised with the last exception message."""
    mock_anthropic_client.messages.create.side_effect = RuntimeError("persistent failure")

    with patch("runbook_exec.llm.time.sleep") as mock_sleep, pytest.raises(LLMError) as exc_info:
        call_llm(
            prompt="Do something",
            system="System prompt",
            model="claude-sonnet-4-5",
        )

    assert "persistent failure" in str(exc_info.value)
    assert mock_anthropic_client.messages.create.call_count == 3
    # Slept after attempt 1 (1s) and attempt 2 (2s); no sleep after final attempt
    assert mock_sleep.call_count == 2
    mock_sleep.assert_has_calls([call(1), call(2)])


# ---------------------------------------------------------------------------
# Correct model and API key are passed to the client
# ---------------------------------------------------------------------------

def test_call_llm_passes_correct_model_and_api_key(monkeypatch):
    """The correct model identifier and API key are forwarded to the Anthropic client."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-abc123")

    with patch("anthropic.Anthropic") as mock_anthropic_class:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _make_response("ok")
        mock_anthropic_class.return_value = mock_client

        call_llm(
            prompt="Hello",
            system="System",
            model="claude-opus-4",
            max_tokens=512,
        )

        # Anthropic client constructed with the env-var key
        mock_anthropic_class.assert_called_once_with(api_key="test-key-abc123")

        # messages.create called with the right model and max_tokens
        mock_client.messages.create.assert_called_once_with(
            model="claude-opus-4",
            max_tokens=512,
            system="System",
            messages=[{"role": "user", "content": "Hello"}],
        )
