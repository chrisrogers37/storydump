"""W4 — the tap, on the real schema, as the production ingress role (phase 1 of
the 2026-09-09 plan, `01_the-tap.md` Test Plan).

A `callback_query` goes through `TelegramDispatcher._tap` on an ingress
connection exactly as the route drives it: resolve the chat through the door,
resolve the tapper, set the actor GUCs, run the command, commit. The triggers
are real (`trg_intent_guard`, `trg_intent_audit`), the supersede rows are
real, and every "which row" assertion has a second binding to be wrong about.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import psycopg2
import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.services.target import commands  # noqa: F401 — the port first: the registry cycle
from src.services.target.telegram_dispatch import TapResult, TelegramDispatcher
from src.services.target.unit_of_work import asyncpg_url
from tests.scripts.conftest import (
    _scratch,
    as_user,
    fetch_one,
    replay_advertised_stream,
    seed_workspace_chain,
    set_test_passwords,
)

TAPPER_TG_ID = "7007"
STRANGER_TG_ID = "9009"
CHAT_A = "-1001"
CHAT_B = "-1002"


@pytest.fixture(scope="module")
def world(admin_conn, owner_actor):
    """Replayed schema; one workspace (API publishing on) with its owner LINKED
    to Telegram id 7007 as "Ada", and TWO bound groups."""
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        stream = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        conn = psycopg2.connect(stream)
        try:
            chain = seed_workspace_chain(conn, "w4-tap")
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SET app.actor_kind = 'migration'")
                cur.execute(
                    "UPDATE workspaces SET api_publishing_enabled = true WHERE id = %s",
                    (chain["ws"],),
                )
                # `approve` refuses a `post` tap without a usable Instagram
                # token (#1276): the API workspace here is connected.
                cur.execute(
                    "INSERT INTO oauth_credentials (workspace_id, ig_account_id,"
                    " provider, encrypted_payload, state)"
                    " VALUES (%s, %s, 'ig_login', 'ciphertext', 'active')",
                    (chain["ws"], chain["iga"]),
                )
                cur.execute(
                    "INSERT INTO user_identities"
                    " (user_id, provider, external_id, display_name, verified_at)"
                    " VALUES (%s, 'telegram', %s, 'Ada', now())",
                    (chain["user"], TAPPER_TG_ID),
                )
                bindings = {}
                for name, chat in (("a", CHAT_A), ("b", CHAT_B)):
                    cur.execute(
                        "INSERT INTO channel_bindings (workspace_id, channel, external_ref)"
                        " VALUES (%s, 'telegram_group', %s) RETURNING id",
                        (chain["ws"], chat),
                    )
                    bindings[name] = str(cur.fetchone()[0])
        finally:
            conn.close()
        yield {
            "stream": stream,
            "ingress": as_user(db, "svc_ingress"),
            "ws": str(chain["ws"]),
            "user": str(chain["user"]),
            "iga": str(chain["iga"]),
            "src": str(chain["src"]),
            "bindings": bindings,
        }
    finally:
        gen.close()


# --- fixture data, as the migration actor -------------------------------------


def _write(world, sql, params=(), fetch=False):
    conn = psycopg2.connect(world["stream"])
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            cur.execute(sql, params)
            return cur.fetchall() if fetch else None
    finally:
        conn.close()


def _one(world, sql, params=()):
    return fetch_one(world["stream"], sql, params)


def _intent(world, tag: str, *, state="awaiting_approval") -> dict:
    """An intent on the chain's account, on its own media item, with a SENT
    card in both groups (refs 1xxx in A, 2xxx in B)."""
    ((media,),) = _write(
        world,
        "INSERT INTO media_items (workspace_id, source_id, content_hash, file_name,"
        " media_kind, provider_file_ref)"
        " VALUES (%s, %s, %s, 'f.jpg', 'image', %s) RETURNING id",
        (world["ws"], world["src"], f"hash-{tag}", f"ref-{tag}"),
        fetch=True,
    )
    # A `posted` row must be complete (`ck_posted_complete`): as the manual
    # path leaves it — published by hand, the day's cap consumed.
    posted_cols = ", published_via, cap_consumed_on" if state == "posted" else ""
    posted_vals = ", 'manual', current_date" if state == "posted" else ""
    ((intent,),) = _write(
        world,
        "INSERT INTO post_intents (workspace_id, ig_account_id, media_item_id,"
        f" provider_account_ref, approval_mode, schedule_slot_at, state{posted_cols})"
        f" VALUES (%s, %s, %s, 'acct-w4-tap', 'manual', now(), %s{posted_vals})"
        " RETURNING id",
        (world["ws"], world["iga"], media, state),
        fetch=True,
    )
    cards = {}
    for n, (name, binding) in enumerate(sorted(world["bindings"].items()), start=1):
        ref = f"{n}{uuid.uuid4().int % 10_000:04d}"
        _write(
            world,
            "INSERT INTO channel_outbox (workspace_id, binding_id, kind, intent_id,"
            " payload, state, external_message_ref)"
            " VALUES (%s, %s, 'approval_prompt', %s, %s, 'sent', %s)",
            (
                world["ws"],
                binding,
                intent,
                json.dumps(
                    {"v": 2, "text": f"📸 f.jpg ({tag})\nSlot: soon", "sent_as": "text"}
                ),
                ref,
            ),
        )
        cards[name] = ref
    return {"id": str(intent), "media": str(media), "cards": cards}


def _tap_payload(
    action: str,
    intent_id: str,
    *,
    chat=CHAT_A,
    message_id=555,
    tg_user=TAPPER_TG_ID,
    update_id=None,
):
    return {
        "update_id": update_id or int(uuid.uuid4().int % 1_000_000_000),
        "callback_query": {
            "id": f"q-{uuid.uuid4().hex[:8]}",
            "from": {"id": int(tg_user), "first_name": "Ada"},
            "message": {
                "message_id": message_id,
                "chat": {"id": int(chat), "type": "supergroup"},
            },
            "data": f"v1:{action}:{intent_id}",
        },
    }


# --- the tap, as the production ingress role ---------------------------------


async def _tap(world, payload) -> TapResult:
    """Exactly the route's shape: one ingress connection, dispatch, commit."""
    engine = create_async_engine(asyncpg_url(world["ingress"]), poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            result = await TelegramDispatcher()(conn, payload)
            await conn.commit()
        return result
    finally:
        await engine.dispose()


def tap(world, action, intent_id, **kw) -> TapResult:
    return asyncio.run(_tap(world, _tap_payload(action, intent_id, **kw)))


def _state(world, intent_id):
    return _one(world, "SELECT state FROM post_intents WHERE id = %s", (intent_id,))[0]


def _audit(world, intent_id):
    return _write(
        world,
        "SELECT to_state, actor_kind, actor_user_id::text, channel FROM audit_events"
        " WHERE entity_kind = 'post_intent' AND entity_id = %s ORDER BY id",
        (intent_id,),
        fetch=True,
    )


def _supersedes(world, intent_id):
    rows = _write(
        world,
        "SELECT binding_id::text, payload FROM channel_outbox"
        " WHERE intent_id = %s AND kind = 'prompt_supersede' ORDER BY created_at",
        (intent_id,),
        fetch=True,
    )
    return [(b, p if isinstance(p, dict) else json.loads(p)) for b, p in rows]


def _card_states(world, intent_id):
    return dict(
        _write(
            world,
            "SELECT external_message_ref, state FROM channel_outbox"
            " WHERE intent_id = %s AND kind = 'approval_prompt'",
            (intent_id,),
            fetch=True,
        )
    )


class TestATapFlipsOnceAndEditsEveryCard:
    def test_post_approves_as_the_tapper_enqueues_the_publish_and_supersedes_both_groups(
        self, world
    ):
        i = _intent(world, "post-1")
        r = tap(world, "post", i["id"])
        assert r.outcome == "executed" and r.handled is True
        assert "Approved" in r.answer_text
        assert _state(world, i["id"]) == "approved"
        (row,) = _audit(world, i["id"])
        assert row == ("approved", "user", world["user"], "telegram")
        assert _one(
            world,
            "SELECT count(*) FROM jobs WHERE kind = 'publish_pipeline'"
            " AND serialization_key = 'ig:acct-w4-tap' AND payload->>'intent_id' = %s",
            (i["id"],),
        ) == (1,)
        # Both groups' cards are superseded, each with the outcome line and
        # its own header, keyed to the ref the card was sent under.
        assert set(_card_states(world, i["id"]).values()) == {"superseded"}
        supersedes = _supersedes(world, i["id"])
        assert {b for b, _ in supersedes} == set(world["bindings"].values())
        for _, payload in supersedes:
            assert payload["supersedes_ref"] in i["cards"].values()
            assert "✅ Approved by Ada" in payload["outcome_text"]
            assert payload["header"].startswith("📸 f.jpg")
            assert payload["sent_as"] == "text"

    def test_skip_writes_the_lock_keyed_to_the_tapper(self, world):
        i = _intent(world, "skip-1")
        r = tap(world, "skip", i["id"])
        assert r.outcome == "executed" and "Skipped" in r.answer_text
        assert _state(world, i["id"]) == "skipped"
        assert _one(
            world,
            "SELECT created_by_user_id::text FROM post_locks"
            " WHERE workspace_id = %s AND media_item_id = %s AND kind = 'skip'",
            (world["ws"], i["media"]),
        ) == (world["user"],)


class TestARepeatOrLateTapAnswers:
    def test_a_second_tap_on_a_decided_card_answers_and_writes_nothing(self, world):
        i = _intent(world, "twice-1")
        first = tap(world, "reject", i["id"])
        assert first.outcome == "executed"
        audits_before = _audit(world, i["id"])
        second = tap(world, "post", i["id"])
        assert second.outcome == "answered"
        assert "Already" in second.answer_text and "Rejected" in second.answer_text
        assert "Ada" in second.answer_text
        assert _state(world, i["id"]) == "rejected"
        assert _audit(world, i["id"]) == audits_before

    def test_a_tap_on_a_posted_card_answers_and_heals_the_card(self, world):
        i = _intent(world, "posted-1", state="posted")
        r = tap(world, "skip", i["id"])
        assert r.outcome == "answered" and "Posted" in r.answer_text
        assert _state(world, i["id"]) == "posted"
        assert _audit(world, i["id"]) == []
        # The stale card (a lost supersede) heals on first touch: both copies.
        assert set(_card_states(world, i["id"]).values()) == {"superseded"}
        assert len(_supersedes(world, i["id"])) == 2

    def test_an_approval_tap_on_a_review_card_answers_the_review_has_its_own_buttons(
        self, world
    ):
        """A stale approval card's `post` on a parked intent answers with
        the state; the review card's own buttons (`retry`/`itposted`/`giveup`)
        are the levers (2026-09-12)."""
        i = _intent(world, "review-1", state="review_required")
        r = tap(world, "post", i["id"])
        assert r.outcome == "answered" and "Needs review" in r.answer_text
        assert _state(world, i["id"]) == "review_required"
        assert _audit(world, i["id"]) == []


class TestTwoTapsRacingOnOneCard:
    @pytest.mark.asyncio
    async def test_exactly_one_flip_the_other_answers_and_the_race_was_genuine(
        self, world
    ):
        """Two connections, two taps, one row lock. The loser blocks on
        `FOR UPDATE` until the winner commits, then reads the committed state
        and answers — never an error. Concurrency is PROVED by the timeline,
        not assumed: both taps start before either finishes."""
        i = _intent(world, "race-1")
        timeline = {}

        async def run(tag, action):
            timeline[f"{tag}_start"] = time.monotonic()
            r = await _tap(world, _tap_payload(action, i["id"], update_id=len(tag)))
            timeline[f"{tag}_end"] = time.monotonic()
            return r

        a, b = await asyncio.gather(run("alpha", "skip"), run("beta", "reject"))
        outcomes = sorted([a.outcome, b.outcome])
        assert outcomes == ["answered", "executed"], (a, b)
        assert max(timeline["alpha_start"], timeline["beta_start"]) < min(
            timeline["alpha_end"], timeline["beta_end"]
        ), "both taps must be in flight at once for this to prove anything"
        flips = [
            row for row in _audit(world, i["id"]) if row[0] in ("skipped", "rejected")
        ]
        assert len(flips) == 1
        assert _state(world, i["id"]) in ("skipped", "rejected")


class TestManyCardsAtOnce:
    @pytest.mark.asyncio
    async def test_twenty_taps_on_twenty_cards_all_land(self, world):
        intents = [_intent(world, f"many-{n}") for n in range(20)]
        results = await asyncio.gather(
            *[
                _tap(world, _tap_payload("skip", i["id"], update_id=10_000 + n))
                for n, i in enumerate(intents)
            ]
        )
        assert [r.outcome for r in results] == ["executed"] * 20
        states = [_state(world, i["id"]) for i in intents]
        assert states == ["skipped"] * 20


class TestWhoMayTap:
    def test_an_unlinked_tapper_flips_nothing_and_leaves_no_audit_row(self, world):
        i = _intent(world, "stranger-1")
        r = tap(world, "post", i["id"], tg_user=STRANGER_TG_ID)
        assert r.outcome == "unlinked" and r.show_alert is True
        assert "Settings" in r.answer_text
        assert _state(world, i["id"]) == "awaiting_approval"
        assert _audit(world, i["id"]) == []

    def test_an_unbound_chat_is_answered_not_connected(self, world):
        i = _intent(world, "nochat-1")
        r = tap(world, "post", i["id"], chat="-1999")
        assert r.outcome == "unknown_binding" and r.show_alert is True
        assert _state(world, i["id"]) == "awaiting_approval"

    def test_a_card_of_another_workspace_is_not_found(self, world):
        r = tap(world, "post", str(uuid.uuid4()))
        assert r.outcome == "not_found"


class TestATapNeverWaitsOnASend:
    def test_a_tap_supersedes_a_card_in_flight_and_commits_at_once(self, world):
        """The sender commits its claim BEFORE the provider call (phase 1 step
        8), so `sending` is a committed, unlocked state the flip meets: the
        supersede takes it, the tap commits, nothing waits on an upload."""
        i = _intent(world, "inflight-1")
        ref_a = i["cards"]["a"]
        _write(
            world,
            "UPDATE channel_outbox SET state = 'sending'"
            " WHERE intent_id = %s AND external_message_ref = %s",
            (i["id"], ref_a),
        )
        started = time.monotonic()
        r = tap(world, "skip", i["id"])
        elapsed = time.monotonic() - started
        assert r.outcome == "executed"
        assert _card_states(world, i["id"]) == {
            ref: "superseded" for ref in i["cards"].values()
        }
        assert elapsed < 5.0, f"the tap waited {elapsed:.1f}s — on what?"


class TestCardsEndInEveryTerminalState:
    def test_the_settled_card_sweep_retires_cards_of_intents_anyone_ended(self, world):
        """Whoever ended the intent — the reaper's expiry here, the pipeline's
        `posted`, a cancellation — its live cards lose their buttons and gain
        the terminal line on the sweep's beat (phase 1 step 7)."""
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from src.services.target import prompts

        i = _intent(world, "expired-1", state="awaiting_approval")
        _write(
            world,
            "UPDATE post_intents SET state = 'expired' WHERE id = %s",
            (i["id"],),
        )
        assert set(_card_states(world, i["id"]).values()) == {"sent"}

        async def sweep():
            engine = create_async_engine(
                asyncpg_url(world["ingress"]), poolclass=NullPool
            )
            try:
                maker = async_sessionmaker(engine, expire_on_commit=False)
                async with maker() as session:
                    from src.services.target import unit_of_work

                    await unit_of_work.apply_gucs(
                        session, tenant_id=world["ws"], actor_kind="system"
                    )
                    healed = await prompts.sweep_settled_cards(session, limit=50)
                    await session.commit()
                    return healed
            finally:
                await engine.dispose()

        assert asyncio.run(sweep()) >= 1
        assert set(_card_states(world, i["id"]).values()) == {"superseded"}
        lines = [p["outcome_text"] for _, p in _supersedes(world, i["id"])]
        assert len(lines) == 2 and all(
            "⌛ Expired — slot passed" in line for line in lines
        )

    def test_a_revoked_bindings_cards_do_not_starve_the_sweep(self, world):
        """A revoked group's cards cannot be edited and are not selected; the
        second beat finds nothing left to heal (structural review of #1271)."""
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from src.services.target import prompts, unit_of_work

        i = _intent(world, "revoked-1", state="awaiting_approval")
        ((revoked,),) = _write(
            world,
            "INSERT INTO channel_bindings (workspace_id, channel, external_ref, state)"
            " VALUES (%s, 'telegram_group', '-1003', 'revoked') RETURNING id",
            (world["ws"],),
            fetch=True,
        )
        _write(
            world,
            "INSERT INTO channel_outbox (workspace_id, binding_id, kind, intent_id,"
            " payload, state, external_message_ref)"
            ' VALUES (%s, %s, \'approval_prompt\', %s, \'{"v": 2, "text": "old"}\','
            " 'sent', '30001')",
            (world["ws"], revoked, i["id"]),
        )
        _write(
            world, "UPDATE post_intents SET state = 'expired' WHERE id = %s", (i["id"],)
        )

        async def sweep():
            engine = create_async_engine(
                asyncpg_url(world["ingress"]), poolclass=NullPool
            )
            try:
                maker = async_sessionmaker(engine, expire_on_commit=False)
                async with maker() as session:
                    await unit_of_work.apply_gucs(
                        session, tenant_id=world["ws"], actor_kind="system"
                    )
                    healed = await prompts.sweep_settled_cards(session, limit=50)
                    await session.commit()
                    return healed
            finally:
                await engine.dispose()

        first = asyncio.run(sweep())
        assert first >= 2  # the two active groups' cards of this intent
        states = _card_states(world, i["id"])
        assert states["30001"] == "sent", "the revoked group's card is left alone"
        assert all(v == "superseded" for r, v in states.items() if r != "30001")
        assert asyncio.run(sweep()) == 0, "nothing left to heal — no starvation"


class TestTapAdmissionOnTheLedger:
    """S.2 for taps (phase 2 step 3, F12 = 120/min/workspace): the window is
    read before the flip and debited only for a flip that ran — on the real
    `rate_counters` row, with the real window truncation."""

    def _window(self):
        from datetime import datetime, timezone

        from src.services.target import rate_counters

        # The dispatcher truncates its own clock to the minute; a test that
        # straddles the boundary would read one window and debit the next.
        # Wait it out once rather than pin a clock seam for a 0.5 % flake.
        import time as _time

        if 60 - _time.time() % 60 < 3:
            _time.sleep(3.5)
        return rate_counters.window_start(datetime.now(timezone.utc), 60)

    def _count(self, world):
        row = _one(
            world,
            "SELECT count FROM rate_counters WHERE scope = 'ws_admission'"
            " AND key = %s AND window_start = %s",
            (world["ws"], self._window()),
        )
        return None if row is None else row[0]

    def test_an_executed_tap_debits_the_workspace_and_a_repeat_does_not(self, world):
        i = _intent(world, "adm-1")
        before = self._count(world) or 0
        assert tap(world, "skip", i["id"]).outcome == "executed"
        assert self._count(world) == before + 1, "one flip, one unit"
        assert tap(world, "skip", i["id"]).outcome == "answered"
        assert self._count(world) == before + 1, "an answered repeat spends nothing"

    def test_at_the_limit_the_tap_is_told_and_the_card_keeps_its_state(self, world):
        from src.config.settings import settings

        i = _intent(world, "adm-2")
        limit = int(settings.TARGET_TAP_ADMISSION_PER_MINUTE)
        _write(
            world,
            "INSERT INTO rate_counters (scope, key, window_start, count)"
            " VALUES ('ws_admission', %s, %s, %s)"
            " ON CONFLICT (scope, key, window_start) DO UPDATE SET count = EXCLUDED.count",
            (world["ws"], self._window(), limit),
        )
        try:
            r = tap(world, "skip", i["id"])
            # The flip RAN and rolled back with the savepoint on real Postgres:
            # the row keeps its state, the counter its count, and the delivery
            # is consumed (the update_id is admitted) so a redelivery is a toast.
            assert r.outcome == "too_many" and r.show_alert is True
            assert _state(world, i["id"]) == "awaiting_approval", "nothing flipped"
            assert self._count(world) == limit, "a refused tap spends nothing"
            assert (
                _one(
                    world,
                    "SELECT count(*) FROM channel_outbox WHERE intent_id = %s"
                    " AND kind = 'prompt_supersede'",
                    (i["id"],),
                )[0]
                == 0
            ), "the flip's supersede rows rolled back too"
        finally:
            _write(
                world,
                "DELETE FROM rate_counters WHERE scope = 'ws_admission' AND key = %s",
                (world["ws"],),
            )


class TestDryRunLeavesTheTapOnTheJob:
    def test_a_post_tap_under_dry_run_snapshots_the_decision_onto_the_job(self, world):
        _write(
            world,
            "UPDATE workspaces SET dry_run_mode = true WHERE id = %s",
            (world["ws"],),
        )
        try:
            i = _intent(world, "dry-1")
            r = tap(world, "post", i["id"])
            assert r.outcome == "executed" and "dry run" in r.answer_text
            payload = _one(
                world,
                "SELECT payload FROM jobs WHERE kind = 'publish_pipeline'"
                " AND payload->>'intent_id' = %s",
                (i["id"],),
            )[0]
            assert payload["dry_run"] is True, payload
        finally:
            _write(
                world,
                "UPDATE workspaces SET dry_run_mode = false WHERE id = %s",
                (world["ws"],),
            )


#: #1286 — the tap's statement budget, counted at the cursor: the GUCs and
#: lock timeout in one, the tapper and their name in one, the intent and its
#: token in one, the flip, the job, the supersede of every binding in one,
#: the admission debit, and the savepoint pair. The route adds its dedup
#: insert and the commit. Was 19 before #1286.
TAP_STATEMENT_BUDGET = 11


class TestATapIsCheapOnRealRows:
    def test_a_post_tap_spends_at_most_its_budget(self, world):
        from sqlalchemy import event
        from sqlalchemy.engine import Engine

        statements: list[str] = []

        def listen(conn, cursor, statement, parameters, context, executemany):
            statements.append(" ".join(statement.split()))

        event.listen(Engine, "before_cursor_execute", listen)
        try:
            i = _intent(world, "post-cheap")
            r = tap(world, "post", i["id"])
        finally:
            event.remove(Engine, "before_cursor_execute", listen)

        assert r.outcome == "executed", r
        assert _state(world, i["id"]) == "approved"
        assert len(statements) <= TAP_STATEMENT_BUDGET, "\n".join(statements)
        # The shape, not just the count: each fold is present as ONE statement.
        assert sum("set_config(" in s for s in statements) == 1
        assert sum("FROM user_identities" in s for s in statements) == 1
        assert sum("has_ig_credential" in s for s in statements) == 1
        assert sum("'prompt_supersede'" in s for s in statements) == 1
        assert not any(s.startswith("SET LOCAL") for s in statements)


# --- the review card is the workspace's to resolve (2026-09-12) --------------


def _parked(world, tag: str, *, publish_step="publish_called", op="ambiguous") -> dict:
    """A `review_required` intent as the pipeline leaves it: the day debited
    (a real bucket row, so a refund is observable), the container present,
    and the cards already SUPERSEDED by the approve tap — so a resolution can
    only reach them by ref."""
    i = _intent(world, tag, state="review_required")
    _write(
        world,
        "UPDATE post_intents SET cap_consumed_on = current_date, publish_step = %s,"
        " ig_container_id = 'c-1' WHERE id = %s",
        (publish_step, i["id"]),
    )
    if op is not None:
        # The publish permit the pipeline minted, in the state the park left it.
        _write(
            world,
            "INSERT INTO provider_operations (workspace_id, intent_id, provider, op_kind,"
            " business_key, generation, state, lease_token)"
            " VALUES (%s, %s, 'ig', 'publish', %s, 1, %s, gen_random_uuid())",
            (world["ws"], i["id"], f"ig:publish:{i['id']}:1", op),
        )
    _write(
        world,
        "INSERT INTO daily_post_counts (workspace_id, ig_account_id, local_date, count, cap_at_write)"
        " VALUES (%s, %s, current_date, 1, 3)"
        " ON CONFLICT (workspace_id, ig_account_id, local_date) DO UPDATE SET count = 1",
        (world["ws"], world["iga"]),
    )
    _write(
        world,
        "UPDATE channel_outbox SET state = 'superseded',"
        ' payload = payload || \'{"outcome_text": "✅ Approved by Ada · earlier"}\''
        " WHERE intent_id = %s AND kind = 'approval_prompt'",
        (i["id"],),
    )
    return i


def _op_state(world, intent_id):
    row = _one(
        world,
        "SELECT state, response_ref FROM provider_operations"
        " WHERE intent_id = %s AND op_kind = 'publish' ORDER BY generation DESC LIMIT 1",
        (intent_id,),
    )
    if row is None:
        return None
    ref = row[1] if isinstance(row[1], dict) or row[1] is None else json.loads(row[1])
    return row[0], ref


def _day_count(world):
    row = _one(
        world,
        "SELECT count FROM daily_post_counts WHERE workspace_id = %s AND ig_account_id = %s"
        " AND local_date = current_date",
        (world["ws"], world["iga"]),
    )
    return row[0] if row else 0


def _intent_cols(world, intent_id):
    return _one(
        world,
        "SELECT state, publish_step, published_via, cap_refunded_at, attempts_by_step"
        " FROM post_intents WHERE id = %s",
        (intent_id,),
    )


class TestTheReviewCardIsTheTenantsToResolve:
    """`review_required` is the workspace's to resolve (ruling 2026-09-12):
    real rows, real triggers, the tap's own connection and GUCs — the audit
    row names the tapper, the resolution reaches the superseded cards by
    ref in BOTH groups, and each `02` §4 edge keeps its cap rule."""

    def test_retry_reapproves_debit_neutral_mints_the_publish_and_restates_both_cards(
        self, world
    ):
        i = _parked(world, "review-retry")
        assert _day_count(world) == 1
        r = tap(world, "notposted", i["id"])
        assert r.outcome == "executed", r.answer_text
        assert "again" in r.answer_text.lower()
        # The member's verdict ended the ambiguous op: the rail never sees a
        # second permitted publish call beside an unresolved one.
        state, ref = _op_state(world, i["id"])
        assert state == "failed" and ref["verdict"] == "not_posted"
        assert ref["by"] == world["user"]
        state, step, _via, refunded, attempts = _intent_cols(world, i["id"])
        assert (state, step) == ("approved", "none")
        assert refunded is None and _day_count(world) == 0, (
            "debit-neutral: the recorded day is refunded; the next flip re-debits"
        )
        assert attempts["retries"] == 1 and attempts["v"] == 1
        assert _audit(world, i["id"]) == [
            ("approved", "user", world["user"], "telegram")
        ]
        ((kind, key, payload),) = _write(
            world,
            "SELECT kind, serialization_key, payload FROM jobs"
            " WHERE payload->>'intent_id' = %s AND kind = 'publish_pipeline'",
            (i["id"],),
            fetch=True,
        )
        assert key == "ig:acct-w4-tap"
        assert (payload if isinstance(payload, dict) else json.loads(payload))[
            "dry_run"
        ] is False
        edits = _supersedes(world, i["id"])
        assert len(edits) == 2 and {b for b, _ in edits} == set(
            world["bindings"].values()
        )
        for _b, p in edits:
            assert "Approved by Ada" in p["outcome_text"]
            assert p["supersedes_ref"] in i["cards"].values()
            assert "reply_markup" not in p, "the review buttons go with the line"
        assert set(_card_states(world, i["id"]).values()) == {"superseded"}

    def test_itposted_confirms_a_publish_call_with_the_posted_effects(self, world):
        i = _parked(world, "review-posted")
        r = tap(world, "itposted", i["id"])
        assert r.outcome == "executed", r.answer_text
        state, ref = _op_state(world, i["id"])
        assert state == "succeeded" and ref["verdict"] == "posted"
        state, step, via, refunded, _ = _intent_cols(world, i["id"])
        assert (state, step, via) == ("posted", "effect_confirmed", "api")
        assert refunded is None and _day_count(world) == 1, "the debit stands"
        assert _audit(world, i["id"]) == [("posted", "user", world["user"], "telegram")]
        (times,) = _one(
            world, "SELECT times_posted FROM media_items WHERE id = %s", (i["media"],)
        )
        assert times == 1
        (locks,) = _one(
            world,
            "SELECT count(*) FROM post_locks WHERE media_item_id = %s AND kind = 'recent'"
            " AND created_by_user_id = %s",
            (i["media"], world["user"]),
        )
        assert locks == 1
        assert all(
            "Posted by Ada" in p["outcome_text"] for _, p in _supersedes(world, i["id"])
        )

    def test_itposted_without_a_publish_call_is_told_why_and_writes_nothing(
        self, world
    ):
        i = _parked(world, "review-noposted", publish_step="container_ready", op=None)
        r = tap(world, "itposted", i["id"])
        assert r.outcome == "nothing_to_confirm" and r.show_alert is True
        assert "post again" in r.answer_text.lower()
        assert _intent_cols(world, i["id"])[0] == "review_required"
        assert _audit(world, i["id"]) == [] and _supersedes(world, i["id"]) == []

    def test_itposted_after_instagram_answered_no_is_refused_the_same_way(self, world):
        """`publish_step` stays `publish_called` when the permit resolved
        `failed`; the op's state is the discriminator, not the step."""
        i = _parked(world, "review-refused", op="failed")
        r = tap(world, "itposted", i["id"])
        assert r.outcome == "nothing_to_confirm"
        assert _intent_cols(world, i["id"])[0] == "review_required"
        assert _op_state(world, i["id"])[0] == "failed"

    def test_giveup_cancels_and_keeps_the_debit(self, world):
        i = _parked(world, "review-giveup")
        r = tap(world, "giveup", i["id"])
        assert r.outcome == "executed" and "Cancelled" in r.answer_text
        state, _, _, refunded, _ = _intent_cols(world, i["id"])
        assert state == "cancelled" and refunded is None
        assert _op_state(world, i["id"])[0] == "failed", (
            "the op retires with the intent"
        )
        assert _day_count(world) == 1, (
            "`02` §4: a review may have published — no refund"
        )
        assert _audit(world, i["id"]) == [
            ("cancelled", "user", world["user"], "telegram")
        ]
        assert all(
            "Cancelled by Ada" in p["outcome_text"]
            for _, p in _supersedes(world, i["id"])
        )

    def test_a_review_tap_on_a_card_that_moved_on_answers(self, world):
        i = _parked(world, "review-moved")
        assert tap(world, "notposted", i["id"]).outcome == "executed"
        r = tap(world, "giveup", i["id"])
        assert r.outcome == "answered" and "Approved" in r.answer_text
        assert _intent_cols(world, i["id"])[0] == "approved"
        assert len(_audit(world, i["id"])) == 1, "the second tap wrote nothing"
