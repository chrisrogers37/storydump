"""Prompt production — the approval card, first-principles (#790 W3).

Two rulings shape this module (Chris, 2026-08-21). **A tap publishes**: the
card's affirmative action is *Post now* — the user contract is post-now, and
whatever queuing the backend does after the tap is invisible to him. **Parity
is out**: the button set serves the next state rather than mirroring legacy,
with one hard floor — the four callbacks production actually uses (autopost,
skip, posted, reject over the last 30 days) all survive, renamed but not
removed. Divergences from the legacy card are enumerated in the PR that
introduced this module, never silent.

The card: **Post now** (only when the workspace can publish via the API —
this module is `workspaces.api_publishing_enabled`'s first production
reader) · **Posted myself** (the matrix's manual-mode path,
awaiting→posted) · **Skip** · **Reject**, plus the zero-backend Instagram
deeplink button. Callback tokens are versioned (`v1:<action>:<intent-uuid>`)
and fit Telegram's 64-byte `callback_data` bound; the inbound half that
parses them is the W4 increment, gated on #854.

`prompt_intent` implements the `02` §4 `scheduled → prompt_pending` edge:
the transition commits in the SAME transaction as its outbox rows ("outbox
rows created for active push bindings, same tx"), one card per active push
binding — and the transition happens whether or not a binding exists. The
advance edge, `prompt_pending → awaiting_approval`, reads "delivered on ≥ 1
binding, **or** workspace has web access": since the web queue (#1033)
every workspace has web access by construction, so the second disjunct is
always true and the sweep advances every `prompt_pending` intent in the
same pass, on no delivery evidence at all. Two consequences, both
deliberate. A workspace with no push binding — the Google-only workspace
milestone 1 exists for — still reaches `awaiting_approval` and simply gets
no card; before #1033 its intents never left `scheduled` and the reaper
expired them. And `prompt_pending → failed` ("no reachable surface") has no
producer any more: an intent whose every card failed is still actionable on
the web until a person acts or `approval_ttl` expires it. That edge stays
seeded in `055`; nothing drives it.
"""

from __future__ import annotations

import logging
import re

from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text

from src.services.target import intent_ledger, outbox
from src.services.target.callback_tokens import ACTIONS, token as _token

logger = logging.getLogger(__name__)

INSTAGRAM_DEEPLINK_URL = "https://www.instagram.com/"

_ACTIONS_API = ACTIONS
_ACTIONS_MANUAL = ("posted", "skip", "reject")

#: The word a card shows for each state a tap can find it in — the outcome
#: line (phase 1 of the 2026-09-09 tap plan, step 5) and the answer a repeat
#: tap gets both read from here, so the two never disagree.
OUTCOME_WORDS = {
    "scheduled": "🗓 Scheduled",
    "prompt_pending": "⏳ Awaiting approval",
    "awaiting_approval": "⏳ Awaiting approval",
    "approved": "✅ Approved",
    "publishing": "🚀 Posting…",
    "publishing_ambiguous": "🚀 Posting… (confirming)",
    "review_required": "👀 Needs review",
    "posted": "✅ Posted",
    "skipped": "⏭️ Skipped",
    "rejected": "🚫 Rejected",
    "expired": "⌛ Expired — slot passed",
    "cancelled": "🚫 Cancelled",
    "failed": "⚠️ Failed",
    "account_disabled": "⏸ Account disabled",
}


def stamp(at: datetime, tz: str) -> str:
    """`%Y-%m-%d %H:%M <tz>` in the WORKSPACE's timezone — the slot line and
    the outcome line share this one spelling. A zone Postgres accepted
    (`fn_safe_tz`: `PST`, `UTC+5`) that the IANA database does not know
    degrades to UTC, as the door does — one workspace's zone must never fail
    a tap or a sweep (adversarial review of #1271)."""
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        zone, tz = ZoneInfo("UTC"), "UTC"
    return f"{at.astimezone(zone).strftime('%Y-%m-%d %H:%M')} {tz}"


def outcome_line(state: str, *, by: Optional[str], at: datetime, tz: str) -> str:
    """What a settled card says under its header: the state's word, who, when."""
    word = OUTCOME_WORDS.get(state, state)
    who = f" by {by}" if by else ""
    return f"{word}{who} · {stamp(at, tz)}"


_LABELS = {
    "post": "🚀 Post now",
    "posted": "✅ Posted myself",
    "skip": "⏭️ Skip",
    "reject": "🚫 Reject",
}


def _canonical_fraction(value: str) -> str:
    """Pad a 1-5 digit fractional-seconds field to the canonical 6 digits.

    Postgres strips trailing fractional zeros when rendering a timestamptz
    into text/jsonb, so a microsecond value can arrive at any width 1-6.
    Widths other than 3 and 6 are rejected by ``fromisoformat`` at the
    repository's 3.10 floor, so the string is canonicalized before parsing
    rather than teaching this module which widths which interpreter accepts.
    Width 0 (no fraction) and width 6 pass through untouched.
    """
    return re.sub(
        r"\.(\d{1,5})(?!\d)", lambda m: "." + m.group(1).ljust(6, "0"), value, count=1
    )


def render_card(intent: dict, *, api_publishing_enabled: bool) -> dict:
    """The approval-prompt payload in the transport contract
    (``{"v": 2, "text", "reply_markup", "media"?, "caption"?}``).

    *intent* carries ``intent_id``, ``file_name``, ``media_kind``,
    ``schedule_slot_at`` (ISO string or datetime) and ``tz``; with
    ``source_id`` + ``provider_file_ref`` (and ``workspace_id``, ``mime_type``,
    ``handle``) the card carries the MEDIA: the transport fetches the bytes and
    sends the photo or video with ``caption`` — the account and the slot, as
    the legacy card did (owner, 2026-09-08) — and ``text`` is the card when it
    cannot. The slot renders in the WORKSPACE's timezone — a solo user reads
    his own clock, never UTC.
    """
    intent_id = str(intent["intent_id"])
    slot = intent.get("schedule_slot_at")
    if isinstance(slot, str):
        slot = datetime.fromisoformat(_canonical_fraction(slot))
    tz = intent.get("tz") or "UTC"
    slot_line = f"Slot: {stamp(slot, tz)}"
    file_name = intent.get("file_name") or "media"
    text = f"📸 {file_name} ({intent.get('media_kind', '?')})\n{slot_line}"
    actions = _ACTIONS_API if api_publishing_enabled else _ACTIONS_MANUAL
    rows = [
        [
            {"text": _LABELS[a], "callback_data": _token(a, intent_id)}
            for a in actions[:2]
        ],
        [
            {"text": _LABELS[a], "callback_data": _token(a, intent_id)}
            for a in actions[2:]
        ],
        [{"text": "📱 Open Instagram", "url": INSTAGRAM_DEEPLINK_URL}],
    ]
    payload: dict = {
        "v": 2,
        "text": text,
        "reply_markup": {"inline_keyboard": [r for r in rows if r]},
    }
    if intent.get("provider_file_ref") and intent.get("source_id"):
        # The card IS the photo (owner, 2026-09-08 — legacy parity): the
        # transport fetches these bytes under the workspace grant and sends
        # them with `caption`; `text` stays as the card when it cannot.
        handle = intent.get("handle")
        # Bounded: Telegram captions stop at 1024 characters, and a Drive
        # file name has no bound of its own.
        who = f"@{handle}" if handle else file_name[:200]
        payload["media"] = {
            "workspace_id": str(intent["workspace_id"]),
            "source_id": str(intent["source_id"]),
            "ref": str(intent["provider_file_ref"]),
            "kind": intent.get("media_kind"),
            "mime": intent.get("mime_type"),
            "file_name": file_name,
        }
        payload["caption"] = f"📸 {who}\n{slot_line}"
    return payload


async def prompt_intent(session, intent_row: dict, bindings: list) -> None:
    """`scheduled → prompt_pending` + one card per active push binding, in
    the CALLER's transaction — and the transition happens whether or not a
    binding exists: the web queue is the surface (module docstring).
    """
    intent_id = str(intent_row["id"])
    await intent_ledger.transition(session, intent_id, "prompt_pending")
    if not bindings:
        return
    payload = render_card(
        {
            "intent_id": intent_id,
            "workspace_id": intent_row.get("workspace_id"),
            "file_name": intent_row.get("file_name"),
            "media_kind": intent_row.get("media_kind"),
            "mime_type": intent_row.get("mime_type"),
            "source_id": intent_row.get("source_id"),
            "provider_file_ref": intent_row.get("provider_file_ref"),
            "handle": intent_row.get("handle"),
            "schedule_slot_at": intent_row.get("schedule_slot_at"),
            "tz": intent_row.get("tz"),
        },
        api_publishing_enabled=bool(intent_row.get("api_publishing_enabled")),
    )
    for binding in bindings:
        await outbox.enqueue(
            session,
            workspace_id=str(intent_row["workspace_id"]),
            binding_id=str(binding["id"]),
            kind="approval_prompt",
            payload=payload,
            intent_id=intent_id,
        )


async def push_bindings(session, workspace_id: str) -> list[str]:
    """Binding ids that can carry a push to this workspace.

    ONE owner for the predicate: the W3 sweep and the W5e reauth prompt both
    route on "where can we say this", and two spellings of that question is
    how the two surfaces drift apart.
    """
    rows = (
        (
            await session.execute(
                text(
                    "SELECT id FROM channel_bindings"
                    " WHERE workspace_id = :ws AND state = 'active'"
                    "   AND channel LIKE 'telegram%'"
                ),
                {"ws": str(workspace_id)},
            )
        )
        .mappings()
        .all()
    )
    return [str(r["id"]) for r in rows]


async def sweep_settled_cards(session, *, limit: int = 50) -> int:
    """Cards end in EVERY terminal state (phase 1 of the 2026-09-09 tap plan,
    step 7): whoever ended the intent — the reaper's expiry, the pipeline's
    `posted`, the worker's cancellation — its live cards lose their buttons
    and gain the terminal line. One mechanism for every writer, run on the
    prompt sweep's cadence; a tap's own supersede is immediate and this is the
    backstop. Returns the intents healed."""
    rows = (
        (
            await session.execute(
                text(
                    # Active bindings only: a revoked group's cards cannot be
                    # edited (the bot is gone) and would otherwise be
                    # re-selected every beat, starving the sweep's LIMIT.
                    "SELECT o.intent_id, o.workspace_id, o.binding_id, i.state,"
                    "       i.entered_state_at, w.tz, min(o.created_at) AS since"
                    "  FROM channel_outbox o"
                    "  JOIN channel_bindings b ON b.id = o.binding_id AND b.state = 'active'"
                    "  JOIN post_intents i ON i.id = o.intent_id"
                    "  JOIN workspaces w ON w.id = i.workspace_id"
                    " WHERE o.kind = 'approval_prompt'"
                    "   AND o.state IN ('pending', 'sending', 'sent', 'ambiguous')"
                    "   AND i.state IN ('posted','skipped','rejected','expired',"
                    "                   'failed','cancelled')"
                    " GROUP BY o.intent_id, o.workspace_id, o.binding_id, i.state,"
                    "          i.entered_state_at, w.tz"
                    " ORDER BY since"
                    " LIMIT :lim"
                ),
                {"lim": int(limit)},
            )
        )
        .mappings()
        .all()
    )
    healed = 0
    for row in rows:
        # One row's fault (a zone, a lost binding) must not fail the reaper's
        # transaction for every workspace: a savepoint per row, and on.
        try:
            async with session.begin_nested():
                settled = await intent_ledger.settlement(
                    session,
                    workspace_id=str(row["workspace_id"]),
                    intent_id=str(row["intent_id"]),
                )
                by = None
                if settled.get("by_user_id"):
                    from src.services.target import identity  # noqa: PLC0415 — cycle

                    by = await identity.display_name_for(
                        session, user_id=settled["by_user_id"]
                    )
                line = outcome_line(
                    str(row["state"]),
                    by=by,
                    at=settled.get("at") or row["entered_state_at"],
                    tz=str(row["tz"] or "UTC"),
                )
                await outbox.supersede_all(
                    session,
                    workspace_id=str(row["workspace_id"]),
                    binding_id=str(row["binding_id"]),
                    intent_id=str(row["intent_id"]),
                    outcome_text=line,
                )
        except Exception:  # noqa: BLE001 — isolated, logged, the sweep goes on
            logger.exception(
                "settled-card sweep: intent %s binding %s skipped",
                row["intent_id"],
                row["binding_id"],
            )
            continue
        healed += 1
    return healed


async def sweep_due_prompts(session, *, limit: int = 50) -> dict:
    """The prompt sweep — the `02` §4 matrix legs, idempotent, in the
    caller's transaction. Two legs, two counts:

    - ``prompted``: due `scheduled` intents (slot arrived, workspace active
      and unpaused) gain their transition + cards via :func:`prompt_intent`.
      This is also the correctness backstop for the fast path in the
      `plan_slot` adapter — an intent minted before this module existed, or
      whose prompt crashed mid-way, is picked up here.
    - ``advanced``: every `prompt_pending` intent moves to
      `awaiting_approval` — "prompt delivered on ≥ 1 binding, or workspace
      has web access", and web access is universal (module docstring), so
      this leg runs in the same pass as ``prompted`` and an intent is
      actionable the moment it is prompted. The guard makes a lost race
      benign.
    """
    counts = {"prompted": 0, "advanced": 0}

    due = (
        (
            await session.execute(
                text(
                    "SELECT i.id, i.workspace_id, i.schedule_slot_at,"
                    "       m.file_name, m.media_kind, m.mime_type,"
                    "       m.source_id, m.provider_file_ref, a.handle, w.tz,"
                    "       w.api_publishing_enabled"
                    "  FROM post_intents i"
                    "  JOIN workspaces w ON w.id = i.workspace_id"
                    "  JOIN media_items m ON m.id = i.media_item_id"
                    "   AND m.workspace_id = i.workspace_id"
                    "  LEFT JOIN ig_accounts a ON a.id = i.ig_account_id"
                    "   AND a.workspace_id = i.workspace_id"
                    " WHERE i.state = 'scheduled' AND i.schedule_slot_at <= now()"
                    "   AND w.state = 'active' AND NOT w.is_paused"
                    " ORDER BY i.schedule_slot_at LIMIT :lim"
                ),
                {"lim": limit},
            )
        )
        .mappings()
        .all()
    )
    bindings_by_workspace: dict[str, list[str]] = {}  # same tx, same answer
    for row in due:
        ws = str(row["workspace_id"])
        if ws not in bindings_by_workspace:
            bindings_by_workspace[ws] = await push_bindings(session, ws)
        await prompt_intent(
            session, dict(row), [{"id": b} for b in bindings_by_workspace[ws]]
        )
        counts["prompted"] += 1

    advanced = (
        (
            await session.execute(
                text(
                    "SELECT i.id FROM post_intents i"
                    " WHERE i.state = 'prompt_pending'"
                    " LIMIT :lim"
                ),
                {"lim": limit},
            )
        )
        .scalars()
        .all()
    )
    for intent_id in advanced:
        try:
            await intent_ledger.transition(session, str(intent_id), "awaiting_approval")
            counts["advanced"] += 1
        except intent_ledger.IntentTransitionRefused:
            pass  # raced by the fast path — the state is already right

    return counts
