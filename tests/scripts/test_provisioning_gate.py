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

from datetime import datetime, timedelta, timezone

import psycopg2
import pytest
from sqlalchemy import text

from src.services.target import provisioning, workspaces
from src.services.target.provisioning import ProvisioningRefused
from tests.scripts.conftest import (
    in_tenant,
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


def destination(world, *, ids=None, ref: str, handle=None, schedule=True):
    ids = ids or world["a"]
    return asyncio.run(
        in_tenant(
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
        in_tenant(
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
            in_tenant(
                world["ingress"],
                str(world["b"]["ws"]),
                str(world["b"]["user"]),
                read,
            )
        )
        assert seen == 0


class TestATypedHandleBecomesADestination:
    """#1089 — the settings form's path. It has no Meta id to send, so the port
    derives a provisional reference. Everything here is about what LANDS."""

    def test_a_handle_alone_creates_a_scheduled_destination(self, world):
        account_id, created = destination(world, ref=None, handle="@TheHandle")
        assert created is True

        row = _owner(
            world,
            "SELECT provider_account_ref, handle, state, next_slot_at"
            "  FROM ig_accounts WHERE id = %s",
            (account_id,),
        )
        # NAMESPACED, not the bare handle: shape (b). Bare, the day OAuth
        # supplies the real Meta id the same feed arrives under a second
        # reference — two destinations, two schedules, one Instagram account.
        assert row[0] == "manual:thehandle"
        # The display column keeps the caller's casing; the `@` does not survive.
        assert row[1] == "TheHandle"
        assert row[2] == "active"
        # Creating a destination is what SCHEDULES it. Without this the clock
        # cannot see the row at all and the whole path is inert.
        assert row[3] is not None, "next_slot_at was not seeded"

    def test_two_spellings_of_one_handle_are_ONE_destination(self, world):
        """The reason `manual_ref_for` case-folds. `uq_ig_account_live` is a
        byte comparison, so without folding this is two schedules against one
        real Instagram feed — the exact failure the index exists to prevent."""
        first, created_first = destination(world, ref=None, handle="@Repeated")
        second, created_second = destination(world, ref=None, handle="repeated")

        assert created_first is True
        assert created_second is False
        assert second == first

    def test_an_explicit_reference_still_wins_over_a_handle(self, world):
        """The OAuth path is untouched: a caller holding a real Meta id is
        never second-guessed, and the handle is stored as the display value."""
        ref = _ref("explicit")
        account_id, _ = destination(world, ref=ref, handle="thehandle")

        row = _owner(
            world,
            "SELECT provider_account_ref, handle FROM ig_accounts WHERE id = %s",
            (account_id,),
        )
        assert row[0] == ref
        assert row[1] == "thehandle"

    def test_a_typed_handle_is_pickable_by_the_clock(self, world):
        """End to end, and the point of the whole build: a destination created
        from a typed handle is one `plan_slot` the dispatcher can mint.

        Asserted against THIS account's own job rather than the tick's count. A
        count is order-coupled to every other tick in the module and cannot tell
        "my row minted" from "somebody's did"."""
        account_id, _ = destination(world, ref=None, handle="clockvisible")
        _make_due(world, account_id)
        tick(world)

        row = _owner(
            world,
            "SELECT count(*) FROM jobs"
            " WHERE kind = 'plan_slot' AND serialization_key = %s",
            (f"acct:{account_id}",),
        )
        assert row[0] == 1, "the clock minted no plan_slot for the typed handle"

    def test_an_explicit_reference_is_not_gated_by_its_display_handle(self, world):
        """REGRESSION (found by review). An earlier revision normalised the
        handle BEFORE choosing a branch, so a decorative display column could
        refuse an identity-bearing create whose identity came from OAuth: this
        call raised `handle_malformed` and wrote nothing."""
        ref = _ref("decorative")
        account_id, created = destination(world, ref=ref, handle="two words")

        assert created is True
        row = _owner(
            world,
            "SELECT provider_account_ref, handle FROM ig_accounts WHERE id = %s",
            (account_id,),
        )
        assert row[0] == ref
        # Stored as given: on this path the handle is decorative and the port
        # does not own its shape.
        assert row[1] == "two words"


class TestDestinationRefusals:
    @pytest.mark.parametrize("bad", ["", "   ", None, 17841, {"a": 1}])
    def test_a_missing_reference_is_refused_by_name(self, world, bad):
        """Unchanged by #1089, and that is the point: with no handle supplied
        every one of these still lands on `account_ref_from`, so a caller who
        supplies nothing sees the reason this gate already pinned."""
        with pytest.raises(ProvisioningRefused) as exc:
            destination(world, ref=bad)
        assert exc.value.reason == "account_ref_required"

    def test_a_malformed_reference_beside_a_handle_is_REFUSED_not_derived(self, world):
        """The branch tests whether a reference was SUPPLIED, never whether it
        is well-formed. An unquoted Meta id is an ordinary JSON mistake, and a
        shape test here would silently derive `manual:<handle>` for a caller who
        plainly meant to send a real id — while refusing a too-long string on
        the same path, two answers to one class of error."""
        with pytest.raises(ProvisioningRefused) as exc:
            destination(world, ref=17841, handle="thehandle")
        assert exc.value.reason == "account_ref_required"

    def test_a_handle_refusal_escapes_before_any_sql_runs(self, world):
        """ONE case, deliberately. `handle_from`'s whole table is pinned at the
        unit tier; what this tier adds is the single fact that a refusal
        propagates out of `create_destination` rather than reaching Postgres,
        and one case proves that."""
        with pytest.raises(ProvisioningRefused) as exc:
            destination(world, ref=None, handle="two words")
        assert exc.value.reason == "handle_malformed"

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
            in_tenant(
                world["ingress"], str(world["b"]["ws"]), str(world["b"]["user"]), read
            )
        )
        assert seen == 0


# --- #1078: the sources payload can say whether a source is CONNECTED ---------


def _sources(world, ids=None):
    ids = ids or world["a"]
    return asyncio.run(
        in_tenant(
            world["ingress"],
            str(ids["ws"]),
            str(ids["user"]),
            lambda s: workspaces.list_sources(s, workspace_id=str(ids["ws"])),
        )
    )


def _credential(
    world, *, source_id, state="active", expires_at=None, ids=None, provider="gdrive"
):
    """Insert one gdrive credential straight in. There is no writer to call for
    this in the target tier yet, which is itself why the status field is worth
    testing rather than assuming."""
    ids = ids or world["a"]

    async def go(s):
        await s.execute(
            text(
                "INSERT INTO oauth_credentials"
                " (workspace_id, media_source_id, provider, encrypted_payload,"
                "  state, expires_at)"
                " VALUES (:ws, :sid, :provider, :payload, :state, :exp)"
            ),
            {
                "ws": str(ids["ws"]),
                "sid": str(source_id),
                "payload": "not-a-real-token",
                "state": state,
                "exp": expires_at,
                "provider": provider,
            },
        )

    asyncio.run(in_tenant(world["ingress"], str(ids["ws"]), str(ids["user"]), go))


class TestSourceCredentialStatus:
    """`media_sources.state` CANNOT answer "is this connected" — a source created
    and never credentialed is `active`, exactly like a healthy one. These pin the
    distinction the caller could not previously make."""

    def test_a_source_with_no_credential_reads_none_not_active(self, world):
        sid, _ = source(world, folder="stat-none")
        row = next(r for r in _sources(world) if str(r["id"]) == str(sid))
        assert row["credential_status"] == "none"
        assert row["credential_connected_at"] is None
        # The trap: the SOURCE is active while the credential does not exist.
        assert row["state"] == "active"

    def test_a_credentialed_source_reads_active_and_carries_a_connected_at(self, world):
        sid, _ = source(world, folder="stat-active")
        _credential(world, source_id=sid)
        row = next(r for r in _sources(world) if str(r["id"]) == str(sid))
        assert row["credential_status"] == "active"
        assert row["credential_connected_at"] is not None

    def test_a_past_expiry_reads_expired_even_though_state_says_active(self, world):
        """The case a passed-through `state` gets WRONG.

        Nothing in the target tier transitions a gdrive credential — only
        `ig_login_oauth` writes `expired`, on the Instagram refresh path — so a
        stored `state` reads `active` forever and "reconnect needed" would never
        appear. The status is derived from the rule `drive_credentials` enforces.
        """
        sid, _ = source(world, folder="stat-expired")
        _credential(
            world,
            source_id=sid,
            state="active",
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        row = next(r for r in _sources(world) if str(r["id"]) == str(sid))
        assert row["credential_status"] == "expired"

    def test_a_null_expiry_is_not_an_expiry(self, world):
        """`drive_credentials` refuses only when `expires_at` HAS PASSED, so an
        unknown expiry must not read as expired."""
        sid, _ = source(world, folder="stat-noexp")
        _credential(world, source_id=sid, state="active", expires_at=None)
        row = next(r for r in _sources(world) if str(r["id"]) == str(sid))
        assert row["credential_status"] == "active"

    def test_a_revoked_credential_reads_revoked_not_none(self, world):
        """Revoked and never-connected are different user actions — reconnect
        versus connect — and collapsing them is the defect this field fixes."""
        sid, _ = source(world, folder="stat-revoked")
        _credential(world, source_id=sid, state="revoked")
        row = next(r for r in _sources(world) if str(r["id"]) == str(sid))
        assert row["credential_status"] == "revoked"

    def test_the_join_does_not_fan_a_source_into_two_rows(self, world):
        """A SECOND credential of another provider must not duplicate the source.

        This fixture is the point: `uq_credential_per_source` is UNIQUE on
        (workspace, source, PROVIDER), and provider equality between a source
        and its credential is service-enforced rather than a constraint (D37) —
        `ck_credentials_one_owner` only counts owner columns, so an `ig_login`
        row hung off a media source is a shape the database accepts. Without
        `AND c.provider = s.provider` in the join, that stray row silently turns
        one source into two in the list.

        An earlier version of this test inserted only the gdrive credential and
        asserted one row. It passed with the provider condition REMOVED — one
        credential cannot fan anything — so it pinned nothing, while the PR body
        claimed it did.
        """
        sid, _ = source(world, folder="stat-onerow")
        _credential(world, source_id=sid)
        _credential(world, source_id=sid, provider="ig_login")
        rows = [r for r in _sources(world) if str(r["id"]) == str(sid)]
        assert len(rows) == 1
        assert rows[0]["credential_status"] == "active"

    def test_no_token_or_envelope_is_ever_returned(self, world):
        sid, _ = source(world, folder="stat-secret")
        _credential(world, source_id=sid)
        row = next(r for r in _sources(world) if str(r["id"]) == str(sid))
        assert "encrypted_payload" not in row
        assert not any("not-a-real-token" == str(v) for v in row.values())
