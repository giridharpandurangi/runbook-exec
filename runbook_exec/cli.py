"""CLI entry point for runbook-exec.

Provides three subcommands:
  run      — execute a runbook
  validate — parse and classify without executing
  replay   — display a previous execution from an audit log
"""

import signal
from pathlib import Path
from typing import Optional

import typer

import runbook_exec.audit as audit_module
import runbook_exec.classifier as classifier
import runbook_exec.config as config_module
import runbook_exec.executor as executor
import runbook_exec.interrupt as interrupt
import runbook_exec.parser as parser
from runbook_exec import __version__, display
from runbook_exec.exceptions import RunbookExecError
from runbook_exec.models import needs_approval

app = typer.Typer(
    name="runbook-exec",
    help="AI-driven runbook automation with safety gates and audit trails.",
    no_args_is_help=True,
)


def _sigint_handler(signum: int, frame: object) -> None:
    """SIGINT handler: set the shared interrupt flag instead of raising KeyboardInterrupt."""
    interrupt.set_interrupted()


# Register the SIGINT handler at module load time.
signal.signal(signal.SIGINT, _sigint_handler)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"runbook-exec {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Optional[bool] = typer.Option(
        None,
        "--version",
        "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """AI-driven runbook automation with safety gates and audit trails."""


@app.command()
def run(
    runbook: Path = typer.Argument(..., help="Path to the Markdown runbook file."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Simulate execution without running commands."),
    incident_id: str | None = typer.Option(None, "--incident-id", help="Identifier used in the audit log filename."),
    auto_approve: str | None = typer.Option(
        None,
        "--auto-approve",
        help="Auto-approve steps at or below this risk level: read_only | modifying | destructive.",
    ),
    no_llm_context: bool = typer.Option(
        False,
        "--no-llm-context",
        help="Disable post-step LLM decision calls (prevents command output leaving the network boundary).",
    ),
) -> None:
    """Execute a runbook end-to-end with safety gates and audit logging."""
    try:
        if no_llm_context:
            display.show_warning(
                "Post-step LLM decision-making is disabled (--no-llm-context). "
                "Execution will continue to the next step after each command."
            )

        config = config_module.load_config(
            dry_run=dry_run,
            incident_id=incident_id,
            auto_approve_level=auto_approve,
            no_llm_context=no_llm_context,
        )
        executor.run_runbook(runbook, config)
    except RunbookExecError as exc:
        display.show_error(str(exc))
        raise typer.Exit(1)
    except KeyboardInterrupt:
        raise typer.Exit(130)


@app.command()
def validate(
    runbook: Path = typer.Argument(..., help="Path to the Markdown runbook file."),
) -> None:
    """Parse and classify a runbook without executing any commands."""
    try:
        config = config_module.load_config()
        steps = parser.parse_runbook(runbook)
        for step in steps:
            step = classifier.classify_step(step, config)
            display.show_classification(step)
            # Show whether approval would be required
            approval_needed = needs_approval(step, config)
            if approval_needed:
                display.show_warning(f"Step {step.index} would require approval ({step.risk_level.value})")
    except RunbookExecError as exc:
        display.show_error(str(exc))
        raise typer.Exit(1)


@app.command()
def replay(
    audit_log_path: Path = typer.Argument(..., help="Path to the audit log JSON file."),
) -> None:
    """Display a previous runbook execution from an audit log."""
    try:
        entries = audit_module.load_log(audit_log_path)
        chain_breaks = audit_module.verify_chain(entries)
        display.show_replay(entries, chain_breaks)
    except RunbookExecError as exc:
        display.show_error(str(exc))
        raise typer.Exit(1)


@app.command(name="mcp-server")
def mcp_server() -> None:
    """Start runbook-exec as an MCP server (for Claude Desktop / Cursor)."""
    from runbook_exec.mcp_server import main as _mcp_main
    _mcp_main()
