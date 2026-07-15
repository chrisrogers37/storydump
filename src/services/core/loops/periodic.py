"""Durable interval gating for the scheduler loop's periodic sub-tasks.

The scheduler runs several sub-tasks on a fixed cadence — retention cleanup,
pool / Drive-token health alerts, and (critically) Instagram token refresh.
These used to be driven by in-memory tick counters that reset to 0 on every
process start. Under Railway's normal sub-24h redeploy cadence the 1440-tick
(24h) token-refresh gate never reached its threshold, so refresh never ran and
IG tokens silently expired after 60 days with no alert — a regression of #397
(see #547). The system only alerts on refresh *failures*; "never attempted"
produces none.

PeriodicScheduler replaces those counters with a last-run timestamp persisted
in ``service_runs``. Each tick asks ``due(task, interval, now)``; a task whose
last run is older than its interval — or that has never run / aged out of
retention — fires promptly and then records the run via ``mark_ran``. Because
the timestamp survives process restarts, the interval is honored on wall-clock
time regardless of redeploy churn, which is what keeps token refresh alive.

The store is injected (a ``ServiceRunRepository``) so the gate is trivially
mockable — tests freeze ``now`` and assert firing without touching a clock or a
database.
"""

from datetime import datetime

from src.repositories.service_run_repository import (
    PERIODIC_MARKER_SERVICE,
    ServiceRunRepository,
)
from src.utils.datetime_utils import ensure_utc


class PeriodicScheduler:
    """Gate scheduler periodic sub-tasks on a durable last-run timestamp."""

    # Reserved service_name for the marker rows this gate writes to
    # service_runs (defined by the repo that owns the table's vocabulary).
    # Each task's key is stored as the marker's method_name.
    MARKER_SERVICE = PERIODIC_MARKER_SERVICE

    def __init__(self, service_run_repo: ServiceRunRepository):
        self._repo = service_run_repo

    def due(self, task: str, interval_seconds: float, *, now: datetime) -> bool:
        """Return True if ``task`` should run at ``now``.

        Due when the persisted last-run timestamp is at least
        ``interval_seconds`` old, or when there is no persisted run at all
        (fresh install, or the marker aged out of retention) — in which case
        the task runs promptly. That "catch up on start" behavior is exactly
        what revives token refresh on a worker that never completes a full 24h
        uptime streak.
        """
        last_run = self._repo.get_last_run_at(self.MARKER_SERVICE, method_name=task)
        if last_run is None:
            return True
        elapsed = (now - ensure_utc(last_run)).total_seconds()
        return elapsed >= interval_seconds

    def mark_ran(self, task: str, *, now: datetime) -> None:
        """Persist that ``task`` ran at ``now`` so the next interval is measured
        from here and survives a restart."""
        self._repo.record_run(self.MARKER_SERVICE, method_name=task, ran_at=now)
