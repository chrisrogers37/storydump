"""#1090 D3/D4 gate — the two customer-notification producers, OBSERVED.

*"D3. When selection finds no media, you are told once — not silently nothing."*
*"D4. A post needing attention notifies you within 24h with a link to it."*

Both lines read **NO (MEASURED)** on the checklist because neither had a
producer. `06` §5 specifies both and `05` sets both windows at 24 h; the DDL for
D4's half has shipped since 059 (`fn_reconciler_sweep` tags rows
`notify_window`) and nothing consumed the tag.

WHY THIS GATE IS A DATABASE GATE AND NOT A UNIT TEST. The checklist's bar is
that a line moves to YES only when the behaviour is observed, and production
cannot demonstrate either of these: it holds 0 workspaces and 0 media sources,
and `ig_accounts WHERE state = 'active'` returns nothing, so no slot is ever
planned and no intent is ever parked. A real schema with real rows is the only
place the producers can be watched running. `lane_db` applies the REAL migration
corpus (`run_lane` → `apply_pending(MIGRATIONS_DIR)`), so 066's column is here
because the migration put it here, not because a fixture declared it.

EVERY NEGATIVE CARRIES A POSITIVE CONTROL. A dedup test that never sees the
notice fire cannot tell "deduped correctly" from "never produced anything",
which is the exact state both lines were already in.
"""

from __future__ import annotations

import itertools
import json
import uuid
from datetime import datetime, timezone

import psycopg2
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from tests.scripts.conftest import (
    _scratch,
    actor_lacks_createrole,
    as_user,
    async_url,
    in_tenant,
    run_bootstrap,
    seed_workspace_chain,
    set_test_passwords,
)
from tests.scripts.test_lineage_lane import run_lane

pytestmark = [pytest.mark.integration]

NO_MEDIA_WINDOW_S = 24 * 3600  # 05: "no media available" notice dedup 24 h
NOTIFY_AFTER_S = 24 * 3600  # 05: customer notification per 06 section 5 after 24 h


_SLOT = itertools.count()


def _unique_slot() -> datetime:
    """A slot no other intent in this run holds.

    `uq_intent_slot` is (workspace, account, `schedule_slot_at`) and
    `seed_intent_chain` births its intent at `now()`, so two rows minted in the
    same test are one transaction-clock tick away from colliding. A counter
    removes the coincidence rather than relying on it.

    A `datetime`, never an ISO string: asyncpg binds `timestamptz` from a
    datetime and refuses a str. Production's `plan_slot` adapter reaches the
    same place from the other side — its payload IS a string, so it casts in
    SQL (`CAST(CAST(:slot AS text) AS timestamptz)`, #969) rather than letting
    the driver infer. This gate has a real datetime to hand and passes one.
    """
    n = next(_SLOT)
    return datetime(2026, 9, 1, n % 24, n % 60, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def notice_lane(admin_conn, owner_actor):
    """The real migration corpus, replayed ONCE.

    Module-scoped for `clock_db`'s reason: `run_lane` applies 001–066 end to
    end, and paying that per test is thirteen full replays against a cluster
    three bots share. Isolation comes from each scenario minting its own
    workspace below — the same trade `clock_db` documents ("scenarios keep it
    safe by minting their own accounts, kinds and slots rather than sharing").

    Not built on `bootstrapped_db`: that fixture is function-scoped, so
    depending on it would pin this to function scope too. Same two steps,
    taken directly — then the LOGIN roles' test passwords, so a scenario can
    act as the production roles (`svc_worker`, `svc_ingress`) the bootstrap
    created.
    """
    reason = actor_lacks_createrole(admin_conn)
    if reason:
        pytest.skip(reason)
    extra: list[str] = []
    gen = _scratch(admin_conn, roles=extra)
    dsn = next(gen)
    try:
        run_bootstrap(admin_conn, dsn)
        run_lane(dsn)
        set_test_passwords(admin_conn)
        yield dsn
    finally:
        gen.close()


@pytest.fixture()
def notice_db(notice_lane):
    """A FRESH workspace chain plus one ACTIVE telegram binding, per test.

    The binding is the sink both producers write to: `prompts.push_bindings`
    selects active `telegram%` rows only, so a workspace without one is #1090
    D5's gap and neither notice would have anywhere to land. Seeding it here
    keeps THIS gate about the producers rather than about D5.

    A fresh workspace per test is what makes the shared schema safe: every
    assertion below counts outbox rows for ITS OWN workspace, so a notice one
    scenario provokes cannot be counted by the next.

    `worker` connects to the same database as `svc_worker`, the role the
    worker runs `plan_slot` as: what the executor reads and writes is held to
    that role's grants and to the tenant its session claims.
    """
    conn = psycopg2.connect(notice_lane)
    try:
        chain = seed_workspace_chain(conn, f"d3d4-{uuid.uuid4().hex[:8]}")
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            cur.execute(
                "INSERT INTO channel_bindings (workspace_id, channel, external_ref)"
                " VALUES (%s, 'telegram_group', %s) RETURNING id",
                (chain["ws"], f"tg-{uuid.uuid4().hex[:8]}"),
            )
            binding = cur.fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    return {
        "dsn": notice_lane,
        "worker": as_user(notice_lane, "svc_worker"),
        "binding": str(binding),
        **chain,
    }


def _sql(notice_db, sql, params=None, fetch=False):
    """One statement as the schema owner. Actor GUC set: the §4 governance
    triggers refuse an anonymous write to ig_accounts and post_intents."""
    conn = psycopg2.connect(notice_db["dsn"])
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            cur.execute(sql, params or ())
            out = cur.fetchall() if fetch else None
        conn.commit()
        return out
    finally:
        conn.close()


def _notices(notice_db, *, like: str):
    return _sql(
        notice_db,
        "SELECT payload->>'text', intent_id FROM channel_outbox"
        " WHERE workspace_id = %s AND kind = 'notification'"
        "   AND payload->>'text' LIKE %s ORDER BY created_at",
        (notice_db["ws"], like),
        fetch=True,
    )


async def _plan(notice_db, *, window=NO_MEDIA_WINDOW_S):
    """One `plan_slot` for the chain's account at a fresh slot, as the worker
    runs it: the executor, as `svc_worker` under the job's tenant, at the
    production level, committed."""
    from sqlalchemy import text

    from src.services.target import content_runway, unit_of_work
    from src.services.target.scheduler import execute_plan_slot

    engine = create_async_engine(async_url(notice_db["worker"]))
    try:
        async with engine.connect() as conn:
            # The role is asserted rather than assumed: a session that quietly
            # connected as the owner would bypass RLS, and every tenant claim
            # below would hold vacuously.
            who = (await conn.execute(text("SELECT current_user"))).scalar()
            assert who == "svc_worker", who
            # `plan_slot` is a TENANT kind, so the worker's session carries
            # the job's own workspace — the opposite of D4's singleton
            # below, and the same `apply_gucs` call in both.
            await unit_of_work.apply_gucs(
                conn, tenant_id=notice_db["ws"], actor_kind="system"
            )
            out = await execute_plan_slot(
                conn,
                workspace_id=notice_db["ws"],
                ig_account_id=notice_db["iga"],
                slot_at=_unique_slot(),
                provider_account_ref="ref-d3",
                approval_mode="manual",
                no_media_notice_after_seconds=window,
                low_runway_days=content_runway.LOW_RUNWAY_DAYS,
            )
            await conn.commit()
        return out
    finally:
        await engine.dispose()


class TestD3TheNoMediaNotice:
    """D3 — "you are told once, not silently nothing"."""

    _plan = staticmethod(_plan)

    async def test_an_empty_selection_tells_the_workspace_once(self, notice_db):
        """THE D3 OBSERVATION. The seeded chain's only media item is already
        claimed by its seeded `scheduled` intent, so selection for this account
        is genuinely empty — the real exhausted-library case, not a doctored
        one."""
        assert (await self._plan(notice_db)).intent_id is None, "nothing minted"

        rows = _notices(notice_db, like="%No media was available%")
        assert len(rows) == 1, f"expected exactly one notice, got {rows}"
        assert "slot" in rows[0][0] and "Drive source" in rows[0][0]

        stamped = _sql(
            notice_db,
            "SELECT last_no_media_notice_at IS NOT NULL FROM ig_accounts WHERE id = %s",
            (notice_db["iga"],),
            fetch=True,
        )
        assert stamped[0][0] is True, "066's marker must be stamped at notice time"

    async def test_a_second_empty_slot_inside_the_window_says_nothing(self, notice_db):
        """ "...once", not once per slot. Without this the notice is a flood:
        a slot is planned per cadence step, so an exhausted library would
        message the workspace on every one."""
        await self._plan(notice_db)
        await self._plan(notice_db)
        rows = _notices(notice_db, like="%No media was available%")
        assert len(rows) == 1, f"the window must suppress the second: {rows}"

    async def test_past_the_window_it_speaks_again(self, notice_db):
        """The dedup is a WINDOW, not a latch — `05` says "at most once per
        window", and a latch would tell a workspace once ever."""
        await self._plan(notice_db)
        _sql(
            notice_db,
            "UPDATE ig_accounts SET last_no_media_notice_at = now()"
            " - interval '25 hours' WHERE id = %s",
            (notice_db["iga"],),
        )
        await self._plan(notice_db)
        rows = _notices(notice_db, like="%No media was available%")
        assert len(rows) == 2, f"past the window a second notice is due: {rows}"

    async def test_no_binding_reports_undeliverable_not_a_quiet_zero(self, notice_db):
        """THE ONE ARI ASKED FOR. A workspace with no push binding (#1090 D5)
        cannot receive this, and that must be distinguishable from having been
        told. The two live producers collapse it — `credential_lifecycle` logs
        and returns success, `media_sync` iterates an empty list in silence —
        and in both the ledger records a clean run. Here the executor's verdict
        is `UNDELIVERABLE`, which `work_loop` turns into a `review_required`
        job rather than a delivery."""
        from src.services.target import outbox

        _sql(
            notice_db,
            "UPDATE channel_bindings SET state = 'revoked' WHERE workspace_id = %s",
            (notice_db["ws"],),
        )
        out = await self._plan(notice_db)
        assert out.notice == outbox.UNDELIVERABLE, (
            "nobody could have been told, and the run must say so"
        )
        assert _notices(notice_db, like="%No media was available%") == []

    async def test_an_unreachable_account_is_not_retried_every_slot(self, notice_db):
        """The marker stamps on the ATTEMPT, so a standing no-binding condition
        parks one job per window rather than one per planned slot. It is a
        window and not a latch, so the account is retried tomorrow — and a
        workspace that gains a binding is told at its next slot."""
        from src.services.target import outbox

        _sql(
            notice_db,
            "UPDATE channel_bindings SET state = 'revoked' WHERE workspace_id = %s",
            (notice_db["ws"],),
        )
        assert (await self._plan(notice_db)).notice == outbox.UNDELIVERABLE
        assert (await self._plan(notice_db)).notice is None, (
            "the second slot is inside the window: recorded, not re-parked"
        )

        _sql(
            notice_db,
            "UPDATE channel_bindings SET state = 'active' WHERE workspace_id = %s",
            (notice_db["ws"],),
        )
        _sql(
            notice_db,
            "UPDATE ig_accounts SET last_no_media_notice_at = now()"
            " - interval '25 hours' WHERE id = %s",
            (notice_db["iga"],),
        )
        await self._plan(notice_db)
        assert len(_notices(notice_db, like="%No media was available%")) == 1, (
            "past the window, with a surface, they are finally told"
        )

    async def test_available_media_mints_and_says_nothing(self, notice_db):
        """THE POSITIVE CONTROL, and it is the load-bearing one. Every
        assertion above is satisfied by a producer that fires on every call;
        only this one separates "notifies when empty" from "notifies always" —
        and a false notice here would tell a working workspace its library is
        empty."""
        # Column spelling taken from `seed_intent_chain`, the suite's one home
        # for this insert — not restated from the DDL.
        _sql(
            notice_db,
            "INSERT INTO media_items"
            " (workspace_id, source_id, content_hash, file_name, media_kind,"
            "  provider_file_ref)"
            " VALUES (%s, %s, %s, 'second.jpg', 'image', %s)",
            (
                notice_db["ws"],
                notice_db["src"],
                f"hash-{uuid.uuid4().hex[:8]}",
                f"ref-{uuid.uuid4().hex[:8]}",
            ),
        )
        assert (await self._plan(notice_db)).intent_id is not None, "minted"
        assert _notices(notice_db, like="%No media was available%") == []


class TestD4TheParkedIntentNotice:
    """D4 — "a post needing attention notifies you within 24h with a link"."""

    def _park(self, notice_db, *, age_hours: int) -> str:
        """A `review_required` intent that entered the state *age_hours* ago,
        carrying the evidence trail the operator surface reads.

        **Born in the state, never UPDATEd into it** — the L.3/L.5 template.
        `tg_intent_no_self_transition` (061) and the `02` §4 matrix guard
        `post_intents.state`, so driving a seeded `scheduled` row into
        `review_required` with an UPDATE is refused by the database. Fresh
        media item and account ref per intent for `uq_intent_live_subject`,
        and `review_required` is a DEBITED state, so it carries
        `cap_consumed_on` with a real bucket row behind it.
        """
        ws, iga = notice_db["ws"], notice_db["iga"]
        day = "2026-09-01"
        media = _sql(
            notice_db,
            "INSERT INTO media_items (workspace_id, source_id, content_hash,"
            " file_name, media_kind, provider_file_ref)"
            " VALUES (%s, %s, %s, 'parked.jpg', 'image', %s) RETURNING id",
            (
                ws,
                notice_db["src"],
                f"h-{uuid.uuid4()}",
                f"r-{uuid.uuid4()}",
            ),
            fetch=True,
        )[0][0]
        _sql(
            notice_db,
            "INSERT INTO daily_post_counts (workspace_id, ig_account_id,"
            " local_date, count, cap_at_write) VALUES (%s, %s, %s, 1, 3)"
            " ON CONFLICT (workspace_id, ig_account_id, local_date)"
            " DO UPDATE SET count = daily_post_counts.count + 1",
            (ws, iga, day),
        )
        rows = _sql(
            notice_db,
            "INSERT INTO post_intents (workspace_id, ig_account_id,"
            " media_item_id, provider_account_ref, approval_mode,"
            " schedule_slot_at, state, entered_state_at, cap_consumed_on,"
            " last_error)"
            " VALUES (%s, %s, %s, %s, 'manual',"
            "         CAST(%s AS timestamptz), 'review_required',"
            "         now() - make_interval(hours => %s), %s,"
            "         CAST(%s AS jsonb)) RETURNING id",
            (
                ws,
                iga,
                media,
                f"acct-{uuid.uuid4()}",
                _unique_slot(),
                age_hours,
                day,
                json.dumps(
                    {
                        "v": 1,
                        "evidence": {
                            "checks": 4,
                            "trail": [{"status_code": "TIMEOUT", "check": 4}],
                        },
                    }
                ),
            ),
            fetch=True,
        )
        return str(rows[0][0])

    async def _sweep_and_notify(self, notice_db, *, origin="https://app.example"):
        """Drive the REAL door and the REAL producer, in that order — the same
        two calls `work_loop.reconcile_ambiguous` makes for a notify row.

        **The session opens with an EMPTY tenant id, and that is the whole
        point.** `reconcile_ambiguous` is a system singleton (`workspace_id
        NULL`), so `make_session_for` gives it ``app.tenant_id = ''``. Handing
        this gate the row's real workspace instead would be the comfortable
        thing to do and would prove nothing: under RLS the producer's writes
        are invisible at an empty tenant, so a gate that pre-scopes the session
        passes over a producer that writes nothing in production. Mirrored
        through `apply_gucs`, the same call the worker makes.
        """
        from src.services.target import outbox, reconciler, unit_of_work

        engine = create_async_engine(async_url(notice_db["dsn"]))
        served, unreachable = [], []
        try:
            async with engine.connect() as conn:
                await unit_of_work.apply_gucs(conn, tenant_id="", actor_kind="system")
                due = await reconciler.sweep_due(
                    conn, limit=50, notify_after_seconds=NOTIFY_AFTER_S
                )
                for op in due:
                    if op["reason"] != "notify_window":
                        continue
                    # The door is cross-tenant BY DESIGN (SECURITY DEFINER), so
                    # it also returns intents parked by earlier scenarios in
                    # this module's shared schema. The producer is still driven
                    # for them — that is real behaviour and must not be
                    # skipped — but only THIS workspace's rows are reported
                    # back, so an assertion here cannot depend on test order.
                    mine = str(op["workspace_id"]) == str(notice_db["ws"])
                    # Count what the producer actually WROTE, never what the
                    # door offered: a run that enqueued nothing must not read
                    # as a notification, which is the whole failure being
                    # tested for.
                    sent = await reconciler.notify_parked_customer(
                        conn,
                        intent_id=op["intent_id"],
                        workspace_id=op["workspace_id"],
                        web_app_origin=origin,
                        retry_after_seconds=NOTIFY_AFTER_S,
                    )
                    # str(): the door hands back UUID objects and `_park`
                    # returns str, so comparing raw would fail on type while
                    # the behaviour is right.
                    if not mine:
                        continue
                    if sent == outbox.UNDELIVERABLE:
                        unreachable.append(str(op["intent_id"]))
                    elif sent:
                        served.append(str(op["intent_id"]))
                await conn.commit()
            return served, unreachable
        finally:
            await engine.dispose()

    async def test_a_parked_intent_past_the_window_notifies_with_a_link(
        self, notice_db
    ):
        """THE D4 OBSERVATION, end to end: the 059 door selects the row, tags
        it `notify_window`, and the producer turns that tag into a message the
        customer receives."""
        intent = self._park(notice_db, age_hours=25)
        assert await self._sweep_and_notify(notice_db) == ([intent], [])

        rows = _notices(notice_db, like="%post needs attention%")
        assert len(rows) == 1, f"expected one notice, got {rows}"
        assert "https://app.example/dashboard/queue" in rows[0][0], "D4 wants a link"
        assert str(rows[0][1]) == intent, "the outbox row carries its intent"

    async def test_the_notice_preserves_the_evidence_trail(self, notice_db):
        """The stamp MERGES. `last_error->'evidence'` is the operator's whole
        inheritance on a parked intent (`06` §5's resolution surface reads the
        trail), so a `jsonb_build_object` rebuild of the kind `_record_evidence`
        does would notify the customer by destroying the evidence."""
        intent = self._park(notice_db, age_hours=25)
        await self._sweep_and_notify(notice_db)
        rows = _sql(
            notice_db,
            "SELECT last_error->'evidence'->>'checks',"
            "       last_error->'evidence'->'trail',"
            "       last_error->'evidence'->>'customer_notified'"
            " FROM post_intents WHERE id = %s",
            (intent,),
            fetch=True,
        )
        checks, trail, notified = rows[0]
        assert checks == "4", "the ladder's check count survived the stamp"
        assert trail and trail[0]["status_code"] == "TIMEOUT", "the trail survived"
        assert notified == "true", "and the stamp landed"

    async def test_a_second_sweep_does_not_notify_again(self, notice_db):
        """`06` §5 says ONE notification per parked intent. The door filters on
        `customer_notified`; this proves the producer actually sets it, which
        is the half that did not exist."""
        self._park(notice_db, age_hours=25)
        await self._sweep_and_notify(notice_db)
        assert await self._sweep_and_notify(notice_db) == ([], []), "nothing twice"
        assert len(_notices(notice_db, like="%post needs attention%")) == 1

    async def test_inside_the_window_nothing_is_due(self, notice_db):
        """THE POSITIVE CONTROL for the window itself: a gate that only ever
        sees aged rows cannot tell "waits 24 h" from "notifies immediately",
        and D4's line is about the window, not just the message."""
        self._park(notice_db, age_hours=1)
        assert await self._sweep_and_notify(notice_db) == ([], [])
        assert _notices(notice_db, like="%post needs attention%") == []

    async def test_no_binding_does_not_silently_burn_the_one_notification(
        self, notice_db
    ):
        """`customer_notified` is a LATCH, not a window — it never reopens. A
        workspace with no active push binding (#1090 D5) must therefore not
        consume it: stamping with nothing to send would mean the customer is
        never told, and the sweep would stop offering the row. Proven by
        restoring the binding and watching the same intent still notify."""
        intent = self._park(notice_db, age_hours=25)
        _sql(
            notice_db,
            "UPDATE channel_bindings SET state = 'revoked' WHERE workspace_id = %s",
            (notice_db["ws"],),
        )
        served, unreachable = await self._sweep_and_notify(notice_db)
        assert served == [], "nothing was sent"
        assert unreachable == [intent], (
            "and that must be SAYABLE — a run that reached nobody reporting"
            " the same value as a quiet successful one is the defect"
        )

        _sql(
            notice_db,
            "UPDATE channel_bindings SET state = 'active' WHERE workspace_id = %s",
            (notice_db["ws"],),
        )
        assert await self._sweep_and_notify(notice_db) == ([intent], []), (
            "the notice must survive the binding gap, not be spent by it"
        )
        assert len(_notices(notice_db, like="%post needs attention%")) == 1

    async def test_an_unreachable_workspace_is_retried_on_the_window_not_the_beat(
        self, notice_db
    ):
        """The sweep runs every 60 s and the condition stands until someone
        adds a binding. Re-attempting on every beat would park a
        `review_required` job a thousand times a day for one workspace and
        bury the signal, so the attempt is stamped and bounded by `05`'s
        window — the same "once, not daily" rule `06` §5 puts on the notice."""
        intent = self._park(notice_db, age_hours=25)
        _sql(
            notice_db,
            "UPDATE channel_bindings SET state = 'revoked' WHERE workspace_id = %s",
            (notice_db["ws"],),
        )
        first = await self._sweep_and_notify(notice_db)
        assert len(first[1]) == 1, "the first attempt reports unreachable"
        assert await self._sweep_and_notify(notice_db) == ([], []), (
            "the second, seconds later, is silent — the condition is recorded"
        )
        assert (
            _sql(
                notice_db,
                "SELECT last_error->'evidence'->>'customer_notified'"
                " FROM post_intents WHERE id = %s",
                (intent,),
                fetch=True,
            )[0][0]
            is None
        ), "and the delivered-latch is still untouched"

    async def test_without_a_web_origin_the_notice_still_fires(self, notice_db):
        """Deployment config must not be able to silence a notification. Being
        told late is a smaller failure than not being told."""
        self._park(notice_db, age_hours=25)
        await self._sweep_and_notify(notice_db, origin=None)
        rows = _notices(notice_db, like="%post needs attention%")
        assert len(rows) == 1 and "http" not in rows[0][0]


# --- #1478: the runway — the figure, and the one notice when it runs low ---


def _files(notice_db, n, *, source=None, state="available"):
    """*n* more files in *source* (the chain's own folder by default)."""
    rows = _sql(
        notice_db,
        "INSERT INTO media_items (workspace_id, source_id, content_hash, file_name,"
        " media_kind, provider_file_ref, state)"
        " SELECT %s, %s, 'hash-' || gen_random_uuid(), 'r.jpg', 'image',"
        "        'ref-' || gen_random_uuid(), %s"
        "   FROM generate_series(1, %s) RETURNING id",
        (notice_db["ws"], source or notice_db["src"], state, n),
        fetch=True,
    )
    return [str(r[0]) for r in rows]


def _folder(notice_db, *, state="active", removed=False):
    config = {"v": 1, "removed": True} if removed else {"v": 1}
    return str(
        _sql(
            notice_db,
            "INSERT INTO media_sources (workspace_id, provider, config, state)"
            " VALUES (%s, 'gdrive', %s, %s) RETURNING id",
            (notice_db["ws"], json.dumps(config), state),
            fetch=True,
        )[0][0]
    )


def _cadence(notice_db, posts_per_day):
    """The account's own posts per day (None inherits the workspace's) and a
    slot cursor, so the clock posts for it (`fn_clock_tick`, 084)."""
    _sql(
        notice_db,
        "UPDATE ig_accounts SET posts_per_day = %s, next_slot_at = now() WHERE id = %s",
        (posts_per_day, notice_db["iga"]),
    )


def _runway_told(notice_db):
    """Every runway notice queued for the workspace: "… of content left" and,
    for the mint that took the last file, "no content left"."""
    return _notices(notice_db, like="%content left%")


async def _mint(notice_db):
    """One `plan_slot` that must mint. A slot that minted nothing says nothing
    about the runway either, so a silence is evidence only beside a mint."""
    out = await _plan(notice_db)
    assert out.intent_id is not None, "the slot minted"
    return out


def _advisory_locks(notice_db, pid, *, granted):
    """The advisory locks backend *pid* holds (*granted*) or waits on."""
    return _sql(
        notice_db,
        "SELECT classid, objid, objsubid FROM pg_locks"
        " WHERE locktype = 'advisory' AND pid = %s AND granted = %s",
        (pid, granted),
        fetch=True,
    )


async def _waits_on_an_advisory_lock(notice_db, pid, task, *, within_s=30.0):
    """Whether backend *pid* queues on an advisory lock while *task* runs on
    it, within *within_s*: polled, because the wait begins when its statement
    reaches the server, which the caller cannot otherwise see. False as soon
    as *task* finishes, since a task that finished did not wait."""
    import asyncio

    loop = asyncio.get_running_loop()
    deadline = loop.time() + within_s
    while not task.done() and loop.time() < deadline:
        if _advisory_locks(notice_db, pid, granted=False):
            return True
        await asyncio.sleep(0.05)
    return False


def _latch(notice_db):
    """The account's runway audit rows, oldest first: the latch's history."""
    from src.services.target.content_runway import NOTICE_EVENT, REARM_EVENT

    return [
        row[0]
        for row in _sql(
            notice_db,
            "SELECT detail->>'event' FROM audit_events"
            " WHERE workspace_id = %s AND entity_kind = 'ig_account'"
            "   AND entity_id = %s AND detail->>'event' IN (%s, %s) ORDER BY id",
            (notice_db["ws"], notice_db["iga"], NOTICE_EVENT, REARM_EVENT),
            fetch=True,
        )
    ]


class TestTheRunwayNotice:
    """#1478 — told BEFORE the library runs dry: once, when a mint leaves the
    account under a week of content, through the empty library's own path (the
    push bindings' outbox), by the executor running as `svc_worker`. The
    chain's own file is held by its seeded intent, so every file below is one
    the test added."""

    async def test_the_mint_that_crosses_a_week_tells_the_workspace_once(
        self, notice_db
    ):
        from src.services.target.content_runway import NOTICE_EVENT

        _cadence(notice_db, 1)
        _files(notice_db, 8)
        await _mint(notice_db)
        assert _runway_told(notice_db) == [], "seven days left is not below a week"

        await _mint(notice_db)
        rows = _runway_told(notice_db)
        assert len(rows) == 1, rows
        assert "about 6 days of content left (6 files at 1 a day)" in rows[0][0]
        assert "Drive source" in rows[0][0]

        await _mint(notice_db)
        assert len(_runway_told(notice_db)) == 1, "once per crossing, not per slot"
        assert _latch(notice_db) == [NOTICE_EVENT]

    async def test_a_library_hovering_at_the_line_is_told_once(self, notice_db):
        """The gap between the two levels: back above a week, but not by a
        day's posts, re-arms nothing — a file coming off its `recent` lock
        must not buy a second notice."""
        from src.services.target.content_runway import NOTICE_EVENT

        _cadence(notice_db, 1)
        _files(notice_db, 7)
        await _mint(notice_db)  # 6 left: told
        _files(notice_db, 2)
        await _mint(notice_db)  # 7 left: above the line, below the re-arm
        await _mint(notice_db)  # 6 left again
        assert len(_runway_told(notice_db)) == 1
        assert _latch(notice_db) == [NOTICE_EVENT]

    async def test_a_library_that_refills_rearms_and_the_next_drop_is_told(
        self, notice_db
    ):
        from src.services.target.content_runway import NOTICE_EVENT, REARM_EVENT

        _cadence(notice_db, 1)
        _files(notice_db, 7)
        await _mint(notice_db)  # 6 left: told
        fresh = _files(notice_db, 10)
        await _mint(notice_db)  # 15 left: re-armed, and nothing said
        assert len(_runway_told(notice_db)) == 1
        assert _latch(notice_db) == [NOTICE_EVENT, REARM_EVENT]

        # The next drop is not a mint's doing: eight still-eligible files leave
        # the library. The next mint reads it, wherever it came from.
        _sql(
            notice_db,
            "UPDATE media_items SET state = 'removed' WHERE id IN ("
            "  SELECT m.id FROM media_items m WHERE m.id = ANY(%s::uuid[])"
            "     AND NOT EXISTS (SELECT 1 FROM post_intents p"
            "                     WHERE p.media_item_id = m.id)"
            "   ORDER BY m.id LIMIT 8)",
            (fresh,),
        )
        await _mint(notice_db)  # 7 eligible, 6 left: told again
        assert len(_runway_told(notice_db)) == 2
        assert _latch(notice_db) == [NOTICE_EVENT, REARM_EVENT, NOTICE_EVENT]

    async def test_an_edit_to_the_account_is_not_the_latch(self, notice_db):
        """The latch is the newest of its OWN two rows. An admin's edit to the
        account (its display name here) writes the account's governance audit
        row after the notice; it must not re-arm it, or the next low mint is
        told again."""
        from src.services.target.content_runway import NOTICE_EVENT

        _cadence(notice_db, 1)
        _files(notice_db, 7)
        await _mint(notice_db)  # 6 left: told
        _sql(
            notice_db,
            "UPDATE ig_accounts SET display_name = 'Renamed by an admin' WHERE id = %s",
            (notice_db["iga"],),
        )
        await _mint(notice_db)  # 5 left: still the same crossing
        assert len(_runway_told(notice_db)) == 1, "an edit is not a re-arm"
        assert _latch(notice_db) == [NOTICE_EVENT]

    async def test_the_workspace_cadence_divides_when_the_account_has_none(
        self, notice_db
    ):
        _cadence(notice_db, None)  # the workspace's 3 a day (053's default)
        _files(notice_db, 22)
        await _mint(notice_db)  # 21 left: exactly a week at 3 a day
        assert _runway_told(notice_db) == []
        await _mint(notice_db)  # 20 left
        rows = _runway_told(notice_db)
        assert len(rows) == 1, rows
        assert "about 6 days of content left (20 files at 3 a day)" in rows[0][0]

    async def test_no_binding_still_mints_and_latches_the_crossing(self, notice_db):
        """The notice never decides the slot. With no push binding the mint
        stands and carries no notice back, so its job finalizes succeeded; the
        crossing is latched with nobody told (`told` 0), so the next slot
        writes no second row, and the Overview's card is that workspace's
        channel."""
        from src.services.target.content_runway import NOTICE_EVENT

        _sql(
            notice_db,
            "UPDATE channel_bindings SET state = 'revoked' WHERE workspace_id = %s",
            (notice_db["ws"],),
        )
        _cadence(notice_db, 1)
        _files(notice_db, 7)
        out = await _mint(notice_db)
        assert out.notice is None, "a runway notice nobody heard rides on nothing"
        assert _runway_told(notice_db) == []
        assert _latch(notice_db) == [NOTICE_EVENT], "the crossing is spoken for"
        told = _sql(
            notice_db,
            "SELECT detail->>'told' FROM audit_events"
            " WHERE workspace_id = %s AND entity_kind = 'ig_account'"
            "   AND entity_id = %s AND detail->>'event' = %s",
            (notice_db["ws"], notice_db["iga"], NOTICE_EVENT),
            fetch=True,
        )
        assert told == [("0",)], "and nobody was told"

        out = await _mint(notice_db)
        assert out.notice is None
        assert _latch(notice_db) == [NOTICE_EVENT], "once per crossing, not per slot"

    async def test_a_full_library_says_nothing(self, notice_db):
        """THE POSITIVE CONTROL: a producer that fired on every mint would pass
        the once-only tests' first halves; it fails here."""
        _cadence(notice_db, 1)
        _files(notice_db, 20)
        await _mint(notice_db)
        assert _runway_told(notice_db) == []
        assert _latch(notice_db) == []

    async def test_a_second_writer_waits_on_the_accounts_lock(self, notice_db):
        """Two transactions settle one account's crossing at once, both as
        `svc_worker`. The first tells and keeps its transaction open; the
        second queues on the account's `runway:` lock, the very one the first
        holds, until the first commits, then reads the latch the first wrote
        and says nothing. Without the lock the second reads no latch (the
        first's row is not yet visible) and tells again."""
        import asyncio

        from sqlalchemy import text

        from src.services.target import content_runway, unit_of_work
        from src.services.target.content_runway import NOTICE_EVENT

        _cadence(notice_db, 1)

        async def settle(conn):
            return await content_runway.after_mint(
                conn,
                workspace_id=notice_db["ws"],
                ig_account_id=notice_db["iga"],
                eligible=3,
                below_days=content_runway.LOW_RUNWAY_DAYS,
            )

        engine = create_async_engine(async_url(notice_db["worker"]))
        try:
            async with engine.connect() as first, engine.connect() as second:
                pids = []
                for conn in (first, second):
                    await unit_of_work.apply_gucs(
                        conn, tenant_id=notice_db["ws"], actor_kind="system"
                    )
                    pids.append(
                        (await conn.execute(text("SELECT pg_backend_pid()"))).scalar()
                    )
                assert await settle(first) == 1, "the first tells its one binding"
                held = _advisory_locks(notice_db, pids[0], granted=True)
                assert len(held) == 1, "positive control: the first holds the lock"

                waiting = asyncio.create_task(settle(second))
                try:
                    assert await _waits_on_an_advisory_lock(
                        notice_db, pids[1], waiting
                    ), "the second did not wait on an advisory lock"
                    assert not waiting.done()
                    assert _advisory_locks(notice_db, pids[1], granted=False) == held
                    await first.commit()
                    assert await asyncio.wait_for(waiting, timeout=30) is None
                finally:
                    # Never leave the second blocked on a lock the first holds,
                    # or its connection busy when the context closes it.
                    if not waiting.done():
                        await first.rollback()
                    await asyncio.wait_for(
                        asyncio.gather(waiting, return_exceptions=True), timeout=30
                    )
                await second.commit()
        finally:
            await engine.dispose()

        assert _latch(notice_db) == [NOTICE_EVENT], "one crossing, one row"
        assert len(_runway_told(notice_db)) == 1


class TestTheRunwayRead:
    """#1478 — the Overview's figure is the planner's own pool, read as the API
    reads it: `svc_ingress`, under the member's tenant."""

    @pytest.fixture()
    def ingress(self, notice_db):
        return as_user(notice_db["dsn"], "svc_ingress")

    async def _read(self, notice_db, ingress, *, workspace=None):
        from src.services.target import content_runway

        async def read(session):
            return await content_runway.runway(
                session,
                workspace_id=str(workspace or notice_db["ws"]),
                below_days=content_runway.LOW_RUNWAY_DAYS,
            )

        return await in_tenant(ingress, notice_db["ws"], notice_db["user"], read)

    async def test_the_count_is_the_planners_pool(self, notice_db, ingress):
        _cadence(notice_db, 1)
        _files(notice_db, 3)
        aged, held = _files(notice_db, 2)
        (skipped,) = _files(notice_db, 1)
        _files(notice_db, 1, state="unsupported")
        _sql(
            notice_db,
            "INSERT INTO post_locks"
            " (workspace_id, media_item_id, ig_account_id, kind, expires_at)"
            " VALUES (%s, %s, %s, 'recent', now() - interval '1 day'),"
            "        (%s, %s, %s, 'recent', now() + interval '30 days'),"
            "        (%s, %s, NULL, 'skip', now() + interval '45 days')",
            (
                notice_db["ws"],
                aged,
                notice_db["iga"],
                notice_db["ws"],
                held,
                notice_db["iga"],
                notice_db["ws"],
                skipped,
            ),
        )
        off = _folder(notice_db)
        _files(notice_db, 2, source=off)
        _sql(
            notice_db,
            "INSERT INTO category_post_case_mix (workspace_id, source_id, category,"
            " ratio) VALUES (%s, %s, 'off', 0)",
            (notice_db["ws"], off),
        )
        _files(notice_db, 1, source=_folder(notice_db, state="error"))
        _files(notice_db, 1, source=_folder(notice_db, removed=True))

        # Three plain files and the one whose `recent` lock has expired; not
        # the chain's own (held by its intent), the live lock, the skip, the
        # unsupported file, nor anything in an Off, error or removed folder.
        (row,) = (await self._read(notice_db, ingress))["accounts"]
        assert (row["eligible"], row["posts_per_day"], row["days_left"]) == (4, 1, 4)
        assert row["posting"] is True and row["low"] is True

        # The planner agrees: it can mint exactly those four, and then nothing.
        for _ in range(4):
            assert (await _plan(notice_db)).intent_id is not None
        assert (await _plan(notice_db)).intent_id is None
        (row,) = (await self._read(notice_db, ingress))["accounts"]
        assert (row["eligible"], row["days_left"]) == (0, 0)

    async def test_a_terminal_intent_releases_its_file(self, notice_db, ingress):
        """An intent that ended holds nothing (`intent_ledger.TERMINAL_STATES`):
        a file whose only intent for this account is `skipped`, under no live
        lock, is counted by the read and minted by the planner. The chain's own
        file, held by its live `scheduled` intent, is the contrast: neither
        counts it."""
        from src.services.target.intent_ledger import TERMINAL_STATES

        assert "skipped" in TERMINAL_STATES
        _cadence(notice_db, 1)
        (released,) = _files(notice_db, 1)
        _sql(
            notice_db,
            "INSERT INTO post_intents (workspace_id, ig_account_id, media_item_id,"
            " provider_account_ref, approval_mode, schedule_slot_at, state)"
            " VALUES (%s, %s, %s, %s, 'manual', %s, 'skipped')",
            (notice_db["ws"], notice_db["iga"], released, "ref-d3", _unique_slot()),
        )
        locks = _sql(
            notice_db,
            "SELECT count(*) FROM post_locks WHERE media_item_id = %s",
            (released,),
            fetch=True,
        )
        assert locks == [(0,)], "precondition: no lock on it, live or not"

        (row,) = (await self._read(notice_db, ingress))["accounts"]
        assert row["eligible"] == 1, "the skipped one's file, not the chain's own"

        minted = (await _mint(notice_db)).intent_id
        drawn = _sql(
            notice_db,
            "SELECT media_item_id FROM post_intents WHERE id = %s",
            (minted,),
            fetch=True,
        )
        assert [str(r[0]) for r in drawn] == [released]
        assert (await _plan(notice_db)).intent_id is None, "and then nothing is left"

    async def test_an_account_the_clock_does_not_post_for_has_no_runway(
        self, notice_db, ingress
    ):
        _files(notice_db, 3)
        # No slot cursor: the clock mints nothing for this account.
        (row,) = (await self._read(notice_db, ingress))["accounts"]
        assert row["eligible"] == 3, "its files are still counted"
        assert (row["posting"], row["days_left"], row["low"]) == (False, None, False)

        _cadence(notice_db, 1)
        (row,) = (await self._read(notice_db, ingress))["accounts"]
        assert (row["posting"], row["days_left"]) == (True, 3), "positive control"

        _sql(
            notice_db,
            "UPDATE workspaces SET is_paused = true WHERE id = %s",
            (notice_db["ws"],),
        )
        (row,) = (await self._read(notice_db, ingress))["accounts"]
        assert (row["posting"], row["days_left"], row["low"]) == (False, None, False)

    async def test_another_workspaces_runway_reads_nothing(self, notice_db, ingress):
        conn = psycopg2.connect(notice_db["dsn"])
        try:
            other = seed_workspace_chain(conn, f"rw-{uuid.uuid4().hex[:8]}")
        finally:
            conn.close()
        assert (await self._read(notice_db, ingress, workspace=other["ws"])) == {
            "below_days": 7,
            "accounts": [],
        }
        assert len((await self._read(notice_db, ingress))["accounts"]) == 1
