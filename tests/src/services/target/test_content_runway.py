"""The runway's arithmetic, its latch and its notice (#1478).

The database half — that the count is the planner's own pool, and that a real
mint crosses the line once — is `tests/scripts/test_customer_notice_gate.py`'s
`TestTheRunway*` classes. This file pins the decisions in between, and pins the
runway's `posting` and its cadence to the clock's own expressions in the
migration.
"""

from __future__ import annotations

import re

import pytest

from src.services.target import category_mix, content_runway, outbox, workspaces
from src.services.target.category_mix import Pool
from src.services.target.content_runway import (
    LOW_RUNWAY_DAYS,
    NOTICE_EVENT,
    REARM_EVENT,
    REARM_MARGIN_DAYS,
    days_left,
    latch_action,
    notice_text,
)

#: The newest migration that defines `fn_clock_tick`. An applied file is
#: immutable, so a change to the tick is a new file, and this pin moves to it.
CLOCK_TICK_MIGRATION = "084_clock_revives_singleton_leases.sql"

#: The `plan_slot` leg's one conjunct the runway leaves out: an account is
#: posting whether or not its next slot is due this tick.
DUE_NOW = "a.next_slot_at <= now()"

#: A definition of the tick, in either spelling a migration can use.
DEFINES_TICK = re.compile(r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+fn_clock_tick\s*\(")


def _plan_slot_leg(migration: str) -> str:
    """`fn_clock_tick`'s body in *migration* up to where its `plan_slot` leg
    inserts its `'plan_slot'` jobs, so it ends with the CTE that picks the
    leg's accounts."""
    from scripts.migration_runner import MIGRATIONS_DIR

    ddl = (MIGRATIONS_DIR / migration).read_text()
    head = DEFINES_TICK.search(ddl)
    assert head, f"no fn_clock_tick in {migration}"
    return ddl[head.end() :].split("$$;", 1)[0].split("'plan_slot'", 1)[0]


def _plan_slot_conjuncts(migration: str) -> list[str]:
    """The conjuncts of the WHERE that picks `fn_clock_tick`'s `plan_slot`
    accounts, in order: the leg's last WHERE."""
    where = _plan_slot_leg(migration).rsplit("WHERE", 1)[1].split("ORDER BY", 1)[0]
    return " ".join(where.split()).split(" AND ")


def _plan_slot_columns(migration: str) -> dict[str, str]:
    """The same CTE's select list, alias to expression: the SELECT before that
    WHERE, split at the commas outside parentheses."""
    head = _plan_slot_leg(migration).rsplit("WHERE", 1)[0]
    select = " ".join(head.rsplit("SELECT", 1)[1].split()).split(" FROM ", 1)[0]
    columns = {}
    for item in re.split(r",\s*(?![^()]*\))", select):
        expression, _, alias = item.rpartition(" AS ")
        if expression:
            columns[alias] = expression
    return columns


class TestTheLevels:
    def test_a_week_warns_and_a_day_more_rearms(self):
        assert (LOW_RUNWAY_DAYS, REARM_MARGIN_DAYS) == (7, 1)


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


class TestTheLatch:
    """One mint, one decision on the days left: told once on the way down,
    re-armed only a margin above the line, and the band between the two
    changes nothing."""

    def _act(self, days, *, latched, below_days=7):
        return latch_action(days=days, latched=latched, below_days=below_days)

    @pytest.mark.parametrize(
        "days, latched, expected",
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
    def test_the_decision_table(self, days, latched, expected):
        assert self._act(days, latched=latched) == expected

    def test_the_levels_scale_with_posts_per_day(self):
        """In files at 3 a day, through the days `after_mint` derives: 20 is
        six days, 21 a week, 23 short of the re-arm, 24 the re-arm."""
        assert self._act(days_left(20, 3), latched=False) == NOTICE_EVENT
        assert self._act(days_left(21, 3), latched=False) is None
        assert self._act(days_left(23, 3), latched=True) is None
        assert self._act(days_left(24, 3), latched=True) == REARM_EVENT

    def test_the_rearm_is_a_margin_over_the_warning_level(self):
        """One level: raise the line and the re-arm moves with it, so it can
        never sit at or below the line."""
        rearm = 10 + REARM_MARGIN_DAYS
        assert self._act(rearm - 1, latched=True, below_days=10) is None
        assert self._act(rearm, latched=True, below_days=10) == REARM_EVENT

    def test_a_level_of_zero_never_warns(self):
        assert self._act(days_left(0, 3), latched=False, below_days=0) is None

    @pytest.mark.parametrize("posts_per_day", [0, None, -1])
    @pytest.mark.parametrize("latched", [False, True])
    def test_an_account_that_posts_nothing_owes_nothing(self, posts_per_day, latched):
        assert self._act(days_left(0, posts_per_day), latched=latched) is None


class TestTheNoticeText:
    """The text names the days the one formula gives, as `after_mint` passes
    them, and the arithmetic behind them."""

    def _text(self, eligible, *, label="@a"):
        return notice_text(
            label=label,
            days=days_left(eligible, 3),
            eligible=eligible,
            posts_per_day=3,
        )

    def test_names_the_account_and_the_arithmetic(self):
        text = self._text(20, label="@acme")
        assert "@acme has about 6 days of content left" in text
        assert "(20 files at 3 a day)" in text
        assert "Drive source" in text

    def test_one_day_and_less_than_a_day(self):
        assert "about 1 day of content" in self._text(4)
        assert "less than a day of content" in self._text(2)
        assert "(1 file at 3 a day)" in self._text(1)


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


class TestThePool:
    def test_eligible_counts_only_the_folders_the_draw_can_land_on(self):
        drawn = Pool(
            weights={"auto": 0.5, "explicit": 0.5, "off": 0.0, "empty": 0.0},
            counts={"auto": 4, "explicit": 3, "off": 9, "empty": 0},
        )
        assert drawn.drawable == [("auto", 0.5), ("explicit", 0.5)]
        assert drawn.eligible == 7

    def test_a_mint_leaves_one_file_fewer(self):
        drawn = Pool(weights={"auto": 1.0}, counts={"auto": 4})
        assert drawn.eligible_after_a_mint == 3

    async def test_only_the_folders_a_draw_can_land_on_are_counted(self):
        """An Off folder weighs 0 whatever it holds, so its files are not read.
        With no folder to read the count still runs, bound to none, so the
        planner's statements keep their order."""
        folders = [
            {"source_id": "auto", "ratio": None},
            {"source_id": "explicit", "ratio": 0.5},
            {"source_id": "off", "ratio": 0},
        ]
        executor = _Executor([{"source_id": "auto", "n": 4}])
        drawn = await category_mix.pool(
            executor, workspace_id="ws-1", ig_account_id="a-1", folders=folders
        )
        ((sql, params),) = executor.sent
        assert "AND m.source_id = ANY(CAST(:sources AS uuid[]))" in sql
        assert params["sources"] == ["auto", "explicit"]
        assert drawn.counts == {"auto": 4, "explicit": 0, "off": 0}

        executor = _Executor([])
        await category_mix.pool(
            executor, workspace_id="ws-1", ig_account_id="a-1", folders=folders[2:]
        )
        ((_, params),) = executor.sent
        assert params["sources"] == []


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
    """The runway's `posting` and its cadence are the clock's own, read from
    the migration rather than trusted to a comment: an account the tick would
    not mint a slot for shows no runway, one it would is never hidden, and its
    days are counted at the posts per day its slots are spent at."""

    def test_it_is_the_plan_slot_legs_where_less_the_due_now_conjunct(self):
        leg = _plan_slot_conjuncts(CLOCK_TICK_MIGRATION)
        assert DUE_NOW in leg, "positive control: the leg's WHERE was found"
        posting = " ".join(content_runway._POSTING_SQL.split())
        assert posting.startswith("(") and posting.endswith(")")
        assert posting[1:-1].split(" AND ") == [c for c in leg if c != DUE_NOW]

    def test_the_cadence_is_the_legs_eff_ppd(self):
        """The expression the leg's CTE selects as `eff_ppd`: the posts per day
        `fn_next_slot` advances the account's slot cursor by."""
        columns = _plan_slot_columns(CLOCK_TICK_MIGRATION)
        assert {"eff_tz", "eff_ppd", "eff_start", "eff_end"} <= set(columns), (
            "positive control: the leg's select list was found"
        )
        cadence = " ".join(content_runway._POSTS_PER_DAY_SQL.split())
        assert columns["eff_ppd"] == cadence

    def test_the_pin_reads_the_newest_definition_of_the_tick(self):
        from scripts.migration_runner import MIGRATIONS_DIR, discover_migrations

        defining = [
            migration.path.name
            for migration in discover_migrations(MIGRATIONS_DIR)
            if DEFINES_TICK.search(migration.sql)
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
        ((_, sql, params),) = [entry for entry in c.log if entry[0] == "read"]
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
