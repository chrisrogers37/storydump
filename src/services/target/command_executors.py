"""Executors for the built half of the interaction-layer vocabulary.

One function per built command, all with the port's executor signature
``async (session, command) -> CommandResult``, all running INSIDE the caller's
unit of work (tenant + actor GUCs applied — the `02` §4 audit triggers refuse
an anonymous state change, and every write here is one). `commands.REGISTRY`
is the only importer; the not-built half of the vocabulary is `None` there and
named in `commands.UNBUILT`.

## The ledger is the authority, so these functions validate nothing twice

`intent_ledger.transition` issues the UPDATE and lets `trg_intent_guard`
decide (L.1 doctrine). An illegal edge — a late approve on an expired intent,
a second skip on a skipped one — comes back as `IntentTransitionRefused`
carrying the trigger's own message, and surfaces as `illegal_transition`. That
is R6's "a late interaction renders the terminal state, never acts": the
adapter reads the current state back and shows it, and nothing was acted on.

## Where the effect lists come from

Every intent edge's side effects are the `02` §4 matrix rows, verbatim:

- `awaiting_approval → posted` (`mark_posted`, the manual-mode path): same
  transaction sets `published_via='manual'`, debits the cap
  (`cap_consumed_on`), `times_posted`++, the account-scoped recent lock,
  `ig_accounts.last_posted_at`. Mirrors `publish_pipeline._confirm`'s
  post-publish effects deliberately — one shape, two entry points.
- `awaiting_approval → rejected`: terminal; upserts a workspace-scoped
  permanent `reject` lock. `→ skipped`: terminal; a workspace-scoped `skip`
  lock with the workspace's skip TTL (`06` §3's selection rule reads both).
- `awaiting_approval → approved` (`approve`): in a workspace with
  `api_publishing_enabled`, the approval flip PRODUCES the `publish_pipeline`
  job (`02` §5 registry: producer "approval flip"). In a manual-mode
  workspace there is nothing to approve INTO — the card offers Posted / Skip
  / Reject (`06` §3) — so `approve` is refused as `manual_mode` and the
  adapter says which command to use instead.
- `cancel`: the user never writes a terminal state. `cancel_requested` is set
  and the worker terminalizes at its next checkpoint (`02` §4: every
  `→ cancelled` edge's actor is the worker, "pre-publish it is always
  honorable"). Refused if the intent is already terminal.

`sync_now` mints the `sync_media_source{reason:'demand'}` job in the exact
shape `fn_clock_tick` mints the baseline one (`059:271`) — same lane, key,
attempts — and declines to mint a second while one is pending for that
source, since the serialization key would only queue it behind the first.
Both job rows go through `jobs.enqueue`, the tier's one INSERT.

A caller-supplied value the writers refuse (`InvalidWorkspaceArgs`) is not
caught here: `commands.execute` maps it once, for every executor.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from src.config.defaults import DEFAULT_REPOST_TTL_DAYS, DEFAULT_SKIP_TTL_DAYS
from src.services.target import intent_ledger, jobs, readers, workspaces
from src.services.target.commands import Command, CommandRefused, CommandResult
from src.services.target.intent_ledger import IntentTransitionRefused

#: `02` §4 terminal states — the reaper/worker own every edge INTO these; a
#: user command on a terminal intent renders it and acts on nothing (R6).
TERMINAL_STATES = ("posted", "skipped", "rejected", "expired", "failed", "cancelled")


def _arg(command: Command, name: str) -> str:
    value = command.args.get(name)
    if not isinstance(value, str) or not value.strip():
        raise CommandRefused("invalid_args", f"{name} is required")
    return value.strip()


async def _intent_row(session, command: Command) -> dict[str, Any]:
    """The intent plus the workspace/account facts the effect lists need.
    Workspace-bound in the WHERE, not only by RLS."""
    intent_id = _arg(command, "intent_id")
    row = await readers.row(
        session,
        "SELECT i.id, i.state, i.media_item_id, i.ig_account_id,"
        "       i.provider_account_ref,"
        "       w.api_publishing_enabled, w.repost_ttl_days, w.skip_ttl_days,"
        "       COALESCE(a.posts_per_day, w.posts_per_day) AS eff_ppd,"
        "       COALESCE(a.tz, w.tz) AS eff_tz"
        "  FROM post_intents i"
        "  JOIN workspaces w ON w.id = i.workspace_id"
        "  JOIN ig_accounts a ON a.id = i.ig_account_id"
        " WHERE i.id = :id AND i.workspace_id = :ws",
        id=intent_id,
        ws=command.workspace_id,
    )
    if row is None:
        raise CommandRefused("not_found", f"intent {intent_id}")
    return row


async def _flip(session, intent_id: str, to_state: str) -> None:
    try:
        await intent_ledger.transition(session, intent_id, to_state)
    except IntentTransitionRefused as exc:
        raise CommandRefused("illegal_transition", str(exc)) from exc


def _result(intent: dict[str, Any], state: str, **extra: Any) -> CommandResult:
    return CommandResult(
        "executed", {"intent_id": str(intent["id"]), "state": state, **extra}
    )


async def approve(session, command: Command) -> CommandResult:
    intent = await _intent_row(session, command)
    if not intent["api_publishing_enabled"]:
        raise CommandRefused(
            "manual_mode",
            "this workspace publishes manually; use mark_posted after posting by hand",
        )
    await _flip(session, str(intent["id"]), "approved")
    await jobs.enqueue(
        session,
        kind="publish_pipeline",
        workspace_id=command.workspace_id,
        serialization_key=f"ig:{intent['provider_account_ref']}",
        payload={"v": 1, "intent_id": str(intent["id"])},
    )
    return CommandResult(
        "enqueued",
        {
            "intent_id": str(intent["id"]),
            "state": "approved",
            "job": "publish_pipeline",
        },
    )


async def skip(session, command: Command) -> CommandResult:
    intent = await _intent_row(session, command)
    await _flip(session, str(intent["id"]), "skipped")
    ttl_days = int(intent["skip_ttl_days"] or DEFAULT_SKIP_TTL_DAYS)
    await session.execute(
        text(
            "INSERT INTO post_locks (workspace_id, media_item_id, kind, expires_at,"
            " created_by_intent_id, created_by_user_id)"
            " VALUES (:ws, :media, 'skip', now() + make_interval(days => :ttl), :intent, :u)"
            " ON CONFLICT (workspace_id, media_item_id, kind) WHERE ig_account_id IS NULL"
            " DO UPDATE SET expires_at = EXCLUDED.expires_at,"
            "               created_by_intent_id = EXCLUDED.created_by_intent_id,"
            "               created_by_user_id = EXCLUDED.created_by_user_id"
        ),
        {
            "ws": command.workspace_id,
            "media": str(intent["media_item_id"]),
            "ttl": ttl_days,
            "intent": str(intent["id"]),
            "u": command.actor_user_id,
        },
    )
    return _result(intent, "skipped", lock="skip", lock_days=ttl_days)


async def reject(session, command: Command) -> CommandResult:
    intent = await _intent_row(session, command)
    await _flip(session, str(intent["id"]), "rejected")
    await session.execute(
        text(
            "INSERT INTO post_locks (workspace_id, media_item_id, kind, expires_at,"
            " created_by_intent_id, created_by_user_id)"
            " VALUES (:ws, :media, 'reject', NULL, :intent, :u)"
            " ON CONFLICT (workspace_id, media_item_id, kind) WHERE ig_account_id IS NULL"
            " DO UPDATE SET expires_at = NULL,"
            "               created_by_intent_id = EXCLUDED.created_by_intent_id,"
            "               created_by_user_id = EXCLUDED.created_by_user_id"
        ),
        {
            "ws": command.workspace_id,
            "media": str(intent["media_item_id"]),
            "intent": str(intent["id"]),
            "u": command.actor_user_id,
        },
    )
    return _result(intent, "rejected", lock="reject")


async def mark_posted(session, command: Command) -> CommandResult:
    """The manual-mode path (`06` §3): the human posted by hand and taps
    Posted. The debit is unconditional — the story is already on Instagram,
    so refusing to record it at the cap would misstate the day, which is the
    over-posting direction R1 exists to avoid; `cap_at_write` freezes at the
    day's first debit exactly as the API path's does."""
    intent = await _intent_row(session, command)
    row = (
        await session.execute(
            text(
                "WITH debit AS ("
                "  INSERT INTO daily_post_counts AS d"
                "    (workspace_id, ig_account_id, local_date, count, cap_at_write)"
                "  VALUES (:ws, :acct,"
                "          (now() AT TIME ZONE fn_safe_tz(:tz))::date, 1, :cap)"
                "  ON CONFLICT (workspace_id, ig_account_id, local_date)"
                "    DO UPDATE SET count = d.count + 1"
                "  RETURNING local_date"
                "), flip AS ("
                "  UPDATE post_intents"
                "     SET state = 'posted', published_via = 'manual',"
                "         cap_consumed_on = (SELECT local_date FROM debit)"
                "   WHERE id = :intent AND state = 'awaiting_approval'"
                "  RETURNING id"
                ") SELECT (SELECT count(*) FROM flip) AS flipped"
            ),
            {
                "ws": command.workspace_id,
                "acct": str(intent["ig_account_id"]),
                "tz": intent["eff_tz"] or "UTC",
                "cap": int(intent["eff_ppd"]),
                "intent": str(intent["id"]),
            },
        )
    ).one()
    if int(row.flipped) != 1:
        # Not awaiting approval any more: render the current state, act on
        # nothing (R6). The debit above rolls back with the caller's
        # transaction, because the adapter maps this refusal to a rollback.
        state = await intent_ledger.current_state(session, str(intent["id"]))
        raise CommandRefused(
            "illegal_transition", f"intent is {state!r}, not awaiting_approval"
        )
    await session.execute(
        text(
            "UPDATE media_items SET times_posted = times_posted + 1,"
            " last_posted_at = now() WHERE id = :media"
        ),
        {"media": str(intent["media_item_id"])},
    )
    await session.execute(
        text(
            "INSERT INTO post_locks (workspace_id, media_item_id, kind,"
            " ig_account_id, expires_at, created_by_intent_id, created_by_user_id)"
            " VALUES (:ws, :media, 'recent', :acct,"
            "         now() + make_interval(days => :ttl_days), :intent, :u)"
            " ON CONFLICT (workspace_id, media_item_id, kind, ig_account_id)"
            "   WHERE ig_account_id IS NOT NULL"
            " DO UPDATE SET expires_at = EXCLUDED.expires_at"
        ),
        {
            "ws": command.workspace_id,
            "media": str(intent["media_item_id"]),
            "acct": str(intent["ig_account_id"]),
            "ttl_days": int(intent["repost_ttl_days"] or DEFAULT_REPOST_TTL_DAYS),
            "intent": str(intent["id"]),
            "u": command.actor_user_id,
        },
    )
    await session.execute(
        text("UPDATE ig_accounts SET last_posted_at = now() WHERE id = :acct"),
        {"acct": str(intent["ig_account_id"])},
    )
    return _result(intent, "posted", published_via="manual")


async def cancel(session, command: Command) -> CommandResult:
    intent = await _intent_row(session, command)
    if intent["state"] in TERMINAL_STATES:
        raise CommandRefused(
            "illegal_transition", f"intent is already {intent['state']!r}"
        )
    await session.execute(
        text("UPDATE post_intents SET cancel_requested = true WHERE id = :id"),
        {"id": str(intent["id"])},
    )
    return _result(intent, intent["state"], cancel_requested=True)


async def sync_now(session, command: Command) -> CommandResult:
    source_id = _arg(command, "source_id")
    source = (
        await session.execute(
            text(
                "SELECT id, state FROM media_sources WHERE id = :s AND workspace_id = :ws"
            ),
            {"s": source_id, "ws": command.workspace_id},
        )
    ).first()
    if source is None:
        raise CommandRefused("not_found", f"media source {source_id}")
    job_id = await jobs.enqueue(
        session,
        kind="sync_media_source",
        workspace_id=command.workspace_id,
        serialization_key=f"src:{source_id}",
        payload={"v": 1, "source_id": source_id, "reason": "demand"},
        unless_pending=True,
    )
    if job_id is None:
        return CommandResult(
            "executed", {"source_id": source_id, "sync": "already_pending"}
        )
    return CommandResult(
        "enqueued",
        {"source_id": source_id, "job": "sync_media_source", "job_id": job_id},
    )


async def settings_change(session, command: Command) -> CommandResult:
    changes = command.args.get("settings")
    if not isinstance(changes, dict):
        raise CommandRefused("invalid_args", "settings must be an object")
    cleaned = await workspaces.change_settings(
        session, workspace_id=command.workspace_id, changes=changes
    )
    return CommandResult("executed", {"changed": sorted(cleaned)})


async def pause_workspace(session, command: Command) -> CommandResult:
    await workspaces.set_paused(
        session,
        workspace_id=command.workspace_id,
        paused=True,
        by_user_id=command.actor_user_id,
    )
    return CommandResult("executed", {"is_paused": True})


async def resume_workspace(session, command: Command) -> CommandResult:
    await workspaces.set_paused(
        session,
        workspace_id=command.workspace_id,
        paused=False,
        by_user_id=command.actor_user_id,
    )
    return CommandResult("executed", {"is_paused": False})


async def rename_workspace(session, command: Command) -> CommandResult:
    name = await workspaces.rename(
        session, workspace_id=command.workspace_id, name=_arg(command, "name")
    )
    return CommandResult("executed", {"name": name})


async def create_workspace(session, command: Command) -> CommandResult:
    tz = command.args.get("tz")
    if tz is not None and not isinstance(tz, str):
        raise CommandRefused("invalid_args", "tz must be a string")
    ws_id = await workspaces.create_workspace(
        session,
        owner_user_id=command.actor_user_id,
        name=_arg(command, "name"),
        tz=tz,
        channel=command.channel,
        workspace_id=command.args.get("workspace_id"),
    )
    return CommandResult("executed", {"workspace_id": ws_id})
