"""Usage service - read-only measurement of per-tenant posting activity.

Measurement only. Nothing here is consulted by a gate, nothing writes, and
there is no code path through this module that can refuse a request. That is a
structural property rather than a policy: entitlements and enforcement are
deferred by FC-9 of the consolidated design plan, whose extension point is a
``workspace_limits`` row-set consulted by the admission path — not this
service.

Measuring usage needs no schema. ``posting_history`` already records the
tenant (``chat_settings_id``), an indexed timestamp, the outcome, and the
posting method, so usage is a query over data the product already keeps.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from src.repositories.history_repository import HistoryRepository
from src.services.base_service import BaseService


class UsageService(BaseService):
    """Read-only per-tenant usage measurement."""

    def __init__(self):
        super().__init__()
        self.history_repo = HistoryRepository()

    def usage_by_tenant(
        self, days: int = 30, until: Optional[datetime] = None
    ) -> list[dict]:
        """Per-tenant posting activity over a trailing window.

        Args:
            days: Window length in days, counted back from ``until``.
            until: End of the window, exclusive. Defaults to now.

        Returns:
            One dict per tenant — ``chat_settings_id``, ``total``,
            ``successful``, ``failed``, ``api_posts``, ``manual_posts`` —
            busiest first. Legacy rows with no tenant are reported under a
            ``chat_settings_id`` of ``None`` rather than dropped, so the totals
            reconcile with the raw row count for the same window.
        """
        if days < 1:
            raise ValueError(f"days must be at least 1, got {days}")

        window_end = until or datetime.now(timezone.utc)
        return self.history_repo.usage_by_tenant(
            since=window_end - timedelta(days=days), until=until
        )

    @staticmethod
    def summarize(rows: list[dict]) -> dict:
        """Fold rows from :meth:`usage_by_tenant` into deployment-wide totals.

        Takes rows rather than a window so a caller wanting both the table and
        its totals pays for one query, over one window. Two calls would compute
        two windows microseconds apart, and a row landing between them would
        make the totals disagree with the rows above them.

        Returns:
            ``tenants`` (how many posted at all), plus summed ``total``,
            ``successful``, ``failed``, ``api_posts`` and ``manual_posts``.
        """
        summed = {
            key: sum(row[key] for row in rows)
            for key in ("total", "successful", "failed", "api_posts", "manual_posts")
        }
        return {"tenants": len(rows), **summed}

    def usage_totals(self, days: int = 30, until: Optional[datetime] = None) -> dict:
        """Deployment-wide totals for the window, for callers wanting only these."""
        return self.summarize(self.usage_by_tenant(days=days, until=until))
