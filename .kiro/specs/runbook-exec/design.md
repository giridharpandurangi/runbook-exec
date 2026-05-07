# Design Document: runbook-exec

## Overview

`runbook-exec` is a Python CLI tool that parses Markdown runbooks, classifies each step by risk level using Claude, executes steps with appropriate safety gates, and produces tamper-evident audit logs. The architecture is a pipeline of loosely coupled modules wired together by `executor.py`, with all external I/O (LLM, Slack, subprocess) isolated behind thin adapter modules.

---

## Data Models (`models.py`)

All shared types live here. No other module defines domain types.

```python
from enum import Enum
from typing import Literal, Optional
from pydantic import BaseModel

class RiskLevel(str, Enum):
    READ_ONLY   = "read_only"
    MODIFYING   = "modifying"
    DESTRUCTIVE = "destructive"

class Step(BaseModel):
    index: int                        # 1-based position in the runbook
    section: Optional[str]            # Nearest preceding heading, or None
    text: str                         # Full step text (prose + command)
    command: Optional[str]            # Extracted shell command, or None
    risk_level: Optional[RiskLevel]   # Set after classification
    classification_reasoning: Optional[str]  # LLM explanation

class ExecutionResult(BaseModel):
    stdout: str
    stderr: str
    exit_code: int
    duration_seconds: float
    timed_out: bool = False

class ActionType(str, Enum):
    PARSE     = "parse"
    CLASSIFY  = "classify"
    EXECUTE   = "execute"
    APPROVE   = "approve"
    DENY      = "deny"
    SKIP      = "skip"
    ABORT     = "abort"
    FAILURE   = "failure"
    SUMMARY   = "summary"

class AuditEntry(BaseModel):
    seq: int                          # Monotonically increasing sequence number
    action: ActionType
    timestamp: str                    # ISO 8601 UTC
    step_index: Optional[int]
    step_text: Optional[str]
    command: Optional[str]
    risk_level: Optional[RiskLevel]
    output: Optional[str]             # Combined stdout+stderr for readability
    stdout: Optional[str]
    stderr: Optional[str]
    exit_code: Optional[int]
    duration_seconds: Optional[float]
    approver_slack_id: Optional[str]
    reasoning: Optional[str]
    mode: Literal["live", "dry_run"] = "live"
    prev_hash: Optional[str]          # SHA-256 of previous entry's canonical JSON
    hash: str                         # SHA-256 of this entry's canonical JSON (excl. hash field)

class Config(BaseModel):
    llm_model: str = "claude-sonnet-4-5"
    slack_channel: str = ""
    slack_bot_token: str = ""         # from SLACK_BOT_TOKEN env var
    slack_app_token: str = ""         # from SLACK_APP_TOKEN env var (Socket Mode)
    timeout_seconds: int = 300        # 5 minutes default
    auto_approve_level: Optional[RiskLevel] = None
    audit_log_dir: str = "./runbook-exec-logs"
    no_llm_context: bool = False
    dry_run: bool = False
    incident_id: Optional[str] = None
```

**Design decisions:**
- `RiskLevel` is a `str` enum so it serializes cleanly to JSON without extra conversion.
- `Step.command` is `Optional` — some steps are prose-only with no extractable command; the executor skips shell execution for those.
- `AuditEntry.hash` covers all fields except `hash` itself, computed over the canonical JSON (keys sorted, no whitespace). This makes the chain verifiable without the file.
- `Config` is the single source of truth for runtime settings; it is constructed once in `cli.py` and passed through.

---

## Module Designs

### `config.py`

Responsible for building a `Config` from three sources in priority order:
1. Built-in defaults (Pydantic field defaults)
2. `.runbook-exec.toml` (if present in CWD)
3. CLI flags (passed in as kwargs)

```python
def load_config(**cli_overrides) -> Config:
    """Load config from file + env vars, then apply CLI overrides."""
```

- Reads `ANTHROPIC_API_KEY`, `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` from environment.
- Raises `ConfigError` (a custom exception) with a clear message if `ANTHROPIC_API_KEY` is missing.
- CLI overrides use `None` as sentinel for "not provided" — only non-None values override.

---

### `parser.py`

Pure function: `Path → list[Step]`. No I/O beyond reading the file. No randomness.

```python
def parse_runbook(path: Path) -> list[Step]:
    """Parse a Markdown file and return ordered Steps."""
```

**Parsing rules:**
1. Walk the `markdown-it-py` token stream.
2. Track the current section heading (last `heading_open` token text).
3. For each `ordered_list_item`, create a `Step`.
4. Command extraction priority:
   - Fenced code block inside the list item → `step.command`
   - First inline code span (backtick) → `step.command`
   - No code → `step.command = None`
5. Unnumbered list items are ignored entirely.
6. On parse failure, raise `ParseError` with the file path and line number.

**Why `markdown-it-py`:** Token-based API makes it straightforward to distinguish ordered vs. unordered lists and to extract nested code blocks without regex heuristics.

---

### `llm.py`

Thin adapter over the Anthropic SDK. All LLM calls go through here.

```python
def call_llm(
    prompt: str,
    system: str,
    model: str,
    max_tokens: int = 1024,
) -> str:
    """Call Claude and return the text response. Retries 3x with exponential backoff."""
```

- Reads `ANTHROPIC_API_KEY` from environment (validated at startup by `config.py`).
- Retry logic: attempts 1, 2, 3 with delays 1s, 2s, 4s (exponential backoff).
- On all retries exhausted, raises `LLMError` with the last exception message.
- No business logic here — just the HTTP call and retry loop.

---

### `classifier.py`

Uses `llm.py` to assign a `RiskLevel` to each `Step`.

```python
def classify_step(step: Step, config: Config) -> Step:
    """Return a new Step with risk_level and classification_reasoning set."""
```

**Prompt design:**
- System prompt establishes the three risk levels with examples and the safety-bias rule.
- User prompt contains the step text and command.
- Response format: structured JSON `{"risk_level": "...", "reasoning": "..."}`.
- Parse the JSON response; if malformed, retry the LLM call once, then raise `ClassificationError`.
- If uncertain between two levels, the prompt instructs Claude to pick the more cautious one.

**Safety bias is enforced in the prompt**, not in post-processing, so the LLM's reasoning reflects the actual decision.

---

### `shell.py`

Runs a shell command via subprocess with timeout and output capture.

```python
def run_command(command: str, timeout_seconds: int) -> ExecutionResult:
    """Execute a shell command and return captured output and metadata."""
```

- Uses `subprocess.run` with `shell=True`, `capture_output=True`, `text=True`.
- Wraps in a `try/except subprocess.TimeoutExpired` to set `timed_out=True` and terminate.
- Records wall-clock duration using `time.monotonic()`.
- Never raises — always returns an `ExecutionResult`. Callers check `exit_code` and `timed_out`.

**Why `shell=True`:** Runbook commands are written for a shell (pipes, redirects, env vars). Splitting them into argv would break many real-world commands.

---

### `approval.py`

Manages the Slack approval workflow using Slack's Socket Mode (real-time event delivery without a public HTTP endpoint).

```python
def request_approval(step: Step, config: Config) -> ApprovalResult:
    """Post an approval request to Slack and wait for a button click."""

def request_failure_direction(
    step: Step,
    failure_reason: str,
    config: Config,
    include_retry_warning: bool = False,
) -> FailureDirection:
    """Post a failure prompt to Slack and wait for a direction choice.
    
    When include_retry_warning=True, the Slack message includes:
    "⚠️ WARNING: This step is <risk_level>. Retry may have unintended side effects."
    This is set for any modifying or destructive step that fails.
    """
```

```python
class ApprovalResult(BaseModel):
    approved: bool
    approver_slack_id: Optional[str]
    timed_out: bool = False

class FailureDirection(str, Enum):
    CONTINUE = "continue"
    RETRY    = "retry"
    ABORT    = "abort"
    SKIP     = "skip"
```

**Slack interaction design:**

1. Post a Block Kit message with:
   - Section block: step text + command (code block)
   - Risk level badge
   - Actions block: `[✅ Approve]` `[❌ Deny]` buttons (for approval) or `[Continue] [Retry] [Abort] [Skip]` (for failure)
2. Open a Socket Mode connection and listen for `block_actions` events.
3. Match the `action_id` to the posted message's `ts` (timestamp) to avoid acting on stale messages.
4. On button click: acknowledge the interaction, update the message to show the decision, close the socket, return result.
5. On timeout: close the socket, return `timed_out=True`.

**Why Socket Mode:** No need for a public webhook URL. Works from a laptop or CI runner without infrastructure setup.

**Slack tokens required:**
- `SLACK_BOT_TOKEN` — for posting messages (`chat.postMessage`)
- `SLACK_APP_TOKEN` — for Socket Mode (`connections.open`)

---

### `audit.py`

Append-only JSON audit log with SHA-256 hash chain.

```python
def create_audit_log(config: Config, runbook_path: Path) -> AuditLog:
    """Create a new audit log file and return a writer handle."""

class AuditLog:
    def append(self, entry_data: dict) -> AuditEntry:
        """Build, hash, and append an AuditEntry. Returns the written entry."""

    def close(self) -> None:
        """Flush and close the file."""
```

**Hash chain mechanics:**
1. Canonical JSON = `json.dumps(entry_dict_without_hash, sort_keys=True, separators=(',', ':'))`.
2. `entry.hash = sha256(canonical_json).hexdigest()`.
3. `entry.prev_hash` = hash of the previous entry (or `None` for the first entry).
4. Each entry is written as a single JSON line (NDJSON format) — easy to stream and append.

**File creation:**
- Directory is created if it doesn't exist (`Path.mkdir(parents=True, exist_ok=True)`).
- File name: `{incident_id}-{timestamp}-{rand4}.json` or `{runbook_stem}-{timestamp}-{rand4}.json`.
- Timestamp format: `YYYYMMDDTHHMMSSZ` (compact ISO 8601 UTC).
- `{rand4}` is a 4-character random hex suffix (e.g. `a3f1`) generated at file creation time. Combined with second-level timestamp precision, collisions are astronomically unlikely.
- File is opened in `'x'` mode (exclusive create) — raises `AuditError` loudly on the rare actual collision. No counter-suffix fallback; the random suffix is the collision-avoidance strategy.

**Replay verification** (`audit.py` also provides):
```python
def verify_chain(entries: list[AuditEntry]) -> list[int]:
    """Return list of seq numbers where the hash chain is broken."""
```

---

### `executor.py`

The orchestration core. Wires all other modules together into the execution loop.

```python
def run_runbook(runbook_path: Path, config: Config) -> ExecutionSummary:
    """Parse, classify, and execute a runbook. Returns a summary."""
```

**Execution is split into two explicit phases:**

**Phase 1 — Parse + Classify (all steps before any execution):**
```
steps = parser.parse_runbook(runbook_path)
audit.append(PARSE, runbook_path, step_count=len(steps))

for step in steps:
    step = classifier.classify_step(step, config)
    audit.append(CLASSIFY, step)
    display.show_classification(step)
```

Classification of all steps completes before the execution phase begins. This ensures `step.risk_level` is always set when `needs_approval` is called, and gives the user a full risk overview before any command runs.

**Phase 2 — Execute (per step):**
```
i = 0
while i < len(steps):
    step = steps[i]

    # Dry-run: never touches Slack or subprocess
    if config.dry_run:
        display.show_dry_run_step(step)
        i += 1
        continue

    # Steps with no extractable command are skipped silently
    if step.command is None:
        display.show_skipped(step, reason="no command")
        audit.append(SKIP, step, reasoning="no command")
        i += 1
        continue

    # Approval gate (only reached in live mode, only for steps with commands)
    if needs_approval(step, config):
        result = approval.request_approval(step, config)
        if result.timed_out:
            audit.append(FAILURE, step, reasoning="approval timed out")
            → treat as failure, request direction
        elif result.approved:
            audit.append(APPROVE, step, approver=result.approver_slack_id)
        else:
            audit.append(DENY, step, approver=result.approver_slack_id)
            direction = approval.request_failure_direction(
                step, "Step was denied", config, include_retry_warning=False
            )
            → handle direction (see below)
            continue  # direction handler sets i

    exec_result = shell.run_command(step.command, config.timeout_seconds)
    audit.append(EXECUTE, step, exec_result)
    display.show_step_result(step, exec_result)

    if exec_result.exit_code != 0 or exec_result.timed_out:
        reason = "timed out" if exec_result.timed_out else f"exit code {exec_result.exit_code}"
        direction = approval.request_failure_direction(
            step, reason, config,
            include_retry_warning=(step.risk_level != RiskLevel.READ_ONLY)
        )
        → handle direction (see below)
        continue

    if not config.no_llm_context:
        decision = llm_decision(step, exec_result, remaining_steps=steps[i+1:], config)
        if decision.action == "abort":
            audit.append(ABORT, step, reasoning=decision.reasoning)
            break
        if decision.action == "skip" and decision.skip_count:
            for skipped in steps[i+1 : i+1+decision.skip_count]:
                audit.append(SKIP, skipped, reasoning=decision.reasoning)
            i += decision.skip_count

    i += 1
```

**Direction handler (shared for deny, failure, and approval timeout):**
```
CONTINUE → audit.append(FAILURE, step, reasoning="operator continued"); i += 1
RETRY    → i unchanged (re-runs same step; second consecutive failure goes back to direction prompt)
SKIP     → audit.append(SKIP, step, reasoning="operator skipped"); i += 1
ABORT    → audit.append(ABORT, reasoning="operator aborted"); break
```

**`needs_approval(step, config) → bool`:**

Risk levels have a strict ordering: `READ_ONLY < MODIFYING < DESTRUCTIVE`.

A step requires approval iff its risk level is **strictly higher** than `auto_approve_level`:

| `auto_approve_level` | `read_only` step | `modifying` step | `destructive` step |
|---|---|---|---|
| `None` (default) | ✅ auto-run | 🔐 approval needed | 🔐 approval needed |
| `read_only` | ✅ auto-run | 🔐 approval needed | 🔐 approval needed |
| `modifying` | ✅ auto-run | ✅ auto-run | 🔐 approval needed |
| `destructive` | ✅ auto-run | ✅ auto-run | ✅ auto-run |

```python
RISK_ORDER = {RiskLevel.READ_ONLY: 0, RiskLevel.MODIFYING: 1, RiskLevel.DESTRUCTIVE: 2}

def needs_approval(step: Step, config: Config) -> bool:
    if config.auto_approve_level is None:
        return step.risk_level != RiskLevel.READ_ONLY
    return RISK_ORDER[step.risk_level] > RISK_ORDER[config.auto_approve_level]
```

**`auto_approve_level` semantics:** Acts as a ceiling — auto-approves all levels at or below the configured level. `None` is equivalent to auto-approving only `read_only` (the safe default).

**Failure direction handling:**
- `CONTINUE` → proceed to next step, log the decision
- `RETRY` → re-run the same step (max 1 retry to avoid infinite loops; second failure goes back to direction prompt)
- `SKIP` → log skip, advance to next step
- `ABORT` → log abort, break loop, return summary

---

### `display.py`

All terminal output via `rich.console.Console`. No other module calls `print()` or `console`.

```python
console = Console()  # module-level singleton

def show_step_running(step: Step) -> None: ...
def show_step_success(step: Step, result: ExecutionResult) -> None: ...
def show_step_failure(step: Step, result: ExecutionResult) -> None: ...
def show_step_skipped(step: Step, reason: str) -> None: ...
def show_dry_run_step(step: Step) -> None: ...
def show_classification(step: Step) -> None: ...
def show_summary(summary: ExecutionSummary) -> None: ...
def show_warning(message: str) -> None: ...
def show_error(message: str) -> None: ...
def show_replay(entries: list[AuditEntry], chain_breaks: list[int]) -> None: ...
```

**Replay UX when hash chain is broken:**

`show_replay` renders each entry in chronological order. If `chain_breaks` is non-empty:
1. Before rendering the first entry whose `seq` is in `chain_breaks`, print a prominent warning panel:
   ```
   ┌─ ⚠️  INTEGRITY WARNING ──────────────────────────────────────┐
   │ Hash chain broken at entry #<seq>.                           │
   │ Audit log may have been tampered with or truncated.          │
   │ Entries from this point forward cannot be trusted.           │
   └──────────────────────────────────────────────────────────────┘
   ```
   Rendered in `bold red` using `rich.panel.Panel`.
2. All entries after the first break are rendered with a `[UNVERIFIED]` prefix in red.
3. If the chain is intact, no warning is shown.
4. The final line of replay output always states either `✅ Hash chain intact` (green) or `❌ Hash chain broken at entries: {seq_list}` (red).

Color scheme (matches requirements):
- Running → `yellow`
- Success → `green`
- Failure → `red`
- Skipped → `dim` (gray)
- Dry-run → `cyan`
- Risk level badges: `read_only` → green, `modifying` → yellow, `destructive` → red bold

---

### `cli.py`

Typer app with three subcommands. Constructs `Config` and delegates to `executor.py`.

```python
app = typer.Typer()

@app.command()
def run(
    runbook: Path,
    dry_run: bool = False,
    incident_id: Optional[str] = None,
    auto_approve: Optional[str] = None,   # "read_only" | "modifying" | "destructive"
    no_llm_context: bool = False,
) -> None: ...

@app.command()
def validate(runbook: Path) -> None: ...

@app.command()
def replay(audit_log: Path) -> None: ...
```

- `validate` calls `parser.parse_runbook` + `classifier.classify_step` for each step, then `display.show_classification`. No audit log written. Exits 0 on success, 1 on error.
- `replay` calls `audit.load_log(path)`, `audit.verify_chain(entries)`, then `display.show_replay(entries)`.
- All `RunbookExecError` subclasses are caught at the top level and displayed via `display.show_error`, then `raise typer.Exit(1)`.

---

## Error Hierarchy

All custom exceptions inherit from `RunbookExecError`:

```
RunbookExecError
├── ConfigError          # Missing env vars, invalid config values
├── ParseError           # Malformed Markdown, unreadable file
├── ClassificationError  # LLM returned unparseable classification
├── LLMError             # All retries exhausted
├── ShellError           # (not raised — shell.py always returns ExecutionResult)
├── ApprovalError        # Slack API failure
└── AuditError           # File I/O failure on audit log
```

---

## LLM Prompt Contracts

### Classification prompt

**System:**
> You are a risk classifier for shell commands in operational runbooks. Classify each step as exactly one of: read_only, modifying, or destructive.
> - read_only: commands that only observe system state (df, ls, cat, kubectl get, ps, curl GET, etc.)
> - modifying: commands that change state but are recoverable (systemctl restart, logrotate, file edits, curl POST/PUT, etc.)
> - destructive: commands that delete data, drop schemas, or require sudo for non-safelisted operations (rm -rf, DROP TABLE, kubectl delete, sudo <unknown>)
> When uncertain between two levels, always choose the more cautious one.
> Respond with JSON only: {"risk_level": "<level>", "reasoning": "<one sentence>"}

**User:** `Step {index}: {step.text}\nCommand: {step.command or "(no command)"}`

### Post-step decision prompt

**System:**
> You are an execution controller for an automated runbook. After each step, decide whether to continue, skip steps, or abort.
> Respond with JSON only: {"action": "continue"|"skip"|"abort", "skip_count": <int or null>, "reasoning": "<one sentence>"}

**User:**
```
Runbook context: {runbook_path}
Completed step {index}: {step.text}
Command: {step.command}
Exit code: {exit_code}
Output:
{stdout}
{stderr}
Remaining steps: {remaining_step_texts}
```

---

## Slack Block Kit Message Structure

### Approval request

```json
[
  {"type": "header", "text": {"type": "plain_text", "text": "⚠️ Approval Required"}},
  {"type": "section", "fields": [
    {"type": "mrkdwn", "text": "*Step:*\n{step.text}"},
    {"type": "mrkdwn", "text": "*Risk Level:*\n{step.risk_level}"}
  ]},
  {"type": "section", "text": {"type": "mrkdwn", "text": "*Command:*\n```{step.command}```"}},
  {"type": "actions", "elements": [
    {"type": "button", "text": {"type": "plain_text", "text": "✅ Approve"},
     "style": "primary", "action_id": "approve_{ts}"},
    {"type": "button", "text": {"type": "plain_text", "text": "❌ Deny"},
     "style": "danger", "action_id": "deny_{ts}"}
  ]}
]
```

### Failure direction request

Same structure but actions block has four buttons: Continue / Retry / Skip / Abort.

---

## Configuration Reference

`.runbook-exec.toml` schema:

```toml
llm_model = "claude-sonnet-4-5"
slack_channel = "#incidents"
timeout_seconds = 300
auto_approve_level = "read_only"   # "read_only" | "modifying" | "destructive"
audit_log_dir = "./runbook-exec-logs"
```

Environment variables (never in config file):
- `ANTHROPIC_API_KEY` — required
- `SLACK_BOT_TOKEN` — required for approval workflow
- `SLACK_APP_TOKEN` — required for Socket Mode

---

## Audit Log Format

NDJSON file — one JSON object per line.

```json
{"seq":1,"action":"parse","timestamp":"2024-01-15T03:00:00Z","step_index":null,...,"prev_hash":null,"hash":"abc123..."}
{"seq":2,"action":"classify","timestamp":"2024-01-15T03:00:01Z","step_index":1,...,"prev_hash":"abc123...","hash":"def456..."}
{"seq":3,"action":"approve","timestamp":"2024-01-15T03:00:45Z","step_index":1,"approver_slack_id":"U012AB3CD",...,"prev_hash":"def456...","hash":"ghi789..."}
```

---

## SIGINT Handling (Ctrl+C)

A `signal.signal(signal.SIGINT, ...)` handler is registered at startup in `cli.py`. It sets a module-level `_interrupted: bool` flag. The executor checks this flag at the top of each loop iteration.

**Shutdown sequence on SIGINT:**

1. **If a subprocess is running:** `shell.py` catches `KeyboardInterrupt` in its `subprocess.run` call, terminates the child process (`proc.terminate()`, then `proc.kill()` after 2s if still alive), and returns an `ExecutionResult` with `exit_code=-1` and `timed_out=False`.

2. **Audit log:** `executor.py` catches the interrupt after the current step resolves and appends an `ABORT` entry with `reasoning="interrupted by SIGINT"`. The `AuditLog` context manager flushes and closes the file in its `__exit__`.

3. **Slack:** If an approval message is currently open (waiting for a button click), `approval.py` updates the message text to `"⚠️ Execution interrupted — no action taken"` and closes the Socket Mode connection.

4. **Exit code:** The process exits with code `130` (the Unix convention for SIGINT termination: `128 + 2`).

`AuditLog` is used as a context manager (`with audit.create_audit_log(...) as log:`) so the file is always flushed even if an unhandled exception escapes the executor.

---

## Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Slack interactivity | Socket Mode | No public webhook needed; works from any network |
| Audit format | NDJSON + hash chain | Streamable, appendable, tamper-evident without a database |
| LLM response format | Structured JSON | Deterministic parsing; avoids brittle text extraction |
| Shell execution | `shell=True` | Runbook commands use shell features (pipes, redirects) |
| Config precedence | defaults → file → CLI | Standard Unix tool convention |
| `auto_approve_level` | Ceiling semantics | Approving `modifying` implicitly approves `read_only` |
| Retry on LLM failure | 3x exponential backoff | Transient API errors are common; 3 attempts balances speed vs. reliability |
| Audit file collision | Timestamp + 4-char random hex suffix, `'x'` mode fails loudly | Suffix makes collisions astronomically unlikely; `'x'` mode is a hard safety net |
| Post-step LLM decision | Optional via `--no-llm-context` | Prevents command output from leaving the network boundary |
