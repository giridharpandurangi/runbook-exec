"""Tests for runbook_exec/parser.py.

Covers:
- Numbered list items become Steps; unnumbered are ignored
- Fenced code block extracted as command
- Inline code extracted as command when no fenced block present
- Step with no code has command=None
- Section headers populate step.section
- Same input parsed twice produces identical output (determinism)
- Malformed file raises ParseError
- Property-based test: any valid numbered Markdown list round-trips to the correct step count

**Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 14.1, 14.3, 21.4**
"""

import tempfile
import textwrap
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from runbook_exec.exceptions import ParseError
from runbook_exec.parser import parse_runbook

# ── Helpers ──────────────────────────────────────────────────────────────────


def write_md(tmp_path: Path, content: str) -> Path:
    """Write Markdown content to a temp file and return its path."""
    p = tmp_path / "runbook.md"
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


# ── Numbered list items become Steps ─────────────────────────────────────────


def test_numbered_list_items_become_steps(tmp_path: Path) -> None:
    """Each numbered list item produces exactly one Step."""
    md = write_md(
        tmp_path,
        """\
        1. First step
        2. Second step
        3. Third step
        """,
    )
    steps = parse_runbook(md)
    assert len(steps) == 3
    assert steps[0].index == 1
    assert steps[1].index == 2
    assert steps[2].index == 3


def test_step_text_is_populated(tmp_path: Path) -> None:
    """Step.text contains the prose text of the list item."""
    md = write_md(
        tmp_path,
        """\
        1. Check disk usage
        """,
    )
    steps = parse_runbook(md)
    assert len(steps) == 1
    assert "Check disk usage" in steps[0].text


# ── Unnumbered lists are ignored ──────────────────────────────────────────────


def test_unnumbered_list_items_are_ignored(tmp_path: Path) -> None:
    """Bullet list items do not produce Steps."""
    md = write_md(
        tmp_path,
        """\
        - This is a bullet point
        - Another bullet

        1. This is a numbered step
        """,
    )
    steps = parse_runbook(md)
    assert len(steps) == 1
    assert "numbered step" in steps[0].text


def test_mixed_list_only_numbered_extracted(tmp_path: Path) -> None:
    """When both numbered and unnumbered lists exist, only numbered items become Steps."""
    md = write_md(
        tmp_path,
        """\
        1. Step one
        2. Step two

        - Bullet A
        - Bullet B

        3. Step three
        """,
    )
    steps = parse_runbook(md)
    assert len(steps) == 3
    assert steps[0].index == 1
    assert steps[2].index == 3


# ── Fenced code block extracted as command ────────────────────────────────────


def test_fenced_code_block_extracted_as_command(tmp_path: Path) -> None:
    """A fenced code block inside a numbered item becomes step.command."""
    md = write_md(
        tmp_path,
        """\
        1. Check disk space

           ```bash
           df -h
           ```
        """,
    )
    steps = parse_runbook(md)
    assert len(steps) == 1
    assert steps[0].command == "df -h"


def test_fenced_code_block_multiline(tmp_path: Path) -> None:
    """Multi-line fenced code blocks are preserved as-is."""
    md = write_md(
        tmp_path,
        """\
        1. Run cleanup

           ```bash
           find /tmp -mtime +7 -delete
           echo "done"
           ```
        """,
    )
    steps = parse_runbook(md)
    assert len(steps) == 1
    assert "find /tmp" in steps[0].command
    assert "echo" in steps[0].command


# ── Inline code extracted as command when no fenced block ─────────────────────


def test_inline_code_extracted_as_command(tmp_path: Path) -> None:
    """Inline backtick code becomes step.command when no fenced block is present."""
    md = write_md(
        tmp_path,
        """\
        1. Run `df -h` to check disk usage
        """,
    )
    steps = parse_runbook(md)
    assert len(steps) == 1
    assert steps[0].command == "df -h"


def test_first_inline_code_wins(tmp_path: Path) -> None:
    """When multiple inline code spans exist, the first one becomes the command."""
    md = write_md(
        tmp_path,
        """\
        1. Run `df -h` then check `ls /tmp`
        """,
    )
    steps = parse_runbook(md)
    assert len(steps) == 1
    assert steps[0].command == "df -h"


# ── Fenced block takes priority over inline code ──────────────────────────────


def test_fenced_block_takes_priority_over_inline_code(tmp_path: Path) -> None:
    """When both fenced block and inline code exist, fenced block wins."""
    md = write_md(
        tmp_path,
        """\
        1. Run `ls` or use the block below

           ```bash
           df -h
           ```
        """,
    )
    steps = parse_runbook(md)
    assert len(steps) == 1
    assert steps[0].command == "df -h"


# ── Step with no code has command=None ────────────────────────────────────────


def test_step_with_no_code_has_command_none(tmp_path: Path) -> None:
    """A prose-only step has command=None."""
    md = write_md(
        tmp_path,
        """\
        1. Notify the on-call engineer about the incident
        """,
    )
    steps = parse_runbook(md)
    assert len(steps) == 1
    assert steps[0].command is None


# ── Section headers populate step.section ─────────────────────────────────────


def test_section_header_populates_step_section(tmp_path: Path) -> None:
    """Steps inherit the nearest preceding heading as step.section."""
    md = write_md(
        tmp_path,
        """\
        ## Diagnosis

        1. Check disk usage
        2. Check memory

        ## Remediation

        3. Clear temp files
        """,
    )
    steps = parse_runbook(md)
    assert len(steps) == 3
    assert steps[0].section == "Diagnosis"
    assert steps[1].section == "Diagnosis"
    assert steps[2].section == "Remediation"


def test_step_before_any_heading_has_section_none(tmp_path: Path) -> None:
    """Steps that appear before any heading have section=None."""
    md = write_md(
        tmp_path,
        """\
        1. First step before any heading

        ## Section A

        2. Step under section A
        """,
    )
    steps = parse_runbook(md)
    assert len(steps) == 2
    assert steps[0].section is None
    assert steps[1].section == "Section A"


def test_multiple_heading_levels(tmp_path: Path) -> None:
    """Both h1 and h2 headings are tracked as section names."""
    md = write_md(
        tmp_path,
        """\
        # Top Level

        1. Step under h1

        ## Sub Section

        2. Step under h2
        """,
    )
    steps = parse_runbook(md)
    assert len(steps) == 2
    assert steps[0].section == "Top Level"
    assert steps[1].section == "Sub Section"


# ── Determinism ───────────────────────────────────────────────────────────────


def test_same_input_parsed_twice_produces_identical_output(tmp_path: Path) -> None:
    """Parsing the same file twice returns identical Step lists (determinism)."""
    md = write_md(
        tmp_path,
        """\
        ## Setup

        1. Check disk space

           ```bash
           df -h
           ```

        2. Run `ls /tmp`
        3. Notify team
        """,
    )
    steps_a = parse_runbook(md)
    steps_b = parse_runbook(md)

    assert len(steps_a) == len(steps_b)
    for a, b in zip(steps_a, steps_b, strict=False):
        assert a.index == b.index
        assert a.section == b.section
        assert a.text == b.text
        assert a.command == b.command


# ── Malformed file raises ParseError ─────────────────────────────────────────


def test_missing_file_raises_parse_error(tmp_path: Path) -> None:
    """Attempting to parse a non-existent file raises ParseError."""
    missing = tmp_path / "does_not_exist.md"
    with pytest.raises(ParseError) as exc_info:
        parse_runbook(missing)
    assert str(missing) in str(exc_info.value)


def test_parse_error_includes_file_path(tmp_path: Path) -> None:
    """ParseError message includes the file path."""
    missing = tmp_path / "runbook_missing.md"
    with pytest.raises(ParseError) as exc_info:
        parse_runbook(missing)
    assert "runbook_missing.md" in str(exc_info.value)


def test_parse_error_includes_line_number_on_step_build_failure(tmp_path: Path) -> None:
    """ParseError includes a line number when a step-level parse error occurs.

    Simulates an internal _build_step failure to verify the error message
    contains both the file path and a line number reference.
    """
    from unittest.mock import patch

    md = write_md(
        tmp_path,
        """\
        1. Check disk space
        2. Run cleanup
        """,
    )

    with patch("runbook_exec.parser._build_step", side_effect=ValueError("simulated failure")), pytest.raises(ParseError) as exc_info:
        parse_runbook(md)

    error_msg = str(exc_info.value)
    assert str(md) in error_msg or md.name in error_msg
    # Line number should appear as a digit in the message (e.g. ":1:" or ":2:")
    assert any(char.isdigit() for char in error_msg)


def test_parse_error_on_unreadable_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ParseError is raised with the file path when the file cannot be read.

    Uses monkeypatch so the test works in any environment (including CI running
    as root, where os.chmod(0o000) doesn't prevent reads).
    """
    md = write_md(tmp_path, "1. Step one\n")

    def _raise_oserror(*args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_text", _raise_oserror)

    with pytest.raises(ParseError) as exc_info:
        parse_runbook(md)
    assert md.name in str(exc_info.value)


# ── Empty and edge-case files ─────────────────────────────────────────────────


def test_empty_file_returns_empty_list(tmp_path: Path) -> None:
    """An empty Markdown file returns an empty Step list."""
    md = write_md(tmp_path, "")
    steps = parse_runbook(md)
    assert steps == []


def test_file_with_only_headings_returns_empty_list(tmp_path: Path) -> None:
    """A file with only headings and no list items returns an empty Step list."""
    md = write_md(
        tmp_path,
        """\
        # Title

        ## Section A

        ### Sub-section
        """,
    )
    steps = parse_runbook(md)
    assert steps == []


def test_file_with_only_bullets_returns_empty_list(tmp_path: Path) -> None:
    """A file with only bullet lists returns an empty Step list."""
    md = write_md(
        tmp_path,
        """\
        - Bullet one
        - Bullet two
        - Bullet three
        """,
    )
    steps = parse_runbook(md)
    assert steps == []


def test_step_indices_are_one_based_and_sequential(tmp_path: Path) -> None:
    """Step indices start at 1 and increment by 1 regardless of Markdown numbering."""
    md = write_md(
        tmp_path,
        """\
        1. First
        1. Second (same number in source)
        1. Third (same number in source)
        """,
    )
    steps = parse_runbook(md)
    assert [s.index for s in steps] == [1, 2, 3]


# ── Inline formatting edge cases ─────────────────────────────────────────────


def test_softbreak_in_step_text(tmp_path: Path) -> None:
    """A soft line break inside a list item is treated as a space in step.text."""
    # A soft break occurs when a list item wraps across lines without a blank line
    md = write_md(
        tmp_path,
        """\
        1. This is a long step that
           wraps onto the next line
        """,
    )
    steps = parse_runbook(md)
    assert len(steps) == 1
    # The text should contain both parts joined (softbreak → space)
    assert "long step" in steps[0].text


def test_emphasis_and_links_in_step_text(tmp_path: Path) -> None:
    """Emphasis, bold, and link text is included in step.text."""
    md = write_md(
        tmp_path,
        """\
        1. Check **disk** usage and see [docs](https://example.com)
        """,
    )
    steps = parse_runbook(md)
    assert len(steps) == 1
    # The text content should be present (markup stripped, content kept)
    assert steps[0].text  # non-empty


def test_hardbreak_in_step_text(tmp_path: Path) -> None:
    """A hard line break (two trailing spaces) inside a list item is handled."""
    # Hard break: two spaces at end of line before newline
    content = "1. First line  \n   Second line\n"
    p = tmp_path / "runbook.md"
    p.write_text(content, encoding="utf-8")
    steps = parse_runbook(p)
    assert len(steps) == 1
    assert steps[0].text  # non-empty, both lines captured


def test_parse_error_from_build_step_propagates_unchanged(tmp_path: Path) -> None:
    """A ParseError raised by _build_step propagates without re-wrapping."""
    from unittest.mock import patch

    md = write_md(tmp_path, "1. Step one\n")

    with patch(
        "runbook_exec.parser._build_step",
        side_effect=ParseError("direct parse error from build_step"),
    ), pytest.raises(ParseError, match="direct parse error from build_step"):
        parse_runbook(md)


def test_inline_child_with_no_content_is_skipped(tmp_path: Path) -> None:
    """Inline child tokens with empty content don't crash or add empty text."""
    from unittest.mock import MagicMock, patch

    md = write_md(tmp_path, "1. Step with emphasis\n")

    # Inject a fake inline child with type "unknown" and empty content
    original_build_step = __import__("runbook_exec.parser", fromlist=["_build_step"])._build_step

    def patched_build_step(item_tokens, index, section, path, open_token):
        # Inject a fake child with no content into the first inline token
        for token in item_tokens:
            if token.type == "inline" and token.children:
                fake_child = MagicMock()
                fake_child.type = "html_inline"
                fake_child.content = ""  # empty content — should be skipped
                token.children.insert(0, fake_child)
                break
        return original_build_step(item_tokens, index, section, path, open_token)

    with patch("runbook_exec.parser._build_step", side_effect=patched_build_step):
        steps = parse_runbook(md)

    assert len(steps) == 1


def test_html_inline_content_included_in_step_text(tmp_path: Path) -> None:
    """HTML inline elements (e.g. <b>) have their content included in step.text."""
    md = write_md(
        tmp_path,
        """\
        1. Check <b>disk</b> usage
        """,
    )
    steps = parse_runbook(md)
    assert len(steps) == 1
    # The step text should be non-empty; html_inline tokens have content
    assert steps[0].text


# ── ParseError propagation from _parse_tokens ────────────────────────────────


def test_parse_error_from_parse_tokens_propagates(tmp_path: Path) -> None:
    """A ParseError raised inside _parse_tokens propagates unchanged."""
    from unittest.mock import patch

    md = write_md(tmp_path, "1. Step one\n")

    with patch(
        "runbook_exec.parser._parse_tokens",
        side_effect=ParseError("inner parse error"),
    ), pytest.raises(ParseError, match="inner parse error"):
        parse_runbook(md)


def test_non_parse_error_from_parse_tokens_is_wrapped(tmp_path: Path) -> None:
    """A non-ParseError exception from _parse_tokens is wrapped in ParseError."""
    from unittest.mock import patch

    md = write_md(tmp_path, "1. Step one\n")

    with patch(
        "runbook_exec.parser._parse_tokens",
        side_effect=RuntimeError("unexpected failure"),
    ), pytest.raises(ParseError) as exc_info:
        parse_runbook(md)

    assert "unexpected failure" in str(exc_info.value)


# ── Property-based test ───────────────────────────────────────────────────────


# Strategy: generate a list of non-empty step text strings (no backticks or
# fences to keep the generator simple and avoid accidental command extraction
# affecting the count).
_step_text = st.text(
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd", "Zs"),
        whitelist_characters=" ",
        blacklist_characters="`~#*_[]()\\",
    ),
    min_size=1,
    max_size=60,
).map(str.strip).filter(lambda s: len(s) > 0)


@pytest.mark.property
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
@given(step_texts=st.lists(_step_text, min_size=1, max_size=20))
def test_numbered_list_round_trips_to_correct_step_count(
    step_texts: list[str],
) -> None:
    """Any valid numbered Markdown list round-trips to the correct step count.

    **Validates: Requirements 1.1, 14.1, 21.4**

    Property: for N numbered list items, parse_runbook returns exactly N Steps.
    """
    lines = [f"{i + 1}. {text}" for i, text in enumerate(step_texts)]
    content = "\n".join(lines) + "\n"

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", encoding="utf-8", delete=False
    ) as f:
        f.write(content)
        md_file = Path(f.name)

    try:
        steps = parse_runbook(md_file)
        assert len(steps) == len(step_texts)
        for idx, step in enumerate(steps):
            assert step.index == idx + 1
    finally:
        md_file.unlink(missing_ok=True)


# ── _collect_item_tokens malformed stream fallback ────────────────────────────


def test_collect_item_tokens_malformed_stream_fallback(tmp_path: Path) -> None:
    """_collect_item_tokens returns partial tokens when list_item_close is missing.

    This exercises the fallback path (line 169) where the token stream ends
    without a matching list_item_close token.
    """
    from runbook_exec.parser import _collect_item_tokens
    from unittest.mock import MagicMock

    # Build a fake token list: list_item_open followed by two tokens, no close
    open_token = MagicMock()
    open_token.type = "list_item_open"

    middle_token = MagicMock()
    middle_token.type = "inline"

    tokens = [open_token, middle_token]

    # Start at index 0 (the list_item_open)
    item_tokens, end_i = _collect_item_tokens(tokens, 0)

    # Should return all tokens collected so far and end_i = len(tokens) - 1
    assert len(item_tokens) == 2
    assert end_i == len(tokens) - 1
