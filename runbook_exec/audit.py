"""Audit log module for runbook-exec.

Provides an append-only NDJSON audit log with SHA-256 hash chain verification.
Each entry hashes the previous entry, making tampering detectable.
"""

import hashlib
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import IO

from runbook_exec.exceptions import AuditError
from runbook_exec.models import AuditEntry, Config


def _canonical_json(data: dict) -> str:
    """Produce canonical JSON: keys sorted, no whitespace.

    This is the string that gets hashed for the chain. The ``hash`` field
    itself is excluded before calling this function so the hash covers
    everything *except* the hash field.

    Args:
        data: Dictionary to serialise.

    Returns:
        Compact JSON string with sorted keys.
    """
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def _hash_entry(entry_data: dict) -> str:
    """Compute the SHA-256 hash of an entry's canonical JSON.

    The ``hash`` key must be absent from *entry_data* before calling this
    function (it is excluded so the hash covers all other fields).

    Args:
        entry_data: Entry dict without the ``hash`` field.

    Returns:
        Lowercase hex SHA-256 digest string.
    """
    canonical = _canonical_json(entry_data)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _make_log_filename(config: Config, runbook_path: Path) -> str:
    """Build the audit log filename from config and runbook path.

    Format: {stem}-{timestamp}-{rand4}.json
    where timestamp = YYYYMMDDTHHMMSSZ and rand4 = 4-char random hex.

    Args:
        config: Runtime configuration (may contain incident_id).
        runbook_path: Path to the runbook being executed.

    Returns:
        Filename string (not a full path).
    """
    stem = config.incident_id if config.incident_id else runbook_path.stem
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rand4 = secrets.token_hex(2)  # 2 bytes → 4 hex chars
    return f"{stem}-{timestamp}-{rand4}.json"


def create_audit_log(config: Config, runbook_path: Path) -> "AuditLog":
    """Create a new audit log file and return a writer handle.

    The log directory is created if it does not exist. The file is opened
    in exclusive-create mode ('x') so that a collision (two processes
    generating the same filename in the same second with the same 4-char
    suffix) raises AuditError loudly rather than silently overwriting data.

    File naming: {stem}-{YYYYMMDDTHHMMSSZ}-{rand4}.json
      - stem = config.incident_id if set, otherwise runbook_path.stem
      - rand4 = 4-character random hex suffix (e.g. "a3f1")

    Args:
        config: Runtime configuration (audit_log_dir, incident_id).
        runbook_path: Path to the runbook being executed.

    Returns:
        An open AuditLog context manager ready for appending entries.

    Raises:
        AuditError: If the file already exists (collision) or the directory
                    cannot be created.
    """
    log_dir = Path(config.audit_log_dir)
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AuditError(
            f"Cannot create audit log directory '{log_dir}': {exc}"
        ) from exc

    filename = _make_log_filename(config, runbook_path)
    log_path = log_dir / filename

    try:
        file_handle: IO[str] = open(log_path, "x", encoding="utf-8")  # noqa: WPS515
    except FileExistsError as exc:
        raise AuditError(
            f"Audit log file already exists (collision): '{log_path}'. "
            "This should be extremely rare — retry the command."
        ) from exc
    except OSError as exc:
        raise AuditError(
            f"Cannot create audit log file '{log_path}': {exc}"
        ) from exc

    return AuditLog(path=log_path, file_handle=file_handle)


class AuditLog:
    """Append-only NDJSON audit log with SHA-256 hash chain.

    Use as a context manager to ensure the file is always flushed and closed:

        with create_audit_log(config, runbook_path) as log:
            log.append({...})

    The hash chain is maintained automatically: each entry's prev_hash is set
    to the hash of the previous entry (None for the first entry).
    """

    def __init__(self, path: Path, file_handle: IO[str]) -> None:
        self._path = path
        self._file = file_handle
        self._seq = 0
        self._prev_hash: str | None = None

    @property
    def path(self) -> Path:
        """Path to the audit log file."""
        return self._path

    def __enter__(self) -> "AuditLog":
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.close()

    def close(self) -> None:
        """Flush and close the underlying file."""
        if not self._file.closed:
            self._file.flush()
            self._file.close()

    def append(self, entry_data: dict) -> AuditEntry:
        """Build, hash, and append an AuditEntry to the log.

        Automatically assigns:
        - ``seq``: next monotonically increasing sequence number (1-based).
        - ``timestamp``: current UTC time in ISO 8601 format.
        - ``prev_hash``: hash of the previous entry, or ``None`` for the first.
        - ``hash``: SHA-256 of this entry's canonical JSON (all fields except ``hash``).

        The entry is written as a single JSON line (NDJSON) and flushed
        immediately so partial writes are visible even if the process is
        interrupted.

        Args:
            entry_data: Partial entry fields as a dict. ``seq``, ``timestamp``,
                        ``prev_hash``, and ``hash`` are computed here and must
                        not be supplied by the caller (they will be overwritten).

        Returns:
            The fully populated and written ``AuditEntry``.

        Raises:
            AuditError: If the file is already closed or the write fails.
        """
        if self._file.closed:
            raise AuditError("Cannot append to a closed audit log.")

        self._seq += 1

        # Build the full entry dict, overwriting any caller-supplied chain fields.
        full_data = dict(entry_data)
        full_data["seq"] = self._seq
        full_data["timestamp"] = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        full_data["prev_hash"] = self._prev_hash

        # Validate and construct the Pydantic model first (raises ValidationError on bad data).
        # Use a placeholder hash so Pydantic can fill in all defaults; we replace it below.
        full_data["hash"] = ""
        entry = AuditEntry(**full_data)

        # Compute hash over the *full* canonical entry (all fields except ``hash``).
        # Using the Pydantic-serialised dict ensures verify_chain can reproduce the
        # same hash from the stored entry without needing the original sparse dict.
        entry_dict = entry.model_dump(mode="json")
        entry_dict.pop("hash")
        entry_hash = _hash_entry(entry_dict)

        # Patch the hash back onto the model.
        entry = entry.model_copy(update={"hash": entry_hash})

        # Serialise using Pydantic's JSON encoder so enums etc. are handled correctly,
        # then write as a single NDJSON line.
        try:
            self._file.write(entry.model_dump_json() + "\n")
            self._file.flush()
        except OSError as exc:
            raise AuditError(f"Failed to write audit entry to '{self._path}': {exc}") from exc

        # Advance the chain pointer.
        self._prev_hash = entry_hash

        return entry


def verify_chain(entries: list[AuditEntry]) -> list[int]:
    """Return list of seq numbers where the hash chain is broken.

    For each entry, two checks are performed:
    1. The entry's own hash is recomputed and compared against ``entry.hash``.
    2. The entry's ``prev_hash`` is checked against the actual hash of the
       previous entry (or ``None`` for the first entry).

    If either check fails, the entry's ``seq`` is added to the broken list.

    Args:
        entries: List of ``AuditEntry`` objects in the order they appear in
                 the log (i.e. sorted by ``seq``).

    Returns:
        List of ``seq`` numbers where the chain is broken.  An empty list
        means the chain is fully intact.
    """
    broken: list[int] = []

    for i, entry in enumerate(entries):
        # --- Check 1: recompute this entry's own hash ---
        entry_dict = entry.model_dump(mode="json")
        entry_dict.pop("hash")
        recomputed_hash = _hash_entry(entry_dict)
        own_hash_ok = recomputed_hash == entry.hash

        # --- Check 2: verify prev_hash linkage ---
        if i == 0:
            prev_hash_ok = entry.prev_hash is None
        else:
            prev_entry = entries[i - 1]
            prev_entry_dict = prev_entry.model_dump(mode="json")
            prev_entry_dict.pop("hash")
            actual_prev_hash = _hash_entry(prev_entry_dict)
            prev_hash_ok = entry.prev_hash == actual_prev_hash

        if not own_hash_ok or not prev_hash_ok:
            broken.append(entry.seq)

    return broken


def load_log(path: Path) -> list[AuditEntry]:
    """Read an NDJSON audit log file and return all entries in order.

    Each line in the file is a JSON object representing one ``AuditEntry``.
    Blank lines are skipped. Lines that cannot be parsed raise ``AuditError``
    with the offending line number so the caller can diagnose truncated or
    corrupted files.

    Args:
        path: Path to the NDJSON audit log file.

    Returns:
        List of ``AuditEntry`` objects in the order they appear in the file.

    Raises:
        AuditError: If the file cannot be opened, a line is not valid JSON,
                    or a JSON object does not conform to the ``AuditEntry`` schema.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise AuditError(f"Audit log file not found: '{path}'") from exc
    except OSError as exc:
        raise AuditError(f"Cannot read audit log file '{path}': {exc}") from exc

    entries: list[AuditEntry] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AuditError(
                f"Invalid JSON on line {lineno} of '{path}': {exc}"
            ) from exc
        try:
            entries.append(AuditEntry(**raw))
        except Exception as exc:
            raise AuditError(
                f"Invalid AuditEntry on line {lineno} of '{path}': {exc}"
            ) from exc

    return entries
