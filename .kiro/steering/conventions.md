---
inclusion: always
---
# Build conventions

## Code style
- Type hints on all public functions
- f-strings, not .format() or %
- pathlib.Path, not os.path
- No print() in library code — use logging or rich.console
- Pydantic v2 with Literal types for string enums (avoid bare str)

## Testing rules
- Every new module gets a matching tests/test_<module>.py
- Each component gets at least one positive and one negative test
- Mock all external calls (LLM, Slack, subprocess) in unit tests
- Coverage target ≥ 80% per module, aim for 100% on core modules (models, parser, audit)
- Use fixtures in conftest.py; don't repeat scaffolding across test files
- Property-based tests use Hypothesis, minimum 100 iterations per property

## Build discipline
- One task at a time, per the tasks.md plan
- No speculative code for future tasks (keeps coverage honest)
- After each task: run `pytest -v` and show output before declaring done
- Don't widen scope mid-task — propose the change, wait for approval
- When uncertain, ask before assuming

## Safety bias
- Default to the more cautious behavior in any ambiguous decision
- Never auto-approve modifying or destructive operations without explicit config
- All risky decisions go to the audit log
- Surface side effects in user-facing output
- Validate inputs at module boundaries; fail loudly with clear errors

## Error handling
- Catch only what you can handle; let the rest propagate
- Use specific exception types, not bare `except Exception`
- All errors that halt execution must produce a clear, actionable message
- Never silently swallow exceptions — log at minimum

## What NOT to do
- Don't add features not in the current task's acceptance criteria
- Don't refactor unrelated code while implementing a task
- Don't introduce new dependencies without flagging it
- Don't write defensive code for situations the design doc says can't happen