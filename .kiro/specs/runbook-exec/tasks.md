# Implementation Tasks: runbook-exec

Tasks are ordered by dependency. Complete one task fully (code + tests passing) before starting the next.

---

## Task 1: Project scaffold

**Goal:** Installable package with working CLI entry point and passing lint.

- [x] Create `pyproject.toml` with pinned dependencies, `[project.scripts]` entry point, and `[tool.ruff]` config
- [x] Create `runbook_exec/__init__.py` (version string only)
- [x] Create `runbook_exec/cli.py` — Typer app with `run`, `validate`, `replay` subcommands as stubs that print "not implemented"
- [x] Create `.gitignore` (include `runbook-exec-logs/`, `__pycache__/`, `.venv/`, `dist/`, `*.egg-info`)
- [x] Create `tests/conftest.py` (empty for now, fixtures added in later tasks)
- [x] Verify: `pip install -e ".[dev]"` succeeds, `runbook-exec --help` shows all three subcommands, `ruff check .` passes

**Dependencies:** none

---

## Task 2: Data models (`models.py`) + core fixtures

**Goal:** All shared types defined, validated, and serializable. Core test fixtures available for all later tasks.

- [x] Implement `RiskLevel`, `Step`, `ExecutionResult`, `ActionType`, `AuditEntry`, `Config`, `ExecutionSummary` per design doc
- [x] Implement `RISK_ORDER` dict and `needs_approval(step, config) -> bool` in `models.py`
- [x] Create `runbook_exec/exceptions.py` with the full error hierarchy (`RunbookExecError` and all subclasses)
- [x] Populate `tests/conftest.py` with reusable fixtures:
  - `make_step` — factory fixture that returns a `Step` with sensible defaults, accepting keyword overrides (index, text, command, risk_level, section)
  - `make_config` — factory fixture that returns a `Config` with test-safe defaults (no real tokens, short timeout, dry_run=False), accepting keyword overrides
  - `mock_anthropic_client` — fixture that patches `anthropic.Anthropic` and returns a mock where `messages.create` returns a configurable response; default response is a valid classification JSON
  - `mock_slack_client` — fixture that patches `slack_sdk.WebClient` and the Socket Mode client; default behaviour is an immediate Approve click from user `U_TEST`
- [x] Write `tests/test_models.py`:
  - `needs_approval` truth table: all 12 combinations of `auto_approve_level` × `risk_level`
  - `AuditEntry` round-trips through JSON without losing data
  - `RiskLevel` serializes to its string value in JSON

**Dependencies:** Task 1

---

## Task 3: Configuration loader (`config.py`)

**Goal:** `Config` built correctly from all three sources in priority order.

- [x] Implement `load_config(**cli_overrides) -> Config`
- [x] Read `ANTHROPIC_API_KEY`, `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` from environment; raise `ConfigError` with actionable message if `ANTHROPIC_API_KEY` is missing
- [x] Parse `.runbook-exec.toml` with `tomllib` if present; silently skip if absent
- [x] Apply CLI overrides (only non-`None` values override)
- [x] Write `tests/test_config.py`:
  - File values override defaults
  - CLI values override file values
  - Missing `ANTHROPIC_API_KEY` raises `ConfigError`
  - Missing config file uses built-in defaults without error
  - `audit_log_dir` and `timeout_seconds` defaults are correct

**Dependencies:** Task 2

---

## Task 4: Markdown parser (`parser.py`)

**Goal:** Pure, deterministic Markdown → `list[Step]` with full command extraction.

- [x] Implement `parse_runbook(path: Path) -> list[Step]` using `markdown-it-py` token stream
- [x] Extract numbered list items only; ignore unnumbered lists
- [x] Track current section heading for `step.section`
- [x] Command extraction: fenced code block takes priority over inline code span
- [x] Raise `ParseError` with file path and line number on failure
- [x] Write `tests/test_parser.py`:
  - Numbered list items become Steps; unnumbered are ignored
  - Fenced code block extracted as command
  - Inline code extracted as command when no fenced block present
  - Step with no code has `command=None`
  - Section headers populate `step.section`
  - Same input parsed twice produces identical output (determinism)
  - Malformed file raises `ParseError`
  - Property-based test (Hypothesis): any valid numbered Markdown list round-trips to the correct step count

**Dependencies:** Task 2

---

## Task 5: LLM adapter (`llm.py`)

**Goal:** Anthropic API calls with retry logic, fully mockable.

- [x] Implement `call_llm(prompt, system, model, max_tokens) -> str`
- [x] Retry up to 3 times with exponential backoff (1s, 2s, 4s delays)
- [x] Raise `LLMError` with last exception message when all retries exhausted
- [x] Write `tests/test_llm.py`:
  - Successful call returns text response
  - First call fails, second succeeds — returns success (mock `anthropic.Anthropic`)
  - All 3 retries fail → raises `LLMError`
  - Correct model and API key are passed to the client

**Dependencies:** Task 2

---

## Task 6: Step classifier (`classifier.py`)

**Goal:** Each Step gets a `RiskLevel` and reasoning via LLM.

- [x] Implement `classify_step(step: Step, config: Config) -> Step`
- [x] Build system + user prompts per design doc
- [x] Parse JSON response `{"risk_level": "...", "reasoning": "..."}`; retry LLM once on malformed JSON, then raise `ClassificationError`
- [x] Write `tests/test_classifier.py`:
  - Mocked LLM returning `read_only` → step has `RiskLevel.READ_ONLY`
  - Mocked LLM returning `destructive` → step has `RiskLevel.DESTRUCTIVE`
  - Malformed JSON on first call, valid on retry → succeeds
  - Malformed JSON on both calls → raises `ClassificationError`
  - `step.classification_reasoning` is populated from LLM response

**Dependencies:** Tasks 3, 5

---

## Task 7: Shell executor (`shell.py`)

**Goal:** Subprocess execution with timeout, output capture, and duration measurement.

- [x] Implement `run_command(command: str, timeout_seconds: int) -> ExecutionResult`
- [x] Capture stdout and stderr separately
- [x] Measure wall-clock duration with `time.monotonic()`
- [x] Handle `subprocess.TimeoutExpired`: terminate process, set `timed_out=True`, return result
- [x] Never raise — always return `ExecutionResult`
- [x] Write `tests/test_shell.py`:
  - Successful command: correct stdout, exit code 0, duration > 0
  - Failing command: non-zero exit code, stderr captured
  - Timeout: `timed_out=True`, process terminated, returns within timeout + 1s
  - stdout and stderr captured separately

**Dependencies:** Task 2

---

## Task 8: Audit log (`audit.py`)

**Goal:** Append-only NDJSON audit log with verified SHA-256 hash chain.

- [x] Implement `create_audit_log(config, runbook_path) -> AuditLog` — creates file with `{stem}-{timestamp}-{rand4}.json` naming, opens in `'x'` mode, raises `AuditError` on collision
- [x] Implement `AuditLog` as a context manager with `append(entry_data) -> AuditEntry` and `close()`
- [x] Hash chain: canonical JSON (sorted keys, no whitespace) → SHA-256; `prev_hash=None` for first entry
- [x] Implement `load_log(path: Path) -> list[AuditEntry]`
- [x] Implement `verify_chain(entries: list[AuditEntry]) -> list[int]` — returns seq numbers of broken links
- [x] Write `tests/test_audit.py`:
  - Appended entries are readable back as valid `AuditEntry` objects
  - `verify_chain` returns empty list for intact chain
  - `verify_chain` returns correct seq numbers when an entry is tampered
  - File opened in `'x'` mode — second `create_audit_log` with same path raises `AuditError`
  - Property-based test (Hypothesis): any sequence of N entries produces a chain where `verify_chain` returns `[]`
  - Property-based test: mutating any single entry's field breaks the chain at that entry's seq

**Dependencies:** Tasks 2, 3

---

## Task 9: Display module (`display.py`)

**Goal:** All terminal output via `rich`, no `print()` anywhere else.

- [x] Implement all functions from design doc: `show_step_running`, `show_step_success`, `show_step_failure`, `show_step_skipped`, `show_dry_run_step`, `show_classification`, `show_summary`, `show_warning`, `show_error`, `show_replay`
- [x] Color scheme: running=yellow, success=green, failure=red, skipped=dim, dry-run=cyan
- [x] Risk level badges: `read_only`=green, `modifying`=yellow, `destructive`=red bold
- [x] `show_replay`: renders entries in order; broken chain entries prefixed `[UNVERIFIED]` in red; bold-red `Panel` warning at first break; final line states chain status
- [x] Write `tests/test_display.py` (use `rich`'s `Console(file=StringIO())` to capture output):
  - `show_step_success` output contains step text
  - `show_step_failure` output contains "red" styling or failure indicator
  - `show_replay` with broken chain includes "INTEGRITY WARNING" text
  - `show_replay` with intact chain includes "Hash chain intact"
  - `show_summary` includes counts for successes, failures, skips

**Dependencies:** Task 2

---

## Task 10: Approval workflow (`approval.py`)

**Goal:** Slack Socket Mode approval and failure-direction prompts.

- [x] Implement `request_approval(step, config) -> ApprovalResult`
- [x] Implement `request_failure_direction(step, failure_reason, config, include_retry_warning) -> FailureDirection`
- [x] Block Kit message structure per design doc (approval: Approve/Deny; failure: Continue/Retry/Skip/Abort)
- [x] Failure direction message includes `"⚠️ WARNING: This step is <level>. Retry may have unintended side effects."` when `include_retry_warning=True`
- [x] Match `block_actions` events to posted message `ts` to ignore stale interactions
- [x] Update Slack message on resolution to show the decision made
- [x] Return `timed_out=True` when no response within `config.timeout_seconds`
- [x] Raise `ApprovalError` on Slack API failure
- [x] Write `tests/test_approval.py` (mock `slack_sdk`):
  - Approve button click → `ApprovalResult(approved=True, approver_slack_id=...)`
  - Deny button click → `ApprovalResult(approved=False, ...)`
  - Timeout → `ApprovalResult(timed_out=True)`
  - Slack API failure → raises `ApprovalError`
  - `include_retry_warning=True` → warning text present in posted message
  - `include_retry_warning=False` → warning text absent

**Dependencies:** Tasks 2, 9

---

## Task 11a: Executor core (`executor.py`)

**Goal:** Two-phase orchestration loop with direction handling and LLM decisions. No signal handling yet.

- [x] Implement `run_runbook(runbook_path, config) -> ExecutionSummary`
- [x] Phase 1: parse all steps, classify all steps, write PARSE + CLASSIFY audit entries
- [x] Phase 2: execution loop per design doc (dry-run check first, then command check, then approval gate, then shell, then LLM decision)
- [x] Implement `llm_decision(step, exec_result, remaining_steps, config) -> LLMDecision`
- [x] Direction handler: CONTINUE, RETRY (max 1 consecutive retry), SKIP, ABORT
- [x] Write `tests/test_executor.py` (mock classifier, shell, approval, llm, audit):
  - `read_only` step executes without approval call
  - `modifying` step triggers approval; approved → executes; denied → direction prompt
  - Dry-run: no shell calls, no Slack calls, all steps shown
  - Step failure triggers direction prompt with retry warning for `destructive` step
  - LLM decision `abort` halts loop and writes ABORT entry
  - LLM decision `skip` advances index and writes SKIP entries
  - `no_llm_context=True` skips post-step LLM call, continues to next step
  - Second consecutive retry failure returns to direction prompt (not infinite loop)

**Dependencies:** Tasks 3–10

---

## Task 11b: SIGINT handling

**Goal:** Clean shutdown on Ctrl+C — subprocess termination, audit flush, Slack update, exit 130.

- [x] Register `SIGINT` handler in `cli.py`; sets module-level `_interrupted: bool` flag
- [x] Executor checks `_interrupted` flag at the top of each loop iteration; if set, appends ABORT entry and breaks
- [x] `shell.py`: catch `KeyboardInterrupt` during `subprocess.run`, call `proc.terminate()`, wait 2s, then `proc.kill()` if still alive; return `ExecutionResult(exit_code=-1)`
- [x] `audit.py`: ensure `AuditLog` is used as a context manager so `__exit__` flushes and closes the file even on unhandled exceptions
- [x] `approval.py`: on `KeyboardInterrupt`, update the open Slack message to `"⚠️ Execution interrupted — no action taken"` and close the Socket Mode connection
- [x] `cli.py`: catch `SystemExit` from the SIGINT path and re-exit with code `130`
- [x] Add to `tests/test_executor.py`:
  - SIGINT mid-execution writes ABORT entry with `reasoning="interrupted by SIGINT"`
  - SIGINT during subprocess: shell returns `exit_code=-1`, audit entry written, process exits 130
- [x] Add to `tests/test_approval.py`:
  - `KeyboardInterrupt` during wait → Slack message updated to interrupted text, connection closed

**Dependencies:** Task 11a

---

## Task 12: CLI subcommands (`cli.py`)

**Goal:** All three subcommands fully wired and tested end-to-end.

- [x] Wire `run` subcommand: build `Config`, call `run_runbook`, handle `RunbookExecError` → `display.show_error` + exit 1
- [x] Wire `validate` subcommand: parse + classify all steps, display each with risk level and approval status, exit 0; no audit log written
- [x] Wire `replay` subcommand: `load_log` + `verify_chain` + `show_replay`
- [x] Display `--no-llm-context` warning via `display.show_warning` at startup when flag is set
- [x] Write `tests/test_cli.py` using Typer's `CliRunner`:
  - `run` with valid runbook invokes executor
  - `run` with missing file exits 1 with error message
  - `validate` displays risk levels, exits 0, writes no audit log
  - `replay` displays entries and chain status
  - `--help` on each subcommand shows usage
  - Invalid subcommand shows usage help

**Dependencies:** Task 11b

---

## Task 13: Packaging + examples

**Goal:** `pip install runbook-exec` works; example runbook included.

- [x] Finalize `pyproject.toml`: all runtime dependencies pinned, `[project.optional-dependencies] dev = [...]`, `include` patterns for `examples/`
- [x] Create `examples/disk-full.md` — a realistic disk-full incident runbook with a mix of `read_only`, `modifying`, and `destructive` steps
- [x] Verify `pip install .` in a fresh venv installs cleanly and `runbook-exec --help` works
- [x] Verify `runbook-exec validate examples/disk-full.md` classifies all steps without error

**Dependencies:** Task 12

---

## Task 14: CI/CD + documentation

**Goal:** GitHub Action validates runbooks on PR; README is complete.

- [x] Create `.github/workflows/validate.yml` — triggers on PR, runs `runbook-exec validate` on all `*.md` files in `examples/`, fails if exit code non-zero
- [x] Write `README.md`:
  - Quickstart (install, set env vars, run example)
  - Configuration reference (`.runbook-exec.toml` keys + env vars)
  - LLM data disclosure (exactly what is sent to Anthropic and when)
  - Safety bias principle
  - `--no-llm-context` flag documentation
  - Link to `examples/disk-full.md`
- [x] Run full test suite: `pytest --cov=runbook_exec --cov-report=term-missing`; confirm ≥ 80% overall, 100% on `models.py`, `parser.py`, `audit.py`

**Dependencies:** Task 13

---

## Coverage targets (enforced in Task 14)

| Module | Target |
|---|---|
| `models.py` | 100% |
| `parser.py` | 100% |
| `audit.py` | 100% |
| `classifier.py` | ≥ 90% |
| `shell.py` | ≥ 90% |
| `executor.py` | ≥ 85% |
| `approval.py` | ≥ 80% |
| `cli.py` | ≥ 80% |
| Overall | ≥ 80% |
