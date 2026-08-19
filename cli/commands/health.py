"""Health check CLI commands.

``check-health`` probes live components. ``service-health`` is the
retrospective view: it aggregates the ``service_runs`` telemetry that
:class:`~src.services.base_service.BaseService` writes, so an operator (or a
cron) can see an error-rate spike without a browser or a dashboard session.

What that telemetry can and cannot show, stated because an ops view that is
trusted past its evidence is worse than none:

- **Only instrumented code is visible.** Every row this view can show comes
  from ``BaseService.track_execution`` — it is the sole caller of
  ``create_run``, and the one other writer of ``service_runs`` (the scheduler's
  periodic marker, via ``record_run``) is excluded from the aggregate by name.
  So a request that fails before entering the service — in the route handler,
  in auth, in request parsing — writes nothing, and is silence here rather than
  a failure.
- **A run killed mid-flight is not a failure.** ``failure_count`` counts only
  rows the service itself resolved to ``failed``; a process killed by OOM or a
  restart leaves its row at ``running`` forever. Those still count as calls, so
  they *lower* the error rate. They are reported in their own ``Unresolved``
  column rather than folded into either side.
- **A service with no calls in the window does not appear.** Absence is not
  health; nothing distinguishes "ran clean" from "was never invoked".
"""

import click
from rich.console import Console
from rich.table import Table

from src.services.core.dashboard_service import DashboardService
from src.services.core.health_check import HealthCheckService

console = Console()


@click.command(name="check-health")
def check_health():
    """Check system health status."""
    console.print("[bold blue]Running health checks...[/bold blue]\n")

    service = HealthCheckService()
    result = service.check_all()

    # Overall status
    if result["status"] == "healthy":
        console.print("[bold green]✓ System Status: HEALTHY[/bold green]\n")
    else:
        console.print("[bold yellow]⚠ System Status: UNHEALTHY[/bold yellow]\n")

    # Create table
    table = Table(title="Health Check Results")
    table.add_column("Component", style="cyan")
    table.add_column("Status", justify="center")
    table.add_column("Message")

    for name, check in result["checks"].items():
        status = "✓" if check["healthy"] else "✗"
        status_color = "green" if check["healthy"] else "red"

        table.add_row(
            name.title(), f"[{status_color}]{status}[/{status_color}]", check["message"]
        )

    console.print(table)


@click.command(name="service-health")
@click.option(
    "--hours",
    default=24,
    show_default=True,
    type=click.IntRange(min=1),
    help="Look-back window for the aggregation.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit the raw payload as JSON instead of a table.",
)
@click.option(
    "--fail-over",
    "fail_over",
    type=click.FloatRange(0.0, 1.0),
    default=None,
    help=(
        "Exit non-zero if any service's error rate is at or above RATE. "
        "Deliberately has no default: what counts as too many errors is an "
        "operator judgement, and a guessed threshold would either page on "
        "noise or stay silent through an outage."
    ),
)
def service_health(hours, as_json, fail_over):
    """Show per-service call counts and error rates from service_runs.

    Read-only. Suitable for cron with --fail-over, which is the only way this
    command reports anything other than exit 0.
    """
    with DashboardService() as service:
        stats = service.get_service_health_stats(hours=hours)

    if as_json:
        console.print_json(data=stats)
    else:
        _render_service_health(stats, hours)

    if fail_over is None:
        return

    breached = [s for s in stats["services"] if s["error_rate"] >= fail_over]
    if breached:
        if not as_json:
            console.print(
                f"\n[bold red]✗ {len(breached)} service(s) at or above "
                f"an error rate of {fail_over}:[/bold red] "
                + ", ".join(s["service_name"] for s in breached)
            )
        raise SystemExit(1)


def _unresolved(row: dict) -> int:
    """Runs that never reached a terminal status.

    Derived rather than queried: the aggregate reports calls, successes and
    failures, and a row that is none of the latter two is still running or was
    killed before it could say otherwise.
    """
    return row["call_count"] - row["success_count"] - row["failure_count"]


def _render_service_health(stats: dict, hours: int) -> None:
    services = stats["services"]
    if not services:
        console.print(
            f"[yellow]No service runs recorded in the last {hours}h.[/yellow]\n"
            "[dim]This is an absence of telemetry, not a clean bill of health.[/dim]"
        )
        return

    table = Table(title=f"Service Health — last {hours}h")
    table.add_column("Service", style="cyan")
    table.add_column("Calls", justify="right")
    table.add_column("OK", justify="right", style="green")
    table.add_column("Failed", justify="right", style="red")
    table.add_column("Unresolved", justify="right", style="yellow")
    table.add_column("Error rate", justify="right")
    table.add_column("Avg ms", justify="right")

    for row in services:
        rate = row["error_rate"]
        rate_style = "red" if rate > 0 else "green"
        table.add_row(
            row["service_name"],
            str(row["call_count"]),
            str(row["success_count"]),
            str(row["failure_count"]),
            str(_unresolved(row)),
            f"[{rate_style}]{rate:.2f}[/{rate_style}]",
            str(row["avg_duration_ms"]),
        )

    console.print(table)
    console.print(
        f"\nTotals: {stats['total_calls']} calls, "
        f"{stats['total_failures']} failed, "
        f"overall error rate {stats['overall_error_rate']:.2f}"
    )
    if any(_unresolved(r) for r in services):
        console.print(
            "[dim]Unresolved runs count as calls but not as failures, so they "
            "pull the error rate down. Investigate them as suspected crashes.[/dim]"
        )
