"""L.7 — scheduler-as-clock, dispatcher, and the recurring system jobs (#864, `04` §L.7).

Three things that are easy to conflate and must not be: the **clock** decides
*when*, the **dispatcher** decides *what runs*, and **system jobs** are the
recurring population riding on both. The clock does not execute work — `05`
puts it "inside an elected worker, no separate pool" — and the dispatcher is
`fn_clock_tick`, a `059` door that already exists. This module is the election,
the loop, and the executors.

## Does L.4's lease argument extend to recurring work? No, and the shape
## difference is exactly the one worth naming

On L.4 (#897) the outbox needed no guard of its own because
`uq_jobs_serialized_lease` admits one live sender per key and **holding the
lease is the proof the predecessor does not**. That argument is about who
*executes*. Recurring work has a second edge a one-shot delivery does not: it
must be **scheduled again**, and the lease says nothing about that.

Look at what `fn_clock_tick`'s recurring leg actually does:

    IF NOT EXISTS (SELECT 1 FROM jobs WHERE kind = k AND state IN ('ready','leased'))
    THEN INSERT INTO jobs (...) ...

That `NOT EXISTS` is a **snapshot read**, not a lock — the same shape as
`fn_claim_job`'s serialization check, which #890 established cannot serialize
two callers. And the index that rescues `fn_claim_job` does not rescue this
one: `uq_jobs_serialized_lease` is partial on ``state = 'leased'``, while the
double-insert happens at ``state = 'ready'``. **Two concurrent ticks would both
see no live row and both insert.** A clock that can double-fire is the same
defect class as an outbox that can double-send.

So the protection here is a *different* mechanism, and it is this module's
main job: **advisory-lock election**, which makes the concurrency impossible
rather than refusing a loser. :class:`ClockElection` is that, and the gate
proves it by removing it and watching the duplicate appear.

**`plan_slot` is the one recurring edge that also has the L.4-shaped guard**,
and the contrast is instructive. Its idempotency lives on the *effect* rather
than the job: `uq_intent_slot` on `(workspace_id, ig_account_id,
schedule_slot_at)` means a duplicate `plan_slot` execution mints no second
intent. That is belt to the election's braces, and it is why `04`'s gate names
it separately — the election protects the scheduling edge, key 1 protects the
minting edge, and neither covers the other.

## Clock skew: out of scope for L.7, by construction rather than by omission

This estate runs on an RTC-less Pi and has had real skew, so this is decided
rather than assumed. **Every scheduling decision reads the DATABASE clock, not
the host's.** `fn_clock_tick` takes no timestamp parameter: `now()`,
`GREATEST(now(), last_done + cadence)` and every `<= now()` predicate are
evaluated in Postgres. A host whose wall clock jumps backwards after a reboot
changes nothing about which accounts are due.

The loop's own pacing is monotonic too — :meth:`Clock.run` sleeps on
``asyncio.sleep``, which measures against ``loop.time()`` (a monotonic source),
so a wall-clock jump cannot make a tick fire early, late, or twice.

What remains is skew of the **database's** clock, which is a deployment
property no application code can detect from inside, and belongs with the other
real-environment verifications in S. Stated here so the next reader knows it
was considered and where it went, rather than finding silence.

## The bounds are the door's, and they arrive as parameters

`05`: tick every **15 s** with **≤ 500 inserts/tick** (`p_max`, a TOTAL across
all four job classes — the door's own comment is the one home for the priority
order), reaper every **60 s** at **500** (`p_lim`, likewise a total). Nothing
here hardcodes them.
"""

from __future__ import annotations

import logging

from typing import Optional

from sqlalchemy import text

from src.exceptions.base import StorydumpError

#: The advisory-lock key the clock elects on. A single fixed key, because there
#: is exactly one clock for the deployment — `05`: "clock runs inside an
#: elected worker". Chosen in the application range and never derived from a
#: name, so two spellings of the same intent cannot elect two clocks.
CLOCK_ELECTION_KEY = 0x5701_C10C


logger = logging.getLogger(__name__)


class ClockNotElected(StorydumpError):
    """This process is not the clock and must not tick.

    Not an error condition: on a multi-replica deployment every replica but
    one raises this every interval, which is the mechanism working. Callers
    log at debug and come back.
    """


class ClockElection:
    """Session-scoped advisory-lock election for the single clock.

    ``pg_try_advisory_lock`` rather than ``pg_advisory_lock``: a loser must
    return immediately and try again next interval, never queue. Queuing would
    turn every non-clock replica into a connection held open forever waiting
    for a lock it does not want.

    **Session-scoped, deliberately, and this is the "killing the clock mid-tick
    loses nothing" half.** A session lock dies with its connection — including
    on SIGKILL, where no `finally` runs — so a clock that is killed releases
    the election within one TCP timeout and a successor takes over. A
    transaction-scoped lock would release at every commit and let a second
    clock in mid-loop; a table-row election would need its own lease, expiry
    and reaper, which is the machinery this avoids.

    The connection is the caller's and is held for the elected process's
    lifetime. That is the one long-lived connection the design permits here,
    and it is why the clock rides inside an elected worker rather than owning a
    pool (`05`).
    """

    def __init__(self, conn, *, key: int = CLOCK_ELECTION_KEY):
        self._conn = conn
        self._key = key
        self.held = False

    async def try_acquire(self) -> bool:
        got = (
            await self._conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": self._key}
            )
        ).scalar()
        self.held = bool(got)
        return self.held

    async def release(self) -> None:
        """Give up the election. Idempotent.

        Never required for correctness — the session dying does this — but a
        clean shutdown that hands over in milliseconds rather than in a TCP
        timeout is worth the call.
        """
        if not self.held:
            return
        await self._conn.execute(
            text("SELECT pg_advisory_unlock(:k)"), {"k": self._key}
        )
        self.held = False


async def tick(
    session,
    *,
    max_inserts: int,
    refresh_cadence_seconds: int,
    recurring: dict,
) -> dict:
    """One dispatcher pass, through the `059` door. Runs in the caller's tx.

    The door is the authority on what a tick does and on how its insert budget
    is shared between the four classes — this wrapper adds no policy, exactly
    as `intent_ledger.transition` adds none to `trg_intent_guard`. It exists so
    call sites read as intent and so the `05` numbers arrive as parameters.

    *recurring* is the `05` seam the door documents: ``{"v": 1, "<kind>":
    seconds, ...}``.
    """
    row = (
        await session.execute(
            text(
                "SELECT o_slot_jobs, o_refresh_jobs, o_sync_jobs, o_recurring_jobs"
                " FROM fn_clock_tick(:m, make_interval(secs => :r), CAST(:g AS jsonb))"
            ),
            {
                "m": max_inserts,
                "r": refresh_cadence_seconds,
                "g": _json(recurring),
            },
        )
    ).first()
    return {
        "slot_jobs": row[0],
        "refresh_jobs": row[1],
        "sync_jobs": row[2],
        "recurring_jobs": row[3],
    }


def _json(value) -> str:
    import json

    return json.dumps(value)


async def execute_plan_slot(
    session,
    *,
    workspace_id: str,
    ig_account_id: str,
    slot_at,
    provider_account_ref: str,
    approval_mode: str,
) -> Optional[str]:
    """The `plan_slot` executor: mint the intent for one slot, or nothing.

    Returns the intent id, or **None** when the slot already had one or no
    media was available. Both Nones are ordinary outcomes, not failures.

    **Idempotent by key 1, not by checking first.** The insert carries
    ``ON CONFLICT … DO NOTHING`` against `uq_intent_slot`, so a duplicate
    `plan_slot` job — which the clock's own `NOT EXISTS` cannot rule out, and
    which a lease cannot either — mints no second intent. A read-then-write
    would be the #883 shape: correct in a test, wrong under two workers.

    Note the conflict target is spelled as **columns**, not
    ``ON CONFLICT ON CONSTRAINT uq_intent_slot`` as `02` §5's prose has it.
    Key 1 ships as a bare ``CREATE UNIQUE INDEX``, and ``ON CONSTRAINT``
    resolves only names in ``pg_constraint``; the prose form does not run. The
    inference form is equivalent and is what the gate exercises.

    Media selection is deliberately the simple eligible-and-not-already-live
    rule. `06` §3's case-mix weighting is a product rule that wants its own
    increment; this is the mechanism it will plug into, and the empty case is
    handled here rather than deferred because "no media" is the outcome `02`
    §5 names for it.
    """
    media = (
        await session.execute(
            text(
                "SELECT m.id FROM media_items m"
                " WHERE m.workspace_id = :ws"
                "   AND NOT EXISTS (SELECT 1 FROM post_intents p"
                "                   WHERE p.workspace_id = m.workspace_id"
                "                     AND p.media_item_id = m.id"
                "                     AND p.ig_account_id = :acct"
                "                     AND p.state NOT IN ('posted','skipped','rejected',"
                "                                         'expired','failed','cancelled'))"
                " ORDER BY m.created_at LIMIT 1"
            ),
            {"ws": workspace_id, "acct": ig_account_id},
        )
    ).first()
    if media is None:
        return None

    row = (
        await session.execute(
            text(
                "INSERT INTO post_intents (workspace_id, ig_account_id, media_item_id,"
                " provider_account_ref, approval_mode, schedule_slot_at, state)"
                " VALUES (:ws, :acct, :media, :ref, :mode, :slot, 'scheduled')"
                " ON CONFLICT (workspace_id, ig_account_id, schedule_slot_at)"
                " DO NOTHING RETURNING id"
            ),
            {
                "ws": workspace_id,
                "acct": ig_account_id,
                "media": media[0],
                "ref": provider_account_ref,
                "mode": approval_mode,
                "slot": slot_at,
            },
        )
    ).first()
    return None if row is None else str(row[0])


async def execute_reap_expired(
    session, *, limit: int, approval_ttl_seconds: int, approved_ttl_seconds: int
) -> int:
    """The `reap_expired` executor. Returns rows touched across all legs.

    A thin call on `fn_reaper_sweep`, whose `p_lim` is a **total** the door
    shares between its legs by construction — this must not re-split it, which
    is the R6 defect the door's own comment records (independent per-leg limits
    made the sweep 7× its bound).
    """
    return (
        await session.execute(
            text(
                "SELECT fn_reaper_sweep(:lim, make_interval(secs => :a),"
                " make_interval(secs => :b))"
            ),
            {"lim": limit, "a": approval_ttl_seconds, "b": approved_ttl_seconds},
        )
    ).scalar()


async def execute_reap_transit_assets(
    session, *, lister, deleter, older_than_seconds: int
) -> int:
    """The `reap_transit_assets` executor (FC-3.6). Returns assets reaped.

    **Discovery is ONE global age-filtered provider listing per sweep, never
    per-workspace listings** — `04` says so, and the reason is the shape of the
    cost: per-workspace discovery is a provider call per tenant per sweep,
    which grows with the customer count for a job whose work does not.

    *lister* and *deleter* are injected for the same reason L.4's transport is:
    the bounded-sweep property is about this code, and a real provider client
    here would make the gate a network test. An asset the deleter refuses is
    left for the next sweep rather than retried in-loop — the hard-TTL sweep is
    the backstop `02` §6 already relies on for exactly this.
    """
    assets = await lister(older_than_seconds=older_than_seconds)
    reaped = 0
    for asset in assets:
        try:
            await deleter(asset)
        except Exception:  # noqa: BLE001 — the next sweep is the retry
            continue
        reaped += 1
    return reaped


class Clock:
    """The elected loop. Ticks on its own timer; executes nothing itself.

    Observables rather than logs, so a supervisor can act: `ticks`, `elected`
    (whether this process currently holds the election), `inserts` (cumulative
    across classes), and `consecutive_failures`, which is what a liveness check
    reads. Losing the election is **not** a failure and deliberately does not
    move that counter — on a three-replica deployment two processes lose it
    every interval, and a supervisor restarting them for it would be the
    defect. That is the same rule L.4's poller applies to a paced tick.

    Escalation, liveness registration and the connection's lifetime are the
    composition root's, exactly as they are for `LeaseHeartbeat` and
    `OutboxPoller`.
    """

    def __init__(
        self,
        election_conn,
        session_factory,
        *,
        interval_seconds: float,
        max_inserts: int,
        refresh_cadence_seconds: int,
        recurring: dict,
    ):
        self._election = ClockElection(election_conn)
        self._session_factory = session_factory
        self._interval = interval_seconds
        self._max_inserts = max_inserts
        self._refresh_cadence_seconds = refresh_cadence_seconds
        self._recurring = recurring
        self.ticks = 0
        self.inserts = 0
        self.elected = False
        self.lost_elections = 0
        self.consecutive_failures = 0
        self._task = None

    async def tick_once(self) -> Optional[dict]:
        """Elect, then tick. Returns None when this process is not the clock."""
        if not await self._election.try_acquire():
            self.elected = False
            self.lost_elections += 1
            self.consecutive_failures = 0  # losing is the mechanism working
            return None
        self.elected = True
        self.ticks += 1
        try:
            async with self._session_factory() as session:
                counts = await tick(
                    session,
                    max_inserts=self._max_inserts,
                    refresh_cadence_seconds=self._refresh_cadence_seconds,
                    recurring=self._recurring,
                )
                await session.commit()
        except Exception as exc:  # noqa: BLE001 — swallowed on purpose; see below
            self.consecutive_failures += 1
            # The SWALLOW stays and so does the counter: a raising clock is
            # worse than a failing one, because the loop dies and the election
            # is never released. What changes is that the exception is no
            # longer the only artifact naming WHAT failed, and then discarded.
            #
            # "The counter IS the report" was true of liveness and false of
            # diagnosis. A count says the clock is failing; it cannot say which
            # leg or which kind, and `tick` runs five legs in one transaction
            # so any one of them aborts all five identically. #1074: a job kind
            # missing from `ck_jobs_kind` stopped the clock estate-wide and the
            # only surviving symptom, five layers away, was a worker gate
            # failing `assert bulk.processed >= 1` on `0 >= 1`.
            #
            # `logger.exception` rather than a formatted message: the failing
            # row is in the DBAPI error's own text, so re-rendering it by hand
            # is how the offending kind goes missing again. Logged on EVERY
            # failure, not the first — a clock that recovers and re-breaks is
            # two incidents, and a once-only log makes the second invisible.
            # THE RECURRING KINDS ARE NAMED EXPLICITLY, and that is not
            # belt-and-braces — it is the whole fix. PostgreSQL's CHECK
            # violation names the CONSTRAINT ("ck_jobs_kind") and puts the
            # failing VALUE in a separate DETAIL line that SQLAlchemy's str()
            # does not carry. Logging the traceback alone therefore reports
            # that a job kind was refused without saying WHICH, which is the
            # same diagnosis-shaped hole one layer in. Measured: the first
            # version of this fix printed the constraint and not the kind, and
            # the test below failed on exactly that.
            #
            # `_recurring` is the clock's own configuration, so it needs no
            # driver introspection and cannot go stale against a wrapped
            # exception. It names the candidate set; the constraint name in the
            # traceback says which column refused. Together those are the
            # answer, in one line, at the moment it happens.
            detail = getattr(getattr(exc, "orig", None), "detail", None)
            logger.exception(
                "clock tick failed (%d consecutive) — NO JOBS WERE MINTED."
                " All five legs share one transaction, so this aborted every"
                " leg, not only the one that raised. Recurring kinds this"
                " clock mints: %s.%s",
                self.consecutive_failures,
                ", ".join(sorted(k for k in self._recurring if k != "v")) or "(none)",
                f" DB detail: {detail}" if detail else "",
            )
            return None
        self.consecutive_failures = 0
        self.inserts += sum(counts.values())
        return counts

    async def start(self) -> None:
        import asyncio

        async def _loop():
            while True:
                await self.tick_once()
                # Monotonic by construction: asyncio.sleep measures against
                # loop.time(), so a wall-clock jump cannot make a tick fire
                # early, late, or twice. See the module docstring on skew.
                await asyncio.sleep(self._interval)

        self._task = asyncio.create_task(_loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except BaseException:  # noqa: BLE001 — cancellation is the happy path
                pass
            self._task = None
        await self._election.release()
