"""X.3 — `06` §1's offboarding workflow against a real database (#1090 H1).

`fn_offboard_finalize` has existed since `059` and `offboard_workspace` has
been in `ck_jobs_kind` since `056`; neither had a caller, and the only test
touching the door asserted its REFUSAL. Nothing in the suite had ever deleted a
workspace. So the load-bearing assertion here is the one nobody had made: the
happy path runs, the row goes, and the cascade takes the tenant's data with it.

Three properties need a real database and cannot be faked:

* **The cascade.** `ON DELETE CASCADE` is the mechanism `06` §1 relies on for
  "everything workspace-keyed dies", and a mock cannot cascade.
* **The job erasing itself.** `jobs.workspace_id` cascades too, so finalizing
  deletes the very row the loop is holding a lease on. That is what forces the
  finalize leg into its own committed transaction, and only a real transaction
  demonstrates the rollback it avoids.
* **The parked drain's audit row.** It is written on the path that raises, so
  the claim is precisely that it survives a rollback — untestable without one.

The transit seam is faked (it is a provider); the database is not.
"""

from __future__ import annotations

import asyncio
import uuid

import psycopg2
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.services.target import commands
from src.services.target.commands import Command, CommandRefused
from src.services.target.offboarding import (
    TERMINAL_STATES,
    DrainTimedOut,
    execute_offboard,
)
from src.services.target.unit_of_work import asyncpg_url, unit_of_work
from src.services.target.work_loop import WorkerConfig, WorkerDeps
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
def off_db(admin_conn, owner_actor):
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        owner_stream = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        yield {
            "owner_stream": owner_stream,
            "ingress": as_user(db, "svc_ingress"),
            "worker": as_user(db, "svc_worker"),
        }
    finally:
        gen.close()


def _migrate(dsn: str, sql: str, params=()):
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            cur.execute(sql, params)
            row = cur.fetchone() if cur.description else None
        conn.commit()
        return row
    finally:
        conn.close()


@pytest.fixture
def ws(off_db):
    """A fresh workspace chain per test. Offboarding is destructive and
    terminal, so a module-scoped tenant would let the first finalize delete the
    subject of every test after it."""
    conn = psycopg2.connect(off_db["owner_stream"])
    try:
        chain = seed_workspace_chain(conn, f"off-{uuid.uuid4().hex[:8]}")
    finally:
        conn.close()
    return {k: str(v) for k, v in chain.items()}


def _one(off_db, sql, params=()):
    return fetch_one(off_db["owner_stream"], sql, params)


def _intent(off_db, ws, state="awaiting_approval", transit=None):
    (media,) = _migrate(
        off_db["owner_stream"],
        "INSERT INTO media_items (workspace_id, source_id, content_hash, file_name,"
        " media_kind, provider_file_ref) VALUES (%s, %s, %s, 'f.jpg', 'image', %s)"
        " RETURNING id",
        (ws["ws"], ws["src"], f"h-{uuid.uuid4().hex[:8]}", f"r-{uuid.uuid4().hex[:6]}"),
    )
    # A publishing/ambiguous row is not free-form: `ck_ambiguous_called` pins
    # `publish_step='publish_called'` and `ck_publishing_debited` requires the
    # cap already debited, because ambiguity exists only AFTER the permit
    # committed. Seeding one without both is a row the schema refuses.
    draining = state in ("publishing", "publishing_ambiguous")
    (intent,) = _migrate(
        off_db["owner_stream"],
        "INSERT INTO post_intents (workspace_id, ig_account_id, media_item_id,"
        " provider_account_ref, approval_mode, schedule_slot_at, state,"
        " transit_asset_ref, publish_step, cap_consumed_on)"
        " VALUES (%s, %s, %s, %s, 'manual', now(), %s, %s, %s, %s) RETURNING id",
        (
            ws["ws"],
            ws["iga"],
            media,
            f"acct-{uuid.uuid4().hex[:6]}",
            state,
            transit,
            "publish_called" if draining else "none",
            "today" if draining else None,
        ),
    )
    return str(intent)


def _credential(off_db, ws):
    (cid,) = _migrate(
        off_db["owner_stream"],
        "INSERT INTO oauth_credentials (workspace_id, media_source_id, provider,"
        " state, encrypted_payload) VALUES (%s, %s, 'gdrive', 'active', %s)"
        " RETURNING id",
        (ws["ws"], ws["src"], b"x"),
    )
    return str(cid)


# --- the command: `06` §1's entry edge ---------------------------------------


async def _command(dsn: str, ws: str, user: str, args: dict):
    engine = create_async_engine(asyncpg_url(dsn), poolclass=NullPool)
    try:
        uow = unit_of_work(
            engine, ws, actor_kind="user", actor_user_id=user, channel="web"
        )
        async with uow.begin() as session:
            assert (
                await session.execute(text("SELECT current_user"))
            ).scalar() == "svc_ingress"
            return await commands.execute(
                session,
                Command(
                    kind="offboard_workspace",
                    workspace_id=ws,
                    actor_user_id=user,
                    channel="web",
                    args=args,
                ),
            )
    finally:
        await engine.dispose()


def offboard(off_db, ws, **args):
    return asyncio.run(_command(off_db["ingress"], ws["ws"], ws["user"], args))


class TestTheCommand:
    def test_confirm_is_required_and_its_absence_writes_nothing(self, off_db, ws):
        """`06` §1: "owner (explicit, confirmed)". This is the one command whose
        effect is irreversible after the grace window, so an empty POST body
        must not start it."""
        for args in ({}, {"confirm": "yes"}, {"confirm": False}):
            with pytest.raises(CommandRefused) as err:
                offboard(off_db, ws, **args)
            assert err.value.reason == "invalid_args"
        assert _one(
            off_db,
            "SELECT state, offboarding_at FROM workspaces WHERE id = %s",
            (ws["ws"],),
        ) == ("active", None)
        assert (
            _one(
                off_db, "SELECT count(*) FROM jobs WHERE workspace_id = %s", (ws["ws"],)
            )[0]
            == 0
        )

    def test_starts_the_workflow_and_stamps_the_grace_anchor(self, off_db, ws):
        out = offboard(off_db, ws, confirm=True)
        assert out.outcome == "enqueued" and out.data["state"] == "offboarding"
        row = _one(
            off_db,
            "SELECT state, offboarding_at IS NOT NULL FROM workspaces WHERE id = %s",
            (ws["ws"],),
        )
        assert row == ("offboarding", True)
        job = _one(
            off_db,
            "SELECT kind, state, lane, serialization_key, workspace_id::text"
            "  FROM jobs WHERE id = %s",
            (out.data["job_id"],),
        )
        assert job == (
            "offboard_workspace",
            "ready",
            "bulk",
            f"ws:{ws['ws']}",
            ws["ws"],
        )

    def test_a_second_offboard_is_refused_and_does_not_move_the_deletion_date(
        self, off_db, ws
    ):
        """Re-stamping `offboarding_at` would silently restart a 30-day clock
        the owner believes is already running."""
        offboard(off_db, ws, confirm=True)
        first = _one(
            off_db, "SELECT offboarding_at FROM workspaces WHERE id = %s", (ws["ws"],)
        )[0]
        with pytest.raises(CommandRefused) as err:
            offboard(off_db, ws, confirm=True)
        assert err.value.reason == "illegal_transition"
        assert (
            _one(
                off_db,
                "SELECT offboarding_at FROM workspaces WHERE id = %s",
                (ws["ws"],),
            )[0]
            == first
        )


# --- the job: the five legs --------------------------------------------------


class _Transit:
    """Matches `CloudinaryTransit.destroy`'s SIGNATURE, not the caller's
    convenience — keyword-only `media_kind`, bool return, "gone NOW" semantics.

    The first version of this fake took a bare ref positionally, which is what
    the first version of the caller passed. It agreed with the code under test
    instead of with the provider, so a leg that could never have worked in
    production passed every assertion here."""

    def __init__(self, refuse=(), raise_on=()):
        self.destroyed = []
        self._refuse = set(refuse)
        self._raise_on = set(raise_on)

    async def destroy(self, transit_asset_ref: str, *, media_kind: str) -> bool:
        assert media_kind in ("image", "video"), media_kind
        if transit_asset_ref in self._raise_on:
            raise RuntimeError("provider said no")
        if transit_asset_ref in self._refuse:
            return False
        self.destroyed.append(transit_asset_ref)
        return True


def _deps(off_db, *, transit, grace, drain_timeout):
    engine = create_async_engine(asyncpg_url(off_db["worker"]), poolclass=NullPool)
    return WorkerDeps(
        engine=engine,
        transit=transit,
        config=WorkerConfig(
            offboard_grace_seconds=grace,
            offboard_drain_timeout_seconds=drain_timeout,
            offboard_drain_recheck_seconds=30,
        ),
    )


def run_job(off_db, ws, *, transit=None, grace=0, drain_timeout=15 * 60):
    """One `offboard_workspace` run, as `svc_worker`, in the shape the work
    loop supplies: the loop's session, a claimed job row."""
    job = _one(
        off_db,
        "SELECT id::text FROM jobs WHERE workspace_id = %s"
        "   AND kind = 'offboard_workspace' AND state = 'ready'"
        " ORDER BY run_at LIMIT 1",
        (ws["ws"],),
    )
    assert job is not None, "no ready offboard job to run"

    async def _go():
        deps = _deps(off_db, transit=transit, grace=grace, drain_timeout=drain_timeout)
        try:
            uow = unit_of_work(deps.engine, ws["ws"], actor_kind="system")
            async with uow.begin() as session:
                assert (
                    await session.execute(text("SELECT current_user"))
                ).scalar() == "svc_worker"
                return await execute_offboard(
                    deps, session, {"id": job[0], "workspace_id": ws["ws"]}
                )
        finally:
            await deps.engine.dispose()

    return asyncio.run(_go())


class TestTheWorkflow:
    def test_the_first_run_drains_revokes_and_schedules_the_finalizer(self, off_db, ws):
        live = _intent(off_db, ws, transit="cloudinary/abc")
        credential = _credential(off_db, ws)
        offboard(off_db, ws, confirm=True)
        transit = _Transit()

        out = run_job(off_db, ws, transit=transit, grace=3600)

        assert out["outcome"] == "drained"
        # The seeded chain carries an intent of its own, so the property is
        # "nothing live is left", not a count the fixture can move.
        assert out["refused"] == 0 and out["revoked"] == 1
        assert _one(
            off_db,
            "SELECT count(*) FROM post_intents WHERE workspace_id = %s"
            "   AND NOT (state = ANY(%s))",
            (ws["ws"], list(TERMINAL_STATES)),
        ) == (0,)
        assert transit.destroyed == ["cloudinary/abc"]
        assert (
            _one(off_db, "SELECT state FROM post_intents WHERE id = %s", (live,))[0]
            == "cancelled"
        )
        assert (
            _one(
                off_db,
                "SELECT state FROM oauth_credentials WHERE id = %s",
                (credential,),
            )[0]
            == "revoked"
        )
        assert (
            _one(
                off_db,
                "SELECT transit_asset_ref FROM post_intents WHERE id = %s",
                (live,),
            )[0]
            == "cloudinary/abc"
        ), "the ref stays: leg 1 froze the row, and it dies with the cascade anyway"
        # `06` §1 leg 2: the provider call is a job, not an inline call.
        assert (
            _one(
                off_db,
                "SELECT count(*) FROM jobs WHERE kind = 'revoke_workspace_credentials'"
                "   AND workspace_id = %s",
                (ws["ws"],),
            )[0]
            == 1
        )
        successor = _one(
            off_db,
            "SELECT run_at = (SELECT offboarding_at + interval '3600 seconds'"
            "                   FROM workspaces WHERE id = %s)"
            "  FROM jobs WHERE id = %s",
            (ws["ws"], out["successor"]),
        )
        assert successor == (True,), "the finalizer is scheduled for the window's end"

    def test_a_second_run_of_the_same_legs_changes_nothing(self, off_db, ws):
        """Legs are idempotent because a re-claimed lease reruns them; the rows
        are the progress marker, so there is nothing to get out of step."""
        _intent(off_db, ws)
        _credential(off_db, ws)
        offboard(off_db, ws, confirm=True)
        run_job(off_db, ws, grace=3600)
        before = _one(
            off_db, "SELECT count(*) FROM jobs WHERE workspace_id = %s", (ws["ws"],)
        )
        out = run_job(off_db, ws, grace=3600)
        assert out["outcome"] == "grace"
        assert out["cancelled"] == 0 and out["revoked"] == 0
        assert (
            _one(
                off_db, "SELECT count(*) FROM jobs WHERE workspace_id = %s", (ws["ws"],)
            )
            == before
        ), "no second successor and no second revoke job"

    def test_the_finalize_run_deletes_the_workspace_and_cascades(self, off_db, ws):
        """The assertion nobody in this suite had made: the door's happy path.

        Everything workspace-keyed goes with the cascade; `audit_events`
        survives, by design and without an FK (`02` §0)."""
        _intent(off_db, ws)
        _credential(off_db, ws)
        offboard(off_db, ws, confirm=True)

        # TWO runs, and that is the contract rather than an inconvenience: the
        # first run's cancels live in the loop's open transaction, and the
        # door runs in its own and would not see them.
        first = run_job(off_db, ws, grace=0)
        assert first["outcome"] == "drained"
        out = run_job(off_db, ws, grace=0)

        assert out["outcome"] == "finalized"
        assert _one(
            off_db, "SELECT count(*) FROM workspaces WHERE id = %s", (ws["ws"],)
        ) == (0,)
        # One query rather than six connections, and a dict comparison rather
        # than a loop: the failure output then NAMES every table that outlived
        # the cascade instead of stopping at the first.
        tables = (
            "post_intents",
            "media_items",
            "media_sources",
            "ig_accounts",
            "oauth_credentials",
            "workspace_members",
        )
        counts = _one(
            off_db,
            "SELECT "
            + ", ".join(
                f"(SELECT count(*) FROM {t} WHERE workspace_id = %s)" for t in tables
            ),
            (ws["ws"],) * len(tables),
        )
        assert dict(zip(tables, counts)) == dict.fromkeys(tables, 0)
        assert (
            _one(
                off_db,
                "SELECT count(*) FROM audit_events WHERE workspace_id = %s",
                (ws["ws"],),
            )[0]
            > 0
        ), "audit outlives the tenant (`02` §0 exception)"

    def test_the_job_row_is_erased_by_the_cascade_it_triggers(self, off_db, ws):
        """Why the finalize leg owns its transaction. `jobs.workspace_id`
        cascades, so the row the loop holds a lease on disappears mid-run; had
        the DELETE ridden the loop's session, the loop's own `finalize_job`
        would raise `JobFenced` inside it and roll the deletion back."""
        offboard(off_db, ws, confirm=True)
        job = _one(
            off_db,
            "SELECT id::text FROM jobs WHERE workspace_id = %s AND kind = 'offboard_workspace'",
            (ws["ws"],),
        )[0]
        run_job(off_db, ws, grace=0)  # drains; mints the finalizer
        run_job(off_db, ws, grace=0)  # finalizes, erasing both job rows
        assert _one(off_db, "SELECT count(*) FROM jobs WHERE id = %s", (job,)) == (0,)

    def test_a_restore_inside_the_window_stops_the_workflow(self, off_db, ws):
        """`06` §1 allows the owner back out within the grace window. The job
        must not delete a workspace that is no longer offboarding."""
        offboard(off_db, ws, confirm=True)
        _migrate(
            off_db["owner_stream"],
            "UPDATE workspaces SET state = 'active', offboarding_at = NULL WHERE id = %s",
            (ws["ws"],),
        )
        out = run_job(off_db, ws, grace=0)
        assert out == {"outcome": "not_offboarding", "state": "active"}
        assert _one(
            off_db, "SELECT count(*) FROM workspaces WHERE id = %s", (ws["ws"],)
        ) == (1,)

    def test_an_already_finalized_workspace_is_not_an_error(self, off_db, ws):
        offboard(off_db, ws, confirm=True)
        run_job(off_db, ws, grace=0)
        run_job(off_db, ws, grace=0)

        async def _go():
            deps = _deps(off_db, transit=None, grace=0, drain_timeout=15 * 60)
            try:
                uow = unit_of_work(deps.engine, ws["ws"], actor_kind="system")
                async with uow.begin() as session:
                    return await execute_offboard(
                        deps,
                        session,
                        {"id": str(uuid.uuid4()), "workspace_id": ws["ws"]},
                    )
            finally:
                await deps.engine.dispose()

        assert asyncio.run(_go()) == {"outcome": "already_finalized"}


class TestTheDrainPark:
    """`06` §1 leg 1: "A drain that times out parks the leg and alerts the
    operator RATHER THAN revoking under live work." R3 §3.4 found the pass-2
    ordering did the opposite and destroyed the reconciliation evidence."""

    def _publishing(self, off_db, ws):
        _intent(off_db, ws, state="publishing_ambiguous")
        return _credential(off_db, ws)

    def test_inside_the_timeout_it_waits_and_leaves_credentials_alive(self, off_db, ws):
        credential = self._publishing(off_db, ws)
        offboard(off_db, ws, confirm=True)
        out = run_job(off_db, ws, grace=0, drain_timeout=900)
        assert out["outcome"] == "draining" and out["publishing"] == 1
        assert (
            _one(
                off_db,
                "SELECT state FROM oauth_credentials WHERE id = %s",
                (credential,),
            )[0]
            == "active"
        ), "credentials must stay alive while publishing work drains"
        assert _one(
            off_db, "SELECT count(*) FROM workspaces WHERE id = %s", (ws["ws"],)
        ) == (1,)
        assert out["successor"], "a recheck is scheduled rather than the job dying"

    def test_past_the_timeout_it_parks_loudly_and_the_record_survives_the_raise(
        self, off_db, ws
    ):
        """The audit row is written on the path that RAISES, so the claim under
        test is that it survives the rollback — which it only does because it is
        committed in its own transaction."""
        credential = self._publishing(off_db, ws)
        offboard(off_db, ws, confirm=True)
        with pytest.raises(DrainTimedOut):
            run_job(off_db, ws, grace=0, drain_timeout=0)
        assert (
            _one(
                off_db,
                "SELECT state FROM oauth_credentials WHERE id = %s",
                (credential,),
            )[0]
            == "active"
        ), "the park must not revoke under live work"
        assert _one(
            off_db, "SELECT count(*) FROM workspaces WHERE id = %s", (ws["ws"],)
        ) == (1,)
        assert _one(
            off_db,
            "SELECT count(*) FROM audit_events WHERE workspace_id = %s"
            "   AND detail->>'event' = 'offboard_drain_timeout'",
            (ws["ws"],),
        ) == (1,)


class TestTheTransitSeam:
    def test_with_no_transit_store_the_refs_are_reported_not_silently_skipped(
        self, off_db, ws
    ):
        """`06` §1 names the FC-3.6 TTL sweep as the backstop, so a missing seam
        delays nothing — but a skip nobody can see is how a backstop becomes an
        assumption."""
        _intent(off_db, ws, transit="cloudinary/orphan")
        offboard(off_db, ws, confirm=True)
        out = run_job(off_db, ws, transit=None, grace=3600)
        assert out["transit"] == {"reaped": 0, "left_to_ttl": 1, "seam": "absent"}

    def test_a_refused_delete_is_counted_and_does_not_abort_the_others(
        self, off_db, ws
    ):
        """Best-effort means the sweep continues. One provider refusal must not
        cost the other assets their reap, nor the offboard its progress."""
        _intent(off_db, ws, transit="cloudinary/refused")
        _intent(off_db, ws, transit="cloudinary/raised")
        _intent(off_db, ws, transit="cloudinary/fine")
        offboard(off_db, ws, confirm=True)
        # Both provider failure shapes: `destroy` answers False for a polite
        # refusal and raises for anything else. Counting only the exception
        # would report a refusal as a successful reap.
        transit = _Transit(
            refuse={"cloudinary/refused"}, raise_on={"cloudinary/raised"}
        )
        out = run_job(off_db, ws, transit=transit, grace=3600)
        assert out["transit"] == {"reaped": 1, "left_to_ttl": 2, "seam": "wired"}
        assert transit.destroyed == ["cloudinary/fine"]
        assert out["outcome"] == "drained", (
            "a refused asset does not stall the workflow"
        )
