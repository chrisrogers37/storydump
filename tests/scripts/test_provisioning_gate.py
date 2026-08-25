"""Provisioning, EXECUTED — as `svc_ingress`, on the replayed target schema,
with the real clock door as the judge (#1041, destination half).

**What this file is for, stated so its green cannot be read as more than it
is.** The claim behind this build is not "an INSERT works". It is that a
destination row is *what turns `plan_slot` from built-and-dead into running*.
An INSERT test cannot tell those apart: `ig_accounts` accepts a row with
`next_slot_at` NULL perfectly happily, and `fn_clock_tick` will then never look
at it again. So the assertions below end at the CLOCK — `fn_clock_tick` is
called for real and the `plan_slot` jobs it mints are counted.

The negative control is not synthetic and did not have to be built: the shared
fixture (`conftest.seed_workspace_chain`) already creates an `ig_accounts` row
by raw INSERT, exactly the way every test in the repo has had to until now, and
that row is invisible to the clock forever. It is asserted here as the
before-state, so the "after" is attributable to the writer rather than to the
tick being called at all.

Roles: writes go through `svc_ingress`, the tick runs as `svc_worker` (the only
login `fn_clock_tick` is granted to), and ground truth is read back as the
schema owner. A single-role version of this file would prove the statements
run, not that a least-privilege identity may run them.

**`svc_ingress` is the role the F.4 posture INTENDS, not the one production
connects as, and the tenant-isolation assertions here therefore do not transfer
to the deployed configuration.** Measured on production: the API connects as
`neondb_owner`, which owns all of `ig_accounts`, `media_sources`, `workspaces`
and `workspace_members` and carries BYPASSRLS; no migration sets FORCE ROW
LEVEL SECURITY, and nothing in `src/` issues `SET ROLE`. So `p_tenant` is inert
on the deployed path and `test_a_source_is_not_visible_from_the_other_tenant`
(and its destination sibling) prove a property of `svc_ingress`, not of
production. This is ratified, not a new hole — `02-domain-model.md`:1466 allows
ENABLE without FORCE and #751 tracks the gap, whose compensating control is the
unbuilt F.4 ("runtime-login + definer-door, no owner role, no BYPASSRLS").

What does hold in production is `tenant_resolution.authorize_member`, which
binds both keys in its WHERE and documents itself as safe on a privileged
connection. Isolation on these two endpoints rests on that gate, not on RLS.
Running this file under `svc_ingress` is still worth doing — it is the only
place the intended posture is exercised at all — but it must not be read as
evidence about the running system.

**Not claimed here:** nothing about credentials, Meta, or publishing. A
destination is not a connection — see `provisioning`'s module docstring.
`api_publishing_enabled` is false on these workspaces, as it is by default, so
the intents this schedules are the manual-approval kind and no code path in
this file can reach Instagram.
"""

from __future__ import annotations

import asyncio
import uuid

import psycopg2
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.services.target import provisioning
from src.services.target.provisioning import ProvisioningRefused
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

#: `fn_clock_tick`'s recurring-singleton seam, deliberately EMPTY. The tick
#: mints recurring system jobs too, and this file is about `plan_slot`; an
#: empty registry keeps the counts it returns unambiguous.
NO_RECURRING = '{"v": 1}'
REFRESH_CADENCE = "30 days"


@pytest.fixture(scope="module")
def world(admin_conn, owner_actor):
    """Replayed schema, two seeded workspaces.

    Two, not one, for the reason every tenancy assertion in this tier needs
    two: "the row landed in workspace A" is only a claim if there was a
    workspace B it could have landed in instead.
    """
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        stream = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        conn = psycopg2.connect(stream)
        try:
            a = seed_workspace_chain(conn, "prov-a")
            b = seed_workspace_chain(conn, "prov-b")
        finally:
            conn.close()
        yield {
            "stream": stream,
            "ingress": as_user(db, "svc_ingress"),
            "worker": as_user(db, "svc_worker"),
            "a": a,
            "b": b,
        }
    finally:
        gen.close()


# --- drivers -----------------------------------------------------------------


async def _in_tenant(dsn: str, ws: str, user: str, fn):
    """Run *fn(session)* in one committed unit of work as `svc_ingress`.

    The role is ASSERTED rather than assumed: a fixture that quietly connected
    as the owner would bypass RLS, and every isolation claim below would be
    vacuous while still reading green.
    """
    engine = create_async_engine(asyncpg_url(dsn), poolclass=NullPool)
    try:
        uow = unit_of_work(
            engine, ws, actor_kind="user", actor_user_id=user, channel="web"
        )
        async with uow.begin() as session:
            who = (await session.execute(text("SELECT current_user"))).scalar()
            assert who == "svc_ingress", who
            return await fn(session)
    finally:
        await engine.dispose()


def destination(world, *, ids=None, ref: str, handle=None, schedule=True):
    ids = ids or world["a"]
    return asyncio.run(
        _in_tenant(
            world["ingress"],
            str(ids["ws"]),
            str(ids["user"]),
            lambda s: provisioning.create_destination(
                s,
                workspace_id=str(ids["ws"]),
                provider_account_ref=ref,
                handle=handle,
                schedule=schedule,
            ),
        )
    )


def source(world, *, ids=None, folder: str, root_name=None):
    ids = ids or world["a"]
    return asyncio.run(
        _in_tenant(
            world["ingress"],
            str(ids["ws"]),
            str(ids["user"]),
            lambda s: provisioning.get_or_create_media_source(
                s,
                workspace_id=str(ids["ws"]),
                folder_ref=folder,
                root_name=root_name,
            ),
        )
    )


def tick(world, *, budget: int = 50) -> int:
    """One real `fn_clock_tick` as `svc_worker`. Returns `o_slot_jobs`."""
    row = fetch_one(
        world["worker"],
        "SELECT o_slot_jobs FROM fn_clock_tick(%s, %s::interval, %s::jsonb)",
        (budget, REFRESH_CADENCE, NO_RECURRING),
    )
    return int(row[0])


def _owner(world, sql: str, params=()):
    return fetch_one(world["stream"], sql, params)


def _make_due(world, account_id: str) -> None:
    """Back-date the cursor so the next tick finds this account due.

    This does NOT weaken the claim, and the distinction matters: the writer's
    job is to put a real value in `next_slot_at`, and `fn_next_slot` correctly
    returns a FUTURE slot — a destination created at 10:00 is due at its next
    posting slot, not immediately. Back-dating tests reachability given a
    value. The rows that have NO value are tested unchanged, and no amount of
    waiting or back-dating can help them, which is exactly the point.
    """
    conn = psycopg2.connect(world["stream"])
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            cur.execute(
                "UPDATE ig_accounts SET next_slot_at = now() - interval '1 minute'"
                " WHERE id = %s",
                (account_id,),
            )
        conn.commit()
    finally:
        conn.close()


def _ref(tag: str) -> str:
    return f"17841{uuid.uuid4().hex[:12]}-{tag}"


# --- the before-state: what a raw INSERT leaves behind ------------------------


class TestTheGapThisCloses:
    def test_the_fixtures_own_account_is_invisible_to_the_clock(self, world):
        """The negative control, and it is not synthetic.

        `conftest.seed_intent_chain` creates its `ig_accounts` row by raw
        INSERT — the only way anything in this repo could, before this module.
        That row has a NULL cursor, so `fn_clock_tick` cannot see it however
        often it runs. This is the state `main` shipped in, asserted rather
        than described.
        """
        row = _owner(
            world,
            "SELECT next_slot_at FROM ig_accounts WHERE id = %s",
            (str(world["a"]["iga"]),),
        )
        assert row[0] is None

        # And the clock agrees: with only NULL-cursor accounts present, a real
        # tick mints nothing at all.
        assert tick(world) == 0


# --- destinations ------------------------------------------------------------


class TestADestinationIsScheduledIntoExistence:
    def test_the_row_lands_with_a_seeded_cursor(self, world):
        ref = _ref("lands")
        account_id, created = destination(world, ref=ref, handle="thehandle")
        assert created is True

        row = _owner(
            world,
            "SELECT workspace_id, provider_account_ref, handle, state, next_slot_at"
            "  FROM ig_accounts WHERE id = %s",
            (account_id,),
        )
        assert str(row[0]) == str(world["a"]["ws"])
        assert row[1] == ref
        assert row[2] == "thehandle"
        assert row[3] == "active"
        # THE assertion this whole build exists for.
        assert row[4] is not None, "next_slot_at was not seeded — plan_slot stays dead"

    def test_the_seeded_cursor_is_a_real_slot_not_a_sentinel(self, world):
        """Within a day, and in the future.

        A writer that "seeded" the cursor with `now()` or epoch would pass a
        NOT NULL check and then post immediately or never, so the value is
        asserted to be a plausible slot rather than merely present.
        """
        account_id, _ = destination(world, ref=_ref("slot"))
        row = _owner(
            world,
            "SELECT next_slot_at > now() AS future,"
            "       next_slot_at < now() + interval '1 day' AS soon"
            "  FROM ig_accounts WHERE id = %s",
            (account_id,),
        )
        assert row[0] is True, "the seeded slot is in the past"
        assert row[1] is True, "the seeded slot is more than a day out"

    def test_the_clock_mints_a_plan_slot_job_for_it(self, world):
        """END TO END: the destination is what makes `plan_slot` reachable.

        Before, the tick mints nothing (the class above). Here the same real
        door, called the same way, mints exactly one job — and the job names
        THIS account, so the count is not being satisfied by something else in
        the database.
        """
        ref = _ref("tick")
        account_id, _ = destination(world, ref=ref)
        _make_due(world, account_id)

        assert tick(world) == 1

        row = _owner(
            world,
            "SELECT kind, lane, serialization_key, payload->>'ig_account_id'"
            "  FROM jobs WHERE payload->>'ig_account_id' = %s",
            (account_id,),
        )
        assert row[0] == "plan_slot"
        assert row[1] == "bulk"
        assert row[2] == f"acct:{account_id}"

    def test_the_cursor_advances_rather_than_repeating(self, world):
        """A tick that did not advance the cursor would mint a plan_slot job
        every tick forever. The advance is `fn_clock_tick`'s, not this
        module's, but a destination that could not be advanced would be a
        destination this writer seeded wrongly."""
        account_id, _ = destination(world, ref=_ref("advance"))
        _make_due(world, account_id)
        assert tick(world) == 1
        # Immediately again: the cursor moved forward, so nothing is due.
        assert tick(world) == 0

    def test_schedule_false_leaves_it_permanently_invisible(self, world):
        """The opt-out, and the proof that seeding is the load-bearing part.

        A parked destination is a real row that the clock can never reach —
        which is the same state the raw-INSERT rows are in, chosen on purpose
        this time.
        """
        account_id, created = destination(world, ref=_ref("parked"), schedule=False)
        assert created is True
        row = _owner(
            world, "SELECT next_slot_at FROM ig_accounts WHERE id = %s", (account_id,)
        )
        assert row[0] is None
        assert tick(world) == 0


class TestDestinationsAreIdempotentAndTenantBound:
    def test_the_same_handle_twice_is_one_destination(self, world):
        """Two rows for one real Instagram feed would be two schedules posting
        to it — the failure this idempotency exists to prevent, not a tidiness
        preference."""
        ref = _ref("twice")
        first, created_first = destination(world, ref=ref)
        second, created_second = destination(world, ref=ref)
        assert created_first is True
        assert created_second is False
        assert first == second

        row = _owner(
            world,
            "SELECT count(*) FROM ig_accounts"
            " WHERE workspace_id = %s AND provider_account_ref = %s",
            (str(world["a"]["ws"]), ref),
        )
        assert row[0] == 1

    def test_a_repeat_does_not_reseed_the_cursor(self, world):
        """Re-adding an existing destination must not move its schedule. A
        writer that reseeded on conflict would let anyone reset a live posting
        cadence by adding the handle again."""
        ref = _ref("noreseed")
        account_id, _ = destination(world, ref=ref)
        before = _owner(
            world, "SELECT next_slot_at FROM ig_accounts WHERE id = %s", (account_id,)
        )[0]
        _make_due(world, account_id)
        destination(world, ref=ref)
        after = _owner(
            world, "SELECT next_slot_at FROM ig_accounts WHERE id = %s", (account_id,)
        )[0]
        assert after != before, "sanity: the back-date should still be in place"
        assert after < before, "the repeat reseeded the cursor"

    def test_a_repeat_does_not_blank_a_handle_we_already_had(self, world):
        ref = _ref("keephandle")
        account_id, _ = destination(world, ref=ref, handle="original")
        destination(world, ref=ref, handle=None)
        row = _owner(
            world, "SELECT handle FROM ig_accounts WHERE id = %s", (account_id,)
        )
        assert row[0] == "original"

    def test_the_same_handle_in_another_workspace_is_another_destination(self, world):
        """Fork PA-1's default (a): independent connections. The same real
        account in two workspaces is two rows, and this pins that the writer
        implements the DDL's choice rather than a global uniqueness nobody
        asked for."""
        ref = _ref("shared")
        in_a, _ = destination(world, ids=world["a"], ref=ref)
        in_b, _ = destination(world, ids=world["b"], ref=ref)
        assert in_a != in_b

    def test_a_destination_is_not_visible_from_the_other_tenant(self, world):
        """RLS, asserted through the production role rather than assumed."""
        ref = _ref("isolated")
        account_id, _ = destination(world, ids=world["a"], ref=ref)

        async def read(session):
            return (
                await session.execute(
                    text("SELECT count(*) FROM ig_accounts WHERE id = :id"),
                    {"id": account_id},
                )
            ).scalar()

        seen = asyncio.run(
            _in_tenant(
                world["ingress"],
                str(world["b"]["ws"]),
                str(world["b"]["user"]),
                read,
            )
        )
        assert seen == 0


class TestDestinationRefusals:
    @pytest.mark.parametrize("bad", ["", "   ", None, 17841, {"a": 1}])
    def test_a_missing_reference_is_refused_by_name(self, world, bad):
        with pytest.raises(ProvisioningRefused) as exc:
            destination(world, ref=bad)
        assert exc.value.reason == "account_ref_required"

    def test_an_absurd_reference_is_refused_by_name(self, world):
        with pytest.raises(ProvisioningRefused) as exc:
            destination(world, ref="1" * 300)
        assert exc.value.reason == "account_ref_too_long"

    def test_surrounding_whitespace_does_not_fork_a_destination(self, world):
        """`uq_ig_account_live` is exact, so `"123 "` and `"123"` would be two
        destinations for one feed. Proven at the DATABASE, not just in the
        parser: the second call must find the first row."""
        ref = _ref("trim")
        first, _ = destination(world, ref=ref)
        second, created = destination(world, ref=f"  {ref}  ")
        assert second == first
        assert created is False


# --- sources -----------------------------------------------------------------


class TestSourcesLandInTheShapeTheSchemaDocuments:
    def test_the_row_lands_with_the_documented_config(self, world):
        folder = f"folder-{uuid.uuid4().hex[:8]}"
        source_id, created = source(world, folder=folder, root_name="Stories")
        assert created is True
        row = _owner(
            world,
            "SELECT provider, config->>'v', config->>'folder_ref',"
            "       config->>'root_name', state, next_sync_at"
            "  FROM media_sources WHERE id = %s",
            (source_id,),
        )
        assert row[0] == "gdrive"
        assert row[1] == "1"
        assert row[2] == folder
        assert row[3] == "Stories"
        assert row[4] == "active"
        # Not due: the Drive seam (#982) is what schedules a first ingest, and
        # a source that queued a sync nothing can run would park a job forever.
        assert row[5] is None

    def test_a_pasted_folder_url_is_the_same_source_as_its_bare_id(self, world):
        """The parser is load-bearing at the DATABASE, not only in isolation:
        two spellings of one folder must not become two sources ingesting it
        twice."""
        folder = f"folder-{uuid.uuid4().hex[:8]}"
        bare, created_first = source(world, folder=folder)
        pasted, created_second = source(
            world, folder=f"https://drive.google.com/drive/folders/{folder}?usp=sharing"
        )
        assert created_first is True
        assert created_second is False
        assert pasted == bare

    def test_the_same_folder_in_another_workspace_is_another_source(self, world):
        folder = f"folder-{uuid.uuid4().hex[:8]}"
        in_a, _ = source(world, ids=world["a"], folder=folder)
        in_b, _ = source(world, ids=world["b"], folder=folder)
        assert in_a != in_b

    @pytest.mark.parametrize(
        "bad", ["", "   ", None, 12, "https://drive.google.com/drive/folders/"]
    )
    def test_a_missing_folder_is_refused_by_name(self, world, bad):
        with pytest.raises(ProvisioningRefused) as exc:
            source(world, folder=bad)
        assert exc.value.reason == "folder_required"

    def test_a_source_is_not_visible_from_the_other_tenant(self, world):
        folder = f"folder-{uuid.uuid4().hex[:8]}"
        source_id, _ = source(world, ids=world["a"], folder=folder)

        async def read(session):
            return (
                await session.execute(
                    text("SELECT count(*) FROM media_sources WHERE id = :id"),
                    {"id": source_id},
                )
            ).scalar()

        seen = asyncio.run(
            _in_tenant(
                world["ingress"], str(world["b"]["ws"]), str(world["b"]["user"]), read
            )
        )
        assert seen == 0
