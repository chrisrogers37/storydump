"""L.1 — the intent-ledger gate (#858, `04` §L.1).

`04` §L.1's gate, verbatim: *"model-based transition tests reject every
illegal/double transition **via raw SQL as well as the service** (the trigger,
not the service, is the authority); terminal rows reject every UPDATE; an
actor-less state change raises; an actor-less raw-SQL mutation on any of the
five governance tables raises."*

**Everything here goes at the database directly.** Driving the service and
watching it refuse would prove the *service* refuses, which is a different and
weaker claim: the whole point of putting the rule in a trigger is that it holds
when the service is bypassed. So these tests open a psycopg2 connection and
issue the UPDATE themselves.

## Every refusal is paired with a rowcount-checked positive control

This is not ceremony, and the reason is a defect this suite caught before it
was written. A first probe asserted "an actor-less mutation on each of the five
governance tables raises" and appeared to pass for all five. Two of them —
`channel_bindings` and `oauth_credentials` — were **not raising at all**: the
shared seed chain creates no rows in those tables, so the UPDATE matched
nothing, the row-level AFTER trigger never fired, and the statement succeeded
trivially. The test would have been green, permanently, about nothing.

A refusal test only means something if the same statement **does** something
when the guard is satisfied. So each pair runs:

1. the statement with no `app.actor_kind` — must raise, **and the message must
   name `app.actor_kind`**, not merely be an exception; and
2. the same statement with an actor — must succeed **and report `rowcount >=
   1`**, which is what proves arm 1 was refused rather than vacuous.

## The transition matrix is exhaustive and derived, not enumerated

The legal edges come from `post_intent_transitions` at runtime; the state list
comes from `ck_intent_state`. Writing either down here would be a second copy
of the authority — the same mistake the service module refuses to make — and
would go stale silently the first time the plan adds an edge.
"""

from __future__ import annotations

import re

import psycopg2
import pytest

from tests.scripts.conftest import replay_advertised_stream, seed_workspace_chain

pytestmark = [pytest.mark.integration, pytest.mark.slow]

#: The five tables `02` §4 puts under the governance audit trigger.
GOVERNANCE_TABLES = (
    "workspaces",
    "workspace_members",
    "ig_accounts",
    "channel_bindings",
    "oauth_credentials",
)

TERMINAL = ("posted", "skipped", "rejected", "expired", "failed", "cancelled")


@pytest.fixture()
def ledger(owner_window_db, owner_actor, admin_conn):
    """A replayed target schema plus the facts the matrix is derived from."""
    dsn = replay_advertised_stream(owner_window_db, owner_actor, admin_conn)
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT from_state, to_state FROM post_intent_transitions")
        edges = {(a, b) for a, b in cur.fetchall()}
        cur.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
            " WHERE conname = 'ck_intent_state'"
        )
        states = tuple(re.findall(r"'([a-z_]+)'::text", cur.fetchone()[0]))
    chain = seed_workspace_chain(conn, "l1-gate")
    # seed_workspace_chain runs its chain in ONE transaction and therefore
    # leaves autocommit OFF. Without restoring it, every row minted below sits
    # in an uncommitted transaction, invisible to the separate connections
    # `_attempt` opens — so the UPDATEs match zero rows, no row-level trigger
    # fires, and the refusal tests pass having refused nothing. That is the
    # exact vacuity this suite is built to catch, and the rowcount control is
    # what surfaced it.
    conn.autocommit = True
    yield {
        "dsn": dsn,
        "conn": conn,
        "edges": edges,
        "states": states,
        "chain": chain,
        "seq": 0,
    }
    conn.close()


def _new_intent(ledger, state: str) -> str:
    """An intent born directly in *state*, on its own fresh media item.

    Two constraints shape this and both were found by running it:

    * ``uq_intent_live_subject`` is unique on ``(workspace_id, media_item_id,
      ig_account_id)`` for non-terminal rows, and the shared seed chain already
      holds one live intent on that triple — so every intent here gets a NEW
      media item rather than reusing the chain's.
    * The state-completeness CHECKs (``ck_posted_complete``,
      ``ck_publishing_debited``, ``ck_ambiguous_called``) mean several states
      cannot simply be asserted into existence. Each is satisfied by its own
      documented route: ``posted`` via the ``legacy_backfill`` exemption, the
      publishing pair by carrying a cap debit, and ``publishing_ambiguous`` by
      also recording that publish was called.

    The birth itself uses the migration door ``trg_intent_insert_guard``
    provides (``app.actor_kind = 'migration'`` is its documented exemption from
    born-scheduled). That is deliberate: it isolates the UPDATE guard from
    path-dependence, so a state the edge graph cannot reach from ``scheduled``
    is still testable, and these assertions are about the update rule rather
    than about how the row got there. The insert guard is tested separately,
    including that this exemption still exists.
    """
    c = ledger["chain"]
    extra_cols, extra_vals = "", []
    if state == "posted":
        # `ck_posted_complete` exempts the legacy_backfill route, which is the
        # only way to assert a `posted` row into existence without also
        # fabricating a container id, publish step and cap debit.
        extra_cols = ", published_via"
        extra_vals = ["legacy_backfill"]
    elif state in ("publishing", "publishing_ambiguous"):
        extra_cols = ", cap_consumed_on, publish_step"
        extra_vals = [
            "2026-01-01",
            "publish_called" if state == "publishing_ambiguous" else "none",
        ]

    with ledger["conn"].cursor() as cur:
        cur.execute("SET app.actor_kind = 'migration'")
        ledger["seq"] += 1
        tag = f"l1-{ledger['seq']}"
        cur.execute(
            "INSERT INTO media_items (workspace_id, source_id, content_hash,"
            " file_name, media_kind, provider_file_ref)"
            " VALUES (%s, %s, %s, %s, 'image', %s) RETURNING id",
            (c["ws"], c["src"], tag, f"{tag}.jpg", tag),
        )
        media = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO post_intents (workspace_id, ig_account_id, media_item_id,"
            f" provider_account_ref, approval_mode, schedule_slot_at, state{extra_cols})"
            f" VALUES (%s, %s, %s, %s, 'manual', now(), %s"
            f"{', %s' * len(extra_vals)}) RETURNING id",
            # provider_account_ref is unique per intent: `uq_publish_exclusive`
            # is a partial unique index on it for the publishing states, so a
            # shared literal makes the second publishing intent collide.
            (c["ws"], c["iga"], media, tag, state, *extra_vals),
        )
        return cur.fetchone()[0]


#: What each destination state requires the SAME update to also set.
#:
#: The edge table says a transition is PERMITTED; the `02` §3 completeness
#: CHECKs say what else must be true of the row once it lands there. Both are
#: the database's rules and neither subsumes the other — measured: six legal
#: edges are refused outright if the companion columns are absent. This is the
#: shape L.5's flip transaction has to satisfy, so it is asserted here in both
#: directions rather than worked around.
COMPLETION = {
    "publishing": ", cap_consumed_on = DATE '2026-01-01'",
    "publishing_ambiguous": (
        ", cap_consumed_on = DATE '2026-01-01', publish_step = 'publish_called'"
    ),
    "posted": ", published_via = 'legacy_backfill'",
}


def _attempt(ledger, sql, params, *, actor="system"):
    """Run *sql* on a fresh connection. Returns (ok, message, rowcount)."""
    conn = psycopg2.connect(ledger["dsn"])
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            if actor is not None:
                cur.execute("SET app.actor_kind = %s", (actor,))
            cur.execute(sql, params)
            return True, "", cur.rowcount
    except Exception as exc:  # noqa: BLE001 — the message is the assertion
        return False, str(exc).strip(), 0
    finally:
        conn.close()


class TestTheTransitionMatrixIsEnforcedByTheTriggerNotTheService:
    """Every legal edge succeeds and every illegal one raises — via raw SQL."""

    def test_the_matrix_is_big_enough_to_be_worth_asserting(self, ledger):
        """Positive control on the derivation itself. If the edge query or the
        state parse returned nothing, every matrix test below would pass by
        having no cases — the vacuity this suite exists to avoid."""
        assert len(ledger["states"]) >= 13, ledger["states"]
        assert len(ledger["edges"]) >= 27, len(ledger["edges"])

    def test_every_legal_edge_is_accepted_when_the_row_is_complete(self, ledger):
        """Each edge, with whatever `02` §3 requires the destination to carry."""
        rejected = []
        for src, dst in sorted(ledger["edges"]):
            if src in TERMINAL:
                continue  # terminal immutability is a separate, stronger rule
            intent = _new_intent(ledger, src)
            ok, msg, _ = _attempt(
                ledger,
                f"UPDATE post_intents SET state=%s{COMPLETION.get(dst, '')}"
                " WHERE id=%s",
                (dst, intent),
            )
            if not ok:
                rejected.append((src, dst, msg.splitlines()[0]))
        assert rejected == [], f"legal edges refused by the guard: {rejected}"

    def test_a_legal_edge_is_STILL_refused_if_the_row_is_left_incomplete(self, ledger):
        """The other half, and the one L.5 will lean on: the edge table permits
        the move, and the completeness CHECK still refuses it when the update
        does not carry what the destination requires — a flip that forgets to
        debit the cap does not land."""
        checked, allowed = 0, []
        for src, dst in sorted(ledger["edges"]):
            if src in TERMINAL or dst not in COMPLETION:
                continue
            checked += 1
            intent = _new_intent(ledger, src)
            ok, msg, _ = _attempt(
                ledger, "UPDATE post_intents SET state=%s WHERE id=%s", (dst, intent)
            )
            if ok:
                allowed.append((src, dst))
            else:
                assert "violates check constraint" in msg, (
                    f"{src}->{dst} incomplete was refused, but not by a "
                    f"completeness CHECK: {msg.splitlines()[0]}"
                )
        assert checked >= 5, f"only {checked} completeness-bearing edges examined"
        assert allowed == [], (
            f"these edges landed without their required columns: {allowed}"
        )

    def test_every_ILLEGAL_edge_is_refused_by_the_trigger(self, ledger):
        """The exhaustive half: all non-edges over the full state cross-product,
        excluding same-state (a no-op by design — see its own test) and
        terminal sources (covered by immutability)."""
        accepted = []
        checked = 0
        for src in ledger["states"]:
            if src in TERMINAL:
                continue
            intent = _new_intent(ledger, src)
            for dst in ledger["states"]:
                if dst == src or (src, dst) in ledger["edges"]:
                    continue
                checked += 1
                ok, msg, _ = _attempt(
                    ledger,
                    f"UPDATE post_intents SET state=%s{COMPLETION.get(dst, '')}"
                    " WHERE id=%s",
                    (dst, intent),
                )
                if ok:
                    accepted.append((src, dst))
                else:
                    assert "illegal transition" in msg, (
                        f"{src}->{dst} was refused, but not by the transition "
                        f"guard: {msg.splitlines()[0]}"
                    )
        assert checked > 50, f"only {checked} illegal pairs examined — too few"
        assert accepted == [], f"illegal transitions the trigger allowed: {accepted}"


class TestDoubleTransition:
    """The gate says "reject every illegal/double transition". Measured, a
    same-state write is a NO-OP rather than a refusal — so this asserts the
    property that actually holds, not the phrasing."""

    def test_a_same_state_write_succeeds_but_forges_no_second_ledger_entry(
        self, ledger
    ):
        intent = _new_intent(ledger, "scheduled")
        with ledger["conn"].cursor() as cur:
            cur.execute("SET app.actor_kind = 'system'")
            cur.execute(
                "SELECT entered_state_at FROM post_intents WHERE id=%s", (intent,)
            )
            before = cur.fetchone()[0]
            cur.execute(
                "SELECT count(*) FROM audit_events WHERE entity_id=%s", (intent,)
            )
            audits_before = cur.fetchone()[0]

        ok, msg, _ = _attempt(
            ledger,
            "UPDATE post_intents SET state='scheduled' WHERE id=%s",
            (intent,),
        )
        assert ok, f"same-state write raised: {msg}"

        with ledger["conn"].cursor() as cur:
            cur.execute(
                "SELECT entered_state_at FROM post_intents WHERE id=%s", (intent,)
            )
            after = cur.fetchone()[0]
            cur.execute(
                "SELECT count(*) FROM audit_events WHERE entity_id=%s", (intent,)
            )
            audits_after = cur.fetchone()[0]
        assert after == before, "a same-state write advanced entered_state_at"
        assert audits_after == audits_before, (
            "a same-state write produced an audit row — a repeated transition "
            "could then forge a second ledger entry"
        )

    def test_replaying_a_COMPLETED_transition_is_refused(self, ledger):
        """The structural half: after A->B you are no longer in A, so the same
        edge no longer matches. This is what actually prevents a double."""
        intent = _new_intent(ledger, "scheduled")
        ok, _, _ = _attempt(
            ledger,
            "UPDATE post_intents SET state='prompt_pending' WHERE id=%s",
            (intent,),
        )
        assert ok
        ok2, msg2, _ = _attempt(
            ledger, "UPDATE post_intents SET state='scheduled' WHERE id=%s", (intent,)
        )
        assert not ok2 and "illegal transition" in msg2, msg2


class TestTerminalRowsRejectEveryUpdate:
    def test_a_terminal_row_refuses_a_STATE_change(self, ledger):
        bad = []
        for state in TERMINAL:
            intent = _new_intent(ledger, state)
            ok, msg, _ = _attempt(
                ledger,
                "UPDATE post_intents SET state='scheduled' WHERE id=%s",
                (intent,),
            )
            if ok or "is terminal" not in msg:
                bad.append((state, "accepted" if ok else msg.splitlines()[0]))
        assert bad == [], f"terminal rows that did not refuse a state change: {bad}"

    def test_a_terminal_row_refuses_a_NON_STATE_update_too(self, ledger):
        """ "Every UPDATE", not "every state change" — the guard reads OLD.state
        before it looks at what changed. A test that only tried state changes
        would assert the weaker rule and pass."""
        bad = []
        for state in TERMINAL:
            intent = _new_intent(ledger, state)
            ok, msg, _ = _attempt(
                ledger,
                "UPDATE post_intents SET ig_permalink='x' WHERE id=%s",
                (intent,),
            )
            if ok or "is terminal" not in msg:
                bad.append((state, "accepted" if ok else msg.splitlines()[0]))
        assert bad == [], f"terminal rows that allowed a non-state update: {bad}"

    def test_a_NON_terminal_row_accepts_the_same_non_state_update(self, ledger):
        """Positive control: the refusals above are terminality, not a column
        that cannot be written."""
        intent = _new_intent(ledger, "scheduled")
        ok, msg, rows = _attempt(
            ledger, "UPDATE post_intents SET ig_permalink='x' WHERE id=%s", (intent,)
        )
        assert ok and rows == 1, msg


class TestAnActorLessStateChangeRaises:
    def test_it_raises_and_names_the_guc(self, ledger):
        intent = _new_intent(ledger, "scheduled")
        ok, msg, _ = _attempt(
            ledger,
            "UPDATE post_intents SET state='prompt_pending' WHERE id=%s",
            (intent,),
            actor=None,
        )
        assert not ok and "app.actor_kind" in msg, msg

    def test_the_same_change_WITH_an_actor_succeeds(self, ledger):
        """Rowcount-checked positive control — otherwise the refusal above
        could be any failure at all."""
        intent = _new_intent(ledger, "scheduled")
        ok, msg, rows = _attempt(
            ledger,
            "UPDATE post_intents SET state='prompt_pending' WHERE id=%s",
            (intent,),
        )
        assert ok and rows == 1, msg


class TestActorLessGovernanceMutationsRaiseOnAllFiveTables:
    """The finding that shaped this suite: two of the five had no seeded row,
    so the refusal test passed on a statement that matched nothing."""

    @pytest.fixture()
    def seeded(self, ledger):
        """Guarantee a row in every governance table, including the two the
        shared chain does not create."""
        c = ledger["chain"]
        with ledger["conn"].cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            cur.execute(
                "INSERT INTO channel_bindings (workspace_id, channel, external_ref)"
                " VALUES (%s, 'telegram_group', 'ext-l1') ON CONFLICT DO NOTHING",
                (c["ws"],),
            )
            cur.execute(
                "INSERT INTO oauth_credentials (workspace_id, ig_account_id, provider,"
                " encrypted_payload) VALUES (%s, %s, 'ig_login', 'x')"
                " ON CONFLICT DO NOTHING",
                (c["ws"], c["iga"]),
            )
        return c

    STATEMENTS = {
        "workspaces": "UPDATE workspaces SET name='gate' WHERE id=%s",
        "workspace_members": "UPDATE workspace_members SET role='owner' WHERE workspace_id=%s",
        "ig_accounts": "UPDATE ig_accounts SET handle='gate' WHERE workspace_id=%s",
        "channel_bindings": "UPDATE channel_bindings SET state='revoked' WHERE workspace_id=%s",
        "oauth_credentials": "UPDATE oauth_credentials SET state='revoked' WHERE workspace_id=%s",
    }

    def test_the_statements_ACTUALLY_TOUCH_A_ROW(self, ledger, seeded):
        """The control that makes the refusal test below mean anything. It is a
        SEPARATE test, deliberately: a vacuous refusal then reports as "matched
        0 rows" rather than hiding inside a passing assertion."""
        vacuous = []
        for table in GOVERNANCE_TABLES:
            ok, msg, rows = _attempt(ledger, self.STATEMENTS[table], (seeded["ws"],))
            if not ok:
                vacuous.append((table, f"control failed: {msg.splitlines()[0]}"))
            elif rows < 1:
                vacuous.append((table, "matched 0 rows"))
        assert vacuous == [], (
            f"these refusal tests would pass vacuously — seed a row first: {vacuous}"
        )

    def test_without_an_actor_they_raise_naming_the_guc(self, ledger, seeded):
        bad = []
        for table in GOVERNANCE_TABLES:
            ok, msg, _ = _attempt(
                ledger, self.STATEMENTS[table], (seeded["ws"],), actor=None
            )
            if ok:
                bad.append((table, "SUCCEEDED with no actor"))
            elif "app.actor_kind" not in msg:
                bad.append((table, f"wrong reason: {msg.splitlines()[0]}"))
        assert bad == [], f"governance tables not guarded by actor_kind: {bad}"


class TestTheInsertGuard:
    def test_an_intent_cannot_be_BORN_non_scheduled(self, ledger):
        c = ledger["chain"]
        ok, msg, _ = _attempt(
            ledger,
            "INSERT INTO post_intents (workspace_id, ig_account_id, media_item_id,"
            " provider_account_ref, approval_mode, schedule_slot_at, state,"
            " published_via)"
            " VALUES (%s, %s, %s, 'guard-a', 'manual', now(), 'posted',"
            " 'legacy_backfill')",
            (c["ws"], c["iga"], c["media"]),
        )
        assert not ok and "born scheduled" in msg, msg

    def test_the_migration_actor_is_the_documented_exemption(self, ledger):
        """Positive control on the same statement, and the door `_new_intent`
        relies on — so if the exemption is ever removed, this suite says so
        rather than silently losing its ability to reach a state."""
        c = ledger["chain"]
        ok, msg, rows = _attempt(
            ledger,
            "INSERT INTO post_intents (workspace_id, ig_account_id, media_item_id,"
            " provider_account_ref, approval_mode, schedule_slot_at, state,"
            " published_via)"
            " VALUES (%s, %s, %s, 'guard-b', 'manual', now(), 'posted',"
            " 'legacy_backfill')",
            (c["ws"], c["iga"], c["media"]),
            actor="migration",
        )
        assert ok and rows == 1, msg


class TestTheGuardsAreLoadBearing:
    """Each refusal above is caused by the trigger it names — proven by
    removing the trigger and watching the refusal disappear.

    This class exists because of a failure mode this repo hit five times in one
    review cycle: a check that runs, passes, and is about something ADJACENT to
    what it names. The sharpest instance could not be caught by mutating the
    code under test, because the test had mocked that code away — reverting the
    bug WAS the mutation and nothing moved.

    Raw SQL against a real trigger is the structural answer: there is nothing
    mocked, so removing the trigger genuinely changes the observable. Each case
    below asserts the statement is refused WITH the trigger and accepted
    WITHOUT it. A test that passed for an adjacent reason — a constraint, a
    missing row, a typo'd column — would keep failing after the DROP and be
    caught here.

    Safe to do destructively: every test gets its own freshly replayed
    database from the module fixture.
    """

    def _drop(self, ledger, trigger: str, table: str) -> None:
        with ledger["conn"].cursor() as cur:
            cur.execute(f"DROP TRIGGER {trigger} ON {table}")

    def test_the_transition_guard_is_what_refuses_an_illegal_edge(self, ledger):
        intent = _new_intent(ledger, "scheduled")
        ok, msg, _ = _attempt(
            ledger, "UPDATE post_intents SET state='approved' WHERE id=%s", (intent,)
        )
        assert not ok and "illegal transition" in msg, msg

        self._drop(ledger, "tg_intent_guard", "post_intents")
        intent2 = _new_intent(ledger, "scheduled")
        ok2, msg2, rows2 = _attempt(
            ledger, "UPDATE post_intents SET state='approved' WHERE id=%s", (intent2,)
        )
        assert ok2 and rows2 == 1, (
            f"the illegal edge was still refused after dropping tg_intent_guard, "
            f"so the refusal above was NOT the transition guard: {msg2}"
        )

    def test_the_transition_guard_is_also_what_enforces_terminal_immutability(
        self, ledger
    ):
        intent = _new_intent(ledger, "cancelled")
        ok, msg, _ = _attempt(
            ledger, "UPDATE post_intents SET ig_permalink='x' WHERE id=%s", (intent,)
        )
        assert not ok and "is terminal" in msg, msg

        self._drop(ledger, "tg_intent_guard", "post_intents")
        intent2 = _new_intent(ledger, "cancelled")
        ok2, msg2, rows2 = _attempt(
            ledger, "UPDATE post_intents SET ig_permalink='x' WHERE id=%s", (intent2,)
        )
        assert ok2 and rows2 == 1, f"still refused without the guard: {msg2}"

    def test_the_audit_trigger_is_what_refuses_an_actor_less_state_change(self, ledger):
        intent = _new_intent(ledger, "scheduled")
        ok, msg, _ = _attempt(
            ledger,
            "UPDATE post_intents SET state='prompt_pending' WHERE id=%s",
            (intent,),
            actor=None,
        )
        assert not ok and "app.actor_kind" in msg, msg

        self._drop(ledger, "tg_intent_audit", "post_intents")
        intent2 = _new_intent(ledger, "scheduled")
        ok2, msg2, rows2 = _attempt(
            ledger,
            "UPDATE post_intents SET state='prompt_pending' WHERE id=%s",
            (intent2,),
            actor=None,
        )
        assert ok2 and rows2 == 1, (
            f"still refused without tg_intent_audit — the actor-less refusal "
            f"came from somewhere else: {msg2}"
        )

    def test_the_insert_guard_is_what_forces_born_scheduled(self, ledger):
        c = ledger["chain"]
        sql = (
            "INSERT INTO post_intents (workspace_id, ig_account_id, media_item_id,"
            " provider_account_ref, approval_mode, schedule_slot_at, state,"
            " published_via) VALUES (%s, %s, %s, %s, 'manual', now(), 'posted',"
            " 'legacy_backfill')"
        )
        ok, msg, _ = _attempt(ledger, sql, (c["ws"], c["iga"], c["media"], "lb-1"))
        assert not ok and "born scheduled" in msg, msg

        self._drop(ledger, "tg_intent_insert_guard", "post_intents")
        ok2, msg2, rows2 = _attempt(
            ledger, sql, (c["ws"], c["iga"], c["media"], "lb-2")
        )
        assert ok2 and rows2 == 1, f"still refused without the insert guard: {msg2}"

    def test_the_governance_trigger_is_what_refuses_an_actor_less_mutation(
        self, ledger
    ):
        ws = ledger["chain"]["ws"]
        sql = "UPDATE workspaces SET name='lb' WHERE id=%s"
        ok, msg, _ = _attempt(ledger, sql, (ws,), actor=None)
        assert not ok and "app.actor_kind" in msg, msg

        self._drop(ledger, "tg_audit_workspaces", "workspaces")
        ok2, msg2, rows2 = _attempt(ledger, sql, (ws,), actor=None)
        assert ok2 and rows2 == 1, f"still refused without tg_audit_workspaces: {msg2}"


class TestTheServicePathAgreesWithTheTrigger:
    """The gate's "as well as the service" half.

    Deliberately the SMALLER half. The service issues the UPDATE and lets the
    trigger decide — it holds no copy of the edge set — so what is worth
    asserting is that the service surfaces the database's refusal rather than
    substituting a judgement of its own. If these ever diverge from the raw-SQL
    results above, the service has grown an authority it should not have.
    """

    def _async_dsn(self, ledger) -> str:
        return ledger["dsn"].replace("postgresql://", "postgresql+asyncpg://", 1)

    @pytest.mark.asyncio
    async def test_a_legal_transition_goes_through_the_service(self, ledger):
        from sqlalchemy.ext.asyncio import create_async_engine

        from src.services.target.intent_ledger import current_state, transition
        from src.services.target.unit_of_work import unit_of_work

        intent = _new_intent(ledger, "scheduled")
        engine = create_async_engine(
            self._async_dsn(ledger), pool_size=1, max_overflow=0
        )
        try:
            uow = unit_of_work(engine, ledger["chain"]["ws"], actor_kind="system")
            async with uow.begin() as session:
                await transition(session, intent, "prompt_pending")
                assert await current_state(session, intent) == "prompt_pending"
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_an_illegal_transition_surfaces_the_TRIGGERS_refusal(self, ledger):
        from sqlalchemy.ext.asyncio import create_async_engine

        from src.services.target.intent_ledger import (
            IntentTransitionRefused,
            transition,
        )
        from src.services.target.unit_of_work import unit_of_work

        intent = _new_intent(ledger, "scheduled")
        engine = create_async_engine(
            self._async_dsn(ledger), pool_size=1, max_overflow=0
        )
        try:
            uow = unit_of_work(engine, ledger["chain"]["ws"], actor_kind="system")
            with pytest.raises(IntentTransitionRefused, match="illegal transition"):
                async with uow.begin() as session:
                    await transition(session, intent, "approved")
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_the_service_holds_no_copy_of_the_edge_set(self, ledger):
        """The design assertion. `legal_transitions` must read the table, so a
        plan change to the edges is picked up without a code change — and, more
        importantly, so no second authority exists to drift from it."""
        from sqlalchemy.ext.asyncio import create_async_engine

        from src.services.target.intent_ledger import legal_transitions
        from src.services.target.unit_of_work import unit_of_work

        engine = create_async_engine(
            self._async_dsn(ledger), pool_size=1, max_overflow=0
        )
        try:
            uow = unit_of_work(engine, ledger["chain"]["ws"], actor_kind="system")
            async with uow.begin() as session:
                from_db = await legal_transitions(session)
            assert from_db == ledger["edges"]

            # And it is genuinely read, not memoised from a literal: delete an
            # edge and the service reports the smaller set.
            with ledger["conn"].cursor() as cur:
                cur.execute(
                    "DELETE FROM post_intent_transitions"
                    " WHERE from_state='scheduled' AND to_state='expired'"
                )
            async with uow.begin() as session:
                after = await legal_transitions(session)
            assert ("scheduled", "expired") not in after
            assert len(after) == len(from_db) - 1
        finally:
            await engine.dispose()
