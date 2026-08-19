"""Service run repository - CRUD operations for service runs."""

from typing import Optional, List
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func

from src.repositories.base_repository import BaseRepository
from src.models.service_run import ServiceRun
from src.utils.datetime_utils import ensure_utc

# Reserved service_name for the scheduler's durable periodic-task marker rows
# (see PeriodicScheduler). Namespaced so it never collides with real service
# audit rows and can be excluded from observability aggregates.
PERIODIC_MARKER_SERVICE = "scheduler_periodic"


class ServiceRunRepository(BaseRepository):
    """Repository for ServiceRun CRUD operations."""

    def __init__(self):
        super().__init__()

    def get_by_id(self, run_id: str) -> Optional[ServiceRun]:
        """Get service run by ID."""
        result = self.db.query(ServiceRun).filter(ServiceRun.id == run_id).first()
        self.end_read_transaction()
        return result

    def create_run(
        self,
        service_name: str,
        method_name: str,
        user_id: Optional[str] = None,
        triggered_by: str = "system",
        input_params: Optional[dict] = None,
        context_metadata: Optional[dict] = None,
    ) -> str:
        """Create a new service run record. Returns run_id."""
        run = ServiceRun(
            service_name=service_name,
            method_name=method_name,
            user_id=user_id,
            triggered_by=triggered_by,
            input_params=input_params,
            context_metadata=context_metadata,
        )
        self.db.add(run)
        self.commit_and_refresh(run)
        return str(run.id)

    def record_run(
        self,
        service_name: str,
        method_name: str,
        ran_at: Optional[datetime] = None,
    ) -> None:
        """Persist a lightweight completed marker row.

        Used by the scheduler loop to stamp when a periodic sub-task last ran
        (see get_last_run_at and PeriodicScheduler), so the interval survives a
        process restart. started_at is stored naive-UTC to match the column
        convention (see ensure_utc).
        """
        if ran_at is None:
            ts = datetime.utcnow()
        else:
            # Store naive-UTC to match the column convention (ensure_utc treats
            # a naive input as already-UTC; astimezone normalizes any offset).
            ts = ensure_utc(ran_at).astimezone(timezone.utc).replace(tzinfo=None)
        run = ServiceRun(
            service_name=service_name,
            method_name=method_name,
            triggered_by="scheduler",
            started_at=ts,
            completed_at=ts,
            status="completed",
            success=True,
            duration_ms=0,
        )
        self.db.add(run)
        self.db.commit()

    def complete_run(
        self,
        run_id: str,
        success: bool,
        duration_ms: int,
        result_summary: Optional[dict] = None,
    ):
        """Mark a service run as completed."""
        run = self.get_by_id(run_id)
        if run:
            run.status = "completed"
            run.success = success
            run.completed_at = datetime.utcnow()
            run.duration_ms = duration_ms
            run.result_summary = result_summary
            self.db.commit()

    def fail_run(
        self,
        run_id: str,
        error_type: str,
        error_message: str,
        stack_trace: str,
        duration_ms: int,
    ):
        """Mark a service run as failed."""
        run = self.get_by_id(run_id)
        if run:
            run.status = "failed"
            run.success = False
            run.completed_at = datetime.utcnow()
            run.duration_ms = duration_ms
            run.error_type = error_type
            run.error_message = error_message
            run.stack_trace = stack_trace
            self.db.commit()

    def set_result_summary(self, run_id: str, summary: dict):
        """Update the result summary for a run."""
        run = self.get_by_id(run_id)
        if run:
            run.result_summary = summary
            self.db.commit()

    # Used by InstagramBackfillService.get_backfill_status() and
    # MediaSyncService.get_last_sync_info(), plus test_base_service.py integration tests.
    def get_recent_runs(
        self, service_name: Optional[str] = None, limit: int = 100
    ) -> List[ServiceRun]:
        """Get recent service runs."""
        query = self.db.query(ServiceRun)

        if service_name:
            query = query.filter(ServiceRun.service_name == service_name)

        result = query.order_by(ServiceRun.started_at.desc()).limit(limit).all()
        self.end_read_transaction()
        return result

    def get_last_run_at(
        self, service_name: str, method_name: Optional[str] = None
    ) -> Optional[datetime]:
        """Return the started_at of the most recent run for a service (+ method).

        Powers the scheduler's durable periodic-task gating
        (PeriodicScheduler): it reads when a task last ran so the interval
        survives process restarts, unlike the in-memory tick counters this
        replaced. Returns None when no matching run exists (or it aged out of
        retention) — the caller treats that as "run promptly".
        """
        query = self.db.query(ServiceRun.started_at).filter(
            ServiceRun.service_name == service_name
        )
        if method_name is not None:
            query = query.filter(ServiceRun.method_name == method_name)
        row = query.order_by(ServiceRun.started_at.desc()).first()
        self.end_read_transaction()
        return row[0] if row else None

    def delete_older_than(self, days: int) -> int:
        """Delete service runs older than the given number of days.

        Used for retention policy to prevent unbounded table growth.

        Args:
            days: Delete runs with started_at older than this many days ago.

        Returns:
            Number of rows deleted.
        """
        cutoff = datetime.utcnow() - timedelta(days=days)
        count = (
            self.db.query(ServiceRun).filter(ServiceRun.started_at < cutoff).delete()
        )
        self.db.commit()
        return count

    # NOTE: Unused in production as of 2026-02-10.
    # Planned for Phase 3 monitoring dashboard and alerting system.
    def get_failed_runs(
        self, since_hours: int = 24, limit: int = 50
    ) -> List[ServiceRun]:
        """Get recent failed runs."""
        since = datetime.utcnow() - timedelta(hours=since_hours)
        result = (
            self.db.query(ServiceRun)
            .filter(ServiceRun.status == "failed", ServiceRun.started_at >= since)
            .order_by(ServiceRun.started_at.desc())
            .limit(limit)
            .all()
        )
        self.end_read_transaction()
        return result

    def get_health_stats(self, hours: int = 24) -> list:
        """Aggregate service run stats per service over a time window.

        Returns per-service: call_count, success_count, failure_count,
        error_rate, avg_duration_ms.
        """
        since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)
        rows = (
            self.db.query(
                ServiceRun.service_name,
                func.count(ServiceRun.id).label("call_count"),
                func.sum(case((ServiceRun.status == "completed", 1), else_=0)).label(
                    "success_count"
                ),
                func.sum(case((ServiceRun.status == "failed", 1), else_=0)).label(
                    "failure_count"
                ),
                func.avg(ServiceRun.duration_ms).label("avg_duration_ms"),
            )
            .filter(ServiceRun.started_at >= since)
            .filter(ServiceRun.service_name != PERIODIC_MARKER_SERVICE)
            .group_by(ServiceRun.service_name)
            .order_by(func.count(ServiceRun.id).desc())
            .all()
        )
        self.end_read_transaction()

        return [self._health_row(r) for r in rows]

    @staticmethod
    def _health_row(r) -> dict:
        """One service's health, with unresolved runs as a FIRST-CLASS term.

        ``unresolved_count`` is every run this service never resolved to
        ``completed`` or ``failed`` — a process killed by OOM or a restart
        leaves its row at ``running`` forever, and an in-flight run looks the
        same until it finishes.

        ``error_rate`` is failures over **resolved** runs, not over
        ``call_count``. Dividing outcomes by attempts-including-unfinished is a
        category error and it fails in the worst available direction: an
        unresolved run landed in the denominator and never the numerator, so a
        crash **lowered** the rate. Measured before this changed: one honest
        failure read 1.00 and a single stuck run alongside it took the same
        service to 0.50 — the detector reporting healthier under exactly the
        condition it exists for. Over resolved runs a stuck row moves the rate
        neither way, because it is not yet evidence about success or failure;
        it is reported on its own axis instead.
        """
        success = r.success_count or 0
        failure = r.failure_count or 0
        resolved = success + failure
        return {
            "service_name": r.service_name,
            "call_count": r.call_count,
            "success_count": success,
            "failure_count": failure,
            "unresolved_count": max(0, r.call_count - resolved),
            "resolved_count": resolved,
            "error_rate": round(failure / resolved, 2) if resolved else 0,
            "avg_duration_ms": round(r.avg_duration_ms) if r.avg_duration_ms else 0,
        }
