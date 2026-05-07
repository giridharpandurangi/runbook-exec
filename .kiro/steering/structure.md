# Project Structure

## Repository Layout

```
runbook-exec/
├── pyproject.toml              # Build metadata, dependencies, tool config
├── README.md                   # Quickstart, config docs, LLM data disclosure
├── .runbook-exec.toml          # Default config (optional, user-provided)
├── .gitignore                  # Must include runbook-exec-logs/ (audit logs are never committed)
├── .github/
│   └── workflows/
│       └── validate.yml        # CI: runs `runbook-exec validate` on PRs
├── examples/
│   └── disk-full.md            # Sample runbook bundled with the package
├── runbook_exec/               # Main package (snake_case)
│   ├── __init__.py
│   ├── cli.py                  # CLI entry point (run, validate, replay subcommands)
│   ├── parser.py               # Markdown runbook parser → Step list
│   ├── classifier.py           # LLM-powered risk level classifier
│   ├── executor.py             # Step execution orchestrator
│   ├── shell.py                # Shell_Executor: subprocess, timeout, output capture
│   ├── approval.py             # Slack-based approval workflow
│   ├── audit.py                # Audit log writer with hash chain
│   ├── config.py               # Config loader (.runbook-exec.toml + CLI flag merging)
│   ├── llm.py                  # Anthropic API client wrapper with retry logic
│   ├── display.py              # Rich terminal output (color-coded status)
│   └── models.py               # Shared data models (Step, AuditEntry, RiskLevel, etc.)
└── tests/
    ├── conftest.py             # Shared fixtures, mocked LLM/Slack clients
    ├── test_parser.py          # Unit tests + property-based round-trip tests
    ├── test_classifier.py      # Unit tests with mocked LLM
    ├── test_shell.py           # Unit tests for subprocess execution
    ├── test_audit.py           # Unit tests + property-based hash chain tests
    ├── test_executor.py        # Integration tests for end-to-end execution
    └── test_cli.py             # CLI subcommand tests
```

## Module Responsibilities

| Module | Responsibility |
|---|---|
| `cli.py` | Argument parsing, flag handling, wires components together |
| `parser.py` | Pure Markdown → `Step` list transformation, no side effects |
| `classifier.py` | Calls `llm.py` to assign `RiskLevel` to each `Step` |
| `executor.py` | Orchestrates parse → classify → approve → execute → audit loop |
| `shell.py` | Runs subprocess with timeout, captures stdout/stderr/exit code |
| `approval.py` | Posts to Slack, waits for Approve/Deny button response |
| `audit.py` | Appends `AuditEntry` records with hash chaining to JSON file |
| `config.py` | Merges `.runbook-exec.toml` defaults with CLI flag overrides |
| `llm.py` | Anthropic API calls with 3-retry exponential backoff |
| `display.py` | All `rich` console output; no display logic in other modules |
| `models.py` | Dataclasses/Pydantic models shared across modules |

## Key Conventions

- **No display logic outside `display.py`** — other modules return data, never print directly
- **No LLM calls outside `llm.py` and `classifier.py`** — all Anthropic API access goes through `llm.py`
- **`parser.py` must be pure** — no I/O, no randomness, deterministic given the same input
- **All tests mock LLM and Slack** — never make real API calls in tests
- **Audit log files are append-only** — never overwrite an existing file
- **Risk level tie-breaking always favors the more cautious level**

## Audit Log Runtime Location

Audit logs are runtime output — never committed to source control.

- Default directory: `./runbook-exec-logs/` (relative to working directory)
- Override via `RUNBOOK_EXEC_LOG_DIR` environment variable or `audit_log_dir` in `.runbook-exec.toml`
- `runbook-exec-logs/` must be listed in `.gitignore` — logs contain timestamps, command output, and approver IDs

## Audit Log File Naming

```
runbook-exec-logs/{incident-id}-{timestamp}-{rand4}.json   # when --incident-id is provided
runbook-exec-logs/{runbook-filename}-{timestamp}-{rand4}.json  # fallback
```

`{timestamp}` = `YYYYMMDDTHHMMSSZ`, `{rand4}` = 4-char random hex (e.g. `a3f1`). File opened in `'x'` mode — fails loudly on collision.

## Data Models (models.py)

Core types that flow through the system:

- `RiskLevel`: enum — `read_only | modifying | destructive`
- `Step`: parsed runbook step with text, command, section header, index
- `AuditEntry`: single log record with action, timestamp, step details, output, approver, prev_hash, hash
- `ExecutionResult`: stdout, stderr, exit_code, duration_seconds
- `Config`: merged runtime configuration
