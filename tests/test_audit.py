"""Tests for audit.py — create_audit_log and AuditLog file creation.

This file covers the create_audit_log sub-task:
  - File naming convention ({stem}-{timestamp}-{rand4}.json)
  - Directory creation
  - 'x' mode exclusive create — second call with same path raises AuditError
  - incident_id used as stem when provided
  - runbook stem used as fallback
  - AuditLog is a usable context manager
"""

import re
from pathlib import Path
from unittest.mock import patch

import pytest

from runbook_exec.audit import AuditLog, create_audit_log
from runbook_exec.exceptions import AuditError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FILENAME_RE = re.compile(
    r"^(?P<stem>[^-].*?)-(?P<ts>\d{8}T\d{6}Z)-(?P<rand>[0-9a-f]{4})\.json$"
)


def _parse_filename(path: Path) -> re.Match:
    """Assert the filename matches the expected pattern and return the match."""
    m = _FILENAME_RE.match(path.name)
    assert m is not None, f"Filename '{path.name}' does not match expected pattern"
    return m


# ---------------------------------------------------------------------------
# File naming
# ---------------------------------------------------------------------------


def test_filename_uses_runbook_stem_by_default(tmp_path, make_config):
    """When no incident_id is set, the runbook stem is used as the filename prefix."""
    runbook = tmp_path / "disk-full.md"
    config = make_config(audit_log_dir=str(tmp_path / "logs"))

    log = create_audit_log(config, runbook)
    log.close()

    m = _parse_filename(log.path)
    assert m.group("stem") == "disk-full"


def test_filename_uses_incident_id_when_provided(tmp_path, make_config):
    """When incident_id is set in config, it is used as the filename prefix."""
    runbook = tmp_path / "disk-full.md"
    config = make_config(
        audit_log_dir=str(tmp_path / "logs"),
        incident_id="INC-1234",
    )

    log = create_audit_log(config, runbook)
    log.close()

    m = _parse_filename(log.path)
    assert m.group("stem") == "INC-1234"


def test_filename_timestamp_format(tmp_path, make_config):
    """Timestamp portion of filename matches YYYYMMDDTHHMMSSZ format."""
    runbook = tmp_path / "runbook.md"
    config = make_config(audit_log_dir=str(tmp_path / "logs"))

    log = create_audit_log(config, runbook)
    log.close()

    m = _parse_filename(log.path)
    ts = m.group("ts")
    # Basic sanity: 8 digits, T, 6 digits, Z
    assert len(ts) == 16
    assert ts[8] == "T"
    assert ts[-1] == "Z"


def test_filename_rand_suffix_is_4_hex_chars(tmp_path, make_config):
    """Random suffix is exactly 4 lowercase hex characters."""
    runbook = tmp_path / "runbook.md"
    config = make_config(audit_log_dir=str(tmp_path / "logs"))

    log = create_audit_log(config, runbook)
    log.close()

    m = _parse_filename(log.path)
    rand = m.group("rand")
    assert len(rand) == 4
    assert all(c in "0123456789abcdef" for c in rand)


def test_filename_rand_suffix_varies_across_calls(tmp_path, make_config):
    """Two calls produce different random suffixes (probabilistic — fails ~1 in 65536)."""
    runbook = tmp_path / "runbook.md"
    config = make_config(audit_log_dir=str(tmp_path / "logs"))

    # Freeze timestamp so only the rand suffix can differ
    with patch("runbook_exec.audit.datetime") as mock_dt:
        mock_dt.now.return_value.strftime.return_value = "20260507T120000Z"

        log1 = create_audit_log(config, runbook)
        log1.close()
        log2 = create_audit_log(config, runbook)
        log2.close()

    assert log1.path.name != log2.path.name


# ---------------------------------------------------------------------------
# Directory creation
# ---------------------------------------------------------------------------


def test_creates_log_directory_if_absent(tmp_path, make_config):
    """create_audit_log creates the audit_log_dir if it does not exist."""
    log_dir = tmp_path / "nested" / "logs"
    assert not log_dir.exists()

    runbook = tmp_path / "runbook.md"
    config = make_config(audit_log_dir=str(log_dir))

    log = create_audit_log(config, runbook)
    log.close()

    assert log_dir.is_dir()


def test_log_file_is_created_in_configured_directory(tmp_path, make_config):
    """The created log file lives inside audit_log_dir."""
    log_dir = tmp_path / "logs"
    runbook = tmp_path / "runbook.md"
    config = make_config(audit_log_dir=str(log_dir))

    log = create_audit_log(config, runbook)
    log.close()

    assert log.path.parent == log_dir
    assert log.path.exists()


# ---------------------------------------------------------------------------
# Exclusive create ('x' mode) — collision raises AuditError
# ---------------------------------------------------------------------------


def test_collision_raises_audit_error(tmp_path, make_config):
    """If the generated filename already exists, AuditError is raised."""
    runbook = tmp_path / "runbook.md"
    config = make_config(audit_log_dir=str(tmp_path / "logs"))

    # Force a deterministic filename so we can pre-create it
    fixed_name = "runbook-20260507T120000Z-abcd.json"
    log_dir = Path(config.audit_log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / fixed_name).write_text("existing content", encoding="utf-8")

    with (
        patch("runbook_exec.audit.datetime") as mock_dt,
        patch("runbook_exec.audit.secrets.token_hex", return_value="abcd"),
    ):
        mock_dt.now.return_value.strftime.return_value = "20260507T120000Z"

        with pytest.raises(AuditError, match="already exists"):
            create_audit_log(config, runbook)


# ---------------------------------------------------------------------------
# AuditLog context manager
# ---------------------------------------------------------------------------


def test_audit_log_is_context_manager(tmp_path, make_config):
    """AuditLog can be used as a context manager; file is closed on exit."""
    runbook = tmp_path / "runbook.md"
    config = make_config(audit_log_dir=str(tmp_path / "logs"))

    with create_audit_log(config, runbook) as log:
        assert isinstance(log, AuditLog)
        assert not log._file.closed

    assert log._file.closed


def test_audit_log_context_manager_closes_on_exception(tmp_path, make_config):
    """AuditLog __exit__ closes the file even when an exception is raised."""
    runbook = tmp_path / "runbook.md"
    config = make_config(audit_log_dir=str(tmp_path / "logs"))

    log_ref = None
    with pytest.raises(RuntimeError):
        with create_audit_log(config, runbook) as log:
            log_ref = log
            raise RuntimeError("simulated error")

    assert log_ref is not None
    assert log_ref._file.closed


def test_audit_log_path_property(tmp_path, make_config):
    """AuditLog.path returns the path to the created file."""
    runbook = tmp_path / "runbook.md"
    config = make_config(audit_log_dir=str(tmp_path / "logs"))

    with create_audit_log(config, runbook) as log:
        assert log.path.exists()
        assert log.path.suffix == ".json"


def test_close_is_idempotent(tmp_path, make_config):
    """Calling close() twice does not raise."""
    runbook = tmp_path / "runbook.md"
    config = make_config(audit_log_dir=str(tmp_path / "logs"))

    log = create_audit_log(config, runbook)
    log.close()
    log.close()  # should not raise


# ---------------------------------------------------------------------------
# append() — basic behaviour
# ---------------------------------------------------------------------------


def test_append_returns_audit_entry(tmp_path, make_config):
    """append() returns a fully populated AuditEntry."""
    from runbook_exec.models import ActionType, AuditEntry

    runbook = tmp_path / "runbook.md"
    config = make_config(audit_log_dir=str(tmp_path / "logs"))

    with create_audit_log(config, runbook) as log:
        entry = log.append({"action": ActionType.PARSE})

    assert isinstance(entry, AuditEntry)
    assert entry.action == ActionType.PARSE
    assert entry.seq == 1
    assert entry.hash  # non-empty string
    assert entry.prev_hash is None  # first entry has no predecessor


def test_append_seq_increments(tmp_path, make_config):
    """Each call to append() increments the sequence number by 1."""
    from runbook_exec.models import ActionType

    runbook = tmp_path / "runbook.md"
    config = make_config(audit_log_dir=str(tmp_path / "logs"))

    with create_audit_log(config, runbook) as log:
        e1 = log.append({"action": ActionType.PARSE})
        e2 = log.append({"action": ActionType.CLASSIFY})
        e3 = log.append({"action": ActionType.EXECUTE})

    assert e1.seq == 1
    assert e2.seq == 2
    assert e3.seq == 3


def test_append_links_prev_hash(tmp_path, make_config):
    """Each entry's prev_hash equals the previous entry's hash."""
    from runbook_exec.models import ActionType

    runbook = tmp_path / "runbook.md"
    config = make_config(audit_log_dir=str(tmp_path / "logs"))

    with create_audit_log(config, runbook) as log:
        e1 = log.append({"action": ActionType.PARSE})
        e2 = log.append({"action": ActionType.CLASSIFY})
        e3 = log.append({"action": ActionType.EXECUTE})

    assert e2.prev_hash == e1.hash
    assert e3.prev_hash == e2.hash


def test_append_first_entry_prev_hash_is_none(tmp_path, make_config):
    """The first appended entry always has prev_hash=None."""
    from runbook_exec.models import ActionType

    runbook = tmp_path / "runbook.md"
    config = make_config(audit_log_dir=str(tmp_path / "logs"))

    with create_audit_log(config, runbook) as log:
        entry = log.append({"action": ActionType.PARSE})

    assert entry.prev_hash is None


def test_append_writes_ndjson_line(tmp_path, make_config):
    """Each appended entry is written as a single JSON line to the file."""
    import json

    from runbook_exec.models import ActionType

    runbook = tmp_path / "runbook.md"
    config = make_config(audit_log_dir=str(tmp_path / "logs"))

    with create_audit_log(config, runbook) as log:
        log.append({"action": ActionType.PARSE})
        log.append({"action": ActionType.CLASSIFY})
        path = log.path

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    # Each line must be valid JSON
    for line in lines:
        obj = json.loads(line)
        assert "hash" in obj
        assert "seq" in obj


def test_append_entry_readable_back(tmp_path, make_config):
    """Entries written by append() can be read back as valid AuditEntry objects."""
    import json

    from runbook_exec.models import ActionType, AuditEntry

    runbook = tmp_path / "runbook.md"
    config = make_config(audit_log_dir=str(tmp_path / "logs"))

    with create_audit_log(config, runbook) as log:
        original = log.append(
            {
                "action": ActionType.EXECUTE,
                "step_index": 1,
                "step_text": "Check disk space",
                "command": "df -h",
            }
        )
        path = log.path

    raw = json.loads(path.read_text(encoding="utf-8").strip())
    recovered = AuditEntry(**raw)

    assert recovered.seq == original.seq
    assert recovered.hash == original.hash
    assert recovered.action == ActionType.EXECUTE
    assert recovered.command == "df -h"


def test_append_to_closed_log_raises_audit_error(tmp_path, make_config):
    """Appending to a closed AuditLog raises AuditError."""
    from runbook_exec.models import ActionType

    runbook = tmp_path / "runbook.md"
    config = make_config(audit_log_dir=str(tmp_path / "logs"))

    log = create_audit_log(config, runbook)
    log.close()

    with pytest.raises(AuditError, match="closed"):
        log.append({"action": ActionType.PARSE})


def test_append_hash_is_deterministic(tmp_path, make_config):
    """Two entries with identical content (same timestamp mocked) produce the same hash."""
    import json
    from unittest.mock import patch

    from runbook_exec.models import ActionType

    runbook = tmp_path / "runbook.md"
    config = make_config(audit_log_dir=str(tmp_path / "logs"))

    fixed_ts = "2026-05-07T12:00:00Z"

    with create_audit_log(config, runbook) as log:
        with patch("runbook_exec.audit.datetime") as mock_dt:
            mock_dt.now.return_value.strftime.return_value = fixed_ts
            e1 = log.append({"action": ActionType.PARSE})

    runbook2 = tmp_path / "runbook2.md"
    config2 = make_config(audit_log_dir=str(tmp_path / "logs2"))

    with create_audit_log(config2, runbook2) as log2:
        with patch("runbook_exec.audit.datetime") as mock_dt:
            mock_dt.now.return_value.strftime.return_value = fixed_ts
            e2 = log2.append({"action": ActionType.PARSE})

    # Same content → same hash
    assert e1.hash == e2.hash


# ---------------------------------------------------------------------------
# load_log — round-trip through file
# ---------------------------------------------------------------------------


def test_load_log_returns_valid_audit_entries(tmp_path, make_config):
    """load_log reads back entries written by append() as valid AuditEntry objects."""
    from runbook_exec.audit import load_log
    from runbook_exec.models import ActionType, AuditEntry

    runbook = tmp_path / "runbook.md"
    config = make_config(audit_log_dir=str(tmp_path / "logs"))

    with create_audit_log(config, runbook) as log:
        log.append({"action": ActionType.PARSE})
        log.append({"action": ActionType.CLASSIFY, "step_index": 1, "step_text": "Check disk"})
        log.append({"action": ActionType.EXECUTE, "command": "df -h", "exit_code": 0})
        path = log.path

    entries = load_log(path)

    assert len(entries) == 3
    assert all(isinstance(e, AuditEntry) for e in entries)
    assert entries[0].action == ActionType.PARSE
    assert entries[1].action == ActionType.CLASSIFY
    assert entries[1].step_text == "Check disk"
    assert entries[2].command == "df -h"
    assert entries[2].exit_code == 0


def test_load_log_preserves_hash_fields(tmp_path, make_config):
    """load_log preserves hash and prev_hash fields exactly as written."""
    from runbook_exec.audit import load_log
    from runbook_exec.models import ActionType

    runbook = tmp_path / "runbook.md"
    config = make_config(audit_log_dir=str(tmp_path / "logs"))

    with create_audit_log(config, runbook) as log:
        e1 = log.append({"action": ActionType.PARSE})
        e2 = log.append({"action": ActionType.EXECUTE})
        path = log.path

    entries = load_log(path)

    assert entries[0].hash == e1.hash
    assert entries[0].prev_hash is None
    assert entries[1].hash == e2.hash
    assert entries[1].prev_hash == e1.hash


# ---------------------------------------------------------------------------
# verify_chain — intact chain
# ---------------------------------------------------------------------------


def test_verify_chain_returns_empty_for_intact_chain(tmp_path, make_config):
    """verify_chain returns [] when all entries form a valid hash chain."""
    from runbook_exec.audit import load_log, verify_chain
    from runbook_exec.models import ActionType

    runbook = tmp_path / "runbook.md"
    config = make_config(audit_log_dir=str(tmp_path / "logs"))

    with create_audit_log(config, runbook) as log:
        log.append({"action": ActionType.PARSE})
        log.append({"action": ActionType.CLASSIFY})
        log.append({"action": ActionType.EXECUTE})
        path = log.path

    entries = load_log(path)
    broken = verify_chain(entries)

    assert broken == []


def test_verify_chain_empty_list_returns_empty(tmp_path, make_config):
    """verify_chain on an empty list returns []."""
    from runbook_exec.audit import verify_chain

    assert verify_chain([]) == []


def test_verify_chain_single_entry_intact(tmp_path, make_config):
    """verify_chain on a single valid entry returns []."""
    from runbook_exec.audit import load_log, verify_chain
    from runbook_exec.models import ActionType

    runbook = tmp_path / "runbook.md"
    config = make_config(audit_log_dir=str(tmp_path / "logs"))

    with create_audit_log(config, runbook) as log:
        log.append({"action": ActionType.PARSE})
        path = log.path

    entries = load_log(path)
    assert verify_chain(entries) == []


# ---------------------------------------------------------------------------
# verify_chain — tampered entries
# ---------------------------------------------------------------------------


def test_verify_chain_detects_tampered_hash_field(tmp_path, make_config):
    """verify_chain returns the seq of an entry whose hash field was altered."""
    from runbook_exec.audit import load_log, verify_chain
    from runbook_exec.models import ActionType

    runbook = tmp_path / "runbook.md"
    config = make_config(audit_log_dir=str(tmp_path / "logs"))

    with create_audit_log(config, runbook) as log:
        log.append({"action": ActionType.PARSE})
        log.append({"action": ActionType.EXECUTE})
        log.append({"action": ActionType.SUMMARY})
        path = log.path

    entries = load_log(path)

    # Tamper with the second entry's hash field
    tampered = entries[1].model_copy(update={"hash": "a" * 64})
    entries[1] = tampered

    broken = verify_chain(entries)

    # seq 2 is broken (own hash wrong), seq 3 is broken (prev_hash points to tampered entry)
    assert 2 in broken


def test_verify_chain_detects_tampered_content_field(tmp_path, make_config):
    """verify_chain returns the seq of an entry whose content was altered."""
    from runbook_exec.audit import load_log, verify_chain
    from runbook_exec.models import ActionType

    runbook = tmp_path / "runbook.md"
    config = make_config(audit_log_dir=str(tmp_path / "logs"))

    with create_audit_log(config, runbook) as log:
        log.append({"action": ActionType.PARSE})
        log.append({"action": ActionType.EXECUTE, "command": "df -h"})
        log.append({"action": ActionType.SUMMARY})
        path = log.path

    entries = load_log(path)

    # Tamper with the second entry's command field (content change, hash unchanged)
    tampered = entries[1].model_copy(update={"command": "rm -rf /"})
    entries[1] = tampered

    broken = verify_chain(entries)

    # seq 2 is broken because recomputed hash won't match stored hash
    assert 2 in broken


def test_verify_chain_detects_tampered_first_entry(tmp_path, make_config):
    """verify_chain detects tampering of the first entry."""
    from runbook_exec.audit import load_log, verify_chain
    from runbook_exec.models import ActionType

    runbook = tmp_path / "runbook.md"
    config = make_config(audit_log_dir=str(tmp_path / "logs"))

    with create_audit_log(config, runbook) as log:
        log.append({"action": ActionType.PARSE})
        log.append({"action": ActionType.EXECUTE})
        path = log.path

    entries = load_log(path)

    # Tamper with the first entry's content
    tampered = entries[0].model_copy(update={"reasoning": "injected"})
    entries[0] = tampered

    broken = verify_chain(entries)

    assert 1 in broken


def test_verify_chain_returns_correct_seq_numbers_for_multiple_tampered(tmp_path, make_config):
    """verify_chain returns all seq numbers where the chain is broken."""
    from runbook_exec.audit import load_log, verify_chain
    from runbook_exec.models import ActionType

    runbook = tmp_path / "runbook.md"
    config = make_config(audit_log_dir=str(tmp_path / "logs"))

    with create_audit_log(config, runbook) as log:
        for action in [ActionType.PARSE, ActionType.CLASSIFY, ActionType.EXECUTE, ActionType.SUMMARY]:
            log.append({"action": action})
        path = log.path

    entries = load_log(path)

    # Tamper with entry at seq=2 (index 1): change its content but keep hash
    entries[1] = entries[1].model_copy(update={"reasoning": "tampered"})

    broken = verify_chain(entries)

    # seq 2 is broken (own hash mismatch); seq 3 is broken (prev_hash mismatch)
    assert 2 in broken
    assert 3 in broken


# ---------------------------------------------------------------------------
# 'x' mode — second create_audit_log with same path raises AuditError
# ---------------------------------------------------------------------------


def test_second_create_with_same_path_raises_audit_error(tmp_path, make_config):
    """create_audit_log raises AuditError when the target file already exists ('x' mode)."""
    runbook = tmp_path / "runbook.md"
    config = make_config(audit_log_dir=str(tmp_path / "logs"))

    # Force a deterministic filename so both calls target the same file
    with (
        patch("runbook_exec.audit.datetime") as mock_dt,
        patch("runbook_exec.audit.secrets.token_hex", return_value="beef"),
    ):
        mock_dt.now.return_value.strftime.return_value = "20260101T000000Z"

        log1 = create_audit_log(config, runbook)
        log1.close()

        with pytest.raises(AuditError, match="already exists"):
            create_audit_log(config, runbook)


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


@pytest.mark.property
def test_property_any_sequence_of_entries_has_intact_chain(tmp_path, make_config):
    """**Validates: Requirements 8.3**

    Property: any sequence of N entries (1–20) appended to a fresh AuditLog
    produces a chain where verify_chain returns [].
    """
    from hypothesis import given, settings
    from hypothesis import strategies as st

    from runbook_exec.audit import load_log, verify_chain
    from runbook_exec.models import ActionType

    action_values = list(ActionType)

    @settings(max_examples=100)
    @given(
        actions=st.lists(
            st.sampled_from(action_values),
            min_size=1,
            max_size=20,
        )
    )
    def _property(actions):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            log_dir = Path(td) / "logs"
            cfg = make_config(audit_log_dir=str(log_dir))
            rb = Path(td) / "runbook.md"

            with create_audit_log(cfg, rb) as log:
                for action in actions:
                    log.append({"action": action})
                p = log.path

            entries = load_log(p)
            broken = verify_chain(entries)
            assert broken == [], f"Chain broken at seqs {broken} for {len(actions)} entries"

    _property()


@pytest.mark.property
def test_property_mutating_any_field_breaks_chain(tmp_path, make_config):
    """**Validates: Requirements 8.4**

    Property: mutating any single non-hash field of any entry in a valid chain
    causes verify_chain to include that entry's seq in the broken list.
    """
    from hypothesis import assume, given, settings
    from hypothesis import strategies as st

    from runbook_exec.audit import load_log, verify_chain
    from runbook_exec.models import ActionType

    action_values = list(ActionType)

    # Fields that can be mutated to a different string value without breaking Pydantic validation
    mutable_fields = ["reasoning", "step_text", "command", "output", "stdout", "stderr"]

    @settings(max_examples=100)
    @given(
        actions=st.lists(
            st.sampled_from(action_values),
            min_size=1,
            max_size=10,
        ),
        entry_idx=st.integers(min_value=0, max_value=9),
        field_name=st.sampled_from(mutable_fields),
        new_value=st.text(min_size=1, max_size=50).filter(lambda s: s.isprintable()),
    )
    def _property(actions, entry_idx, field_name, new_value):
        import tempfile

        # Clamp entry_idx to valid range
        entry_idx = entry_idx % len(actions)

        with tempfile.TemporaryDirectory() as td:
            log_dir = Path(td) / "logs"
            cfg = make_config(audit_log_dir=str(log_dir))
            rb = Path(td) / "runbook.md"

            with create_audit_log(cfg, rb) as log:
                for action in actions:
                    log.append({"action": action})
                p = log.path

            entries = load_log(p)

            # Verify chain is intact before mutation
            assert verify_chain(entries) == []

            original_entry = entries[entry_idx]
            original_value = getattr(original_entry, field_name)

            # Skip if the new value happens to equal the original (no actual mutation)
            assume(new_value != original_value)

            # Mutate the field (keep hash unchanged — simulates content tampering)
            mutated = original_entry.model_copy(update={field_name: new_value})
            entries[entry_idx] = mutated

            broken = verify_chain(entries)
            target_seq = original_entry.seq

            assert target_seq in broken, (
                f"Expected seq {target_seq} in broken list after mutating "
                f"field '{field_name}' on entry {entry_idx}, but got {broken}"
            )

    _property()


# ---------------------------------------------------------------------------
# Error paths in create_audit_log
# ---------------------------------------------------------------------------


def test_create_audit_log_raises_on_mkdir_failure(tmp_path, make_config):
    """create_audit_log raises AuditError when the directory cannot be created."""
    import sys
    from unittest.mock import patch

    runbook = tmp_path / "runbook.md"
    config = make_config(audit_log_dir=str(tmp_path / "logs"))

    with patch("pathlib.Path.mkdir", side_effect=OSError("permission denied")):
        with pytest.raises(AuditError, match="Cannot create audit log directory"):
            create_audit_log(config, runbook)


def test_create_audit_log_raises_on_file_open_oserror(tmp_path, make_config):
    """create_audit_log raises AuditError when the file cannot be opened (non-FileExistsError OSError)."""
    from unittest.mock import patch

    runbook = tmp_path / "runbook.md"
    config = make_config(audit_log_dir=str(tmp_path / "logs"))

    # Patch open() to raise a generic OSError (not FileExistsError)
    with patch("builtins.open", side_effect=OSError("disk full")):
        with pytest.raises(AuditError, match="Cannot create audit log file"):
            create_audit_log(config, runbook)


# ---------------------------------------------------------------------------
# Error paths in AuditLog.append
# ---------------------------------------------------------------------------


def test_append_raises_audit_error_on_write_failure(tmp_path, make_config):
    """append() raises AuditError when the file write fails with OSError."""
    from runbook_exec.models import ActionType

    runbook = tmp_path / "runbook.md"
    config = make_config(audit_log_dir=str(tmp_path / "logs"))

    with create_audit_log(config, runbook) as log:
        # Patch the file's write method to raise OSError
        log._file.write = lambda _: (_ for _ in ()).throw(OSError("disk full"))
        with pytest.raises(AuditError, match="Failed to write audit entry"):
            log.append({"action": ActionType.PARSE})


# ---------------------------------------------------------------------------
# Error paths in load_log
# ---------------------------------------------------------------------------


def test_load_log_raises_on_missing_file(tmp_path):
    """load_log raises AuditError when the file does not exist."""
    from runbook_exec.audit import load_log

    missing = tmp_path / "nonexistent.json"
    with pytest.raises(AuditError, match="not found"):
        load_log(missing)


def test_load_log_raises_on_oserror(tmp_path):
    """load_log raises AuditError when the file cannot be read (OSError)."""
    from unittest.mock import patch
    from runbook_exec.audit import load_log

    fake_path = tmp_path / "fake.json"
    fake_path.write_text("{}", encoding="utf-8")

    with patch("pathlib.Path.read_text", side_effect=OSError("permission denied")):
        with pytest.raises(AuditError, match="Cannot read audit log file"):
            load_log(fake_path)


def test_load_log_raises_on_invalid_json(tmp_path):
    """load_log raises AuditError when a line contains invalid JSON."""
    from runbook_exec.audit import load_log

    bad_file = tmp_path / "bad.json"
    bad_file.write_text("this is not json\n", encoding="utf-8")

    with pytest.raises(AuditError, match="Invalid JSON"):
        load_log(bad_file)


def test_load_log_raises_on_invalid_audit_entry(tmp_path):
    """load_log raises AuditError when a JSON line doesn't conform to AuditEntry schema."""
    from runbook_exec.audit import load_log

    bad_file = tmp_path / "bad_entry.json"
    # Valid JSON but missing required fields for AuditEntry
    bad_file.write_text('{"seq": 1, "action": "parse"}\n', encoding="utf-8")

    with pytest.raises(AuditError, match="Invalid AuditEntry"):
        load_log(bad_file)


def test_load_log_skips_blank_lines(tmp_path, make_config):
    """load_log skips blank lines in the NDJSON file."""
    from runbook_exec.audit import load_log
    from runbook_exec.models import ActionType

    runbook = tmp_path / "runbook.md"
    config = make_config(audit_log_dir=str(tmp_path / "logs"))

    with create_audit_log(config, runbook) as log:
        log.append({"action": ActionType.PARSE})
        path = log.path

    # Inject blank lines into the file
    content = path.read_text(encoding="utf-8")
    path.write_text("\n" + content + "\n\n", encoding="utf-8")

    entries = load_log(path)
    assert len(entries) == 1
