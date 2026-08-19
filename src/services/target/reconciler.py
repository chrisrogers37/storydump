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
from typing import Any, Callable, Optional

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
    """
    rungs = "{" + ",".join(f'"{s} seconds"' for s in LADDER_SECONDS) + "}"
    result = await conn.execute(
        text(
            "SELECT o_intent_id, o_workspace_id, o_reason"
            " FROM fn_reconciler_sweep("
            "   :lim, CAST(:rungs AS interval[]),"
            "   make_interval(secs => :notify))"
        ),
        {"lim": limit, "rungs": rungs, "notify": notify_after_seconds},
    )
    return [dict(r) for r in result.mappings().all()]


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
