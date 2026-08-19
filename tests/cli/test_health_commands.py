"""Tests for health CLI commands."""

from contextlib import contextmanager
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from cli.commands.health import _unresolved, check_health, service_health


@pytest.mark.unit
class TestHealthCommands:
    """Test suite for health CLI commands."""

    def test_check_health_command(self, test_db):
        """Test check-health CLI command."""
        runner = CliRunner()

        result = runner.invoke(check_health, [])

        # Command should execute successfully
        assert result.exit_code == 0
        # Should show health check results
        assert "database" in result.output.lower() or "health" in result.output.lower()

    def test_check_health_shows_all_checks(self, test_db):
        """Test that check-health shows all health checks."""
        runner = CliRunner()

        result = runner.invoke(check_health, [])

        assert result.exit_code == 0

        # Should include main health check categories
        output_lower = result.output.lower()
        # At least some health check information should be present
        assert any(
            keyword in output_lower
            for keyword in ["database", "telegram", "queue", "health", "check"]
        )


# ---------------------------------------------------------------------------
# service-health (#878) — the ops view over service_runs telemetry
# ---------------------------------------------------------------------------


def _row(name="MediaSyncService", calls=4, ok=3, failed=1, avg=120):
    return {
        "service_name": name,
        "call_count": calls,
        "success_count": ok,
        "failure_count": failed,
        "error_rate": round(failed / calls, 2) if calls else 0,
        "avg_duration_ms": avg,
    }


def _payload(rows, hours=24):
    calls = sum(r["call_count"] for r in rows)
    fails = sum(r["failure_count"] for r in rows)
    return {
        "services": rows,
        "total_calls": calls,
        "total_failures": fails,
        "overall_error_rate": round(fails / calls, 2) if calls else 0,
        "hours": hours,
    }


def _flat(output: str) -> str:
    """Rich hard-wraps at the terminal width, so a phrase can arrive split
    across lines. Collapsing whitespace before matching keeps the negative
    assertions honest — a substring check against wrapped output passes
    whether the text is absent OR merely broken, which is no control at all."""
    return " ".join(output.split())


@contextmanager
def _service_returning(payload):
    """Patch DashboardService at the name the command resolves."""
    with patch("cli.commands.health.DashboardService") as cls:
        svc = cls.return_value.__enter__.return_value
        svc.get_service_health_stats.return_value = payload
        yield svc


@pytest.mark.unit
class TestServiceHealthCommand:
    def test_it_renders_a_row_per_service(self):
        with _service_returning(
            _payload([_row(), _row("SchedulerService", 10, 10, 0)])
        ):
            result = CliRunner().invoke(service_health, [])
        assert result.exit_code == 0
        assert "MediaSyncService" in result.output
        assert "SchedulerService" in result.output

    def test_the_lookback_window_is_passed_through(self):
        with _service_returning(_payload([_row()], hours=6)) as svc:
            result = CliRunner().invoke(service_health, ["--hours", "6"])
        assert result.exit_code == 0
        svc.get_service_health_stats.assert_called_once_with(hours=6)

    def test_an_empty_window_is_reported_as_absence_not_health(self):
        """A service with no calls does not appear at all, so silence must not
        render as a clean bill of health."""
        with _service_returning(_payload([])):
            result = CliRunner().invoke(service_health, [])
        assert result.exit_code == 0
        assert "No service runs recorded" in _flat(result.output)
        assert "not a clean bill of health" in _flat(result.output)

    def test_json_mode_emits_the_payload(self):
        with _service_returning(_payload([_row()])):
            result = CliRunner().invoke(service_health, ["--json"])
        assert result.exit_code == 0
        assert "MediaSyncService" in result.output
        assert "overall_error_rate" in result.output


@pytest.mark.unit
class TestUnresolvedRunsAreVisibleRatherThanFolded:
    """A run killed mid-flight stays 'running' forever: it counts as a call but
    never as a failure, so it pulls the error rate DOWN. Measured on a live
    database while building this: one honest failure reports 1.00, and adding a
    single stuck run takes the same service to 0.50. The column exists so a
    crash cannot read as an improvement."""

    def test_unresolved_is_calls_minus_ok_minus_failed(self):
        assert _unresolved(_row(calls=5, ok=3, failed=1)) == 1

    def test_a_stuck_run_is_shown_in_its_own_column(self):
        # 4 calls, 1 ok, 1 failed -> 2 unresolved, and error_rate stays 0.25
        with _service_returning(_payload([_row(calls=4, ok=1, failed=1)])):
            result = CliRunner().invoke(service_health, [])
        assert result.exit_code == 0
        assert "Unresolved" in result.output
        assert "pull the error rate down" in _flat(result.output)

    def test_no_unresolved_runs_means_no_warning(self):
        """Paired negative: the caveat must not print when it does not apply,
        or it becomes wallpaper."""
        with _service_returning(_payload([_row(calls=4, ok=3, failed=1)])):
            result = CliRunner().invoke(service_health, [])
        assert "pull the error rate down" not in _flat(result.output)


@pytest.mark.unit
class TestFailOverIsTheOnlyNonZeroExit:
    """The cron contract. Without --fail-over the command is a report; with it,
    it is a detector."""

    def test_without_the_flag_a_total_outage_still_exits_zero(self):
        with _service_returning(_payload([_row(calls=9, ok=0, failed=9)])):
            result = CliRunner().invoke(service_health, [])
        assert result.exit_code == 0

    def test_a_breach_exits_one_and_names_the_service(self):
        with _service_returning(_payload([_row(calls=9, ok=0, failed=9)])):
            result = CliRunner().invoke(service_health, ["--fail-over", "0.5"])
        assert result.exit_code == 1
        assert "MediaSyncService" in result.output

    def test_a_rate_below_the_threshold_exits_zero(self):
        with _service_returning(_payload([_row(calls=10, ok=9, failed=1)])):
            result = CliRunner().invoke(service_health, ["--fail-over", "0.5"])
        assert result.exit_code == 0

    def test_the_threshold_is_inclusive(self):
        """'at or above RATE' — pinned because an off-by-one here is the
        difference between paging and silence."""
        with _service_returning(_payload([_row(calls=2, ok=1, failed=1)])):
            result = CliRunner().invoke(service_health, ["--fail-over", "0.5"])
        assert result.exit_code == 1

    def test_only_the_breaching_service_is_named(self):
        rows = [_row("Healthy", 10, 10, 0), _row("Broken", 4, 0, 4)]
        with _service_returning(_payload(rows)):
            result = CliRunner().invoke(service_health, ["--fail-over", "0.9"])
        assert result.exit_code == 1
        tail = _flat(result.output).split("service(s) at or above")[-1]
        assert "Broken" in tail and "Healthy" not in tail


@pytest.mark.integration
class TestTheDetectorSeesARealInstrumentedFailure:
    """End-to-end against a live database, because the value claim for this
    command is empirical: it is meant to surface the class of outage where a
    service 500s on every call with no signal reaching anyone.

    Everything above this class mocks the service layer, so it pins the
    rendering and the exit code but says nothing about whether a real failure
    ever reaches them. This does.
    """

    @pytest.fixture(autouse=True)
    def _clean_slate(self, route_repos_to_test_db):
        """``route_repos_to_test_db`` rebinds the session factory but does not
        roll back, and ``--fail-over`` aggregates across every service — so one
        test's failures decide the next test's verdict. Measured while writing
        this: with these two tests in the opposite order the healthy control
        reads an overall rate of 0.75 and exits 1.
        """
        import src.config.database as db_module
        from src.models.service_run import ServiceRun

        session = db_module.SessionLocal()
        try:
            session.query(ServiceRun).delete()
            session.commit()
        finally:
            session.close()
        yield

    def _run(self, name, *, fail):
        from src.repositories.service_run_repository import ServiceRunRepository

        repo = ServiceRunRepository()
        run_id = repo.create_run(
            service_name=name, method_name="sync", triggered_by="user"
        )
        if fail:
            repo.fail_run(
                run_id=run_id,
                error_type="TenantContextError",
                error_message="tenant context is required",
                stack_trace="",
                duration_ms=5,
            )
        else:
            repo.complete_run(run_id=run_id, success=True, duration_ms=5)

    def test_a_healthy_service_exits_zero(self, route_repos_to_test_db):
        """Positive control. Without this, the failing case below proves only
        that the command can exit 1, not that it discriminates."""
        self._run("ControlService", fail=False)
        result = CliRunner().invoke(service_health, ["--fail-over", "0.5"])
        assert result.exit_code == 0, result.output
        assert "ControlService" in result.output

    def test_a_service_failing_every_call_is_caught(self, route_repos_to_test_db):
        for _ in range(3):
            self._run("MediaSyncService", fail=True)
        result = CliRunner().invoke(service_health, ["--fail-over", "0.5"])
        assert result.exit_code == 1, result.output
        assert "MediaSyncService" in _flat(result.output)
        assert "1.00" in result.output
