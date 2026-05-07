"""Tests for runbook_exec.classifier.

All LLM calls are mocked — no real Anthropic API calls are made.
"""

from unittest.mock import Mock, patch

import pytest

from runbook_exec.classifier import classify_step
from runbook_exec.exceptions import ClassificationError
from runbook_exec.models import RiskLevel

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_llm_response(risk_level: str, reasoning: str) -> Mock:
    """Build a mock that call_llm returns for a given classification."""
    mock = Mock()
    mock.return_value = f'{{"risk_level": "{risk_level}", "reasoning": "{reasoning}"}}'
    return mock


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------

class TestClassifyStepSuccess:
    def test_read_only_classification(self, make_step, make_config):
        """Mocked LLM returning read_only → step has RiskLevel.READ_ONLY."""
        step = make_step(index=1, text="Check disk space", command="df -h")
        config = make_config()

        with patch("runbook_exec.classifier.call_llm") as mock_llm:
            mock_llm.return_value = (
                '{"risk_level": "read_only", "reasoning": "df is a read-only command"}'
            )
            result = classify_step(step, config)

        assert result.risk_level == RiskLevel.READ_ONLY
        assert result.classification_reasoning == "df is a read-only command"

    def test_destructive_classification(self, make_step, make_config):
        """Mocked LLM returning destructive → step has RiskLevel.DESTRUCTIVE."""
        step = make_step(index=2, text="Remove old logs", command="rm -rf /var/log/old")
        config = make_config()

        with patch("runbook_exec.classifier.call_llm") as mock_llm:
            mock_llm.return_value = (
                '{"risk_level": "destructive", "reasoning": "rm -rf deletes data permanently"}'
            )
            result = classify_step(step, config)

        assert result.risk_level == RiskLevel.DESTRUCTIVE
        assert result.classification_reasoning == "rm -rf deletes data permanently"

    def test_modifying_classification(self, make_step, make_config):
        """Mocked LLM returning modifying → step has RiskLevel.MODIFYING."""
        step = make_step(index=3, text="Restart nginx", command="systemctl restart nginx")
        config = make_config()

        with patch("runbook_exec.classifier.call_llm") as mock_llm:
            mock_llm.return_value = (
                '{"risk_level": "modifying", "reasoning": "Restarts a service, recoverable"}'
            )
            result = classify_step(step, config)

        assert result.risk_level == RiskLevel.MODIFYING
        assert result.classification_reasoning == "Restarts a service, recoverable"

    def test_classification_reasoning_populated(self, make_step, make_config):
        """step.classification_reasoning is populated from LLM response."""
        step = make_step(index=1, text="List files", command="ls -la")
        config = make_config()
        expected_reasoning = "ls only lists directory contents without modification"

        with patch("runbook_exec.classifier.call_llm") as mock_llm:
            mock_llm.return_value = (
                f'{{"risk_level": "read_only", "reasoning": "{expected_reasoning}"}}'
            )
            result = classify_step(step, config)

        assert result.classification_reasoning == expected_reasoning

    def test_original_step_not_mutated(self, make_step, make_config):
        """classify_step returns a new Step; the original is unchanged."""
        step = make_step(index=1, text="Check disk", command="df -h")
        config = make_config()

        with patch("runbook_exec.classifier.call_llm") as mock_llm:
            mock_llm.return_value = (
                '{"risk_level": "read_only", "reasoning": "Read-only"}'
            )
            result = classify_step(step, config)

        assert step.risk_level is None
        assert step.classification_reasoning is None
        assert result is not step

    def test_step_with_no_command(self, make_step, make_config):
        """Steps with no command are still classified (prose-only steps)."""
        step = make_step(index=1, text="Notify the on-call team", command=None)
        config = make_config()

        with patch("runbook_exec.classifier.call_llm") as mock_llm:
            mock_llm.return_value = (
                '{"risk_level": "modifying", "reasoning": "External communication"}'
            )
            result = classify_step(step, config)

        assert result.risk_level == RiskLevel.MODIFYING

    def test_uses_configured_model(self, make_step, make_config):
        """classify_step passes config.llm_model to call_llm."""
        step = make_step()
        config = make_config(llm_model="claude-opus-4")

        with patch("runbook_exec.classifier.call_llm") as mock_llm:
            mock_llm.return_value = (
                '{"risk_level": "read_only", "reasoning": "Safe"}'
            )
            classify_step(step, config)

        _, kwargs = mock_llm.call_args
        assert kwargs.get("model") == "claude-opus-4" or mock_llm.call_args[0][2] == "claude-opus-4"


# ---------------------------------------------------------------------------
# Retry tests
# ---------------------------------------------------------------------------

class TestClassifyStepRetry:
    def test_malformed_json_first_call_valid_on_retry(self, make_step, make_config):
        """Malformed JSON on first call, valid on retry → succeeds."""
        step = make_step(index=1, text="Check disk", command="df -h")
        config = make_config()

        with patch("runbook_exec.classifier.call_llm") as mock_llm:
            mock_llm.side_effect = [
                "not valid json at all",
                '{"risk_level": "read_only", "reasoning": "Safe read-only command"}',
            ]
            result = classify_step(step, config)

        assert result.risk_level == RiskLevel.READ_ONLY
        assert mock_llm.call_count == 2

    def test_malformed_json_both_calls_raises_classification_error(
        self, make_step, make_config
    ):
        """Malformed JSON on both calls → raises ClassificationError."""
        step = make_step(index=1, text="Check disk", command="df -h")
        config = make_config()

        with patch("runbook_exec.classifier.call_llm") as mock_llm:
            mock_llm.side_effect = [
                "not json",
                "also not json",
            ]
            with pytest.raises(ClassificationError) as exc_info:
                classify_step(step, config)

        assert "step 1" in str(exc_info.value)
        assert mock_llm.call_count == 2

    def test_missing_risk_level_key_triggers_retry(self, make_step, make_config):
        """JSON missing 'risk_level' key triggers retry."""
        step = make_step(index=2, text="Restart service", command="systemctl restart app")
        config = make_config()

        with patch("runbook_exec.classifier.call_llm") as mock_llm:
            mock_llm.side_effect = [
                '{"reasoning": "missing risk_level key"}',
                '{"risk_level": "modifying", "reasoning": "Restarts a service"}',
            ]
            result = classify_step(step, config)

        assert result.risk_level == RiskLevel.MODIFYING
        assert mock_llm.call_count == 2

    def test_invalid_risk_level_value_triggers_retry(self, make_step, make_config):
        """JSON with an invalid risk_level value triggers retry."""
        step = make_step(index=1, text="Check disk", command="df -h")
        config = make_config()

        with patch("runbook_exec.classifier.call_llm") as mock_llm:
            mock_llm.side_effect = [
                '{"risk_level": "unknown_level", "reasoning": "Bad value"}',
                '{"risk_level": "read_only", "reasoning": "Correct"}',
            ]
            result = classify_step(step, config)

        assert result.risk_level == RiskLevel.READ_ONLY
        assert mock_llm.call_count == 2

    def test_both_calls_missing_key_raises_classification_error(
        self, make_step, make_config
    ):
        """Both calls return JSON missing required keys → ClassificationError."""
        step = make_step(index=3, text="Drop table", command="psql -c 'DROP TABLE users'")
        config = make_config()

        with patch("runbook_exec.classifier.call_llm") as mock_llm:
            mock_llm.side_effect = [
                '{"reasoning": "no risk_level"}',
                '{"reasoning": "still no risk_level"}',
            ]
            with pytest.raises(ClassificationError):
                classify_step(step, config)

        assert mock_llm.call_count == 2


# ---------------------------------------------------------------------------
# Markdown fence stripping tests
# ---------------------------------------------------------------------------

class TestStripMarkdownFences:
    """Tests for _strip_markdown_fences and its integration with classify_step."""

    def test_json_fenced_with_language_tag(self, make_step, make_config):
        """LLM returns ```json\\n{...}\\n``` → parses correctly."""
        step = make_step(index=1, text="Check disk", command="df -h")
        config = make_config()

        with patch("runbook_exec.classifier.call_llm") as mock_llm:
            mock_llm.return_value = (
                '```json\n{"risk_level": "read_only", "reasoning": "Safe"}\n```'
            )
            result = classify_step(step, config)

        assert result.risk_level == RiskLevel.READ_ONLY

    def test_json_fenced_without_language_tag(self, make_step, make_config):
        """LLM returns ```\\n{...}\\n``` (no language tag) → parses correctly."""
        step = make_step(index=1, text="Check disk", command="df -h")
        config = make_config()

        with patch("runbook_exec.classifier.call_llm") as mock_llm:
            mock_llm.return_value = (
                '```\n{"risk_level": "modifying", "reasoning": "Changes state"}\n```'
            )
            result = classify_step(step, config)

        assert result.risk_level == RiskLevel.MODIFYING

    def test_plain_json_no_fences(self, make_step, make_config):
        """LLM returns plain {...} (no fences) → still parses correctly."""
        step = make_step(index=1, text="Drop table", command="psql -c 'DROP TABLE x'")
        config = make_config()

        with patch("runbook_exec.classifier.call_llm") as mock_llm:
            mock_llm.return_value = (
                '{"risk_level": "destructive", "reasoning": "Drops a table"}'
            )
            result = classify_step(step, config)

        assert result.risk_level == RiskLevel.DESTRUCTIVE

    def test_json_fenced_with_surrounding_whitespace(self, make_step, make_config):
        """LLM returns fenced JSON with leading/trailing whitespace → parses correctly."""
        step = make_step(index=1, text="Check disk", command="df -h")
        config = make_config()

        with patch("runbook_exec.classifier.call_llm") as mock_llm:
            mock_llm.return_value = (
                '  ```json\n{"risk_level": "read_only", "reasoning": "Safe"}\n```\n  '
            )
            result = classify_step(step, config)

        assert result.risk_level == RiskLevel.READ_ONLY

    def test_tilde_fences_stripped(self, make_step, make_config):
        """LLM returns ~~~json\\n{...}\\n~~~ (tilde fences) → parses correctly."""
        step = make_step(index=1, text="Restart service", command="systemctl restart app")
        config = make_config()

        with patch("runbook_exec.classifier.call_llm") as mock_llm:
            mock_llm.return_value = (
                '~~~json\n{"risk_level": "modifying", "reasoning": "Restarts a service"}\n~~~'
            )
            result = classify_step(step, config)

        assert result.risk_level == RiskLevel.MODIFYING

    def test_strip_markdown_fences_directly(self):
        """Unit test strip_markdown_fences in isolation."""
        from runbook_exec._json_utils import strip_markdown_fences

        assert strip_markdown_fences('```json\n{"a": 1}\n```') == '{"a": 1}'
        assert strip_markdown_fences('```\n{"a": 1}\n```') == '{"a": 1}'
        assert strip_markdown_fences('{"a": 1}') == '{"a": 1}'
        assert strip_markdown_fences('  ```json\n{"a": 1}\n```\n  ') == '{"a": 1}'
        assert strip_markdown_fences('~~~\n{"a": 1}\n~~~') == '{"a": 1}'
