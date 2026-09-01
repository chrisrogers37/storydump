"""L.2 — the jobs service: claim, heartbeat, and lease-token CAS finalization
(#859, `04` §L.2).

A deliberately thin layer over the merged machinery, on the L.1 doctrine: the
DATABASE is the authority. Migration `056` shipped the `jobs` table with the
kind registry, the pairing equivalence, and `uq_jobs_serialized_lease` — THE
serialization guard ("two leased jobs with one key are impossible by
constraint, not by claim-query discipline"). Migration `059` shipped the two
`SECURITY DEFINER` doors this module calls: `fn_claim_job` (the `02` §5 claim
query, `FOR UPDATE SKIP LOCKED`, `LIMIT 1`, token minted and `attempts`
incremented at claim time) and `fn_extend_leases` (the heartbeat UPDATE,
returning how many of the caller's tokens still held a live lease). This
module re-implements none of that; it wraps the calls, manages the one race
the claim query cannot avoid, and gives the CAS its typed refusal.

## Connection contract, stated because the seam is real

The doors execute as their `svc_claim` owner and are granted to `svc_worker`
only — so claiming and heartbeating happen on a plain `svc_worker`
connection, BEFORE any tenant is known (the claim is what tells you the
workspace). The `UnitOfWork` deliberately has no tenant-less mode and none is
needed here:

* :func:`claim_job` and :func:`extend_leases` take a plain ``AsyncConnection``
  and OWN their transaction — a lease that is not committed does not exist to
  the expiry sweep or any other worker. Both are single self-contained door
  calls, so an ``isolation_level="AUTOCOMMIT"`` connection serves them with
  four fewer round trips per call; the commit here is then a no-op, and the
  choice belongs to the composition root.
* :func:`finalize_job` takes the CALLER's session and deliberately does NOT
  commit: `02` §5 — "the domain transaction (intent flip + counters + audit)
  and job finalization commit together". For a tenant job that session is the
  job's `UnitOfWork` (`SET LOCAL app.tenant_id = job.workspace_id`); a system
  job (`workspace_id IS NULL`) finalizes on a plain worker connection —
  `p_jobs` exposes system rows under any (or no) tenant GUC, by design
  (`02` §7 machinery exception).

## Which guarantees are the database's, measured rather than assumed

* **The serialization race loses correctly, not silently.** Two claimers that
  pass the claim query's ``NOT EXISTS`` before either commits update two
  different `ready` rows sharing a key; the second hits
  `uq_jobs_serialized_lease` and gets `unique_violation` — it is REFUSED, not
  told it won (the #883 failure shape, prevented here by an index rather than
  a trigger). :func:`claim_job` rolls back and retries, and the retry is
  naturally clean: the winner's committed lease now trips the ``NOT
  EXISTS``, so the key is excluded without any bookkeeping ("`02` §5: retry
  the claim excluding that serialization_key" — the exclusion is the claim
  query's own predicate; the loop holds no state and is no second authority).
* **A stale owner cannot finalize.** Re-claiming after lease expiry mints a
  fresh `lease_token`, so the previous owner's CAS matches zero rows — the
  WHERE fails on the token, which is why this rowcount CAN discriminate where
  #883's could not: that was a self-transition no-op reported as `rows=1`;
  this is a predicate miss reported as `rows=0`.
* **Expiry recovery is the reaper's, not ours.** `fn_reaper_sweep`'s first leg
  re-readies expired leases (liveness-priority, budget-limited). This module
  does not duplicate it.
* **`jobs` has NO transition guard, and this contract is written against
  that measured fact** (#883 follow-on; the gate pins the absence as a
  tripwire). What makes that safe is the MECHANISM on each
  concurrency-bearing edge, not any classification of the table (#887
  ruling): `ready → leased` is guarded by a partial UNIQUE INDEX —
  writer-independent, so #883's the-trigger-is-the-authority argument does
  not transfer at all — and `leased → terminal` by the token+state CAS,
  where the argument transfers INVERTED: only a WRONG caller (a stale
  owner) can be misled, about its own staleness, which is a far weaker
  failure than #883's correct caller misled about its own success. This
  argument is PER-EDGE and does not extend to a table lacking both
  mechanisms — `channel_outbox` (L.4) has neither, and must build or
  justify its own. (`02` §5's "bookkeeping, never the authority on whether
  an external effect happened" is containment of one proposition, not the
  carrier of this conclusion.) Defense-in-depth remains #887's question.

Every `05` number (lease duration, heartbeat interval, per-workspace lane cap)
arrives as a parameter — callers read config and pass values (`02` §7's door
rule, applied one layer up).
"""

from __future__ import annotations

import json

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.exceptions.base import StorydumpError
from src.services.target._dbapi import driver_candidates

logger = logging.getLogger(__name__)

#: The serialization guard's index name, matched against the driver's
#: constraint report so an unrelated unique violation is never mistaken for
#: the race this module knows how to retry — a broad catch would render a
#: future real error as "queue empty"; the name-match re-raises it loudly.
_SERIALIZATION_GUARD = "uq_jobs_serialized_lease"


class JobFenced(StorydumpError):
    """A finalization CAS matched zero rows: the lease token is stale.

    `02` §5: "zero rows = fenced, the worker aborts." Raised so the caller's
    whole domain transaction aborts with it — a fenced worker must not commit
    any of the work the lease was supposed to authorize.
    """


def _is_serialization_race(exc: BaseException) -> bool:
    return any(
        getattr(c, "constraint_name", None) == _SERIALIZATION_GUARD
        for c in driver_candidates(exc)
    )


async def claim_job(
    conn,
    *,
    lane: str,
    worker: str,
    lease_seconds: float,
    ws_lane_cap: int,
    race_retries: int = 3,
) -> Optional[dict[str, Any]]:
    """Claim one runnable job on *lane*, or return None.

    Calls the `fn_claim_job` door and COMMITS the claim. A lost serialization
    race is rolled back and retried up to *race_retries* times; the loser
    never sees an exception and never a phantom win — it gets the next
    runnable job or None. Any other error is rolled back (the caller must not
    inherit a connection stuck in a failed transaction) and re-raised.
    """
    for _ in range(race_retries):
        try:
            result = await conn.execute(
                text(
                    "SELECT * FROM fn_claim_job("
                    ":lane, :worker, make_interval(secs => :lease), :cap)"
                ),
                {
                    "lane": lane,
                    "worker": worker,
                    "lease": lease_seconds,
                    "cap": ws_lane_cap,
                },
            )
            row = result.mappings().first()
            await conn.commit()
            return dict(row) if row is not None else None
        except DBAPIError as exc:
            await conn.rollback()
            if not _is_serialization_race(exc):
                raise
            logger.debug("claim on lane %s lost a serialization race; retrying", lane)
    return None


async def extend_leases(conn, tokens: list, lease_seconds: float) -> int:
    """Extend every live lease in *tokens*; commit; return how many were.

    A return smaller than ``len(tokens)`` is the fencing tripwire — the door
    deliberately reports only a count, and the CAS is the authority on which
    lease went stale.
    """
    result = await conn.execute(
        text(
            "SELECT fn_extend_leases("
            "CAST(:tokens AS uuid[]), make_interval(secs => :lease))"
        ),
        {"tokens": tokens, "lease": lease_seconds},
    )
    extended = int(result.scalar_one())
    await conn.commit()
    return extended


async def finalize_job(session, job_id, lease_token, terminal_state: str) -> None:
    """`02` §5's finalization CAS, plus the state predicate that keeping the
    token requires — inside the CALLER's transaction, which is the point: the
    domain writes and this finalization commit together or not at all.

    The plan's literal CAS is ``WHERE id = :id AND lease_token = :token``;
    terminal rows deliberately KEEP their token as the record of who
    finalized them, so without ``AND state = 'leased'`` a double-finalize (a
    lost commit ack, a client retry) would match the terminal row and report
    ``rows=1`` — the #883 phantom-win shape reappearing one table over. With
    it, zero rows means stale token OR already-terminal, and both mean the
    same thing to the caller: abort, you do not own this job.
    """
    result = await session.execute(
        text(
            "UPDATE jobs SET state = :terminal"
            " WHERE id = :id AND lease_token = :token AND state = 'leased'"
        ),
        {"terminal": terminal_state, "id": job_id, "token": lease_token},
    )
    if result.rowcount == 0:
        raise _fenced(job_id)


async def lease_is_live(executor, job_id, lease_token) -> bool:
    """THE lease-liveness predicate (`02` §6 step 1/step 3) — the one SQL
    site, shared by :func:`assert_lease` and the permit rail. State AND
    expiry, not merely id + token (the pass-2 form passed a lease that had
    expired without reassignment), and ``FOR SHARE`` holds the row against a
    concurrent finalization for the rest of the transaction — the check and
    the writes that follow it are one indivisible decision.

    One home on purpose: this predicate has been written wrong once already,
    and each copy gets pinned by a different gate, so a strengthening lands
    in one while the other's suite stays green.
    """
    result = await executor.execute(
        text(
            "SELECT 1 FROM jobs"
            " WHERE id = :job AND lease_token = :token"
            "   AND state = 'leased' AND locked_until > now()"
            " FOR SHARE"
        ),
        {"job": str(job_id), "token": str(lease_token)},
    )
    return result.first() is not None


def _fenced(job_id) -> JobFenced:
    return JobFenced(f"job {job_id}: lease token no longer owns the job; aborting")


async def assert_lease(session, job_id, lease_token) -> None:
    """`02` §6 step 3's re-CAS for the domain transactions BETWEEN permits:
    "the worker records the outcome ... in the same domain transaction (which
    also re-CASes the lease token — a fenced worker cannot record outcomes
    either)". Raises :class:`JobFenced` so the caller's transaction aborts
    with it — a stale owner resuming mid-ladder cannot write checkpoints over
    the new owner's progress, and cannot reach provider work behind it.
    """
    if not await lease_is_live(session, job_id, lease_token):
        raise _fenced(job_id)


async def enqueue(
    session,
    *,
    kind: str,
    # Optional because `ck_jobs_system_kinds` is a BICONDITIONAL: a system kind
    # (`send_email` and six others) must carry a NULL workspace, and a tenant
    # kind must not. The column has always accepted NULL and the annotation
    # simply predated the first caller that needed one — every earlier caller
    # enqueues a tenant kind.
    workspace_id: Optional[str],
    serialization_key: str,
    payload: dict,
    lane: str = "bulk",
    max_attempts: int = 5,
    unless_pending: bool = False,
) -> Optional[str]:
    """Insert one `ready` job in the caller's transaction; returns its id.

    *workspace_id* is None for a system kind and a real id for a tenant kind;
    the database enforces which is which, so passing the wrong one fails loudly
    at the constraint rather than writing a row nothing can claim.

    The payload is built in Python and bound ONCE as jsonb — the tier's shape
    (`outbox.enqueue`, `media_sync`); building JSON inside the statement is
    what asyncpg cannot type. *unless_pending* declines to mint when a
    ready/leased job already holds *serialization_key* (the demand-sync case:
    a second row would only queue behind the first) and returns None. Every
    bind is cast explicitly because the guarded form is an INSERT … SELECT,
    whose parameters carry no target-column type.
    """
    guard = (
        " WHERE NOT EXISTS (SELECT 1 FROM jobs WHERE serialization_key = :key"
        "                     AND state IN ('ready', 'leased'))"
        if unless_pending
        else ""
    )
    row = (
        await session.execute(
            text(
                "INSERT INTO jobs (kind, workspace_id, lane, serialization_key,"
                " run_at, max_attempts, payload)"
                " SELECT CAST(:kind AS text), CAST(:ws AS uuid), CAST(:lane AS text),"
                "        CAST(:key AS text), now(), CAST(:attempts AS int),"
                f"        CAST(:p AS jsonb){guard}"
                " RETURNING id"
            ),
            {
                "kind": kind,
                "ws": workspace_id,
                "lane": lane,
                "key": serialization_key,
                "attempts": max_attempts,
                "p": json.dumps(payload),
            },
        )
    ).first()
    return str(row[0]) if row else None


async def reschedule_job(
    session, job_id, lease_token, *, run_at, restore_attempt: bool
) -> None:
    """`leased → ready` at a chosen `run_at` — the §4 deferral edge ("the
    worker reschedules the job to the account's next slot") and the R8
    retryable-failure backoff, in the CALLER's transaction (a deferral's audit
    row and this reschedule commit together or not at all).

    Mirrors `fn_reaper_sweep`'s re-ready shape exactly (state, locked_by,
    lease_token, locked_until all cleared) so a rescheduled job is
    indistinguishable from a reaped one to every claimer. CAS'd on the lease
    like finalization — zero rows raises :class:`JobFenced`.

    *restore_attempt* returns the attempt the claim consumed: a DEFERRAL is
    normal operation (`06`), not a failure, and the attempts budget is R8's
    retryable-failure budget — §4 names the approval-TTL reaper, not attempts,
    as the bound on endless deferral. A retryable FAILURE keeps its attempt.
    """
    result = await session.execute(
        text(
            "UPDATE jobs SET state = 'ready', locked_by = NULL,"
            "  lease_token = NULL, locked_until = NULL, run_at = :run_at,"
            "  attempts = CASE WHEN :restore THEN GREATEST(attempts - 1, 0)"
            "                  ELSE attempts END"
            " WHERE id = :job AND lease_token = :token AND state = 'leased'"
        ),
        {
            "run_at": run_at,
            "restore": restore_attempt,
            "job": str(job_id),
            "token": str(lease_token),
        },
    )
    if result.rowcount == 0:
        raise _fenced(job_id)


async def wait_or_stop(event, timeout_seconds: float) -> bool:
    """Wait for *event* up to *timeout_seconds*; True when it fired.

    The 3.10-safe form of the wait-with-deadline idiom. Neither of the obvious
    spellings works here: `asyncio.timeout` is 3.11+ (the repo floor is 3.10 —
    CI's supervision log was the discovery: `lease-heartbeat DIED
    (AttributeError...)`), and `wait_for(event.wait(), ...)` leaks an
    unawaited coroutine under cancel races (#958). Owning the waiter task and
    always reaping it is leak-free on every supported version.
    """
    waiter = asyncio.ensure_future(event.wait())
    done, _ = await asyncio.wait({waiter}, timeout=timeout_seconds)
    if waiter not in done:
        waiter.cancel()
        try:
            await waiter
        except asyncio.CancelledError:
            pass
        return False
    return True


class LeaseHeartbeat:
    """One independent asyncio task per worker process (`02` §5): extends
    `locked_until` for every registered lease each interval, in a single
    `fn_extend_leases` call, on its OWN connection — the beat task is never
    in a pipeline's await chain, so a provider wait cannot starve it.

    Composition-root notes, for whoever wires this (L.5/L.6) — deliberately
    NOT handled here:

    * **Escalation and liveness registration.** A beat that fails every
      interval logs and keeps trying; `consecutive_failures` is the
      "presumed dead after two missed beats" observable (`02` §5). Wiring it
      to `loops/heartbeat.record_heartbeat` and to alerting belongs with the
      runner — registering a loop nothing starts yet would false-alarm
      `/health`.
    * **Pool shape.** *connect* against the shared 10-slot engine means each
      beat is a pooled checkout — fine — but FIFO rotation plus
      `pool_recycle` makes an otherwise-idle worker pay a real reconnect on
      up to every beat at the `05` intervals. `pool_use_lifo=True`, or a
      dedicated one-slot engine for the beat, keeps it on one hot
      connection. A saturated pool (`pool_timeout`) surfaces as a failed
      beat here, not a hang.
    """

    def __init__(
        self,
        connect: Callable[[], Awaitable[Any]],
        *,
        interval_seconds: float,
        lease_seconds: float,
        on_short_count: Optional[Callable[[int, int], None]] = None,
    ):
        self._connect = connect
        self._interval = interval_seconds
        self._lease = lease_seconds
        self._on_short_count = on_short_count
        self._tokens: set = set()
        self._task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()
        self.beats = 0
        self.short_beats = 0
        self.consecutive_failures = 0

    def register(self, token) -> None:
        self._tokens.add(token)

    def unregister(self, token) -> None:
        self._tokens.discard(token)

    async def beat_once(self) -> Optional[int]:
        """One beat: extend everything currently registered.

        Public so the beat's whole effect is testable without wall-clock
        sleeps; :meth:`run` is this in a timer.
        """
        tokens = list(self._tokens)
        if not tokens:
            return None
        conn = await self._connect()
        try:
            extended = await extend_leases(conn, tokens, self._lease)
        finally:
            await conn.close()
        self.beats += 1
        self.consecutive_failures = 0
        if extended < len(tokens):
            self.short_beats += 1
            if self._on_short_count is not None:
                self._on_short_count(len(tokens), extended)
        return extended

    async def run(self) -> None:
        """Beat every interval until :meth:`stop`. A failed beat increments
        `consecutive_failures`, logs, and the loop continues — the task's
        whole purpose is surviving until the next beat."""
        while True:
            if await wait_or_stop(self._stopping, self._interval):
                return
            try:
                await self.beat_once()
            except Exception:  # noqa: BLE001 — the loop must outlive one beat
                self.consecutive_failures += 1
                logger.exception("lease heartbeat failed; continuing")

    def start(self) -> None:
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            await self._task
            self._task = None
