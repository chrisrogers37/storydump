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
Both job rows go through `jobs.enqueue`, which is this module's only INSERT
into `jobs` — not the tier's, as an earlier version of this line claimed:
`media_sync`, `work_loop.ensure_sender_jobs` and `offboarding._mint_successor`
each write the table directly where the shape needs SQL `enqueue` cannot
carry.

A caller-supplied value the writers refuse (`InvalidWorkspaceArgs`) is not
caught here: `commands.execute` maps it once, for every executor.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from src.config.defaults import DEFAULT_REPOST_TTL_DAYS, DEFAULT_SKIP_TTL_DAYS
from src.services.target import (
    google_drive_oauth,
    intent_ledger,
    jobs,
    offboarding,
    readers,
    workspaces,
)
from src.services.target.ig_login_oauth import issue_state
from src.services.target.commands import Command, CommandRefused, CommandResult
from src.services.target.intent_ledger import IntentTransitionRefused

#: `02` §4 terminal states — the reaper/worker own every edge INTO these; a
#: user command on a terminal intent renders it and acts on nothing (R6). Re-
#: exported from `intent_ledger`, which owns the one Python copy.
TERMINAL_STATES = intent_ledger.TERMINAL_STATES


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


async def _drive_source(session, command: Command) -> str:
    """The command's `source_id`, proven to belong to this workspace.

    Both keys in the WHERE, not just the id: RLS is inert under the deployed
    owner role (#751), so this clause is what actually binds the lookup to the
    tenant. Drive folder ids are global and a source id is a UUID a caller
    supplies, so an unscoped read here would be the cross-tenant hazard
    #982 named.
    """
    source_id = _arg(command, "source_id")
    row = (
        await session.execute(
            text(
                "SELECT id FROM media_sources"
                " WHERE id = :s AND workspace_id = :ws AND provider = 'gdrive'"
            ),
            {"s": source_id, "ws": command.workspace_id},
        )
    ).first()
    if row is None:
        raise CommandRefused("not_found", f"gdrive media source {source_id}")
    return source_id


async def _begin_drive_link(session, command: Command, *, expect: str) -> CommandResult:
    """Shared body of `connect_account` / `reconnect_account`.

    THIN BY DESIGN — F1 (a). The OAuth leg is the API route's (#1065 shipped
    the callback); this door only initiates and records. Initiating IS minting
    the `oauth_states` row, which is the thing nothing else does: #1065 landed
    the callback and no start leg, so until this executor there was no way to
    begin a Drive connect at all.

    **It returns the state, not a URL, and that is a layering decision rather
    than an omission.** Composing the URL needs `(client_id, redirect_uri)`,
    which `src/api/google_client.py` owns — an API module that raises
    `HTTPException`, and whose docstring says it exists so "the two legs cannot
    disagree on a redirect URI". No module under `src/services/` imports from
    `src.api` today. Reading settings again here to avoid that import would
    fork the single owner of the redirect URI, which is the exact drift that
    module was written to prevent. So the adapter renders, as it already does
    for sign-in at `routes/auth.py:175`.

    The connect/reconnect split is the SCHEMA's answer, not the caller's:
    `connect_purpose` reports which one this source is in, and a command that
    disagrees is refused by name rather than quietly doing the other. A
    reconnect that ran as a connect would skip `issue_state`'s
    invalidate-prior-states step (`07` §2, "last issued wins") and leave two
    live callbacks for one source.
    """
    source_id = await _drive_source(session, command)
    purpose = await google_drive_oauth.connect_purpose(
        session, workspace_id=command.workspace_id, media_source_id=source_id
    )
    if purpose is None:
        raise CommandRefused("not_found", f"gdrive media source {source_id}")
    if purpose != expect:
        # `illegal_transition`, from the closed REASONS set — this is exactly
        # that: a move the port will not make from the state the source is in.
        # An invented reason raises ValueError at construction (a programming
        # error, by design), and the web adapter's status table is pinned
        # TOTAL over REASONS, so a new member would silently have no mapping.
        raise CommandRefused(
            "illegal_transition",
            f"source {source_id} needs {purpose}, not {expect}",
        )
    state = await issue_state(
        session,
        purpose=purpose,
        user_id=command.actor_user_id,
        workspace_id=command.workspace_id,
        reconnect_target=source_id,
        provider=google_drive_oauth.PROVIDER,
    )
    return CommandResult(
        "executed",
        {
            "source_id": source_id,
            "provider": google_drive_oauth.PROVIDER,
            "purpose": purpose,
            "state": state,
        },
    )


async def connect_account(session, command: Command) -> CommandResult:
    """Begin a Drive connect for a source that has no credential yet."""
    return await _begin_drive_link(session, command, expect="connect")


async def reconnect_account(session, command: Command) -> CommandResult:
    """Begin a Drive reconnect for a source whose credential exists."""
    return await _begin_drive_link(session, command, expect="reconnect")


async def disconnect_account(session, command: Command) -> CommandResult:
    """F5 (a): revoke the credential, KEEP the row, and pause the source.

    `paused`, deliberately not `error`. A disconnect is a decision, not a
    fault, and reserving `error` for faults is what keeps the #1061 disconnect
    alert meaningful — if a user disconnecting produced `error`, that beat
    would re-alert them every day about something they chose, and someone
    would eventually silence it, taking the real faults with it. `paused` is
    outside the beat's scan by construction.

    The row is kept rather than deleted because `oauth_credentials` cascades
    from the source: deleting it erases an audit trail nobody misses until
    they need it.

    **The best-effort Google revoke is NOT performed here, and that is a
    scope statement rather than an oversight.** A provider call inside a unit
    of work violates this codebase's checkpoint discipline — `transit.upload`
    carries the same rule in its own docstring — and F5 (a) makes the remote
    revoke best-effort precisely because it may fail and must not block the
    local state change. What must be atomic is the pair below; the remote call
    belongs on a job outside this transaction and is not built.
    """
    source_id = await _drive_source(session, command)
    revoked = (
        await session.execute(
            text(
                "UPDATE oauth_credentials SET state = 'revoked'"
                " WHERE workspace_id = :ws AND media_source_id = :s"
                "   AND provider = :provider AND state <> 'revoked'"
                " RETURNING id"
            ),
            {
                "ws": command.workspace_id,
                "s": source_id,
                "provider": google_drive_oauth.PROVIDER,
            },
        )
    ).first()
    await session.execute(
        text(
            "UPDATE media_sources SET state = 'paused', alerted_at = NULL"
            " WHERE id = :s AND workspace_id = :ws"
        ),
        {"s": source_id, "ws": command.workspace_id},
    )
    # The remote half (#1083), enqueued rather than called. Everything above
    # is the atomic pair; this rides a separate transaction so a Google that
    # is slow, angry or absent cannot roll back a disconnect the user has
    # already been told succeeded — which is what F5 (a)'s "best-effort"
    # requires and what this file's own docstring said it was waiting for.
    #
    # Only when a row actually flipped: a repeat disconnect updates nothing,
    # and minting a second revoke for a grant already revoked would spend a
    # provider call to learn that. `unless_pending` covers the racing case.
    if revoked is not None:
        await jobs.enqueue(
            session,
            kind="revoke_workspace_credentials",
            workspace_id=command.workspace_id,
            lane="bulk",
            serialization_key=f"revoke:{revoked[0]}",
            payload={"v": 1, "credential_id": str(revoked[0])},
            unless_pending=True,
        )
    return CommandResult(
        "executed",
        {"source_id": source_id, "credential": "revoked", "source": "paused"},
    )


async def settings_change(session, command: Command) -> CommandResult:
    changes = command.args.get("settings")
    if not isinstance(changes, dict):
        raise CommandRefused("invalid_args", "settings must be an object")
    cleaned = await workspaces.change_settings(
        session, workspace_id=command.workspace_id, changes=changes
    )
    return CommandResult("executed", {"changed": sorted(cleaned)})


async def account_settings_change(session, command: Command) -> CommandResult:
    """One account's schedule overrides (#1175 / `06` §3).

    **What this closes.** `054` gives every account its own `posts_per_day`,
    `posting_hours_start/end` and `tz`, `06` §3 ratifies the account as "the
    unit of scheduling", and `fn_clock_tick` already resolves the ladder per
    row on every tick. Nothing could write those four columns. The only
    statements that touched an account at all set `last_posted_at`,
    `state`, `last_no_media_notice_at` or upserted `handle`, and provisioning
    uses a supplied schedule ONLY to compute the opening `next_slot_at` —
    never to store it. So a second account was addable and, having no way to
    differ from the first, silently inherited every default.

    **A separate kind rather than a scope on `settings_change`** — the gate
    reads `ROLE_FLOOR[command.kind]` with no scope parameter, so the scoped
    form would have to move authorization into this function, which
    `01-target-architecture.md:39` rules out by name ("one central
    authorization gate ... one place, not per handler").

    **Nothing here recomputes `next_slot_at`.** The cursor advances through
    `fn_next_slot(a.next_slot_at, eff_tz, eff_start, eff_end, eff_ppd)` with
    the effective values re-read each tick, so a change lands on the NEXT
    advance by itself. The slot already on the row was computed under the old
    settings and still fires at its old time; the new cadence governs from the
    one after. Recomputing here would be a second scheduling authority beside
    the clock, which is the thing `06` §3 gives the clock alone.
    """
    account_id = _arg(command, "ig_account_id")
    changes = command.args.get("settings")
    if not isinstance(changes, dict):
        raise CommandRefused("invalid_args", "settings must be an object")
    cleaned = await workspaces.change_account_settings(
        session,
        workspace_id=command.workspace_id,
        ig_account_id=account_id,
        changes=changes,
    )
    if cleaned is None:
        raise CommandRefused("not_found", f"account {account_id}")
    return CommandResult(
        "executed", {"ig_account_id": account_id, "changed": sorted(cleaned)}
    )


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


async def offboard_workspace(session, command: Command) -> CommandResult:
    """`06` §1's entry edge, owner-only (`ROLE_FLOOR`). Two writes and a job.

    The flip and the job are one transaction on purpose: a workspace left
    `offboarding` with nothing scheduled to finish the job would sit invisible
    to the clock forever, which is worse than not having started.

    **`confirm` is required, and it is the port's half of `06` §1's "owner
    (explicit, confirmed)".** The dialog is the front end's; what the port can
    enforce is that the destructive intent was stated rather than arrived at.
    This is the one command in the vocabulary whose effect is irreversible
    after the grace window, and a `POST` with an empty body should not start
    it.

    **A second offboard is refused rather than absorbed.** `06` §1's table
    admits `active/suspended → offboarding` and nothing else into that state,
    and re-stamping `offboarding_at` would silently restart a 30-day clock the
    owner believes is already running — moving a deletion date is not a no-op.
    """
    if command.args.get("confirm") is not True:
        raise CommandRefused(
            "invalid_args",
            "offboarding deletes this workspace and everything in it after the"
            " grace window; pass confirm=true to start it",
        )
    row = (
        await session.execute(
            text(
                "UPDATE workspaces SET state = 'offboarding', offboarding_at = now()"
                " WHERE id = :ws AND state IN ('active', 'suspended')"
                " RETURNING offboarding_at"
            ),
            {"ws": command.workspace_id},
        )
    ).first()
    if row is None:
        state = await readers.row(
            session,
            "SELECT state FROM workspaces WHERE id = :ws",
            ws=command.workspace_id,
        )
        if state is None:
            raise CommandRefused("not_found", f"workspace {command.workspace_id}")
        raise CommandRefused(
            "illegal_transition",
            f"workspace is {state['state']}, not active or suspended",
        )
    job_id = await jobs.enqueue(
        session,
        kind="offboard_workspace",
        workspace_id=command.workspace_id,
        lane=offboarding.LANE,
        serialization_key=offboarding.serialization_key(command.workspace_id),
        payload={"v": 1},
    )
    return CommandResult(
        "enqueued",
        {
            "state": "offboarding",
            "offboarding_at": row[0],
            "job": "offboard_workspace",
            "job_id": job_id,
        },
    )
