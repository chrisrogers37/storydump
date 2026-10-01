"""The runway's arithmetic, its latch and its notice (#1478).

The database half — that the count is the planner's own pool, and that a real
mint crosses the line once — is `tests/scripts/test_customer_notice_gate.py`'s
`TestTheRunway*` classes. This file pins the decisions in between, and pins the
runway's `posting` to the clock's own predicate in the migration.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.services.target import content_runway, outbox, workspaces
from src.services.target.category_mix import Pool
from src.services.target.content_runway import (
    LOW_RUNWAY_DAYS,
    NOTICE_EVENT,
    REARM_EVENT,
    REARM_RUNWAY_DAYS,
    days_left,
    is_below,
    latch_action,
    notice_text,
)

MIGRATIONS = Path(__file__).resolve().parents[4] / "scripts" / "migrations"

#: The newest migration that defines `fn_clock_tick`. An applied file is
#: immutable, so a change to the tick is a new file, and this pin moves to it.
CLOCK_TICK_MIGRATION = "084_clock_revives_singleton_leases.sql"

#: The `plan_slot` leg's one conjunct the runway leaves out: an account is
#: posting whether or not its next slot is due this tick.
DUE_NOW = "a.next_slot_at <= now()"

#: A definition of the tick, in either spelling a migration can use.
DEFINES_TICK = re.compile(r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+fn_clock_tick\s*\(")


def _plan_slot_conjuncts(migration: str) -> list[str]:
    """The conjuncts of the WHERE that picks `fn_clock_tick`'s `plan_slot`
    accounts, in order: the last WHERE before the leg inserts its
    `'plan_slot'` jobs."""
    ddl = (MIGRATIONS / migration).read_text()
    head = DEFINES_TICK.search(ddl)
    assert head, f"no fn_clock_tick in {migration}"
    body = ddl[head.end() :].split("$$;", 1)[0]
    leg = body.split("'plan_slot'", 1)[0]
    where = leg.rsplit("WHERE", 1)[1].split("ORDER BY", 1)[0]
    return " ".join(where.split()).split(" AND ")


class TestTheLevels:
    def test_a_week_warns_and_a_day_more_rearms(self):
        assert (LOW_RUNWAY_DAYS, REARM_RUNWAY_DAYS) == (7, 8)


class TestDaysLeft:
    def test_eligible_files_over_posts_per_day_in_whole_days(self):
        """A part of a day is never counted as one: 20 files at 3 a day is
        six days, not seven."""
        assert days_left(20, 3) == 6
        assert isinstance(days_left(20, 3), int)
        assert days_left(21, 3) == 7
        assert days_left(2, 3) == 0
        assert days_left(0, 3) == 0

    @pytest.mark.parametrize("posts_per_day", [0, None, -1])
    def test_an_account_that_posts_nothing_has_no_runway(self, posts_per_day):
        assert days_left(20, posts_per_day) is None


class TestIsBelow:
    def test_counted_in_files_with_the_line_itself_not_below(self):
        assert is_below(20, 3, 7) is True  # 20 < 21
        assert is_below(21, 3, 7) is False  # exactly a week is not below it

    @pytest.mark.parametrize("posts_per_day", [0, None])
    def test_never_below_for_an_account_that_posts_nothing(self, posts_per_day):
        assert is_below(0, posts_per_day, 7) is False

    def test_zero_days_never_warns(self):
        assert is_below(0, 3, 0) is False


class TestTheLatch:
    """One mint, one decision: told once on the way down, re-armed only at the
    higher level, and the band between the two changes nothing."""

    def _act(self, eligible, *, latched, posts_per_day=1):
        return latch_action(
            eligible=eligible,
            posts_per_day=posts_per_day,
            latched=latched,
            below_days=7,
            rearm_days=8,
        )

    @pytest.mark.parametrize(
        "eligible, latched, expected",
        [
            (6, False, NOTICE_EVENT),  # crossed: tell
            (0, False, NOTICE_EVENT),  # crossed all the way: still one notice
            (6, True, None),  # already told for this crossing
            (7, False, None),  # exactly a week left is not below it
            (7, True, None),  # the band: not below, not yet re-armed
            (8, True, REARM_EVENT),  # climbed back: re-arm
            (50, True, REARM_EVENT),
            (8, False, None),  # armed and full: nothing to write
        ],
    )
    def test_the_decision_table(self, eligible, latched, expected):
        assert self._act(eligible, latched=latched) == expected

    def test_the_levels_scale_with_posts_per_day(self):
        assert self._act(20, latched=False, posts_per_day=3) == NOTICE_EVENT
        assert self._act(21, latched=False, posts_per_day=3) is None
        assert self._act(23, latched=True, posts_per_day=3) is None
        assert self._act(24, latched=True, posts_per_day=3) == REARM_EVENT

    @pytest.mark.parametrize("posts_per_day", [0, None])
    @pytest.mark.parametrize("latched", [False, True])
    def test_an_account_that_posts_nothing_owes_nothing(self, posts_per_day, latched):
        assert self._act(0, latched=latched, posts_per_day=posts_per_day) is None


class TestTheNoticeText:
    def test_names_the_account_and_the_arithmetic(self):
        text = notice_text(label="@acme", eligible=20, posts_per_day=3)
        assert "@acme has about 6 days of content left" in text
        assert "(20 files at 3 a day)" in text
        assert "Drive source" in text

    def test_one_day_and_less_than_a_day(self):
        assert "about 1 day of content" in notice_text(
            label="@a", eligible=4, posts_per_day=3
        )
        assert "less than a day of content" in notice_text(
            label="@a", eligible=2, posts_per_day=3
        )
        assert "(1 file at 3 a day)" in notice_text(
            label="@a", eligible=1, posts_per_day=3
        )


class TestThePool:
    def test_eligible_counts_only_the_folders_the_draw_can_land_on(self):
        drawn = Pool(
            weights={"auto": 0.5, "explicit": 0.5, "off": 0.0, "empty": 0.0},
            counts={"auto": 4, "explicit": 3, "off": 9, "empty": 0},
        )
        assert drawn.drawable == [("auto", 0.5), ("explicit", 0.5)]
        assert drawn.eligible == 7


class _Executor:
    """A scripted executor: each statement is answered in turn from *answers*,
    and recorded with its parameters."""

    def __init__(self, *answers):
        self.answers, self.sent = list(answers), []

    async def execute(self, statement, params=None):
        self.sent.append((str(statement), params))
        rows = self.answers.pop(0) if self.answers else []

        class _Result:
            def mappings(self_inner):
                return iter(rows)

        return _Result()


class TestTheRunway:
    async def test_the_folders_are_read_once_for_every_account(self):
        executor = _Executor(
            [
                {
                    "id": "a-1",
                    "handle": "one",
                    "display_name": None,
                    "state": "active",
                    "posts_per_day": 3,
                    "posting": True,
                },
                {
                    "id": "a-2",
                    "handle": "two",
                    "display_name": None,
                    "state": "active",
                    "posts_per_day": 2,
                    "posting": False,
                },
            ],
            [{"source_id": "s-1", "ratio": None}],
            [{"source_id": "s-1", "n": 20}],
            [{"source_id": "s-1", "n": 5}],
        )
        out = await content_runway.runway(executor, workspace_id="ws-1", below_days=7)
        statements = [sql for sql, _ in executor.sent]
        assert len(statements) == 4, (
            "the accounts, the folders once, then one count each"
        )
        assert sum("FROM media_sources" in sql for sql in statements) == 1
        assert workspaces.LISTED_ACCOUNT_SQL in statements[0]
        assert workspaces.LISTED_ACCOUNT_ORDER_SQL in statements[0]
        assert [params["acct"] for _, params in executor.sent[2:]] == ["a-1", "a-2"]
        assert out["below_days"] == 7
        assert [
            (a["id"], a["eligible"], a["days_left"], a["low"]) for a in out["accounts"]
        ] == [("a-1", 20, 6, True), ("a-2", 5, None, False)]


class TestThePostingPredicate:
    """The runway's `posting` is the clock's own predicate, read from the
    migration rather than trusted to a comment: an account the tick would not
    mint a slot for shows no runway, and one it would is never hidden."""

    def test_it_is_the_plan_slot_legs_where_less_the_due_now_conjunct(self):
        leg = _plan_slot_conjuncts(CLOCK_TICK_MIGRATION)
        assert DUE_NOW in leg, "positive control: the leg's WHERE was found"
        posting = " ".join(content_runway._POSTING_SQL.split())
        assert posting.startswith("(") and posting.endswith(")")
        assert posting[1:-1].split(" AND ") == [c for c in leg if c != DUE_NOW]

    def test_the_pin_reads_the_newest_definition_of_the_tick(self):
        defining = [
            path.name
            for path in sorted(MIGRATIONS.glob("*.sql"))
            if DEFINES_TICK.search(path.read_text())
        ]
        assert defining[-1] == CLOCK_TICK_MIGRATION, defining


class _Session:
    """`after_mint`'s session. Its reads and writes go through the patched
    collaborators; the one statement it sends itself is the account's lock,
    recorded in *log* beside the read so the two can be ordered."""

    def __init__(self, log):
        self.log = log

    async def execute(self, statement, params=None):
        self.log.append(("execute", str(statement), params))


class _Collaborators:
    """`after_mint`'s session and its four collaborators, recorded: the read,
    the binding lookup, the latch row and the fan-out."""

    def __init__(self, monkeypatch, *, account, bindings=("b-1",)):
        self.rows = []
        self.sent = []
        self.log = []
        self.session = _Session(self.log)

        async def row(executor, sql, **params):
            self.read = (sql, params)
            self.log.append(("read", sql, params))
            return account

        async def push_bindings(session, workspace_id):
            return list(bindings)

        async def record(executor, **kwargs):
            self.rows.append(kwargs)

        async def fanout_notification(session, *, workspace_id, bindings, text):
            self.sent.append((list(bindings), text))

        monkeypatch.setattr(content_runway.readers, "row", row)
        monkeypatch.setattr(content_runway.prompts, "push_bindings", push_bindings)
        monkeypatch.setattr(content_runway.audit, "record", record)
        monkeypatch.setattr(
            content_runway.outbox, "fanout_notification", fanout_notification
        )


async def _after_mint(c, eligible):
    return await content_runway.after_mint(
        c.session,
        workspace_id="ws-1",
        ig_account_id="acct-1",
        eligible=eligible,
        below_days=7,
        rearm_days=8,
    )


class TestAfterMint:
    async def test_a_crossing_tells_every_binding_once_and_latches(self, monkeypatch):
        c = _Collaborators(
            monkeypatch,
            account={"handle": "acme", "posts_per_day": 3, "latch": None},
            bindings=("b-1", "b-2"),
        )
        assert await _after_mint(c, 20) == 2
        assert [r["detail"]["event"] for r in c.rows] == [NOTICE_EVENT]
        assert c.rows[0]["entity_kind"] == "ig_account"
        assert c.rows[0]["acct"] == "acct-1"
        assert c.rows[0]["detail"] == {
            "v": 1,
            "event": NOTICE_EVENT,
            "eligible": 20,
            "posts_per_day": 3,
            "below_days": 7,
            "told": 2,
        }
        assert len(c.sent) == 1 and c.sent[0][0] == ["b-1", "b-2"]
        assert "@acme has about 6 days of content left" in c.sent[0][1]

    async def test_a_latched_account_is_not_told_again(self, monkeypatch):
        c = _Collaborators(
            monkeypatch,
            account={"handle": "acme", "posts_per_day": 3, "latch": NOTICE_EVENT},
        )
        assert await _after_mint(c, 5) is None
        assert c.rows == [] and c.sent == []

    async def test_climbing_back_rearms_without_a_message(self, monkeypatch):
        c = _Collaborators(
            monkeypatch,
            account={"handle": "acme", "posts_per_day": 3, "latch": NOTICE_EVENT},
        )
        assert await _after_mint(c, 24) is None
        assert [r["detail"]["event"] for r in c.rows] == [REARM_EVENT]
        assert c.rows[0]["detail"]["rearm_days"] == 8
        assert c.sent == []

    async def test_a_rearmed_account_is_told_again(self, monkeypatch):
        c = _Collaborators(
            monkeypatch,
            account={"handle": "acme", "posts_per_day": 3, "latch": REARM_EVENT},
        )
        assert await _after_mint(c, 20) == 1
        assert [r["detail"]["event"] for r in c.rows] == [NOTICE_EVENT]

    async def test_no_binding_is_undeliverable_and_still_latches(self, monkeypatch):
        c = _Collaborators(
            monkeypatch,
            account={"handle": "acme", "posts_per_day": 3, "latch": None},
            bindings=(),
        )
        assert await _after_mint(c, 20) == outbox.UNDELIVERABLE
        assert [r["detail"]["event"] for r in c.rows] == [NOTICE_EVENT]
        assert c.rows[0]["detail"]["told"] == 0
        assert c.sent == []

    async def test_a_full_library_owes_nothing(self, monkeypatch):
        c = _Collaborators(
            monkeypatch,
            account={"handle": "acme", "posts_per_day": 3, "latch": None},
        )
        assert await _after_mint(c, 21) is None
        assert c.rows == [] and c.sent == []

    async def test_an_account_it_cannot_read_owes_nothing(self, monkeypatch):
        c = _Collaborators(monkeypatch, account=None)
        assert await _after_mint(c, 0) is None
        assert c.rows == [] and c.sent == []

    async def test_the_latch_read_is_scoped_to_the_account_and_its_tenant(
        self, monkeypatch
    ):
        c = _Collaborators(
            monkeypatch,
            account={"handle": "acme", "posts_per_day": 3, "latch": None},
        )
        await _after_mint(c, 30)
        sql, params = c.read
        assert (
            "a.workspace_id = :ws" in sql and "e.workspace_id = a.workspace_id" in sql
        )
        assert "e.entity_kind = 'ig_account' AND e.entity_id = a.id" in sql
        assert "ORDER BY e.id DESC LIMIT 1" in sql
        assert params["told"] == NOTICE_EVENT and params["rearmed"] == REARM_EVENT

    async def test_the_latch_is_read_under_the_accounts_own_lock(self, monkeypatch):
        """One decision per account at a time: the lock is its own statement,
        keyed on the account, and taken before the latch is read."""
        c = _Collaborators(
            monkeypatch,
            account={"handle": "acme", "posts_per_day": 3, "latch": None},
        )
        await _after_mint(c, 30)
        assert [entry[0] for entry in c.log] == ["execute", "read"]
        _, sql, params = c.log[0]
        assert "pg_advisory_xact_lock(hashtextextended(:key, 0))" in sql
        assert params == {"key": "runway:acct-1"}
