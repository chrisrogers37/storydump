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

import psycopg2
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.services.target import bindings, outbox, prompts, work_loop
from src.services.target.bindings import BOUND, REBOUND, TAKEN, BindingRefused
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
        yield {"stream": stream, "ingress": as_user(db, "svc_ingress"), "a": a, "b": b}
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
        run(world, work_loop.ensure_sender_jobs, ids=world["b"])
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
            return await work_loop.ensure_sender_jobs(session)

        assert run(world, enqueue_and_sweep) >= 1
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
