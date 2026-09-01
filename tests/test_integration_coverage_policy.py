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

**The mocked tests pin the branch; the last class pins its PREMISE (#804).**
Every test in `TestTheListenerProbe` monkeypatches `server_is_listening` to a
lambda, which is right for asserting a branch and blind to whether the branch's
premise still holds — that a real refusal from a real PostgreSQL arrives as an
`OperationalError` while the address is still answering TCP. That premise was
established once, by a reproduction in a review comment and in one session, and
nothing re-established it. `TestTheRefusalAgainstARealServer` does, with no
mocks at all.
"""

import socket
import uuid
from contextlib import closing
from unittest.mock import Mock

import psycopg2
import pytest

from src.config.settings import settings
from tests.conftest import (
    MAX_EXPECTED_SKIPS,
    REQUIRE_DB_ENV,
    SESSION_DB_SUFFIX,
    database_is_required,
    integration_verdict,
    maintenance_connection,
    precondition_absent,
    require_role_privilege,
    server_answered,
    server_is_listening,
    skip_ceiling_breach,
)


def _free_port() -> int:
    """A port nothing is listening on: bind one, note it, release it."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


#: Password for the probe role below. Not a credential for anything: the role
#: exists only to be refused, and is dropped in the same fixture that made it.
#: `CREATE ROLE ... LOGIN` needs one to produce a login role at all.
PROBE_ROLE_PASSWORD = "probe_role_password"


@pytest.fixture
def a_refusing_role():
    """A real login role the server will refuse — `CONNECTION LIMIT 0` (#801).

    The reproduction that established the #769 fix, pinned. It is cluster-scoped
    state, so the footprint is kept to the minimum that still reproduces:

    - **Uniquely named**, carrying #763's session token, so concurrent runs from
      other checkouts on the same cluster cannot collide on it — unlike the
      fixed-name `svc_*` roles that made `tests/scripts/` serialize on a mutex.
    - **Owns nothing and is granted nothing**, so it writes no `pg_shdepend`
      rows and its `DROP` cannot be blocked by a dependent object.
    - **Dropped in a `finally`.** A leak from a hard kill is inert rather than
      poisoning — a `probe804_*` role with no grants breaks no later run — so
      there is no sweep here and no need for one.

    The postcondition is asserted rather than assumed, because the failure it
    guards against reads as success: a **superuser is exempt from
    `rolconnlimit`** (documented PostgreSQL behaviour — not measured here, since
    a CREATEROLE-only test role cannot create a superuser to check it against),
    so a probe role that somehow gained that flag would connect happily, and
    every test below would pass down `server_answered()`'s happy path.

    Yields the credential as a PAIR for the same reason. The tests prove *a*
    refusal happened, not which one, and a password mismatch raises the same
    `OperationalError` — so a name travelling with the password by convention
    rather than by contract is one hardening (a randomized password) away from
    three green tests reproducing an auth failure instead of a connection limit.

    The privilege probe duplicates `actor_lacks_createrole`
    (`tests/scripts/conftest.py`). Left as two copies deliberately: hoisting it
    into `tests/conftest.py` means editing the module under test plus a suite
    outside this change, to share one catalog query over stable PostgreSQL
    semantics. Worth doing the next time either copy is touched.
    """
    try:
        conn = maintenance_connection()
    except psycopg2.OperationalError as exc:
        precondition_absent(
            f"no PostgreSQL answered at {settings.DB_HOST}:{settings.DB_PORT} — {exc}"
        )

    with closing(conn):
        require_role_privilege(conn, "rolcreaterole", "CREATEROLE")
        with conn.cursor() as cur:
            name = f"probe804_{SESSION_DB_SUFFIX}_{uuid.uuid4().hex[:8]}"
            cur.execute(
                f'CREATE ROLE "{name}" LOGIN PASSWORD %s CONNECTION LIMIT 0',
                (PROBE_ROLE_PASSWORD,),
            )
            cur.execute(
                "SELECT rolconnlimit, rolsuper FROM pg_roles WHERE rolname = %s",
                (name,),
            )
            assert cur.fetchone() == (0, False), (
                f"{name} is not actually refusable — the fixture would then"
                " reproduce nothing and every test using it would pass down the"
                " happy path"
            )
        try:
            yield name, PROBE_ROLE_PASSWORD
        finally:
            with conn.cursor() as cur:
                cur.execute(f'DROP ROLE IF EXISTS "{name}"')


def _aim_settings_at_the_refused_role(monkeypatch, credential) -> None:
    """Point `server_answered()` at the role the server will refuse.

    The settings singleton is the ONLY seam that leaves every component real:
    `server_answered` and `maintenance_connection` both take no arguments, so
    the alternative is patching the module-global — which is exactly the
    mocking this class exists to escape.
    """
    user, password = credential
    monkeypatch.setattr(settings, "DB_USER", user)
    monkeypatch.setattr(settings, "DB_PASSWORD", password)


def _aim_settings_at_an_empty_address(monkeypatch) -> None:
    """Point `server_answered()` at an address with nothing behind it.

    The host is pinned to the literal rather than left as `localhost`, so the
    port proven free on `127.0.0.1` is the one both libpq and the listener
    probe actually reach — `localhost` may resolve to `::1` first.
    """
    monkeypatch.setattr(settings, "DB_HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "DB_PORT", _free_port())


class TestTheListenerProbe:
    """#769: an ``OperationalError`` does not mean "no server".

    ``too many connections``, ``password authentication failed`` and ``database
    does not exist`` are all the server ANSWERING and refusing. Measured on
    psycopg2 2.9.12 / PostgreSQL 15.18, all of them — and a genuinely dead port
    — carry ``pgcode = None``, so the exception cannot be the discriminator.
    The address can.
    """

    def test_an_occupied_address_is_listening(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]

            assert server_is_listening("127.0.0.1", port) is True

    def test_an_empty_address_is_not(self):
        assert server_is_listening("127.0.0.1", _free_port(), timeout=1) is False

    def test_a_successful_connection_answers_without_consulting_the_probe(
        self, monkeypatch
    ):
        """The happy path keeps its own evidence — a completed login is proof.

        Pinned because the obvious refactor is to probe first and connect
        after, which would ask the address a question the connection already
        answered.
        """
        monkeypatch.setattr("tests.conftest.maintenance_connection", lambda: Mock())
        monkeypatch.setattr(
            "tests.conftest.server_is_listening",
            Mock(side_effect=AssertionError("probe must not run on success")),
        )

        assert server_answered() is True

    def test_a_refusal_from_a_live_server_is_not_an_absent_server(self, monkeypatch):
        """THE BUG. Before #769 this returned False and every integration test
        skipped at rc 0 — a run that reads as a pass having executed none."""
        monkeypatch.setattr(
            "tests.conftest.maintenance_connection",
            Mock(side_effect=psycopg2.OperationalError("FATAL: too many connections")),
        )
        monkeypatch.setattr("tests.conftest.server_is_listening", lambda *a, **k: True)

        assert server_answered() is True

    def test_nothing_listening_is_still_an_honest_skip(self, monkeypatch):
        """The widened fail branch must not swallow the one legitimate skip:
        a contributor with no PostgreSQL at all."""
        monkeypatch.setattr(
            "tests.conftest.maintenance_connection",
            Mock(side_effect=psycopg2.OperationalError("Connection refused")),
        )
        monkeypatch.setattr("tests.conftest.server_is_listening", lambda *a, **k: False)

        assert server_answered() is False

    def test_the_two_refusal_paths_reach_opposite_verdicts(self, monkeypatch):
        """The pair, asserted together — the fix is a DISTINCTION, and either
        half alone passes for a function that always returns one answer."""
        monkeypatch.setattr(
            "tests.conftest.maintenance_connection",
            Mock(side_effect=psycopg2.OperationalError("connection failed")),
        )

        monkeypatch.setattr("tests.conftest.server_is_listening", lambda *a, **k: True)
        listening = integration_verdict(server_answered(), required=False)

        monkeypatch.setattr("tests.conftest.server_is_listening", lambda *a, **k: False)
        silent = integration_verdict(server_answered(), required=False)

        assert (listening, silent) == ("run", "skip")


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

        Raised 10 -> 11 for #1195's live-drift audit, which skips on pull
        requests by design and runs only on the schedule.
        """
        assert MAX_EXPECTED_SKIPS == 11


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


class TestTheRefusalAgainstARealServer:
    """#804: the #801 reproduction, pinned — no mocks, real refusal.

    WHAT THIS COVERS THAT THE MOCKED TESTS CANNOT. `TestTheListenerProbe`
    replaces `server_is_listening` with a lambda in every case that matters, so
    it pins how `server_answered` COMBINES its two inputs and is blind to
    whether either input is still what it was measured to be. The premise the
    whole design rests on — a live server refusing a connection raises
    `OperationalError` while the socket keeps answering — was established by a
    reproduction in a review comment and in one session, and nothing in CI
    re-established it. A `maintenance_connection` that grew a retry, a psycopg2
    that raised a different class, a libpq that failed before the server was
    reached: each restores the #769 ambiguity with every mocked test green.

    THE REFUSAL IS NOT ASSUMED. Each test that needs one proves it happened
    first, because the failure mode here reads as success — `server_answered()`
    also returns True for a connection that SUCCEEDS, so a probe role that
    stopped being refused would leave these green while reproducing nothing.
    """

    @pytest.mark.integration
    def test_a_connection_limit_refusal_is_a_server_that_answered(
        self, a_refusing_role, monkeypatch
    ):
        """THE REPRODUCTION. Measured on PostgreSQL 15.19 / psycopg2 2.9.12:
        `OperationalError`, and the socket still accepting in the same instant.

        Before #769 this exact condition returned False and every integration
        test skipped at `rc 0` — a run that reads as a pass having executed
        none of what it claims to cover. That is #758's false PASS, reproduced
        one layer down inside the probe that exists to prevent it.

        The `pytest.raises` is load-bearing and the only extra claim needed:
        `server_answered()` is True for a connection that SUCCEEDS as well, so
        without it a probe role that stopped being refused stays green. A third
        assertion on `server_is_listening` was dropped as derivable — given a
        raise, True is reachable only through the probe, with the same
        arguments.
        """
        _aim_settings_at_the_refused_role(monkeypatch, a_refusing_role)

        with pytest.raises(psycopg2.OperationalError):
            maintenance_connection()

        assert server_answered() is True

    def test_a_genuinely_absent_listener_is_still_an_honest_skip(self, monkeypatch):
        """The negative direction, also unmocked.

        #804 asks for both sides because the defect was an INABILITY TO
        SEPARATE them, and a test covering only the refusal is satisfied by a
        `server_answered()` that always returns True — the same bug with the
        opposite sign. Deliberately unmarked: it needs no PostgreSQL, only an
        address with nothing behind it, so it guards the negative half even
        where the positive half has to skip.

        The `pytest.raises` here is diagnostic rather than load-bearing —
        `False` is reachable only through the `except`, so it is entailed —
        and is kept because the two ways this can fail need telling apart:
        something answered at an address proven free, versus the verdict being
        wrong. `assert True is False` says neither.
        """
        _aim_settings_at_an_empty_address(monkeypatch)

        with pytest.raises(psycopg2.OperationalError):
            maintenance_connection()

        assert server_answered() is False

    @pytest.mark.integration
    def test_the_two_real_paths_reach_opposite_verdicts(
        self, a_refusing_role, monkeypatch
    ):
        """The pair, asserted together and with real components throughout —
        `test_the_two_refusal_paths_reach_opposite_verdicts` one layer down
        does this against mocks, and this is the same claim about the world.

        Either half alone is satisfied by a constant function. The fix is a
        DISTINCTION, so only the pair states it as one claim.

        DISCLOSED RATHER THAN OVERSOLD: across the three mutants run for #804
        this reddens exactly when the two tests above already do, so it adds no
        mutation-detected coverage on top of them today. It is kept because it
        is the only assertion here whose SUBJECT is the distinction rather than
        one side of it, and because it composes the two through
        `integration_verdict` the way `setup_test_database` actually calls them
        — which neither half does. Delete it knowing that, not instead of it.
        """
        _aim_settings_at_the_refused_role(monkeypatch, a_refusing_role)
        refused = integration_verdict(server_answered(), required=False)

        _aim_settings_at_an_empty_address(monkeypatch)
        silent = integration_verdict(server_answered(), required=False)

        assert (refused, silent) == ("run", "skip")

    @pytest.mark.integration
    def test_the_exception_still_cannot_discriminate(
        self, a_refusing_role, monkeypatch
    ):
        """The premise the listener probe exists for, re-measured rather than
        cited: a real refusal and a real absent server both arrive as
        `OperationalError` carrying `pgcode = None`, so the exception holds
        nothing to branch on and only the address can answer.

        Its own test because it is an assertion about psycopg2, not about this
        repo. If a future psycopg2 sets a code on either side, this goes red and
        the probe could potentially be retired — a review event, not a breakage,
        and the test name is what says so.
        """
        _aim_settings_at_the_refused_role(monkeypatch, a_refusing_role)
        with pytest.raises(psycopg2.OperationalError) as refusal:
            maintenance_connection()

        _aim_settings_at_an_empty_address(monkeypatch)
        with pytest.raises(psycopg2.OperationalError) as absence:
            maintenance_connection()

        assert (refusal.value.pgcode, absence.value.pgcode) == (None, None)
