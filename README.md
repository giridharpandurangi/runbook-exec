# runbook-exec

> **Status**: v0.1.0 — initial release. Core functionality validated end-to-end. Slack approval workflow tested with mocks; production Slack testing pending.

AI-driven CLI tool that transforms passive Markdown runbooks into executable automation with safety gates and audit trails.

At incident time (e.g., 3 AM), instead of a human manually following a runbook step-by-step, `runbook-exec` reads the Markdown runbook, classifies each step by risk level using Claude, executes safe steps autonomously, requests human approval for risky operations via Slack, and produces a tamper-evident audit log.

## Quickstart

### 1. Install

Until v0.1.0 ships to PyPI, install from source:

```bash
git clone https://github.com/<your-username>/runbook-exec.git
cd runbook-exec
pip install -e .
```

Once published, you'll be able to:

```bash
pip install runbook-exec
```

### Free Anthropic credits

New Anthropic accounts get $5 in free credits at [console.anthropic.com](https://console.anthropic.com) — enough for hundreds of `runbook-exec validate` runs.

### 2. Set environment variables

```bash
export ANTHROPIC_API_KEY=sk-ant-...          # Required
export SLACK_BOT_TOKEN=xoxb-...              # Required for approval workflow
export SLACK_APP_TOKEN=xapp-...              # Required for Socket Mode
```

### 3. Run an example runbook

```bash
# Dry-run first to see what would happen
runbook-exec run examples/disk-full.md --dry-run

# Validate risk levels without executing
runbook-exec validate examples/disk-full.md

# Execute for real
runbook-exec run examples/disk-full.md
```

## Platform support

Tested on Linux, macOS, and Windows. The shell executor uses `subprocess` and runs whatever shell command you provide — runbook commands need to be valid for the host OS. Most realistic runbooks target Linux. On Windows, use `cmd.exe`-compatible commands or wrap PowerShell via `powershell -Command "..."`.

## Runbook formatting

`runbook-exec` parses Markdown using the CommonMark spec. Two rules matter in practice:

**Numbered lists only.** Only numbered (`1.`, `2.`, ...) list items become steps. Bullet points (`-`, `*`) are ignored.

**Code blocks must be indented under their list item.** A fenced code block is only associated with a step if it is indented at least as far as the list item's content. This is standard CommonMark behaviour — a code block at the left margin is not part of the list.

✅ Correct — code block indented under the list item:
```markdown
1. Check disk usage

   ```bash
   df -h
   ```
```

❌ Wrong — code block at the left margin (will not be extracted as the step's command):
```markdown
1. Check disk usage

```bash
df -h
```
```

When in doubt, run `runbook-exec validate <runbook.md>` — steps with `command=None` in the output mean the code block was not picked up.

## Subcommands

### `run`

Execute a runbook end-to-end with safety gates and audit logging.

```bash
runbook-exec run <runbook.md> [OPTIONS]

Options:
  --dry-run              Simulate execution without running commands
  --incident-id TEXT     Identifier used in the audit log filename
  --auto-approve TEXT    Auto-approve steps at or below this risk level:
                         read_only | modifying | destructive
  --no-llm-context       Disable post-step LLM decision calls
```

### `validate`

Parse and classify a runbook without executing any commands. Useful for CI/CD.

```bash
runbook-exec validate <runbook.md>
```

### `replay`

Display a previous runbook execution from an audit log.

```bash
runbook-exec replay <audit-log.json>
```

## Configuration

Create a `.runbook-exec.toml` file in your project root to set defaults:

```toml
llm_model = "claude-sonnet-4-5"
slack_channel = "#incidents"
timeout_seconds = 300
auto_approve_level = "read_only"   # "read_only" | "modifying" | "destructive"
audit_log_dir = "./runbook-exec-logs"
```

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Anthropic API key for Claude |
| `SLACK_BOT_TOKEN` | For approvals | Slack bot token (`xoxb-...`) |
| `SLACK_APP_TOKEN` | For approvals | Slack app token for Socket Mode (`xapp-...`) |

### Configuration precedence

CLI flags > `.runbook-exec.toml` > built-in defaults

### Slack fallback

If Slack is not configured (missing `SLACK_BOT_TOKEN` or `SLACK_APP_TOKEN`), `runbook-exec` falls back to interactive terminal prompts for approvals and failure direction. A banner is shown at startup indicating which mode is active.

Slack is recommended for production use because terminal prompts require an active SSH or console session. Terminal mode is useful for local testing and development.

## Risk levels

Each step is classified as one of three risk levels:

| Level | Description | Examples |
|---|---|---|
| `read_only` | Observes system state only | `df`, `ls`, `cat`, `kubectl get`, `ps` |
| `modifying` | Changes state but recoverable | `systemctl restart`, `logrotate`, file edits |
| `destructive` | Deletes data or requires sudo | `rm -rf`, `DROP TABLE`, `kubectl delete` |

By default, `read_only` steps run automatically. `modifying` and `destructive` steps require Slack approval.

## Safety bias principle

When the classifier is uncertain between two risk levels, it always chooses the more cautious (higher risk) level. This is enforced in the LLM prompt, not in post-processing, so the reasoning reflects the actual decision.

## LLM data disclosure

`runbook-exec` sends data to the Anthropic API in two scenarios:

1. **Step classification** (always): The step text and extracted command are sent to Claude to determine the risk level. This happens for every step before any execution begins.

2. **Post-step decisions** (unless `--no-llm-context` is set): After each step executes successfully, the step text, command, exit code, and stdout/stderr output are sent to Claude to decide whether to continue, skip subsequent steps, or abort.

**What is NOT sent**: Slack tokens, audit log contents, or any data from steps that have not yet executed.

## `--no-llm-context` flag

Use `--no-llm-context` to prevent command output from being sent to the Anthropic API:

```bash
runbook-exec run runbook.md --no-llm-context
```

When this flag is set:
- Step classification still uses the LLM (only step text and command are sent)
- Post-step decision calls are disabled
- Execution continues to the next step after each command completes
- A warning is displayed at startup

Use this flag when running runbooks that produce sensitive output (credentials, PII, internal hostnames) that should not leave your network boundary.

## Audit logs

Every execution produces a tamper-evident audit log in `./runbook-exec-logs/` (configurable via `audit_log_dir`).

Audit logs use NDJSON format with a SHA-256 hash chain. Each entry includes a hash of the previous entry, making tampering detectable.

To replay and verify an audit log:

```bash
runbook-exec replay runbook-exec-logs/disk-full-20240115T030000Z-a3f1.json
```

Audit log files are never committed to source control (`.gitignore` includes `runbook-exec-logs/`).

## CI/CD integration

Add runbook validation to your PR checks using the included GitHub Action:

```yaml
# .github/workflows/validate.yml
name: Validate Runbooks
on:
  pull_request:
    paths:
      - "examples/**/*.md"

jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install runbook-exec
      - run: runbook-exec validate examples/disk-full.md
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

The workflow fails if `runbook-exec validate` exits with a non-zero code.

## Example runbook

See [`examples/disk-full.md`](examples/disk-full.md) for a realistic disk-full incident runbook with a mix of `read_only`, `modifying`, and `destructive` steps.

## Development

```bash
# Install in development mode
pip install -e ".[dev]"

# Run tests
pytest

# Run tests with coverage
pytest --cov=runbook_exec --cov-report=term-missing

# Lint
ruff check .
```
