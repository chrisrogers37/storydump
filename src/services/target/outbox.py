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

**And the write is bound to the job's own fence.** `deliver` marks the row
inside the CALLER's transaction and never commits; the caller commits it
together with `finalize_job`, whose token CAS raises `JobFenced` for a stale
owner (`02` §5's "the domain transaction and job finalization commit
together"). So a stale sender's outbox write rolls back with its failed
finalization. That co-location is a service discipline rather than a database
one, which is exactly the shape #883 warns about — so the gate proves it by
running a stale sender against a live one, not by asserting it here.

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
from typing import Optional

from sqlalchemy import text

from src.exceptions.base import StorydumpError
from src.services.target.rate_counters import increment, window_start

#: `02` §6 kinds whose ambiguity resolves by resend rather than retry-once.
#: Two live cards are tolerable; a duplicate notification is not.
RESEND_KINDS = frozenset({"approval_prompt", "invitation"})

#: Kinds whose ambiguity is retried EXACTLY once, then failed.
RETRY_ONCE_KINDS = frozenset({"notification", "ack"})

#: The bound the policy states: at most one extra send of a notification.
#: A row that has been ambiguous once has spent it.
MAX_NOTIFICATION_RESENDS = 1


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
                " RETURNING id, kind, payload, attempts, intent_id"
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


async def mark_sent(session, *, outbox_id: str, external_message_ref: str) -> None:
    """`sending → sent`, recording the ref the channel returned."""
    await _leave_sending(
        session, outbox_id, "sent", external_message_ref=external_message_ref
    )


async def mark_ambiguous(session, *, outbox_id: str) -> None:
    """`sending → ambiguous`: the send left, the response did not come back.

    R8's no-blind-retry rule lands here. Resolution is the per-kind policy in
    :func:`resolve_ambiguous`, never an immediate re-send at the call site.
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
        to_state = "pending"  # resend; the duplicate card is tolerated
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


async def supersede_all(
    session, *, workspace_id: str, binding_id: str, intent_id: str
) -> int:
    """Supersede every live card for *intent_id* and queue the supersede rows.

    `02` §6: on any intent state change, `prompt_supersede` rows target **every
    known** `external_message_ref`; a card whose ref was lost simply ages out.
    Returns the number of cards superseded.

    This is also the only edit path — "edits always go supersede-then-send,
    never edit-in-place on an ambiguous ref". A caller wanting to change a card
    calls this and then :func:`enqueue`.
    """
    live = (
        await session.execute(
            text(
                "UPDATE channel_outbox SET state = 'superseded'"
                " WHERE workspace_id = :ws AND binding_id = :b AND intent_id = :i"
                "   AND kind IN ('approval_prompt', 'invitation')"
                "   AND state IN ('pending', 'sent', 'ambiguous')"
                " RETURNING external_message_ref"
            ),
            {"ws": workspace_id, "b": binding_id, "i": intent_id},
        )
    ).fetchall()

    for (ref,) in live:
        if ref is None:
            continue  # the ref was lost; the card ages out under R6
        await enqueue(
            session,
            workspace_id=workspace_id,
            binding_id=binding_id,
            kind="prompt_supersede",
            payload={"v": 1, "supersedes_ref": ref},
            intent_id=intent_id,
        )
    return len(live)


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

    Order is recover → pace → claim → send. Recovery first is load-bearing: a
    predecessor's stranded row is older work than anything pending, and
    resolving it may put a row back in the queue this same call then takes.

    **Pacing before the claim is a preference, not a guard, and saying so is
    the point.** Correctness there comes from the transaction — a paced call
    raises before committing, so the caller's rollback un-claims anything it
    took. Measured: moving the claim above the pacing check leaves the whole
    gate green. What the order buys is a cheaper and clearer deferral (no
    write, no attempts bump) rather than a different outcome, and a docstring
    that called it a guard would be dressing a preference as a mechanism.

    *transport* is injected and channel-neutral: it takes the payload and
    returns an external ref, or raises to signal a lost response. The floor is
    about send-state, not about Telegram, and a real client here would make
    every gate test a network test.
    """
    stranded = await recover_stranded(session, binding_id=binding_id)
    for outbox_id in stranded:
        await resolve_ambiguous(session, outbox_id=outbox_id)

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

    row = await claim_next(session, binding_id=binding_id)
    if row is None:
        return None

    try:
        ref = await transport(row)
    except Exception:  # noqa: BLE001 — a lost response is the ambiguous case
        await mark_ambiguous(session, outbox_id=row["id"])
        return {**row, "state": "ambiguous", "external_message_ref": None}

    await mark_sent(session, outbox_id=row["id"], external_message_ref=ref)
    return {**row, "state": "sent", "external_message_ref": ref}


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
        self.consecutive_failures = 0
        self._task = None

    async def tick(self) -> Optional[dict]:
        """One poll. Never raises: a tick that failed is an observable, not an
        exception the loop has to survive twice."""
        self.ticks += 1
        try:
            async with self._session_factory() as session:
                result = await deliver(
                    session,
                    binding_id=self._binding_id,
                    transport=self._transport,
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
        self.consecutive_failures = 0
        if result is not None and result["state"] == "sent":
            self.sent += 1
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
