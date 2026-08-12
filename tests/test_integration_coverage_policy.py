"""The policy that decides run / skip / fail for integration coverage (#758).

The bug this pins was a *policy decision buried in an `except Exception`*: every
failure mode — no PostgreSQL installed, a service container that never came up,
a database another session dropped mid-run — collapsed into the same answer,
"skip". A run then reported `N passed, M skipped` at `rc 0`, and reading the
tick, or "0 failed", said the integration tests passed when they had never
executed.

Nothing could test that decision while it lived inside the `except`. It lives in
two pure functions now, and this file is the reason they are pure: the whole
truth table is assertable with no PostgreSQL anywhere near it, which is exactly
the environment where the old behaviour was least visible.

These tests deliberately import from `tests.conftest` rather than re-deriving
the rules — a check that re-implements the contract tests the re-implementation.
"""

import pytest

from tests.conftest import (
    MAX_EXPECTED_SKIPS,
    REQUIRE_DB_ENV,
    database_is_required,
    integration_verdict,
    skip_ceiling_breach,
)


class TestTheVerdict:
    """Three outcomes. The old code had two, and the missing one is the bug."""

    def test_a_reachable_server_runs_the_tests(self):
        assert integration_verdict(server_answered=True, required=False) == "run"
        assert integration_verdict(server_answered=True, required=True) == "run"

    def test_no_server_and_no_requirement_skips(self):
        """The one honest skip: a contributor with no PostgreSQL installed."""
        assert integration_verdict(server_answered=False, required=False) == "skip"

    def test_no_server_where_one_is_required_fails(self):
        """THE REGRESSION. In CI "no database" is not a contributor without
        PostgreSQL — it is the service container failing to come up, and the
        old code answered that with a green run."""
        assert integration_verdict(server_answered=False, required=True) == "fail"

    def test_the_requirement_is_what_separates_the_two_negatives(self):
        """Stated as an assertion because it is the whole design: identical
        observable state, opposite correct answers, and only the environment's
        declared requirement distinguishes them."""
        assert integration_verdict(False, required=False) != integration_verdict(
            False, required=True
        )


class TestTheSkipCeiling:
    """The backstop for mass-skip shapes the verdict cannot see — a stray
    module-level skipmark, a missing optional dependency."""

    def test_a_normal_run_is_silent(self):
        assert skip_ceiling_breach(MAX_EXPECTED_SKIPS, MAX_EXPECTED_SKIPS, True) is None
        assert skip_ceiling_breach(0, MAX_EXPECTED_SKIPS, True) is None

    def test_a_mass_skip_where_a_database_is_required_is_reported(self):
        breach = skip_ceiling_breach(500, MAX_EXPECTED_SKIPS, True)
        assert breach is not None
        assert "500" in breach and str(MAX_EXPECTED_SKIPS) in breach

    def test_it_stays_silent_where_no_database_is_required(self):
        """A contributor without PostgreSQL legitimately skips everything —
        failing their run would punish the honest case this exists to protect.
        """
        assert skip_ceiling_breach(500, MAX_EXPECTED_SKIPS, False) is None

    def test_the_ceiling_is_the_measured_baseline(self):
        """Provenance, pinned. Five consecutive CI runs sat at exactly 10
        skipped while the passed count moved from 2356 to 2376; two of them
        (31648859395, 31642935597) were re-measured independently.

        This number is hand-maintained and WILL drift as tests are added —
        that is its known cost, and it is why the verdict above, which derives
        its input, is the load-bearing gate and this is the backstop. Raise it
        deliberately when a legitimate skip lands; do not delete the check.
        """
        assert MAX_EXPECTED_SKIPS == 10


class TestTheRequirementSwitch:
    def test_it_is_off_unless_the_environment_asks_for_it(self, monkeypatch):
        monkeypatch.delenv(REQUIRE_DB_ENV, raising=False)
        assert database_is_required() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", " 1 "])
    def test_the_usual_truthy_spellings_all_work(self, monkeypatch, value):
        """CI YAML quotes booleans inconsistently and a switch that silently
        reads `"true"` as off would disarm the gate exactly where it matters."""
        monkeypatch.setenv(REQUIRE_DB_ENV, value)
        assert database_is_required() is True

    @pytest.mark.parametrize("value", ["", "0", "false", "no"])
    def test_falsey_spellings_leave_it_off(self, monkeypatch, value):
        monkeypatch.setenv(REQUIRE_DB_ENV, value)
        assert database_is_required() is False
