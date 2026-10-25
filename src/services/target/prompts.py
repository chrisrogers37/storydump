"""Prompt production — the approval card, first-principles.

Two rules shape this module. **A tap publishes**: the
card's affirmative action is *Post now* — the user contract is post-now, and
whatever queuing the backend does after the tap is invisible to him. **The
button set serves the next state**, with one hard floor: the four callbacks
production uses (autopost, skip, posted, reject) all exist.

The card: **Post now** (only when the workspace can publish via the API —
this module is `workspaces.api_publishing_enabled`'s first production
reader) · **Posted myself** (the manual-mode path, awaiting→posted) ·
**Skip** · **Reject**, plus the zero-backend Instagram deeplink button.
Callback tokens are versioned (`v1:<action>:<intent-uuid>`) and fit
Telegram's 64-byte `callback_data` bound; the inbound half that parses them
is the tap.

`prompt_intent` implements the `scheduled → prompt_pending` edge:
the transition commits in the SAME transaction as its outbox rows ("outbox
rows created for active push bindings, same tx"), one card per binding a push
may reach — and the transition happens whether or not a binding exists. The
advance edge, `prompt_pending → awaiting_approval`, reads "delivered on ≥ 1
binding, **or** workspace has web access": with the web queue
every workspace has web access by construction, so the second disjunct is
always true and the sweep advances every `prompt_pending` intent in the
same pass, on no delivery evidence at all. Two consequences, both
deliberate. A workspace with no push binding — a Google-only workspace —
still reaches `awaiting_approval` and simply gets
no card. And `prompt_pending → failed` ("no reachable surface") has no
producer: an intent whose every card failed is still actionable on
the web until a person acts or `approval_ttl` expires it. That edge stays
seeded in `intent_ledger.py`; nothing drives it.

A PLANNED story (`origin = 'planned'`: a person chose its time) ends
unserved only out loud (`fn_planned_serve` / `fn_planned_misses`). The due door serves it while it can be served
and within the worker's late window, its card naming who scheduled it; the
miss door (`fn_planned_misses`) lists the rest, and `sweep_planned_misses`
expires each with its reason and tells the bound chats in the same
transaction. The reaper's slot expiry takes cadence rows only, so nothing
else can end a planned story before its time is served or missed.
"""

from __future__ import annotations

import json
import logging

from datetime import datetime, timedelta
from typing import Mapping, Optional, Union

from sqlalchemy import text

from src.services.target import (
    auto_approval,
    bindings,
    intent_ledger,
    outbox,
    readers,
    vocabulary,
)
from src.services.target.prompt_rendering import (
    FILE_NAME_BOUND as FILE_NAME_BOUND,
    INSTAGRAM_DEEPLINK_URL as INSTAGRAM_DEEPLINK_URL,
    LIVE_FOR_A_CARD as LIVE_FOR_A_CARD,
    MENU_ACTIONS as MENU_ACTIONS,
    MISS_REASONS as MISS_REASONS,
    OUTCOME_WORDS as OUTCOME_WORDS,
    REVIEW_ACTIONS as REVIEW_ACTIONS,
    RULE_LINE_STATES as RULE_LINE_STATES,
    SERIES_ENDED as SERIES_ENDED,
    SERIES_STOPPED as SERIES_STOPPED,
    SERIES_WORDS as SERIES_WORDS,
    SERVED_LATE_AFTER as SERVED_LATE_AFTER,
    SETTINGS_PATH as SETTINGS_PATH,
    SETTLED_CARD_STATES as SETTLED_CARD_STATES,
    account_label as account_label,
    block_note as block_note,
    card_keyboard as card_keyboard,
    keyboard_of as keyboard_of,
    menu_keyboard as menu_keyboard,
    missed_notice as missed_notice,
    outcome_line as outcome_line,
    outcome_word as outcome_word,
    render_card as render_card,
    sends_settled as sends_settled,
    settings_url as settings_url,
    stamp as stamp,
    waiting_line as waiting_line,
)
from src.utils.datetime_utils import utcnow

logger = logging.getLogger(__name__)


#: How long the settled-card sweep leaves a card that is sent settled in
#: `pending`, for the sender to claim (`sweep_settled_cards`). The sender
#: claims a card within seconds; ten minutes covers a backlog, and then the
#: card is retired as any other, so a parked sender cannot hold the sweep.
SETTLED_CARD_GRACE = timedelta(minutes=10)


async def settled_line(
    session, settled: Mapping, *, tz: str, at: datetime, state: Optional[str] = None
) -> str:
    """The outcome line for a settlement (`intent_ledger.settlement`): the
    state's word (*state*'s, when given), who moved it — a person as a shared
    chat may see them, or the rule that approved it — and when, *at* standing
    in when the settlement has no time. The one spelling for the settled card,
    the settled-card sweep and the sender's after-send edit."""
    by = None
    if settled.get("by_user_id"):
        from src.services.target import identity  # noqa: PLC0415 — cycle

        by = await identity.display_name_for(session, user_id=settled["by_user_id"])
    return outcome_line(
        state or settled["state"],
        by=by,
        at=settled.get("at") or at,
        tz=tz,
        published_via=settled.get("published_via"),
        rule=settled.get("approval_rule"),
    )


async def _still_due(session, row: dict) -> bool:
    """Whether a story is still as the door read it: in its state, not
    flagged for a cancel, at its time. The door read takes no lock, so the
    row is locked here and read again before it is served: a reschedule, a
    cancel or another sweep that got the row first changed it, and it waits
    for the next reading; one that comes after waits on this lock. The miss
    leg makes the same check in its UPDATE."""
    found = await readers.row(
        session,
        "SELECT state, cancel_requested, schedule_slot_at FROM post_intents"
        " WHERE id = :id AND workspace_id = :ws FOR UPDATE",
        id=str(row["id"]),
        ws=str(row["workspace_id"]),
    )
    return (
        found is not None
        and found["state"] == row["state"]
        and not found["cancel_requested"]
        and found["schedule_slot_at"] == row["schedule_slot_at"]
    )


def _sweep_order(row: dict) -> tuple:
    """The order both sweeps visit their rows in: the workspace, then the due
    time, then the id, which makes it total. Each sweep locks a row as it
    reaches it (`_still_due`, the miss leg's UPDATE), so one order keeps the
    two sweeps from taking the same rows' locks in opposite orders."""
    return (str(row["workspace_id"]), row["schedule_slot_at"], str(row["id"]))


async def prompt_intent(session, intent_row: dict, bindings: list) -> None:
    """`scheduled → prompt_pending` + one card per binding in *bindings*, in
    the CALLER's transaction. The sweep passes :func:`push_bindings`' answer,
    the bindings a push may reach. The transition happens whether or not a
    binding exists: the web queue is the surface (module docstring).
    """
    intent_id = str(intent_row["id"])
    await intent_ledger.transition(session, intent_id, "prompt_pending")
    if not bindings:
        return
    payload = _card_for(intent_row, intent_id=intent_id)
    for binding in bindings:
        await outbox.enqueue(
            session,
            workspace_id=str(intent_row["workspace_id"]),
            binding_id=str(binding["id"]),
            kind="approval_prompt",
            payload=payload,
            intent_id=intent_id,
        )


#: Everything a card is rendered from — the one spelling for the prompt sweep
#: and for a card rendered again at send time; each caller adds its WHERE.
_CARD_SELECT = (
    "SELECT i.id, i.state, i.workspace_id, i.schedule_slot_at,"
    "       m.file_name, m.media_kind, m.mime_type,"
    "       m.source_id, m.provider_file_ref, a.handle, w.tz,"
    "       w.api_publishing_enabled, i.origin, i.scheduled_by_user_id"
    "  FROM post_intents i"
    "  JOIN workspaces w ON w.id = i.workspace_id"
    "  JOIN media_items m ON m.id = i.media_item_id"
    "   AND m.workspace_id = i.workspace_id"
    "  LEFT JOIN ig_accounts a ON a.id = i.ig_account_id"
    "   AND a.workspace_id = i.workspace_id"
)


# Keep the planned-door's core SELECT contract. Provider is routing metadata,
# added by the local reader without altering that door's return schema.
_CARD_WITH_PROVIDER = (
    "SELECT card.*, s.provider AS source_provider FROM (" + _CARD_SELECT + ") card "
    "JOIN media_sources s ON s.workspace_id=card.workspace_id AND s.id=card.source_id"
)


def _card_for(row, *, intent_id: str, note: Optional[str] = None) -> dict:
    """The card for one `_CARD_SELECT` row (`render_card` reads the keys it
    needs and ignores the rest)."""
    card = render_card(
        {**row, "intent_id": intent_id},
        api_publishing_enabled=bool(row.get("api_publishing_enabled")),
        note=note,
    )
    if row.get("source_provider") == "instagram_past_posts" and "media" in card:
        if (
            row.get("state") == "review_required"
            or row.get("state") in intent_ledger.TERMINAL_STATES
        ):
            # Reviews/terminal outcomes never request replacement past-posts bytes.
            card.pop("media", None)
            card.pop("caption", None)
            if row.get("state") in intent_ledger.TERMINAL_STATES:
                card.pop("reply_markup", None)
        else:
            # A past-posts card's bytes are the server-selected intent's, never a
            # stored reference (`worker._card_media_fetch`): the block names the
            # intent and leaves out `ref`, which for a past-posts item is the Graph id.
            card["media"].pop("ref", None)
            card["media"]["intent_id"] = str(intent_id)
    return card


def _settled_card(row, *, intent_id: str, line: str) -> dict:
    """The card of a story a rule approved, sent settled: the card as
    rendered, and its line in ``outcome_text`` — the outbox's own record of
    the line a card carries. It leaves with the keyboard the sender gives
    every approval card: for a settled story, ⚙️ Configure alone,
    or none once its item is removed. The transport sends the line under the
    card's text; a later edit replaces it rather than stacking under it,
    since the card's text stays its header; the sender records the card
    `superseded`; and its after-send edit heals it only when the line it would
    write is not the one the card already carries."""
    card = _card_for(row, intent_id=intent_id)
    card["outcome_text"] = line
    return card


#: What a card's keyboard is made of, and what Configure acts on: the story's
#: state and pending cancel, its item (the item's own state, its id and its
#: Auto-approve flag), and whether the workspace posts through the API. Every
#: table is bound on the workspace.
_CARD_FACTS = (
    "SELECT i.state, i.cancel_requested, i.media_item_id, m.state AS item_state,"
    "       m.auto_approve, w.api_publishing_enabled"
    "  FROM post_intents i"
    "  JOIN media_items m ON m.workspace_id = :ws AND m.id = i.media_item_id"
    "  JOIN workspaces w ON w.id = :ws"
    " WHERE i.workspace_id = :ws AND i.id = :id"
)


async def card_facts(session, *, workspace_id: str, intent_id: str) -> Optional[dict]:
    """A card's story as it is now (`_CARD_FACTS`), or None when this session
    cannot see it: the one read behind a card's keyboard — the sender's at
    every send and edit of an approval card, and Configure's at
    every tap."""
    return await readers.row(
        session, _CARD_FACTS, ws=str(workspace_id), id=str(intent_id)
    )


async def card_keyboard_for(
    session, *, workspace_id: str, intent_id: str
) -> Optional[dict]:
    """A card's keyboard for its story as it is now: the sender's, at every
    send and every edit of an approval card. None when the story
    is gone, or carries no keyboard."""
    facts = await card_facts(session, workspace_id=workspace_id, intent_id=intent_id)
    return None if facts is None else keyboard_of(intent_id, facts)


async def _scheduler_name(
    session, user_id, workspace_id, names: Optional[dict] = None
) -> Optional[str]:
    """Who scheduled a planned story, as a shared chat may see them:
    `identity.member_name_for`, the one spelling of that rule (never an
    email). None when the user is gone, or no longer a member of the
    workspace. A sweep passes *names* to look each scheduler up once
    per workspace."""
    if user_id is None:
        return None
    key = (str(workspace_id), str(user_id))
    if names is not None and key in names:
        return names[key]
    from src.services.target import identity  # noqa: PLC0415 — cycle

    name = await identity.member_name_for(session, user_id=key[1], workspace_id=key[0])
    if names is not None:
        names[key] = name
    return name


async def _with_scheduler(session, row: Mapping) -> dict:
    """*row* as a dict and, when it is planned, who scheduled it
    (``scheduled_by``, as a shared chat may see them), its repeat rule
    (``repeat``, repeat rules) and its account's own zone (``repeat_tz``, NULL
    when it keeps the workspace's), in one read. Neither the due door nor
    `_CARD_SELECT` carries them, and both card paths, the sweep and the
    send-time render, come through here. The name is the scheduler's only
    while they are still a member of the workspace."""
    row = dict(row)
    if row.get("origin") == "planned":
        from src.services.target import identity  # noqa: PLC0415 — cycle

        scheduled_by = identity.member_name_sql(
            "i.scheduled_by_user_id", "i.workspace_id"
        )
        found = await readers.row(
            session,
            f"SELECT {intent_ledger.repeat_sql('i.')} AS repeat, a.tz AS repeat_tz,"
            f"       {scheduled_by} AS scheduled_by"
            "  FROM post_intents i JOIN ig_accounts a ON a.id = i.ig_account_id"
            " WHERE i.id = :id AND i.workspace_id = :ws",
            id=str(row["id"]),
            ws=str(row["workspace_id"]),
        )
        row.update(found or {"repeat": None, "repeat_tz": None, "scheduled_by": None})
    return row


async def _block_note_for(session, row: Mapping, *, intent_id: str) -> Optional[str]:
    """A waiting card's block note, from the decision's own facts, read in
    a savepoint of its own: the note is advisory, so a read that fails, or a
    note that cannot be rendered, sends the card without it and never holds
    the card, or the chat's queue behind it, back. The serve step isolates the
    same read."""
    try:
        async with session.begin_nested():
            found = await auto_approval.facts(
                session, workspace_id=str(row["workspace_id"]), intent_id=intent_id
            )
        # Outside the savepoint, which holds the read alone; inside the guard.
        if found is None:
            return None
        return block_note(auto_approval.decide(found), handle=row.get("handle"))
    except Exception:  # noqa: BLE001 — advisory: the card goes without its note
        logger.warning(
            "card for story %s: its block note could not be read; sent without it",
            intent_id,
            exc_info=True,
        )
        return None


async def rerender_prompt(session, *, intent_id: str) -> Optional[dict]:
    """The card for *intent_id* rendered from the intent AS IT IS NOW — at
    send time: the workspace's current buttons, the slot in its
    clock. None when the slot has moved on (answered, expired, ended) — such
    a card is never sent. Three exceptions: a waiting card says
    when a block stopped a rule from approving it, a story a rule
    approved is sent settled (`sends_settled`), and a story parked for
    review is sent as its review card, which is the workspace's to resolve."""
    row = (
        (
            await session.execute(
                text(_CARD_WITH_PROVIDER + " WHERE card.id = :id"), {"id": intent_id}
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    if row.get("state") in LIVE_FOR_A_CARD:
        from src.services.target import source_policy

        verdict = await source_policy.intent_suitability(
            session, workspace_id=str(row["workspace_id"]), intent_id=intent_id
        )
        if verdict is None or not verdict["suitable"]:
            raise source_policy.MediaAdmissionPending(
                verdict["reason"] if verdict else "not_found"
            )
        return _card_for(
            await _with_scheduler(session, row),
            intent_id=intent_id,
            note=await _block_note_for(session, row, intent_id=intent_id),
        )
    in_review = row.get("state") == "review_required"
    if not in_review and row.get("state") not in SETTLED_CARD_STATES:
        return None
    settled = await intent_ledger.settlement(
        session, workspace_id=str(row["workspace_id"]), intent_id=intent_id
    )
    if not in_review and not sends_settled(
        settled["state"], settled.get("approval_rule")
    ):
        return None
    line = await settled_line(
        session, settled, tz=str(row.get("tz") or "UTC"), at=utcnow()
    )
    return _settled_card(
        await _with_scheduler(session, row), intent_id=intent_id, line=line
    )


async def push_bindings(session, workspace_id: str) -> list[str]:
    """Binding ids that can carry a push to this workspace.

    ONE owner for the predicate: the prompt sweep and the reauth prompt both
    route on "where can we say this", and two spellings of that question is
    how the two surfaces drift apart.
    """
    rows = (
        (
            await session.execute(
                text(
                    "SELECT id FROM channel_bindings"
                    f" WHERE workspace_id = :ws AND {bindings.DELIVERABLE_BINDING_WHERE}"
                ),
                {"ws": str(workspace_id)},
            )
        )
        .mappings()
        .all()
    )
    return [str(r["id"]) for r in rows]


async def sweep_settled_cards(session, *, limit: int = 50) -> int:
    """Cards end in EVERY terminal state: whoever ended the intent —
    the reaper's expiry, the pipeline's
    `posted`, the worker's cancellation — its live cards lose their buttons
    and gain the terminal line. One mechanism for every writer, run in the
    reaper's pass; a tap's own supersede is immediate and this is the
    backstop. Returns the intents healed. A card that is sent settled
    (`sends_settled`) is left in `pending` for `SETTLED_CARD_GRACE`,
    for the sender to claim, and is not counted healed until it goes.

    The selection is a door (worker sweep functions, `fn_settled_cards`): the reaper runs with no
    tenant and every table it joins is policy-covered — the direct read found
    nothing under `svc_worker`, so no ended story's card would ever lose its
    buttons. The door keeps the active-bindings join (a revoked
    group's cards cannot be edited and would starve the LIMIT every beat) and
    takes the terminal states as its argument, so their one spelling stays
    `intent_ledger.TERMINAL_STATES`. The settlement read and the supersede
    then run per row under that row's workspace, claimed before the savepoint
    (a rolled-back savepoint would revert a claim made inside it), and the
    caller's own scope is handed back at the end.
    """
    from src.services.target import unit_of_work

    rows = (
        (
            await session.execute(
                text(
                    "SELECT o_intent_id AS intent_id, o_workspace_id AS workspace_id,"
                    "       o_binding_id AS binding_id, o_state AS state,"
                    "       o_entered_state_at AS entered_state_at, o_tz AS tz"
                    "  FROM fn_settled_cards(CAST(:terminal AS text[]), :lim)"
                ),
                {
                    "lim": int(limit),
                    "terminal": list(intent_ledger.TERMINAL_STATES),
                },
            )
        )
        .mappings()
        .all()
    )
    claims = unit_of_work.WorkspaceClaims(session)
    healed = 0
    for row in rows:
        await claims.claim(str(row["workspace_id"]))
        # One row's fault (a zone, a lost binding) must not fail the reaper's
        # transaction for every workspace: a savepoint per row, and on.
        try:
            async with session.begin_nested():
                settled = await intent_ledger.settlement(
                    session,
                    workspace_id=str(row["workspace_id"]),
                    intent_id=str(row["intent_id"]),
                )
                line = await settled_line(
                    session,
                    settled,
                    tz=str(row["tz"] or "UTC"),
                    at=row["entered_state_at"],
                    state=str(row["state"]),
                )
                spare = (
                    SETTLED_CARD_GRACE
                    if sends_settled(str(row["state"]), settled.get("approval_rule"))
                    else None
                )
                superseded = await outbox.supersede_all(
                    session,
                    workspace_id=str(row["workspace_id"]),
                    binding_id=str(row["binding_id"]),
                    intent_id=str(row["intent_id"]),
                    outcome_text=line,
                    spare_pending=spare,
                )
        except Exception:  # noqa: BLE001 — isolated, logged, the sweep goes on
            logger.exception(
                "settled-card sweep: intent %s binding %s skipped",
                row["intent_id"],
                row["binding_id"],
            )
            continue
        if superseded or spare is None:
            healed += 1
    await claims.release()
    return healed


async def sweep_due_prompts(
    session, *, late_seconds: int, limit: int, workspace_id=None
) -> dict:
    """The prompt sweep, idempotent, in the caller's
    transaction. Two legs, three counts:

    - ``prompted``: due `scheduled` intents (slot arrived, workspace active
      and unpaused, no cancel flag) gain their transition + cards via
      :func:`prompt_intent`. A planned one is due only while it can be served
      and within *late_seconds* of its time (`fn_planned_serve` / `fn_planned_misses`); its card names who
      scheduled it.
      This is also the correctness backstop for the fast path in the
      `plan_slot` adapter — an intent whose prompt crashed mid-way is
      picked up here.
    - ``advanced``: every `prompt_pending` intent moves to
      `awaiting_approval` — "prompt delivered on ≥ 1 binding, or workspace
      has web access", and web access is universal (module docstring), so
      this leg runs in the same pass as ``prompted`` and an intent is
      actionable the moment it is prompted. The guard makes a lost race
      benign.
    - ``approved``: each story the leg advances is then served its decision
      a rule approves it as the system
      (`auto_approval.approve_if_due`), in a savepoint of its own. Any
      failure there rolls back that savepoint alone and is logged; the story
      waits for a person, and the sweep goes on.
    """
    from src.services.target import unit_of_work

    counts = {"prompted": 0, "advanced": 0, "approved": 0}

    # The two cross-tenant READS are doors (worker sweep functions): the sweep runs with no tenant
    # and every table here is policy-covered. The WRITES — the cards, the
    # transitions — run per workspace under that workspace's tenant and the
    # system actor, exactly as a member's own action does, so the ledger's
    # triggers attribute them. The claims are transaction-local, re-made
    # whenever the workspace changes, and the caller's own scope is handed
    # back at the end: `plan_slot` runs this sweep inside a TENANT job's
    # transaction and then finalizes that job, which the policies would fence
    # under the last workspace prompted (`WorkspaceClaims`).
    due = (
        (
            await session.execute(
                text(
                    "SELECT o_id AS id, o_state AS state,"
                    "       o_workspace_id AS workspace_id,"
                    "       o_schedule_slot_at AS schedule_slot_at,"
                    "       o_file_name AS file_name, o_media_kind AS media_kind,"
                    "       o_mime_type AS mime_type, o_source_id AS source_id,"
                    "       o_provider_file_ref AS provider_file_ref,"
                    "       o_handle AS handle, o_tz AS tz,"
                    "       o_api_publishing_enabled AS api_publishing_enabled,"
                    "       o_origin AS origin,"
                    "       o_scheduled_by_user_id AS scheduled_by_user_id"
                    "  FROM fn_prompts_due(:lim, make_interval(secs => :late))"
                ),
                {"lim": limit, "late": float(late_seconds)},
            )
        )
        .mappings()
        .all()
    )
    advanced = (
        (
            await session.execute(
                text(
                    "SELECT o_id AS id, o_workspace_id AS workspace_id"
                    "  FROM fn_prompts_pending(:lim)"
                ),
                {"lim": limit},
            )
        )
        .mappings()
        .all()
    )
    from src.services.target import source_policy

    if workspace_id is not None:
        due = [r for r in due if str(r["workspace_id"]) == str(workspace_id)]
        advanced = [r for r in advanced if str(r["workspace_id"]) == str(workspace_id)]
    # Freeze both discovery legs before any later intent/outbox row lock.
    claims = unit_of_work.WorkspaceClaims(session)
    contexts = {}
    for ws in sorted({str(r["workspace_id"]) for r in [*due, *advanced]}):
        await claims.claim(ws)
        contexts[ws] = await source_policy.suitability_context(session, workspace_id=ws)
    bindings_by_workspace: dict[str, list[str]] = {}  # same tx, same answer
    for row in sorted(due, key=_sweep_order):
        ws = str(row["workspace_id"])
        await claims.claim(ws)
        verdict = await source_policy.intent_suitability(
            session, workspace_id=ws, intent_id=str(row["id"]), context=contexts[ws]
        )
        if verdict is None or not verdict["suitable"]:
            continue
        if not await _still_due(session, row):
            # its next reading decides: moved, flagged or served since the door read
            logger.info("prompt sweep: intent %s changed under the sweep", row["id"])
            continue
        if ws not in bindings_by_workspace:
            bindings_by_workspace[ws] = await push_bindings(session, ws)
        await prompt_intent(
            session,
            await _with_scheduler(
                session, {**row, "source_provider": verdict["provider"]}
            ),
            [{"id": b} for b in bindings_by_workspace[ws]],
        )
        counts["prompted"] += 1
        advanced.append({"id": row["id"], "workspace_id": row["workspace_id"]})

    advanced = list({str(r["id"]): r for r in advanced}.values())
    for row in sorted(advanced, key=lambda r: (str(r["workspace_id"]), str(r["id"]))):
        ws = str(row["workspace_id"])
        await claims.claim(ws)
        verdict = await source_policy.intent_suitability(
            session, workspace_id=ws, intent_id=str(row["id"]), context=contexts[ws]
        )
        if verdict is None or not verdict["suitable"]:
            continue
        # A refusal is a Postgres error, which aborts the transaction: the
        # savepoint is what lets the sweep go on to the next row, and hand the
        # caller's scope back, after one.
        try:
            async with session.begin_nested():
                await intent_ledger.transition(
                    session, str(row["id"]), "awaiting_approval"
                )
            counts["advanced"] += 1
        except intent_ledger.IntentTransitionRefused:
            continue  # the ledger refused; most often the fast path moved it first
        # The serve step, in its own savepoint: whatever goes wrong
        # deciding or approving rolls back only that, and the story waits for
        # a person — the safe failure. The settled sweep isolates its rows the
        # same way.
        try:
            async with session.begin_nested():
                if await auto_approval.approve_if_due(
                    session, workspace_id=ws, intent_id=str(row["id"])
                ):
                    counts["approved"] += 1
        except Exception:  # noqa: BLE001 — isolated, logged, the sweep goes on
            logger.warning(
                "prompt sweep: story %s was not approved automatically and waits"
                " for a person",
                row["id"],
                exc_info=True,
            )
    await claims.release()
    return counts


async def say_not_served(
    session,
    row: Mapping,
    *,
    reason: str,
    surface: list[str],
    by: Optional[str],
    repeats: Optional[bool] = None,
) -> Union[int, str]:
    """Tell a workspace's bound chats that one planned story was not served,
    in the CALLER's transaction — the one that ended it or flagged it, so the
    notice commits with the change it announces. *row* is a `_NOTICE_SELECT`
    row, *surface* the workspace's push bindings (`push_bindings`, resolved
    once per workspace by the caller) and *by* the scheduler's display name.

    Returns the rows written, or `outbox.UNDELIVERABLE` when the workspace has
    no push binding: "told nobody, because there is nobody" is a verdict
    (`scheduler._notice_no_media`'s rule), never a quiet zero. The story still
    ends `expired`, and its reason stays on the row (`last_error`) for the
    operator's views."""
    if not surface:
        logger.warning(
            "planned story %s was not served (%s) and its workspace has NO push"
            " binding — nobody was told",
            row["id"],
            reason,
        )
        return outbox.UNDELIVERABLE
    return await outbox.fanout_notification(
        session,
        workspace_id=str(row["workspace_id"]),
        bindings=surface,
        text=missed_notice(
            file_name=row.get("file_name"),
            handle=row.get("handle"),
            slot=row["schedule_slot_at"],
            tz=str(row.get("tz") or "UTC"),
            by=by,
            reason=reason,
            repeats=repeats,
        ),
        intent_id=str(row["id"]),
    )


#: The row a "not served" notice is written from, and the one spelling of it
#: in Python. The removal's notice reads it directly; `fn_planned_misses`
#: (`fn_planned_serve` / `fn_planned_misses`) spells the same columns and joins, a door being unable to call the
#: Python, and the pin in `test_prompts.py` holds the two together, as it
#: holds `_CARD_SELECT` to `fn_prompts_due`.
_NOTICE_SELECT = (
    "SELECT i.id, i.workspace_id, i.schedule_slot_at, i.scheduled_by_user_id,"
    "       m.file_name, a.handle, w.tz"
    "  FROM post_intents i"
    "  JOIN workspaces w ON w.id = i.workspace_id"
    "  LEFT JOIN media_items m ON m.id = i.media_item_id"
    "   AND m.workspace_id = i.workspace_id"
    "  LEFT JOIN ig_accounts a ON a.id = i.ig_account_id"
    "   AND a.workspace_id = i.workspace_id"
)


async def say_removed_before_served(
    session, *, workspace_id: str, intent_ids: list[str]
) -> None:
    """Queue the `account_removed` notice for each planned story a
    destination's removal flagged before its time was served
    (`provisioning.disable_destination`), in the removal's own transaction.
    Flagged, such a story never reaches the miss door, which lists no flagged
    row, so this is its one notice — the same words the miss leg sends."""
    rows = await readers.rows(
        session,
        _NOTICE_SELECT + " WHERE i.workspace_id = :ws"
        " AND i.id = ANY(CAST(:ids AS uuid[])) ORDER BY i.schedule_slot_at",
        ws=workspace_id,
        ids=intent_ids,
    )
    surface = await push_bindings(session, workspace_id)
    names: dict[str, str] = {}
    for row in rows:
        await say_not_served(
            session,
            row,
            reason="account_removed",
            surface=surface,
            by=await _scheduler_name(
                session, row.get("scheduled_by_user_id"), row["workspace_id"], names
            ),
        )


async def _next_waits(session, workspace_id: str, ended) -> bool:
    """Whether a live story now waits for the ended story's item on its
    account. Read in the transaction that ended it, that is the story its
    ending made (`tg_intent_repeat`), so a notice says what became of a
    series rather than what its reason predicts."""
    found = await readers.row(
        session,
        "SELECT EXISTS (SELECT 1 FROM post_intents"
        "                WHERE workspace_id = :ws AND media_item_id = :media"
        f"                 AND ig_account_id = :acct AND {intent_ledger.NOT_TERMINAL})"
        "         AS waits",
        ws=workspace_id,
        media=str(ended.media_item_id),
        acct=str(ended.ig_account_id),
    )
    return bool(found["waits"])


async def sweep_planned_misses(session, *, late_seconds: int, limit: int) -> dict:
    """The miss leg (`fn_planned_serve` / `fn_planned_misses`), on the prompt sweep's cadence and in the CALLER's
    transaction: every planned story the miss door lists ends `expired`, with
    `last_error = {v: 1, class: planned_missed, message: <reason>}`, and its
    notice is queued in the SAME savepoint, so none ends unserved without
    saying so. Returns ``missed`` (stories ended) and ``unheard`` (those whose
    workspace had no surface to tell).

    The door is the rule: what can be served, and whether the window has
    passed, is decided in SQL, as the due door decides it, and the two are
    each other's complement. The UPDATE is guarded on `scheduled` and on the
    flag, so a story served, flagged or ended since the door read it is left
    alone: a cancelled story gets no miss notice (its person already knows),
    and only the reaper's cancel leg ever writes `cancelled`. Each row runs
    under its workspace's tenant in its own savepoint, so one row's fault is
    logged and the sweep goes on; the caller's scope is handed back.

    A repeating story's notice says what became of its series (`_next_waits`),
    and a repeat's occurrence born after its time, whose series goes on, ends
    with no notice at all."""
    from src.services.target import unit_of_work

    rows = (
        (
            await session.execute(
                text(
                    "SELECT o_id AS id, o_workspace_id AS workspace_id,"
                    "       o_reason AS reason, o_schedule_slot_at AS schedule_slot_at,"
                    "       o_file_name AS file_name, o_handle AS handle, o_tz AS tz,"
                    "       o_scheduled_by_user_id AS scheduled_by_user_id"
                    "  FROM fn_planned_misses(:lim, make_interval(secs => :late))"
                ),
                {"lim": limit, "late": float(late_seconds)},
            )
        )
        .mappings()
        .all()
    )
    counts = {"missed": 0, "unheard": 0}
    surfaces: dict[str, list[str]] = {}  # same tx, same answer
    names: dict[str, str] = {}  # a scheduler's display name, once per sweep
    claims = unit_of_work.WorkspaceClaims(session)
    from src.services.target import source_state

    for ws in sorted({str(r["workspace_id"]) for r in rows}):
        await claims.claim(ws)
        await source_state.lock_all_workspace_sources(session, workspace_id=ws)
    for row in sorted(rows, key=_sweep_order):
        ws = str(row["workspace_id"])
        await claims.claim(ws)
        try:
            async with session.begin_nested():
                ended = (
                    await session.execute(
                        text(
                            "UPDATE post_intents SET state = 'expired',"
                            " last_error = CAST(:e AS jsonb)"
                            " WHERE id = :id AND workspace_id = :ws"
                            "   AND state = 'scheduled' AND origin = 'planned'"
                            "   AND NOT cancel_requested"
                            "   AND schedule_slot_at = :slot"
                            " RETURNING created_at > schedule_slot_at"
                            "           AND repeat_unit IS NOT NULL AS born_late,"
                            "           repeat_unit IS NOT NULL AS repeating,"
                            "           media_item_id, ig_account_id"
                        ),
                        {
                            "id": str(row["id"]),
                            "ws": ws,
                            "slot": row["schedule_slot_at"],
                            "e": json.dumps(
                                {
                                    "v": 1,
                                    "class": vocabulary.PLANNED_MISSED,
                                    "message": row["reason"],
                                }
                            ),
                        },
                    )
                ).first()
                if ended is None:
                    continue  # served, flagged, moved or ended since the door read it
                repeats = (
                    await _next_waits(session, ws, ended) if ended.repeating else None
                )
                told = None
                # A repeat's occurrence born after its time was made when the
                # one before it ended late, while that one's card was out:
                # nobody waited for this one, and when its series goes on there
                # is nothing to say. A series it stops is said.
                # `schedule_item` refuses a past time, so no other story is
                # born late.
                if not (ended.born_late and repeats):
                    if ws not in surfaces:
                        surfaces[ws] = await push_bindings(session, ws)
                    told = await say_not_served(
                        session,
                        row,
                        reason=str(row["reason"]),
                        surface=surfaces[ws],
                        by=await _scheduler_name(
                            session,
                            row.get("scheduled_by_user_id"),
                            row["workspace_id"],
                            names,
                        ),
                        repeats=repeats,
                    )
        except Exception:  # noqa: BLE001 — isolated, logged, the sweep goes on
            logger.exception("planned-miss sweep: intent %s skipped", row["id"])
            continue
        counts["missed"] += 1
        if told == outbox.UNDELIVERABLE:
            counts["unheard"] += 1
    await claims.release()
    return counts
