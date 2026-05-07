# Tech Stack

## Language & Runtime

- **Python 3.11+**
- Packaged as a pip-installable CLI tool (`pip install runbook-exec`)
- Entry point: `runbook-exec` command available in PATH after install

## Key Libraries & Frameworks

| Category | Library |
|---|---|
| CLI framework | `typer` (built on click, better type-hint ergonomics) |
| Markdown parsing | `markdown-it-py` (CommonMark-compliant, actively maintained) |
| LLM integration | `anthropic` (official Python SDK) |
| Slack integration | `slack-sdk` |
| Rich terminal output | `rich` |
| TOML config parsing | `tomllib` (Python 3.11+ stdlib, no extra dependency needed) |
| Property-based testing | `hypothesis` |
| Test framework | `pytest` |
| Test coverage | `pytest-cov` |

## LLM

- Provider: **Anthropic Claude**
- Default model: `claude-sonnet-4-5`
- API key: read from `ANTHROPIC_API_KEY` environment variable
- Retry policy: up to 3 retries with exponential backoff on failure

## Configuration

- Config file: `.runbook-exec.toml` in project root
- Supported keys: `timeout`, `slack_channel`, `auto_approve_level`, `llm_model`
- CLI flags override config file values; config file values override built-in defaults

## Packaging

- `pyproject.toml` for build metadata and dependencies
- Use exact/pinned dependency versions

## Common Commands

```bash
# Install in development mode
pip install -e ".[dev]"

# Run the CLI
runbook-exec run <runbook.md>
runbook-exec validate <runbook.md>
runbook-exec replay <audit-log.json>

# Run tests
pytest

# Run tests with coverage
pytest --cov=runbook_exec --cov-report=term-missing

# Run property-based tests only
pytest -m property

# Lint / format
ruff check .
ruff format .
```

## CI/CD

- GitHub Actions workflow runs `runbook-exec validate` on runbook files in PRs
- Workflow fails if validation fails
