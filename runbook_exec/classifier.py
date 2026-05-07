"""Step classifier for runbook-exec.

Uses the LLM to assign a RiskLevel and reasoning to each Step.
All LLM calls go through llm.call_llm — no direct Anthropic SDK usage here.
"""

import json

from runbook_exec._json_utils import strip_markdown_fences
from runbook_exec.exceptions import ClassificationError
from runbook_exec.llm import call_llm
from runbook_exec.models import Config, RiskLevel, Step

_SYSTEM_PROMPT = """\
You are a risk classifier for shell commands in operational runbooks. \
Classify each step as exactly one of: read_only, modifying, or destructive.

- read_only: commands that only observe system state \
(df, ls, cat, kubectl get, ps, curl GET, etc.)
- modifying: commands that change state but are recoverable \
(systemctl restart, logrotate, file edits, curl POST/PUT, etc.)
- destructive: commands that delete data, drop schemas, or require sudo \
for non-safelisted operations (rm -rf, DROP TABLE, kubectl delete, sudo <unknown>)

When uncertain between two levels, always choose the more cautious one.
Return ONLY the JSON object. Do not wrap it in markdown code fences. \
Do not include any prose before or after.
Respond with JSON only: {"risk_level": "<level>", "reasoning": "<one sentence>"}\
"""

# Regex patterns moved to runbook_exec._json_utils — imported as strip_markdown_fences above.


def _build_user_prompt(step: Step) -> str:
    """Build the user prompt for a step classification request."""
    command_text = step.command if step.command is not None else "(no command)"
    return f"Step {step.index}: {step.text}\nCommand: {command_text}"


def _parse_classification(response_text: str) -> tuple[RiskLevel, str]:
    """Parse the LLM JSON response into (RiskLevel, reasoning).

    Strips markdown code fences before parsing so that responses like
    ```json\\n{...}\\n``` are handled correctly.

    Args:
        response_text: Raw text from the LLM response.

    Returns:
        Tuple of (RiskLevel, reasoning string).

    Raises:
        ValueError: If the JSON is malformed or contains an invalid risk_level.
    """
    clean = strip_markdown_fences(response_text)
    data = json.loads(clean)
    risk_level_str = data["risk_level"]
    reasoning = data["reasoning"]
    risk_level = RiskLevel(risk_level_str)
    return risk_level, reasoning


def classify_step(step: Step, config: Config) -> Step:
    """Return a new Step with risk_level and classification_reasoning set.

    Calls the LLM to classify the step. If the first response is malformed JSON,
    retries the LLM call once. Raises ClassificationError if both attempts fail.

    Args:
        step: The step to classify.
        config: Runtime configuration (provides llm_model).

    Returns:
        A new Step instance with risk_level and classification_reasoning populated.

    Raises:
        ClassificationError: If the LLM returns unparseable JSON on both attempts.
        LLMError: If the underlying LLM API call fails after all retries.
    """
    user_prompt = _build_user_prompt(step)

    for attempt in range(2):
        response_text = call_llm(
            prompt=user_prompt,
            system=_SYSTEM_PROMPT,
            model=config.llm_model,
        )
        try:
            risk_level, reasoning = _parse_classification(response_text)
            return step.model_copy(
                update={
                    "risk_level": risk_level,
                    "classification_reasoning": reasoning,
                }
            )
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            if attempt == 1:
                raise ClassificationError(
                    f"LLM returned unparseable classification for step {step.index} "
                    f"after 2 attempts. Last response: {response_text!r}"
                ) from exc
            # First attempt failed — retry once

    # Unreachable, but satisfies type checkers
    raise ClassificationError(f"Classification failed for step {step.index}")  # pragma: no cover
