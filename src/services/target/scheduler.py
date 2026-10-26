"""Scheduler-as-clock, dispatcher, and the recurring system jobs.

Three things that are easy to conflate and must not be: the **clock** decides
*when*, the **dispatcher** decides *what runs*, and **system jobs** are the
recurring population riding on both. The clock does not execute work — it runs
"inside an elected worker, no separate pool" — and the dispatcher is
`fn_clock_tick`, a `04_functions.sql` door. This module is the election, the loop, and the
executors.

## Does the outbox's lease argument extend to recurring work? No, and the shape
## difference is exactly the one worth naming

The outbox needs no guard of its own because
`uq_jobs_serialized_lease` admits one live sender per key and **holding the
lease is the proof the predecessor does not**. That argument is about who
*executes*. Recurring work has a second edge a one-shot delivery does not: it
must be **scheduled again**, and the lease says nothing about that.

Look at what `fn_clock_tick`'s recurring leg actually does:

    IF NOT EXISTS (SELECT 1 FROM jobs WHERE kind = k AND state IN ('ready','leased'))
    THEN INSERT INTO jobs (...)...

That `NOT EXISTS` is a **snapshot read**, not a lock — the same shape as
`fn_claim_job`'s serialization check, which cannot serialize two callers.
And the index that rescues `fn_claim_job` does not rescue this one:
`uq_jobs_serialized_lease` is partial on ``state = 'leased'``, while the
double-insert happens at ``state = 'ready'``. **Two concurrent ticks would both
see no live row and both insert.** A clock that can double-fire is the same
defect class as an outbox that can double-send.

So the protection here is a *different* mechanism, and it is this module's
main job: **advisory-lock election**, which makes the concurrency impossible
rather than refusing a loser. :class:`ClockElection` is that, and the gate
proves it by removing it and watching the duplicate appear.

**`plan_slot` is the one recurring edge that also has an effect guard**,
and the contrast is instructive. Its idempotency lives on the *effect* rather
than the job: `uq_intent_slot` on `(workspace_id, ig_account_id,
schedule_slot_at)` means a duplicate `plan_slot` execution mints no second
intent. That is belt to the election's braces, and it is why the gate names
it separately — the election protects the scheduling edge, key 1 protects the
minting edge, and neither covers the other.

## Clock skew: out of scope here, by construction rather than by omission

Host clocks skew (an RTC-less host does after a reboot), so **every scheduling
decision reads the DATABASE clock, not the host's.** `fn_clock_tick` takes no
timestamp parameter: `now()`, `GREATEST(now(), last_done + cadence)` and every
`<= now()` predicate are evaluated in Postgres. A host whose wall clock jumps
backwards after a reboot changes nothing about which accounts are due.

The loop's own pacing is monotonic too — :meth:`Clock.run` sleeps on
``asyncio.sleep``, which measures against ``loop.time()`` (a monotonic source),
so a wall-clock jump cannot make a tick fire early, late, or twice.

What remains is skew of the **database's** clock, which is a deployment
property no application code can detect from inside, and belongs with the other
real-environment verifications. Stated here so the next reader knows it
was considered, rather than finding silence.

## The bounds are the door's, and they arrive as parameters

Tick every **15 s** with **≤ 500 inserts/tick** (`p_max`, a TOTAL across
all four job classes — the door's own comment is the one home for the priority
order), reaper every **60 s** at **500** (`p_lim`, likewise a total). Nothing
here hardcodes them.
"""

from __future__ import annotations

import json
import logging
import random
import time

from dataclasses import dataclass
from typing import Optional, Union

from sqlalchemy import text

from src.services.target import (
    category_mix,
    content_runway,
    outbox,
    prompts,
)


#: The advisory-lock key the clock elects on. A single fixed key, because there
#: is exactly one clock for the deployment — the clock runs inside an
#: elected worker. Chosen in the application range and never derived from a
#: name, so two spellings of the same intent cannot elect two clocks.
CLOCK_ELECTION_KEY = 0x5701_C10C

#: Elect, or confirm, in one statement that never adds a hold. A session that
#: already holds the key (its own granted row in `pg_locks`, matched on the
#: whole key: `objsubid = 1` is the one-bigint form, whose halves are
#: `classid` and `objid`) answers true without taking it again; any other
#: session tries for it. `CASE` evaluates only the branch it needs, so the
#: holder never calls `pg_try_advisory_lock`, which is re-entrant: called
#: again by the holder, it adds a hold that one unlock cannot take back.
CLOCK_ELECTION_SQL = (
    "SELECT CASE WHEN EXISTS ("
    "SELECT 1 FROM pg_locks"
    " WHERE locktype = 'advisory' AND granted AND mode = 'ExclusiveLock'"
    " AND pid = pg_backend_pid() AND objsubid = 1"
    " AND ((classid::bigint << 32) | objid::bigint) = :k"
    ") THEN true ELSE pg_try_advisory_lock(:k) END"
)


logger = logging.getLogger(__name__)


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

    **Only where the connection IS the session.** Through a transaction
    pooler (PgBouncer in transaction mode; Neon's ``-pooler`` host) the server
    session outlives the client's connection, so a lock taken on it stays
    held after this process closes, or dies, and no successor elects. The
    worker refuses a pooled host at boot (``src.worker.main``,
    ``unit_of_work.engine_url_refusal``).

    **One hold.** The lock is re-entrant: each success by the holding session
    adds a hold, and ``pg_advisory_unlock`` takes away one. So
    :meth:`try_acquire` confirms a lock this session already holds (its own
    row in ``pg_locks``) instead of taking it again (``CLOCK_ELECTION_SQL``),
    an elected clock holds exactly one, and :meth:`release` hands over at
    once. Taken again every tick, a clock held one per tick, and the one
    unlock at a clean shutdown handed nothing over.

    The connection is the caller's and is held for the elected process's
    lifetime. That is the one long-lived connection the design permits here,
    and it is why the clock rides inside an elected worker rather than owning a
    pool.
    """

    def __init__(self, conn, *, key: int = CLOCK_ELECTION_KEY):
        self._conn = conn
        self._key = key
        self.held = False

    async def try_acquire(self) -> bool:
        """Elect, or confirm that this session still holds the election.

        One statement either way (`CLOCK_ELECTION_SQL`), and the database
        decides, not `held`: a lock this session lost reads as lost even while
        `held` says True, and a held one is never taken a second time.
        """
        got = (
            await self._conn.execute(text(CLOCK_ELECTION_SQL), {"k": self._key})
        ).scalar()
        self.held = bool(got)
        return self.held

    async def release(self) -> None:
        """Give up the election. Idempotent.

        Never required for correctness — the session dying does this — but a
        clean shutdown that hands over in milliseconds rather than in a TCP
        timeout is worth the call. One unlock is enough because this session
        holds one count (`try_acquire` never takes a second).
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
    capability=None,
) -> dict:
    """One dispatcher pass, through the `04_functions.sql` door. Runs in the caller's tx.

    The door is the authority on what a tick does and on how its insert budget
    is shared between the five classes — this wrapper adds no policy, exactly
    as `intent_ledger.transition` adds none to `trg_intent_guard`. It exists so
    call sites read as intent and so the numbers arrive as parameters.

    *recurring* is the seam the door documents: ``{"v": 1, "<kind>":
    seconds,...}``.
    """
    from src.services.target.instagram_sources import is_activated

    activated = is_activated(capability)
    row = (
        await session.execute(
            text(
                "SELECT o_slot_jobs, o_refresh_jobs, o_sync_jobs, o_recurring_jobs"
                + (
                    ", past_posts_reauth_jobs AS o_reauth_jobs"
                    if activated
                    else ", o_reauth_jobs"
                )
                + " FROM fn_clock_tick(:m, make_interval(secs => :r), CAST(:g AS jsonb)"
                + (", CAST(:activated AS boolean))" if activated else ")")
            ),
            {
                "m": max_inserts,
                "r": refresh_cadence_seconds,
                "g": json.dumps(recurring),
                **({"activated": True} if activated else {}),
            },
        )
    ).first()
    return {
        "slot_jobs": row[0],
        "refresh_jobs": row[1],
        "sync_jobs": row[2],
        "recurring_jobs": row[3],
        "reauth_jobs": row[4],
    }


@dataclass(frozen=True)
class SlotOutcome:
    """What one `plan_slot` execution did — the intent, and what it SAID.

    The two are independent and the executor needs both: `intent_id` is None
    for a duplicate slot AND for an empty library, while `notice` is set only
    when the empty library could not be reported to anyone. Collapsing them
    back into a single `Optional[str]` is what would hide an undeliverable
    notice behind an ordinary "nothing minted".
    """

    intent_id: Optional[str] = None
    #: `outbox.UNDELIVERABLE` when nobody could have been told; else None.
    notice: Optional[str] = None


async def _notice_no_media(
    session, *, workspace_id: str, ig_account_id: str, dedup_after_seconds: int
) -> Union[int, str]:
    """Tell the workspace one of its slots found no media.

    Returns the number of outbox rows written, or `outbox.UNDELIVERABLE` when
    there was no surface to write to.

    A slot that finds no media lapses, and its "no media available"
    notice fires at most once per account per 24 h window.

    **The dedup is an atomic claim, not a read-then-write.** The UPDATE both
    tests the window and stamps it in one statement, so two workers racing the
    same starved account produce one notice, not two. A `SELECT... IF stale
    THEN UPDATE` would be correct in a test and wrong under the second worker
    — the shape `execute_plan_slot` already avoids on the minting edge.

    **An empty binding set is NOT a success, and not a bare `0` either.**
    A workspace with no push binding has nowhere for this to land ( —
    `push_bindings` selects active `telegram%` rows only, so a web-born
    workspace has none), so it returns `outbox.UNDELIVERABLE` and the job
    finalizes `review_required`. Returning `0` would collapse "told nobody,
    because there is nobody" into "told nobody, because they were told
    yesterday" — two facts with opposite remedies, and the reassuring one wins
    every time they are collapsed. That collapse is live in two producers
    (`credential_lifecycle` logs and succeeds; `media_sync` iterates an
    empty list in silence), which is exactly why this one does not copy them.

    **The marker still stamps on the undeliverable attempt.** It records WHEN
    this account last tried to say it, not whether anyone heard — and because
    it is a 24 h *window* rather than a latch, a workspace that later gains a
    binding is told at its next slot. Skipping the stamp instead would park a
    `review_required` job on every planned slot for as long as the condition
    stands, which buries the signal it is meant to raise.
    """
    claimed = (
        await session.execute(
            text(
                "UPDATE ig_accounts SET last_no_media_notice_at = now()"
                " WHERE id = :acct AND workspace_id = :ws"
                "   AND (last_no_media_notice_at IS NULL"
                "        OR last_no_media_notice_at"
                "           < now() - make_interval(secs => :age))"
                " RETURNING handle"
            ),
            {
                "acct": str(ig_account_id),
                "ws": str(workspace_id),
                "age": float(dedup_after_seconds),
            },
        )
    ).first()
    if claimed is None:
        # Inside the window: already said, nothing owed. A real success, and
        # the common case — which is why the binding lookup sits below it.
        return 0

    bindings = await prompts.push_bindings(session, workspace_id)
    if not bindings:
        logger.warning(
            "plan_slot: no media for account %s and its workspace has NO push"
            " binding — nobody was told (#1090)",
            ig_account_id,
        )
        return outbox.UNDELIVERABLE

    # Name the account, as every notice names one (`prompts.account_label`).
    label = prompts.account_label(claimed[0])
    await outbox.fanout_notification(
        session,
        workspace_id=workspace_id,
        bindings=bindings,
        text=(
            f"\U0001f4ed No media was available for {label}'s scheduled slot,"
            " so nothing was queued. Add media to this workspace's Drive"
            " source, or pause the account if that is intended."
        ),
    )
    logger.info(
        "plan_slot: no media for account %s — notified %d binding(s)",
        ig_account_id,
        len(bindings),
    )
    return len(bindings)


async def execute_plan_slot(
    session,
    *,
    workspace_id: str,
    ig_account_id: str,
    slot_at,
    provider_account_ref: str,
    no_media_notice_after_seconds: int,
    low_runway_days: int,
    rng: Optional[random.Random] = None,
) -> "SlotOutcome":
    """The `plan_slot` executor: mint the intent for one slot, or nothing.

    Returns a :class:`SlotOutcome`; its docstring says what `intent_id` and
    `notice` each report. A None `intent_id` — the slot already had one, or
    no media was available — is an ordinary outcome, not a failure.

    **The two Nones are not the same fact, and only one of them speaks.** A
    slot that already had an intent is the idempotency guard doing its job and
    the customer has nothing to learn from it; a slot that found no media is a
    missed slot, which the customer is owed a notice about ("you are told once
    — not silently nothing"). That notice is emitted here, where the
    empty case already lives.

    *no_media_notice_after_seconds* is the dedup window (24 h) and is
    **required, not defaulted**: a dedup window that can be silently omitted is
    how a once-a-day notice becomes either a flood or a silence, and there is
    exactly one production caller to pass it.

    *low_runway_days* is the runway notice's level (`content_runway.after_mint`): a mint that leaves the account with fewer
    days of eligible content than this tells the workspace once, and the next
    drop is told only after the account has climbed back
    `content_runway.REARM_MARGIN_DAYS` above it. Required for the same reason
    as the dedup window. **That notice never decides the slot**: it is said
    in a savepoint after the mint and its verdict is not carried back, so a
    slot that minted returns the intent alone, whether the notice reached
    nobody or could not be written at all.

    **Idempotent by key 1, not by checking first.** The insert carries
    ``ON CONFLICT … DO NOTHING`` against `uq_intent_slot`, so a duplicate
    `plan_slot` job — which the clock's own `NOT EXISTS` cannot rule out, and
    which a lease cannot either — mints no second intent. A read-then-write
    would be the shape: correct in a test, wrong under two workers.

    Note the conflict target is spelled as **columns**, not
    ``ON CONFLICT ON CONSTRAINT uq_intent_slot``.
    Key 1 ships as a bare ``CREATE UNIQUE INDEX``, and ``ON CONSTRAINT``
    resolves only names in ``pg_constraint``; that form does not run. The
    inference form is equivalent and is what the gate exercises.

    The target carries ``WHERE origin = 'cadence'`` because the slot key is a
    cadence rule: a planned row (a story a person scheduled for a chosen time)
    takes no slot, so it must never absorb a slot's mint, and a slot's mint
    must never meet it. `uq_intent_slot` is the partial unique index on
    cadence slots. Postgres infers a partial unique index only
    from a conflict target whose predicate implies the index's: the
    predicate-less spelling finds no arbiter and raises on every mint.

    Selection, keyed on the CONNECTED FOLDER: the
    connected folders that have eligible media are drawn by
    `category_mix.weights` (explicit weights by ratio; folders without a
    weight in proportion to their files, together never more than the smallest
    explicit weight; Off never; a folder gone in Drive never), then
    oldest-first within the drawn one. No pool behind that set: a removed
    folder's rows and an Off folder's are the two things that must never post,
    and they are all that would be left. `rng` is injectable so a test can
    seed the draw; production uses the system generator.

    Within the drawn folder: never-posted files go in the folder's shuffled
    order — the row's random id, not index time, which puts a batch exported
    together in a row — and once everything has posted, the file posted
    longest ago goes first so a small folder rotates."""
    draw = rng if rng is not None else random.SystemRandom()
    # Never-posted first, in the folder's shuffled order: `m.id` is a random
    # UUID, a stable per-file shuffle key — index time puts a batch exported
    # together in a row. Once everything has posted, least-recently-posted
    # first, so a small folder rotates through its files instead of
    # repeating the oldest one.
    order = " ORDER BY m.last_posted_at NULLS FIRST, m.id LIMIT 1"
    # Selection's rule and the connected folders' weights, read as the runway
    # reads them: the files this draws from and the count the Overview
    # shows are one pool, so they cannot disagree.
    from src.services.target import source_policy, versioned_mix, readers
    from decimal import Decimal

    context = await source_policy.suitability_context(
        session, workspace_id=workspace_id, ig_account_ids=[ig_account_id]
    )
    converted = versioned_mix.converted(
        await versioned_mix.state(session, str(workspace_id))
    )
    if converted:
        account = await readers.row(
            session,
            "SELECT state FROM ig_accounts WHERE workspace_id=:ws AND id=:acct",
            ws=str(workspace_id),
            acct=str(ig_account_id),
        )
        if account is None or account["state"] != "active":
            return SlotOutcome()
    drawn = await category_mix.pool(
        session, workspace_id=workspace_id, ig_account_id=ig_account_id, context=context
    )
    weighted = drawn.drawable
    # No pool behind the weighted set: every connected folder that may post
    # is in `weighted` once it has eligible media, so anything left would
    # belong to a removed folder or an Off one — the two things that must
    # never post. Nothing eligible is the no-media path below.
    chosen: Optional[str] = None
    if weighted:
        total = sum(w for _, w in weighted)
        point = (Decimal(str(draw.random())) if converted else draw.random()) * total
        for source_id, weight in weighted:
            point -= weight
            if point < 0:
                chosen = source_id
                break
        else:
            chosen = weighted[-1][0]
    else:
        logger.info(
            "plan_slot: no connected folder has eligible media for workspace %s",
            workspace_id,
        )
    media = None
    if chosen is not None and converted:
        source_policy.require_context(session, context, workspace_id)
        media = (
            await session.execute(
                text(
                    "SELECT m.id FROM ("
                    + source_policy.CANDIDATE_SQL
                    + ") v JOIN media_items m ON m.workspace_id=:ws AND m.id=v.media_item_id WHERE v.suitable"
                    + order
                ),
                dict(
                    context.binds(),
                    acct=str(ig_account_id),
                    sid=chosen,
                    media=None,
                    intent=None,
                ),
            )
        ).first()
    elif chosen is not None:
        media = (
            await session.execute(
                text(
                    "SELECT m.id FROM media_items m"
                    + category_mix.ELIGIBLE_SQL
                    + "   AND m.source_id = CAST(:source_id AS uuid)"
                    + order
                ),
                {"ws": workspace_id, "acct": ig_account_id, "source_id": chosen},
            )
        ).first()
    if media is None:
        said = await _notice_no_media(
            session,
            workspace_id=workspace_id,
            ig_account_id=ig_account_id,
            dedup_after_seconds=no_media_notice_after_seconds,
        )
        # `said` is a row count on an ordinary beat and the sentinel when
        # nobody could be told; only the second is a verdict worth carrying.
        return SlotOutcome(notice=said if said == outbox.UNDELIVERABLE else None)

    row = (
        await session.execute(
            text(
                "INSERT INTO post_intents (workspace_id, ig_account_id, media_item_id,"
                " provider_account_ref, approval_mode, schedule_slot_at, state)"
                " VALUES (:ws, :acct, :media, :ref, 'manual', :slot, 'scheduled')"
                " ON CONFLICT (workspace_id, ig_account_id, schedule_slot_at)"
                " WHERE origin = 'cadence'"
                " DO NOTHING RETURNING id"
            ),
            {
                "ws": workspace_id,
                "acct": ig_account_id,
                "media": media[0],
                "ref": provider_account_ref,
                "slot": slot_at,
            },
        )
    ).first()
    if row is None:
        return SlotOutcome()
    # The runway notice is said beside the mint and never decides the slot
    # (`content_runway`, "The notice never decides the slot"): its verdict is
    # not carried back, so a notice nobody could receive still finalizes the
    # job succeeded. It rides a savepoint, so a failure writing it rolls back
    # alone and the mint stands.
    try:
        async with session.begin_nested():
            # Nothing in this transaction moved the pool between its read and
            # the mint, so what is left is the pool less the minted file.
            await content_runway.after_mint(
                session,
                workspace_id=workspace_id,
                ig_account_id=ig_account_id,
                eligible=drawn.candidates_after_a_mint,
                below_days=low_runway_days,
            )
    except Exception:  # noqa: BLE001 — logged; the mint stands
        logger.exception(
            "plan_slot: the runway notice for account %s was NOT written;"
            " the mint stands",
            ig_account_id,
        )
    return SlotOutcome(intent_id=str(row[0]))


async def execute_reap_expired(
    engine,
    *,
    limit: int,
    approval_ttl_seconds: int,
    approved_ttl_seconds: int,
    capability=None,
    past_posts_late_seconds: Optional[int] = None,
) -> int:
    """Reap in closed door, discovery, subject and card transactions.

    The SQL door's total budget is retained. Its job/intent locks cannot
    survive into cleanup's canonical source prefix, and one subject never
    carries that prefix into a later workspace. Cleanup does not authorize
    new media effects, so an Off source still receives terminal healing.

    *capability* is the past-posts source's activation. It reaches only the legs that
    judge a sync job or re-arm its source: an expired past-posts sync is re-readied
    or ended by whether its source may still be read. Without it the
    three-argument door runs, which answers as an inactive past-posts source does.
    *past_posts_late_seconds* rides with it: how long after its slot a past-posts
    cadence story waits for its file before the cadence leg expires it. With
    neither, that story expires at its slot, as a Drive story does.
    """
    from src.services.target import publish_pipeline, unit_of_work
    from src.services.target.instagram_sources import is_activated

    activated = is_activated(capability)
    system = unit_of_work.poller_session_factory(engine, "")
    async with system() as session:
        swept = int(
            (
                await session.execute(
                    text(
                        "SELECT fn_reaper_sweep(:lim, make_interval(secs => :a),"
                        " make_interval(secs => :b)"
                        + (
                            ", CAST(:activated AS boolean), make_interval(secs => :late))"
                            if activated
                            else ")"
                        )
                    ),
                    {
                        "lim": limit,
                        "a": approval_ttl_seconds,
                        "b": approved_ttl_seconds,
                        **(
                            {"activated": True, "late": past_posts_late_seconds}
                            if activated
                            else {}
                        ),
                    },
                )
            ).scalar()
            or 0
        )
        await session.commit()

    remaining = max(0, int(limit) - swept)
    subjects = []
    if remaining:
        async with system() as session:
            subjects = (
                await session.execute(
                    text(
                        "SELECT o_intent_id, o_workspace_id FROM "
                        "fn_reaper_stale_approved(make_interval(secs => :ttl), :lim)"
                    ),
                    {"ttl": int(approved_ttl_seconds), "lim": remaining},
                )
            ).all()
            await session.commit()

    parked = 0
    for intent_id, workspace_id in sorted(
        subjects, key=lambda row: (str(row[1]), str(row[0]))
    ):
        try:
            async with unit_of_work.UnitOfWork(
                engine, str(workspace_id), actor_kind="reaper"
            ).begin() as session:
                parked += await publish_pipeline.park_stale_approved_subject(
                    session,
                    workspace_id=str(workspace_id),
                    intent_id=str(intent_id),
                    older_than_seconds=approved_ttl_seconds,
                )
        except Exception:  # noqa: BLE001 — one subject rolls back alone
            logger.exception("reaper: stale approved subject skipped this tick")

    # Header/ref cleanup acquires no new source authority and shares none of
    # the door or subject locks. WorkLoop alone finalizes the reaper job.
    async with system() as session:
        await prompts.sweep_settled_cards(session, limit=limit)
        await session.commit()
    return swept + parked


async def execute_retention_sweep(
    session_factory, *, keep_seconds: int, batch: int, budget_seconds: float
) -> int:
    """The `retention_sweep` executor. Returns rate-counter rows deleted.

    Runs ONE retention class, `rate_counters`, through the `04_functions.sql` door
    `fn_retention_batch`. Each call deletes at most *batch* rows in a short
    transaction of its own from *session_factory*, repeated until a call
    comes back short or *budget_seconds* is spent. The door accepts only
    `rate_counters`; the audit trail, cap ledger and archive exports remain
    outside this sweep.
    """
    stop_at = time.monotonic() + budget_seconds
    total = 0
    while True:
        async with session_factory() as session:
            deleted = (
                await session.execute(
                    text(
                        "SELECT fn_retention_batch('rate_counters',"
                        " make_interval(secs => :keep), :batch)"
                    ),
                    {"keep": keep_seconds, "batch": batch},
                )
            ).scalar()
        total += deleted
        drained = deleted < batch
        if drained or time.monotonic() >= stop_at:
            break
    logger.info(
        "retention_sweep: deleted %d rate_counters row(s) older than %ds; %s",
        total,
        keep_seconds,
        "drained" if drained else "time budget spent, more may remain",
    )
    return total


async def execute_reap_transit_assets(
    session, *, lister, deleter, older_than_seconds: int
) -> int:
    """The `reap_transit_assets` executor. Returns assets reaped.

    **Discovery is ONE global age-filtered provider listing per sweep, never
    per-workspace listings** — the reason is the shape of the
    cost: per-workspace discovery is a provider call per tenant per sweep,
    which grows with the customer count for a job whose work does not.

    *lister* and *deleter* are injected, as the outbox transport is, because
    the bounded-sweep property is about this code, and a real provider client
    here would make the gate a network test. An asset the deleter refuses is
    left for the next sweep rather than retried in-loop — the hard-TTL sweep is
    the backstop for exactly this.
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
    defect. That is the same rule the outbox poller applies to a paced tick.

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
        conversion_evidence=None,
        capability=None,
    ):
        self._election = ClockElection(election_conn)
        self._session_factory = session_factory
        self._interval = interval_seconds
        self._max_inserts = max_inserts
        self._refresh_cadence_seconds = refresh_cadence_seconds
        self._recurring = recurring
        self._conversion_evidence = conversion_evidence
        #: The past-posts source's activation, handed to every tick; None keeps the
        #: five-leg door an inactive past-posts source gets.
        self._capability = capability
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
                    capability=self._capability,
                )
                await session.commit()
        except Exception as exc:  # noqa: BLE001 — swallowed on purpose; see below
            self.consecutive_failures += 1
            # The exception is swallowed and counted: a raising clock is worse
            # than a failing one, because the loop dies and the election is
            # never released. It is logged before it is discarded, so it is
            # not the only artifact naming WHAT failed.
            #
            # The counter covers liveness, not diagnosis. A count says the
            # clock is failing; it cannot say which leg or which kind, and
            # `tick` runs five legs in one transaction so any one of them
            # aborts all five identically. A job kind missing from
            # `ck_jobs_kind` stops the clock estate-wide, and without the log
            # its only symptom, five layers away, is a worker gate failing
            # `assert bulk.processed >= 1` on `0 >= 1`.
            #
            # `logger.exception` rather than a formatted message: the failing
            # row is in the DBAPI error's own text, so re-rendering it by hand
            # is how the offending kind goes missing. Logged on EVERY failure,
            # not the first — a clock that recovers and re-breaks is two
            # incidents, and a once-only log makes the second invisible.
            # THE RECURRING KINDS ARE NAMED EXPLICITLY, and that is not
            # belt-and-braces — it is what lets the log say WHICH kind.
            # PostgreSQL's CHECK violation names the CONSTRAINT
            # ("ck_jobs_kind") and puts the failing VALUE in a separate DETAIL
            # line that SQLAlchemy's str() does not carry. Logging the
            # traceback alone therefore reports that a job kind was refused
            # without saying WHICH, which is the same diagnosis-shaped hole
            # one layer in. The test below fails on exactly that.
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
        from src.services.target import mix_conversion

        conversion_failed = False
        if isinstance(self._conversion_evidence, mix_conversion.MixConversionEvidence):
            counts["conversion_jobs"] = 0
        if isinstance(
            self._conversion_evidence, mix_conversion.MixConversionEvidence
        ) and self._max_inserts > sum(counts.values()):
            try:
                # Conversion waits if ordinary discovery spent the total budget.
                # Job discovery never shares the ordinary tick's domain locks.
                async with self._session_factory() as session:
                    counts["conversion_jobs"] = await mix_conversion.mint_jobs(
                        session, evidence=self._conversion_evidence
                    )
                    await session.commit()
            except Exception:  # noqa: BLE001 — retry discovery on the next tick
                counts["conversion_jobs"] = 0
                conversion_failed = True
                logger.exception(
                    "mix conversion discovery failed; ordinary tick committed"
                )
        self.consecutive_failures = (
            self.consecutive_failures + 1 if conversion_failed else 0
        )
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
