"""The jobs service: claim, heartbeat, and lease-token CAS finalization.

A deliberately thin layer over the job machinery the schema defines: the
DATABASE is the authority. `machinery.py` defines the `jobs` table with the
kind registry, the pairing equivalence, and `uq_jobs_serialized_lease` — THE
serialization guard ("two leased jobs with one key are impossible by
constraint, not by claim-query discipline"). `04_functions.sql` defines the two
`SECURITY DEFINER` doors this module calls: `fn_claim_job` (the claim
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
  commit: the domain transaction (intent flip + counters + audit)
  and job finalization commit together. For a tenant job that session is the
  job's `UnitOfWork` (`SET LOCAL app.tenant_id = job.workspace_id`); a system
  job (`workspace_id IS NULL`) finalizes on a plain worker connection —
  `p_jobs` exposes system rows under any (or no) tenant GUC, by design
  (the machinery exception).

## Which guarantees are the database's

* **The serialization race loses correctly, not silently.** Two claimers that
  pass the claim query's ``NOT EXISTS`` before either commits update two
  different `ready` rows sharing a key; the second hits
  `uq_jobs_serialized_lease` and gets `unique_violation` — it is REFUSED, not
  told it won (the failure shape, prevented here by an index rather than
  a trigger). :func:`claim_job` rolls back and retries, and the retry is
  naturally clean: the winner's committed lease now trips the ``NOT
  EXISTS``, so the key is excluded without any bookkeeping (the retry
  excludes that serialization_key — the exclusion is the claim
  query's own predicate; the loop holds no state and is no second authority).
* **A stale owner cannot finalize.** Re-claiming after lease expiry mints a
  fresh `lease_token`, so the previous owner's CAS matches zero rows — the
  WHERE fails on the token, which is why this rowcount CAN discriminate. A
  predicate miss reports `rows=0` here; a self-transition no-op (the
  shape) reports `rows=1`.
* **Expiry recovery is the reaper's, not ours.** `fn_reaper_sweep`'s first leg
  re-readies expired leases (liveness-priority, budget-limited). This module
  does not duplicate it; the clock revives its own singletons' leases (`fn_clock_tick`),
  the reaper's among them. `fn_reaper_sweep`'s last leg ends a
  `ready` job past its `deadline_at` when a sweep re-mints its kind (`fn_reaper_sweep` deadline leg):
  `budget_exhausted` below is only ever asked after a run has
  failed, so without that leg a job nothing claims would never reach its
  deadline. A deferral carries the deadline with it (`reschedule_job`).
* **`jobs` has NO transition guard, and this contract is written against
  that fact** (the gate pins the absence as a tripwire). What makes
  that safe is the MECHANISM on each concurrency-bearing edge, not any
  classification of the table: `ready → leased` is guarded by a
  partial UNIQUE INDEX — writer-independent, so no trigger needs to be the
  authority on that edge — and `leased → terminal` by the token+state CAS,
  where only a WRONG caller (a stale owner) can be misled, about its own
  staleness: a far weaker failure than a correct caller misled about its
  own success (the shape). This argument is PER-EDGE and does not
  extend to a table lacking both mechanisms — `channel_outbox` has
  neither, and must build or justify its own. (The bookkeeping rule —
  never the authority on whether an external effect happened — is
  containment of one proposition, not the carrier of this conclusion.)
  No guard is added as defense in depth.

Every operational number (lease duration, heartbeat interval, per-workspace
lane cap) arrives as a parameter — callers read config and pass values (the
door rule, applied one layer up).
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

    Zero rows = fenced, the worker aborts. Raised so the caller's
    whole domain transaction aborts with it — a fenced worker must not commit
    any of the work the lease was supposed to authorize.
    """


def _is_serialization_race(exc: BaseException) -> bool:
    return any(
        getattr(c, "constraint_name", None) == _SERIALIZATION_GUARD
        for c in driver_candidates(exc)
    )


#: What an executor returns when it finalized (or rescheduled) its OWN job
#: inside its transaction — the loop must not finalize again (the
#: publish pipeline and a paced sender do this; a second finalize by the
#: loop would be fenced and counted as an error on every such run).
SELF_FINALIZED = "self-finalized"

#: The retryable-failure budget per lane: attempts and the
#: deadline from mint (interactive: 3 attempts, +10 min; bulk: 5 attempts,
#: +6 h for a job with no slot). Both bound `_run_job`'s failure path.
LANE_BUDGETS: dict[str, tuple[int, int]] = {
    "interactive": (3, 10 * 60),
    "bulk": (5, 6 * 3600),
}
#: The serialization key of a binding's sender job: one live sender per
#: binding, and `work_loop` parses the binding id back off it.
SENDER_KEY_PREFIX = "tg:"
#: `enqueue(deadline_seconds=NO_DEADLINE)`: the job carries no deadline (`deadline_at`
#: NULL) — for a kind whose ceiling is its own, like the publish pipeline, whose
#: slot may be a day away (deadline = slot end). Attempts still bound it.
NO_DEADLINE = -1
#: The backoff ladder per lane (seconds), ± 20 % jitter applied by `backoff_seconds`.
BACKOFF_SECONDS: dict[str, tuple[int, ...]] = {
    "interactive": (10, 30, 60),
    "bulk": (60, 300, 900, 3600),
}


def backoff_seconds(lane: str, attempts: int, *, jitter=None) -> float:
    """Seconds until the next attempt after *attempts* have been consumed:
    the lane's rung (the last rung repeats) with ± 20 % jitter so a burst of
    failures does not re-arrive as a burst. *jitter* is a callable returning a
    float in [0, 1) — `random.random` by default, injectable for tests."""
    import random

    rungs = BACKOFF_SECONDS.get(lane) or BACKOFF_SECONDS["bulk"]
    rung = rungs[min(max(attempts, 1) - 1, len(rungs) - 1)]
    draw = (jitter or random.random)()
    return rung * (0.8 + 0.4 * draw)


def budget_exhausted(job: dict, *, now) -> bool:
    """The retry ceiling: the attempts consumed reach `max_attempts`, or
    `deadline_at` has passed. A job with no deadline is bound by attempts alone."""
    attempts = int(job.get("attempts") or 0)
    max_attempts = int(job.get("max_attempts") or 0)
    if max_attempts and attempts >= max_attempts:
        return True
    deadline = job.get("deadline_at")
    return bool(deadline is not None and now >= deadline)


async def claim_job(
    conn,
    *,
    lane: str,
    worker: str,
    lease_seconds: float,
    ws_lane_cap: int,
    race_retries: int = 3,
    capability=None,
) -> Optional[dict[str, Any]]:
    """Claim one runnable job on *lane*, or return None.

    Calls the `fn_claim_job` door and COMMITS the claim. A lost serialization
    race is rolled back and retried up to *race_retries* times; the loser
    never sees an exception and never a phantom win — it gets the next
    runnable job or None. Any other error is rolled back (the caller must not
    inherit a connection stuck in a failed transaction) and re-raised.
    """
    from src.services.target.instagram_sources import is_activated

    activated = is_activated(capability)
    for _ in range(race_retries):
        try:
            result = await conn.execute(
                text(
                    "SELECT * FROM fn_claim_job("
                    ":lane, :worker, make_interval(secs => :lease), :cap"
                    + (", CAST(:activated AS boolean))" if activated else ")")
                ),
                {
                    "lane": lane,
                    "worker": worker,
                    "lease": lease_seconds,
                    "cap": ws_lane_cap,
                    **({"activated": True} if activated else {}),
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
    """The finalization CAS, plus the state predicate that keeping the
    token requires — inside the CALLER's transaction, which is the point: the
    domain writes and this finalization commit together or not at all.

    A bare CAS is ``WHERE id = :id AND lease_token = :token``;
    terminal rows deliberately KEEP their token as the record of who
    finalized them, so without ``AND state = 'leased'`` a double-finalize (a
    lost commit ack, a client retry) would match the terminal row and report
    ``rows=1`` — the phantom-win shape reappearing one table over. With
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
    """THE lease-liveness predicate — the one SQL
    site, shared by :func:`assert_lease` and the permit rail. State AND
    expiry, not merely id + token (which alone passes a lease that has
    expired without reassignment), and ``FOR SHARE`` holds the row against a
    concurrent finalization for the rest of the transaction. Fresh database
    wall time rejects an owner that waited past expiry on a domain lock, even
    when its transaction began before expiry — the check and
    the writes that follow it are one indivisible decision.

    One home on purpose: this predicate is easy to get wrong,
    and each copy gets pinned by a different gate, so a strengthening lands
    in one while the other's suite stays green.
    """
    result = await executor.execute(
        text(
            "SELECT 1 FROM jobs"
            " WHERE id = :job AND lease_token = :token"
            "   AND state = 'leased' AND locked_until > clock_timestamp()"
            " FOR SHARE"
        ),
        {"job": str(job_id), "token": str(lease_token)},
    )
    return result.first() is not None


def _fenced(job_id) -> JobFenced:
    return JobFenced(f"job {job_id}: lease token no longer owns the job; aborting")


async def assert_lease(session, job_id, lease_token) -> None:
    """The re-CAS for the domain transactions BETWEEN permits: the
    worker records the outcome in the same domain transaction (which
    also re-CASes the lease token — a fenced worker cannot record outcomes
    either). Raises :class:`JobFenced` so the caller's transaction aborts
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
    # (`send_email` and eight others) must carry a NULL workspace, and a tenant
    # kind must not.
    workspace_id: Optional[str],
    serialization_key: str,
    payload: dict,
    lane: str = "bulk",
    max_attempts: Optional[int] = None,
    deadline_seconds: Optional[int] = None,
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
    # The lane's budget unless the caller names its own. The deadline
    # is written at mint so `_run_job` can read it back without knowing the
    # lane's table (a job re-minted by a sweep gets a fresh one); a negative
    # `deadline_seconds` (`NO_DEADLINE`) writes NULL — no deadline at all.
    lane_attempts, lane_deadline = LANE_BUDGETS.get(lane, LANE_BUDGETS["bulk"])
    row = (
        await session.execute(
            text(
                "INSERT INTO jobs (kind, workspace_id, lane, serialization_key,"
                " run_at, max_attempts, deadline_at, payload)"
                " SELECT CAST(:kind AS text), CAST(:ws AS uuid), CAST(:lane AS text),"
                "        CAST(:key AS text), now(), CAST(:attempts AS int),"
                "        CASE WHEN CAST(:deadline AS int) < 0 THEN NULL"
                "             ELSE now() + make_interval(secs => CAST(:deadline AS int)) END,"
                f"        CAST(:p AS jsonb){guard}"
                " RETURNING id"
            ),
            {
                "kind": kind,
                "ws": workspace_id,
                "lane": lane,
                "key": serialization_key,
                "attempts": lane_attempts if max_attempts is None else max_attempts,
                "deadline": (
                    lane_deadline if deadline_seconds is None else deadline_seconds
                ),
                "p": json.dumps(payload),
            },
        )
    ).first()
    return str(row[0]) if row else None


async def mint_publish_job(
    session,
    *,
    workspace_id: str,
    intent_id: str,
    provider_account_ref: str,
    dry_run: bool,
) -> None:
    """The `publish_pipeline` job for a story entering the ladder: the one mint
    for a person's `approve`, the serve step's automatic approval,
    `resolve_review` and a give-up that leaves a transit copy
    (`command_executors._give_up`).
    """
    await enqueue(
        session,
        kind="publish_pipeline",
        workspace_id=workspace_id,
        serialization_key=f"ig:{provider_account_ref}",
        # The pipeline's ceiling is its own (deadline = slot end; the
        # slot may be a day away) — the loop's deadline would end a deferred
        # publish on its first escaped error. Attempts still bound it.
        deadline_seconds=NO_DEADLINE,
        # The dry-run decision travels WITH the job: what the approver was told
        # is what the run does, whatever the flag says by the time it runs.
        payload={"v": 1, "intent_id": str(intent_id), "dry_run": bool(dry_run)},
    )


async def reschedule_job(
    session, job_id, lease_token, *, run_at, restore_attempt: bool
) -> None:
    """`leased → ready` at a chosen `run_at` — the deferral edge (the
    worker reschedules the job to the account's next slot) and the
    retryable-failure backoff, in the CALLER's transaction (a deferral's audit
    row and this reschedule commit together or not at all).

    Mirrors `fn_reaper_sweep`'s re-ready shape exactly (state, locked_by,
    lease_token, locked_until all cleared) so a rescheduled job is
    indistinguishable from a reaped one to every claimer. CAS'd on the lease
    like finalization — zero rows raises :class:`JobFenced`.

    *restore_attempt* returns the attempt the claim consumed: a DEFERRAL is
    normal operation, not a failure, and the attempts budget is the
    retryable-failure budget — the approval-TTL reaper, not attempts, is
    the bound on endless deferral. A retryable FAILURE keeps its attempt.

    A deferral also carries the job's deadline: `deadline_at` moves by as much
    as `run_at` does (both SET expressions read the row as it was), so a park,
    a pacing wait or a budget deferral keeps the job's slack: both the
    reaper's deadline leg (`fn_reaper_sweep` deadline leg) and `budget_exhausted` measure from the
    moved deadline. A retryable failure keeps its deadline, as it keeps its
    attempt.
    """
    result = await session.execute(
        text(
            "UPDATE jobs SET state = 'ready', locked_by = NULL,"
            "  lease_token = NULL, locked_until = NULL, run_at = :run_at,"
            "  deadline_at = CASE WHEN :restore"
            "                THEN deadline_at + (CAST(:run_at AS timestamptz) - run_at)"
            "                ELSE deadline_at END,"
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
    spellings works here: `asyncio.timeout` is 3.11+ (the repo floor is 3.10,
    where it raises `AttributeError` and the `lease-heartbeat` task dies), and
    `wait_for(event.wait(),...)` leaks an unawaited coroutine under cancel
    races. Owning the waiter task and always reaping it is leak-free on
    every supported version.
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
    """One independent asyncio task per worker process: extends
    `locked_until` for every registered lease each interval, in a single
    `fn_extend_leases` call, on its OWN connection — the beat task is never
    in a pipeline's await chain, so a provider wait cannot starve it.

    Composition-root notes — `src/worker.py` is that root, and it starts this
    as `lease-heartbeat` alongside the clock and the lanes:

    * **Escalation and liveness registration.** A beat that fails every
      interval logs and keeps trying; `consecutive_failures` is the
      "presumed dead after two missed beats" observable. It is
      REPORTED, never a gate: `worker_health` says so in its own header —
      a database blip would raise it on a worker that is going to recover,
      and 503-ing would spend the restart budget on an outage no restart
      mends. Alerting on it is unwired.
    * **Pool shape.** *connect* against the shared 10-slot engine means each
      beat is a pooled checkout — fine — but FIFO rotation plus
      `pool_recycle` makes an otherwise-idle worker pay a real reconnect on
      up to every beat at the configured intervals. `pool_use_lifo=True`, or a
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
        # A lease that its task finished (and unregistered) while the beat
        # was in flight is not a lost lease: measure against what is STILL
        # registered, or K tasks finishing jobs would read as fencing noise.
        still = [t for t in tokens if t in self._tokens]
        if extended < len(still):
            self.short_beats += 1
            if self._on_short_count is not None:
                self._on_short_count(len(still), extended)
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
