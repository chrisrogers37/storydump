"""Did a post actually LAND?

## The axis this module watches

`scheduling_health` — the module beside this one — states its own blind spot in
its docstring, and this module covers it:

> **It cannot see a failure PAST the mint.** Cursors advancing while nothing
> posts is a different outage and wants its own signal.

This is that signal. Every field `/health/scheduling` publishes is about
MACHINERY: the clock advances a cursor, the worker finishes a job. All of it can
be true while the product posts nothing at all. `approval_mode` defaults to `manual`
(`provisioning.py`), so an intent awaiting approval is **not overdue, it is
waiting correctly**, and the machinery gauge reads healthy because the machinery
*is* healthy.

Two different causes give the same green light:

- **(a) an EMPTY tier**: 0 `ig_accounts`, 0 `workspaces`, 0 `media_sources`.
  Nothing can mint an intent.
- **(b) a populated tier** whose intents all strand `awaiting_approval`.

Different causes, same reading. The one assertion false in both is *a post
landed*, which is what this module measures.

## THE EVIDENCE IS `post_intents`, NOT the cap ledger

The cap ledger, `daily_post_counts`, is read here and returned. But
it **cannot be the thing the verdict rests on**, because of when it is
written.

`publish_cap` debits at the `approved → publishing` flip — one CTE, cap debit
coupled to the state change — which happens **BEFORE the publish call**. So
`count > 0` means *an attempt claimed a cap slot*, not *a post reached
Instagram*. A publish loop that debits and fails every time moves that number
and lands nothing. A monitor resting on it would go green on exactly the failure
it exists to catch, one layer inward.

`post_intents.state = 'posted'` is the landing. `ck_posted_complete` requires a
debited cap for every posted row. API posts also require an Instagram container
ID and `publish_step = 'effect_confirmed'`; dry runs require the confirmed
effect step and are excluded from the aggregates below. The database enforces
the evidence each publication mode requires.

So both are returned and each answers something the other cannot:

| reading | means |
|---|---|
| `posted_ever = 0`, `debited_total = 0` | nothing ever even tried |
| `posted_ever = 0`, `debited_total > 0` | **attempts are being made and none is landing** |
| `posted_ever > 0` | it has posted; `last_post_age_seconds` says when |

The middle row is invisible to either signal alone, and it is the sharper
diagnosis of the two.

## Dry runs do not prove a post reached Instagram

Both posting aggregates exclude `published_via = 'dry_run'`. The schema
requires a cap debit and completed effect evidence for a dry run, but the
worker's simulated publish does not create a real Instagram Story.

## Why an age is `None` rather than `0` when nothing has ever posted

`0` is the most reassuring value the field has — "a post landed just now" — and
it is what a product that has NEVER posted would return under the obvious
`coalesce`. Same rule `worker_freshness` states for `last_success_age_seconds`,
and for the same reason.

## The grace anchor, and why the destination count is not it

`oldest_intent_age_seconds` is returned so a poller can tell how long the estate
has had something to post. It is an ANCHOR, never a gate: it can only make a
check fire SOONER, never suppress one.

The rejected design is gating on `accounts_active > 0` — "no destinations, so
nothing is expected to post, so stay quiet." That is a permanent exemption
wearing a reasonable face, and phase (a) above **is** that state: an empty tier
would be excused forever by it, the very silence
this instrument is built to catch. `accounts_active` is returned for the
alert TEXT, so a human is told which of the two failures they have, and the
poller must not consult it for the verdict.

## Bounds, stated because they will not be obvious later

**Cross-tenant reach is a door, not a privilege.** Each read here is a
SECURITY DEFINER function owned by `svc_maintenance` (fleet health functions) with
EXECUTE for the runtime logins — the same footing `scheduling_lag` and the
backpressure snapshot stand on. Without it, under `svc_ingress` every
policy-covered table reads empty to these tenant-less queries, `posted_ever`
reads 0 for a live estate, and a poller's verdict becomes *never-posted* —
the alarm direction, but for the wrong reason, and blind to a real silence.
`tests/scripts/test_fleet_health_doors_gate.py`
runs this module as `svc_ingress` against a seeded estate.

**The cap ledger has no retention sweep.** `fn_retention_batch` accepts only
`rate_counters`, the class `scheduler.execute_retention_sweep` calls.
`debited_total` and `ledger_days` therefore summarize all retained cap rows.

**Nothing identifying is returned.** Counts and ages, never a workspace, an
account, a handle or a permalink. The endpoint serving this is unauthenticated
by design, so the aggregate has to be safe to say out loud.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text


async def posting_freshness(executor) -> dict[str, Any]:
    """Has a post LANDED, and how long ago — estate-wide.

    ``posted_ever`` counts confirmed landings; ``last_post_age_seconds`` is the
    freshest of them, or ``None`` when there has never been one. The null is
    deliberate and is not a `coalesce` waiting to happen: see the docstring.

    ``intents_ever`` and ``oldest_intent_age_seconds`` describe how long the
    estate has had anything to post — the poller's grace anchor, which can only
    make it speak sooner.

    No threshold is applied here. The poller owns that, the same split
    `scheduling_lag` and `worker_freshness` make: a cadence is deployment
    configuration, not a fact about this query.
    """
    row = (
        (
            await executor.execute(
                text(
                    # The door's body (fleet health functions): min-of-ages, not max-of-times,
                    # because FILTER binds to the AGGREGATE — the freshest
                    # landing is the SMALLEST age.
                    "SELECT o_posted_ever AS posted_ever,"
                    "   o_last_post_age_seconds AS last_post_age_seconds,"
                    "   o_intents_ever AS intents_ever,"
                    "   o_oldest_intent_age_seconds AS oldest_intent_age_seconds"
                    " FROM fn_health_posting_freshness()"
                )
            )
        )
        .mappings()
        .one()
    )
    age = row["last_post_age_seconds"]
    oldest = row["oldest_intent_age_seconds"]
    return {
        "posted_ever": int(row["posted_ever"]),
        "last_post_age_seconds": None if age is None else int(age),
        "intents_ever": int(row["intents_ever"]),
        "oldest_intent_age_seconds": None if oldest is None else int(oldest),
    }


async def publish_attempts(executor) -> dict[str, Any]:
    """The cap ledger — what TRIED, against what landed.

    `daily_post_counts` is debited at the `approved → publishing` flip, before
    the publish call, and refunded on a failure after debit. So this is the
    attempt record, and its value here is entirely in the CONTRAST with
    `posted_ever`: nonzero attempts beside zero landings is a publish path
    failing every time, which neither number says on its own.

    ``ledger_days`` counts (workspace, account, day) buckets that ever debited —
    a bucket survives a refund at `count = 0`, so it distinguishes "never
    debited" from "debited and given back", which the sum alone cannot.
    """
    row = (
        (
            await executor.execute(
                text(
                    "SELECT o_debited_total AS debited_total,"
                    "   o_ledger_days AS ledger_days"
                    " FROM fn_health_publish_attempts()"
                )
            )
        )
        .mappings()
        .one()
    )
    return {
        "debited_total": int(row["debited_total"]),
        "ledger_days": int(row["ledger_days"]),
    }


async def destinations(executor) -> dict[str, Any]:
    """The destinations that could receive a post — a COUNT and an AGE.

    ## The count is context and must never be a gate

    A poller that stayed quiet because `accounts_active` is zero would excuse an
    empty tier forever, which is phase (a) above. It is
    returned so an alert can name which failure a human is looking at — an
    estate with nothing to post to, or an estate whose posts are stranded — and
    the poller's verdict must not consult it.

    ## The age is an EARLIER RUNG of the grace ladder, and it is the point

    A never-posted estate's grace clock needs an anchor, and the poller's own
    `first_seen_at` is off-host by design but can be LOST — the one direction
    that makes a monitor speak later. `oldest_intent_age_seconds` backstops it,
    but is null in exactly the window that needs it most: an estate can hold
    live destinations for days before minting a single intent. A watcher that
    started inside that window would have no database-side anchor at all and
    would sit on a notice past the grace while destinations idle.

    So the ladder is published rung by rung — a destination exists, then an
    intent exists — and the poller takes the LATEST of all of them. Every rung
    can only make it speak SOONER, never suppress it.

    **Deliberately not a `WHERE state = 'active'` shared with
    `scheduling_health.scheduling_lag`**, which computes an identical
    `accounts_active` on the neighbouring endpoint. They agree and the
    duplication is real, but the two are different questions — that one asks
    which cursors could stall, this one asks which destinations could receive —
    and `ck_ig_accounts_state` carries four states, so they can legitimately
    diverge (`reauth_required` cannot receive a post; its cursor still
    advances). They stay separate, and the hazard is that they diverge
    SILENTLY under the same key name: whoever changes one must change or fork
    the other deliberately.
    """
    row = (
        (
            await executor.execute(
                text(
                    # max-of-ages, not min-of-times, inside the door (fleet health functions):
                    # the OLDEST destination is the LARGEST age, and it is the
                    # rung that dates the estate's expectation to post.
                    "SELECT o_accounts_active AS accounts_active,"
                    "   o_oldest_active_destination_age_seconds"
                    "     AS oldest_active_destination_age_seconds"
                    " FROM fn_health_destinations()"
                )
            )
        )
        .mappings()
        .one()
    )
    oldest = row["oldest_active_destination_age_seconds"]
    return {
        "accounts_active": int(row["accounts_active"]),
        "oldest_active_destination_age_seconds": (
            None if oldest is None else int(oldest)
        ),
    }
