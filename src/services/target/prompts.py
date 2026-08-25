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

import re

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import text

from src.services.target import intent_ledger, outbox

INSTAGRAM_DEEPLINK_URL = "https://www.instagram.com/"

_ACTIONS_API = ("post", "posted", "skip", "reject")
_ACTIONS_MANUAL = ("posted", "skip", "reject")

_LABELS = {
    "post": "🚀 Post now",
    "posted": "✅ Posted myself",
    "skip": "⏭️ Skip",
    "reject": "🚫 Reject",
}


def _token(action: str, intent_id: str) -> str:
    token = f"v1:{action}:{intent_id}"
    if len(token.encode()) > 64:  # Telegram's callback_data bound
        raise ValueError(f"callback token exceeds 64 bytes: {token!r}")
    return token


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
    (``{"v": 1, "text": ..., "reply_markup": ...}``).

    *intent* carries ``intent_id``, ``file_name``, ``media_kind``,
    ``schedule_slot_at`` (ISO string or datetime) and ``tz``. The slot renders
    in the WORKSPACE's timezone — a solo user reads his own clock, never UTC.
    """
    intent_id = str(intent["intent_id"])
    slot = intent.get("schedule_slot_at")
    if isinstance(slot, str):
        slot = datetime.fromisoformat(_canonical_fraction(slot))
    tz = intent.get("tz") or "UTC"
    local = slot.astimezone(ZoneInfo(tz))
    text = (
        f"📸 {intent.get('file_name', 'media')} ({intent.get('media_kind', '?')})\n"
        f"Slot: {local.strftime('%Y-%m-%d %H:%M')} {tz}"
    )
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
    return {
        "v": 1,
        "text": text,
        "reply_markup": {"inline_keyboard": [r for r in rows if r]},
    }


async def prompt_intent(session, intent_row: dict, bindings: list) -> list:
    """`scheduled → prompt_pending` + one card per active push binding, in
    the CALLER's transaction. Returns the outbox row ids — empty when there
    is no push binding, in which case the transition still happened: the web
    queue is the surface (module docstring).
    """
    intent_id = str(intent_row["id"])
    await intent_ledger.transition(session, intent_id, "prompt_pending")
    if not bindings:
        return []
    payload = render_card(
        {
            "intent_id": intent_id,
            "file_name": intent_row.get("file_name"),
            "media_kind": intent_row.get("media_kind"),
            "schedule_slot_at": intent_row.get("schedule_slot_at"),
            "tz": intent_row.get("tz"),
        },
        api_publishing_enabled=bool(intent_row.get("api_publishing_enabled")),
    )
    out_ids = []
    for binding in bindings:
        out_ids.append(
            await outbox.enqueue(
                session,
                workspace_id=str(intent_row["workspace_id"]),
                binding_id=str(binding["id"]),
                kind="approval_prompt",
                payload=payload,
                intent_id=intent_id,
            )
        )
    return out_ids


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
                    "       m.file_name, m.media_kind, w.tz,"
                    "       w.api_publishing_enabled"
                    "  FROM post_intents i"
                    "  JOIN workspaces w ON w.id = i.workspace_id"
                    "  JOIN media_items m ON m.id = i.media_item_id"
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
    for row in due:
        binding_ids = await push_bindings(session, str(row["workspace_id"]))
        await prompt_intent(session, dict(row), [{"id": b} for b in binding_ids])
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
