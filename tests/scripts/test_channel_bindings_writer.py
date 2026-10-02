"""The `channel_bindings` writer, executed against the replayed target schema.

`06`/D13 ratifies `0..n` bindings per workspace and **zero had ever been
written** — no `INSERT INTO channel_bindings` existed anywhere in `src/`. These
drive the writer as `svc_ingress` in a real unit of work and read the effects
back, including the two things that could only be settled by running them: what
the database does when a second workspace reaches for a chat that is taken, and
whether a binding actually un-inerts the delivery chain that reads
`FROM channel_bindings`.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import psycopg2
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from scripts.migration_runner import MIGRATIONS_DIR, split_statements
from src.exceptions.tenancy import TenantResolutionError
from src.services.target import (
    bindings,
    invitations,
    oauth_states,
    outbox,
    prompts,
    workspaces,
)
from src.services.target.bindings import BOUND, REBOUND, TAKEN, BindingRefused
from src.services.target.unit_of_work import asyncpg_url, unit_of_work
from tests.scripts.conftest import (
    _scratch,
    as_user,
    fetch_one,
    in_user_plane,
    replay_advertised_stream,
    seed_workspace_chain,
    set_test_passwords,
    sweep_as_worker,
    txn,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]


@pytest.fixture(scope="module")
def world(admin_conn, owner_actor):
    """Two seeded workspaces — the two-identity discipline: every "which row"
    assertion has a second tenant to be wrong about."""
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        stream = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        conn = psycopg2.connect(stream)
        try:
            a = seed_workspace_chain(conn, "bind-a")
            b = seed_workspace_chain(conn, "bind-b")
        finally:
            conn.close()
        yield {
            "stream": stream,
            "ingress": as_user(db, "svc_ingress"),
            # the sender sweep is the worker's (082: a door granted to
            # svc_worker alone), run here the way the worker runs it
            "worker": as_user(db, "svc_worker"),
            "a": a,
            "b": b,
        }
    finally:
        gen.close()


async def _in_uow(dsn: str, ws: str, user: str, fn):
    engine = create_async_engine(asyncpg_url(dsn), poolclass=NullPool)
    try:
        uow = unit_of_work(
            engine, ws, actor_kind="user", actor_user_id=user, channel="telegram"
        )
        async with uow.begin() as session:
            who = (await session.execute(text("SELECT current_user"))).scalar()
            assert who == "svc_ingress", who
            return await fn(session)
    finally:
        await engine.dispose()


def run(world, fn, *, ids=None):
    ids = ids or world["a"]
    return asyncio.run(_in_uow(world["ingress"], str(ids["ws"]), str(ids["user"]), fn))


def _bind(world, ref, *, ids=None, chat_type="supergroup"):
    """Default `supergroup` deliberately: it is the most common real group type
    and the one a two-of-three mapping drops."""
    ids = ids or world["a"]
    return run(
        world,
        lambda s: bindings.bind(
            s, workspace_id=str(ids["ws"]), chat_type=chat_type, external_ref=ref
        ),
        ids=ids,
    )


def _row(world, ref):
    return fetch_one(
        world["stream"],
        "SELECT workspace_id, state FROM channel_bindings WHERE external_ref = %s",
        (ref,),
    )


def _binding_id(world, ref: str) -> str:
    row = fetch_one(
        world["stream"],
        "SELECT id FROM channel_bindings WHERE external_ref = %s",
        (ref,),
    )
    assert row is not None, f"no binding holds {ref}"
    return row[0]


def _migrate(world, sql, params=()):
    """One committed statement as the migration actor — the governance audit
    triggers refuse an anonymous write."""
    conn = psycopg2.connect(world["stream"])
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            cur.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _chat() -> str:
    return f"-100{uuid.uuid4().int % 10**10:010d}"


class TestBind:
    def test_binds_a_fresh_chat_and_reports_it_as_new(self, world):
        ref = _chat()
        assert _bind(world, ref) == BOUND
        ws, state = _row(world, ref)
        assert str(ws) == str(world["a"]["ws"])
        assert state == "active"

    def test_the_same_workspace_re_binding_is_rebound_not_a_second_row(self, world):
        ref = _chat()
        assert _bind(world, ref) == BOUND
        assert _bind(world, ref) == REBOUND
        (n,) = fetch_one(
            world["stream"],
            "SELECT count(*) FROM channel_bindings WHERE external_ref = %s",
            (ref,),
        )
        assert n == 1

    def test_re_adding_the_bot_flips_revoked_back_to_active(self, world):
        """`02` §1's DDL comment specifies exactly this: `uq_binding_external`
        holds across states, so the row is reactivated rather than replaced and
        the history survives."""
        ref = _chat()
        _bind(world, ref)
        assert (
            run(
                world,
                lambda s: bindings.revoke(
                    s,
                    workspace_id=str(world["a"]["ws"]),
                    chat_type="group",
                    external_ref=ref,
                ),
            )
            is True
        )
        assert _row(world, ref)[1] == "revoked"
        assert _bind(world, ref) == REBOUND
        assert _row(world, ref)[1] == "active"

    def test_a_chat_held_by_another_workspace_is_TAKEN_and_not_moved(self, world):
        """**The product rule, and it is stated rather than inferred:** `02` §6
        calls a chat being un-double-bindable *"inherent to the product"*. The
        load-bearing half is the second assertion — a refusal that had already
        re-pointed the row would be a tenant stealing another's chat."""
        ref = _chat()
        assert _bind(world, ref) == BOUND
        assert _bind(world, ref, ids=world["b"]) == TAKEN
        ws, state = _row(world, ref)
        assert str(ws) == str(world["a"]["ws"]), "still A's chat"
        assert state == "active"

    def test_the_two_channels_are_separate_bindings_of_one_chat_id(self, world):
        """`uq_binding_external` is `(channel, external_ref)`, so the same id in
        a DM and a group are two rows. Pinned because reading the constraint as
        "one row per chat id" would make the DM path silently TAKEN."""
        ref = _chat()
        assert _bind(world, ref, chat_type="group") == BOUND
        assert _bind(world, ref, chat_type="private") == BOUND


class TestZeroToN:
    def test_two_chats_in_one_workspace_are_both_active(self, world):
        """**D13's `0..n` delivered for the first time.** It was ratified in
        `06`, legal in the schema, and had never existed in either system —
        legacy only ever created a FIRST binding, keyed on an onboarding
        session that exists once."""
        first, second = _chat(), _chat()
        assert _bind(world, first) == BOUND
        assert _bind(world, second) == BOUND
        ids = run(
            world,
            lambda s: prompts.push_bindings(s, str(world["a"]["ws"])),
        )
        assert len(ids) >= 2
        assert len(set(ids)) == len(ids)

    def test_revoking_one_leaves_the_other_deliverable(self, world):
        first, second = _chat(), _chat()
        _bind(world, first)
        _bind(world, second)
        before = run(
            world,
            lambda s: prompts.push_bindings(s, str(world["a"]["ws"])),
        )
        run(
            world,
            lambda s: bindings.revoke(
                s,
                workspace_id=str(world["a"]["ws"]),
                chat_type="group",
                external_ref=first,
            ),
        )
        after = run(
            world,
            lambda s: prompts.push_bindings(s, str(world["a"]["ws"])),
        )
        assert len(after) == len(before) - 1


class TestRefusals:
    def test_an_unknown_chat_type_is_refused_by_name(self, world):
        """Including the ALREADY-MAPPED spellings. A writer that quietly took
        `telegram_group` as well would let a caller bypass the mapping, which
        is how the two lanes drift back apart."""
        for bad in ("channel", "web", "telegram", "telegram_group", "", None, 3):
            with pytest.raises(BindingRefused) as e:
                _bind(world, _chat(), chat_type=bad)
            assert e.value.reason == "chat_type_unsupported", bad

    def test_a_missing_or_malformed_chat_id_is_refused_by_name(self, world):
        for bad, reason in (
            ("", "external_ref_required"),
            ("   ", "external_ref_required"),
            (None, "external_ref_required"),
            ("not-a-chat", "external_ref_malformed"),
            ("-100 555", "external_ref_malformed"),
        ):
            with pytest.raises(BindingRefused) as e:
                _bind(world, bad)
            assert e.value.reason == reason, bad

    def test_revoking_a_chat_this_workspace_never_held_is_false_not_an_error(
        self, world
    ):
        ref = _chat()
        _bind(world, ref)
        assert (
            run(
                world,
                lambda s: bindings.revoke(
                    s,
                    workspace_id=str(world["b"]["ws"]),
                    chat_type="group",
                    external_ref=ref,
                ),
                ids=world["b"],
            )
            is False
        )
        assert _row(world, ref)[1] == "active", "A's binding untouched"


class TestItAuditsAndUnInertsTheChain:
    def test_a_bind_writes_a_governance_audit_row(self, world):
        """`055` attaches `tg_audit_channel_bindings` on INSERT/UPDATE/DELETE
        with `entity_kind = 'channel_binding'`. Asserted rather than assumed —
        the trail this writer needs already exists in the schema."""
        ref = _chat()
        (before,) = fetch_one(
            world["stream"],
            "SELECT count(*) FROM audit_events WHERE entity_kind = 'channel_binding'"
            "   AND workspace_id = %s",
            (str(world["a"]["ws"]),),
        )
        _bind(world, ref)
        (after,) = fetch_one(
            world["stream"],
            "SELECT count(*) FROM audit_events WHERE entity_kind = 'channel_binding'"
            "   AND workspace_id = %s",
            (str(world["a"]["ws"]),),
        )
        assert after > before

    def test_a_binding_lets_the_already_built_sweep_mint_a_sender_job(self, world):
        """**The keystone claim, driven rather than asserted.**

        `ensure_sender_jobs` selects `FROM channel_bindings ... WHERE EXISTS
        (pending outbox row)`. It has always been built and has never been able
        to return anything, because the table was empty. This binds, enqueues
        one row through the real `outbox.enqueue`, and asserts the sweep now
        mints — which is the whole reason clause 4 needed a writer.
        """
        ref = _chat()

        # POSITIVE CONTROL, and it is what makes the claim above a measurement
        # rather than a story: workspace B never acquires a binding anywhere in
        # this module (its only attempt is the TAKEN case), so the sweep has
        # nothing of B's to find. That is the state the WHOLE product was in.
        sweep_as_worker(world["worker"])
        (b_jobs,) = fetch_one(
            world["stream"],
            "SELECT count(*) FROM jobs WHERE kind = 'deliver_outbox'"
            "   AND workspace_id = %s",
            (str(world["b"]["ws"]),),
        )
        assert b_jobs == 0, "a workspace with no binding can mint nothing"

        assert _bind(world, ref) == BOUND

        async def enqueue_and_sweep(session):
            ids = await prompts.push_bindings(session, str(world["a"]["ws"]))
            target = [
                i
                for i in ids
                if str(
                    (
                        await session.execute(
                            text(
                                "SELECT external_ref FROM channel_bindings"
                                " WHERE id = :b"
                            ),
                            {"b": i},
                        )
                    ).scalar()
                )
                == ref
            ]
            assert target, "the binding just written is deliverable"
            await outbox.enqueue(
                session,
                workspace_id=str(world["a"]["ws"]),
                binding_id=target[0],
                kind="invitation",
                payload={"v": 1, "text": "join"},
            )
            return None

        # The enqueue commits with the ingress unit of work; the sweep is the
        # worker's (082) — its own login, its own session, after the commit.
        run(world, enqueue_and_sweep)
        assert sweep_as_worker(world["worker"]) >= 1
        (jobs,) = fetch_one(
            world["stream"],
            "SELECT count(*) FROM jobs WHERE kind = 'deliver_outbox'"
            "   AND workspace_id = %s",
            (str(world["a"]["ws"]),),
        )
        assert jobs >= 1


class TestChatTypeMapping:
    """The mapping lives HERE and in one place. It is not duplicated in the
    `/start` router, and before this it existed nowhere at all — the legacy
    handler tested `chat.type not in ("group", "supergroup")` inline and had no
    DM path, so `telegram_dm` was a schema value nothing could ever produce."""

    def test_every_bindable_telegram_chat_type_maps(self):
        assert bindings.channel_for_chat_type("private") == "telegram_dm"
        assert bindings.channel_for_chat_type("group") == "telegram_group"
        assert bindings.channel_for_chat_type("supergroup") == "telegram_group"

    def test_a_broadcast_channel_is_refused_rather_than_guessed(self):
        """`channel` is a real Telegram chat type with no `ck_bindings_channel`
        value. Mapping it to either would invent a product decision."""
        with pytest.raises(BindingRefused) as e:
            bindings.channel_for_chat_type("channel")
        assert e.value.reason == "chat_type_unsupported"

    def test_a_missing_or_non_string_type_is_refused_by_the_same_name(self):
        for bad in (None, "", "Group", 3, {}):
            with pytest.raises(BindingRefused) as e:
                bindings.channel_for_chat_type(bad)
            assert e.value.reason == "chat_type_unsupported", bad

    def test_every_mapped_value_is_a_legal_channel(self):
        """Totality against the writer's own vocabulary, so the two cannot
        drift into a mapping that produces a value `bind` would refuse."""
        for value in set(bindings._CHAT_TYPES.values()):
            assert value in bindings.CHANNELS


class TestAFreedChatIsBindableAgain:
    def test_a_finalized_tenants_chat_has_no_tombstone_and_rebinds_clean(self, world):
        """**Lane D's seam, pinned here because it lands on THIS flow.**

        `fn_offboard_finalize` deletes the workspace and the FK cascade takes
        `channel_bindings` with it — no code of mine runs. Because
        `uq_binding_external` is GLOBAL, the freed chat then becomes bindable by
        any workspace, and there is no tombstone: the row is gone and the chat
        looks pristine.

        That is correct, and it is worth driving rather than assuming, because
        the alternative failure is silent — a stale row would make the chat
        permanently `TAKEN` for a tenant that no longer exists, and nothing
        would ever report it.

        **"No tombstone" is true of `channel_bindings` and FALSE of the record
        as a whole**, which is worth being exact about because the imprecise
        version was briefly believed across two lanes. `audit_events` carries
        `workspace_id UUID NOT NULL` with **no FK** — `§0`'s stated exception,
        *"audit outlives the tenant"* — so the governance trigger's rows for
        this binding survive the delete that removes the binding itself. The
        creation flow sees a pristine chat; the AUDIT still knows. Both halves
        are asserted below.
        """
        conn = psycopg2.connect(world["stream"])
        try:
            doomed = seed_workspace_chain(conn, f"doomed-{uuid.uuid4().hex[:6]}")
        finally:
            conn.close()

        ref = _chat()
        assert _bind(world, ref, ids=doomed) == BOUND
        assert _bind(world, ref) == TAKEN, "held while that tenant lives"

        _migrate(world, "DELETE FROM workspaces WHERE id = %s", (str(doomed["ws"]),))

        assert _row(world, ref) is None, "cascade removed it — no tombstone"
        assert _bind(world, ref) == BOUND, "the chat is genuinely free again"

        (surviving,) = fetch_one(
            world["stream"],
            "SELECT count(*) FROM audit_events"
            " WHERE entity_kind = 'channel_binding' AND workspace_id = %s",
            (str(doomed["ws"]),),
        )
        assert surviving > 0, (
            "audit_events has no FK to workspaces (§0: audit outlives the "
            "tenant), so the deleted tenant's binding history is still there"
        )


class TestTheStartDoorBindsAsSvcIngressWithNoContextOfItsOwn:
    """The lane on the door's own connection shape (#1240 review): a bare
    `svc_ingress` connection with NO GUCs set by the caller — the state row
    supplies them. The fixture that first proved the lane ran as the schema
    owner with the actor preset, which is exactly not the door."""

    def _linked_admin(self, world, tg_user_id: str) -> None:
        _migrate(
            world,
            "INSERT INTO user_identities (user_id, provider, external_id, display_name)"
            " VALUES (%s, 'telegram', %s, 'ada')"
            " ON CONFLICT (user_id, provider) DO UPDATE SET external_id = EXCLUDED.external_id",
            (str(world["a"]["user"]), tg_user_id),
        )

    def _mint(self, world):
        from src.services.target import channel_bind

        return run(
            world,
            lambda s: channel_bind.issue_bind_state(
                s,
                user_id=str(world["a"]["user"]),
                workspace_id=str(world["a"]["ws"]),
                bot_username="storydump_app_bot",
            ),
        )

    def _tap(self, world, state: str, *, tg_user_id: str, external_ref: str):
        from sqlalchemy.pool import NullPool

        from src.services.target import channel_bind
        from src.services.target.start_router import StartContext
        from src.services.target.unit_of_work import asyncpg_url

        async def go():
            engine = create_async_engine(
                asyncpg_url(world["ingress"]), poolclass=NullPool
            )
            try:
                async with engine.connect() as conn:
                    who = (await conn.execute(text("SELECT current_user"))).scalar()
                    assert who == "svc_ingress", who
                    result = await channel_bind.handle_bind(
                        conn,
                        StartContext(
                            payload=state,
                            telegram_user_id=tg_user_id,
                            chat_id=external_ref,
                            chat_type="supergroup",
                            display_name="ada",
                        ),
                    )
                    await conn.commit()
                    return result
            finally:
                await engine.dispose()

        return asyncio.run(go())

    def test_the_minting_admin_binds_a_group_through_the_bare_door(self, world):
        self._linked_admin(world, "tg-admin-1")
        state = self._mint(world).rsplit("bind-", 1)[1]
        result = self._tap(
            world, state, tg_user_id="tg-admin-1", external_ref="-1009000000001"
        )
        assert result.handled and result.outcome == "bound", result.outcome
        row = _row(world, "-1009000000001")
        assert row is not None and str(row[0]) == str(world["a"]["ws"])

    def test_a_stranger_holding_the_link_binds_nothing(self, world):
        self._linked_admin(world, "tg-admin-1")
        state = self._mint(world).rsplit("bind-", 1)[1]
        result = self._tap(
            world, state, tg_user_id="tg-stranger", external_ref="-1009000000002"
        )
        assert not result.handled and result.outcome == "tapper_not_minter"
        assert _row(world, "-1009000000002") is None


class TestRetiringAndFollowingAChat:
    def test_revoke_by_id_keeps_the_row_and_flips_it(self, world):
        _bind(world, "-1009000000010")
        binding = _binding_id(world, "-1009000000010")
        moved = run(world, lambda s: bindings.revoke_by_id(s, binding_id=binding))
        assert moved is True
        assert _row(world, "-1009000000010")[1] == "revoked"
        assert (
            run(world, lambda s: bindings.revoke_by_id(s, binding_id=binding)) is False
        )

    def test_repoint_follows_a_supergroup_upgrade_unless_the_new_id_is_taken(
        self, world
    ):
        _bind(world, "-777000001")
        binding = _binding_id(world, "-777000001")
        assert (
            run(
                world,
                lambda s: bindings._repoint(
                    s, binding_id=binding, external_ref="-1009000000020"
                ),
            )
            is True
        )
        assert _row(world, "-1009000000020")[1] == "active"
        # Workspace B holds the id the next upgrade would land on.
        _bind(world, "-1009000000021", ids=world["b"])
        assert (
            run(
                world,
                lambda s: bindings._repoint(
                    s, binding_id=binding, external_ref="-1009000000021"
                ),
            )
            is False
        )

    def test_a_refused_repoint_leaves_the_session_usable_for_the_revoke(self, world):
        """The callers' real shape: repoint, and revoke on the SAME session.

        `follow_or_retire` does exactly this for both of them — the deliverer
        and the migration notice — so when `_repoint` swallows the unique
        violation and answers False, the very next statement runs on a
        transaction Postgres has already aborted unless the savepoint scoped
        it (#1362). Every other test here calls `_repoint` in a session of its
        own, which is why this survived: the abort is invisible once the
        session ends.

        A supergroup migration onto an id another workspace already holds is a
        real user path, and this is the only test that drives both halves of
        it through one transaction.
        """
        _bind(world, "-777000002")
        binding = _binding_id(world, "-777000002")
        # Workspace B already holds the id the upgrade would land on.
        _bind(world, "-1009000000030", ids=world["b"])

        followed = run(
            world,
            lambda s: bindings.follow_or_retire(
                s, binding_id=binding, successor="-1009000000030"
            ),
        )
        assert followed is False, "the taken id must refuse the repoint"
        assert _row(world, "-777000002")[1] == "revoked"
        assert _row(world, "-1009000000030") == (world["b"]["ws"], "active")


class TestTheJoinPathThroughTheDoors:
    """`06`'s Telegram join path on postgres:15, driven as a bare `svc_ingress`
    connection with no GUCs (the door sets its own): a linked person speaking
    in a bound group becomes a member; nothing else writes. A person an admin
    removed stays removed until an invitation makes them a member (`07` §37)."""

    def _link(self, world, user_id, tg_user_id):
        _migrate(
            world,
            "INSERT INTO user_identities (user_id, provider, external_id, display_name)"
            " VALUES (%s, 'telegram', %s, 'joiner')"
            " ON CONFLICT (user_id, provider) DO UPDATE SET external_id = EXCLUDED.external_id",
            (str(user_id), tg_user_id),
        )

    def _observe(self, world, *, external_ref, tg_user_id, chat_type="supergroup"):
        from sqlalchemy.pool import NullPool

        from src.services.target import membership_sync
        from src.services.target.unit_of_work import asyncpg_url

        async def go():
            engine = create_async_engine(
                asyncpg_url(world["ingress"]), poolclass=NullPool
            )
            try:
                async with engine.connect() as conn:
                    who = (await conn.execute(text("SELECT current_user"))).scalar()
                    assert who == "svc_ingress", who
                    result = await membership_sync.observe(
                        conn,
                        chat_type=chat_type,
                        external_ref=external_ref,
                        telegram_user_id=tg_user_id,
                    )
                    await conn.commit()
                    return result
            finally:
                await engine.dispose()

        return asyncio.run(go())

    def _role(self, world, ws, user):
        row = fetch_one(
            world["stream"],
            "SELECT role FROM workspace_members WHERE workspace_id = %s AND user_id = %s",
            (str(ws), str(user)),
        )
        return None if row is None else row[0]

    def test_a_linked_person_in_a_bound_group_becomes_a_member(self, world):
        _bind(world, "-1009000000100")
        joiner = world["b"]["user"]  # a real user, not yet in workspace A
        self._link(world, joiner, "tg-joiner-1")
        first = self._observe(
            world, external_ref="-1009000000100", tg_user_id="tg-joiner-1"
        )
        assert first.outcome == "joined" and first.handled
        assert self._role(world, world["a"]["ws"], joiner) == "member"
        again = self._observe(
            world, external_ref="-1009000000100", tg_user_id="tg-joiner-1"
        )
        assert again.outcome == "already_member"

    def test_an_owner_seen_speaking_is_never_downgraded(self, world):
        _bind(world, "-1009000000101")
        self._link(world, world["a"]["user"], "tg-owner-a")
        result = self._observe(
            world, external_ref="-1009000000101", tg_user_id="tg-owner-a"
        )
        assert result.outcome == "already_member"
        assert self._role(world, world["a"]["ws"], world["a"]["user"]) == "owner"

    def test_an_unknown_identity_or_an_unbound_chat_writes_nothing(self, world):
        _bind(world, "-1009000000102")
        assert (
            self._observe(
                world, external_ref="-1009000000102", tg_user_id="tg-nobody"
            ).outcome
            == "unknown_identity"
        )
        self._link(world, world["b"]["user"], "tg-joiner-2")
        count = lambda: fetch_one(  # noqa: E731 — a two-line probe, read twice
            world["stream"],
            "SELECT count(*) FROM workspace_members WHERE user_id = %s",
            (str(world["b"]["user"]),),
        )[0]
        before = count()
        assert (
            self._observe(
                world, external_ref="-1009000000999", tg_user_id="tg-joiner-2"
            ).outcome
            == "unbound_chat"
        )
        assert count() == before, "an unbound chat must add no membership anywhere"

    def test_a_revoked_binding_is_named_for_the_join_path(self, world):
        _bind(world, "-1009000000103")
        row = fetch_one(
            world["stream"],
            "SELECT id FROM channel_bindings WHERE external_ref = %s",
            ("-1009000000103",),
        )
        run(world, lambda s: bindings.revoke_by_id(s, binding_id=str(row[0])))
        self._link(world, world["b"]["user"], "tg-joiner-3")
        assert (
            self._observe(
                world, external_ref="-1009000000103", tg_user_id="tg-joiner-3"
            ).outcome
            == "revoked_chat"
        )

    def test_the_join_is_attributed_to_the_joiner_in_the_audit(self, world):
        _bind(world, "-1009000000105")
        joiner = world["b"]["user"]
        self._link(world, joiner, "tg-joiner-5")
        self._observe(world, external_ref="-1009000000105", tg_user_id="tg-joiner-5")
        row = fetch_one(
            world["stream"],
            "SELECT actor_kind, actor_user_id::text, channel FROM audit_events"
            " WHERE entity_kind = 'member' AND entity_id::text = %s"
            " ORDER BY created_at DESC LIMIT 1",
            (str(joiner),),
        )
        assert row is not None, "the governance audit row for the join is missing"
        assert row == ("user", str(joiner), "telegram")

    def test_a_revoked_chat_and_a_closing_workspace_are_named(self, world):
        _bind(world, "-1009000000106")
        rowb = fetch_one(
            world["stream"],
            "SELECT id FROM channel_bindings WHERE external_ref = %s",
            ("-1009000000106",),
        )
        self._link(world, world["b"]["user"], "tg-joiner-6")
        run(world, lambda s: bindings.revoke_by_id(s, binding_id=str(rowb[0])))
        assert (
            self._observe(
                world, external_ref="-1009000000106", tg_user_id="tg-joiner-6"
            ).outcome
            == "revoked_chat"
        )
        _bind(world, "-1009000000107", ids=world["b"])
        _migrate(
            world,
            "UPDATE workspaces SET state = 'offboarding' WHERE id = %s",
            (str(world["b"]["ws"]),),
        )
        try:
            self._link(world, world["a"]["user"], "tg-owner-a6")
            assert (
                self._observe(
                    world, external_ref="-1009000000107", tg_user_id="tg-owner-a6"
                ).outcome
                == "workspace_inactive"
            )
        finally:
            _migrate(
                world,
                "UPDATE workspaces SET state = 'active' WHERE id = %s",
                (str(world["b"]["ws"]),),
            )

    def _person(self, world):
        """A fresh account. The removal record is per (workspace, person), so a
        test that removes someone removes someone of its own."""
        person = str(uuid.uuid4())
        _migrate(world, "INSERT INTO users (id) VALUES (%s)", (person,))
        return person

    def _remove(self, world, person, *, by=None, claim=None):
        """`remove_member` in workspace A, as its owner unless *by* says who, in
        a unit of work claiming A unless *claim* names the other workspace."""
        return run(
            world,
            lambda s: workspaces.remove_member(
                s,
                workspace_id=str(world["a"]["ws"]),
                user_id=str(person),
                by_user_id=str(by or world["a"]["user"]),
            ),
            ids=claim,
        )

    def _removal(self, world, person):
        return fetch_one(
            world["stream"],
            "SELECT removed_by_user_id::text, updated_at FROM workspace_member_removals"
            " WHERE workspace_id = %s AND user_id = %s",
            (str(world["a"]["ws"]), person),
        )

    def _invite_and_accept(self, world, person):
        """A real invitation into workspace A, accepted through
        `fn_invitation_accept` in the user plane, both as `svc_ingress`."""

        async def mint(session):
            _id, token = await invitations.create(
                session,
                workspace_id=str(world["a"]["ws"]),
                invited_by_user_id=str(world["a"]["user"]),
                delivery_channel="telegram",
            )
            return token

        token = run(world, mint)
        return asyncio.run(
            in_user_plane(
                world["ingress"],
                lambda conn: invitations.accept(
                    conn, token=token, user_id=person, channel="web"
                ),
            )
        )

    def test_the_owner_is_never_removed_even_by_an_admin(self, world):
        admin = self._person(world)
        _migrate(
            world,
            "INSERT INTO workspace_members (workspace_id, user_id, role)"
            " VALUES (%s, %s, 'admin')",
            (str(world["a"]["ws"]), admin),
        )
        with pytest.raises(ValueError):
            self._remove(world, world["a"]["user"], by=admin)
        assert self._role(world, world["a"]["ws"], world["a"]["user"]) == "owner"

    def test_a_removal_holds_until_an_invitation_and_again_after_the_next(self, world):
        """`07` §37: removed, a person speaking in the bound group is added back
        by nothing; an accepted invitation makes them a member again; removed
        again, the group adds nothing again."""
        _bind(world, "-1009000000109")
        person = self._person(world)
        self._link(world, person, "tg-joiner-9")

        def seen():
            return self._observe(
                world, external_ref="-1009000000109", tg_user_id="tg-joiner-9"
            )

        assert seen().outcome == "joined"
        assert self._remove(world, person) == "member"
        after = seen()
        assert after.outcome == "removed_by_admin" and not after.handled
        assert self._role(world, world["a"]["ws"], person) is None
        by, first = self._removal(world, person)
        assert by == str(world["a"]["user"])

        assert self._invite_and_accept(world, person)["role"] == "member"
        assert self._role(world, world["a"]["ws"], person) == "member"
        assert seen().outcome == "already_member", "a membership outranks the record"

        assert self._remove(world, person) == "member"
        assert seen().outcome == "removed_by_admin"
        assert self._role(world, world["a"]["ws"], person) is None
        _by, updated = self._removal(world, person)
        assert updated > first, "a later removal updates the record"

    def test_the_remove_door_refuses_another_workspace_and_a_non_admin(self, world):
        """`07` §37: the door checks its caller itself. The workspace must be
        the claimed tenant and the remover an owner or admin of it."""
        _bind(world, "-1009000000110")
        person, peer = self._person(world), self._person(world)
        for who, tg in ((person, "tg-joiner-10"), (peer, "tg-joiner-11")):
            self._link(world, who, tg)
            self._observe(world, external_ref="-1009000000110", tg_user_id=tg)
        # A's own owner, but under B's claim: the tenant clause alone refuses.
        with pytest.raises(DBAPIError, match="outside the caller's admin scope"):
            self._remove(world, person, claim=world["b"])
        # A's claim, but a remover below admin: the role clause alone refuses.
        with pytest.raises(DBAPIError, match="outside the caller's admin scope"):
            self._remove(world, person, by=peer)
        assert self._role(world, world["a"]["ws"], person) == "member"
        assert self._removal(world, person) is None

    def test_a_removal_retires_the_persons_live_states_in_that_workspace(self, world):
        """`07` §37: the bind, Drive and Instagram states the removed person
        holds for the workspace are spent with the removal. Their states for
        another workspace, and other people's here, stay live."""
        _bind(world, "-1009000000112")
        person = self._person(world)
        self._link(world, person, "tg-joiner-12")
        self._observe(world, external_ref="-1009000000112", tg_user_id="tg-joiner-12")

        def mint(user, ids, provider, purpose):
            return run(
                world,
                lambda s: oauth_states.issue_state(
                    s,
                    purpose=purpose,
                    user_id=str(user),
                    workspace_id=str(ids["ws"]),
                    provider=provider,
                ),
                ids=ids,
            )

        theirs_here = [
            mint(person, world["a"], "telegram", "bind"),
            mint(person, world["a"], "gdrive", "connect"),
            mint(person, world["a"], "ig_login", "connect"),
        ]
        untouched = [
            mint(person, world["b"], "gdrive", "connect"),
            mint(world["a"]["user"], world["a"], "gdrive", "connect"),
        ]
        assert self._remove(world, person) == "member"

        def live(state):
            return fetch_one(
                world["stream"],
                "SELECT consumed_at IS NULL FROM oauth_states WHERE state = %s",
                (state,),
            )[0]

        assert [live(st) for st in theirs_here] == [False, False, False]
        assert [live(st) for st in untouched] == [True, True]


class TestTheRemovalBackfill:
    """`094`'s backfill, the file's own statement, run over a crafted audit
    trail in a transaction that is rolled back: the latest removal a person
    made is recorded for whoever is not a member again, and nothing else is."""

    def _backfill_sql(self):
        sql = (MIGRATIONS_DIR / "094_member_removal_record.sql").read_text()
        return next(
            st
            for st in split_statements(sql)
            if st.lstrip().startswith("INSERT INTO workspace_member_removals")
        )

    def test_the_latest_removal_by_a_person_is_recorded_and_nothing_else(self, world):
        ws, owner = str(world["a"]["ws"]), str(world["a"]["user"])
        gone_ws, gone_user = str(uuid.uuid4()), str(uuid.uuid4())
        with txn(world["stream"]) as conn, conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            people = {}
            for name in ("twice", "back", "migrated", "elsewhere", "orphaned"):
                cur.execute("INSERT INTO users DEFAULT VALUES RETURNING id::text")
                people[name] = cur.fetchone()[0]

            def removal(person, *, ago, actor_kind="user", actor=owner, at=ws):
                cur.execute(
                    "INSERT INTO audit_events (workspace_id, entity_kind,"
                    " entity_id, from_state, actor_kind, actor_user_id,"
                    " channel, detail, created_at)"
                    " VALUES (%s, 'member', %s, 'member', %s, %s, 'web',"
                    " jsonb_build_object('v', 1, 'op', 'DELETE'),"
                    " now() - %s * interval '1 day')",
                    (at, person, actor_kind, actor, ago),
                )

            removal(people["twice"], ago=3)
            removal(people["twice"], ago=1)
            removal(people["back"], ago=2)
            cur.execute(
                "INSERT INTO workspace_members (workspace_id, user_id, role)"
                " VALUES (%s, %s, 'member')",
                (ws, people["back"]),
            )
            removal(people["migrated"], ago=2, actor_kind="migration", actor=None)
            removal(people["elsewhere"], ago=2, at=gone_ws)
            removal(people["orphaned"], ago=2, actor=gone_user)
            removal(gone_user, ago=2)

            cur.execute("DELETE FROM workspace_member_removals")
            cur.execute(self._backfill_sql())
            cur.execute(
                "SELECT user_id::text, removed_by_user_id::text, now() - updated_at"
                "  FROM workspace_member_removals"
                " WHERE workspace_id = %s AND user_id = ANY(%s::uuid[])",
                (ws, list(people.values()) + [gone_user]),
            )
            recorded = {row[0]: row[1:] for row in cur.fetchall()}
        assert recorded == {
            people["twice"]: (owner, timedelta(days=1)),
            people["orphaned"]: (None, timedelta(days=2)),
        }


def _basic_group() -> str:
    """A basic group's chat id — negative, without the `-100` a supergroup
    carries. The id a migration retires."""
    return f"-4{uuid.uuid4().int % 10**9:09d}"


def _message(update_id: int, chat_id: str, chat_type: str, *, sender=4040, **fields):
    """One Telegram `message` update, in the Bot API's shape."""
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id % 1000,
            "date": 1790000000,
            "from": {"id": sender, "is_bot": False, "first_name": "admin"},
            "chat": {"id": int(chat_id), "type": chat_type},
            **fields,
        },
    }


def _migrated_to(old: str, new: str, update_id: int) -> dict:
    """The notice Telegram posts in the OLD group as it retires it."""
    return _message(update_id, old, "group", migrate_to_chat_id=int(new))


def _migrated_from(old: str, new: str, update_id: int) -> dict:
    """The notice Telegram posts in the NEW supergroup."""
    return _message(update_id, new, "supergroup", migrate_from_chat_id=int(old))


def _dispatch(world, payload: dict):
    """One admitted update through the real ingress dispatcher, on the door's
    own connection shape: bare `svc_ingress`, no GUCs, the route's commit."""
    from src.services.target.telegram_dispatch import TelegramDispatcher

    return asyncio.run(
        in_user_plane(world["ingress"], lambda c: TelegramDispatcher()(c, payload))
    )


def _resolve(world, ref: str):
    """What every inbound update from *ref* resolves to — a tap, a member
    speaking: the one resolver, as `svc_ingress`."""
    from src.services.target import tenant_resolution

    return asyncio.run(
        in_user_plane(
            world["ingress"],
            lambda c: tenant_resolution.resolve_chat(c, "telegram_group", ref),
        )
    )


class TestAGroupThatBecameASupergroupKeepsItsWorkspace:
    """#743 on the target tier. Telegram retires a group's chat id when the
    group is upgraded to a supergroup, and says so twice as it happens: a
    service message in the old group naming its successor, and one in the new
    supergroup naming its predecessor. Everything inbound resolves through the
    binding's `external_ref`, so the binding has to follow the chat or the
    workspace is unreachable from its own group."""

    def test_the_old_groups_notice_moves_the_binding_to_the_new_id(self, world):
        old, new = _basic_group(), _chat()
        _bind(world, old, chat_type="group")
        before = _binding_id(world, old)
        other = _chat()  # the second tenant, which must not move
        _bind(world, other, ids=world["b"])

        _dispatch(world, _migrated_to(old, new, 700001))

        after = _resolve(world, new)
        assert after.workspace_id == str(world["a"]["ws"])
        assert after.channel_binding_id == before, "the SAME binding, not a new one"
        with pytest.raises(TenantResolutionError) as gone:
            _resolve(world, old)
        assert gone.value.reason == "unknown_binding"
        assert _row(world, other) == (world["b"]["ws"], "active")

    def test_the_new_supergroups_notice_moves_it_just_the_same(self, world):
        old, new = _basic_group(), _chat()
        _bind(world, old, chat_type="group")
        before = _binding_id(world, old)

        _dispatch(world, _migrated_from(old, new, 700002))

        after = _resolve(world, new)
        assert (after.workspace_id, after.channel_binding_id) == (
            str(world["a"]["ws"]),
            before,
        )

    @pytest.mark.parametrize("to_first", [True, False], ids=["to-first", "from-first"])
    def test_both_notices_arriving_move_it_once(self, world, to_first):
        """Telegram sends both, in either order, and the second finds the old
        id already released. It must be a quiet no-op — not an error, which
        would roll the admission back and have Telegram redeliver it forever,
        and not a second binding."""
        old, new = _basic_group(), _chat()
        _bind(world, old, chat_type="group")
        before = _binding_id(world, old)
        notices = [_migrated_to(old, new, 700011), _migrated_from(old, new, 700012)]
        for notice in notices if to_first else reversed(notices):
            _dispatch(world, notice)

        (n,) = fetch_one(
            world["stream"],
            "SELECT count(*) FROM channel_bindings WHERE external_ref IN (%s, %s)",
            (old, new),
        )
        assert n == 1
        assert _resolve(world, new).channel_binding_id == before

    def test_a_member_speaking_in_the_supergroup_joins_the_same_workspace(self, world):
        """The join path is what a group does all day; after the move it has
        to land in the workspace the group always belonged to."""
        old, new = _basic_group(), _chat()
        _bind(world, old, chat_type="group")
        joiner, speaker = str(uuid.uuid4()), 500_000_000 + uuid.uuid4().int % 10**8
        _migrate(world, "INSERT INTO users (id) VALUES (%s)", (joiner,))
        _migrate(
            world,
            "INSERT INTO user_identities (user_id, provider, external_id, display_name)"
            " VALUES (%s, 'telegram', %s, 'mover')",
            (joiner, str(speaker)),
        )

        _dispatch(world, _migrated_from(old, new, 700021))
        said = _dispatch(
            world, _message(700022, new, "supergroup", sender=speaker, text="hello")
        )

        assert said.outcome == "joined", said.outcome
        (role,) = fetch_one(
            world["stream"],
            "SELECT role FROM workspace_members WHERE workspace_id = %s AND user_id = %s",
            (world["a"]["ws"], joiner),
        )
        assert role == "member"

    def test_a_successor_another_workspace_holds_retires_the_old_binding(self, world):
        """The deliverer's rule, applied the moment Telegram says the chat
        moved: the old id will never take a message again, and the new one is
        already another binding's — one chat, one workspace — so the old
        binding is revoked and the holder is left exactly as it was."""
        old, new = _basic_group(), _chat()
        _bind(world, old, chat_type="group")
        _bind(world, new, ids=world["b"])

        _dispatch(world, _migrated_from(old, new, 700031))

        assert _row(world, old) == (world["a"]["ws"], "revoked")
        assert _row(world, new) == (world["b"]["ws"], "active")

    def test_a_chat_nobody_bound_is_followed_nowhere(self, world):
        """Guards over-reach, and passes against a handler that does nothing,
        so it is not evidence for the fix: a migration of a chat no workspace
        holds must never mint a binding for the new id. That mint is exactly
        how #743 stranded tenants on the legacy tier."""
        old, new = _basic_group(), _chat()

        _dispatch(world, _migrated_to(old, new, 700041))
        _dispatch(world, _migrated_from(old, new, 700042))

        assert _row(world, old) is None and _row(world, new) is None

    def test_the_move_is_audited_as_the_system_on_telegrams_word(self, world):
        """Nobody commanded the move: Telegram reported it. A row naming the
        admin who upgraded the group would put a decision in their name."""
        old, new = _basic_group(), _chat()
        _bind(world, old, chat_type="group")
        binding = _binding_id(world, old)

        _dispatch(world, _migrated_to(old, new, 700051))

        row = fetch_one(
            world["stream"],
            "SELECT actor_kind, actor_user_id, channel FROM audit_events"
            " WHERE entity_kind = 'channel_binding' AND entity_id::text = %s"
            "   AND detail->>'op' = 'UPDATE'"
            " ORDER BY created_at DESC LIMIT 1",
            (binding,),
        )
        assert row == ("system", None, "telegram")
