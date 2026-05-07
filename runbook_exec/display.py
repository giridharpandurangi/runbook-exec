"""Display module for runbook-exec.

All terminal output goes through the module-level `console` singleton.
No other module should call `print()` or `console` directly.
"""

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from runbook_exec.models import AuditEntry, ExecutionResult, ExecutionSummary, RiskLevel, Step

console = Console()

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_RISK_STYLE: dict[RiskLevel, str] = {
    RiskLevel.READ_ONLY: "green",
    RiskLevel.MODIFYING: "yellow",
    RiskLevel.DESTRUCTIVE: "bold red",
}

_RISK_LABEL: dict[RiskLevel, str] = {
    RiskLevel.READ_ONLY: "read_only",
    RiskLevel.MODIFYING: "modifying",
    RiskLevel.DESTRUCTIVE: "destructive",
}


def _risk_badge(risk_level: RiskLevel | None) -> str:
    """Return a styled risk-level badge string for use in rich markup."""
    if risk_level is None:
        return "[dim]unclassified[/dim]"
    style = _RISK_STYLE[risk_level]
    label = _RISK_LABEL[risk_level]
    return f"[{style}]{label}[/{style}]"


def _step_prefix(step: Step) -> str:
    """Return a short step identifier like 'Step 3'."""
    return f"Step {step.index}"


# ---------------------------------------------------------------------------
# Public display functions
# ---------------------------------------------------------------------------


def show_step_running(step: Step) -> None:
    """Print a 'running' indicator for a step about to be executed.

    Args:
        step: The step being executed.
    """
    console.print(f"[yellow]⟳ {_step_prefix(step)}:[/yellow] {step.text}")
    if step.command:
        console.print(f"  [yellow]$ {step.command}[/yellow]")


def show_step_success(step: Step, result: ExecutionResult) -> None:
    """Print a success indicator after a step completes successfully.

    Args:
        step: The step that completed.
        result: The execution result with stdout/stderr/exit_code.
    """
    console.print(
        f"[green]✓ {_step_prefix(step)}:[/green] {step.text} "
        f"[dim]({result.duration_seconds:.2f}s)[/dim]"
    )
    if result.stdout.strip():
        console.print(f"  [dim]{result.stdout.strip()}[/dim]")


def show_step_failure(step: Step, result: ExecutionResult) -> None:
    """Print a failure indicator after a step exits with a non-zero code or times out.

    Args:
        step: The step that failed.
        result: The execution result with stdout/stderr/exit_code.
    """
    status = "timed out" if result.timed_out else f"exit code {result.exit_code}"

    console.print(
        f"[red]✗ {_step_prefix(step)}:[/red] {step.text} "
        f"[red]({status})[/red]"
    )
    if result.stderr.strip():
        console.print(f"  [red]{result.stderr.strip()}[/red]")
    elif result.stdout.strip():
        console.print(f"  [dim]{result.stdout.strip()}[/dim]")


def show_step_skipped(step: Step, reason: str) -> None:
    """Print a skipped indicator for a step that was not executed.

    Args:
        step: The step that was skipped.
        reason: Human-readable reason for skipping.
    """
    console.print(f"[dim]⊘ {_step_prefix(step)}: {step.text} — skipped ({reason})[/dim]")


def show_dry_run_step(step: Step) -> None:
    """Print a dry-run preview of a step without executing it.

    Args:
        step: The step that would be executed.
    """
    console.print(f"[cyan]◎ {_step_prefix(step)} \\[dry-run]:[/cyan] {step.text}")
    if step.command:
        console.print(f"  [cyan]$ {step.command}[/cyan]")
    if step.risk_level is not None:
        console.print(f"  risk: {_risk_badge(step.risk_level)}")


def show_classification(step: Step) -> None:
    """Print the classification result for a step after LLM analysis.

    Args:
        step: The step with risk_level and classification_reasoning populated.
    """
    badge = _risk_badge(step.risk_level)
    console.print(f"  {_step_prefix(step)}: {badge}", end="")
    if step.classification_reasoning:
        console.print(f" — [dim]{step.classification_reasoning}[/dim]")
    else:
        console.print()


def show_summary(summary: ExecutionSummary) -> None:
    """Print a final execution summary table.

    Args:
        summary: Aggregated counts and metadata for the completed run.
    """
    table = Table(title="Execution Summary", show_header=True, header_style="bold")
    table.add_column("Metric", style="bold")
    table.add_column("Count")

    table.add_row("Total steps", str(summary.total_steps))
    table.add_row("[green]Successful[/green]", f"[green]{summary.successful}[/green]")
    table.add_row("[red]Failed[/red]", f"[red]{summary.failed}[/red]")
    table.add_row("[dim]Skipped[/dim]", f"[dim]{summary.skipped}[/dim]")

    if summary.aborted:
        table.add_row("[red bold]Status[/red bold]", "[red bold]ABORTED[/red bold]")
    else:
        table.add_row("[green]Status[/green]", "[green]COMPLETED[/green]")

    console.print(table)

    if summary.audit_log_path:
        console.print(f"[dim]Audit log: {summary.audit_log_path}[/dim]")


def show_warning(message: str) -> None:
    """Print a warning message in yellow.

    Args:
        message: The warning text to display.
    """
    console.print(f"[yellow]⚠ WARNING:[/yellow] {message}")


def show_error(message: str) -> None:
    """Print an error message in red.

    Args:
        message: The error text to display.
    """
    console.print(f"[red]✗ ERROR:[/red] {message}")


def show_approval_mode_banner(slack_enabled: bool) -> None:
    """Print a startup banner showing the approval mode.

    Args:
        slack_enabled: True when Slack tokens are configured; False for terminal mode.
    """
    if slack_enabled:
        console.print("[green]🔔 Slack approval enabled[/green]")
    else:
        console.print(
            "[yellow]⚠ Slack not configured — approvals will be requested in the terminal[/yellow]"
        )


def show_replay(entries: list[AuditEntry], chain_breaks: list[int]) -> None:
    """Render an audit log replay to the terminal.

    Entries are rendered in chronological order. If the hash chain is broken:
    - A bold-red Panel warning is printed before the first broken entry.
    - All entries from the first break onward are prefixed with [UNVERIFIED] in red.
    - The final line states the chain status.

    If the chain is intact, no warning is shown and the final line confirms integrity.

    Args:
        entries: Ordered list of audit entries to display.
        chain_breaks: Sequence numbers where the hash chain is broken (from verify_chain).
    """
    break_set = set(chain_breaks)
    first_break = min(chain_breaks) if chain_breaks else None
    warning_shown = False
    unverified = False

    for entry in entries:
        # Show integrity warning panel before the first broken entry
        if first_break is not None and entry.seq == first_break and not warning_shown:
            panel = Panel(
                f"Hash chain broken at entry #{entry.seq}.\n"
                "Audit log may have been tampered with or truncated.\n"
                "Entries from this point forward cannot be trusted.",
                title="⚠️  INTEGRITY WARNING",
                style="bold red",
                border_style="bold red",
            )
            console.print(panel)
            warning_shown = True
            unverified = True

        # Build the entry line
        timestamp = entry.timestamp
        action = entry.action.value.upper()
        step_info = f" step={entry.step_index}" if entry.step_index is not None else ""
        risk_info = f" [{entry.risk_level.value}]" if entry.risk_level else ""
        exit_info = f" exit={entry.exit_code}" if entry.exit_code is not None else ""
        reasoning_info = f" — {entry.reasoning}" if entry.reasoning else ""

        line = (
            f"#{entry.seq:>4}  {timestamp}  {action:<10}{step_info}{risk_info}"
            f"{exit_info}{reasoning_info}"
        )

        if unverified or entry.seq in break_set:
            console.print(f"[red][UNVERIFIED][/red] {line}")
        else:
            console.print(line)

    # Final chain status line
    if chain_breaks:
        seq_list = ", ".join(f"#{s}" for s in sorted(chain_breaks))
        console.print(f"[red]❌ Hash chain broken at entries: {seq_list}[/red]")
    else:
        console.print("[green]✅ Hash chain intact[/green]")
