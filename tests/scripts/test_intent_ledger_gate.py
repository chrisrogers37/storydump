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
import uuid

import psycopg2
import pytest

from tests.scripts.conftest import (
    as_user,
    async_url,
    ingress_engine,
    replay_advertised_stream,
    seed_workspace_chain,
)

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


def _new_media(ledger) -> str:
    """A fresh media item on the chain's source (`uq_intent_live_subject`
    admits one live intent per item and account)."""
    c = ledger["chain"]
    ledger["seq"] += 1
    tag = f"l1m-{ledger['seq']}"
    with ledger["conn"].cursor() as cur:
        cur.execute("SET app.actor_kind = 'migration'")
        cur.execute(
            "INSERT INTO media_items (workspace_id, source_id, content_hash,"
            " file_name, media_kind, provider_file_ref)"
            " VALUES (%s, %s, %s, %s, 'image', %s) RETURNING id",
            (c["ws"], c["src"], tag, f"{tag}.jpg", tag),
        )
        return cur.fetchone()[0]


def _new_intent(ledger, state: str, *, origin: str = "cadence") -> str:
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

    *origin* is written only when it is not the column default, so a cadence
    row is inserted exactly as it was before 088 (`origin` is fixed at birth:
    a planned row has to be BORN planned).
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

    if origin != "cadence":
        extra_cols += ", origin"
        extra_vals = [*extra_vals, origin]

    media = _new_media(ledger)
    tag = f"l1-{ledger['seq']}"
    with ledger["conn"].cursor() as cur:
        cur.execute("SET app.actor_kind = 'migration'")
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


def _attempt(ledger, sql, params, *, actor="system", user_id=None, reused=False):
    """Run *sql* on a fresh connection. Returns (ok, message, rowcount).

    *reused* first commits a transaction that claims an actor, which is the
    state a pooled connection is in: an unset `app.actor_kind` then reads ''
    rather than NULL (#1421), and a refusal must not depend on which."""
    conn = psycopg2.connect(ledger["dsn"])
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            if reused:
                cur.execute("BEGIN; SET LOCAL app.actor_kind = 'system'; COMMIT")
            if actor is not None:
                cur.execute("SET app.actor_kind = %s", (actor,))
            if user_id is not None:
                cur.execute("SET app.actor_user_id = %s", (str(user_id),))
            cur.execute(sql, params)
            return True, "", cur.rowcount
    except Exception as exc:  # noqa: BLE001 — the message is the assertion
        return False, str(exc).strip(), 0
    finally:
        conn.close()


def _names_the_guc(msg: str) -> bool:
    """The refusal is the trigger's own. Only the error's first line counts:
    psycopg2 appends a CONTEXT quoting the audit INSERT, which reads
    `current_setting('app.actor_kind')`, so a `ck_audit_actor` failure also
    contains the name further down."""
    return "app.actor_kind" in msg.splitlines()[0]


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
        excluding same-state (refused by `061` — see its own test) and
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
    """The gate says "reject every illegal/double transition". A same-state
    write was measured a NO-OP until #883; `061` makes it a refusal, and that
    is what this asserts."""

    def test_a_same_state_write_is_REFUSED_and_moves_nothing(self, ledger):
        """#883 closed this. Before `061` a same-state write SUCCEEDED as a
        no-op, and this test asserted that — correctly, because asserting the
        gate's wording would have asserted something false at the time.

        The ledger-integrity half is unchanged and still asserted: no audit
        row, `entered_state_at` unmoved. What changed is that those now hold
        because the write was REFUSED rather than because it was inert, which
        is the stronger property and the one a caller can act on.
        """
        intent = _new_intent(ledger, "scheduled")
        with ledger["conn"].cursor() as cur:
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
        assert not ok, "a same-state write was accepted — #883 has regressed"
        assert "same-state" in msg, f"refused, but not by the #883 guard: {msg}"

        with ledger["conn"].cursor() as cur:
            cur.execute(
                "SELECT entered_state_at FROM post_intents WHERE id=%s", (intent,)
            )
            after = cur.fetchone()[0]
            cur.execute(
                "SELECT count(*) FROM audit_events WHERE entity_id=%s", (intent,)
            )
            audits_after = cur.fetchone()[0]
        assert after == before, (
            "a refused same-state write still advanced entered_state_at"
        )
        assert audits_after == audits_before, (
            "a refused same-state write wrote an audit row"
        )

    def test_a_NON_state_update_is_untouched_by_the_self_transition_guard(self, ledger):
        """The other half of `061`, and the reason it is `UPDATE OF state`.

        A checkpoint update does not name `state`, so the guard must never
        fire on it. Without this, the fix for #883 would silently forbid every
        checkpoint write — a far worse defect than the one it closes.
        """
        intent = _new_intent(ledger, "scheduled")
        ok, msg, rows = _attempt(
            ledger, "UPDATE post_intents SET ig_permalink='x' WHERE id=%s", (intent,)
        )
        assert ok and rows == 1, f"the #883 guard fired on a checkpoint update: {msg}"

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

    def test_the_LOSER_of_a_concurrent_transition_is_REFUSED_not_told_it_won(
        self, ledger
    ):
        """#883. The test that would have caught the self-transition defect.

        Two writers take the same edge on the same intent. The loser blocks on
        the row lock, and when the winner commits it re-evaluates against the
        winner's row — so a BEFORE trigger sees ``OLD.state = NEW.state`` and,
        before `061`, skipped every check and wrote a no-op. Both callers were
        told they had transitioned; one had. `transition()` raising or not is
        the caller's only success signal, and ``rowcount`` does not separate
        them either: the loser got 1, identical to the winner.

        Born red against the pre-`061` trigger, asserting the defect verbatim
        (`the LOSER ... was told it succeeded (rowcount=1)`). It is here so the
        next person to touch these triggers cannot reintroduce it quietly.

        READ COMMITTED is asserted rather than assumed. At REPEATABLE READ and
        above the loser already gets a serialization error, so a suite that
        silently ran at a higher level would pass without reaching the guard
        at all — the vacuity this file exists to refuse.
        """
        import threading

        intent = _new_intent(ledger, "scheduled")
        winner = psycopg2.connect(ledger["dsn"])
        loser = psycopg2.connect(ledger["dsn"])
        try:
            for conn in (winner, loser):
                conn.autocommit = False
                with conn.cursor() as cur:
                    cur.execute("SET app.actor_kind = 'system'")
            with loser.cursor() as cur:
                cur.execute("SHOW transaction_isolation")
                assert cur.fetchone()[0] == "read committed", (
                    "this gate is only meaningful at READ COMMITTED — at a "
                    "higher level the loser gets a serialization error and the "
                    "trigger is never reached"
                )

            wc, lc = winner.cursor(), loser.cursor()
            wc.execute(
                "UPDATE post_intents SET state='prompt_pending' WHERE id=%s", (intent,)
            )

            outcome = {}

            def race():
                try:
                    lc.execute(
                        "UPDATE post_intents SET state='prompt_pending' WHERE id=%s",
                        (intent,),
                    )
                    outcome["ok"], outcome["rows"] = True, lc.rowcount
                except Exception as exc:  # noqa: BLE001 — the message is the assertion
                    outcome["ok"] = False
                    outcome["msg"] = str(exc).strip()

            t = threading.Thread(target=race)
            t.start()
            t.join(timeout=2.0)
            assert t.is_alive(), (
                "the second writer did not block — it never contended for the "
                "row, so this test is not exercising the race it names"
            )

            winner.commit()
            t.join(timeout=30)
            assert not t.is_alive(), "the losing writer never returned"

            assert not outcome["ok"], (
                "the LOSER of a concurrent transition was told it succeeded "
                f"(rowcount={outcome.get('rows')}) — it did not transition "
                "anything, and rowcount cannot tell it apart from the winner "
                "(#883)"
            )
            assert "same-state" in outcome["msg"], (
                f"refused, but not by the #883 guard: {outcome['msg']}"
            )
            loser.rollback()

            with ledger["conn"].cursor() as cur:
                cur.execute("SELECT state FROM post_intents WHERE id=%s", (intent,))
                assert cur.fetchone()[0] == "prompt_pending"
                cur.execute(
                    "SELECT count(*) FROM audit_events WHERE entity_id=%s", (intent,)
                )
                assert cur.fetchone()[0] == 1, (
                    "exactly one transition happened, so exactly one audit row"
                )
        finally:
            winner.close()
            loser.close()


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
    @pytest.mark.parametrize("reused", [False, True], ids=["fresh", "reused"])
    def test_it_raises_and_names_the_guc(self, ledger, reused):
        intent = _new_intent(ledger, "scheduled")
        ok, msg, _ = _attempt(
            ledger,
            "UPDATE post_intents SET state='prompt_pending' WHERE id=%s",
            (intent,),
            actor=None,
            reused=reused,
        )
        assert not ok and _names_the_guc(msg), msg

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

    #: Writes that move only a dual-role table's machinery columns. The trigger
    #: exits before its audit INSERT for these, so its actor test is the only
    #: thing that refuses them: on a reused connection under `055`, nothing did
    #: (#1421).
    MACHINERY = {
        "ig_accounts": "UPDATE ig_accounts SET next_slot_at = now() WHERE workspace_id=%s",
        "oauth_credentials": (
            "UPDATE oauth_credentials SET next_refresh_at = now() WHERE workspace_id=%s"
        ),
    }

    def _statements(self):
        return [(table, self.STATEMENTS[table]) for table in GOVERNANCE_TABLES] + [
            (f"{table} machinery", sql) for table, sql in self.MACHINERY.items()
        ]

    def test_the_statements_ACTUALLY_TOUCH_A_ROW(self, ledger, seeded):
        """The control that makes the refusal test below mean anything. It is a
        SEPARATE test, deliberately: a vacuous refusal then reports as "matched
        0 rows" rather than hiding inside a passing assertion."""
        vacuous = []
        for label, sql in self._statements():
            ok, msg, rows = _attempt(ledger, sql, (seeded["ws"],))
            if not ok:
                vacuous.append((label, f"control failed: {msg.splitlines()[0]}"))
            elif rows < 1:
                vacuous.append((label, "matched 0 rows"))
        assert vacuous == [], (
            f"these refusal tests would pass vacuously — seed a row first: {vacuous}"
        )

    @pytest.mark.parametrize("reused", [False, True], ids=["fresh", "reused"])
    def test_without_an_actor_they_raise_naming_the_guc(self, ledger, seeded, reused):
        bad = []
        for label, sql in self._statements():
            ok, msg, _ = _attempt(
                ledger, sql, (seeded["ws"],), actor=None, reused=reused
            )
            if ok:
                bad.append((label, "SUCCEEDED with no actor"))
            elif not _names_the_guc(msg):
                bad.append((label, f"wrong reason: {msg.splitlines()[0]}"))
        assert bad == [], f"governance tables not guarded by actor_kind: {bad}"


class TestEveryGucReadTreatsEmptyAsUnset:
    """#1421's class, not its two instances. A `SET LOCAL`'s value dies with
    its transaction but the setting stays defined, so a pooled connection reads
    an unset `app.*` setting as '', and a read that tests only for NULL is
    right on a fresh connection alone. Every `current_setting('app.…', true)`
    in the live schema, in a function body or a policy, must treat '' as unset:
    `NULLIF(…, '')` makes both NULL, `COALESCE(…, '')` makes both ''. `055`'s
    two audit triggers were the only reads that did not, until `085`.

    The check is syntactic. It also refuses the two folds that undo themselves
    (`COALESCE(…, '') IS NULL`, `NULLIF(…, '') = ''`), and any spelling of the
    read it does not recognise, so a new form fails loudly rather than going
    uncounted. A read built in dynamic SQL is invisible to it."""

    ANY_READ = re.compile(r"current_setting\s*\(\s*'app\.", re.IGNORECASE)
    STRICT = re.compile(
        r"current_setting\s*\(\s*'app\.[a-z0-9_]+'(?:::text)?\s*\)", re.IGNORECASE
    )
    READ = re.compile(
        r"current_setting\s*\(\s*'app\.[a-z0-9_]+'(?:::text)?\s*,\s*"
        r"(?:missing_ok\s*=>\s*)?(?:true|'t(?:rue)?'|'on')(?:::boolean)?\s*\)",
        re.IGNORECASE,
    )
    FOLD_BEFORE = re.compile(r"(NULLIF|COALESCE)\s*\(\s*$", re.IGNORECASE)
    FOLD_AFTER = re.compile(r"\s*,\s*''(?:::text)?\s*\)")
    UNDONE = {
        "COALESCE": re.compile(r"\s*(?:::\w+\s*)?IS\s+(?:NOT\s+)?NULL", re.IGNORECASE),
        "NULLIF": re.compile(r"\s*(?:::\w+\s*)?(?:=|<>|!=)\s*''", re.IGNORECASE),
    }

    def test_every_read_treats_the_empty_string_as_unset(self, ledger):
        with ledger["conn"].cursor() as cur:
            cur.execute(
                "SELECT 'function ' || p.proname, p.prosrc FROM pg_proc p"
                " JOIN pg_namespace n ON n.oid = p.pronamespace"
                " WHERE n.nspname = 'public'"
            )
            sources = cur.fetchall()
            cur.execute(
                "SELECT 'policy ' || tablename || '.' || policyname,"
                "       concat_ws(' ', qual, with_check)"
                "  FROM pg_policies WHERE schemaname = 'public'"
            )
            sources += cur.fetchall()
        reads, bad = 0, []
        for where, src in sources:
            for m in self.ANY_READ.finditer(src):
                at = m.start()
                snippet = f"{where}: …{src[max(0, at - 24) : at + 72]}…"
                if self.STRICT.match(src, at):
                    continue  # raises when unset; these run after a guard
                read = self.READ.match(src, at)
                if read is None:
                    bad.append(f"unrecognised read: {snippet}")
                    continue
                reads += 1
                fold = self.FOLD_BEFORE.search(src[:at])
                after = self.FOLD_AFTER.match(src, read.end())
                if not (fold and after):
                    bad.append(f"bare: {snippet}")
                elif self.UNDONE[fold.group(1).upper()].match(src, after.end()):
                    bad.append(f"fold undone: {snippet}")
        # The positive control: the pattern still finds the policies' tenant
        # reads, so an empty `bad` is not a regex that stopped matching.
        assert reads >= 40, f"only {reads} reads found"
        assert bad == [], bad


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


#: The approval edge the person rule is about, as raw SQL.
APPROVE = "UPDATE post_intents SET state = 'approved' WHERE id = %s"


class TestAPlannedRowIsApprovedOnlyByAPerson:
    """088: `awaiting_approval -> approved` on a PLANNED row needs a person,
    `app.actor_kind = 'user'` with an `app.actor_user_id`, whatever issues the
    UPDATE. Each refusal has its rowcount-checked positive control: the same
    statement as a person lands, and on a cadence row the rule is silent."""

    @pytest.mark.parametrize(
        "actor, with_user",
        [("system", False), ("system", True), ("operator", True), ("user", False)],
    )
    def test_anything_but_a_person_is_refused(self, ledger, actor, with_user):
        intent = _new_intent(ledger, "awaiting_approval", origin="planned")
        user = ledger["chain"]["user"] if with_user else None
        ok, msg, _ = _attempt(ledger, APPROVE, (intent,), actor=actor, user_id=user)
        assert not ok and "only a person approves it" in msg, msg

    def test_a_person_approves_it(self, ledger):
        intent = _new_intent(ledger, "awaiting_approval", origin="planned")
        ok, msg, rows = _attempt(
            ledger, APPROVE, (intent,), actor="user", user_id=ledger["chain"]["user"]
        )
        assert ok and rows == 1, msg

    def test_a_cadence_row_is_untouched_by_the_rule(self, ledger):
        intent = _new_intent(ledger, "awaiting_approval")
        ok, msg, rows = _attempt(ledger, APPROVE, (intent,), actor="system")
        assert ok and rows == 1, msg

    def test_the_pipelines_step_back_is_untouched_on_a_planned_row(self, ledger):
        """`publishing -> approved` is the float's step back (076), issued by
        the worker as `system`: the rule is about approval, not about every
        edge that lands in `approved`."""
        intent = _new_intent(ledger, "publishing", origin="planned")
        ok, msg, rows = _attempt(ledger, APPROVE, (intent,), actor="system")
        assert ok and rows == 1, msg


class TestOriginIsFixedAtBirthAndAPlannedRowIsManual:
    """The person rule keys on `origin`, so `origin` must not move: a rule one
    UPDATE could step around would not hold. And a planned row is `manual`
    whatever the workspace's stored approval settings say (088's CHECK)."""

    @pytest.mark.parametrize(
        "born, becomes", [("planned", "cadence"), ("cadence", "planned")]
    )
    def test_origin_cannot_be_rewritten(self, ledger, born, becomes):
        intent = _new_intent(ledger, "awaiting_approval", origin=born)
        ok, msg, _ = _attempt(
            ledger,
            "UPDATE post_intents SET origin = %s WHERE id = %s",
            (becomes, intent),
        )
        assert not ok and "fixed at birth" in msg, msg

    def test_a_rewrite_that_would_admit_a_system_approval_is_refused(self, ledger):
        intent = _new_intent(ledger, "awaiting_approval", origin="planned")
        ok, msg, _ = _attempt(
            ledger,
            "UPDATE post_intents SET origin = 'cadence', state = 'approved' WHERE id = %s",
            (intent,),
        )
        assert not ok and "fixed at birth" in msg, msg

    def test_a_write_that_leaves_origin_alone_is_untouched(self, ledger):
        """Positive control for the two refusals above."""
        intent = _new_intent(ledger, "awaiting_approval", origin="planned")
        ok, msg, rows = _attempt(
            ledger,
            "UPDATE post_intents SET last_error = CAST(%s AS jsonb) WHERE id = %s",
            ('{"v": 1, "class": "probe", "message": "m"}', intent),
        )
        assert ok and rows == 1, msg

    @pytest.mark.parametrize("mode, lands", [("auto", False), ("manual", True)])
    def test_a_planned_row_is_born_manual(self, ledger, mode, lands):
        c = ledger["chain"]
        ok, msg, rows = _attempt(
            ledger,
            "INSERT INTO post_intents (workspace_id, ig_account_id, media_item_id,"
            " provider_account_ref, approval_mode, schedule_slot_at, origin)"
            " VALUES (%s, %s, %s, %s, %s, now(), 'planned')",
            (c["ws"], c["iga"], _new_media(ledger), f"born-{mode}", mode),
        )
        if lands:
            assert ok and rows == 1, msg
        else:
            assert not ok and "ck_intent_planned_manual" in msg, msg


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

    def test_the_self_transition_guard_is_what_refuses_a_same_state_write(self, ledger):
        """#883's trigger, held to the same standard as the other four."""
        intent = _new_intent(ledger, "scheduled")
        ok, msg, _ = _attempt(
            ledger, "UPDATE post_intents SET state='scheduled' WHERE id=%s", (intent,)
        )
        assert not ok and "same-state" in msg, msg

        self._drop(ledger, "tg_intent_no_self_transition", "post_intents")
        intent2 = _new_intent(ledger, "scheduled")
        ok2, msg2, rows2 = _attempt(
            ledger, "UPDATE post_intents SET state='scheduled' WHERE id=%s", (intent2,)
        )
        assert ok2 and rows2 == 1, (
            f"the same-state write was still refused after dropping "
            f"tg_intent_no_self_transition, so the refusal above was NOT it: {msg2}"
        )

    def test_the_audit_trigger_is_what_refuses_an_actor_less_state_change(self, ledger):
        intent = _new_intent(ledger, "scheduled")
        ok, msg, _ = _attempt(
            ledger,
            "UPDATE post_intents SET state='prompt_pending' WHERE id=%s",
            (intent,),
            actor=None,
        )
        assert not ok and _names_the_guc(msg), msg

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
        assert not ok and _names_the_guc(msg), msg

        self._drop(ledger, "tg_audit_workspaces", "workspaces")
        ok2, msg2, rows2 = _attempt(ledger, sql, (ws,), actor=None)
        assert ok2 and rows2 == 1, f"still refused without tg_audit_workspaces: {msg2}"

    def test_the_person_trigger_is_what_refuses_a_system_approval_of_a_planned_row(
        self, ledger
    ):
        intent = _new_intent(ledger, "awaiting_approval", origin="planned")
        ok, msg, _ = _attempt(ledger, APPROVE, (intent,), actor="system")
        assert not ok and "only a person approves it" in msg, msg

        self._drop(ledger, "tg_intent_planned_person", "post_intents")
        ok2, msg2, rows2 = _attempt(ledger, APPROVE, (intent,), actor="system")
        assert ok2 and rows2 == 1, (
            f"still refused without tg_intent_planned_person: {msg2}"
        )


class TestTheServicePathAgreesWithTheTrigger:
    """The gate's "as well as the service" half.

    Deliberately the SMALLER half. The service issues the UPDATE and lets the
    trigger decide — it holds no copy of the edge set — so what is worth
    asserting is that the service surfaces the database's refusal rather than
    substituting a judgement of its own. If these ever diverge from the raw-SQL
    results above, the service has grown an authority it should not have.
    """

    def _async_dsn(self, ledger) -> str:
        return async_url(ledger["dsn"])

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
    @pytest.mark.parametrize("reused", [False, True], ids=["fresh", "reused"])
    async def test_an_ACTOR_LESS_transition_is_refused_by_the_AUDIT_TRIGGER(
        self, ledger, reused
    ):
        """The one refusal that is not a `check_violation` (#1402).

        `trg_intent_audit` refuses an anonymous state change with a bare
        `RAISE EXCEPTION`, which asyncpg surfaces as `RaiseError` — so this is
        the test that proves `transition()` catches both driver shapes. The edge
        is legal, so the guard passes and the missing actor is the only
        objection left. On a fresh connection the unset setting reads NULL; on
        a pooled connection that has carried an actor it reads '', which the
        trigger reads as unset too since `085` (#1421).
        """
        from sqlalchemy import text
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
            if reused:  # the one connection carries an actor once
                primed = unit_of_work(
                    engine, ledger["chain"]["ws"], actor_kind="system"
                )
                async with primed.begin():
                    pass
            anonymous = unit_of_work(engine, ledger["chain"]["ws"])
            with pytest.raises(IntentTransitionRefused, match="app.actor_kind"):
                async with anonymous.begin() as session:  # pool_size=1: the same one
                    unset = await session.execute(
                        text("SELECT current_setting('app.actor_kind', true)")
                    )
                    assert unset.scalar_one() == ("" if reused else None)  # the premise
                    await transition(session, intent, "prompt_pending")
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_a_transition_that_matches_no_row_raises_IntentNotVisible(
        self, ledger
    ):
        """#1423, the plain case: an id that does not exist. The UPDATE touches
        no row, and that is not a success."""
        from sqlalchemy.ext.asyncio import create_async_engine

        from src.services.target.intent_ledger import IntentNotVisible, transition
        from src.services.target.unit_of_work import unit_of_work

        engine = create_async_engine(
            self._async_dsn(ledger), pool_size=1, max_overflow=0
        )
        try:
            uow = unit_of_work(engine, ledger["chain"]["ws"], actor_kind="system")
            with pytest.raises(IntentNotVisible, match="matched no row"):
                async with uow.begin() as session:
                    await transition(session, str(uuid.uuid4()), "prompt_pending")
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_another_tenants_intent_is_NOT_VISIBLE_under_row_level_security(
        self, ledger
    ):
        """#1423's real case. Both services run as their own logins without
        BYPASSRLS (#1379), so a session claimed for another workspace cannot see
        this one's intent, and the UPDATE matches no row. Run as `svc_worker`,
        because the gate's own login is the owner, which bypasses the policies.
        The same login, claimed for the intent's own workspace, moves it: the
        pair shows that the policy hides the row, not that the row is missing.
        """
        from src.services.target.intent_ledger import IntentNotVisible, transition
        from src.services.target.unit_of_work import unit_of_work

        intent = _new_intent(ledger, "scheduled")  # committed: the fixture autocommits
        async with ingress_engine(as_user(ledger["dsn"], "svc_worker")) as engine:
            stranger = unit_of_work(engine, str(uuid.uuid4()), actor_kind="system")
            with pytest.raises(IntentNotVisible, match="matched no row"):
                async with stranger.begin() as session:
                    await transition(session, intent, "prompt_pending")
            owner = unit_of_work(engine, ledger["chain"]["ws"], actor_kind="system")
            async with owner.begin() as session:
                await transition(session, intent, "prompt_pending")

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
