"""The channel outbox and its sender.

The rule: **the outbox row IS the delivery record and the only
authority on "did this send" — the sender job carries execution state only.**
Everything here follows from that sentence.

## The mechanism question

`channel_outbox` carries no unique index and no lease token, so the
argument making `jobs` safe does **not** transfer: `jobs` has
`uq_jobs_serialized_lease` on `ready → leased` and a token CAS on
`leased → terminal`, and this table has neither. An outbox exists to make a
send happen exactly once, so "two writers both told they won" means the
machine believing it sent something it did not, or sending twice while
believing it sent once.

**It does not need its own guard, and the reason is a mechanism rather than a
category.** Exactly-once here rests on two database-enforced facts, neither of
them on `channel_outbox`:

1. **One sender per chat, writer-independently.** The sender is the
   `deliver_outbox` job kind, whose serialization key is `tg:<binding_id>`.
   `uq_jobs_serialized_lease` is a partial unique index, so
   two live leases on one binding are impossible for **every** writer including
   a `psql` session — the same guard that made `jobs` safe, reused rather than
   re-derived. Two senders never race for one chat's rows.
2. **Every write out of `sending` is a CAS on the state it is leaving**, so
   it is re-evaluation-correct at READ COMMITTED: `sending → *` requires
   `state = 'sending'`. A loser gets zero rows — a predicate miss on a column
   the winner changed, a discrimination that a bare `WHERE id = :id` cannot make.
   (Entering `sending` is single-winner by ``FOR UPDATE SKIP
   LOCKED`` instead; :func:`claim_next` says why its CAS clause is redundant
   rather than claiming credit for it.)

Fact 1 removes concurrent senders; fact 2 fences the one case fact 1 cannot see
— a partitioned predecessor whose lease has expired but whose process is still
running. Its `sending → sent` finds the row no longer `sending` and refuses.

**And the production sender commits per checkpoint.**
`OutboxPoller.tick` runs pace-and-claim in one committed
transaction, speaks to the provider with none open, and settles in another —
the rule that a transaction never spans a provider call, and what keeps
the fleet-wide `tg_global` rate row and the claimed row unlocked across an
upload. The guarantee against a stale or dead sender is therefore NOT
a co-located commit: it is `recover_stranded` (a `sending` row seen by the
lease holder belongs to a predecessor → `ambiguous`, per-kind policy) plus the
`_leave_sending` CAS. `deliver` remains the single-transaction composition of
the same two halves for callers that hold their own transaction — the gate
pins its never-commits contract by running a stale sender against a live one.

**No migration.** Adding a send token to the outbox would be a second lease
token for one delivery, and the design gives the sender job execution state and
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

Recovery never blindly re-sends: Telegram has no read-back
for a lost ``sendMessage`` response (no "list my sent messages" API), so an
unresolved send becomes `ambiguous` and the per-kind policy decides.

## The per-kind ambiguity policy

* **`notification` / `ack`** — retry once after backoff, then `failed`. A
  duplicate notification is the accepted cost, **bounded at one**.
* **`approval_prompt` / `invitation`** — resend, at most `MAX_PROMPT_RESENDS`
  times. **An approval card leaves with no keyboard**: its keyboard reaches
  the chat only as an edit of a message whose id the ledger holds, queued as
  the send is settled (:func:`_edit_sent_card`). So a copy a lost answer left
  in the chat carries no buttons, and a resend can never make a second live
  card; what a lost answer can leave is one inert copy, which no tap reaches
  (Telegram has no idempotent send and no read-back to find it).
* **`prompt_supersede`** — resend until Telegram confirms it: an edit is
  idempotent ("message is not modified" is success), so it is never failed
  for a lost answer, only for a definitive refusal, and an older edit of the
  same message is retired once a newer one exists. Each lost answer waits
  longer before the resend (`AMBIGUOUS_RESOLVE_AFTER_SECONDS` × attempts, the
  same schedule `fn_sender_sweep` mints the sender on).
* **Edits always go supersede-then-send** — never edit-in-place on an ambiguous
  ref. On any intent state change, **supersede-all**: `prompt_supersede` rows
  target every known `external_message_ref`. A tapped message the ledger does
  not know is adopted (:func:`adopt_card`), and a tapped card of the story
  whose newest edit did not land is edited again.

## Pacing

Both scopes, in one transaction with the send that they admit:
`tg_chat` keyed on the binding and `tg_global` keyed on `''`. Over budget is a
**defer**, not a failure — the row stays `pending` and the next poll takes it,
which is slip-a-slot rather than pile-up.

Every number arrives as a parameter. `WorkerConfig` defaults to a 2 s poll,
18 sends per minute per chat and 25 per second globally. These worker budgets
leave room below Telegram's limits of 20 messages per minute per group and
30 messages per second globally. Retention is passed to the retention worker;
this module does not own a retention duration.
"""

from __future__ import annotations

import json
from datetime import timedelta
import logging
from typing import Any, NamedTuple, Optional

from sqlalchemy import text

from src.exceptions.base import StorydumpError
from src.services.target import bindings
from src.services.target.rate_counters import increment, window_start

logger = logging.getLogger(__name__)

#: Kinds whose ambiguity resolves by resend rather than retry-once, at most
#: `MAX_PROMPT_RESENDS` times. A card's resend leaves no second live card (a
#: card leaves without its keyboard); a duplicate notification is bounded at
#: one. A card EDIT (`prompt_supersede`) has its own rule in
#: `resolve_ambiguous`: it is idempotent — "not modified" is success — and,
#: since the route edits a card only for a menu tap, it is the only path that
#: gives a card its buttons or removes them, so it is resent until Telegram
#: confirms it (`EDIT_RESEND_HORIZON_SECONDS`).
RESEND_KINDS = frozenset({"approval_prompt", "invitation"})

#: Kinds whose ambiguity is retried EXACTLY once, then failed.
RETRY_ONCE_KINDS = frozenset({"notification", "ack"})

#: The bound the policy states: at most one extra send of a notification.
#: A row that has been ambiguous once has spent it.
MAX_NOTIFICATION_RESENDS = 1

# Bound each claim pass; persisted deferral ordering survives fresh pollers.
MEDIA_ADMISSION_PASS_LIMIT = 25


class ChannelSendError(StorydumpError):
    """What a channel's transport raises instead of a receipt.

    ``failure_class`` is what the outbox records the attempt as (outbox failure records):
    each definitive answer below names its own, and anything else is a lost
    answer, ``ambiguous``. ``code`` is the provider's own code when it answered
    with one: an int or nothing, never invented."""

    failure_class = "ambiguous"

    def __init__(self, *args, code: Optional[int] = None):
        super().__init__(*args)
        # A bool is an int to isinstance, and is no provider's code.
        valid = isinstance(code, int) and not isinstance(code, bool)
        self.code = code if valid else None


class MediaAdmissionNotSent(ChannelSendError):
    """Trusted selected-media refusal before any channel-provider dispatch.

    Never use for network failures or an uncertain send. Its reason is a closed
    service diagnostic; no provider text is retained.
    """

    def __init__(self, reason: str, *, preparation_failed: bool = False):
        self.reason = reason
        self.preparation_failed = preparation_failed
        super().__init__("selected media admission deferred")


class ChannelPaced(ChannelSendError):
    """The provider answered 429: not a lost response (the message did not
    go) and not a fault of the row: the row returns to
    `pending`, and the budget the provider named is written as a DURABLE hold
    on the pacing rows so every replica's poller defers until it passes —
    never an in-task sleep. `scope` is `global` or `chat`."""

    failure_class = "rate_limited"

    def __init__(
        self,
        message: str,
        *,
        retry_after_s: float,
        scope: str = "global",
        code: Optional[int] = None,
    ):
        super().__init__(message, code=code)
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
#: The pacing windows `settle` assumes when a caller passes none (only the
#: unit seam does; production always passes its budgets).
DEFAULT_CHAT_WINDOW_SECONDS = 60
DEFAULT_GLOBAL_WINDOW_SECONDS = 1
#: An `approval_prompt` (or invitation) is resent after an ambiguous send at
#: most this many times; past it the row fails and the intent's reaper
#: handles the rest (no cap would be a resend forever). An edit has no count
#: cap: it is idempotent, and its waits grow (`AMBIGUOUS_RESOLVE_AFTER_SECONDS`).
MAX_PROMPT_RESENDS = 3

#: An edit of a card is resent until Telegram confirms it, for at most this
#: long from its creation: one that never answers then fails, so it holds
#: nothing open (an intent's unsettled rows hold its past-posts redaction),
#: while the story's next change queues a fresh edit and a tap on the card
#: repaints it.
EDIT_RESEND_HORIZON_SECONDS = 3600


class ChannelRefused(ChannelSendError):
    """The provider answered, and said no for good — a 4xx that is neither a
    gone destination, a dead credential nor a flood limit. DEFINITIVE, like
    :class:`DestinationGone`: the message as shaped never landed, so the row
    fails outright instead of entering the ambiguity policy (a
    refused edit retried to the resend cap would be four paced calls for
    nothing)."""

    failure_class = "refused"


#: A live sender's lost answer (`settle` → `ambiguous`) waits this long,
#: times the row's attempts, before the binding's next claim applies the
#: per-kind policy (`resolve_ambiguous`): long enough for the provider's
#: answer, had it been merely slow, to have been a timeout rather than a
#: partition, and longer after each lost answer, so a row resent until
#: confirmed slows down instead of storming. `fn_sender_sweep` mints the
#: binding's sender on the same schedule. The policy's "retry once after
#: backoff" — this is the backoff.
AMBIGUOUS_RESOLVE_AFTER_SECONDS = 30

#: How many of a binding's aged `ambiguous` rows one pass resolves. Bounded
#: like every sweep; the next pass takes the rest.
AMBIGUOUS_RESOLVE_BATCH = 20


class DestinationGone(ChannelSendError):
    """The transport's DEFINITIVE answer that the destination no longer takes
    messages — the bot kicked or blocked, the chat deleted or migrated. Not a
    lost response (that is the ambiguous case), so the row fails outright and
    the caller retires or re-points the binding. *migrate_to* carries the
    successor chat id when the provider named one."""

    failure_class = "destination_gone"

    def __init__(
        self,
        detail: str = "",
        *,
        migrate_to: Optional[str] = None,
        code: Optional[int] = None,
    ):
        self.migrate_to = migrate_to
        super().__init__(
            f"destination gone{': ' + detail if detail else ''}", code=code
        )


class CredentialDead(ChannelSendError):
    """The provider rejected the channel's CREDENTIAL (Telegram: 401), not the
    message and not the destination: every send will meet the same answer until
    the token is replaced. Channel-neutral so the outbox never imports a
    channel (`TelegramAuthDead` subclasses it).

    DEFINITIVE, like :class:`ChannelRefused`: the row fails after its one
    attempt, and the binding stays."""

    failure_class = "credential_dead"


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
#: `review_required` and not `failed`: `failed` reschedules on the backoff,
#: and retrying cannot conjure a binding — it would trade a silent success for
#: a poison loop. `review_required` is terminal, is already in `ck_jobs_state`
#: (no migration), is covered by `ix_jobs_retire`, and is the state
#: meaning a human has to look. It is also countable, which "log and succeed"
#: never was: `SELECT count(*) FROM jobs WHERE state = 'review_required'`.
UNDELIVERABLE = "no-delivery-surface"


async def fanout_notification(
    session, *, workspace_id, bindings, text: str, intent_id=None
) -> int:
    """Write one `notification` row per binding. Returns rows written.

    Every customer-visible failure is routed to "the workspace's bindings",
    and this is the one loop that does it, so a change to the envelope is one
    edit rather than a hunt.

    It deliberately does NOT resolve the bindings itself. Each producer must
    decide what an EMPTY set means before it gets here — `outbox.UNDELIVERABLE`
    versus an ordinary quiet beat — and a helper that both fetched and iterated
    would make "nobody to tell" a zero-length loop, which is the exact silence
    this module's `UNDELIVERABLE` exists to break.
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
    effect it announces ("outbox rows created for active push
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
    outer CAS**: removing the outer ``AND state = 'pending'`` leaves the whole
    gate green. It cannot fire. A row another transaction already committed to
    `sending` fails the subquery's own filter at READ COMMITTED, and one being
    changed uncommittedly is locked, so SKIP LOCKED passes it over. There is
    no path where the outer predicate is the thing that refuses.

    The clause stays anyway, labelled for what it is: the statement is then
    correct on its own terms rather than on SKIP LOCKED's, so a later edit that
    changes the locking for fairness cannot silently reintroduce the race. That
    is defense in depth, and calling it the guard would be dressing a
    redundancy as a mechanism — the outbox's real fence is on the way OUT of
    `sending` (:func:`_leave_sending`), which the gate does prove load-bearing.

    Edits of cards go first, so a card's keyboard, which only an edit
    brings, follows the card before the next one is sent. Ordinary rows
    retain creation order. Approval cards use their last update so a pending
    media deferral gives later rows a turn without superseding the card.
    Creation time and id break ties deterministically.

    **Only a binding a card may go to is claimed**
    (`bindings.deliverable_binding_where`): a `deliver_outbox` job minted
    before an admin removed the group (`bindings.revoke_for_workspace`), before
    the bot was kicked, or for a private chat whose person does not belong to
    the workspace drains nothing.
    """
    row = (
        await session.execute(
            text(
                "UPDATE channel_outbox SET state = 'sending', attempts = attempts + 1"
                " WHERE id = (SELECT id FROM channel_outbox"
                "             WHERE binding_id = :b AND state = 'pending'"
                "               AND EXISTS (SELECT 1 FROM channel_bindings b"
                "                 WHERE b.id = :b"
                "                   AND b.workspace_id = channel_outbox.workspace_id"
                f"                  AND {bindings.deliverable_binding_where('b')})"
                "             ORDER BY kind <> 'prompt_supersede',"
                "               CASE WHEN kind = 'approval_prompt' THEN updated_at"
                "               ELSE created_at END, created_at, id"
                "             LIMIT 1 FOR UPDATE SKIP LOCKED)"
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
        # (a media block naming another workspace is refused).
        "workspace_id": str(row[5]),
        # Its binding, for the sender's own post-send edit.
        "binding_id": str(row[6]),
    }


async def _leave_sending(session, outbox_id: str, to_state: str, **extra) -> None:
    """Every `sending → *` write, through one CAS.

    One spelling so the fence cannot be present on some edges and absent on
    others — the divergence that made the intent ledger's CAS and its test copy
    two independent statements.

    ``failure=(class, code)`` records why the attempt failed in the SAME
    statement (outbox failure records): the record is written exactly when, and only if, the
    state change is, so a fenced writer records nothing.
    """
    sets = ["state = :s"]
    params = {"s": to_state, "i": outbox_id}
    if extra.get("failure"):
        failure_class, error_code = extra["failure"]
        sets += [
            "last_failure_class = :fc",
            "last_error_code = :fcode",
            "last_failed_at = now()",
        ]
        params["fc"] = str(failure_class)
        params["fcode"] = error_code
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
    if "payload" in extra:
        # The card as it is about to be sent (`_with_the_storys_keyboard`):
        # the record must be what goes out, so the ambiguous path and the
        # later edits read the right header.
        sets.append("payload = CAST(:p AS jsonb)")
        params["p"] = json.dumps(extra["payload"])
    if extra.get("restore_attempt"):
        # A provider's limit (429) is not the row's failure: the attempt the
        # claim consumed is given back, so the ambiguity budget (one retry for
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


async def mark_ambiguous(session, *, outbox_id: str, failure: tuple) -> None:
    """`sending → ambiguous`: the send left, the response did not come back.

    The no-blind-retry rule lands here. Resolution is the per-kind policy in
    :func:`resolve_ambiguous`, never an immediate re-send at the call site:
    the binding's sender applies it on a later tick, once the row has waited
    `AMBIGUOUS_RESOLVE_AFTER_SECONDS` (`resolve_aged_ambiguous`).
    """
    await _leave_sending(session, outbox_id, "ambiguous", failure=failure)


async def resolve_ambiguous(session, *, outbox_id: str) -> str:
    """Apply the per-kind policy to one `ambiguous` row.

    Returns the state it moved to. Reads the kind from the row rather than
    taking it as an argument: the policy is a property of the row, and a caller
    that could pass the wrong kind is a second authority on which rule applies.
    """
    row = (
        await session.execute(
            text(
                "SELECT o.kind, o.attempts,"
                # A later edit of the SAME message (any state): resending this
                # one would land its older line over the newer. Joined
                # on the intent so it rides `ix_outbox_intent` (`ix_outbox_intent`); an
                # intent-less edit (an invitation card's) has no such check.
                "       EXISTS (SELECT 1 FROM channel_outbox n"
                "                WHERE n.intent_id = o.intent_id"
                "                  AND n.binding_id = o.binding_id"
                "                  AND n.kind = 'prompt_supersede'"
                "                  AND n.payload->>'supersedes_ref' = o.payload->>'supersedes_ref'"
                "                  AND n.created_at > o.created_at) AS newer_edit,"
                "       o.created_at > now() - make_interval(secs => :horizon) AS fresh"
                "  FROM channel_outbox o"
                " WHERE o.id = :i AND o.state = 'ambiguous'"
            ),
            {"i": outbox_id, "horizon": EDIT_RESEND_HORIZON_SECONDS},
        )
    ).first()
    if row is None:
        raise OutboxFenced(
            f"outbox {outbox_id}: not 'ambiguous' — already resolved elsewhere"
        )
    kind, attempts, newer_edit = row[0], row[1], bool(row[2])

    if kind == "prompt_supersede":
        # Idempotent, so resent until Telegram confirms it, each wait longer
        # than the last (`resolve_aged_ambiguous`). Retired, never resent,
        # once the message has a newer line queued or landed (a resend carries
        # ITS line); failed past `EDIT_RESEND_HORIZON_SECONDS`.
        if newer_edit:
            to_state = "superseded"
        else:
            to_state = "pending" if row[3] else "failed"
    elif kind in RESEND_KINDS:
        # Resend — an approval card leaves without its keyboard, so its
        # resend leaves no second live card; an invitation leaves with its
        # own — but not forever: a row that keeps losing its answer ends
        # `failed` after MAX_PROMPT_RESENDS.
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
    the edit keeps it and appends the outcome), and how the card went out. No
    keyboard: the sender gives the edit its story's when it claims it."""
    payload = _json(payload)
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
    spare_pending: Optional[timedelta] = None,
) -> int:
    """Supersede every live card for *intent_id* and queue the supersede rows.

    On any intent state change, `prompt_supersede` rows target **every
    known** `external_message_ref`; a copy whose ref was lost carries no
    keyboard, since a card's keyboard arrives only by an edit of a known
    message. Returns the number of cards superseded.

    This is also the only edit path — "edits always go supersede-then-send,
    never edit-in-place on an ambiguous ref". With *outcome_text* the edit
    writes the card's header plus that line under it; without it only the
    keyboard is edited, to the story's as the sender claims the row.

    A row in `sending` is superseded too: the sender commits its claim before
    the provider call, so `sending` is a committed state a
    tap can meet; the sender finds its `mark_sent` fenced and edits the card
    it just sent itself (`OutboxPoller`).

    The edit is queued only where a card may go
    (`bindings.deliverable_binding_where`); elsewhere the card is superseded
    and nothing is queued.

    *spare_pending* leaves a card younger than that alone while it still waits
    to go out — `pending`, or `ambiguous` and owed its resend: the settled-card
    sweep passes `prompts.SETTLED_CARD_GRACE` for a card the sender will send
    settled, so a story that posts before its card goes out still
    sends one card that says so.
    """
    spare = (
        "   AND NOT (state IN ('pending', 'ambiguous')"
        "            AND created_at > now() - make_interval(secs => :spare))"
        if spare_pending is not None
        else ""
    )
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
                f"{spare}"
                " RETURNING external_message_ref, payload,"
                "   EXISTS (SELECT 1 FROM channel_bindings cb WHERE cb.id = :b"
                f"           AND {bindings.deliverable_binding_where('cb')}) AS deliverable"
            ),
            {
                "ws": workspace_id,
                "b": binding_id,
                "i": intent_id,
                "o": outcome_text,
                **(
                    {"spare": spare_pending.total_seconds()}
                    if spare_pending is not None
                    else {}
                ),
            },
        )
    ).fetchall()

    for ref, payload, deliverable in live:
        if ref is None or not deliverable:
            # The ref was lost (or never sent) — any copy carries no
            # keyboard — or no card may go to this chat.
            continue
        await enqueue(
            session,
            workspace_id=workspace_id,
            binding_id=binding_id,
            kind="prompt_supersede",
            payload=_supersede_payload(str(ref), payload, outcome_text),
            intent_id=intent_id,
        )
    return len(live)


class Touched(NamedTuple):
    """An every-binding edit's answer when the caller names the card a tap
    came from: how many cards it edited, and whether that card was among
    them. When it was not, the caller adopts it (:func:`adopt_card`) — a
    twin the ledger never learned — without a second read of the ledger on
    the ordinary tap (the statement budget holds)."""

    count: int
    touched_known: bool


_TOUCHED_FLAG = (
    ", (SELECT count(*) FROM {src} s2"
    "     WHERE s2.binding_id = CAST(:tb AS uuid)"
    "       AND s2.external_message_ref = CAST(:tref AS text)) AS touched_known"
)


def _touched_params(touched: Optional[tuple[str, str]]) -> dict:
    """The flag's parameters — none when no card was named, so the count
    form's statement and parameters are exactly what they were."""
    if touched is None:
        return {}
    return {"tb": str(touched[0]), "tref": str(touched[1])}


async def supersede_everywhere(
    session, *, workspace_id: str, intent_id: str, outcome_text: Optional[str]
) -> int:
    """`supersede_all` for EVERY active Telegram binding of the workspace, in
    one statement: the bindings, the supersede of every live card and
    the `prompt_supersede` rows are one round trip inside the tap's
    transaction instead of one read plus two writes per binding. The payload
    is `_supersede_payload`'s, built in SQL. Returns the cards superseded."""
    return (
        await supersede_everywhere_touched(
            session,
            workspace_id=workspace_id,
            intent_id=intent_id,
            outcome_text=outcome_text,
            touched=None,
        )
    ).count


async def supersede_everywhere_touched(
    session,
    *,
    workspace_id: str,
    intent_id: str,
    outcome_text: Optional[str],
    touched: Optional[tuple[str, str]],
) -> Touched:
    """Func:`supersede_everywhere`, also answering whether *touched* — the
    `(binding_id, external_message_ref)` a tap came from — was among the
    cards it superseded. Cards are superseded in every live
    binding; the edit is queued only where a card may go."""
    row = (
        await session.execute(
            text(
                "WITH b AS ("
                f"  SELECT id, ({bindings.DELIVERABLE_BINDING_WHERE}) AS deliverable"
                "     FROM channel_bindings"
                f"   WHERE workspace_id = :ws AND {bindings.push_binding_where()}"
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
                "    FROM sup s JOIN b ON b.id = s.binding_id"
                "   WHERE s.external_message_ref IS NOT NULL AND b.deliverable"
                "  RETURNING id"
                ")"
                " SELECT (SELECT count(*) FROM sup) AS superseded,"
                "        (SELECT count(*) FROM ins) AS queued"
                + (_TOUCHED_FLAG.format(src="sup") if touched is not None else "")
            ),
            {
                "ws": workspace_id,
                "i": intent_id,
                "o": outcome_text,
                **_touched_params(touched),
            },
        )
    ).first()
    if row is None:
        return Touched(0, False)
    # The flag is the statement's last column; a caller that named no card
    # asked no question (and a scripted session may answer a bare tuple).
    return Touched(int(row[0] or 0), bool(row[-1]) if touched is not None else False)


#: An edit that is in flight or has landed: a tapped card with one is left
#: to it (`adopt_card`).
_LANDING = ("pending", "sending", "ambiguous", "sent")


async def adopt_card(
    session,
    *,
    workspace_id: str,
    binding_id: str,
    intent_id: str,
    ref: str,
    outcome_text: str,
) -> bool:
    """The message a tap came from shows its story as it is now —
    restate-by-ref on touch.

    A message with buttons the ledger never learned (a copy whose send lost
    its answer) is adopted: a tap
    carries its message id, so if *ref* is not a card of *intent_id* on
    *binding_id*, INSERT it as one — `superseded`, with the known card's own
    content (header, `sent_as`) so the edit renders as the card was sent —
    and queue the edit that writes *outcome_text* onto it, which leaves with
    the story's keyboard as the sender claims it. Adopted, every later
    restate-by-ref (the Posted line, a review resolution) reaches it as it
    reaches the known card. A card of this story the ledger knows is edited
    again only when its newest edit did not land (failed, retired, or never
    queued); one with an edit in flight or landed is left to it
    (`_LANDING`). Returns True when an
    edit was queued. Queues nothing when the story has no card on the
    binding, when another row on the binding owns the ref (another story's
    card, an invitation, a notice — Telegram does not bind a callback's token
    to its message), or past the evidence: the known row's send attempts
    bound the copies it can have (one per lost answer). In the caller's
    transaction."""
    known = (
        await session.execute(
            text(
                "SELECT payload, external_message_ref, attempts FROM channel_outbox"
                " WHERE workspace_id = :ws AND binding_id = :b AND intent_id = :i"
                "   AND kind = 'approval_prompt'"
                " ORDER BY created_at DESC"
            ),
            {"ws": workspace_id, "b": binding_id, "i": intent_id},
        )
    ).fetchall()
    if not known:
        # A twin exists only where the story's card was sent: no card
        # on this binding, nothing to adopt.
        return False
    card = next((r for r in known if str(r[1]) == str(ref)), None)
    if card is not None:
        # This story's own card: edited again only when its newest edit did
        # not land (failed, retired, or never queued).
        newest = (
            await session.execute(
                text(
                    "SELECT state FROM channel_outbox"
                    " WHERE workspace_id = :ws AND binding_id = :b AND intent_id = :i"
                    "   AND kind = 'prompt_supersede'"
                    "   AND payload->>'supersedes_ref' = :ref"
                    " ORDER BY created_at DESC LIMIT 1"
                ),
                {"ws": workspace_id, "b": binding_id, "i": intent_id, "ref": str(ref)},
            )
        ).first()
        if newest is not None and newest[0] in _LANDING:
            return False
        template = dict(_json(card[0]))
    else:
        owned = (
            await session.execute(
                text(
                    "SELECT 1 FROM channel_outbox"
                    " WHERE workspace_id = :ws AND binding_id = :b"
                    "   AND external_message_ref = :ref LIMIT 1"
                ),
                {"ws": workspace_id, "b": binding_id, "ref": str(ref)},
            )
        ).first()
        if owned is not None:
            # Telegram does not bind a callback's intent token to its message
            # id: a modified client can send this story's token with another
            # bot message's id. A message another row owns on this binding —
            # another story's card, an invitation, a notice — is never touched.
            return False
        sends = max(int(r[2] or 0) for r in known)
        if len(known) >= sends:
            # A twin exists only per lost answer: the card's row counts its
            # send attempts, so a card sent once has no twin and a row showing
            # two sends can have one. Adoption stays within that evidence — a
            # forged callback with a fresh message id adopts nothing past it.
            return False
        template = dict(_json(known[0][0]))
        template.setdefault("v", 2)
        await session.execute(
            text(
                "INSERT INTO channel_outbox"
                " (workspace_id, binding_id, kind, intent_id, payload, state,"
                "  external_message_ref)"
                " VALUES (:ws, :b, 'approval_prompt', :i, CAST(:p AS jsonb),"
                "         'superseded', :ref)"
            ),
            {
                "ws": workspace_id,
                "b": binding_id,
                "i": intent_id,
                "p": json.dumps({**template, "outcome_text": outcome_text}),
                "ref": str(ref),
            },
        )
    await enqueue(
        session,
        workspace_id=workspace_id,
        binding_id=binding_id,
        kind="prompt_supersede",
        payload=_supersede_payload(str(ref), template, outcome_text),
        intent_id=intent_id,
    )
    return True


async def restate_everywhere(
    session,
    *,
    workspace_id: str,
    intent_id: str,
    outcome_text: str,
) -> int:
    """The count form of :func:`restate_everywhere_touched`."""
    return (
        await restate_everywhere_touched(
            session,
            workspace_id=workspace_id,
            intent_id=intent_id,
            outcome_text=outcome_text,
            touched=None,
        )
    ).count


async def restate_everywhere_touched(
    session,
    *,
    workspace_id: str,
    intent_id: str,
    outcome_text: str,
    touched: Optional[tuple[str, str]],
) -> Touched:
    """`restate_cards` for EVERY active Telegram binding of the workspace, in
    one statement — the review resolutions' door: the card a
    `review_required` intent shows was superseded by the approve tap, so the
    resolution's line reaches it by ref, and the shape is
    `supersede_everywhere`'s: one round trip inside the transaction.
    Returns the edits queued — only where a card may go."""
    row = (
        await session.execute(
            text(
                "WITH b AS ("
                f"  SELECT id, ({bindings.DELIVERABLE_BINDING_WHERE}) AS deliverable"
                "     FROM channel_bindings"
                f"   WHERE workspace_id = :ws AND {bindings.push_binding_where()}"
                "), upd AS ("
                "  UPDATE channel_outbox o"
                "     SET payload = o.payload || jsonb_build_object('outcome_text', CAST(:o AS text))"
                "   WHERE o.workspace_id = :ws AND o.intent_id = :i"
                "     AND o.binding_id IN (SELECT id FROM b)"
                "     AND o.kind = 'approval_prompt'"
                "     AND o.state IN ('sent', 'superseded', 'ambiguous')"
                "     AND o.external_message_ref IS NOT NULL"
                "   RETURNING o.binding_id, o.external_message_ref, o.payload"
                "), refs AS ("
                "  SELECT DISTINCT ON (binding_id, external_message_ref)"
                "         binding_id, external_message_ref, payload FROM upd"
                "), ins AS ("
                "  INSERT INTO channel_outbox (workspace_id, binding_id, kind, intent_id, payload)"
                "  SELECT :ws, r.binding_id, 'prompt_supersede', :i,"
                "         jsonb_strip_nulls(jsonb_build_object("
                "           'v', 1,"
                "           'supersedes_ref', r.external_message_ref,"
                "           'outcome_text', CAST(:o AS text),"
                "           'header', COALESCE(NULLIF(r.payload->>'caption', ''),"
                "                              NULLIF(r.payload->>'text', '')),"
                "           'sent_as', NULLIF(r.payload->>'sent_as', '')))"
                "    FROM refs r JOIN b ON b.id = r.binding_id WHERE b.deliverable"
                "  RETURNING id"
                ")"
                " SELECT (SELECT count(*) FROM ins) AS queued"
                + (_TOUCHED_FLAG.format(src="refs") if touched is not None else "")
            ),
            {
                "ws": workspace_id,
                "i": intent_id,
                "o": outcome_text,
                **_touched_params(touched),
            },
        )
    ).first()
    if row is None:
        return Touched(0, False)
    # The flag is the statement's last column; a caller that named no card
    # asked no question (and a scripted session may answer a bare tuple).
    return Touched(int(row[0] or 0), bool(row[-1]) if touched is not None else False)


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
    `superseded` and `supersede_all` would find nothing. This
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


async def chats_without_card(
    session, *, workspace_id: str, intent_id: str
) -> list[str]:
    """The push bindings (`bindings.DELIVERABLE_BINDING_WHERE`) that hold no
    card of *intent_id*: no `approval_prompt` row that reached the chat (it
    has a message ref) or may still reach it (pending, sending, ambiguous).
    A card retired or failed before it was sent left the chat nothing. These
    are the chats a parked story's review card is owed to
    (`prompts.prompt_review`). In the caller's transaction."""
    rows = (
        await session.execute(
            text(
                "SELECT b.id FROM channel_bindings b"
                f" WHERE b.workspace_id = :ws AND {bindings.deliverable_binding_where('b')}"
                "   AND NOT EXISTS (SELECT 1 FROM channel_outbox o"
                "     WHERE o.workspace_id = b.workspace_id AND o.binding_id = b.id"
                "       AND o.intent_id = CAST(:i AS uuid)"
                "       AND o.kind = 'approval_prompt'"
                "       AND (o.external_message_ref IS NOT NULL"
                "            OR o.state IN ('pending', 'sending', 'ambiguous')))"
                " ORDER BY b.id"
            ),
            {"ws": workspace_id, "i": intent_id},
        )
    ).fetchall()
    return [str(r[0]) for r in rows]


def _needs_refresh(row: dict) -> bool:
    """Whether a claimed row is rendered again before the send: every
    `approval_prompt` with an intent (a card is minted on the assumption it
    leaves within seconds; anything that delays it — a lost answer, a pacing
    hold, a dead sender — ships a snapshot of settings that may have
    changed)."""
    return row.get("kind") == "approval_prompt" and bool(row.get("intent_id"))


def _takes_the_storys_keyboard(row: dict) -> bool:
    """Whether the keyboard a claimed row leaves with is its story's to
    decide: an approval card (which leaves with none) or an edit of one — a
    `prompt_supersede` with an intent. An invitation's edit carries no
    intent, and is left as it was."""
    return row.get("kind") in ("approval_prompt", "prompt_supersede") and bool(
        row.get("intent_id")
    )


async def _touch_media_deferred(session, *, outbox_id: str) -> None:
    await session.execute(
        text(
            "UPDATE channel_outbox SET updated_at=clock_timestamp()"
            " WHERE id=:id AND state='pending' AND kind='approval_prompt'"
        ),
        {"id": outbox_id},
    )


async def _claim_current(
    session, *, binding_id: str, on_deferred=None
) -> Optional[dict]:
    """The next row to send, as the slot IS now: a card is
    rendered again at claim time (`prompts.rerender_prompt`) — the slot in
    the workspace's clock — and a card whose slot has moved on is retired
    unsent and the next row claimed, so a card resent days after its mint
    never shows the text of the day it was minted. The card leaves with no
    keyboard, and its record is written after the pacing
    (`_with_the_storys_keyboard`).

    A bounded claim/refresh pass; pending cards rotate without spending
    attempts. The outer caller holds the complete source prefix. A refused
    refresh rolls back only its claim, then persists a pending-row touch
    before trying a later subject. Exhaustion returns normally so these
    touches can commit."""
    from src.services.target import prompts  # noqa: PLC0415 — cycle
    from src.services.target.source_policy import MediaAdmissionPending

    for _ in range(MEDIA_ADMISSION_PASS_LIMIT):
        row = None
        try:
            async with session.begin_nested():
                row = await claim_next(session, binding_id=binding_id)
                if row is None or not _needs_refresh(row):
                    return row
                fresh = await prompts.rerender_prompt(
                    session, intent_id=str(row["intent_id"])
                )
                if fresh is None:
                    await _leave_sending(session, row["id"], "superseded")
                    continue
                row["payload"] = fresh
                return row
        except MediaAdmissionPending:
            await _touch_media_deferred(session, outbox_id=row["id"])
            if on_deferred is not None:
                on_deferred()
    return None


async def _with_the_storys_keyboard(session, row: dict) -> dict:
    """*row* as it leaves. An approval card leaves with NO keyboard: a
    keyboard reaches a chat only as an edit of a message whose id the ledger
    holds (:func:`_edit_sent_card` queues it as the send is settled), so a
    copy a lost answer left behind carries no buttons. The card's record is
    rewritten through the one fence, so the ambiguous path and the later
    edits read what was sent. An edit of a card gets the keyboard of its
    story as it is now (`prompts.card_keyboard_for`): no caller picks one,
    so a rejected card loses its buttons because it is rejected. An edit's
    record is not rewritten, since only the transport reads its keyboard, and
    a resent edit is claimed and given one again. Called after the pacing,
    which a keyboard never changes, so a paced attempt reads and writes none
    of this."""
    from src.services.target import prompts  # noqa: PLC0415 — cycle

    if not _takes_the_storys_keyboard(row):
        return row
    payload = dict(_json(row["payload"]))
    if row["kind"] == "approval_prompt":
        payload.pop("reply_markup", None)
        await _leave_sending(session, row["id"], "sending", payload=payload)
        return {**row, "payload": payload}
    keyboard = await prompts.card_keyboard_for(
        session,
        workspace_id=str(row["workspace_id"]),
        intent_id=str(row["intent_id"]),
    )
    if keyboard is None:
        payload.pop("reply_markup", None)
    else:
        payload["reply_markup"] = keyboard
    return {**row, "payload": payload}


async def resolve_aged_ambiguous(session, *, binding_id: str) -> list:
    """Apply the per-kind policy to the binding's `ambiguous` rows that have
    waited the backoff, `AMBIGUOUS_RESOLVE_AFTER_SECONDS` times their
    attempts. Returns the ids resolved, oldest first.

    `settle` marks a live sender's lost answer `ambiguous`, and the binding's
    sender brings it back here on a later tick (`pace_and_claim`), so
    the resend policy reaches a live sender's rows as well as the rows a DEAD
    sender stranded. Bounded per tick; the binding's sender is single
    (`tg:<binding_id>`), so no other writer races the rows. `fn_sender_sweep`
    mints the sender on the same schedule, so a row still waiting mints none."""
    rows = (
        await session.execute(
            text(
                "SELECT id FROM channel_outbox"
                " WHERE binding_id = :b AND state = 'ambiguous'"
                "   AND updated_at <= now() - make_interval(secs => :age)"
                "   AND updated_at <= now() - make_interval("
                "         secs => CAST(:age AS double precision) * GREATEST(attempts, 1))"
                " ORDER BY created_at LIMIT :lim"
            ),
            {
                "b": binding_id,
                "age": AMBIGUOUS_RESOLVE_AFTER_SECONDS,
                "lim": AMBIGUOUS_RESOLVE_BATCH,
            },
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
    whether the send left is exactly what nobody knows, and
    :func:`resolve_ambiguous` owns what happens next.
    """
    rows = (
        await session.execute(
            text(
                "UPDATE channel_outbox SET state = 'ambiguous',"
                "       last_failure_class = 'ambiguous', last_error_code = NULL,"
                "       last_failed_at = now()"
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
    takes the outbox write with it. A `deliver` that committed on its
    own would put the send-state authority outside the fence that protects it.

    Order is recover → claim → pace → send. Recovery first is load-bearing: a
    predecessor's stranded row is older work than anything pending, and
    resolving it may put a row back in the queue this same call then takes.

    **The claim precedes the pacing so an EMPTY queue debits
    nothing** — an idle poller ticking every 2 s would otherwise spend a
    chat's whole window on silence. Correctness of the paced case comes from
    the transaction: a paced call raises before committing, so the caller's
    rollback un-claims the row it took (the gate proves a paced row stays
    pending). The order is a preference for the idle case, never the guard.

    *transport* is injected and channel-neutral: it takes the payload and
    returns an external ref, or raises to signal a lost response. The floor is
    about send-state, not about Telegram, and a real client here would make
    every gate test a network test.

    Production runs :meth:`OutboxPoller.tick`; this is the same sequence as ONE
    transaction, kept as the seam the settle-taxonomy tests drive ( —
    not a second production path). The edit a sent card is owed
    (:func:`_edit_sent_card`) is the poller's, queued once the settle stands.
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
    on_media_deferred=None,
) -> Optional[dict]:
    """Recover → pace → claim: everything BEFORE the provider is spoken to,
    in the caller's transaction, the claimed card's keyboard last. The poller
    commits this before it sends, so the `tg_global` rate row and the
    claimed row are never locked across the call."""
    await _lock_binding_sources(session, binding_id=binding_id)
    stranded = await recover_stranded(session, binding_id=binding_id)
    for outbox_id in stranded:
        await resolve_ambiguous(session, outbox_id=outbox_id)
    # A live sender's own lost answers, once they have waited the backoff.
    await resolve_aged_ambiguous(session, binding_id=binding_id)

    # Claim FIRST, then pace: nothing pending means nothing debited, so an
    # idle poller never spends the chat's (or the fleet's) window on empty
    # ticks — at the 2 s cadence that would burn a 20/min chat budget in 40 s
    # of silence and defer the card that then arrives. A
    # paced claim raises, and the caller's rollback un-claims the row — the
    # order was always a preference, never the guard (see `deliver`).
    row = await _claim_current(
        session, binding_id=binding_id, on_deferred=on_media_deferred
    )
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

    return await _with_the_storys_keyboard(session, row)


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
    the row's exit from `sending` by the taxonomy. *receipt* is the ref the
    channel returned (a `SendReceipt` also says how the card went out);
    *error* is what the transport raised instead. A `ChannelPaced` error
    (429) returns the row to `pending` and — when the caller passes its
    clock and budgets — writes the provider's `retry_after` as a durable hold
    on the pacing rows: the chat's row for the whole
    `retry_after` when the 429 is chat-scoped (with a short brake on the
    global row), the global row for up to a minute otherwise."""
    if isinstance(error, MediaAdmissionNotSent) and receipt is None:
        await _leave_sending(session, row["id"], "pending", restore_attempt=True)
        # Pacing admission committed before the byte read. Keep that charge:
        # refunding here could undo a concurrent provider hold or another bucket.
        return {
            **row,
            "state": "media_deferred",
            "external_message_ref": None,
            "preparation_failed": error.preparation_failed,
        }
    # What a failed attempt is recorded as (outbox failure records): the transport's own class and
    # code, or a lost answer with no code for anything else.
    failure = (
        (error.failure_class, error.code)
        if isinstance(error, ChannelSendError)
        else ("ambiguous", None)
    )
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
                    window_seconds=chat_window_seconds or DEFAULT_CHAT_WINDOW_SECONDS,
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
                window_seconds=global_window_seconds or DEFAULT_GLOBAL_WINDOW_SECONDS,
            )
        # The row goes back to `pending` with the attempt the claim consumed
        # restored: the provider's limit is not this row's failure. It is
        # still RECORDED (a deferral, never counted as a failure).
        await _leave_sending(
            session,
            row["id"],
            "pending",
            restore_attempt=True,
            failure=failure,
        )
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
        await _leave_sending(session, row["id"], "failed", failure=failure)
        return {
            **row,
            "state": "failed",
            "external_message_ref": None,
            "destination_gone": True,
            "migrate_to": error.migrate_to,
        }
    if isinstance(error, (ChannelRefused, CredentialDead)):
        # Definitive, like a gone destination: this message as shaped will
        # never land, or no message will until the credential is replaced.
        # Nothing to resend; the row fails, and the binding stays.
        await _leave_sending(session, row["id"], "failed", failure=failure)
        return {**row, "state": "failed", "external_message_ref": None}
    if error is not None:
        # A lost response is the ambiguous case.
        await mark_ambiguous(session, outbox_id=row["id"], failure=failure)
        return {**row, "state": "ambiguous", "external_message_ref": None}

    if row.get("kind") == "approval_prompt" and _json(row.get("payload")).get(
        "outcome_text"
    ):
        # A card sent settled went out with its line, so it is
        # recorded `superseded` with its ref: a restate by ref still reaches it,
        # and the settled-card sweep no longer selects it. It is still a card
        # that was SENT, which is what the caller is told, so its after-send
        # edit still runs for a story that moved on during the send.
        await _leave_sending(
            session,
            row["id"],
            "superseded",
            external_message_ref=str(receipt),
            sent_as=getattr(receipt, "sent_as", None),
        )
    else:
        await mark_sent(
            session,
            outbox_id=row["id"],
            external_message_ref=str(receipt),
            sent_as=getattr(receipt, "sent_as", None),
        )
    return {**row, "state": "sent", "external_message_ref": str(receipt)}


def _json(payload: Any) -> dict:
    """A payload as a dict: the claim-time render is one, and a driver may hand
    jsonb back as text."""
    if isinstance(payload, str):
        payload = json.loads(payload)
    return payload or {}


async def _edit_sent_card(session, row: dict, receipt, *, force: bool) -> bool:
    """After a card's send: queue the edit that gives the message its
    keyboard — the card left with none (`_with_the_storys_keyboard`) — and,
    when the intent has moved past `awaiting_approval` since the claim (a tap,
    the web queue, the reaper), the line it now carries. The keyboard is the
    story's as the sender claims the edit, so a card whose story was decided
    meanwhile never shows decision buttons. *force* is the fenced case: the
    row left `sending` under us. Only a row that was SUPERSEDED (a tap or a
    cancellation retired the card in flight) is owed the edit, with the line
    that supersede carried, and the row takes the ref, so a later line
    reaches the message; a row a successor moved to `ambiguous`
    (`recover_stranded`, a partition) is the successor's to resend, and the
    copy this send left carries no buttons. *receipt* is the `SendReceipt`
    (its `sent_as` decides caption vs text). A card sent settled already
    carries its line, so its edit brings the keyboard alone unless the story
    has moved on from the state the line reads. Returns whether an edit was
    queued."""
    if row.get("kind") != "approval_prompt" or not row.get("intent_id"):
        return False
    from src.services.target import intent_ledger, prompts  # noqa: PLC0415 — cycle

    ref = str(receipt)
    payload = _json(row.get("payload"))
    sent_as = getattr(receipt, "sent_as", None) or payload.get("sent_as")
    carried = None
    if force:
        current = (
            await session.execute(
                text(
                    "UPDATE channel_outbox SET external_message_ref = :ref,"
                    "   payload = CASE WHEN CAST(:sent_as AS text) IS NULL THEN payload"
                    "     ELSE payload || jsonb_build_object('sent_as', CAST(:sent_as AS text))"
                    "   END"
                    " WHERE id = :i AND workspace_id = :ws AND state = 'superseded'"
                    " RETURNING payload->>'outcome_text'"
                ),
                {
                    "ref": ref,
                    "sent_as": sent_as,
                    "i": row["id"],
                    "ws": str(row["workspace_id"]),
                },
            )
        ).first()
        if current is None:
            return False
        carried = current[0]
    found = await intent_ledger.settlement(
        session, workspace_id=str(row["workspace_id"]), intent_id=str(row["intent_id"])
    )
    state = found["state"]
    if state is None:
        return False
    outcome = carried
    if outcome is None and state not in prompts.LIVE_FOR_A_CARD:
        from datetime import datetime, timezone  # noqa: PLC0415

        tz_row = (
            await session.execute(
                text("SELECT tz FROM workspaces WHERE id = :ws"),
                {"ws": str(row["workspace_id"])},
            )
        ).first()
        tz = str(tz_row[0]) if tz_row and tz_row[0] else "UTC"
        outcome = await prompts.settled_line(
            session, found, tz=tz, at=datetime.now(timezone.utc)
        )
    if outcome == payload.get("outcome_text"):
        outcome = None  # the card already carries its line: the keyboard alone
    await enqueue(
        session,
        workspace_id=str(row["workspace_id"]),
        binding_id=str(row["binding_id"]),
        kind="prompt_supersede",
        payload=_supersede_payload(
            ref,
            {**payload, **({"sent_as": sent_as} if sent_as else {})},
            outcome,
        ),
        intent_id=str(row["intent_id"]),
    )
    return True


class OutboxPoller:
    """The `run_at` polling loop.

    The cadence is **2 s** — "invisible in pg at ≪1 msg/s" — and it
    arrives as a parameter like every other number. This class is the
    whole wake-up mechanism; no second channel sits beside it.

    One tick is one :func:`deliver` in its own transaction, so a paced or
    fenced tick rolls back alone and the next one is unaffected. Deliberately
    NOT in any pipeline's await chain, for the same reason the heartbeat is
    not: a provider wait must never delay the loop that drains the queue.

    Counters a supervisor can act on: `ticks`,
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
        past_posts_capability=None,
    ):
        self._past_posts_capability = past_posts_capability
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

    def _media_deferred(self) -> None:
        self.deferred += 1

    async def tick(self) -> Optional[dict]:
        """One poll, transaction-per-checkpoint: pace and claim,
        COMMIT, speak to the
        provider with no transaction open, then settle in a fresh one. The
        `tg_global` rate row is never held across the call, and the
        claimed row's `sending` is a committed state a tap can supersede.
        Never raises: a tick that failed is an observable, not an exception
        the loop has to survive twice."""
        from src.services.target.source_policy import MediaAdmissionPending

        self.ticks += 1
        try:
            async with self._session_factory() as session:
                if self._past_posts_capability is not None:
                    session.info["past_posts_capability"] = self._past_posts_capability
                row = await pace_and_claim(
                    session,
                    binding_id=self._binding_id,
                    now=self._clock(),
                    on_media_deferred=self._media_deferred,
                    **self._budgets,
                )
                await session.commit()
        except (OutboxPaced, MediaAdmissionPending):
            self.deferred += 1
            self.consecutive_failures = 0  # pacing is health, not failure
            return None
        except Exception as exc:  # noqa: BLE001 — counted and safely reported
            self.consecutive_failures += 1
            logger.warning(
                "outbox tick failure phase=claim exception=%s consecutive_failures=%s",
                type(exc).__name__,
                self.consecutive_failures,
            )
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
                    # supersede. The card is out there without its keyboard —
                    # record its ref and edit it ourselves, with the line the
                    # supersede carried and the story's keyboard.
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
        self.consecutive_failures = (
            self.consecutive_failures + 1 if result.get("preparation_failed") else 0
        )
        if result["state"] == "sent":
            self.sent += 1
        elif result["state"] == "media_deferred":
            self.deferred += 1
        elif result["state"] == "paced":
            # A 429: the hold is written; this binding's sender should yield
            # its lane and come back when the provider said to.
            self.deferred += 1
            self.held_for_s = float(result.get("retry_after_s") or 0.0)
        return result


async def _lock_binding_sources(session, *, binding_id):
    from src.services.target import source_policy, readers

    binding = await readers.row(
        session,
        "SELECT workspace_id FROM channel_bindings WHERE id=:id",
        id=str(binding_id),
    )
    if binding is not None:
        await source_policy.suitability_context(
            session, workspace_id=str(binding["workspace_id"])
        )
