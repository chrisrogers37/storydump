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

`prompt_intent` implements the `02` §4 matrix edge exactly: `scheduled →
prompt_pending` commits in the SAME transaction as its outbox rows ("outbox
rows created for active push bindings, same tx"), one card per active push
binding. No active push binding → no transition at all: an intent parked in
`prompt_pending` with no card anywhere would be unreachable by any tap, so
the no-surface case stays `scheduled` for a later sweep to retry or the
reaper to expire.
"""

from __future__ import annotations

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
        slot = datetime.fromisoformat(slot)
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
    the CALLER's transaction. Returns the outbox row ids (empty when there is
    no surface to prompt on — in which case no transition happened either).
    """
    if not bindings:
        return []
    intent_id = str(intent_row["id"])
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
    await intent_ledger.transition(session, intent_id, "prompt_pending")
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


async def sweep_due_prompts(session, *, limit: int = 50) -> dict:
    """The prompt sweep — the `02` §4 matrix legs, idempotent, in the
    caller's transaction. Three legs, three counts:

    - ``prompted``: due `scheduled` intents (slot arrived, workspace active
      and unpaused) gain their transition + cards via :func:`prompt_intent`.
      This is also the correctness backstop for the fast path in the
      `plan_slot` adapter — an intent minted before this module existed, or
      whose prompt crashed mid-way, is picked up here.
    - ``advanced``: `prompt_pending` intents with ≥1 SENT card move to
      `awaiting_approval` ("prompt delivered on ≥ 1 binding"). The guard
      makes a lost race benign.
    - ``failed_no_surface``: `prompt_pending` intents whose cards exist and
      ALL landed `failed` move to `failed` ("no reachable surface").
    """
    counts = {"prompted": 0, "advanced": 0, "failed_no_surface": 0}

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
        bindings = (
            (
                await session.execute(
                    text(
                        "SELECT id FROM channel_bindings"
                        " WHERE workspace_id = :ws AND state = 'active'"
                        "   AND channel LIKE 'telegram%'"
                    ),
                    {"ws": str(row["workspace_id"])},
                )
            )
            .mappings()
            .all()
        )
        minted = await prompt_intent(session, dict(row), [dict(b) for b in bindings])
        if minted:
            counts["prompted"] += 1

    advanced = (
        (
            await session.execute(
                text(
                    "SELECT i.id FROM post_intents i"
                    " WHERE i.state = 'prompt_pending'"
                    "   AND EXISTS (SELECT 1 FROM channel_outbox o"
                    "                WHERE o.intent_id = i.id"
                    "                  AND o.kind = 'approval_prompt'"
                    "                  AND o.state = 'sent')"
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

    dead = (
        (
            await session.execute(
                text(
                    "SELECT i.id FROM post_intents i"
                    " WHERE i.state = 'prompt_pending'"
                    "   AND EXISTS (SELECT 1 FROM channel_outbox o"
                    "                WHERE o.intent_id = i.id"
                    "                  AND o.kind = 'approval_prompt')"
                    "   AND NOT EXISTS (SELECT 1 FROM channel_outbox o"
                    "                    WHERE o.intent_id = i.id"
                    "                      AND o.kind = 'approval_prompt'"
                    "                      AND o.state <> 'failed')"
                    " LIMIT :lim"
                ),
                {"lim": limit},
            )
        )
        .scalars()
        .all()
    )
    for intent_id in dead:
        try:
            await intent_ledger.transition(session, str(intent_id), "failed")
            counts["failed_no_surface"] += 1
        except intent_ledger.IntentTransitionRefused:
            pass

    return counts
