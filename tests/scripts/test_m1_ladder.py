"""The owner-derivation ladder (`scripts/m1_ladder.py`, M.1a / #790).

**Why this file has to exist, stated once.** Production exercises rungs 3 and 4
and nothing else: no chat there has an `owner` or an `admin` membership row, so
the first two rungs of a four-rung precedence ladder have **zero** real rows
behind them. `m1_preflight` prints that coverage on every run. A transform
validated only against production would ship two untested rungs into a window
that has no shadow phase.

**Reachability is the weak version of this test and is deliberately not what is
asserted.** A fixture in which only rung 1 is possible proves rung 1 is
*reachable* and says nothing about precedence — and precedence is the half that
breaks, because it is the only part a wrong `ORDER BY` or a mis-ordered `CASE`
can silently invert. So every rung test seeds evidence for its own rung **and
for every rung beneath it**, and asserts the ladder picks the higher one.

Fixtures are seeded into the REAL replayed legacy schema, not a mock: the SQL
under test names `user_chat_memberships` and `chat_settings` columns, and a
stand-in that agreed with my idea of those columns would agree with a wrong idea
just as readily.
"""

from __future__ import annotations

import itertools
import uuid

import psycopg2
import pytest

from scripts.m1_ladder import (
    LADDER_DISAGREEMENT_SQL,
    LADDER_SQL_PERMISSIVE,
    LADDER_SQL_STRICT,
    RUNG_ACTIVE,
    RUNG_ADMIN,
    RUNG_OWNER,
    RUNG_QUARANTINE,
)

pytestmark = [pytest.mark.integration]


def _uuid():
    return str(uuid.uuid4())


_TG_IDS = itertools.count(1)
"""Telegram ids are UNIQUE on legacy.users, so a per-Seeder counter collides as
soon as a test builds two chats. Module-scoped, because the uniqueness is."""


class Seeder:
    """Writes real rows into the real legacy tables."""

    def __init__(self, conn):
        self.conn = conn
        self.chat_id = _uuid()

    def chat(self):
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO chat_settings (id, telegram_chat_id) VALUES (%s, %s)",
                (self.chat_id, -abs(hash(self.chat_id)) % 10**12),
            )
        return self.chat_id

    def member(self, role, joined_at, is_active=True, user_id=None):
        user_id = user_id or _uuid()
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (id, telegram_user_id) VALUES (%s, %s)",
                (user_id, next(_TG_IDS)),
            )
            cur.execute(
                "INSERT INTO user_chat_memberships"
                " (id, user_id, chat_settings_id, instance_role, joined_at, is_active)"
                " VALUES (%s,%s,%s,%s,%s,%s)",
                (_uuid(), user_id, self.chat_id, role, joined_at, is_active),
            )
        return user_id

    def resolve(self, sql=LADDER_SQL_STRICT):
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT owner_user_id, rung FROM ({sql}) l"
                " WHERE chat_settings_id = %s",
                (self.chat_id,),
            )
            return cur.fetchone()


@pytest.fixture()
def seeder(replayed_db):
    conn = psycopg2.connect(replayed_db)
    conn.autocommit = True
    s = Seeder(conn)
    s.chat()
    yield s
    conn.close()


class TestEachRungIsReachedAndTakesPrecedence:
    """One test per rung. Each seeds its rung PLUS every rung below it."""

    def test_rung1_owner_wins_over_admin_and_active(self, seeder):
        seeder.member("member", "2026-01-01", is_active=True)
        seeder.member("admin", "2026-01-02")
        expected = seeder.member("owner", "2026-01-03")

        owner, rung = seeder.resolve()

        assert rung == RUNG_OWNER
        assert owner == expected, (
            "the owner row must win even though it joined LAST — precedence is"
            " by rung first and joined_at only within a rung"
        )

    def test_rung2_admin_wins_over_active_when_no_owner_row_exists(self, seeder):
        seeder.member("member", "2026-01-01", is_active=True)
        expected = seeder.member("admin", "2026-01-02")

        owner, rung = seeder.resolve()

        assert rung == RUNG_ADMIN
        assert owner == expected

    def test_rung3_earliest_active_when_no_owner_and_no_admin(self, seeder):
        expected = seeder.member("member", "2026-01-01", is_active=True)
        seeder.member("member", "2026-01-02", is_active=True)

        owner, rung = seeder.resolve()

        assert rung == RUNG_ACTIVE
        assert owner == expected

    def test_rung4_quarantine_when_the_chat_has_no_memberships_at_all(self, seeder):
        owner, rung = seeder.resolve()

        assert rung == RUNG_QUARANTINE
        assert owner is None

    def test_rung4_quarantine_when_every_membership_is_inactive(self, seeder):
        seeder.member("member", "2026-01-01", is_active=False)
        seeder.member("member", "2026-01-02", is_active=False)

        owner, rung = seeder.resolve()

        assert rung == RUNG_QUARANTINE, (
            "an inactive-only chat resolves nowhere under the strict reading —"
            " this is the case the two readings are ABOUT"
        )


class TestTieBreaksWithinARung:
    """`04` L80 says 'earliest joined_at if several' for rung 1 and rung 2.

    Both need two rows in the same rung or the tie-break is never evaluated.
    """

    def test_rung1_ties_break_on_earliest_joined_at(self, seeder):
        expected = seeder.member("owner", "2026-01-01")
        seeder.member("owner", "2026-01-02")

        owner, rung = seeder.resolve()

        assert (owner, rung) == (expected, RUNG_OWNER)

    def test_rung2_ties_break_on_earliest_joined_at(self, seeder):
        expected = seeder.member("admin", "2026-01-01")
        seeder.member("admin", "2026-01-02")

        owner, rung = seeder.resolve()

        assert (owner, rung) == (expected, RUNG_ADMIN)

    def test_an_exact_joined_at_tie_breaks_on_user_id_not_on_insertion_order(
        self, seeder
    ):
        """`joined_at` carries no uniqueness constraint, so a genuine tie is
        expressible. Without the trailing `user_id` sort key the winner is
        whatever the plan happens to emit — and a transform that picks a
        different owner on a re-run is not re-runnable, which is exactly what
        the M.2 rehearsal repeats.

        THE INSERTION ORDER IS INVERTED ON PURPOSE, and that is the whole test.
        An earlier version seeded two tied rows in arbitrary order, asserted the
        smaller `user_id` won, and then re-resolved five times asserting
        stability. **Mutation testing killed it**: dropping `user_id` from the
        `ORDER BY` left all twelve tests green. Both halves were hollow —
        repetition cannot detect non-determinism when the plan is stable, and
        the min() assertion passes half the time by luck, on whichever run you
        happen to look at.

        Seeding the LARGER id first makes the two orderings disagree by
        construction: a plan returning heap order yields the larger, and only an
        explicit ascending `user_id` sort yields the smaller."""
        same = "2026-01-01 00:00:00"
        smaller, larger = sorted([_uuid(), _uuid()])
        seeder.member("owner", same, user_id=larger)
        seeder.member("owner", same, user_id=smaller)

        winner, rung = seeder.resolve()

        assert rung == RUNG_OWNER
        assert winner == smaller, (
            "the tie must break on ascending user_id, not on the order the rows"
            " were written — the larger id was inserted FIRST so heap order and"
            " sort order disagree"
        )


class TestTheTwoReadingsOfTheActivityQualifier:
    """`m1_ladder` computes both readings rather than picking one. These tests
    pin what each one DOES, so a ruling collapses them by deletion rather than
    by rewriting behaviour someone has to re-derive."""

    def test_an_inactive_owner_row_is_ignored_by_strict_and_taken_by_permissive(
        self, seeder
    ):
        inactive_owner = seeder.member("owner", "2026-01-01", is_active=False)
        active_member = seeder.member("member", "2026-01-02", is_active=True)

        assert seeder.resolve(LADDER_SQL_STRICT) == (active_member, RUNG_ACTIVE)
        assert seeder.resolve(LADDER_SQL_PERMISSIVE) == (inactive_owner, RUNG_OWNER)

    def test_the_disagreement_query_names_exactly_that_chat(self, seeder):
        seeder.member("owner", "2026-01-01", is_active=False)
        seeder.member("member", "2026-01-02", is_active=True)

        with seeder.conn.cursor() as cur:
            cur.execute(f"SELECT chat_settings_id FROM ({LADDER_DISAGREEMENT_SQL}) d")
            flagged = [r[0] for r in cur.fetchall()]

        assert str(seeder.chat_id) in [str(f) for f in flagged]

    def test_the_disagreement_query_is_silent_when_the_readings_agree(self, seeder):
        """The positive control for the test above. A disagreement detector that
        fired on everything would pass that test while being useless."""
        seeder.member("owner", "2026-01-01", is_active=True)
        seeder.member("member", "2026-01-02", is_active=True)

        with seeder.conn.cursor() as cur:
            cur.execute(f"SELECT chat_settings_id FROM ({LADDER_DISAGREEMENT_SQL}) d")
            flagged = [str(r[0]) for r in cur.fetchall()]

        assert str(seeder.chat_id) not in flagged


class TestChatsAreResolvedIndependently:
    def test_one_chats_owner_never_leaks_into_another(self, replayed_db):
        """The LATERAL join correlates on chat_settings_id. Drop that predicate
        and every chat resolves to the globally-earliest membership — which
        still returns a plausible owner for every chat, so nothing else here
        would catch it."""
        conn = psycopg2.connect(replayed_db)
        conn.autocommit = True
        try:
            a, b = Seeder(conn), Seeder(conn)
            a.chat()
            b.chat()
            a_owner = a.member("owner", "2026-01-01")
            b_owner = b.member("owner", "2026-02-01")

            assert a.resolve() == (a_owner, RUNG_OWNER)
            assert b.resolve() == (b_owner, RUNG_OWNER)
            assert a_owner != b_owner
        finally:
            conn.close()
