"""Did a post actually LAND? (#1268)

## The axis nothing watched

`scheduling_health` — the module beside this one — states its own blind spot in
its docstring, and this is that blind spot named as a deliverable:

> **It cannot see a failure PAST the mint.** Cursors advancing while nothing
> posts is a different outage and wants its own signal.

This is that signal. Every field `/health/scheduling` publishes is about
MACHINERY: the clock advances a cursor, the worker finishes a job. All of it can
be true — and was, for 1936 consecutive `healthy` readings over ~6.7 days —
while the product posted nothing at all. `approval_mode` defaults to `manual`
(`provisioning.py`), so an intent awaiting approval is **not overdue, it is
waiting correctly**, and the machinery gauge reads healthy because the machinery
*is* healthy.

The outage that exposed this had TWO stacked causes and the same green light
across both:

- **08-24 → ~09-02** — the target tier was EMPTY: 0 `ig_accounts`, 0
  `workspaces`, 0 `media_sources`. Nothing could mint an intent.
- **09-07 → onward** — the tier populated, intents minted, and all six stranded
  `awaiting_approval`.

Different causes, same reading. The one assertion false in both is *a post
landed*, which is what this module measures.

## THE EVIDENCE IS `post_intents`, NOT the cap ledger — and that is not a
## deviation from the filing, it is the filing carried one layer deeper

#1268 names `daily_post_counts` as the query, and that table is read here and
returned. But it **cannot be the thing the verdict rests on**, because of when
it is written.

`publish_cap` debits at the `approved → publishing` flip — one CTE, cap debit
coupled to the state change — which happens **BEFORE the publish call**. So
`count > 0` means *an attempt claimed a cap slot*, not *a post reached
Instagram*. A publish loop that debits and fails every time moves that number
and lands nothing. A monitor resting on it would go green on exactly the failure
it exists to catch, one layer inward — the same shape as the gauge it replaces.

`post_intents.state = 'posted'` is the landing, and the DATABASE enforces that
it carries proof. `ck_posted_complete` refuses a `posted` row unless it is
`legacy_backfill`, or `manual` with a debited cap, or `api` with
`ig_container_id IS NOT NULL AND publish_step = 'effect_confirmed'`. The
strongest available assertion that a post reached Instagram is a constraint the
schema will not let a row violate.

So both are returned and each answers something the other cannot:

| reading | means |
|---|---|
| `posted_ever = 0`, `debited_total = 0` | nothing ever even tried |
| `posted_ever = 0`, `debited_total > 0` | **attempts are being made and none is landing** |
| `posted_ever > 0` | it has posted; `last_post_age_seconds` says when |

The middle row is invisible to either signal alone, and it is the sharper
diagnosis of the two.

## `legacy_backfill` is EXCLUDED — for a LIVE schema reason, not the migration

The obvious justification is the M.3 history transform, and it is **wrong**: no
M.1/M.3 transform file was ever written and none will be (FC-7 §6, owner ruling
confirmed 2026-09-02 — the target is greenfield). Nothing produces these rows
today, and a comment claiming otherwise would be exactly the authoritative-
sounding-and-false reading this whole instrument exists to stop.

The filter stays because of what the SCHEMA still permits, which is the stronger
reason anyway. `published_via` accepts `legacy_backfill` (`ck_intent_via`), and
`ck_posted_complete` **exempts** such a row from every evidence requirement:

    state <> 'posted'
    OR published_via = 'legacy_backfill'          <-- no evidence required
    OR (published_via = 'manual' AND cap_consumed_on IS NOT NULL)
    OR (published_via = 'api' AND ig_container_id IS NOT NULL
        AND publish_step = 'effect_confirmed' AND cap_consumed_on IS NOT NULL)

So it is the one `posted` row the database will accept **with no proof that
anything reached Instagram** — precisely the row this module must not count.
Any future bulk insert (a re-imagined backfill, a repair script, a test
fixture that escapes) would carry a fresh `entered_state_at` and read as *a post
just landed*, announcing a recovery nobody observed and then going quiet for a
full threshold. The filter is the module refusing to be an instance of its own
subject.

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
would be excused forever by it, which is the sixteen-day silence reproduced
inside the instrument built to end it. `accounts_active` is returned for the
alert TEXT, so a human is told which of the two failures they have, and the
poller must not consult it for the verdict.

## Bounds, stated because they will not be obvious later

**Cross-tenant reach rests on a tracked gap.** Production connects as
`neondb_owner`, which owns these tables and bypasses RLS, so `p_tenant` is inert
(#751) — the same footing `scheduling_lag` documents and the same door whoever
closes #751 must provide. Under a role the policy covers, a tenant-less read
returns zero rows, and zero rows here reads as *nothing has posted*: this module
fails toward the ALARM rather than toward good news, which is the survivable
direction, but it would be alarming for the wrong reason.

**A retention door exists for the cap ledger, and nothing runs it today.**
`059`'s `fn_retention_purge` holds a `DELETE FROM daily_post_counts`, but
`retention_sweep` is in `work_loop.UNBUILT_KINDS` — the registry parks it
unconditionally, so no executor has ever aged a row out. If it is ever built,
`debited_total` and `ledger_days` become windowed rather than all-time and the
attempt/landing contrast below weakens for old estates. `posted_ever` is
unaffected: the class list does not name `post_intents`.

**Nothing identifying is returned.** Counts and ages, never a workspace, an
account, a handle or a permalink. The endpoint serving this is unauthenticated
by design, so the aggregate has to be safe to say out loud.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

#: The one `posted` row `ck_posted_complete` accepts with NO provider evidence.
#: Excluded from every posting signal — see the module docstring. Nothing
#: produces these today (the transform was cancelled, FC-7 §6); the filter
#: guards what the schema still permits, not a migration that is coming.
_REAL_POST = "state = 'posted' AND published_via NOT IN ('legacy_backfill', 'dry_run')"


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
                    "SELECT"
                    f"   count(*) FILTER (WHERE {_REAL_POST}) AS posted_ever,"
                    # min-of-ages, not max-of-times: FILTER binds to the
                    # AGGREGATE, so `EXTRACT(... max(...)) FILTER (...)` is a
                    # syntax error. The freshest landing is the SMALLEST age.
                    "   min(EXTRACT(EPOCH FROM now() - entered_state_at))"
                    f"     FILTER (WHERE {_REAL_POST})"
                    "     AS last_post_age_seconds,"
                    "   count(*) AS intents_ever,"
                    "   max(EXTRACT(EPOCH FROM now() - created_at))"
                    "     AS oldest_intent_age_seconds"
                    " FROM post_intents"
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
    """The cap ledger #1268 names — what TRIED, against what landed.

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
                    "SELECT"
                    "   coalesce(sum(count), 0) AS debited_total,"
                    "   count(*) AS ledger_days"
                    " FROM daily_post_counts"
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
    empty tier forever, which is phase (a) of the outage this exists for. It is
    returned so an alert can name which failure a human is looking at — an
    estate with nothing to post to, or an estate whose posts are stranded — and
    the poller's verdict must not consult it.

    ## The age is an EARLIER RUNG of the grace ladder, and it is the point

    A never-posted estate's grace clock needs an anchor, and the poller's own
    `first_seen_at` is off-host by design but can be LOST — the one direction
    that makes a monitor speak later. `oldest_intent_age_seconds` backstops it,
    but is null in exactly the window that needs it most: an estate can hold
    live destinations for days before minting a single intent (production did,
    2026-09-02 → 09-06). A monitor installed inside that window would have had
    no database-side anchor at all and would have sat on a notice for 72h with
    two destinations idle for six days.

    So the ladder is published rung by rung — a destination exists, then an
    intent exists — and the poller takes the LATEST of all of them. Every rung
    can only make it speak SOONER, never suppress it.

    **Deliberately not a `WHERE state = 'active'` shared with
    `scheduling_health.scheduling_lag`**, which computes an identical
    `accounts_active` on the neighbouring endpoint. They agree today and the
    duplication is real, but the two are different questions — that one asks
    which cursors could stall, this one asks which destinations could receive —
    and `ck_ig_accounts_state` carries four states, so they can legitimately
    diverge (`reauth_required` cannot receive a post; its cursor still
    advances). Consolidating is tracked rather than done here: the hazard is
    that they diverge SILENTLY under the same key name, and whoever changes one
    must change or fork the other deliberately.
    """
    row = (
        (
            await executor.execute(
                text(
                    "SELECT count(*) AS accounts_active,"
                    # max-of-ages, not min-of-times: the OLDEST destination is
                    # the LARGEST age, and it is the rung that dates the
                    # estate's expectation to post.
                    "   max(EXTRACT(EPOCH FROM now() - created_at))"
                    "     AS oldest_active_destination_age_seconds"
                    " FROM ig_accounts WHERE state = 'active'"
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
