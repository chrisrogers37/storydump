"""Tests for health CLI commands."""

import json
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
    """Mirrors ServiceRunRepository._health_row: the rate is over RESOLVED
    runs, and unresolved is a real field rather than a rendering-time
    subtraction."""
    resolved = ok + failed
    return {
        "service_name": name,
        "call_count": calls,
        "success_count": ok,
        "failure_count": failed,
        "unresolved_count": max(0, calls - resolved),
        "resolved_count": resolved,
        "error_rate": round(failed / resolved, 2) if resolved else 0,
        "avg_duration_ms": avg,
    }


def _payload(rows, hours=24):
    calls = sum(r["call_count"] for r in rows)
    fails = sum(r["failure_count"] for r in rows)
    unres = sum(r["unresolved_count"] for r in rows)
    resolved = sum(r["resolved_count"] for r in rows)
    return {
        "services": rows,
        "total_calls": calls,
        "total_failures": fails,
        "total_unresolved": unres,
        "total_resolved": resolved,
        "overall_error_rate": round(fails / resolved, 2) if resolved else 0,
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

    def test_unresolved_is_read_from_the_payload_not_derived_in_the_renderer(self):
        assert _unresolved(_row(calls=5, ok=3, failed=1)) == 1

    def test_a_stuck_run_is_shown_in_its_own_column(self):
        # 4 calls, 1 ok, 1 failed -> 2 unresolved, and error_rate stays 0.25
        with _service_returning(_payload([_row(calls=4, ok=1, failed=1)])):
            result = CliRunner().invoke(service_health, [])
        assert result.exit_code == 0
        assert "Unresolved" in result.output
        assert "the rate will never show it" in _flat(result.output)

    def test_no_unresolved_runs_means_no_warning(self):
        """Paired negative: the caveat must not print when it does not apply,
        or it becomes wallpaper."""
        with _service_returning(_payload([_row(calls=4, ok=3, failed=1)])):
            result = CliRunner().invoke(service_health, [])
        assert "the rate will never show it" not in _flat(result.output)


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
        tail = _flat(result.output).split("threshold breach(es):")[-1]
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

    def _start_but_never_resolve(self, name):
        """A process killed mid-flight: the row is created and never updated."""
        from src.repositories.service_run_repository import ServiceRunRepository

        ServiceRunRepository().create_run(
            service_name=name, method_name="sync", triggered_by="user"
        )

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

    def test_rajans_reproduction_a_real_crash_is_caught_at_a_realistic_threshold(
        self, route_repos_to_test_db
    ):
        """The #882 review case, end to end through the REAL repository.

        One honest failure plus one crashed run. Before this fix the rate was
        failures/call_count = 0.50, so --fail-over caught it only at 0.5 and
        missed at 0.6 and 0.99 — thresholds an operator would reasonably pick
        to avoid noise — while a real crash sat in the data. Over resolved runs
        the rate is 1.00 and every threshold catches it.
        """
        self._run("MediaSyncService", fail=True)
        self._start_but_never_resolve("MediaSyncService")

        for threshold in ("0.5", "0.6", "0.75", "0.99", "1.0"):
            result = CliRunner().invoke(service_health, ["--fail-over", threshold])
            assert result.exit_code == 1, f"{threshold} missed it: {result.output}"

    @pytest.mark.integration
    def test_unresolved_reaches_the_json_consumer_from_the_real_repository(
        self, route_repos_to_test_db
    ):
        """The review's minimum ask, checked at the real data path rather than
        against a mocked payload: the number has to survive the repository and
        the service, not just the renderer."""
        self._run("MediaSyncService", fail=True)
        self._start_but_never_resolve("MediaSyncService")

        result = CliRunner().invoke(service_health, ["--json"])
        assert result.exit_code == 0, result.output
        row = json.loads(result.output)["services"][0]
        assert row["unresolved_count"] == 1
        assert row["resolved_count"] == 1
        assert row["error_rate"] == 1.0

    def test_a_service_failing_every_call_is_caught(self, route_repos_to_test_db):
        for _ in range(3):
            self._run("MediaSyncService", fail=True)
        result = CliRunner().invoke(service_health, ["--fail-over", "0.5"])
        assert result.exit_code == 1, result.output
        assert "MediaSyncService" in _flat(result.output)
        assert "1.00" in result.output


@pytest.mark.unit
class TestACrashCannotMakeTheCronDetectorLookHealthy:
    """Review finding on #882, reproduced against a live database before the
    fix: with 1 real failure and 1 crashed run, --fail-over MISSED at 0.6 and
    at 0.99, and --json carried no unresolved field at all. So the blind spot
    was visible in the table and invisible to both machine consumers — the
    detector reporting healthier under exactly the condition it exists for.
    """

    def _one_failure_and_one_crash(self):
        # call_count=2, success=0, failure=1, unresolved=1
        return _payload([_row("MediaSyncService", calls=2, ok=0, failed=1)])

    @pytest.mark.parametrize("threshold", ["0.5", "0.6", "0.75", "0.99", "1.0"])
    def test_fail_over_catches_it_at_every_threshold_not_just_a_lucky_one(
        self, threshold
    ):
        """Over resolved runs the rate is 1.00, so the crash cannot dilute it.
        Over call_count it was 0.50 and everything above that read clean."""
        with _service_returning(self._one_failure_and_one_crash()):
            result = CliRunner().invoke(service_health, ["--fail-over", threshold])
        assert result.exit_code == 1, f"{threshold}: {result.output}"

    def test_the_rate_does_not_improve_when_a_run_dies(self):
        one = _payload([_row("S", calls=1, ok=0, failed=1)])
        plus_crash = _payload([_row("S", calls=2, ok=0, failed=1)])
        assert one["services"][0]["error_rate"] == 1.0
        assert plus_crash["services"][0]["error_rate"] == 1.0

    def test_json_carries_unresolved_as_a_first_class_field(self):
        """The minimum the review asked for: a machine consumer must be able to
        build its own check, which it could not when the number existed only in
        the rendering layer."""
        with _service_returning(self._one_failure_and_one_crash()):
            result = CliRunner().invoke(service_health, ["--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["services"][0]["unresolved_count"] == 1
        assert payload["total_unresolved"] == 1

    def test_a_crash_with_no_failures_is_invisible_to_the_rate_and_needs_its_own_gate(
        self,
    ):
        """The residual the rate genuinely cannot carry: 5 successes and one
        crashed run is an error rate of 0.00 and a broken service."""
        payload = _payload([_row("S", calls=6, ok=5, failed=0)])
        assert payload["services"][0]["error_rate"] == 0.0
        assert payload["services"][0]["unresolved_count"] == 1

        with _service_returning(payload):
            missed = CliRunner().invoke(service_health, ["--fail-over", "0.01"])
        assert missed.exit_code == 0

        with _service_returning(payload):
            caught = CliRunner().invoke(service_health, ["--fail-unresolved", "1"])
        assert caught.exit_code == 1
        assert "unresolved" in _flat(caught.output)

    def test_fail_unresolved_does_not_fire_below_its_threshold(self):
        payload = _payload([_row("S", calls=6, ok=5, failed=0)])
        with _service_returning(payload):
            result = CliRunner().invoke(service_health, ["--fail-unresolved", "2"])
        assert result.exit_code == 0

    def test_neither_flag_still_means_exit_zero(self):
        with _service_returning(self._one_failure_and_one_crash()):
            result = CliRunner().invoke(service_health, [])
        assert result.exit_code == 0
