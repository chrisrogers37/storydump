"""The M.1 pre-window profile battery (`scripts/m1_preflight.py`, #790).

The preflight's whole job is to answer "can the transform run?" **before** the
window, so the tests that matter are the ones proving it can say *no* — and,
just as much, that it can say *yes*. A gate that halts unconditionally passes
every halt test ever written and is worthless, so the empty-corpus CLEAR case
below is a positive control, not a formality.

The other axis is the one that is easy to leave untested because nothing fails
when it breaks: a check whose SQL is wrong records an ERROR, an unclaimed table
records nothing at all, and both of those render as "not a blocker". So
`ERROR`, `INCOMPLETE` and `skipped-absent` each get a test asserting they are
*distinguishable from a pass*, which is the property the exit codes exist for.
"""

from __future__ import annotations

import json

import psycopg2
import pytest

from scripts import m1_preflight as pf

pytestmark = [pytest.mark.integration]


def _count(report, key):
    for r in report.results:
        if f"{r.check.table}.{r.check.name}" == key:
            return r.value
    raise AssertionError(f"no check named {key}")


@pytest.fixture()
def conn(replayed_db):
    """A real, replayed, EMPTY legacy schema. Every count is genuinely 0."""
    c = psycopg2.connect(replayed_db)
    c.autocommit = True
    yield c
    c.close()


class TestTheRegistryIsWellFormed:
    def test_every_check_declares_a_kind_the_renderer_understands(self):
        assert {c.kind for c in pf.CHECKS} <= {pf.BLOCKER, pf.COUNT, pf.DISCLOSURE}

    def test_every_check_cites_the_spec_section_it_comes_from(self):
        """The battery is §5.1's, not a second enumeration of the same
        predicates. A check with no section is one nobody ratified, and it is
        how the two lists start to drift."""
        uncited = [f"{c.table}.{c.name}" for c in pf.CHECKS if not c.spec.strip()]
        assert uncited == []

    def test_every_blocker_states_a_remedy(self):
        """A blocker that halts the window without saying what to do about it
        turns a gate into an obstacle."""
        silent = [
            f"{c.table}.{c.name}"
            for c in pf.CHECKS
            if c.kind == pf.BLOCKER and not c.remedy.strip()
        ]
        assert silent == []

    def test_check_names_are_unique_within_a_table(self):
        seen = [(c.table, c.name) for c in pf.CHECKS]
        assert len(seen) == len(set(seen))


class TestItRunsAgainstARealLegacySchema:
    def test_every_check_executes_no_check_silently_errors(self, conn):
        """A typo in a check's SQL does not fail loudly — it records an ERROR,
        and an errored check contributes no blocker. Without this test a broken
        check reads exactly like a clean one."""
        report = pf.run(conn)

        errored = {f"{r.check.table}.{r.check.name}": r.error for r in report.errored}
        assert errored == {}, f"checks failed to execute: {errored}"

    def test_an_empty_legacy_corpus_is_CLEAR(self, conn):
        """THE POSITIVE CONTROL. Every halt test below is only evidence because
        this one passes: a preflight that always halted would satisfy them all."""
        report = pf.run(conn)

        assert report.blockers == [], [
            f"{r.check.table}.{r.check.name}={r.value}" for r in report.blockers
        ]
        assert report.exit_code() == pf.CLEAR

    def test_the_replayed_corpus_claims_every_table_it_contains(self, conn):
        report = pf.run(conn)
        assert report.unclaimed_tables == []


class TestABlockerHalts:
    def test_a_chat_with_no_membership_halts_on_the_quarantine_blocker(self, conn):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO chat_settings (telegram_chat_id, display_name)"
                " VALUES (-991, 'named')"
            )

        report = pf.run(conn)

        names = {f"{r.check.table}.{r.check.name}" for r in report.blockers}
        assert "user_chat_memberships.rung4_quarantine_feed" in names
        assert report.exit_code() == pf.HALT

    def test_a_null_display_name_is_COUNTED_not_blocked(self, conn):
        """SD-12 rules the default (`COALESCE(display_name, 'Workspace ' ||
        left(id::text,8))` + a G-LOG row), so a NULL name is handled, not fatal.

        This test exists because an earlier version of the battery blocked on
        it — reporting a spec-answered case as a hard stop. The count still has
        to move, or the check has stopped measuring anything."""
        before = _count(pf.run(conn), "chat_settings.name_defaulted_by_sd12")

        with conn.cursor() as cur:
            cur.execute("INSERT INTO chat_settings (telegram_chat_id) VALUES (-992)")

        report = pf.run(conn)
        assert _count(report, "chat_settings.name_defaulted_by_sd12") == before + 1
        assert "chat_settings.name_defaulted_by_sd12" not in {
            f"{r.check.table}.{r.check.name}" for r in report.blockers
        }

    def test_a_live_onboarding_session_blocks_but_an_expired_one_does_not(self, conn):
        """§4.10's drop-check counts NON-EXPIRED sessions, not rows. The
        expired half is the control, and it is the half that matters: reading
        this as a raw row count turned a passing gate into a reported owner
        decision."""
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (id, telegram_user_id) VALUES"
                " ('33333333-3333-3333-3333-333333333333', 90001)"
            )
            cur.execute(
                "INSERT INTO onboarding_sessions (user_id, expires_at) VALUES"
                " ('33333333-3333-3333-3333-333333333333',"
                "  (now() AT TIME ZONE 'UTC') - interval '1 day')"
            )
        expired_only = {
            f"{r.check.table}.{r.check.name}" for r in pf.run(conn).blockers
        }
        assert "onboarding_sessions.live_sessions_blocking_the_drop" not in expired_only

        with conn.cursor() as cur:
            cur.execute(
                "UPDATE onboarding_sessions SET expires_at ="
                " (now() AT TIME ZONE 'UTC') + interval '1 day'"
            )
        now_live = {f"{r.check.table}.{r.check.name}" for r in pf.run(conn).blockers}
        assert "onboarding_sessions.live_sessions_blocking_the_drop" in now_live


class TestUnreachableIsNotTheSameAsEmpty:
    """The three ways this instrument can fail to answer, each asserted to be
    distinguishable from CLEAR. Collapsing any of them into a pass fails toward
    'everything is fine', which is the direction nobody audits."""

    def test_an_unclaimed_table_yields_INCOMPLETE_not_CLEAR(self, conn):
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE a_table_nobody_dispositioned (id int)")

        report = pf.run(conn)

        assert "a_table_nobody_dispositioned" in report.unclaimed_tables
        assert report.blockers == [], "no blocker fired — so only coverage catches it"
        assert report.exit_code() == pf.INCOMPLETE

    def test_a_missing_required_table_yields_ERROR_not_CLEAR(self, conn):
        with conn.cursor() as cur:
            cur.execute("DROP TABLE audit_log CASCADE")

        report = pf.run(conn)

        assert report.errored, "dropping a checked table must be visible"
        assert report.exit_code() == pf.ERROR
        assert report.exit_code() != pf.CLEAR

    def test_an_absent_optional_table_is_skipped_and_disclosed(self, conn):
        """`posting_history_dedup_archive` is genuinely absent from a replayed
        corpus and from any PITR branch cut before 2026-08-19. That is a real
        state, not a broken instrument — so it must NOT error, and it must NOT
        pass silently either."""
        report = pf.run(conn)

        skipped = {r.check.table for r in report.absent}
        assert "posting_history_dedup_archive" in skipped
        assert not any(
            r.check.table == "posting_history_dedup_archive" for r in report.errored
        )
        assert "Skipped is not passed." in pf.render(report)

    def test_an_unevaluable_fc8_census_is_ERROR_not_CLEAR(self, conn):
        """FC-8's two counts are a Phase M precondition. If the census cannot
        run, the honest answer is "could not answer" — but the error used to be
        recorded into `report.fc8` ONLY, where it renders as a line of text and
        contributes to no verdict, so the run could still exit CLEAR.

        Dropping `media_items` is how the census becomes unevaluable, and it is
        also what several other checks read — which is the point: EVERY path
        that could not answer has to reach the exit code, not just the ones
        that happen to be in the registry."""
        with conn.cursor() as cur:
            cur.execute("DROP TABLE media_items CASCADE")

        report = pf.run(conn)

        assert "error" in report.fc8
        assert any(r.check.table == "(fc8_gate)" for r in report.errored), (
            "the census failure must land in `errored`, or it contributes to no verdict"
        )
        assert report.exit_code() == pf.ERROR
        assert report.exit_code() != pf.CLEAR

    def test_an_unevaluable_rung_query_is_ERROR_not_CLEAR(self, conn):
        """Same shape, other reporting-only field. `report.rungs` is printed
        for coverage honesty; a failure to compute it must not read as a corpus
        that exercises no rungs."""
        with conn.cursor() as cur:
            cur.execute("DROP TABLE user_chat_memberships CASCADE")

        report = pf.run(conn)

        assert any(r.check.table == "(ladder)" for r in report.errored)
        assert report.exit_code() == pf.ERROR

    def test_an_absent_optional_table_still_blocks_when_it_IS_present(self, conn):
        """The other half. Absence is tolerated; presence is not, because the
        disposition gap (#941) is real wherever the table is."""
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE posting_history_dedup_archive (id int)")
            cur.execute("INSERT INTO posting_history_dedup_archive VALUES (1)")

        report = pf.run(conn)

        names = {f"{r.check.table}.{r.check.name}" for r in report.blockers}
        assert "posting_history_dedup_archive.undispositioned_rows" in names


class TestTheReportTellsTheTruthAboutItself:
    def test_rung_coverage_names_the_rungs_the_corpus_does_not_exercise(self, conn):
        """The reason this is printed at all: production exercises rungs 3 and 4
        only, and a reader must not take 'the ladder is tested' for 'the ladder
        is tested on real data'."""
        with conn.cursor() as cur:
            cur.execute("INSERT INTO chat_settings (telegram_chat_id) VALUES (-993)")

        text = pf.render(pf.run(conn))

        assert "NOT EXERCISED by this corpus" in text
        assert "rung1-owner-row" in text

    def test_the_coverage_line_states_how_many_checks_ran(self, conn):
        text = pf.render(pf.run(conn))
        assert "COVERAGE" in text
        assert f"of {len(pf.CHECKS)} checks ran" in text

    def test_json_and_text_agree_on_the_verdict(self, conn):
        report = pf.run(conn)
        assert pf.to_json(report)["verdict"] in pf.render(report)

    def test_jsonable_coerces_every_type_json_would_refuse(self):
        """Unit-level because no disclosure returns a date today. That is
        exactly why it is here — the Decimal case shipped because the only
        test that could have caught it ran on a corpus that could not produce
        the value."""
        import datetime as dt
        import decimal as dec

        assert pf._jsonable(dec.Decimal("1.5")) == 1.5
        assert pf._jsonable(dt.date(2026, 8, 20)) == "2026-08-20"
        assert pf._jsonable(dt.datetime(2026, 8, 20, 1, 2, 3)).startswith("2026-08-20T")
        assert pf._jsonable(7) == 7
        assert pf._jsonable("x") == "x"
        assert pf._jsonable(None) is None
        json.dumps(
            [pf._jsonable(dec.Decimal("1.5")), pf._jsonable(dt.date(2026, 8, 20))]
        )

    def test_json_survives_a_disclosure_carrying_a_numeric(self, conn):
        """`--json` crashed on production while `--text` rendered fine: the
        `posting_queue.by_status` average comes back as a `Decimal`, which the
        JSON encoder refuses and the text renderer `str()`s without noticing.

        The pre-existing agreement test could not catch it — it runs on an
        EMPTY corpus, where no aggregate produces a numeric at all. So this one
        SEEDS a queue row, which is the only way the offending value exists.
        Serialising is the assertion; there is nothing else to check."""
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO media_items (id, file_path, file_name, file_size,"
                " file_hash, source_type) VALUES"
                " ('44444444-4444-4444-4444-444444444444','/p','n',1,'h','google_drive')"
            )
            cur.execute(
                "INSERT INTO posting_queue (media_item_id, scheduled_for, status)"
                " VALUES ('44444444-4444-4444-4444-444444444444',"
                "         (now() AT TIME ZONE 'UTC'), 'pending')"
            )

        report = pf.run(conn)
        rows = pf.to_json(report)["disclosures"]["posting_queue.by_status"]
        assert rows, "the seeded row must reach the disclosure, or this proves nothing"

        json.dumps(pf.to_json(report))
