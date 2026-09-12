"""L.4 — the channel outbox and its sender (#861, `04` §L.4).

`02` §6, normative: **the outbox row IS the delivery record and the only
authority on "did this send" — the sender job carries execution state only.**
Everything here follows from that sentence.

## The mechanism question, which is this increment rather than a preamble

`channel_outbox` carries no unique index and no lease token — flagged on #886
from DDL inspection, and the #887 ruling was explicit that the argument making
`jobs` safe does **not** transfer: `jobs` has `uq_jobs_serialized_lease` on
`ready → leased` and a token CAS on `leased → terminal`, and this table has
neither. An outbox exists to make a send happen exactly once, so "two writers
both told they won" (#883) means the machine believing it sent something it did
not, or sending twice while believing it sent once.

**It does not need its own guard, and the reason is a mechanism rather than a
category.** Exactly-once here rests on two database-enforced facts, neither of
them on `channel_outbox`:

1. **One sender per chat, writer-independently.** The sender is the
   `deliver_outbox` job kind, whose serialization key is `tg:<binding_id>`
   (`02` §5 registry). `uq_jobs_serialized_lease` is a partial unique index, so
   two live leases on one binding are impossible for **every** writer including
   a `psql` session — the same guard that made `jobs` safe, reused rather than
   re-derived. Two senders never race for one chat's rows.
2. **Every write out of `sending` is a CAS on the state it is leaving**, so
   it is re-evaluation-correct at READ COMMITTED: `sending → *` requires
   `state = 'sending'`. A loser gets zero rows — a predicate miss on a column
   the winner changed, which is the discrimination #883's `WHERE id = :id`
   could not make. (Entering `sending` is single-winner by ``FOR UPDATE SKIP
   LOCKED`` instead; :func:`claim_next` says why its CAS clause is redundant
   rather than claiming credit for it.)

Fact 1 removes concurrent senders; fact 2 fences the one case fact 1 cannot see
— a partitioned predecessor whose lease has expired but whose process is still
running. Its `sending → sent` finds the row no longer `sending` and refuses.

**And the production sender commits per checkpoint (2026-09-09, the tap
plan's phase 1).** `OutboxPoller.tick` runs pace-and-claim in one committed
transaction, speaks to the provider with none open, and settles in another —
`02:1254`'s rule that a transaction never spans a provider call, and what keeps
the fleet-wide `tg_global` rate row and the claimed row unlocked across an
upload (#1260). The guarantee against a stale or dead sender is therefore NOT
a co-located commit: it is `recover_stranded` (a `sending` row seen by the
lease holder belongs to a predecessor → `ambiguous`, per-kind policy) plus the
`_leave_sending` CAS. `deliver` remains the single-transaction composition of
the same two halves for callers that hold their own transaction — the gate
pins its never-commits contract by running a stale sender against a live one.

**No migration.** Adding a send token to the outbox would be a second lease
token for one delivery, and `02` §6 gives the sender job execution state and
this table the delivery record. Two tokens for one thing is the two-truths seam
the design kills elsewhere.

## Stuck `sending` rows are recovered by whoever holds the binding's lease

There is no reaper leg for the outbox — `fn_reaper_sweep` re-readies job leases
and the retention door deletes terminal rows; neither touches a stranded
`sending`. It does not need one. Holding the binding's lease is itself the
proof that the previous holder does not, because fact 1 admits only one, so a
row left `sending` when you hold the lease belongs to a dead predecessor and is
yours to resolve. That is why :func:`deliver` recovers before it claims, and
why the stopped-sender injection strands nothing.

Recovery never blindly re-sends. `02` §6's R8 rule: Telegram has no read-back
for a lost ``sendMessage`` response (no "list my sent messages" API), so an
unresolved send becomes `ambiguous` and the per-kind policy decides.

## The per-kind ambiguity policy (`02` §6, verbatim in behaviour)

* **`notification` / `ack`** — retry once after backoff, then `failed`. A
  duplicate notification is the accepted cost, **bounded at one**.
* **`approval_prompt` / `invitation`** — resend. Two live cards for one intent
  are tolerable: both resolve to the same intent and terminal-state-first reads
  (R6) make whichever is tapped later render the terminal state. On any intent
  state change, **supersede-all** — `prompt_supersede` rows target every known
  `external_message_ref`; a card whose ref was lost ages out under R6.
* **Edits always go supersede-then-send** — never edit-in-place on an ambiguous
  ref. :func:`supersede_all` is the only edit path there is.

## Pacing

Both `02` §6 scopes, in one transaction with the send that they admit:
`tg_chat` keyed on the binding and `tg_global` keyed on `''`. Over budget is a
**defer**, not a failure — the row stays `pending` and the next poll takes it,
which is `01` H5's slip-a-slot rather than pile-up.

Every `05` number arrives as a parameter — poll cadence 2 s, 20 msgs/min/group,
30/s global, retention 30 d / 90 d. A hardcoded number in this module would be
the same review-blocking defect as one in a door body.
"""

from __future__ import annotations

import json
from datetime import timedelta
import logging
from typing import Any, Optional

from sqlalchemy import text

from src.exceptions.base import StorydumpError
from src.services.target.rate_counters import increment, window_start

logger = logging.getLogger(__name__)

#: `02` §6 kinds whose ambiguity resolves by resend rather than retry-once.
#: Two live cards are tolerable; a duplicate notification is not. A card EDIT
#: (`prompt_supersede`) is idempotent — "not modified" is success — so a lost
#: answer costs one paced call to resend; and since the route no longer strips
#: a card (#1297), this row is the only path that removes its buttons, so it
#: may not die after the notification's single retry.
RESEND_KINDS = frozenset({"approval_prompt", "invitation", "prompt_supersede"})

#: Kinds whose ambiguity is retried EXACTLY once, then failed.
RETRY_ONCE_KINDS = frozenset({"notification", "ack"})

#: The bound the policy states: at most one extra send of a notification.
#: A row that has been ambiguous once has spent it.
MAX_NOTIFICATION_RESENDS = 1


class ChannelPaced(StorydumpError):
    """The provider answered 429: not a lost response (the message did not
    go) and not a fault of the row. Phase 3a step 2: the row returns to
    `pending`, and the budget the provider named is written as a DURABLE hold
    on the pacing rows so every replica's poller defers until it passes —
    never an in-task sleep (#1035). `scope` is `global` or `chat`."""

    def __init__(self, message: str, *, retry_after_s: float, scope: str = "global"):
        super().__init__(message)
        self.retry_after_s = float(retry_after_s)
        self.scope = scope


#: A provider's `retry_after` is honoured up to this long on the GLOBAL row
#: (a longer one is re-learned on the next 429); the chat row takes it whole.
MAX_GLOBAL_HOLD_SECONDS = 60
MAX_CHAT_HOLD_SECONDS = 3600
#: A 429 on a chat-addressed call is almost always the chat's limit (Telegram:
#: ~20/min per group, against ~30/s bot-wide), so the fleet's global row takes
#: only a short brake for it — in case it was the bot-wide limit after all —
#: while the chat's row takes the whole `retry_after`.
CHAT_SCOPED_GLOBAL_BRAKE_SECONDS = 2
#: An `approval_prompt` (or invitation) is resent after an ambiguous send at
#: most this many times; past it the row fails and the intent's reaper
#: handles the rest (phase 3a step 3 — no cap was a resend forever).
MAX_PROMPT_RESENDS = 3


class ChannelRefused(StorydumpError):
    """The provider answered, and said no for good — a 4xx that is neither a
    gone destination, a dead credential nor a flood limit. DEFINITIVE, like
    :class:`DestinationGone`: the message as shaped never landed, so the row
    fails outright instead of entering the ambiguity policy (a refused edit
    retried to the resend cap was four paced calls for nothing; #1297)."""


#: A live sender's lost answer (`settle` → `ambiguous`) waits this long
#: before the binding's next claim applies the per-kind policy
#: (`resolve_ambiguous`): long enough for the provider's answer, had it been
#: merely slow, to have been a timeout rather than a partition. `02` §6's
#: "retry once after backoff" — this is the backoff.
AMBIGUOUS_RESOLVE_AFTER_SECONDS = 30


class DestinationGone(StorydumpError):
    """The transport's DEFINITIVE answer that the destination no longer takes
    messages — the bot kicked or blocked, the chat deleted or migrated. Not a
    lost response (that is the ambiguous case), so the row fails outright and
    the caller retires or re-points the binding. *migrate_to* carries the
    successor chat id when the provider named one."""

    def __init__(self, detail: str = "", *, migrate_to: Optional[str] = None):
        self.migrate_to = migrate_to
        super().__init__(f"destination gone{': ' + detail if detail else ''}")


class OutboxFenced(StorydumpError):
    """An outbox write found the row no longer in the state it was leaving.

    Raised where a CAS matched zero rows: the row was claimed, resolved or
    superseded by someone else first. Carries the same meaning as
    :class:`~src.services.target.jobs.JobFenced` one table over — you are not
    the writer you thought you were — and the caller aborts.
    """


class OutboxPaced(StorydumpError):
    """The send was deferred by a pacing budget, not failed.

    The row is left `pending` and the next poll takes it. Distinct from
    :class:`OutboxFenced` because the dispositions are opposite: a fenced
    writer must abort, a paced one must simply come back.
    """


#: An executor returns this when it did its work and reached NO delivery
#: surface — the workspace has no active push binding, so nobody could have
#: been told. The job finalizes `review_required` rather than `succeeded`.
#:
#: **This exists because "nothing to deliver to" was recorded as success.**
#: `credential_lifecycle` logs a warning and returns `"no-surface"`; the sweep
#: in `media_sync` iterates an empty binding list with no warning at all. In
#: both, the customer is told nothing and the ledger records a clean run, so
#: the weekly reauth cadence re-prompts into the void forever and nothing can
#: count it. A run nobody could have received must not look like a delivered
#: one — that is the same failure as an instrument over an empty population
#: returning its reassuring value.
#:
#: `review_required` and not `failed`: `failed` reschedules on the R8 backoff,
#: and retrying cannot conjure a binding — it would trade a silent success for
#: a poison loop. `review_required` is terminal, is already in `ck_jobs_state`
#: (no migration), is covered by `ix_jobs_retire`, and is the `02` §5 state
#: meaning a human has to look. It is also countable, which "log and succeed"
#: never was: `SELECT count(*) FROM jobs WHERE state = 'review_required'`.
UNDELIVERABLE = "no-delivery-surface"


async def fanout_notification(
    session, *, workspace_id, bindings, text: str, intent_id=None
) -> int:
    """Write one `notification` row per binding. Returns rows written.

    `06` §5 routes every customer-visible failure to "the workspace's
    bindings", and the loop that does it had been copied to five call sites —
    each re-deciding the `kind`, the `{v: 1, text}` envelope and the str()
    coercions. One home, so a change to the envelope is one edit rather than a
    hunt.

    It deliberately does NOT resolve the bindings itself. Each producer must
    decide what an EMPTY set means before it gets here — `outbox.UNDELIVERABLE`
    versus an ordinary quiet beat — and a helper that both fetched and iterated
    would make "nobody to tell" a zero-length loop again, which is the exact
    silence this module's `UNDELIVERABLE` exists to break.

    Remaining callers to move: `media_sync` (two sites) and
    `credential_lifecycle` (one). They belong to #1090 D1/D2 rather than here.
    """
    for binding_id in bindings:
        await enqueue(
            session,
            workspace_id=str(workspace_id),
            binding_id=binding_id,
            kind="notification",
            intent_id=None if intent_id is None else str(intent_id),
            payload={"v": 1, "text": text},
        )
    return len(bindings)


async def enqueue(
    session,
    *,
    workspace_id: str,
    binding_id: str,
    kind: str,
    payload: dict,
    intent_id: Optional[str] = None,
) -> str:
    """Write one `pending` outbox row. Runs in the caller's transaction.

    Deliberately no commit: an outbox row is created by the transaction whose
    effect it announces (`02` §4 — "outbox rows created for active push
    bindings, same tx"), so a rolled-back intent flip must take its
    notification with it.
    """
    row = (
        await session.execute(
            text(
                "INSERT INTO channel_outbox"
                " (workspace_id, binding_id, kind, intent_id, payload)"
                " VALUES (:ws, :b, :k, :i, CAST(:p AS jsonb)) RETURNING id"
            ),
            {
                "ws": workspace_id,
                "b": binding_id,
                "k": kind,
                "i": intent_id,
                "p": json.dumps(payload),
            },
        )
    ).first()
    return str(row[0])


async def claim_next(session, *, binding_id: str) -> Optional[dict]:
    """Take the oldest `pending` row for *binding_id*, or return None.

    **What makes this single-winner is ``FOR UPDATE SKIP LOCKED``, not the
    outer CAS**, and the distinction is measured rather than assumed: removing
    the outer ``AND state = 'pending'`` leaves the whole gate green. It cannot
    fire. A row another transaction already committed to `sending` fails the
    subquery's own filter at READ COMMITTED, and one being changed
    uncommittedly is locked, so SKIP LOCKED passes it over. There is no path
    where the outer predicate is the thing that refuses.

    The clause stays anyway, labelled for what it is: the statement is then
    correct on its own terms rather than on SKIP LOCKED's, so a later edit that
    changes the locking for fairness cannot silently reintroduce the race. That
    is defense in depth, and calling it the guard would be dressing a
    redundancy as a mechanism — the outbox's real fence is on the way OUT of
    `sending` (:func:`_leave_sending`), which the gate does prove load-bearing.

    Ordering is `created_at` per binding, which is exactly `ix_outbox_due`'s
    shape, so the poll rides the index the schema already ships. Plan
    verification belongs at S.1 scale — on a gate-sized table Postgres will
    seq-scan whatever the index says, so an EXPLAIN assertion here would prove
    nothing.
    """
    row = (
        await session.execute(
            text(
                "UPDATE channel_outbox SET state = 'sending', attempts = attempts + 1"
                " WHERE id = (SELECT id FROM channel_outbox"
                "             WHERE binding_id = :b AND state = 'pending'"
                "             ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED)"
                "   AND state = 'pending'"
                " RETURNING id, kind, payload, attempts, intent_id, workspace_id,"
                "           binding_id"
            ),
            {"b": binding_id},
        )
    ).first()
    if row is None:
        return None
    return {
        "id": str(row[0]),
        "kind": row[1],
        "payload": row[2],
        "attempts": row[3],
        "intent_id": None if row[4] is None else str(row[4]),
        # The row's own tenant, for the transport to hold a payload against
        # (a media block naming another workspace is refused — #1259).
        "workspace_id": str(row[5]),
        # Its binding, for the sender's own post-send edit (phase 1 step 8).
        "binding_id": str(row[6]),
    }


async def _leave_sending(session, outbox_id: str, to_state: str, **extra) -> None:
    """Every `sending → *` write, through one CAS.

    One spelling so the fence cannot be present on some edges and absent on
    others — the divergence that made the intent ledger's CAS and its test copy
    two independent statements (#890).
    """
    sets = ["state = :s"]
    params = {"s": to_state, "i": outbox_id}
    if "external_message_ref" in extra:
        sets.append("external_message_ref = :ref")
        params["ref"] = extra["external_message_ref"]
    if extra.get("sent_as"):
        # How the card went out (media or text), so a later edit knows which
        # editMessage* to call — on the JSONB payload, no migration.
        sets.append(
            "payload = payload || jsonb_build_object('sent_as', CAST(:sent_as AS text))"
        )
        params["sent_as"] = str(extra["sent_as"])
    if extra.get("restore_attempt"):
        # A provider's limit (429) is not the row's failure: the attempt the
        # claim consumed is given back, so R8's ambiguity budget (one retry for
        # a notification, MAX_PROMPT_RESENDS for a card) is spent only by sends
        # that were actually lost.
        sets.append("attempts = GREATEST(attempts - 1, 0)")
    result = await session.execute(
        text(
            f"UPDATE channel_outbox SET {', '.join(sets)}"
            " WHERE id = :i AND state = 'sending'"
        ),
        params,
    )
    if result.rowcount == 0:
        raise OutboxFenced(
            f"outbox {outbox_id}: the row is no longer 'sending' — another"
            " sender resolved it first; aborting rather than overwriting"
        )


async def mark_sent(
    session,
    *,
    outbox_id: str,
    external_message_ref: str,
    sent_as: Optional[str] = None,
) -> None:
    """`sending → sent`, recording the ref the channel returned and how the
    card went out."""
    await _leave_sending(
        session,
        outbox_id,
        "sent",
        external_message_ref=str(external_message_ref),
        sent_as=sent_as,
    )


async def mark_ambiguous(session, *, outbox_id: str) -> None:
    """`sending → ambiguous`: the send left, the response did not come back.

    R8's no-blind-retry rule lands here. Resolution is the per-kind policy in
    :func:`resolve_ambiguous`, never an immediate re-send at the call site:
    the binding's sender applies it on a later tick, once the row has waited
    `AMBIGUOUS_RESOLVE_AFTER_SECONDS` (`resolve_aged_ambiguous`).
    """
    await _leave_sending(session, outbox_id, "ambiguous")


async def resolve_ambiguous(session, *, outbox_id: str) -> str:
    """Apply `02` §6's per-kind policy to one `ambiguous` row.

    Returns the state it moved to. Reads the kind from the row rather than
    taking it as an argument: the policy is a property of the row, and a caller
    that could pass the wrong kind is a second authority on which rule applies.
    """
    row = (
        await session.execute(
            text(
                "SELECT kind, attempts FROM channel_outbox"
                " WHERE id = :i AND state = 'ambiguous'"
            ),
            {"i": outbox_id},
        )
    ).first()
    if row is None:
        raise OutboxFenced(
            f"outbox {outbox_id}: not 'ambiguous' — already resolved elsewhere"
        )
    kind, attempts = row[0], row[1]

    if kind in RESEND_KINDS:
        # Resend — a duplicate card is tolerated, and a card edit is
        # idempotent — but not forever: a row that keeps losing its answer
        # ends `failed` after MAX_PROMPT_RESENDS.
        to_state = "pending" if attempts <= MAX_PROMPT_RESENDS else "failed"
    elif attempts <= MAX_NOTIFICATION_RESENDS:
        to_state = "pending"  # the ONE retry the policy allows
    else:
        to_state = "failed"  # spent it; a second duplicate is not the cost

    result = await session.execute(
        text(
            "UPDATE channel_outbox SET state = :s WHERE id = :i AND state = 'ambiguous'"
        ),
        {"s": to_state, "i": outbox_id},
    )
    if result.rowcount == 0:
        raise OutboxFenced(f"outbox {outbox_id}: resolved by someone else first")
    return to_state


def _supersede_payload(ref: str, payload: Any, outcome_text: Optional[str]) -> dict:
    """What the sender needs to edit the card it names: the ref, the outcome
    line, the original header (a media card's caption or a text card's text —
    the edit keeps it and appends the outcome) and how the card went out."""
    if isinstance(payload, str):
        payload = json.loads(payload)
    payload = payload or {}
    body: dict = {"v": 1, "supersedes_ref": str(ref)}
    if outcome_text:
        body["outcome_text"] = outcome_text
    header = payload.get("caption") or payload.get("text")
    if header:
        body["header"] = header
    if payload.get("sent_as"):
        body["sent_as"] = payload["sent_as"]
    return body


async def supersede_all(
    session,
    *,
    workspace_id: str,
    binding_id: str,
    intent_id: str,
    outcome_text: Optional[str] = None,
) -> int:
    """Supersede every live card for *intent_id* and queue the supersede rows.

    `02` §6: on any intent state change, `prompt_supersede` rows target **every
    known** `external_message_ref`; a card whose ref was lost simply ages out.
    Returns the number of cards superseded.

    This is also the only edit path — "edits always go supersede-then-send,
    never edit-in-place on an ambiguous ref". With *outcome_text* the edit
    writes the card's header plus that line under it (phase 1 of the
    2026-09-09 tap plan, step 5); without it the keyboard is only stripped.

    A row in `sending` is superseded too: the sender commits its claim before
    the provider call (phase 1 step 8), so `sending` is a committed state a
    tap can meet; the sender finds its `mark_sent` fenced and edits the card
    it just sent itself (`OutboxPoller`).
    """
    live = (
        await session.execute(
            text(
                # The superseded row itself keeps the line it was superseded
                # with: a sender whose in-flight row this takes reads it back
                # to edit the card only it holds the ref of.
                "UPDATE channel_outbox SET state = 'superseded',"
                "   payload = CASE WHEN CAST(:o AS text) IS NULL THEN payload"
                "             ELSE payload || jsonb_build_object('outcome_text', CAST(:o AS text)) END"
                " WHERE workspace_id = :ws AND binding_id = :b AND intent_id = :i"
                "   AND kind IN ('approval_prompt', 'invitation')"
                "   AND state IN ('pending', 'sending', 'sent', 'ambiguous')"
                " RETURNING external_message_ref, payload"
            ),
            {"ws": workspace_id, "b": binding_id, "i": intent_id, "o": outcome_text},
        )
    ).fetchall()

    for ref, payload in live:
        if ref is None:
            continue  # the ref was lost (or never sent); the card ages out under R6
        await enqueue(
            session,
            workspace_id=workspace_id,
            binding_id=binding_id,
            kind="prompt_supersede",
            payload=_supersede_payload(str(ref), payload, outcome_text),
            intent_id=intent_id,
        )
    return len(live)


async def supersede_everywhere(
    session, *, workspace_id: str, intent_id: str, outcome_text: Optional[str]
) -> int:
    """`supersede_all` for EVERY active Telegram binding of the workspace, in
    one statement (#1286): the bindings, the supersede of every live card and
    the `prompt_supersede` rows are one round trip inside the tap's
    transaction instead of one read plus two writes per binding. The payload
    is `_supersede_payload`'s, built in SQL. Returns the cards superseded."""
    row = (
        await session.execute(
            text(
                "WITH b AS ("
                "  SELECT id FROM channel_bindings"
                "   WHERE workspace_id = :ws AND state = 'active'"
                "     AND channel LIKE 'telegram%'"
                "), sup AS ("
                "  UPDATE channel_outbox o SET state = 'superseded',"
                "     payload = CASE WHEN CAST(:o AS text) IS NULL THEN o.payload"
                "               ELSE o.payload || jsonb_build_object('outcome_text', CAST(:o AS text)) END"
                "   WHERE o.workspace_id = :ws AND o.intent_id = :i"
                "     AND o.binding_id IN (SELECT id FROM b)"
                "     AND o.kind IN ('approval_prompt', 'invitation')"
                "     AND o.state IN ('pending', 'sending', 'sent', 'ambiguous')"
                "   RETURNING o.binding_id, o.external_message_ref, o.payload"
                "), ins AS ("
                "  INSERT INTO channel_outbox (workspace_id, binding_id, kind, intent_id, payload)"
                "  SELECT :ws, s.binding_id, 'prompt_supersede', :i,"
                "         jsonb_strip_nulls(jsonb_build_object("
                "           'v', 1,"
                "           'supersedes_ref', s.external_message_ref,"
                "           'outcome_text', NULLIF(CAST(:o AS text), ''),"
                "           'header', COALESCE(NULLIF(s.payload->>'caption', ''),"
                "                              NULLIF(s.payload->>'text', '')),"
                "           'sent_as', NULLIF(s.payload->>'sent_as', '')))"
                "    FROM sup s WHERE s.external_message_ref IS NOT NULL"
                "  RETURNING id"
                ")"
                " SELECT (SELECT count(*) FROM sup) AS superseded,"
                "        (SELECT count(*) FROM ins) AS queued"
            ),
            {"ws": workspace_id, "i": intent_id, "o": outcome_text},
        )
    ).first()
    return int(row[0] or 0) if row is not None else 0


async def restate_cards(
    session,
    *,
    workspace_id: str,
    binding_id: str,
    intent_id: str,
    outcome_text: str,
) -> int:
    """Write a NEW outcome line onto every card of *intent_id* that still has
    a message to edit, and queue the edit — whether or not the card is already
    superseded.

    `supersede_all` is the tap's door: it takes a LIVE card's buttons away and
    says what happened. By the time the publish leg confirms a post, the tap
    has already done that (the card reads "✅ Approved by …"), so the card is
    `superseded` and `supersede_all` would find nothing (#1276 review). This
    door addresses the ref instead of the state: an `approval_prompt` row with
    an `external_message_ref` in any settled state gets the line and a
    `prompt_supersede` row per ref; the poller edits the message under the
    same fenced path (a second strip of an already-stripped keyboard is
    Telegram's "message is not modified", which the transport treats as done).
    Returns the number of refs queued.
    """
    rows = (
        await session.execute(
            text(
                "UPDATE channel_outbox"
                "   SET payload = payload || jsonb_build_object('outcome_text', CAST(:o AS text))"
                " WHERE workspace_id = :ws AND binding_id = :b AND intent_id = :i"
                "   AND kind = 'approval_prompt'"
                "   AND state IN ('sent', 'superseded', 'ambiguous')"
                "   AND external_message_ref IS NOT NULL"
                " RETURNING external_message_ref, payload"
            ),
            {"ws": workspace_id, "b": binding_id, "i": intent_id, "o": outcome_text},
        )
    ).fetchall()
    seen: set[str] = set()
    for ref, payload in rows:
        if str(ref) in seen:
            continue
        seen.add(str(ref))
        await enqueue(
            session,
            workspace_id=workspace_id,
            binding_id=binding_id,
            kind="prompt_supersede",
            payload=_supersede_payload(str(ref), payload, outcome_text),
            intent_id=intent_id,
        )
    return len(seen)


async def resolve_aged_ambiguous(session, *, binding_id: str) -> list:
    """Apply the per-kind policy to the binding's `ambiguous` rows that have
    waited the backoff. Returns the ids resolved, oldest first.

    `settle` marks a live sender's lost answer `ambiguous` and, until #1297,
    nothing ever came back for it — only a DEAD sender's stranded rows went
    through `resolve_ambiguous` (`pace_and_claim`), so the resend policy was
    unreachable for the common case and the row sat until retention deleted
    it. Bounded per tick; the binding's sender is single (`tg:<binding_id>`),
    so no other writer races the rows."""
    rows = (
        await session.execute(
            text(
                "SELECT id FROM channel_outbox"
                " WHERE binding_id = :b AND state = 'ambiguous'"
                "   AND updated_at <= now() - make_interval(secs => :age)"
                " ORDER BY created_at LIMIT 20"
            ),
            {"b": binding_id, "age": AMBIGUOUS_RESOLVE_AFTER_SECONDS},
        )
    ).fetchall()
    resolved = []
    for (outbox_id,) in rows:
        await resolve_ambiguous(session, outbox_id=str(outbox_id))
        resolved.append(str(outbox_id))
    return resolved


async def recover_stranded(session, *, binding_id: str) -> list:
    """Resolve rows a dead predecessor left `sending`. Returns their ids.

    Safe because of the lease, not because of a timeout: only one
    `deliver_outbox` lease per `tg:<binding_id>` can exist, so a `sending` row
    seen while holding that lease belongs to a holder that no longer has it.
    It moves to `ambiguous` — never straight back to `pending` — because
    whether the send left is exactly what nobody knows (R8), and
    :func:`resolve_ambiguous` owns what happens next.
    """
    rows = (
        await session.execute(
            text(
                "UPDATE channel_outbox SET state = 'ambiguous'"
                " WHERE binding_id = :b AND state = 'sending' RETURNING id"
            ),
            {"b": binding_id},
        )
    ).fetchall()
    return [str(r[0]) for r in rows]


async def deliver(
    session,
    *,
    binding_id: str,
    transport,
    now,
    chat_limit: int,
    chat_window_seconds: int,
    global_limit: int,
    global_window_seconds: int,
) -> Optional[dict]:
    """One delivery attempt for one binding. Runs in the CALLER's transaction.

    Never commits, by design: the row's move to `sent` and the sender job's
    `finalize_job` must commit together, so a stale owner's fenced finalization
    takes the outbox write with it (`02` §5). A `deliver` that committed on its
    own would put the send-state authority outside the fence that protects it.

    Order is recover → claim → pace → send. Recovery first is load-bearing: a
    predecessor's stranded row is older work than anything pending, and
    resolving it may put a row back in the queue this same call then takes.

    **The claim precedes the pacing (2026-09-09) so an EMPTY queue debits
    nothing** — an idle poller ticking every 2 s would otherwise spend a
    chat's whole window on silence. Correctness of the paced case comes from
    the transaction: a paced call raises before committing, so the caller's
    rollback un-claims the row it took (the gate proves a paced row stays
    pending). The order is a preference for the idle case, never the guard.

    *transport* is injected and channel-neutral: it takes the payload and
    returns an external ref, or raises to signal a lost response. The floor is
    about send-state, not about Telegram, and a real client here would make
    every gate test a network test.
    """
    row = await pace_and_claim(
        session,
        binding_id=binding_id,
        now=now,
        chat_limit=chat_limit,
        chat_window_seconds=chat_window_seconds,
        global_limit=global_limit,
        global_window_seconds=global_window_seconds,
    )
    if row is None:
        return None
    receipt, error = None, None
    try:
        receipt = await transport(row)
    except Exception as exc:  # noqa: BLE001 — classified in `settle`
        error = exc
    return await settle(
        session,
        row,
        receipt=receipt,
        error=error,
        now=now,
        chat_limit=chat_limit,
        chat_window_seconds=chat_window_seconds,
        global_limit=global_limit,
        global_window_seconds=global_window_seconds,
    )


async def pace_and_claim(
    session,
    *,
    binding_id: str,
    now,
    chat_limit: int,
    chat_window_seconds: int,
    global_limit: int,
    global_window_seconds: int,
) -> Optional[dict]:
    """Recover → pace → claim: everything BEFORE the provider is spoken to,
    in the caller's transaction. The poller commits this before it sends
    (phase 1 of the 2026-09-09 tap plan, step 8 / F8 (a)), so the `tg_global`
    rate row and the claimed row are never locked across the call (#1260)."""
    stranded = await recover_stranded(session, binding_id=binding_id)
    for outbox_id in stranded:
        await resolve_ambiguous(session, outbox_id=outbox_id)
    # A live sender's own lost answers, once they have waited the backoff.
    await resolve_aged_ambiguous(session, binding_id=binding_id)

    # Claim FIRST, then pace: nothing pending means nothing debited, so an
    # idle poller never spends the chat's (or the fleet's) window on empty
    # ticks — at the 2 s cadence that would burn a 20/min chat budget in 40 s
    # of silence and defer the card that then arrives (review of #1271). A
    # paced claim raises, and the caller's rollback un-claims the row — the
    # order was always a preference, never the guard (see `deliver`).
    row = await claim_next(session, binding_id=binding_id)
    if row is None:
        return None

    for scope, key, limit, window in (
        ("tg_chat", binding_id, chat_limit, chat_window_seconds),
        ("tg_global", "", global_limit, global_window_seconds),
    ):
        allowed = await increment(
            session,
            scope=scope,
            key=key,
            window_start=window_start(now, window),
            limit=limit,
        )
        if allowed is None:
            raise OutboxPaced(
                f"{scope} budget spent for this window — deferring; the row"
                " stays pending and the next poll takes it"
            )

    return row


async def write_pacing_hold(
    session,
    *,
    scope: str,
    key: str,
    now,
    seconds: float,
    limit: int,
    window_seconds: int,
) -> int:
    """Spend every *window_seconds* window of *scope*/*key* from *now* through
    `now + seconds` to *limit* in ONE statement, so `increment` on any replica
    defers (`OutboxPaced`) until the hold passes. Idempotent and monotonic: a
    window already spent stays spent (`GREATEST`). Returns the windows held."""
    first = window_start(now, window_seconds)
    last = window_start(now + timedelta(seconds=max(0.0, seconds)), window_seconds)
    result = await session.execute(
        text(
            "INSERT INTO rate_counters AS rc (scope, key, window_start, count)"
            " SELECT :scope, :key, gs, :limit"
            "   FROM generate_series(CAST(:first AS timestamptz),"
            "                        CAST(:last AS timestamptz),"
            "                        make_interval(secs => :window)) AS gs"
            " ON CONFLICT (scope, key, window_start)"
            "   DO UPDATE SET count = GREATEST(rc.count, EXCLUDED.count)"
        ),
        {
            "scope": scope,
            "key": key,
            "limit": limit,
            "first": first,
            "last": last,
            "window": window_seconds,
        },
    )
    return int(result.rowcount or 0)


async def settle(
    session,
    row: dict,
    *,
    receipt=None,
    error=None,
    now=None,
    chat_limit: Optional[int] = None,
    chat_window_seconds: Optional[int] = None,
    global_limit: Optional[int] = None,
    global_window_seconds: Optional[int] = None,
) -> dict:
    """Everything AFTER the provider answered, in the caller's transaction:
    the row's exit from `sending` by R8's taxonomy. *receipt* is the ref the
    channel returned (a `SendReceipt` also says how the card went out);
    *error* is what the transport raised instead. A `ChannelPaced` error
    (429) returns the row to `pending` and — when the caller passes its
    clock and budgets — writes the provider's `retry_after` as a durable hold
    on the pacing rows (phase 3a step 2): the chat's row for the whole
    `retry_after` when the 429 is chat-scoped (with a short brake on the
    global row), the global row for up to a minute otherwise."""
    if isinstance(error, ChannelPaced):
        # The holds FIRST: a 429 is a fact about the provider, not about this
        # row. If the row was superseded in flight, `_leave_sending` is fenced
        # and the poller commits what came before it — the holds must be in
        # that set, or the next tick calls straight back into the flood.
        held = {"global": 0, "chat": 0}
        if now is not None and global_limit is not None:
            # Chat row first, then global — the same order `pace_and_claim`
            # locks them in, so a stale sender still inside its hold cannot
            # deadlock with this settle.
            if error.scope == "chat" and chat_limit is not None:
                held["chat"] = await write_pacing_hold(
                    session,
                    scope="tg_chat",
                    key=str(row["binding_id"]),
                    now=now,
                    seconds=min(error.retry_after_s, MAX_CHAT_HOLD_SECONDS),
                    limit=chat_limit,
                    window_seconds=chat_window_seconds or 60,
                )
            global_seconds = (
                CHAT_SCOPED_GLOBAL_BRAKE_SECONDS
                if error.scope == "chat"
                else MAX_GLOBAL_HOLD_SECONDS
            )
            held["global"] = await write_pacing_hold(
                session,
                scope="tg_global",
                key="",
                now=now,
                seconds=min(error.retry_after_s, global_seconds),
                limit=global_limit,
                window_seconds=global_window_seconds or 1,
            )
        # The row goes back to `pending` with the attempt the claim consumed
        # restored: the provider's limit is not this row's failure.
        await _leave_sending(session, row["id"], "pending", restore_attempt=True)
        return {
            **row,
            "state": "paced",
            "external_message_ref": None,
            "retry_after_s": error.retry_after_s,
            "held_windows": held,
        }
    if isinstance(error, DestinationGone):
        # Definitive, not ambiguous: the provider said the chat will not take
        # it. The row fails; the caller retires the binding so the sweep stops
        # minting for a chat that is gone (a kicked bot is NOT a dead token).
        await _leave_sending(session, row["id"], "failed")
        return {
            **row,
            "state": "failed",
            "external_message_ref": None,
            "destination_gone": True,
            "migrate_to": error.migrate_to,
        }
    if isinstance(error, ChannelRefused):
        # Definitive, like a gone destination: the provider said this message
        # as shaped will never land. Nothing to resend; the row fails.
        await _leave_sending(session, row["id"], "failed")
        return {**row, "state": "failed", "external_message_ref": None}
    if error is not None:
        # A lost response is the ambiguous case.
        await mark_ambiguous(session, outbox_id=row["id"])
        return {**row, "state": "ambiguous", "external_message_ref": None}

    await mark_sent(
        session,
        outbox_id=row["id"],
        external_message_ref=str(receipt),
        sent_as=getattr(receipt, "sent_as", None),
    )
    return {**row, "state": "sent", "external_message_ref": str(receipt)}


async def _edit_sent_card(session, row: dict, receipt, *, force: bool) -> bool:
    """R6 after a send: re-read the intent an `approval_prompt` card is for;
    if it has moved past `awaiting_approval` since the claim (a tap, the web
    queue, the reaper), the card just sent must not keep live buttons — queue
    the supersede for the ref we just received. *force* is the fenced case:
    the row left `sending` under us. Only a row that was SUPERSEDED (a tap or
    a cancellation retired the card in flight) is owed the edit, with the line
    that supersede carried; a row a successor moved to `ambiguous`
    (`recover_stranded`, a partition) is the successor's to resend and is left
    alone. *receipt* is the `SendReceipt` (its `sent_as` decides caption vs
    text). Returns whether an edit was queued."""
    if row.get("kind") != "approval_prompt" or not row.get("intent_id"):
        return False
    from src.services.target import identity, intent_ledger, prompts  # noqa: PLC0415 — cycle

    ref = str(receipt)
    carried = None
    if force:
        current = (
            await session.execute(
                text(
                    "SELECT state, payload->>'outcome_text' FROM channel_outbox"
                    " WHERE id = :i AND workspace_id = :ws"
                ),
                {"i": row["id"], "ws": str(row["workspace_id"])},
            )
        ).first()
        if current is None or current[0] != "superseded":
            return False
        carried = current[1]
    found = await intent_ledger.settlement(
        session, workspace_id=str(row["workspace_id"]), intent_id=str(row["intent_id"])
    )
    state = found["state"]
    if state is None or (
        not force and state in ("scheduled", "prompt_pending", "awaiting_approval")
    ):
        return False
    tz_row = (
        await session.execute(
            text("SELECT tz FROM workspaces WHERE id = :ws"),
            {"ws": str(row["workspace_id"])},
        )
    ).first()
    tz = str(tz_row[0]) if tz_row and tz_row[0] else "UTC"
    outcome = carried
    if outcome is None and state not in (
        "scheduled",
        "prompt_pending",
        "awaiting_approval",
    ):
        by = (
            await identity.display_name_for(session, user_id=found["by_user_id"])
            if found.get("by_user_id")
            else None
        )
        from datetime import datetime, timezone  # noqa: PLC0415

        outcome = prompts.outcome_line(
            state,
            by=by,
            at=found["at"] or datetime.now(timezone.utc),
            tz=tz,
            published_via=found.get("published_via"),
        )
    payload = row.get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    sent_as = getattr(receipt, "sent_as", None) or payload.get("sent_as")
    await enqueue(
        session,
        workspace_id=str(row["workspace_id"]),
        binding_id=str(row["binding_id"]),
        kind="prompt_supersede",
        payload=_supersede_payload(
            ref, {**payload, **({"sent_as": sent_as} if sent_as else {})}, outcome
        ),
        intent_id=str(row["intent_id"]),
    )
    return True


class OutboxPoller:
    """The `run_at` polling loop that replaced the Redis wake-up (C3, TT:P0-07).

    `05` sets the cadence at **2 s** — "invisible in pg at ≪1 msg/s" — and it
    arrives as a parameter like every other `05` number. The Redis half is
    **struck, not deferred**: this class is the whole wake-up mechanism and no
    channel should be reintroduced beside it.

    One tick is one :func:`deliver` in its own transaction, so a paced or
    fenced tick rolls back alone and the next one is unaffected. Deliberately
    NOT in any pipeline's await chain, for the same reason L.2's heartbeat is
    not: a provider wait must never delay the loop that drains the queue.

    Observables rather than logs, so a supervisor can act on them: `ticks`,
    `sent`, `deferred` (pacing — expected and healthy), and
    `consecutive_failures`, which is what a liveness check reads. Escalation,
    liveness registration and pool shape are the composition root's, exactly
    as they are for `LeaseHeartbeat` — a poller that escalated on its own
    would be a second policy home.

    **What this class does not do is claim the sender lease.** The caller runs
    it while holding the `deliver_outbox` job for the binding; that lease is
    what makes one sender per chat true, and it is `fn_claim_job`'s to grant.
    """

    def __init__(
        self,
        session_factory,
        *,
        binding_id: str,
        transport,
        clock,
        interval_seconds: float,
        chat_limit: int,
        chat_window_seconds: int,
        global_limit: int,
        global_window_seconds: int,
    ):
        self._session_factory = session_factory
        self._binding_id = binding_id
        self._transport = transport
        self._clock = clock
        self._interval = interval_seconds
        self._budgets = {
            "chat_limit": chat_limit,
            "chat_window_seconds": chat_window_seconds,
            "global_limit": global_limit,
            "global_window_seconds": global_window_seconds,
        }
        self.ticks = 0
        self.sent = 0
        self.deferred = 0
        #: Set by a 429: how long the provider asked this sender to wait.
        self.held_for_s = 0.0
        self.consecutive_failures = 0
        self._task = None

    async def tick(self) -> Optional[dict]:
        """One poll, transaction-per-checkpoint (`02:1254`; phase 1 of the
        2026-09-09 tap plan, step 8): pace and claim, COMMIT, speak to the
        provider with no transaction open, then settle in a fresh one. The
        `tg_global` rate row is never held across the call (#1260), and the
        claimed row's `sending` is a committed state a tap can supersede.
        Never raises: a tick that failed is an observable, not an exception
        the loop has to survive twice."""
        self.ticks += 1
        try:
            async with self._session_factory() as session:
                row = await pace_and_claim(
                    session,
                    binding_id=self._binding_id,
                    now=self._clock(),
                    **self._budgets,
                )
                await session.commit()
        except OutboxPaced:
            self.deferred += 1
            self.consecutive_failures = 0  # pacing is health, not failure
            return None
        except Exception:  # noqa: BLE001 — the counter IS the report
            self.consecutive_failures += 1
            return None
        if row is None:
            self.consecutive_failures = 0
            return None

        receipt, error = None, None
        try:
            receipt = await self._transport(row)
        except Exception as exc:  # noqa: BLE001 — classified in `settle`
            error = exc

        try:
            async with self._session_factory() as session:
                try:
                    result = await settle(
                        session,
                        row,
                        receipt=receipt,
                        error=error,
                        now=self._clock(),
                        **self._budgets,
                    )
                except OutboxFenced:
                    # Superseded while in flight: a tap retired the card between
                    # the claim and the send, and its ref was unknown to the
                    # supersede. The card is out there with buttons — edit it
                    # ourselves (R6), with the intent's current outcome.
                    # Labelled by what we read: `superseded` when the card we
                    # sent is ours to edit, `fenced` when a successor moved the
                    # row to `ambiguous` and the resend is its business.
                    edited = receipt is not None and await _edit_sent_card(
                        session, row, receipt, force=True
                    )
                    result = {
                        **row,
                        "state": "superseded" if edited else "fenced",
                        "external_message_ref": None
                        if receipt is None
                        else str(receipt),
                    }
                else:
                    if result["state"] == "sent":
                        await _edit_sent_card(session, row, receipt, force=False)
                await session.commit()
        except Exception as exc:  # noqa: BLE001 — the counter IS the report
            self.consecutive_failures += 1
            logger.warning(
                "outbox tick: settling row %s after the send failed: %r",
                row.get("id"),
                exc,
            )
            return None
        self.consecutive_failures = 0
        if result["state"] == "sent":
            self.sent += 1
        elif result["state"] == "paced":
            # A 429: the hold is written; this binding's sender should yield
            # its lane and come back when the provider said to.
            self.deferred += 1
            self.held_for_s = float(result.get("retry_after_s") or 0.0)
        return result

    async def start(self) -> None:
        import asyncio

        async def _loop():
            while True:
                await self.tick()
                await asyncio.sleep(self._interval)

        self._task = asyncio.create_task(_loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except BaseException:  # noqa: BLE001 — cancellation is the happy path
            pass
        self._task = None
