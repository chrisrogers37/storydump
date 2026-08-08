"""Usage measurement CLI commands.

Read-only. Reports what tenants did; refuses nothing and changes nothing.
"""

import click
from rich.console import Console
from rich.table import Table

from src.services.core.usage_service import UsageService

console = Console()


@click.command(name="usage-report")
@click.option(
    "--days",
    default=30,
    show_default=True,
    type=click.IntRange(min=1),
    help="Trailing window in days.",
)
@click.option(
    "--limit",
    default=None,
    type=click.IntRange(min=1),
    help="Show only the busiest N tenants. Totals still cover every tenant.",
)
def usage_report(days: int, limit: int | None):
    """Report per-tenant posting activity over a trailing window."""
    with UsageService() as service:
        rows = service.usage_by_tenant(days=days)

    if not rows:
        console.print(f"[yellow]No posting activity in the last {days} days[/yellow]")
        return

    # Folded from the rows already fetched: one query, one window. Asking the
    # service for totals separately would re-run the aggregate over a window
    # computed microseconds later, and the footer could disagree with the table.
    totals = UsageService.summarize(rows)
    shown = rows[:limit] if limit else rows

    table = Table(title=f"Usage — last {days} days")
    table.add_column("Tenant (chat_settings_id)", style="cyan")
    table.add_column("Posts", justify="right")
    table.add_column("OK", justify="right")
    table.add_column("Failed", justify="right")
    table.add_column("API", justify="right")
    table.add_column("Manual", justify="right")

    for row in shown:
        tenant = row["chat_settings_id"] or "— (pre-tenancy)"
        table.add_row(
            tenant,
            str(row["total"]),
            str(row["successful"]),
            str(row["failed"]),
            str(row["api_posts"]),
            str(row["manual_posts"]),
        )

    console.print(table)

    if limit and len(rows) > len(shown):
        console.print(
            f"[dim]Showing {len(shown)} of {len(rows)} tenants; "
            f"totals below cover all {len(rows)}.[/dim]"
        )

    console.print(
        f"\n[bold]{totals['tenants']}[/bold] tenants · "
        f"[bold]{totals['total']}[/bold] posts "
        f"({totals['successful']} ok, {totals['failed']} failed) · "
        f"{totals['api_posts']} API, {totals['manual_posts']} manual"
    )
