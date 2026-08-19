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
        raise JobFenced(f"job {job_id}: lease token no longer owns the job; aborting")


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
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._interval)
                return
            except asyncio.TimeoutError:
                pass
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
