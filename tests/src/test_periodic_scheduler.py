"""Tests for PeriodicScheduler — durable last-run gating for scheduler sub-tasks.

Regression coverage for #547: the scheduler's periodic sub-tasks (notably
Instagram token refresh) were gated on in-memory tick counters that reset to 0
on every process start. Under Railway's sub-24h redeploy cadence the 1440-tick
(24h) token-refresh gate never reached its threshold, so refresh never ran and
IG tokens silently expired after 60 days with no alert. PeriodicScheduler gates
each sub-task on a timestamp persisted in service_runs, so the interval is
honored on wall-clock time regardless of restarts.
"""

from datetime import datetime, timedelta, timezone

import pytest
from unittest.mock import Mock

from src.services.core.loops.periodic import PeriodicScheduler

DAY = 86400
HOUR = 3600
NOW = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


def _gate(last_run_at):
    """Build a PeriodicScheduler over a mocked repo returning last_run_at."""
    repo = Mock()
    repo.get_last_run_at.return_value = last_run_at
    return PeriodicScheduler(repo), repo


@pytest.mark.unit
class TestPeriodicSchedulerDue:
    def test_due_when_last_run_older_than_interval(self):
        """A sub-task whose persisted last_run is older than its interval is
        due — even on a fresh process whose in-memory counter would be 0."""
        gate, _ = _gate(NOW - timedelta(hours=25))
        assert gate.due("token_refresh", DAY, now=NOW) is True

    def test_not_due_when_last_run_recent(self):
        """A recent persisted last_run keeps the task from re-firing — the
        timestamp is honored, so a restart cannot re-trigger it (no re-spam)."""
        gate, _ = _gate(NOW - timedelta(hours=1))
        assert gate.due("token_refresh", DAY, now=NOW) is False

    def test_due_when_never_run(self):
        """No persisted run (fresh install, or aged out of retention) → run
        promptly. This catch-up is what revives token_refresh on a worker that
        has never completed a full 24h uptime streak."""
        gate, _ = _gate(None)
        assert gate.due("token_refresh", DAY, now=NOW) is True

    def test_due_at_exact_interval_boundary(self):
        """Elapsed == interval is due (>= comparison)."""
        gate, _ = _gate(NOW - timedelta(seconds=DAY))
        assert gate.due("token_refresh", DAY, now=NOW) is True

    def test_due_handles_naive_persisted_timestamp(self):
        """service_runs DateTime columns are naive-UTC; due() must not
        TypeError comparing a naive last_run to an aware now."""
        naive_last = datetime(2026, 6, 1, 12, 0)  # naive, well in the past
        gate, _ = _gate(naive_last)
        assert gate.due("token_refresh", DAY, now=NOW) is True

    def test_due_queries_marker_namespace_by_task(self):
        gate, repo = _gate(None)
        gate.due("retention", HOUR, now=NOW)
        repo.get_last_run_at.assert_called_once_with(
            PeriodicScheduler.MARKER_SERVICE, method_name="retention"
        )


@pytest.mark.unit
class TestPeriodicSchedulerMarkRan:
    def test_mark_ran_persists_marker_at_now(self):
        gate, repo = _gate(None)
        gate.mark_ran("token_refresh", now=NOW)
        repo.record_run.assert_called_once_with(
            PeriodicScheduler.MARKER_SERVICE, method_name="token_refresh", ran_at=NOW
        )


@pytest.mark.unit
class TestTokenRefreshSurvivesRedeploys:
    def test_token_refresh_fires_daily_despite_sub_interval_restarts(self):
        """The #547 regression test.

        Simulate a worker that redeploys every 6h — a NEW PeriodicScheduler
        each time (per-process counter state is gone) — backed by a store that
        survives restarts (the durable service_runs table). token_refresh must
        stay suppressed while <24h has elapsed since its last real run, then
        fire once 24h passes — driven purely by the persisted timestamp, never
        by a per-process counter (which would sit at 0 forever and never fire).
        """
        store: dict[str, datetime] = {}

        def fresh_process():
            """A new gate == a new process (counters reset), same durable store."""
            repo = Mock()
            repo.get_last_run_at.side_effect = lambda svc, method_name: store.get(
                method_name
            )
            repo.record_run.side_effect = lambda svc, method_name, ran_at: (
                store.__setitem__(method_name, ran_at)
            )
            return PeriodicScheduler(repo)

        t0 = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)

        # Boot 1: never run → due → fires and records.
        gate = fresh_process()
        assert gate.due("token_refresh", DAY, now=t0) is True
        gate.mark_ran("token_refresh", now=t0)

        # Redeploys at +6h, +12h, +18h: fresh process each time, still <24h
        # since the recorded run. The old counter would be 0 (never fires);
        # the persisted timestamp correctly suppresses instead.
        for hours in (6, 12, 18):
            t = t0 + timedelta(hours=hours)
            assert fresh_process().due("token_refresh", DAY, now=t) is False, (
                f"+{hours}h"
            )

        # Redeploy at +25h: 24h has now elapsed since the last real run → fires,
        # even though this process's counter is 0.
        t = t0 + timedelta(hours=25)
        gate = fresh_process()
        assert gate.due("token_refresh", DAY, now=t) is True
        gate.mark_ran("token_refresh", now=t)
        assert store["token_refresh"] == t
