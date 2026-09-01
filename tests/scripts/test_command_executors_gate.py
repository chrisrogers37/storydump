"""Every command executor body, EXECUTED — as `svc_ingress`, through the real
port, on the replayed target schema.

navi's finding on #1035, in substance: the unit gate
(`tests/src/services/target/test_commands.py`) patches `commands.REGISTRY`, so
a green run there proves the DISPATCH and says nothing about the BODY; the X.2
gate (`test_web_router_x2_gate.py`) runs real bodies for three of ten —
`create_workspace`, `settings_change`, `approve`. This file drives the other
seven — `skip`, `reject`, `mark_posted`, `cancel`, `sync_now`,
`pause_workspace`, `resume_workspace` — and `rename_workspace`, each through
`commands.execute` with the REAL registry inside a real unit of work opened as
the production role, and asserts the EFFECTS the `02` §4 matrix rows name,
read back as the owner. None of these executors talks to a provider, so
nothing is excluded for needing one.

The two that carry the PR's own R1 claim are pinned hardest. `mark_posted`:
the debit is UNCONDITIONAL and lands past the cap (the story is already on
Instagram, so refusing to record it would misstate the day), `cap_at_write`
freezes at the day's first debit, and a REFUSED mark_posted rolls its debit
back with the transaction rather than leaving a phantom count. `sync_now`:
exactly one job while one is pending, and a fresh one once it is not.
"""

from __future__ import annotations

import asyncio
import uuid

import psycopg2
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.config.defaults import DEFAULT_REPOST_TTL_DAYS, DEFAULT_SKIP_TTL_DAYS
from src.services.target import commands
from src.services.target.commands import Command, CommandRefused
from src.services.target.unit_of_work import asyncpg_url, unit_of_work
from tests.scripts.conftest import (
    _scratch,
    as_user,
    fetch_one,
    replay_advertised_stream,
    seed_workspace_chain,
    set_test_passwords,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]


@pytest.fixture(scope="module")
def world(admin_conn, owner_actor):
    """Replayed schema, two seeded workspaces (the two-identity discipline:
    every "which row" assertion has a second tenant to be wrong about)."""
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        stream = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        conn = psycopg2.connect(stream)
        try:
            a = seed_workspace_chain(conn, "exec-a")
            b = seed_workspace_chain(conn, "exec-b")
        finally:
            conn.close()
        yield {"stream": stream, "ingress": as_user(db, "svc_ingress"), "a": a, "b": b}
    finally:
        gen.close()


# --- fixture data and ground truth, as the migration actor / the owner --------


def _migrate(dsn: str, sql: str, params=()):
    """One committed statement as the migration actor (the audit triggers
    refuse an anonymous write). Returns the first row, if any."""
    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            cur.execute(sql, params)
            row = cur.fetchone() if cur.description else None
        conn.commit()
        return row
    finally:
        conn.close()


def _media(world, ids, tag: str) -> str:
    (mi,) = _migrate(
        world["stream"],
        "INSERT INTO media_items"
        " (workspace_id, source_id, content_hash, file_name, media_kind, provider_file_ref)"
        " VALUES (%s, %s, %s, 'f.jpg', 'image', %s) RETURNING id",
        (
            str(ids["ws"]),
            str(ids["src"]),
            f"hash-{tag}-{uuid.uuid4().hex[:6]}",
            f"ref-{tag}",
        ),
    )
    return str(mi)


def _intent(
    world, ids, tag: str, *, media: str | None = None, state="awaiting_approval"
) -> dict:
    """An intent on the chain's account (so per-account counters accumulate),
    on a fresh media item unless one is given."""
    media = media or _media(world, ids, tag)
    (intent,) = _migrate(
        world["stream"],
        "INSERT INTO post_intents"
        " (workspace_id, ig_account_id, media_item_id, provider_account_ref,"
        "  approval_mode, schedule_slot_at, state)"
        " VALUES (%s, %s, %s, %s, 'manual', now(), %s) RETURNING id",
        (str(ids["ws"]), str(ids["iga"]), media, f"acct-{ids['name']}", state),
    )
    return {"id": str(intent), "media": media}


def _one(world, sql: str, params=()):
    return fetch_one(world["stream"], sql, params)


# --- the port, as the production role ----------------------------------------


async def _execute(dsn: str, ws: str, user: str, kind: str, args: dict):
    engine = create_async_engine(asyncpg_url(dsn), poolclass=NullPool)
    try:
        uow = unit_of_work(
            engine, ws, actor_kind="user", actor_user_id=user, channel="web"
        )
        async with uow.begin() as session:
            who = (await session.execute(text("SELECT current_user"))).scalar()
            assert who == "svc_ingress", who
            return await commands.execute(
                session,
                Command(
                    kind=kind,
                    workspace_id=ws,
                    actor_user_id=user,
                    channel="web",
                    args=args,
                ),
            )
    finally:
        await engine.dispose()


def run(world, kind: str, *, ids=None, **args):
    """Execute *kind* as the owner of workspace *ids* (default: A), through the
    real registry, in one committed unit of work."""
    ids = ids or world["a"]
    return asyncio.run(
        _execute(world["ingress"], str(ids["ws"]), str(ids["user"]), kind, args)
    )


def refused(world, kind: str, *, ids=None, **args) -> str:
    with pytest.raises(CommandRefused) as err:
        run(world, kind, ids=ids, **args)
    return err.value.reason


@pytest.fixture(autouse=True, scope="module")
def _names(world):
    world["a"]["name"] = "exec-a"
    world["b"]["name"] = "exec-b"


# --- skip / reject: the terminal flips and their locks -----------------------


class TestSkip:
    def test_skips_and_holds_a_workspace_wide_lock_for_the_skip_ttl(self, world):
        i = _intent(world, world["a"], "skip-1")
        out = run(world, "skip", intent_id=i["id"])
        assert out.outcome == "executed"
        assert out.data == {
            "intent_id": i["id"],
            "state": "skipped",
            "lock": "skip",
            "lock_days": DEFAULT_SKIP_TTL_DAYS,
        }
        assert _one(
            world, "SELECT state FROM post_intents WHERE id = %s", (i["id"],)
        ) == ("skipped",)
        lock = _one(
            world,
            "SELECT ig_account_id, created_by_intent_id::text, created_by_user_id::text,"
            "       expires_at BETWEEN now() + interval '44 days' AND now() + interval '46 days'"
            "  FROM post_locks WHERE workspace_id = %s AND media_item_id = %s AND kind = 'skip'",
            (str(world["a"]["ws"]), i["media"]),
        )
        assert lock == (None, i["id"], str(world["a"]["user"]), True)

    def test_a_second_skip_of_the_same_media_refreshes_the_one_lock(self, world):
        first = _intent(world, world["a"], "skip-2a")
        run(world, "skip", intent_id=first["id"])
        second = _intent(world, world["a"], "skip-2b", media=first["media"])
        run(world, "skip", intent_id=second["id"])
        assert _one(
            world,
            "SELECT count(*), max(created_by_intent_id::text) FROM post_locks"
            " WHERE workspace_id = %s AND media_item_id = %s AND kind = 'skip'",
            (str(world["a"]["ws"]), first["media"]),
        ) == (1, second["id"])

    def test_a_terminal_intent_is_refused_by_the_ledger_and_untouched(self, world):
        i = _intent(world, world["a"], "skip-3")
        run(world, "skip", intent_id=i["id"])
        assert refused(world, "skip", intent_id=i["id"]) == "illegal_transition"
        assert refused(world, "reject", intent_id=i["id"]) == "illegal_transition"
        assert _one(
            world, "SELECT state FROM post_intents WHERE id = %s", (i["id"],)
        ) == ("skipped",)


class TestReject:
    def test_rejects_and_holds_a_permanent_workspace_wide_lock(self, world):
        i = _intent(world, world["a"], "reject-1")
        out = run(world, "reject", intent_id=i["id"])
        assert out.data == {"intent_id": i["id"], "state": "rejected", "lock": "reject"}
        assert _one(
            world, "SELECT state FROM post_intents WHERE id = %s", (i["id"],)
        ) == ("rejected",)
        assert _one(
            world,
            "SELECT ig_account_id, expires_at, created_by_intent_id::text FROM post_locks"
            " WHERE workspace_id = %s AND media_item_id = %s AND kind = 'reject'",
            (str(world["a"]["ws"]), i["media"]),
        ) == (None, None, i["id"])

    def test_a_second_reject_of_the_same_media_keeps_one_lock(self, world):
        first = _intent(world, world["a"], "reject-2a")
        run(world, "reject", intent_id=first["id"])
        second = _intent(world, world["a"], "reject-2b", media=first["media"])
        run(world, "reject", intent_id=second["id"])
        assert _one(
            world,
            "SELECT count(*), max(created_by_intent_id::text) FROM post_locks"
            " WHERE workspace_id = %s AND media_item_id = %s AND kind = 'reject'",
            (str(world["a"]["ws"]), first["media"]),
        ) == (1, second["id"])


# --- mark_posted: R1, the manual-mode path ------------------------------------


class TestMarkPosted:
    """The account's day is debited unconditionally, the cap is frozen at the
    first debit, and a refusal leaves no debit behind."""

    @pytest.fixture(autouse=True, scope="class")
    def _cap_of_one(self, world):
        _migrate(
            world["stream"],
            "UPDATE workspaces SET posts_per_day = 1 WHERE id = %s",
            (str(world["a"]["ws"]),),
        )

    def _count(self, world):
        return _one(
            world,
            "SELECT count, cap_at_write FROM daily_post_counts"
            " WHERE workspace_id = %s AND ig_account_id = %s"
            "   AND local_date = (now() AT TIME ZONE 'UTC')::date",
            (str(world["a"]["ws"]), str(world["a"]["iga"])),
        )

    def test_the_first_post_debits_the_day_and_records_every_effect(self, world):
        i = _intent(world, world["a"], "posted-1")
        out = run(world, "mark_posted", intent_id=i["id"])
        assert out.data == {
            "intent_id": i["id"],
            "state": "posted",
            "published_via": "manual",
        }
        assert _one(
            world,
            "SELECT state, published_via, cap_consumed_on = (now() AT TIME ZONE 'UTC')::date"
            "  FROM post_intents WHERE id = %s",
            (i["id"],),
        ) == ("posted", "manual", True)
        assert self._count(world) == (1, 1)
        assert _one(
            world,
            "SELECT times_posted, last_posted_at IS NOT NULL FROM media_items WHERE id = %s",
            (i["media"],),
        ) == (1, True)
        assert _one(
            world,
            "SELECT ig_account_id::text, created_by_intent_id::text,"
            "       expires_at BETWEEN now() + interval '%s days' AND now() + interval '%s days'"
            "  FROM post_locks WHERE workspace_id = %s AND media_item_id = %s AND kind = 'recent'",
            (
                DEFAULT_REPOST_TTL_DAYS - 1,
                DEFAULT_REPOST_TTL_DAYS + 1,
                str(world["a"]["ws"]),
                i["media"],
            ),
        ) == (str(world["a"]["iga"]), i["id"], True)
        assert _one(
            world,
            "SELECT last_posted_at IS NOT NULL FROM ig_accounts WHERE id = %s",
            (str(world["a"]["iga"]),),
        ) == (True,)

    def test_the_debit_is_unconditional_past_the_cap_and_the_cap_stays_frozen(
        self, world
    ):
        _migrate(
            world["stream"],
            "UPDATE workspaces SET posts_per_day = 5 WHERE id = %s",
            (str(world["a"]["ws"]),),
        )
        i = _intent(world, world["a"], "posted-2")
        run(world, "mark_posted", intent_id=i["id"])
        # count 2 against a cap that was 1 at first debit: recorded, not refused;
        # cap_at_write is still the first debit's 1, not today's 5.
        assert self._count(world) == (2, 1)

    def test_a_refused_mark_posted_rolls_its_debit_back(self, world):
        posted = _intent(world, world["a"], "posted-3")
        run(world, "mark_posted", intent_id=posted["id"])
        before = self._count(world)
        with pytest.raises(CommandRefused) as err:
            run(world, "mark_posted", intent_id=posted["id"])
        assert err.value.reason == "illegal_transition"
        assert "'posted'" in str(err.value)
        assert self._count(world) == before, (
            "the refused command's debit leaked past the rollback"
        )

    def test_only_an_awaiting_intent_can_be_marked(self, world):
        scheduled = _intent(world, world["a"], "posted-4", state="scheduled")
        before = self._count(world)
        assert (
            refused(world, "mark_posted", intent_id=scheduled["id"])
            == "illegal_transition"
        )
        assert self._count(world) == before


# --- cancel: an overlay flag, never a terminal state --------------------------


class TestCancel:
    def test_requests_cancellation_and_leaves_the_state_to_the_worker(self, world):
        i = _intent(world, world["a"], "cancel-1")
        out = run(world, "cancel", intent_id=i["id"])
        assert out.data == {
            "intent_id": i["id"],
            "state": "awaiting_approval",
            "cancel_requested": True,
        }
        assert _one(
            world,
            "SELECT state, cancel_requested FROM post_intents WHERE id = %s",
            (i["id"],),
        ) == ("awaiting_approval", True)

    def test_a_terminal_intent_cannot_be_cancelled(self, world):
        i = _intent(world, world["a"], "cancel-2")
        run(world, "skip", intent_id=i["id"])
        assert refused(world, "cancel", intent_id=i["id"]) == "illegal_transition"
        assert _one(
            world, "SELECT cancel_requested FROM post_intents WHERE id = %s", (i["id"],)
        ) == (False,)


# --- sync_now: one demand job at a time ---------------------------------------


class TestSyncNow:
    def _jobs(self, world, src: str):
        return _one(
            world,
            "SELECT count(*) FROM jobs WHERE serialization_key = %s AND kind = 'sync_media_source'",
            (f"src:{src}",),
        )[0]

    def test_mints_the_demand_sync_job_in_the_clocks_shape(self, world):
        src = str(world["a"]["src"])
        out = run(world, "sync_now", source_id=src)
        assert out.outcome == "enqueued"
        assert out.data["job"] == "sync_media_source" and out.data["source_id"] == src
        row = _one(
            world,
            "SELECT kind, state, lane, max_attempts, serialization_key, workspace_id::text, payload"
            "  FROM jobs WHERE id = %s",
            (out.data["job_id"],),
        )
        assert row == (
            "sync_media_source",
            "ready",
            "bulk",
            5,
            f"src:{src}",
            str(world["a"]["ws"]),
            {"v": 1, "source_id": src, "reason": "demand"},
        )

    def test_a_second_request_while_one_is_pending_mints_nothing(self, world):
        src = str(world["a"]["src"])
        before = self._jobs(world, src)
        out = run(world, "sync_now", source_id=src)
        assert out.outcome == "executed" and out.data["sync"] == "already_pending"
        assert self._jobs(world, src) == before

    def test_once_the_pending_job_is_done_a_new_one_is_minted(self, world):
        src = str(world["a"]["src"])
        _migrate(
            world["stream"],
            "UPDATE jobs SET state = 'succeeded' WHERE serialization_key = %s AND state = 'ready'",
            (f"src:{src}",),
        )
        before = self._jobs(world, src)
        assert run(world, "sync_now", source_id=src).outcome == "enqueued"
        assert self._jobs(world, src) == before + 1

    def test_an_unknown_or_foreign_source_is_not_found(self, world):
        assert refused(world, "sync_now", source_id=str(uuid.uuid4())) == "not_found"
        # B's source, asked for under A: the WHERE binds the workspace, RLS or not.
        assert (
            refused(world, "sync_now", source_id=str(world["b"]["src"])) == "not_found"
        )


# --- pause / resume / rename: the workspace writers ---------------------------


class TestPauseResumeRename:
    def _paused(self, world):
        return _one(
            world,
            "SELECT is_paused, paused_at IS NOT NULL, paused_by_user_id::text FROM workspaces WHERE id = %s",
            (str(world["a"]["ws"]),),
        )

    def test_pause_records_who_and_when_and_resume_clears_both(self, world):
        assert run(world, "pause_workspace").data == {"is_paused": True}
        assert self._paused(world) == (True, True, str(world["a"]["user"]))
        assert run(world, "resume_workspace").data == {"is_paused": False}
        assert self._paused(world) == (False, False, None)

    def test_rename_writes_the_cleaned_name_and_refuses_an_overlong_one(self, world):
        assert run(world, "rename_workspace", name="  Renamed  ").data == {
            "name": "Renamed"
        }
        assert _one(
            world, "SELECT name FROM workspaces WHERE id = %s", (str(world["a"]["ws"]),)
        ) == ("Renamed",)
        assert refused(world, "rename_workspace", name="x" * 101) == "invalid_args"


# --- two identities: the executors bind the workspace, not only RLS -----------


class TestAnotherTenantsIntent:
    def test_is_not_found_under_the_wrong_workspace(self, world):
        i = _intent(world, world["a"], "foreign-1")
        # B's owner, in B's own unit of work, naming A's intent: the executor's
        # WHERE binds workspace_id, so this is not_found before RLS is even asked.
        assert refused(world, "skip", ids=world["b"], intent_id=i["id"]) == "not_found"
        assert (
            refused(world, "mark_posted", ids=world["b"], intent_id=i["id"])
            == "not_found"
        )
        assert _one(
            world, "SELECT state FROM post_intents WHERE id = %s", (i["id"],)
        ) == ("awaiting_approval",)


# --- account_settings_change: the per-account schedule overrides (#1175) ------


def _acct(world, ids=None) -> dict:
    """The four override columns plus what the clock would actually resolve —
    the same `COALESCE(account, workspace)` the due-scan applies per tick, so a
    test cannot pass by writing a column the scheduler does not read."""
    ids = ids or world["a"]
    ppd, hs, he, tz, eff_ppd, eff_tz = _one(
        world,
        "SELECT a.posts_per_day, a.posting_hours_start, a.posting_hours_end, a.tz,"
        "       COALESCE(a.posts_per_day, w.posts_per_day),"
        "       COALESCE(a.tz, w.tz)"
        "  FROM ig_accounts a JOIN workspaces w ON w.id = a.workspace_id"
        " WHERE a.id = %s",
        (str(ids["iga"]),),
    )
    return {
        "posts_per_day": ppd,
        "posting_hours_start": hs,
        "posting_hours_end": he,
        "tz": tz,
        "eff_ppd": eff_ppd,
        "eff_tz": eff_tz,
    }


class TestAccountSettingsChange:
    """`054` gave every account its own cadence, window and tz; `06` §3 made the
    account the unit of scheduling; `fn_clock_tick` resolves the ladder per row.
    Nothing could write those columns, so a second account silently inherited
    every default. These drive the write end to end and read back what the CLOCK
    reads, not merely what was stored."""

    def test_sets_an_override_the_clock_will_resolve(self, world):
        before = _acct(world)
        assert before["posts_per_day"] is None, "the chain seeds a bare account"
        out = run(
            world,
            "account_settings_change",
            ig_account_id=str(world["a"]["iga"]),
            settings={"posts_per_day": 7, "tz": "America/New_York"},
        )
        assert out.outcome == "executed"
        assert out.data["changed"] == ["posts_per_day", "tz"]
        after = _acct(world)
        assert after["posts_per_day"] == 7
        assert after["eff_ppd"] == 7, "the override must win the COALESCE"
        assert after["eff_tz"] == "America/New_York"

    def test_null_puts_the_account_back_on_the_workspace_default(self, world):
        """NULL is the INHERIT arm, not "unset". Without this the command is a
        one-way door: an account could leave the default and never return."""
        (ws_default,) = _one(
            world,
            "SELECT posts_per_day FROM workspaces WHERE id = %s",
            (str(world["a"]["ws"]),),
        )
        run(
            world,
            "account_settings_change",
            ig_account_id=str(world["a"]["iga"]),
            settings={"posts_per_day": 11},
        )
        assert _acct(world)["eff_ppd"] == 11
        run(
            world,
            "account_settings_change",
            ig_account_id=str(world["a"]["iga"]),
            settings={"posts_per_day": None},
        )
        back = _acct(world)
        assert back["posts_per_day"] is None
        assert back["eff_ppd"] == ws_default, "inheritance restored"

    def test_another_workspaces_account_is_not_found_and_is_not_written(self, world):
        """The half that matters is the second assertion: a refusal that had
        already written would be the tenancy bug wearing a 404.

        **What enforces this is RLS, not the writer's WHERE clause, and this
        test cannot tell them apart.** Mutation-tested: delete
        `AND workspace_id = :ws` from `change_account_settings` and this stays
        green, because `p_tenant` on `ig_accounts` (`058`:266) already covers
        `svc_ingress`. Read it as the CONTRACT being pinned — cross-tenant is
        `not_found` and writes nothing — never as evidence for the clause."""
        b_before = _acct(world, world["b"])
        assert (
            refused(
                world,
                "account_settings_change",
                ig_account_id=str(world["b"]["iga"]),
                settings={"posts_per_day": 9},
            )
            == "not_found"
        )
        assert _acct(world, world["b"]) == b_before

    def test_a_missing_or_unparseable_account_id_is_invalid_args(self, world):
        """Not a 500. `ig_accounts.id` is UUID, so an unparseable string would
        reach Postgres as a failed cast — a DataError this tier does not
        translate."""
        assert (
            refused(world, "account_settings_change", settings={"posts_per_day": 3})
            == "invalid_args"
        )
        assert (
            refused(
                world,
                "account_settings_change",
                ig_account_id="not-a-uuid",
                settings={"posts_per_day": 3},
            )
            == "invalid_args"
        )

    def test_an_unlisted_column_is_refused_by_name(self, world):
        """`state` is a governed transition and `handle` is identity; neither is
        a setting. The allowlist is what keeps them out of a settings write."""
        for bad in ({"state": "disabled"}, {"handle": "someone_else"}):
            assert (
                refused(
                    world,
                    "account_settings_change",
                    ig_account_id=str(world["a"]["iga"]),
                    settings=bad,
                )
                == "invalid_args"
            ), bad

    def test_a_bool_is_not_an_integer_and_an_empty_map_is_refused(self, world):
        """`True` IS an int in Python and would land as `posts_per_day = 1`."""
        assert (
            refused(
                world,
                "account_settings_change",
                ig_account_id=str(world["a"]["iga"]),
                settings={"posts_per_day": True},
            )
            == "invalid_args"
        )
        assert (
            refused(
                world,
                "account_settings_change",
                ig_account_id=str(world["a"]["iga"]),
                settings={},
            )
            == "invalid_args"
        )

    def test_the_database_check_decides_the_value(self, world):
        """The allowlist bounds the KEYS; `ck_iga_ppd` bounds the values, and a
        check_violation on the account table has to surface as a refusal rather
        than a 500 — the same translation the workspace writer relies on."""
        assert (
            refused(
                world,
                "account_settings_change",
                ig_account_id=str(world["a"]["iga"]),
                settings={"posts_per_day": 0},
            )
            == "invalid_args"
        )
        assert _acct(world)["posts_per_day"] != 0

    def test_the_change_is_audited(self, world):
        """`055`'s trigger early-exits only for `next_slot_at`/`last_posted_at`,
        so these four columns audit already — the trail this command needs
        exists in the schema and is asserted rather than assumed."""
        (before,) = _one(
            world,
            "SELECT count(*) FROM audit_events"
            " WHERE workspace_id = %s AND entity_id = %s",
            (str(world["a"]["ws"]), str(world["a"]["iga"])),
        )
        run(
            world,
            "account_settings_change",
            ig_account_id=str(world["a"]["iga"]),
            settings={"posting_hours_start": 9, "posting_hours_end": 17},
        )
        (after,) = _one(
            world,
            "SELECT count(*) FROM audit_events"
            " WHERE workspace_id = %s AND entity_id = %s",
            (str(world["a"]["ws"]), str(world["a"]["iga"])),
        )
        assert after > before

    def test_two_accounts_in_one_workspace_diverge(self, world):
        """**The first time `n >= 2` has been driven anywhere.**

        `uq_ig_account_live` keys on `(workspace_id, provider_account_ref)` so
        two accounts were always LEGAL, and `fn_clock_tick` resolves the ladder
        per row so they were always structurally able to differ. Both of those
        are reads of the schema. Until this test nothing had ever put a second
        row in one workspace and made the two resolve differently, which is the
        whole point of the command and the X.3 gate's precondition.

        It is still not driven in PRODUCTION — no second account exists on any
        real workspace, and this creates one only inside the scratch database.
        """
        (second,) = _migrate(
            world["stream"],
            "INSERT INTO ig_accounts (workspace_id, provider_account_ref, handle)"
            " VALUES (%s, %s, %s) RETURNING id",
            (str(world["a"]["ws"]), "manual:second_account", "second_account"),
        )
        run(
            world,
            "account_settings_change",
            ig_account_id=str(world["a"]["iga"]),
            settings={"posts_per_day": 2, "tz": "America/New_York"},
        )
        run(
            world,
            "account_settings_change",
            ig_account_id=str(second),
            settings={"posts_per_day": 9, "tz": "Europe/London"},
        )
        first_ppd, first_tz = _one(
            world,
            "SELECT COALESCE(a.posts_per_day, w.posts_per_day),"
            "       COALESCE(a.tz, w.tz)"
            "  FROM ig_accounts a JOIN workspaces w ON w.id = a.workspace_id"
            " WHERE a.id = %s",
            (str(world["a"]["iga"]),),
        )
        second_ppd, second_tz = _one(
            world,
            "SELECT COALESCE(a.posts_per_day, w.posts_per_day),"
            "       COALESCE(a.tz, w.tz)"
            "  FROM ig_accounts a JOIN workspaces w ON w.id = a.workspace_id"
            " WHERE a.id = %s",
            (str(second),),
        )
        assert (first_ppd, first_tz) == (2, "America/New_York")
        assert (second_ppd, second_tz) == (9, "Europe/London")

        # And one of them can go back to inheriting while the other does not —
        # the two rows are independent in BOTH directions, not merely settable.
        run(
            world,
            "account_settings_change",
            ig_account_id=str(second),
            settings={"posts_per_day": None},
        )
        (ws_default,) = _one(
            world,
            "SELECT posts_per_day FROM workspaces WHERE id = %s",
            (str(world["a"]["ws"]),),
        )
        (second_after,) = _one(
            world,
            "SELECT COALESCE(a.posts_per_day, w.posts_per_day)"
            "  FROM ig_accounts a JOIN workspaces w ON w.id = a.workspace_id"
            " WHERE a.id = %s",
            (str(second),),
        )
        (first_after,) = _one(
            world,
            "SELECT COALESCE(a.posts_per_day, w.posts_per_day)"
            "  FROM ig_accounts a JOIN workspaces w ON w.id = a.workspace_id"
            " WHERE a.id = %s",
            (str(world["a"]["iga"]),),
        )
        assert second_after == ws_default
        assert first_after == 2, "clearing one override must not touch the other"
