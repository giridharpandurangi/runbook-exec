"""LLM adapter for runbook-exec.

Thin wrapper over the Anthropic SDK with retry logic.
All LLM calls go through call_llm — no business logic here.
"""

import os
import time

import anthropic

from runbook_exec.exceptions import LLMError


def call_llm(
    prompt: str,
    system: str,
    model: str,
    max_tokens: int = 1024,
) -> str:
    """Call Claude and return the text response. Retries 3x with exponential backoff.

    Args:
        prompt: The user message to send.
        system: The system prompt.
        model: The Claude model identifier (e.g. "claude-sonnet-4-5").
        max_tokens: Maximum tokens in the response.

    Returns:
        The text content of the first response block.

    Raises:
        LLMError: When all 3 retry attempts are exhausted.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    client = anthropic.Anthropic(api_key=api_key)

    delays = [1, 2, 4]
    last_exc: Exception | None = None

    for attempt, delay in enumerate(delays, start=1):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text
        except Exception as exc:
            last_exc = exc
            if attempt < len(delays):
                time.sleep(delay)

    raise LLMError(f"LLM call failed after {len(delays)} attempts: {last_exc}")
