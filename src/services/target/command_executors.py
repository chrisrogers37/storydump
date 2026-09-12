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

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text

from src.config.defaults import DEFAULT_SKIP_TTL_DAYS
from src.config.settings import settings
from src.services.target import (
    google_drive_oauth,
    identity,
    intent_ledger,
    invitations,
    jobs,
    offboarding,
    outbox,
    prompts,
    provider_ops,
    provisioning,
    publish_cap,
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
    """The intent plus the workspace/account facts the effect lists need,
    read `FOR UPDATE` (F2 (a), phase 1 of the 2026-09-09 tap plan): the lock
    is what makes the read the DECISION — two taps racing on one card queue
    here, and the second reads the first's committed state and answers with
    it. Workspace-bound in the WHERE, not only by RLS."""
    intent_id = _arg(command, "intent_id")
    row = await readers.row(
        session,
        "SELECT i.id, i.workspace_id, i.state, i.media_item_id, i.ig_account_id,"
        "       i.provider_account_ref, i.cancel_requested, i.published_via,"
        "       i.publish_step, i.ig_container_id, i.attempts_by_step,"
        "       w.api_publishing_enabled, w.repost_ttl_days, w.skip_ttl_days,"
        "       w.dry_run_mode, w.is_paused,"
        "       COALESCE(a.posts_per_day, w.posts_per_day) AS eff_ppd,"
        "       COALESCE(a.tz, w.tz) AS eff_tz, a.handle,"
        # The publish precondition, read with the row rather than after it
        # (#1286): a usable Instagram Login token for this account.
        "       EXISTS (SELECT 1 FROM oauth_credentials c"
        "                WHERE c.workspace_id = i.workspace_id"
        "                  AND c.ig_account_id = i.ig_account_id"
        "                  AND c.provider = 'ig_login' AND c.state = 'active')"
        "         AS has_ig_credential"
        "  FROM post_intents i"
        "  JOIN workspaces w ON w.id = i.workspace_id"
        "  JOIN ig_accounts a ON a.id = i.ig_account_id"
        " WHERE i.id = :id AND i.workspace_id = :ws"
        " FOR UPDATE OF i",
        id=intent_id,
        ws=command.workspace_id,
    )
    if row is None:
        raise CommandRefused("not_found", f"intent {intent_id}")
    return row


def _refuse_if_cancelling(intent: dict[str, Any]) -> None:
    """A card whose cancellation is requested offers no lever. The flag is
    set by `cancel` and by `disable_account` (a removed destination's cards);
    the worker terminalizes at its next checkpoint (`02` §4), and until it
    does, acting on the card would post for a destination someone removed.
    Refused by its own name so a tap can say so."""
    if intent.get("cancel_requested"):
        raise CommandRefused("cancelling", f"intent {intent['id']} is being cancelled")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _actor_name(
    session, user_id: Optional[str], *, label: Optional[str] = None
) -> Optional[str]:
    """The name a shared chat may see for the actor (F3): never an email. A
    tap carries the tapper's name on the command (`Command.actor_label`, read
    with the identity — #1286) and passes it as *label*; anything else asks
    the identity table. `args` is never consulted: the web route fills it
    from the request body."""
    if label:
        return str(label)
    if not user_id:
        return None
    return await identity.display_name_for(session, user_id=str(user_id))


async def _settlement(session, *, workspace_id: str, intent_id: str) -> dict[str, Any]:
    """The card's state, who last moved it and when — for a tap that arrives
    after the decision (R6)."""
    found = await intent_ledger.settlement(
        session, workspace_id=workspace_id, intent_id=intent_id
    )
    return {
        "state": found["state"],
        "published_via": found.get("published_via"),
        "by": await _actor_name(session, found.get("by_user_id")),
        "at": found.get("at"),
    }


async def _supersede_everywhere(
    session, *, workspace_id: str, intent_id: str, outcome_text: str
) -> int:
    """Every live card for the intent, in EVERY binding of the workspace,
    loses its buttons and gains the outcome line — in the caller's transaction,
    so a rolled-back flip takes its edits with it (phase 1 step 7). One
    statement for all bindings (#1286: it was one read plus two writes per
    binding, inside the tap's transaction)."""
    return await outbox.supersede_everywhere(
        session,
        workspace_id=workspace_id,
        intent_id=intent_id,
        outcome_text=outcome_text,
    )


def _tz(intent: dict[str, Any]) -> str:
    return str(intent.get("eff_tz") or "UTC")


async def _settle(
    session,
    intent: dict[str, Any],
    command: Command,
    *,
    expected: str = "awaiting_approval",
) -> Optional[CommandResult]:
    """Read, then decide (F2 (a)). A row in ANY state other than *expected*
    (`awaiting_approval` for the approval card's four commands,
    `review_required` for the review card's resolutions) — a repeat of the
    same tap, a finished card, a card not yet prompted — answers with its
    current state and writes nothing to the intent. A terminal card is
    superseded again with that state. Returns None when the row is in the
    expected state and the caller may flip it."""
    if intent["state"] == expected:
        return None
    found = await _settlement(
        session, workspace_id=command.workspace_id, intent_id=str(intent["id"])
    )
    state = found["state"] or intent["state"]
    at = found["at"] or _utcnow()
    line = prompts.outcome_line(
        state,
        by=found["by"],
        at=at,
        tz=_tz(intent),
        published_via=found.get("published_via") or intent.get("published_via"),
    )
    if state in intent_ledger.TERMINAL_STATES:
        # A stale card heals on first touch — with its FINAL line. A card in a
        # transit state (`approved`, `publishing`, `review_required`) is not
        # superseded here: the flip's own supersede rows edited every copy,
        # and a repeat tap answers without writing (one write per tap is the
        # throughput ruling, 2026-09-12). The accepted window: a copy whose
        # supersede row failed past the resend cap keeps its buttons — the
        # sweep and `supersede_everywhere` address live rows only, and the
        # pipeline's restate-by-ref reaches a superseded card at posted,
        # failed or review — until a restate-by-ref on touch is built
        # (review of #1271; #1297 re-verify).
        await _supersede_everywhere(
            session,
            workspace_id=command.workspace_id,
            intent_id=str(intent["id"]),
            outcome_text=line,
        )
    return CommandResult(
        "answered",
        {
            "intent_id": str(intent["id"]),
            "state": state,
            "settled_by": found["by"],
            "settled_at": prompts.stamp(at, _tz(intent)),
            "outcome_text": line,
            "published_via": found.get("published_via") or intent.get("published_via"),
        },
    )


async def _record_outcome(
    session, intent: dict[str, Any], command: Command, state: str
) -> str:
    """After a flip: the outcome line, written onto every card of the intent."""
    line = prompts.outcome_line(
        state,
        by=await _actor_name(session, command.actor_user_id, label=command.actor_label),
        at=_utcnow(),
        tz=_tz(intent),
    )
    await _supersede_everywhere(
        session,
        workspace_id=command.workspace_id,
        intent_id=str(intent["id"]),
        outcome_text=line,
    )
    return line


async def _restate_everywhere(
    session, *, workspace_id: str, intent_id: str, outcome_text: str
) -> int:
    """The resolution's door to a card the approve tap already superseded:
    by ref, in every binding, one statement (`outbox.restate_everywhere`)."""
    return await outbox.restate_everywhere(
        session,
        workspace_id=workspace_id,
        intent_id=intent_id,
        outcome_text=outcome_text,
    )


async def _restate_outcome(
    session, intent: dict[str, Any], command: Command, state: str
) -> str:
    """After a review resolution: the outcome line, written by ref onto
    every card of the intent — the review keyboard goes with it (the edit
    carries no `reply_markup`, so the transport leaves none)."""
    line = prompts.outcome_line(
        state,
        by=await _actor_name(session, command.actor_user_id, label=command.actor_label),
        at=_utcnow(),
        tz=_tz(intent),
    )
    await _restate_everywhere(
        session,
        workspace_id=command.workspace_id,
        intent_id=str(intent["id"]),
        outcome_text=line,
    )
    return line


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
    _refuse_if_cancelling(intent)
    settled = await _settle(session, intent, command)
    if settled is not None:
        return settled
    if not intent["api_publishing_enabled"]:
        raise CommandRefused(
            "manual_mode",
            "this workspace publishes manually; use mark_posted after posting by hand",
        )
    # A dry run posts nowhere, so it needs no token: the owner can rehearse
    # the whole flow before Instagram is connected. The token's presence was
    # read with the intent (`_intent_row`, #1286).
    if not intent.get("dry_run_mode") and not intent.get("has_ig_credential"):
        # Said at the tap, not an hour later: without a token the publish leg
        # cannot post, and a job minted anyway would only burn its ladder and
        # land on a human (#1276 review).
        raise CommandRefused(
            "not_connected",
            "Instagram is not connected for this account — connect it in"
            " Settings › Integrations, or post by hand and use mark_posted",
        )
    await _flip(session, str(intent["id"]), "approved")
    await jobs.enqueue(
        session,
        kind="publish_pipeline",
        workspace_id=command.workspace_id,
        serialization_key=f"ig:{intent['provider_account_ref']}",
        # The pipeline's ceiling is its own (`05:38`: deadline = slot end; the
        # slot may be a day away) — the loop's deadline would end a deferred
        # publish on its first escaped error. Attempts still bound it.
        deadline_seconds=jobs.NO_DEADLINE,
        # The dry-run decision travels WITH the job: what the tapper was told
        # is what the run does, whatever the flag says by the time it runs.
        payload={
            "v": 1,
            "intent_id": str(intent["id"]),
            "dry_run": bool(intent.get("dry_run_mode")),
        },
    )
    await _record_outcome(session, intent, command, "approved")
    return CommandResult(
        "enqueued",
        {
            "intent_id": str(intent["id"]),
            "state": "approved",
            "job": "publish_pipeline",
            # What the tap's answer says next: a dry run posts nowhere; a
            # paused workspace holds the job until it is resumed.
            "dry_run": bool(intent.get("dry_run_mode")),
            "paused": bool(intent.get("is_paused")),
        },
    )


async def skip(session, command: Command) -> CommandResult:
    intent = await _intent_row(session, command)
    _refuse_if_cancelling(intent)
    settled = await _settle(session, intent, command)
    if settled is not None:
        return settled
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
    await _record_outcome(session, intent, command, "skipped")
    return _result(intent, "skipped", lock="skip", lock_days=ttl_days)


async def reject(session, command: Command) -> CommandResult:
    intent = await _intent_row(session, command)
    _refuse_if_cancelling(intent)
    settled = await _settle(session, intent, command)
    if settled is not None:
        return settled
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
    await _record_outcome(session, intent, command, "rejected")
    return _result(intent, "rejected", lock="reject")


async def mark_posted(session, command: Command) -> CommandResult:
    """The manual-mode path (`06` §3): the human posted by hand and taps
    Posted. The debit is unconditional — the story is already on Instagram,
    so refusing to record it at the cap would misstate the day, which is the
    over-posting direction R1 exists to avoid; `cap_at_write` freezes at the
    day's first debit exactly as the API path's does."""
    intent = await _intent_row(session, command)
    _refuse_if_cancelling(intent)
    settled = await _settle(session, intent, command)
    if settled is not None:
        return settled
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
    await _posted_effects(session, intent, command)
    await _record_outcome(session, intent, command, "posted")
    return _result(intent, "posted", published_via="manual")


async def _posted_effects(session, intent: dict[str, Any], command: Command) -> None:
    """The ledger's one spelling of what a post leaves (`intent_ledger.
    posted_effects`), with the tapper on the lock."""
    await intent_ledger.posted_effects(
        session,
        workspace_id=command.workspace_id,
        media_item_id=str(intent["media_item_id"]),
        ig_account_id=str(intent["ig_account_id"]),
        intent_id=str(intent["id"]),
        ttl_days=intent.get("repost_ttl_days"),
        created_by_user_id=command.actor_user_id,
    )


async def _latest_publish_op(session, intent_id: str) -> Optional[dict[str, Any]]:
    """The intent's latest `publish` permit (`provider_operations`), or None
    when Instagram was never asked to post it. Its state is the review's
    discriminator: `ambiguous` = the answer was lost (it MAY have posted);
    `failed` = Instagram answered no; `permitted` = the call may not have
    been made."""
    return await readers.row(
        session,
        "SELECT id, state FROM provider_operations"
        " WHERE intent_id = :i AND op_kind = 'publish'"
        " ORDER BY generation DESC LIMIT 1",
        i=intent_id,
    )


async def _end_op_by_verdict(
    session,
    op: Optional[dict[str, Any]],
    *,
    outcome: str,
    verdict: str,
    command: Command,
) -> None:
    """Terminalize an unresolved publish op with the member's verdict, so no
    op stays in the un-retirable class after its intent resolves (`02` §6)."""
    if op is None or op["state"] not in ("ambiguous", "permitted"):
        return
    await provider_ops.resolve_by_human(
        session,
        op_id=str(op["id"]),
        outcome=outcome,
        verdict=verdict,
        actor_user_id=command.actor_user_id,
        from_state=str(op["state"]),
    )


#: The review card's resolutions (`02` §4's `review_required` exits a member
#: may take; `failed` — a refund — stays the operator's).
RESOLUTIONS: tuple[str, ...] = ("retry", "posted", "cancel")

#: The one verdict a resolution may carry: the member looked, and the story
#: is not on Instagram. `retry` needs it when the publish answer was lost.
NOT_POSTED = "not_posted"


async def resolve_review(session, command: Command) -> CommandResult:
    """The review card is the workspace's to resolve (ruling 2026-09-12 —
    first principles for many tenants: the member is the human who can look
    at their own story, and an operator-only surface cannot scale to
    thousands of workspaces). `args.resolution`:

    - `retry`: `review_required → approved`, debit-neutral
      (`publish_cap.resolve_retry`), and the publish job is minted again
      under `approve`'s own gates (manual mode, a usable token). When the
      intent's publish answer was LOST (the latest publish op is
      `ambiguous`), Instagram may have posted: a plain retry would re-permit
      a second call beside an unresolved one — the permit rail's one
      forbidden thing — so it is refused (`may_have_posted`) unless the
      member's verdict `not_posted` rides the command (the card's "🔁 Not
      there — post again"; the web's confirm), which first ends the op.
    - `posted`: Instagram did post it — legal only when a publish call was
      made AND Instagram did not answer no (`02` §4 resolve-posted: the
      latest publish op exists and is not `failed`; `publish_step` alone
      stays `publish_called` after a definitive refusal). The debit stands,
      the post's effects are written, the op ends `succeeded` by verdict.
    - `cancel`: give up; the debit is RETAINED (`02` §4: the story may have
      published). Honoured even when a cancel was already requested; the op
      ends `failed` by verdict so it can retire.

    A row in any other state answers (read-then-decide, F2 (a))."""
    resolution = _arg(command, "resolution")
    if resolution not in RESOLUTIONS:
        raise CommandRefused(
            "invalid_args", f"resolution must be one of {', '.join(RESOLUTIONS)}"
        )
    intent = await _intent_row(session, command)
    settled = await _settle(session, intent, command, expected="review_required")
    if settled is not None:
        return settled
    intent_id = str(intent["id"])
    op = await _latest_publish_op(session, intent_id)
    if resolution == "cancel":
        return await _give_up(session, intent, command, op)
    _refuse_if_cancelling(intent)
    if resolution == "posted":
        if (
            intent.get("publish_step") != "publish_called"
            or not intent.get("ig_container_id")
            or op is None
            or op["state"] == "failed"
        ):
            raise CommandRefused(
                "nothing_to_confirm",
                "Instagram did not post this one (never asked, or it answered no)"
                " — post again, or give up",
            )
        if not await publish_cap.resolve_posted(session, intent_id=intent_id):
            raise CommandRefused(
                "illegal_transition", "the review was resolved by someone else first"
            )
        await _end_op_by_verdict(
            session, op, outcome="succeeded", verdict="posted", command=command
        )
        await _posted_effects(session, intent, command)
        await _restate_outcome(session, intent, command, "posted")
        return _result(intent, "posted", published_via="api")
    # retry — approve's gates, re-checked: the flag or the token may have
    # gone since the card was approved.
    if not intent["api_publishing_enabled"]:
        raise CommandRefused(
            "manual_mode",
            "this workspace publishes manually; give up here and post by hand",
        )
    if not intent.get("dry_run_mode") and not intent.get("has_ig_credential"):
        raise CommandRefused(
            "not_connected",
            "Instagram is not connected for this account — connect it in"
            " Settings › Integrations, or post by hand",
        )
    if op is not None and op["state"] in ("ambiguous", "permitted"):
        if command.args.get("verdict") != NOT_POSTED:
            raise CommandRefused(
                "may_have_posted",
                "Instagram may have posted this one — check the story first",
            )
        await _end_op_by_verdict(
            session, op, outcome="failed", verdict=NOT_POSTED, command=command
        )
    attempts = dict(intent.get("attempts_by_step") or {})
    attempts.setdefault("v", 1)
    attempts["retries"] = int(attempts.get("retries") or 0) + 1
    flipped = await publish_cap.resolve_retry(
        session,
        intent_id=intent_id,
        workspace_id=command.workspace_id,
        ig_account_id=str(intent["ig_account_id"]),
        attempts_by_step=attempts,
    )
    if not flipped:
        raise CommandRefused(
            "illegal_transition", "the review was resolved by someone else first"
        )
    await jobs.enqueue(
        session,
        kind="publish_pipeline",
        workspace_id=command.workspace_id,
        serialization_key=f"ig:{intent['provider_account_ref']}",
        deadline_seconds=jobs.NO_DEADLINE,
        payload={
            "v": 1,
            "intent_id": intent_id,
            "dry_run": bool(intent.get("dry_run_mode")),
        },
    )
    await _restate_outcome(session, intent, command, "approved")
    return CommandResult(
        "enqueued",
        {
            "intent_id": intent_id,
            "state": "approved",
            "job": "publish_pipeline",
            "dry_run": bool(intent.get("dry_run_mode")),
            "paused": bool(intent.get("is_paused")),
            "retries": attempts["retries"],
        },
    )


async def _give_up(
    session, intent: dict[str, Any], command: Command, op: Optional[dict[str, Any]]
) -> CommandResult:
    """`review_required → cancelled`, the debit retained; the unresolved op
    ends by verdict; the line reaches every card by ref, without buttons."""
    if not await publish_cap.resolve_cancel(session, intent_id=str(intent["id"])):
        raise CommandRefused(
            "illegal_transition", "the review was resolved by someone else first"
        )
    await _end_op_by_verdict(
        session, op, outcome="failed", verdict="given_up", command=command
    )
    await _restate_outcome(session, intent, command, "cancelled")
    return _result(intent, "cancelled")


async def cancel(session, command: Command) -> CommandResult:
    intent = await _intent_row(session, command)
    if intent["state"] == "review_required":
        # A parked row has no worker checkpoint to honour a flag at; the
        # cancel IS the give-up (2026-09-12), and the card loses its buttons.
        op = await _latest_publish_op(session, str(intent["id"]))
        return await _give_up(session, intent, command, op)
    _refuse_if_cancelling(intent)
    if intent["state"] in TERMINAL_STATES:
        raise CommandRefused(
            "illegal_transition", f"intent is already {intent['state']!r}"
        )
    await session.execute(
        text("UPDATE post_intents SET cancel_requested = true WHERE id = :id"),
        {"id": str(intent["id"])},
    )
    # The card loses its buttons now: a cancelling card offers no lever, and
    # the worker's terminalization (#1235) is a later checkpoint.
    await _record_outcome(session, intent, command, "cancelled")
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


async def _begin_drive_link(session, command: Command, *, expect: str) -> CommandResult:
    """Shared body of `connect_account` / `reconnect_account` — the chat-side
    start of the WORKSPACE's Drive grant (069, `07` §15: one Google grant per
    workspace, folders picked under it).

    THIN BY DESIGN — F1 (a). The OAuth leg is the API route's; this door only
    initiates and records. It returns the state, not a URL, because composing
    the URL needs `(client_id, redirect_uri)`, which `src/api/google_client.py`
    owns — the adapter renders, as it already does for sign-in.

    The connect/reconnect split is the SCHEMA's answer, not the caller's:
    `connect_purpose` reports which one this workspace is in, and a command
    that disagrees is refused by name rather than quietly doing the other. A
    reconnect that ran as a connect would skip `issue_state`'s
    invalidate-prior-states step (`07` §2, "last issued wins") and leave two
    live callbacks for one workspace.
    """
    purpose = await google_drive_oauth.connect_purpose(
        session, workspace_id=command.workspace_id
    )
    if purpose != expect:
        raise CommandRefused(
            "illegal_transition",
            f"this workspace's Drive needs {purpose}, not {expect}",
        )
    state = await issue_state(
        session,
        purpose=purpose,
        user_id=command.actor_user_id,
        workspace_id=command.workspace_id,
        reconnect_target=command.workspace_id,
        provider=google_drive_oauth.PROVIDER,
    )
    return CommandResult(
        "executed",
        {"provider": google_drive_oauth.PROVIDER, "purpose": purpose, "state": state},
    )


async def connect_account(session, command: Command) -> CommandResult:
    """Begin the workspace's Drive connect — it holds no grant yet."""
    return await _begin_drive_link(session, command, expect="connect")


async def reconnect_account(session, command: Command) -> CommandResult:
    """Begin the workspace's Drive reconnect — it holds a grant already."""
    return await _begin_drive_link(session, command, expect="reconnect")


async def remove_member(session, command: Command) -> CommandResult:
    """`06`: "an admin removes membership explicitly." The revoke for every
    join edge — invitations and the Telegram group path alike. The owner is
    never removable here (`transfer_ownership` is that edge) and nobody
    removes themselves through this command; both are `illegal_transition`."""
    user_id = _arg(command, "user_id")
    try:
        role = await workspaces.remove_member(
            session,
            workspace_id=command.workspace_id,
            user_id=user_id,
            by_user_id=command.actor_user_id,
        )
    except LookupError:
        raise CommandRefused("not_found", f"member {user_id}") from None
    except ValueError as exc:
        raise CommandRefused(
            "illegal_transition",
            "the owner cannot be removed"
            if str(exc) == "owner"
            else "you cannot remove yourself",
        ) from None
    return CommandResult("executed", {"user_id": user_id, "removed_role": role})


async def disable_account(session, command: Command) -> CommandResult:
    """`02`'s "active ↔ disabled (user command, audited)" edge, the disabling
    half — what the web calls Remove (owner decision 2026-09-04).

    The destination leaves the clock's scan (`fn_clock_tick` reads
    `state = 'active'` only), its Instagram credential is revoked locally,
    and its live intents are flagged `cancel_requested` — as `cancel` flags
    one; the user never writes a terminal state (`02` §4) — for the worker
    to finish, with the Queue offering a flagged card no action meanwhile.
    The row stays: the history hangs off it, and connecting the
    same account again is what brings it back (`connect_destination` adopts
    a `disabled` row and `attach_connected_identity` flips it `active`).
    The audit row is the governance trigger's, under this unit of work's
    actor. No provider call — the same scope statement as `disconnect_account`.
    """
    account_id = _arg(command, "ig_account_id")
    try:
        effects = await provisioning.disable_destination(
            session, workspace_id=command.workspace_id, ig_account_id=account_id
        )
    except provisioning.ProvisioningRefused as exc:
        if exc.reason == "already_disabled":
            raise CommandRefused(
                "illegal_transition", f"destination {account_id} is already disabled"
            ) from exc
        raise CommandRefused("not_found", f"destination {account_id}") from exc
    # Its live cards lose their buttons (phase 1 step 7): the flagged intents
    # are the ones the worker will terminalize.
    live = await readers.rows(
        session,
        "SELECT i.id, i.state, COALESCE(a.tz, w.tz) AS eff_tz FROM post_intents i"
        "  JOIN workspaces w ON w.id = i.workspace_id"
        "  JOIN ig_accounts a ON a.id = i.ig_account_id"
        " WHERE i.workspace_id = :ws AND i.ig_account_id = :acct"
        "   AND i.cancel_requested AND i.state NOT IN"
        "   ('posted','skipped','rejected','expired','failed','cancelled')",
        ws=command.workspace_id,
        acct=account_id,
    )
    for row in live:
        line = prompts.outcome_line(
            "account_disabled", by=None, at=_utcnow(), tz=str(row["eff_tz"] or "UTC")
        )
        if row["state"] == "review_required":
            # A parked row has no worker checkpoint to finish it at: the
            # removal is its give-up, and its card loses the review buttons
            # (by ref — the approve tap superseded it long ago).
            intent_id = str(row["id"])
            await publish_cap.resolve_cancel(session, intent_id=intent_id)
            await _end_op_by_verdict(
                session,
                await _latest_publish_op(session, intent_id),
                outcome="failed",
                verdict="given_up",
                command=command,
            )
            await _restate_everywhere(
                session,
                workspace_id=command.workspace_id,
                intent_id=intent_id,
                outcome_text=line,
            )
            continue
        await _supersede_everywhere(
            session,
            workspace_id=command.workspace_id,
            intent_id=str(row["id"]),
            outcome_text=line,
        )
    return CommandResult(
        "executed", {"ig_account_id": account_id, "state": "disabled", **effects}
    )


async def disconnect_account(session, command: Command) -> CommandResult:
    """F5 (a), for the WORKSPACE's Drive grant (069, `07` §15): revoke the one
    credential, KEEP the row, and pause every folder under it.

    `paused`, deliberately not `error`. A disconnect is a decision, not a
    fault, and reserving `error` for faults is what keeps the #1061 disconnect
    alert meaningful — if a user disconnecting produced `error`, that beat
    would re-alert them every day about something they chose, and someone
    would eventually silence it, taking the real faults with it. `paused` is
    outside the beat's scan by construction.

    The rows are kept rather than deleted: the media and its history hang off
    the sources, and the credential row is an audit trail nobody misses until
    they need it.

    **The best-effort Google revoke is NOT performed here, and that is a
    scope statement rather than an oversight.** A provider call inside a unit
    of work violates this codebase's checkpoint discipline, and F5 (a) makes
    the remote revoke best-effort precisely because it may fail and must not
    block the local state change. What must be atomic is the pair below; the
    remote call rides a job outside this transaction.
    """
    revoked = (
        await session.execute(
            text(
                "UPDATE oauth_credentials SET state = 'revoked'"
                " WHERE workspace_id = :ws AND provider = :provider"
                "   AND ig_account_id IS NULL AND media_source_id IS NULL"
                "   AND state <> 'revoked'"
                " RETURNING id"
            ),
            {"ws": command.workspace_id, "provider": google_drive_oauth.PROVIDER},
        )
    ).first()
    paused = await session.execute(
        text(
            "UPDATE media_sources SET state = 'paused', alerted_at = NULL"
            " WHERE workspace_id = :ws AND provider = 'gdrive' AND state <> 'paused'"
        ),
        {"ws": command.workspace_id},
    )
    # The remote half (#1083), enqueued rather than called. Everything above
    # is the atomic pair; this rides a separate transaction so a Google that
    # is slow, angry or absent cannot roll back a disconnect the user has
    # already been told succeeded — which is what F5 (a)'s "best-effort"
    # requires. Only when a row actually flipped: a repeat disconnect updates
    # nothing, and minting a second revoke for a grant already revoked would
    # spend a provider call to learn that. `unless_pending` covers the racing
    # case.
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
        {
            "provider": google_drive_oauth.PROVIDER,
            "credential_revoked": revoked is not None,
            "sources_paused": int(paused.rowcount or 0),
        },
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


async def invite_member(session, command: Command) -> CommandResult:
    """Invite someone to this workspace. Returns the invitation and its token.

    **The accept half already existed** — `fn_invitation_accept` and
    `/join/[token]` shipped with `06` §2 / #1090 G2 — so until now an
    invitation could be accepted but not created. This is the create half,
    built to the acceptor's contract rather than to a fresh design: the token
    is hashed with `sessions.token_hash` because that door resolves by
    `token_hash` alone, and `invitations.create` writes every column its D33
    identity check reads.

    **The token is returned, once.** It is the credential — possession
    accepts — and only its hash is stored, so this return value is the single
    opportunity to deliver it. A delivery producer (email, or a Telegram card
    in `06` §2's other half) is what turns it into something a person
    receives; the two share this one minting door rather than each having
    their own.

    **`delivery_channel` is the caller's, defaulting to `email`.** It was
    pinned to `email` here while `invitations.create` accepted both, which
    made the Telegram half of `06` §2 unmintable — the general writer existed
    and this door closed it one layer up. The wrong repair would have been to
    let the Telegram producer read an EMAIL invitation and post a card for it:
    that broadcasts a token minted for one person's inbox into a group, and
    `053` makes email the D33 acceptance value, so a tapper fails the identity
    match and takes the recorded-skip path — landing as `member` with an
    elevation-pending notice. It would look like it worked. Two schema facts
    make the honest shape safe instead: `uq_invite_live` is
    `(workspace_id, email)` and NULLs never collide there, so Telegram
    invitations do not conflict with each other or with an email invite to the
    same workspace; and a hint-only invitation carries no identity proof, so
    D33/D36 downgrades an admin invite on accept rather than elevating.
    (Raised by lane C rather than built around, which is what kept the
    broadcast shape out of the tier.)

    **BOUND, and read this before concluding clause 4 is done: a `telegram`
    invitation minted here is announced NOWHERE.** This change makes the mint
    possible; the card producer that would deliver it is #1188 and is not on
    `main` yet, so between these two landing, a Telegram invitation is a real
    row with a real token that no person is ever told about. That is the
    advertise-a-capability-nothing-performs shape the epic exists to remove, so
    it is stated rather than left for someone to find in the seam. It is
    inert in practice — no surface passes `delivery_channel` today, so nothing
    mints one — and it closes when #1188 wires the producer to this call site.
    The `email` arm has no such gap.

    `role` defaults to `member` and is a CEILING, never a grant: the acceptor
    downgrades an unmatched admin invite to `member` plus an
    elevation-pending notification (D36). So minting an admin invitation
    cannot itself elevate anyone, which is why the floor for this command is
    `admin` rather than `owner`.
    """
    role = command.args.get("role", "member")
    if not isinstance(role, str):
        raise CommandRefused("invalid_args", "role must be a string")
    channel = command.args.get("delivery_channel", "email")
    if not isinstance(channel, str):
        raise CommandRefused("invalid_args", "delivery_channel must be a string")
    # NOT `_arg`: that one is the required-argument reader, and an address is
    # required for an email invitation only. `invitations.create` already
    # refuses `email_required` by name, so forcing it here would move the rule
    # away from the writer that owns it and break the telegram channel.
    email = command.args.get("email")
    if email is not None and not isinstance(email, str):
        raise CommandRefused("invalid_args", "email must be a string")
    tg_user_id = command.args.get("invited_tg_user_id")
    # Bool first: `isinstance(True, int)` is True in Python, so a JSON `true`
    # would otherwise reach the column as the user id 1.
    if tg_user_id is not None and (
        isinstance(tg_user_id, bool) or not isinstance(tg_user_id, int)
    ):
        raise CommandRefused("invalid_args", "invited_tg_user_id must be an integer")
    hint = command.args.get("invited_channel_hint")
    if hint is not None and not isinstance(hint, str):
        raise CommandRefused("invalid_args", "invited_channel_hint must be a string")
    try:
        invitation_id, token = await invitations.create(
            session,
            workspace_id=command.workspace_id,
            invited_by_user_id=command.actor_user_id,
            role=role,
            delivery_channel=channel,
            email=email,
            invited_tg_user_id=tg_user_id,
            invited_channel_hint=hint,
        )
    except invitations.InvitationRefused as exc:
        # The port's closed vocabulary, not this module's: `already_invited`
        # and `email_required` are both the caller's input being wrong, and
        # `invalid_role` likewise. Mapping them to `invalid_args` keeps
        # `REASONS` closed while the detail carries which.
        raise CommandRefused("invalid_args", str(exc)) from exc

    # The email arm of `06` §2. NOT discarded: a producer whose verdict reaches
    # nobody is #1132's defect one layer up, where the work loop dropped an
    # executor's return value and a run nobody could have received recorded as
    # a success. `delivery` carries `channel` + `state` for either arm, so a
    # caller reads one shape whichever channel was used.
    delivery: dict[str, Any] = {"channel": channel}
    if channel == "email":
        job_id = await invitations.deliver_by_email(
            session,
            workspace_id=command.workspace_id,
            invitation_id=invitation_id,
            token=token,
            email=email,
            web_app_origin=settings.web_app_origin,
        )
        if job_id is None:
            delivery["state"] = "not_configured"
        else:
            delivery["state"] = "queued"
            delivery["job_id"] = job_id
    else:
        # The card producer is #1188 and is not wired here yet — see the BOUND
        # in the docstring. Reported as the gap it is rather than omitted,
        # because an absent key reads as "not applicable" and this is not that.
        delivery["state"] = "none_produced"
        delivery["cards"] = 0

    return CommandResult(
        "executed",
        {
            "invitation_id": invitation_id,
            "invite_token": token,
            "role": role,
            "delivery": delivery,
        },
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


async def restore_workspace(session, command: Command) -> CommandResult:
    """`06` §1's way back: `offboarding → active`, **within the grace window
    only**, owner-only (`ROLE_FLOOR`).

    Two writes, and the second one is the whole point.

    **THE CLOCK NEVER CONSULTS CREDENTIAL STATE.** `fn_clock_tick`'s due-scan
    (`059`) selects `WHERE a.state = 'active' AND ... AND w.state = 'active'`,
    and `ix_ig_accounts_due` is partial on the same account predicate. Nothing
    in that path looks at `oauth_credentials`. Offboarding, for its part, never
    touches `ig_accounts.state` at all — leg 2 revokes credentials and stops.

    So flipping the workspace back is NOT sufficient, and the failure it leaves
    is quiet: accounts are still `active`, their credentials are `revoked`, and
    the clock resumes planning slots that every publish then fails. `06` §1's
    "posting resumes as reconnects land" is a PRECONDITION — posting must not
    resume before them — and `ig_accounts.state = 'reauth_required'` is the only
    mechanism in the schema that expresses it, because it is the one the
    dispatcher predicate actually reads.

    A derived `credential_status` badge (#1078) renders `revoked` as
    reconnect-needed and is genuinely useful, but it is a DISPLAY fact. **A badge
    does not stop the clock.**

    **Scoped to accounts holding a revoked credential, deliberately.** A manual
    destination has no `oauth_credentials` row by construction (`provisioning`:
    "a destination needs no credential, no OAuth round trip and no Meta call to
    exist"), so there is nothing for its owner to reconnect. A blanket freeze
    would strand exactly the destinations that were never broken — currently the
    only kind the estate has.

    **The grace guard is policy; safety is structural.** `fn_offboard_finalize`
    deletes the workspace row, so a restore attempted after it ran matches
    nothing and refuses `not_found` on its own. This guard is what makes
    "irreversible after the grace window" true in the interval *before* the
    finalizer happens to run, and it reads the one shared
    `offboarding.GRACE_SECONDS_DEFAULT` so it cannot disagree with the finalizer
    about when the window closed.

    No job is cancelled or enqueued. A pending offboard job re-reads state and
    returns `not_offboarding` untouched, which is already covered.
    """
    restored = (
        await session.execute(
            text(
                "UPDATE workspaces SET state = 'active', offboarding_at = NULL"
                " WHERE id = :ws AND state = 'offboarding'"
                "   AND offboarding_at"
                "       + interval '1 second' * CAST(:g AS bigint) > now()"
                " RETURNING id::text"
            ),
            {"ws": command.workspace_id, "g": offboarding.GRACE_SECONDS_DEFAULT},
        )
    ).first()
    if restored is None:
        # Three distinct refusals, kept distinct: a caller must be able to tell
        # "no such workspace" from "not offboarding" from "too late".
        current = await readers.row(
            session,
            "SELECT state, offboarding_at"
            "       + interval '1 second' * CAST(:g AS bigint) AS closed_at"
            "  FROM workspaces WHERE id = :ws",
            ws=command.workspace_id,
            g=offboarding.GRACE_SECONDS_DEFAULT,
        )
        if current is None:
            raise CommandRefused("not_found", f"workspace {command.workspace_id}")
        if current["state"] != "offboarding":
            raise CommandRefused(
                "illegal_transition",
                f"workspace is {current['state']}, not offboarding",
            )
        raise CommandRefused(
            "illegal_transition",
            "the grace window closed at"
            f" {current['closed_at']}; this workspace can no longer be restored",
        )
    reauth = (
        await session.execute(
            text(
                "UPDATE ig_accounts a SET state = 'reauth_required'"
                " WHERE a.workspace_id = :ws AND a.state = 'active'"
                "   AND EXISTS (SELECT 1 FROM oauth_credentials c"
                "                WHERE c.ig_account_id = a.id"
                "                  AND c.workspace_id = a.workspace_id"
                "                  AND c.state = 'revoked')"
                " RETURNING a.id::text"
            ),
            {"ws": command.workspace_id},
        )
    ).all()
    # `executed`, not an invented outcome: the flip is inline, and the two
    # legal values are the documented contract (`v1.py` maps 202/200 off it).
    return CommandResult(
        "executed",
        {
            "state": "active",
            "accounts_needing_reconnect": len(reauth),
        },
    )


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
