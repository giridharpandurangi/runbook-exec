"""Shared JSON parsing utilities for runbook-exec.

Centralises the markdown-fence stripping logic so every LLM response parser
goes through the same pre-processing step. This prevents the recurring bug
where the LLM wraps its JSON response in ```json ... ``` fences.
"""

import re

# Matches opening fence: optional whitespace, ``` or ~~~, optional language tag
_FENCE_OPEN = re.compile(r"^\s*(?:```|~~~)\w*\s*\n?", re.MULTILINE)
# Matches closing fence: optional whitespace, ``` or ~~~
_FENCE_CLOSE = re.compile(r"\n?\s*(?:```|~~~)\s*$", re.MULTILINE)


def strip_markdown_fences(text: str) -> str:
    """Strip markdown code fences from an LLM response.

    Handles all common variants:
      - ```json\\n{...}\\n```
      - ```\\n{...}\\n```
      - ~~~json\\n{...}\\n~~~
      - Leading/trailing whitespace around fences

    Args:
        text: Raw LLM response text, possibly wrapped in code fences.

    Returns:
        The text with any enclosing code fence stripped, or the original
        text unchanged if no fence is detected.
    """
    stripped = text.strip()
    # Only strip if the text actually starts with a fence marker
    if not (stripped.startswith("```") or stripped.startswith("~~~")):
        return stripped
    result = _FENCE_OPEN.sub("", stripped, count=1)
    result = _FENCE_CLOSE.sub("", result, count=1)
    return result.strip()
