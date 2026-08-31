"""L.3 — the `reconcile_ambiguous` executor (`02` §6, `05` ladder, issue #860).

This ships with the permit rail rather than after it, because the rail is what
starts manufacturing ``publishing_ambiguous`` intents. An ambiguity producer
without its resolver strands every artefact the kill tests create.

**One contract, parameterized by evidence authority.** The seam
``reconciler_evidence_mode`` records 0.4's doc-cited verdict as the sets of
container ``status_code`` values that are authoritative *after*
``publish_called``. Setting the seam is a config change, never a design change,
and the machinery below is mode-independent.

0.4 selected **container-verdict** mode (2026-08-13). Both modes ship:
evidence-capture remains correct for any account or API version where the
positive value stops arriving, and building only the selected one would fail
L.3's own gate, which requires a test per mode.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Callable, Optional, Union

from sqlalchemy import text

logger = logging.getLogger(__name__)

#: `05`: per-intent exponential backoff, capped at container expiry (~24 h).
#: This is the ladder that kills the flat-poll pathology — roughly 15-20 status
#: calls per ambiguous intent worst case, against ~1,440 at a flat 60 s poll.
LADDER_SECONDS = (60, 300, 1800, 7200)

#: Container expiry. The ladder never polls past it.
CONTAINER_EXPIRY_SECONDS = 24 * 60 * 60

#: The seam. 0.4's values, doc-cited (`0.4-meta-primary-doc-verification.md`).
#:
#: `FINISHED` IS THE TRAP AND IT IS DELIBERATELY NON-TERMINAL. After
#: `publish_called` it means *ready to be published*, which is evidence the
#: publish has NOT landed — not evidence that it failed. Classifying it
#: authoritative-negative would terminalize a still-live publish as a definite
#: non-event, which is the one wrong answer that is worse than no answer.
EVIDENCE_MODES: dict[str, dict[str, frozenset]] = {
    "container_verdict": {
        "positive": frozenset({"PUBLISHED"}),
        "negative": frozenset({"EXPIRED", "ERROR"}),
    },
    #: Both sets empty: the machine never self-terminalizes. Every exhausted
    #: ladder parks `review_required` with the captured trail.
    "evidence_capture": {
        "positive": frozenset(),
        "negative": frozenset(),
    },
}

DEFAULT_MODE = "container_verdict"

#: Verdicts this module can reach.
POSITIVE, NEGATIVE, INCONCLUSIVE = "positive", "negative", "inconclusive"


def classify(status_code: Optional[str], mode: str = DEFAULT_MODE) -> str:
    """Map an observed container ``status_code`` to a verdict under *mode*.

    Everything not in the mode's authoritative sets is INCONCLUSIVE — recorded
    as evidence, never acted on, and the ladder continues. That includes
    `ERROR`/`EXPIRED` sightings under evidence-capture mode, and `FINISHED`
    under both.
    """
    try:
        sets = EVIDENCE_MODES[mode]
    except KeyError:
        raise ValueError(f"unknown evidence mode: {mode!r}") from None
    if status_code in sets["positive"]:
        return POSITIVE
    if status_code in sets["negative"]:
        return NEGATIVE
    return INCONCLUSIVE


def next_delay_seconds(checks: int) -> Optional[int]:
    """Seconds until the next ladder rung, or None once the ladder is spent.

    *checks* is how many polls have already happened. The last rung repeats
    until the cumulative wait reaches container expiry, at which point the
    ladder is exhausted and the exhaustion tail runs.
    """
    if checks < 0:
        raise ValueError("checks cannot be negative")
    elapsed = 0
    for i in range(checks + 1):
        rung = LADDER_SECONDS[min(i, len(LADDER_SECONDS) - 1)]
        if i == checks:
            return None if elapsed + rung > CONTAINER_EXPIRY_SECONDS else rung
        elapsed += rung
    return None


async def sweep_due(conn, *, limit: int, notify_after_seconds: int) -> list[dict]:
    """Call the `fn_reconciler_sweep` door (shipped in migration 059).

    The door owns the budget: ``p_lim`` bounds the WHOLE sweep across both
    reasons, ladder-due rows taking priority over notify-window rows. This
    module does not re-implement that split — an unresolved ambiguity blocks
    its account's next publish via ``uq_publish_exclusive``, while a
    notify-window row is informational, and the door already encodes that.

    **Keys are the door's names minus the ``o_`` prefix, aliased in the SELECT
    so there is one spelling.** The prefix is a SQL OUT-parameter convention
    and does not belong in the Python contract; the sole consumer already read
    ``intent_id``/``workspace_id`` against a door that returned
    ``o_intent_id``/``o_workspace_id``, so the first non-empty sweep would have
    raised ``KeyError``. Nothing caught it because the kind parks without a
    provider poll seam and no test drove the executor. Aliasing here fixes it
    where the names are chosen rather than at the one call site that happens to
    exist today.

    ``reason`` is ``ladder_due`` or ``notify_window`` and the caller MUST
    branch on it: the two rows want opposite work (poll the provider vs. tell
    the customer), and the second needs no provider seam at all.
    """
    # A LIST of timedeltas, not a Postgres array LITERAL in a string. asyncpg
    # binds `interval[]` from a Python sequence of timedeltas and REFUSES a
    # str outright ("a sized iterable container expected"), so the literal
    # form made this door raise `DataError` on its first statement — every
    # call, not an edge case. Nothing caught it because the only caller sits
    # behind a kind that parks without a provider poll seam, and no test drove
    # it; this is why #1090 D4 had no notification even in principle.
    rungs = [timedelta(seconds=s) for s in LADDER_SECONDS]
    result = await conn.execute(
        text(
            "SELECT o_intent_id AS intent_id, o_workspace_id AS workspace_id,"
            "       o_reason AS reason"
            " FROM fn_reconciler_sweep("
            "   :lim, CAST(:rungs AS interval[]),"
            "   make_interval(secs => :notify))"
        ),
        {"lim": limit, "rungs": rungs, "notify": notify_after_seconds},
    )
    return [dict(r) for r in result.mappings().all()]


async def _record_no_surface(
    conn, *, intent_id, retry_after_seconds: int
) -> Union[int, str]:
    """No push binding: record the ATTEMPT, and say so upward.

    Two things must both be true and they pull opposite ways. The run must not
    read as a delivery — `outbox.UNDELIVERABLE` is how that reaches the
    ledger. And it must not park a job every 60 s for a condition that will
    stand until someone adds a binding, which is what re-attempting on every
    sweep would do. So the attempt is stamped and re-attempted on `05`'s
    window, exactly the "once, not daily" bound `06` §5 puts on the notice
    itself.

    ``customer_notified`` is deliberately NOT set: it is the permanent latch
    that says the customer WAS told, and nobody was. So the row stays due, and
    the notice lands the moment a surface exists.

    Returns `outbox.UNDELIVERABLE` on the beat that RECORDS the condition, and
    `0` on the beats inside the window that follow it. The distinction being
    drawn is between *a message was owed and could not be sent* and *nothing
    was owed on this beat*, which is the same distinction the whole change is
    about — not between "sent" and "not sent".
    """
    from src.services.target.outbox import UNDELIVERABLE

    fresh = (
        await conn.execute(
            text(
                "UPDATE post_intents"
                " SET last_error ="
                "   COALESCE(last_error, CAST('{\"v\": 1}' AS jsonb))"
                "   || jsonb_build_object('evidence',"
                "        COALESCE(last_error->'evidence', CAST('{}' AS jsonb))"
                "        || jsonb_build_object('notify_attempted_at', now()))"
                " WHERE id = :intent"
                "   AND state = 'review_required'"
                "   AND COALESCE("
                "         CAST(last_error->'evidence'->>'notify_attempted_at'"
                "              AS timestamptz),"
                "         CAST('-infinity' AS timestamptz))"
                "       < now() - make_interval(secs => :age)"
                " RETURNING id"
            ),
            {"intent": str(intent_id), "age": float(retry_after_seconds)},
        )
    ).first()
    if fresh is None:
        # Already recorded inside this window. The condition still stands and
        # is still visible — the `review_required` job from the first attempt
        # and this intent's own stamp both persist — so re-reporting it on
        # every 60 s beat would park a thousand jobs a day for one workspace
        # and bury the signal it exists to raise. Bounded, not silenced.
        return 0

    logger.warning(
        "reconciler: intent %s needs attention but its workspace has NO push"
        " binding — nobody was told (#1090 D5)",
        intent_id,
    )
    return UNDELIVERABLE


async def notify_parked_customer(
    conn,
    *,
    intent_id,
    workspace_id,
    web_app_origin: Optional[str] = None,
    retry_after_seconds: int = 24 * 3600,
) -> Union[int, str]:
    """Tell the workspace a parked post needs attention.

    Returns the number of outbox rows written, or `outbox.UNDELIVERABLE`
    when the workspace has no surface to receive it — **never a bare `0` for
    both**, which is the shape that let two existing producers report a clean
    run to nobody.

    `06` §5's `review_required` row: after `05`'s customer-notification window
    the workspace gets "one workspace notification (\"a post needs attention\",
    deep link to the web queue)". `fn_reconciler_sweep` (059) already selects
    exactly these rows — state `review_required`, past the window, not yet
    notified — and tags them ``notify_window``. **Nothing consumed that tag
    until now**, which is why #1090 D4 had no producer: the door was built and
    its output was fed straight into the ladder.

    **The stamp is an atomic claim, and it MERGES.** Two things matter here.
    The claim (``NOT ... customer_notified`` inside the UPDATE) closes the
    window between the sweep's read and this write — the door's own filter
    cannot, because the loop runs between them. The merge preserves
    ``last_error->'evidence'``: that object carries ``checks``,
    ``last_checked_at`` and the full ``trail``, which is the operator's entire
    inheritance on a parked intent (`06` §5's resolution surface reads it), so
    a ``jsonb_build_object`` rebuild of the kind :func:`_record_evidence` does
    would notify the customer by destroying the evidence.

    **Bindings are read before the claim**, for the reason spelled out at the
    call — the same shape as `scheduler._notice_no_media`, and load-bearing
    here because this latch never reopens.

    *web_app_origin* is the front end's origin (`settings.web_app_origin`).
    **Absent, the notice still fires without a link** — being told late is a
    smaller failure than not being told, and a workspace whose deployment has
    no web origin configured still needs to know a post is stuck.

    **It scopes its own writes, and that is not defensive coding.**
    `reconcile_ambiguous` is a SYSTEM singleton: it carries `workspace_id
    NULL`, so `make_session_for` gives it ``app.tenant_id = ''``. The sweep
    reads cross-tenant because `fn_reconciler_sweep` is SECURITY DEFINER —
    and 059's own comment says the notification WRITE "then runs tenant-scoped
    as svc_worker", which nothing implemented. Under an empty tenant every
    statement below is invisible to `p_tenant`: the UPDATE matches no row,
    `push_bindings` returns nothing, and this reports success having written
    nothing. So the row's workspace is asserted here, through `apply_gucs`
    rather than a hand-rolled `SET LOCAL` — its docstring is explicit that a
    second copy of that call is how a third ships `is_local=false`.
    """
    from src.services.target import outbox, prompts, unit_of_work

    # `02` §4's worker actor, matching `make_session_for`: the governance
    # triggers on post_intents refuse a write that names no actor.
    await unit_of_work.apply_gucs(
        conn, tenant_id=str(workspace_id), actor_kind="system"
    )

    # Bindings BEFORE the claim, and here the ordering matters more than it
    # does for the no-media notice: `customer_notified` is a permanent latch
    # rather than a window, so stamping it for a workspace with no push
    # binding (#1090 D5) would spend the ONE notification `06` §5 allows on an
    # audience that cannot hear it, and the customer would never be told a
    # post is stuck. Left unstamped, the row stays due — it is a genuinely
    # outstanding condition — and the notice lands whenever a surface exists.
    bindings = await prompts.push_bindings(conn, str(workspace_id))
    if not bindings:
        return await _record_no_surface(
            conn, intent_id=intent_id, retry_after_seconds=retry_after_seconds
        )

    claimed = (
        await conn.execute(
            text(
                "UPDATE post_intents"
                " SET last_error ="
                "   COALESCE(last_error, CAST('{\"v\": 1}' AS jsonb))"
                "   || jsonb_build_object('evidence',"
                "        COALESCE(last_error->'evidence', CAST('{}' AS jsonb))"
                "        || jsonb_build_object('customer_notified', true))"
                " WHERE id = :intent"
                "   AND state = 'review_required'"
                "   AND NOT COALESCE("
                "         CAST(last_error->'evidence'->>'customer_notified'"
                "              AS boolean), false)"
                " RETURNING id"
            ),
            {"intent": str(intent_id)},
        )
    ).first()
    if claimed is None:
        return 0

    link = f" {web_app_origin}/dashboard/queue" if web_app_origin else ""
    await outbox.fanout_notification(
        conn,
        workspace_id=workspace_id,
        bindings=bindings,
        intent_id=intent_id,
        text=(
            "\u26a0\ufe0f A post needs attention: it could not be confirmed"
            " as published and is waiting for a decision. Open the queue to"
            f" resolve it.{link}"
        ),
    )
    logger.info(
        "reconciler: parked intent %s past the notify window — notified %d binding(s)",
        intent_id,
        len(bindings),
    )
    return len(bindings)


async def _record_evidence(conn, *, intent_id, checks: int, trail: list) -> None:
    """`last_error.evidence` carries `{checks, last_checked_at}` plus the trail."""
    await conn.execute(
        text(
            "UPDATE post_intents"
            " SET last_error = jsonb_build_object("
            "   'v', 1,"
            "   'evidence', jsonb_build_object("
            "     'checks', CAST(:checks AS int),"
            "     'last_checked_at', now(),"
            "     'trail', CAST(:trail AS jsonb)))"
            " WHERE id = :intent"
        ),
        {"intent": str(intent_id), "checks": checks, "trail": _json(trail)},
    )


def _json(payload) -> str:
    import json

    return json.dumps(payload)


async def reconcile_intent(
    conn,
    *,
    intent_id,
    workspace_id,
    poll: Callable[..., Any],
    stories_check: Optional[Callable[..., Any]] = None,
    mode: str = DEFAULT_MODE,
    checks: int = 0,
    trail: Optional[list] = None,
) -> str:
    """One ladder step for one ambiguous intent. Returns the outcome reached.

    ``"posted"`` / ``"failed"`` terminalize; ``"pending"`` means the ladder
    continues; ``"review_required"`` means the ladder is spent and the intent
    is parked for the operator surface (`06` §5).

    *poll* is injected rather than imported so this is drivable from stubbed
    evidence — the L.3 gate requires exactly that, in both modes.
    """
    trail = list(trail or [])
    status_code = await _maybe_await(poll, intent_id=intent_id)
    trail.append({"status_code": status_code, "check": checks + 1})
    verdict = classify(status_code, mode)

    if verdict == POSITIVE:
        await _terminalize(conn, intent_id=intent_id, state="posted", trail=trail)
        return "posted"
    if verdict == NEGATIVE:
        await _terminalize(conn, intent_id=intent_id, state="failed", trail=trail)
        return "failed"

    if next_delay_seconds(checks + 1) is not None:
        await _record_evidence(
            conn, intent_id=intent_id, checks=checks + 1, trail=trail
        )
        return "pending"

    # Ladder exhausted: one final stories check, the full trail, then park.
    #
    # The stories check is CORROBORATING, NEVER DISPOSITIVE. 0.4 confirmed the
    # 24 h lookback and surfaced two exclusions: responses omit Live Video
    # stories, and a story created by resharing is not returned. So absence
    # from the list is not proof it was never published, and this must not be
    # read as a negative verdict on its own.
    if stories_check is not None:
        trail.append(
            {"stories": await _maybe_await(stories_check, intent_id=intent_id)}
        )
    await _record_evidence(conn, intent_id=intent_id, checks=checks + 1, trail=trail)
    await _park_review_required(conn, intent_id=intent_id)
    return "review_required"


async def _maybe_await(fn, **kwargs):
    out = fn(**kwargs)
    if hasattr(out, "__await__"):
        return await out
    return out


async def _terminalize(conn, *, intent_id, state: str, trail: list) -> None:
    """Terminalize the intent AND the operation row it judged.

    `02` §6: every reconciler verdict also terminalizes the
    ``provider_operations`` row, so no op stays ambiguous after its intent
    resolves and the ``ix_ops_retire`` retention class eventually drains.
    Skipping this is how a table grows a permanently un-retirable class.
    """
    from src.services.target import provider_ops

    await _record_evidence(conn, intent_id=intent_id, checks=len(trail), trail=trail)
    if state == "posted":
        # `ck_posted_complete` refuses a bare state flip, and it is right to:
        # for the `api` route it demands ig_container_id, publish_step =
        # 'effect_confirmed' AND a cap debit. A verdict that set only `state`
        # would leave a posted row that cannot say what it posted. The schema
        # caught this — the first version of this function did exactly that.
        await conn.execute(
            text(
                "UPDATE post_intents"
                " SET state = 'posted', published_via = 'api',"
                "     publish_step = 'effect_confirmed'"
                " WHERE id = :intent AND state = 'publishing_ambiguous'"
            ),
            {"intent": str(intent_id)},
        )
    else:
        await conn.execute(
            text(
                "UPDATE post_intents SET state = :state"
                " WHERE id = :intent AND state = 'publishing_ambiguous'"
            ),
            {"intent": str(intent_id), "state": state},
        )
    result = await conn.execute(
        text(
            "SELECT id FROM provider_operations"
            " WHERE intent_id = :intent AND op_kind = 'publish' AND state = 'ambiguous'"
        ),
        {"intent": str(intent_id)},
    )
    op_state = "succeeded" if state == "posted" else "failed"
    for row in result.mappings().all():
        await provider_ops._advance(
            conn,
            op_id=row["id"],
            to_state=op_state,
            response_ref={"v": 1, "evidence": trail},
            from_state="ambiguous",
        )


async def _park_review_required(conn, *, intent_id) -> None:
    await conn.execute(
        text(
            "UPDATE post_intents SET state = 'review_required'"
            " WHERE id = :intent AND state = 'publishing_ambiguous'"
        ),
        {"intent": str(intent_id)},
    )
