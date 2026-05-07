"""Markdown runbook parser.

Parses a Markdown file into an ordered list of Steps using the markdown-it-py
token stream. Only numbered (ordered) list items are extracted; unnumbered
lists are ignored entirely.
"""

from pathlib import Path

from markdown_it import MarkdownIt

from runbook_exec.exceptions import ParseError
from runbook_exec.models import Step


def parse_runbook(path: Path) -> list[Step]:
    """Parse a Markdown runbook file and return an ordered list of Steps.

    Parsing rules:
    - Only numbered (ordered) list items become Steps; unnumbered lists are ignored.
    - The nearest preceding heading becomes step.section.
    - Command extraction priority:
        1. Fenced code block inside the list item → step.command
        2. First inline code span (backtick) → step.command
        3. No code → step.command = None
    - step.index is 1-based.

    Args:
        path: Path to the Markdown file to parse.

    Returns:
        Ordered list of Step objects.

    Raises:
        ParseError: If the file cannot be read or the token stream is malformed.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ParseError(f"Cannot read runbook file '{path}': {exc}") from exc

    try:
        return _parse_tokens(text, path)
    except ParseError:
        raise
    except Exception as exc:
        raise ParseError(f"Failed to parse runbook '{path}': {exc}") from exc


def _parse_tokens(text: str, path: Path) -> list[Step]:
    """Walk the markdown-it-py token stream and extract Steps.

    The flat token stream uses these key types:
    - heading_open / inline / heading_close  — section headings
    - ordered_list_open / ordered_list_close — wraps numbered list items
    - bullet_list_open / bullet_list_close   — wraps unnumbered list items (ignored)
    - list_item_open / list_item_close       — individual list items (both types)
    - fence                                  — fenced code block (content in token.content)
    - inline                                 — prose + inline code children

    Args:
        text: Raw Markdown text.
        path: Source file path (used in error messages).

    Returns:
        Ordered list of Step objects.
    """
    md = MarkdownIt()
    tokens = md.parse(text)

    steps: list[Step] = []
    current_section: str | None = None
    step_index = 0

    # Track nesting: stack of list types ("ordered" | "bullet")
    list_type_stack: list[str] = []

    i = 0
    while i < len(tokens):
        token = tokens[i]

        # ── Section heading ──────────────────────────────────────────────────
        if token.type == "heading_open":
            # The very next token should be an inline token with the heading text
            if i + 1 < len(tokens) and tokens[i + 1].type == "inline":
                current_section = tokens[i + 1].content.strip()
            i += 1
            continue

        # ── List type tracking ────────────────────────────────────────────────
        if token.type == "ordered_list_open":
            list_type_stack.append("ordered")
            i += 1
            continue

        if token.type == "bullet_list_open":
            list_type_stack.append("bullet")
            i += 1
            continue

        if token.type in ("ordered_list_close", "bullet_list_close"):
            if list_type_stack:
                list_type_stack.pop()
            i += 1
            continue

        # ── List item ─────────────────────────────────────────────────────────
        if token.type == "list_item_open":
            # Only process items inside an ordered list
            in_ordered = bool(list_type_stack) and list_type_stack[-1] == "ordered"

            # Collect all tokens for this list item
            item_tokens, end_i = _collect_item_tokens(tokens, i)

            if in_ordered:
                step_index += 1
                try:
                    step = _build_step(
                        item_tokens=item_tokens,
                        index=step_index,
                        section=current_section,
                        path=path,
                        open_token=token,
                    )
                except ParseError:
                    raise
                except Exception as exc:
                    line = token.map[0] + 1 if token.map else "?"
                    raise ParseError(
                        f"Failed to build step {step_index} at '{path}':{line}: {exc}"
                    ) from exc
                steps.append(step)

            i = end_i + 1
            continue

        i += 1

    return steps


def _collect_item_tokens(tokens: list, start: int) -> tuple[list, int]:
    """Collect all tokens from list_item_open to its matching list_item_close.

    Handles nested list items by tracking depth.

    Args:
        tokens: Full flat token list.
        start: Index of the list_item_open token.

    Returns:
        (item_tokens, end_index) where end_index points to the
        list_item_close token.
    """
    depth = 0
    i = start
    item_tokens = []
    while i < len(tokens):
        t = tokens[i]
        item_tokens.append(t)
        if t.type == "list_item_open":
            depth += 1
        elif t.type == "list_item_close":
            depth -= 1
            if depth == 0:
                return item_tokens, i
        i += 1
    # Malformed token stream — return what we have
    return item_tokens, i - 1


def _build_step(
    item_tokens: list,
    index: int,
    section: str | None,
    path: Path,
    open_token,
) -> Step:
    """Build a Step from the tokens belonging to a single ordered list item.

    Args:
        item_tokens: Tokens from list_item_open to list_item_close.
        index: 1-based step index.
        section: Current section heading (or None).
        path: Source file path (for error messages).
        open_token: The list_item_open token (for line number).

    Returns:
        A Step instance.
    """
    full_text_parts: list[str] = []
    fenced_command: str | None = None
    inline_command: str | None = None

    for token in item_tokens:
        # Fenced code block — highest priority for command extraction
        if token.type == "fence":
            content = token.content.rstrip("\n")
            if fenced_command is None:
                fenced_command = content

        # Inline token — prose text and inline code spans
        elif token.type == "inline" and token.children:
            prose_parts: list[str] = []
            for child in token.children:
                if child.type == "text":
                    prose_parts.append(child.content)
                elif child.type == "softbreak":
                    prose_parts.append(" ")
                elif child.type == "hardbreak":
                    prose_parts.append("\n")
                elif child.type == "code_inline":
                    prose_parts.append(f"`{child.content}`")
                    # First inline code span becomes the command if no fenced block
                    if inline_command is None:
                        inline_command = child.content
                else:
                    # Other inline types (links, emphasis, etc.) — include their content
                    if child.content:
                        prose_parts.append(child.content)
            full_text_parts.append("".join(prose_parts))

    text = " ".join(part.strip() for part in full_text_parts if part.strip())

    # Command priority: fenced block > inline code > None
    command = fenced_command if fenced_command is not None else inline_command

    return Step(
        index=index,
        section=section,
        text=text,
        command=command,
        risk_level=None,
        classification_reasoning=None,
    )
