"""Executors for the built half of the interaction-layer vocabulary.

One function per built command, all with the port's executor signature
``async (session, command) -> CommandResult``, all running INSIDE the caller's
unit of work (tenant + actor GUCs applied — the audit triggers refuse
an anonymous state change, and every write here is one). `commands.REGISTRY`
is the only importer; the not-built half of the vocabulary is `None` there and
named in `commands.UNBUILT`.

## The ledger is the authority, so these functions validate nothing twice

`intent_ledger.transition` issues the UPDATE and lets `trg_intent_guard`
decide. An illegal edge — a late approve on an expired intent,
a second skip on a skipped one — comes back as `IntentTransitionRefused`
carrying the trigger's own message, and surfaces as `illegal_transition`. That
is the rule that a late interaction renders the terminal state, never acts: the
adapter reads the current state back and shows it, and nothing was acted on.

## Where the effect lists come from

Every intent edge's side effects:

- `awaiting_approval → posted` (`mark_posted`, the manual-mode path): same
  transaction sets `published_via='manual'`, stamps the day (`cap_consumed_on`)
  and, for a cadence story, debits it (`publish_cap.flip_to_posted_by_hand`),
  `times_posted`++, the account-scoped recent lock,
  `ig_accounts.last_posted_at`. Mirrors `publish_pipeline._confirm`'s
  post-publish effects deliberately — one shape, two entry points.
- `awaiting_approval → rejected`: terminal; upserts a workspace-scoped
  permanent `reject` lock. `→ skipped`: terminal; a workspace-scoped `skip`
  lock with the workspace's skip TTL (the selection rule reads both).
- `awaiting_approval → approved` (`approve`): in a workspace with
  `api_publishing_enabled`, the approval flip PRODUCES the `publish_pipeline`
  job (its registered producer is "approval flip"). In a manual-mode
  workspace there is nothing to approve INTO — the card offers Posted / Skip
  / Reject — so `approve` is refused as `manual_mode` and the
  adapter says which command to use instead.
- `cancel`: the user never writes a terminal state. `cancel_requested` is set
  and the worker terminalizes at its next checkpoint (every
  `→ cancelled` edge's actor is the worker; pre-publish it is always
  honorable). Refused if the intent is already terminal.

`sync_now` mints the `sync_media_source{reason:'demand'}` job in the exact
shape `fn_clock_tick` mints the baseline one (`04_functions.sql`) — same lane, key,
attempts — and declines to mint a second while one is pending for that
source, since the serialization key would only queue it behind the first.
Both job rows go through `jobs.enqueue`, which is this module's only INSERT
into `jobs` — not the tier's:
`media_sync`, `work_loop.ensure_sender_jobs` and `offboarding._mint_successor`
each write the table directly where the shape needs SQL `enqueue` cannot
carry.

A caller-supplied value the writers refuse (`InvalidWorkspaceArgs`) is not
caught here: `commands.execute` maps it once, for every executor.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, time
from typing import Any, Optional, Sequence
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.config.defaults import DEFAULT_SKIP_TTL_DAYS
from src.config.settings import settings
from src.services.target import (
    _dbapi,
    past_posts_lifecycle,
    audit,
    google_drive_oauth,
    identity,
    ig_credentials,
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
    vocabulary,
    workspaces,
)
from src.services.target.commands import (
    CONNECT_PURPOSE_KIND,
    Command,
    CommandRefused,
    CommandResult,
)
from src.services.target.intent_ledger import IntentTransitionRefused
from src.utils.datetime_utils import utcnow

#: The terminal states — the reaper/worker own every edge INTO these; a
#: user command on a terminal intent renders it and acts on nothing. Re-
#: exported from `intent_ledger`, which owns the one Python copy.
TERMINAL_STATES = intent_ledger.TERMINAL_STATES


def _arg(command: Command, name: str) -> str:
    value = command.args.get(name)
    if not isinstance(value, str) or not value.strip():
        raise CommandRefused("invalid_args", f"{name} is required")
    return value.strip()


async def _intent_row(session, command: Command) -> dict[str, Any]:
    """The intent plus the workspace/account facts the effect lists need,
    read `FOR UPDATE`: the lock
    is what makes the read the DECISION — two taps racing on one card queue
    here, and the second reads the first's committed state and answers with
    it. Workspace-bound in the WHERE, not only by RLS. A string that is not
    an id is refused by name (`_id_arg`), never left to the driver."""
    intent_id = _id_arg(command, "intent_id")
    from src.services.target import source_policy

    context = await source_policy.intent_context(
        session, workspace_id=command.workspace_id, intent_id=intent_id
    )
    row = await readers.row(
        session,
        "SELECT i.id, i.workspace_id, i.state, i.media_item_id, i.ig_account_id,"
        "       i.provider_account_ref, i.cancel_requested, i.published_via,"
        "       i.publish_step, i.ig_container_id, i.attempts_by_step,"
        "       i.origin, i.schedule_slot_at, i.transit_asset_ref, i.repeat_unit,"
        "       i.repeat_days,"
        "       w.api_publishing_enabled, w.repost_ttl_days, w.skip_ttl_days,"
        "       w.dry_run_mode, w.is_paused,"
        "       COALESCE(a.posts_per_day, w.posts_per_day) AS eff_ppd,"
        "       COALESCE(a.tz, w.tz) AS eff_tz, a.handle,"
        # The publish precondition, read with the row rather than after it
        # a usable Instagram Login token for this account.
        "       "
        + ig_credentials.usable_login_sql(
            workspace="i.workspace_id", account="i.ig_account_id"
        )
        + " AS has_ig_credential"
        "  FROM post_intents i"
        "  JOIN workspaces w ON w.id = i.workspace_id"
        "  JOIN ig_accounts a ON a.id = i.ig_account_id"
        " WHERE i.id = :id AND i.workspace_id = :ws"
        " FOR UPDATE OF i",
        id=intent_id,
        ws=command.workspace_id,
        ig_provider=vocabulary.PROVIDER_IG_LOGIN,
    )
    if row is None:
        raise CommandRefused("not_found", f"intent {intent_id}")
    row["_suitability_context"] = context
    return row


def _refuse_if_cancelling(intent: dict[str, Any]) -> None:
    """A card whose cancellation is requested offers no lever. The flag is
    set by `cancel` and by `disable_account` (a removed destination's cards);
    the worker terminalizes at its next checkpoint, and until it
    does, acting on the card would post for a destination someone removed.
    Refused by its own name so a tap can say so."""
    if intent.get("cancel_requested"):
        raise CommandRefused("cancelling", f"intent {intent['id']} is being cancelled")


async def _actor_name(
    session, user_id: Optional[str], *, label: Optional[str] = None
) -> Optional[str]:
    """The name a shared chat may see for the actor: never an email. A
    tap carries the tapper's name on the command (`Command.actor_label`, read
    with the identity) and passes it as *label*; anything else asks
    the identity table. `args` is never consulted: the web route fills it
    from the request body."""
    if label:
        return str(label)
    if not user_id:
        return None
    return await identity.display_name_for(session, user_id=str(user_id))


async def _settlement(session, *, workspace_id: str, intent_id: str) -> dict[str, Any]:
    """The card's state, who last moved it and when — for a tap that arrives
    after the decision."""
    found = await intent_ledger.settlement(
        session, workspace_id=workspace_id, intent_id=intent_id
    )
    return {
        "state": found["state"],
        "published_via": found.get("published_via"),
        "approval_rule": found.get("approval_rule"),
        "by": await _actor_name(session, found.get("by_user_id")),
        "at": found.get("at"),
    }


async def _supersede_everywhere(
    session, *, workspace_id: str, intent_id: str, outcome_text: str
) -> int:
    """Every live card for the intent, in EVERY binding of the workspace,
    gains the outcome line and the story's keyboard as the sender claims the
    edit (its decision buttons go; ⚙️ Configure stays on a settled card, and
    a rejected story's or a removed item's card keeps none) — in
    the caller's transaction, so a rolled-back flip takes its edits with it.
    One statement for all bindings."""
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
    """Read, then decide. A row in ANY state other than *expected*
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
    at = found["at"] or utcnow()
    line = prompts.outcome_line(
        state,
        by=found["by"],
        at=at,
        tz=_tz(intent),
        published_via=found.get("published_via") or intent.get("published_via"),
        # A line a rule caused names it when no person moved the story.
        rule=found.get("approval_rule"),
    )
    if state in intent_ledger.TERMINAL_STATES:
        # A stale card heals on first touch — with its FINAL line. A card in a
        # transit state (`approved`, `publishing`, `review_required`) is not
        # superseded here: the flip's own supersede rows edited every copy,
        # and a repeat tap answers without writing (one write per tap is the
        # throughput rule). The card the tap came from is edited again when
        # its newest edit did not land (`outbox.adopt_card`): the sweep and
        # `supersede_everywhere` address live rows only, and the pipeline's
        # restate-by-ref reaches a superseded card at posted, failed or
        # review.
        adopted = await _supersede_and_adopt(session, intent, command, line)
    else:
        # The adopted card's keyboard is the sender's to render, from the
        # story as it is when the edit goes.
        adopted = await _adopt_touched(session, intent, command, line)
    return CommandResult(
        "answered",
        {
            "intent_id": str(intent["id"]),
            # A repeat tap that adopted a card WROTE (a row and a paced edit):
            # the tap's admission spends on it.
            "adopted": adopted,
            "state": state,
            "settled_by": found["by"],
            "settled_at": prompts.stamp(at, _tz(intent)),
            "outcome_text": line,
            "published_via": found.get("published_via") or intent.get("published_via"),
        },
    )


async def _adopt_touched(
    session,
    intent: dict[str, Any],
    command: Command,
    line: str,
) -> bool:
    """Restate-by-ref on touch. The message a tap came from may be one the
    ledger never learned (a copy with buttons whose send lost its answer),
    or a known card of the story whose newest edit did not land. The tap is
    the moment its id is known: adopt it as a card of the story, or queue
    its edit again, with *line* — the same line every known card carries,
    and the story's keyboard as the sender claims the edit — so every later
    line reaches it too. Every tap that writes a line calls this: the
    deciding tap, a resolution, a repeat on a settled story (the deciding
    tap is as likely to come from such a copy). A web command carries no
    card and adopts nothing."""
    if not (command.card_ref and command.binding_id):
        return False
    return await outbox.adopt_card(
        session,
        workspace_id=command.workspace_id,
        binding_id=command.binding_id,
        intent_id=str(intent["id"]),
        ref=command.card_ref,
        outcome_text=line,
    )


async def _supersede_and_adopt(
    session, intent: dict[str, Any], command: Command, line: str
) -> bool:
    """Supersede every known card with *line*, and — when the card the tap
    came from was not among them — adopt it (:func:`_adopt_touched`). The
    every-binding door answers both in its one statement, so the ordinary
    tap, from a card the ledger knows, needs no further read. Returns whether
    a card was adopted."""
    if not (command.card_ref and command.binding_id):
        await _supersede_everywhere(
            session,
            workspace_id=command.workspace_id,
            intent_id=str(intent["id"]),
            outcome_text=line,
        )
        return False
    touched = await outbox.supersede_everywhere_touched(
        session,
        workspace_id=command.workspace_id,
        intent_id=str(intent["id"]),
        outcome_text=line,
        touched=(command.binding_id, command.card_ref),
    )
    if touched.touched_known:
        return False
    return await _adopt_touched(session, intent, command, line)


async def _restate_and_adopt(
    session, intent: dict[str, Any], command: Command, line: str
) -> bool:
    """`_supersede_and_adopt`'s shape for the review resolutions, which
    restate by ref."""
    if not (command.card_ref and command.binding_id):
        await _restate_everywhere(
            session,
            workspace_id=command.workspace_id,
            intent_id=str(intent["id"]),
            outcome_text=line,
        )
        return False
    touched = await outbox.restate_everywhere_touched(
        session,
        workspace_id=command.workspace_id,
        intent_id=str(intent["id"]),
        outcome_text=line,
        touched=(command.binding_id, command.card_ref),
    )
    if touched.touched_known:
        return False
    return await _adopt_touched(session, intent, command, line)


async def _record_outcome(
    session,
    intent: dict[str, Any],
    command: Command,
    state: str,
    *,
    series: Optional[str] = None,
) -> str:
    """After a flip: the outcome line, written onto every card of the intent
    — and onto the card the tap came from, when the ledger did not know it.
    *series* says the act ended the story's series (`prompts.outcome_line`)."""
    line = prompts.outcome_line(
        state,
        by=await _actor_name(session, command.actor_user_id, label=command.actor_label),
        at=utcnow(),
        tz=_tz(intent),
        series=series,
    )
    await _supersede_and_adopt(session, intent, command, line)
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
    session,
    intent: dict[str, Any],
    command: Command,
    state: str,
    *,
    series: Optional[str] = None,
) -> str:
    """After a review resolution, or a stop sent while a cancel lands: the
    outcome line, written by ref onto every card of the intent. The review
    buttons go with the review: the sender gives each edit the story's
    keyboard as it is then.
    *series* says the act ended the story's series (`prompts.outcome_line`)."""
    line = prompts.outcome_line(
        state,
        by=await _actor_name(session, command.actor_user_id, label=command.actor_label),
        at=utcnow(),
        tz=_tz(intent),
        series=series,
    )
    await _restate_and_adopt(session, intent, command, line)
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
    # read with the intent (`_intent_row`).
    if not intent.get("dry_run_mode") and not intent.get("has_ig_credential"):
        # Said at the tap, not an hour later: without a token the publish leg
        # cannot post, and a job minted anyway would only burn its ladder and
        # land on a human.
        raise CommandRefused(
            "not_connected",
            "Instagram is not connected for this account — connect it in"
            " Settings › Accounts, or post by hand and use mark_posted",
        )
    await _admit_media(session, command, intent, require_validation=True)
    await _flip(session, str(intent["id"]), "approved")
    await jobs.mint_publish_job(
        session,
        workspace_id=command.workspace_id,
        intent_id=str(intent["id"]),
        provider_account_ref=intent["provider_account_ref"],
        dry_run=bool(intent.get("dry_run_mode")),
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
    # Reject ends a series: the successor trigger leaves `rejected` out.
    series = prompts.SERIES_ENDED if intent.get("repeat_unit") else None
    await _record_outcome(session, intent, command, "rejected", series=series)
    return _result(intent, "rejected", lock="reject")


async def mark_posted(session, command: Command) -> CommandResult:
    """The manual-mode path: the human posted by hand and taps
    Posted. The flip and the day's debit are the cap ledger's one statement
    (`publish_cap.flip_to_posted_by_hand`)."""
    intent = await _intent_row(session, command)
    _refuse_if_cancelling(intent)
    settled = await _settle(session, intent, command)
    if settled is not None:
        return settled
    posted = await publish_cap.flip_to_posted_by_hand(
        session,
        intent_id=str(intent["id"]),
        workspace_id=command.workspace_id,
        ig_account_id=str(intent["ig_account_id"]),
        tz=_tz(intent),
        effective_cap=int(intent["eff_ppd"]),
    )
    if not posted:
        # Not awaiting approval any more: render the current state, act on
        # nothing. The flip matched no row, so nothing was debited.
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
    op stays in the un-retirable class after its intent resolves."""
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


#: The review card's resolutions (the `review_required` exits a member
#: may take; `failed` — a refund — stays the operator's).
RESOLUTIONS: tuple[str, ...] = vocabulary.RESOLUTIONS

#: The one verdict a resolution may carry: the member looked, and the story
#: is not on Instagram. `retry` needs it when the publish answer was lost.
NOT_POSTED = vocabulary.NOT_POSTED

#: What a resolution is refused with when the conditional flip matched no row:
#: the three `publish_cap.resolve_*` calls each race the others, and whichever
#: loses says the same thing. Local to this file on purpose — it is one
#: sentence for one reason code, not a shared vocabulary entry.
_RESOLVED_BY_SOMEONE_ELSE = "the review was resolved by someone else first"


async def resolve_review(session, command: Command) -> CommandResult:
    """The review card is the workspace's to resolve (first principles for
    many tenants: the member is the human who can look at their own story,
    and an operator-only surface cannot scale to thousands of workspaces).
    `args.resolution`:

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
      made AND Instagram did not answer no (the
      latest publish op exists and is not `failed`; `publish_step` alone
      stays `publish_called` after a definitive refusal). The debit stands,
      the post's effects are written, the op ends `succeeded` by verdict.
    - `cancel`: give up; the debit is RETAINED (the story may have
      published). Honoured even when a cancel was already requested; the op
      ends `failed` by verdict so it can retire.

    A row in any other state answers (read-then-decide)."""
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
            raise CommandRefused("illegal_transition", _RESOLVED_BY_SOMEONE_ELSE)
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
            " Settings › Accounts, or post by hand",
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
    # A fresh float: the class counters and the float's start are
    # zeroed — a story parked after six fetch waits must not re-park on its
    # first refusal — and only the retry count is kept.
    previous = dict(intent.get("attempts_by_step") or {})
    attempts = {"v": 1, "retries": int(previous.get("retries") or 0) + 1}
    flipped = await publish_cap.resolve_retry(
        session,
        intent_id=intent_id,
        workspace_id=command.workspace_id,
        ig_account_id=str(intent["ig_account_id"]),
        attempts_by_step=attempts,
    )
    if not flipped:
        raise CommandRefused("illegal_transition", _RESOLVED_BY_SOMEONE_ELSE)
    await jobs.mint_publish_job(
        session,
        workspace_id=command.workspace_id,
        intent_id=str(intent["id"]),
        provider_account_ref=intent["provider_account_ref"],
        dry_run=bool(intent.get("dry_run_mode")),
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
    session,
    intent: dict[str, Any],
    command: Command,
    op: Optional[dict[str, Any]],
    *,
    series: Optional[str] = None,
) -> CommandResult:
    """`review_required → cancelled`, the debit retained; the unresolved op
    ends by verdict; the line reaches every card by ref, without buttons.

    A story that reached the transit upload carries a copy on Cloudinary,
    and the API holds no transit credentials to destroy it. The give-up
    mints a `publish_pipeline` job in the same transaction instead: the
    worker meets the intent already `cancelled`, destroys the copy and
    finalizes — the pipeline's terminal route, which never posts (the row
    is frozen before the job is visible). Without the job, the backstop sweep
    would hold the copy for up to two days."""
    if not await publish_cap.resolve_cancel(session, intent_id=str(intent["id"])):
        raise CommandRefused("illegal_transition", _RESOLVED_BY_SOMEONE_ELSE)
    await _end_op_by_verdict(
        session, op, outcome="failed", verdict="given_up", command=command
    )
    if intent.get("transit_asset_ref"):
        await jobs.mint_publish_job(
            session,
            workspace_id=command.workspace_id,
            intent_id=str(intent["id"]),
            provider_account_ref=intent["provider_account_ref"],
            dry_run=bool(intent.get("dry_run_mode")),
        )
    await _restate_outcome(session, intent, command, "cancelled", series=series)
    return _result(intent, "cancelled")


async def cancel(session, command: Command) -> CommandResult:
    """Cancel a story. With `stop_repeat` it also ends the story's series:
    the rule is cleared in this transaction, before the ending that would
    make its next occurrence (`tg_intent_repeat`). Without it, the
    series goes on from the next time."""
    stop_repeat = _flag_arg(command, "stop_repeat")
    intent = await _intent_row(session, command)
    # A parked row has no worker checkpoint to honour a flag at; the cancel IS
    # the give-up, and the card loses its buttons.
    parked = intent["state"] == "review_required"
    if not parked:
        if (
            stop_repeat
            and intent.get("cancel_requested")
            and intent.get("repeat_unit") is not None
            and intent["state"] not in TERMINAL_STATES
        ):
            # Already being cancelled: that cancel stands, and its series is
            # the one thing left to stop. Clearing a rule posts nothing. The
            # card the first cancel superseded now says the series stopped.
            stopped = await _stop_repeat(session, intent, stop_repeat=True)
            await _restate_outcome(
                session, intent, command, "cancelled", series=prompts.SERIES_STOPPED
            )
            return _result(
                intent, intent["state"], cancel_requested=True, repeat_stopped=stopped
            )
        _refuse_if_cancelling(intent)
        if intent["state"] in TERMINAL_STATES:
            raise CommandRefused(
                "illegal_transition", f"intent is already {intent['state']!r}"
            )
    stopped = (
        {"repeat_stopped": await _stop_repeat(session, intent, stop_repeat=True)}
        if stop_repeat
        else {}
    )
    series = prompts.SERIES_STOPPED if stopped.get("repeat_stopped") else None
    if parked:
        op = await _latest_publish_op(session, str(intent["id"]))
        result = await _give_up(session, intent, command, op, series=series)
        return CommandResult(result.outcome, {**result.data, **stopped})
    await session.execute(
        text(
            "UPDATE post_intents SET cancel_requested = true"
            " WHERE id = :id AND workspace_id = :ws"
        ),
        {"id": str(intent["id"]), "ws": command.workspace_id},
    )
    # The flag is not a state change, so the intent's audit trigger writes
    # nothing for it: the request is recorded here, naming the person.
    await _audit_intent(
        session,
        intent,
        from_state=intent["state"],
        to_state=intent["state"],
        detail={"event": "cancel_requested"},
    )
    # The card loses its decision buttons now: a cancelling card offers no
    # lever but ⚙️ Configure (`prompts.card_keyboard`'s *cancelling*), and the
    # worker's terminalization is a later checkpoint (the reaper, `fn_reaper_sweep` cancel leg).
    await _record_outcome(session, intent, command, "cancelled", series=series)
    return _result(intent, intent["state"], cancel_requested=True, **stopped)


# --- a planned story ---------------------------------------------------

#: A planned story's time as the person gives it: a date and a clock time,
#: seconds optional, and NO offset — the account's zone makes it an instant.
#: Postgres would read an offset on a `timestamp` and silently drop it.
_LOCAL_AT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}(:[0-9]{2})?")

#: The wall time :local_at in the zone :tz as an instant, NULL when the
#: clocks skip it: `fn_wall_instant`, the reading the repeat grid
#: (`fn_repeat_times`) also takes, so a one-off time and a repeat's time never
#: disagree. Two readings are candidates: Postgres's own (a skipped time takes
#: the offset before the jump, an ambiguous one the offset after it — its
#: SECOND occurrence), and the reading with a day earlier's offset. The answer
#: is the earliest candidate that reads back as the wall time: a skipped time
#: has none, an ambiguous one is its first occurrence. The zone is the
#: database's reading, passed as given; unlike the clock, which reads a zone
#: through `fn_safe_tz`, a stored zone the tzdata has since withdrawn fails the
#: request here (an error, not a refusal) rather than being read as UTC — a
#: planned time is never guessed.
_INSTANT = (
    "SELECT fn_wall_instant(CAST(CAST(:local_at AS text) AS timestamp),"
    "                       CAST(:tz AS text)) AS at,"
    "       now() AS now,"
    "       now() + make_interval(days => CAST(:horizon AS integer)) AS horizon"
)


def _id_arg(command: Command, name: str) -> str:
    """`_arg` for an id: a string that is not one is refused by name, not
    left to a failed cast the database answers with a 500."""
    value = _arg(command, name)
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise CommandRefused("invalid_args", f"{name} is not an id") from None


def _flag_arg(command: Command, name: str) -> bool:
    """An optional true-or-false argument, false when absent."""
    value = command.args.get(name, False)
    if not isinstance(value, bool):
        raise CommandRefused("invalid_args", f"{name} is true or false")
    return value


def _local_at(command: Command) -> str:
    """The wall time: its shape checked here, the date by the parser."""
    value = _arg(command, "local_at")
    if not _LOCAL_AT.fullmatch(value):
        raise CommandRefused(
            "invalid_args",
            "local_at is a date and a time with no offset: YYYY-MM-DD HH:MM",
            facts={"at_rule": "shape"},
        )
    try:
        return datetime.fromisoformat(value).isoformat(sep=" ")
    except ValueError:
        raise CommandRefused(
            "invalid_args",
            f"local_at {value!r} is not a date and time",
            facts={"at_rule": "not_a_date"},
        ) from None


async def _planned_instant(session, *, local_at: str, tz: str) -> datetime:
    """When a planned story is due: *local_at* in *tz*, after now and within
    the horizon, both as the database's clock reads them."""
    found = await readers.row(
        session,
        _INSTANT,
        local_at=local_at,
        tz=tz,
        horizon=vocabulary.PLAN_HORIZON_DAYS,
    )
    at = found["at"]
    if at is None:
        raise CommandRefused(
            "invalid_args",
            f"{local_at} does not happen in {tz}: the clocks skip it",
            facts={"at_rule": "skipped"},
        )
    if at <= found["now"]:
        raise CommandRefused(
            "invalid_args",
            f"{local_at} {tz} is not in the future",
            facts={"at_rule": "past"},
        )
    if at > found["horizon"]:
        raise CommandRefused(
            "invalid_args",
            f"{local_at} {tz} is more than {vocabulary.PLAN_HORIZON_DAYS} days ahead",
            facts={"at_rule": "horizon"},
        )
    return at


async def _audit_intent(
    session,
    intent: dict[str, Any],
    *,
    from_state: Optional[str],
    to_state: str,
    detail: dict[str, Any],
) -> None:
    """An intent's audit row for a write its trigger cannot see — a birth, a
    new time, a flag — under the command's actor (the unit of work's GUCs)."""
    await audit.record(
        session,
        workspace_id=str(intent["workspace_id"]),
        entity_kind="post_intent",
        entity_id_sql=":intent",
        from_state=from_state,
        to_state=to_state,
        detail={"v": 1, **detail},
        intent=str(intent["id"]),
    )


async def _audit_same_state(
    session,
    command: Command,
    *,
    entity_kind: str,
    entity_id_sql: str,
    state: str,
    detail: dict[str, Any],
    **params: Any,
) -> None:
    """The audit row for a write that changes no state and that no audit
    trigger records (an item's link or flag, a folder's position), under the
    command's actor (the unit of work's GUCs). The entity's own *state* rides
    on both sides, so `audit.moved` never reads the row as a move.
    *entity_id_sql* names the entity's bind, which rides in *params*, as
    `audit.record` asks. *detail* is stored as given, its `"v": 1` included;
    `_audit_intent` adds its own."""
    await audit.record(
        session,
        workspace_id=command.workspace_id,
        entity_kind=entity_kind,
        entity_id_sql=entity_id_sql,
        from_state=state,
        to_state=state,
        detail=detail,
        **params,
    )


#: The CHECKs that judge a repeat rule, matched by name: the terminal
#: guard raises the same SQLSTATE (23514) and names no constraint.
_REPEAT_CHECKS = ("ck_intent_repeat", "ck_intent_repeat_unit")

#: Writes a story's repeat rule, the four columns together, and answers it in
#: its one shape (`intent_ledger.repeat_sql`). With every value None
#: (`_NO_REPEAT`) it clears the rule, as `_stop_repeat` does for both
#: `reschedule_item` and `cancel`.
_WRITE_REPEAT = (
    "UPDATE post_intents SET repeat_unit = :repeat_unit,"
    " repeat_every = :repeat_every, repeat_days = CAST(:repeat_days AS smallint[]),"
    " repeat_at = CAST(:repeat_at AS time)"
    " WHERE id = :id AND workspace_id = :ws"
    f" RETURNING {intent_ledger.repeat_sql()} AS repeat"
)
_NO_REPEAT: dict[str, Any] = dict.fromkeys(
    ("repeat_unit", "repeat_every", "repeat_days", "repeat_at")
)


def _smallint(value: Any) -> bool:
    """A whole number a smallint column holds; a bool is not one."""
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and -32768 <= value <= 32767
    )


def _bad_repeat(message: str) -> CommandRefused:
    return CommandRefused("invalid_args", message, facts={"repeat": "invalid"})


#: The ISO weekdays, as `repeat_days` stores them.
_ISO_WEEKDAYS = frozenset(range(1, 8))


def _clock(wall: datetime) -> time:
    """A rule's time of day: *wall*'s, to the minute, as every surface shows
    it (`intent_ledger.repeat_sql`)."""
    return wall.time().replace(second=0, microsecond=0)


def _on_its_day(
    days: Sequence[int], wall: datetime, *, carry: bool = True
) -> list[int]:
    """A weekly series' days, its story on *wall*'s date. That day must be one
    of them: off them, the series would post on it and on its own days as
    well. A series of one day moves its day with the story when *carry*;
    days a person named, or several, are refused instead."""
    day = wall.isoweekday()
    if day in days:
        return sorted(set(days))
    if carry and len(set(days)) == 1:
        return [day]
    raise CommandRefused(
        "invalid_args",
        f"the story falls on {vocabulary.WEEKDAYS[day - 1].capitalize()},"
        " which is not one of the series' days",
        facts={"repeat": "off_day"},
    )


def _repeat_arg(
    command: Command, *, wall: datetime, kept_days: Optional[Sequence[int]] = None
) -> Optional[dict[str, Any]]:
    """`args["repeat"]` as the rule's four columns, or None when it is absent
    or null. Only types are checked here: `unit` is text, and `every` and each
    of `days` a whole number that fits its column. Absent, `every` is 1, and a
    weekly rule's `days` are *kept_days*, those of the weekly series it
    replaces, else *wall*'s weekday. Either way the story's own day must be
    one of them (`_on_its_day`). Bounds and combinations are the CHECKs' to
    refuse (`_write_rule`). The rule's time of day is *wall*'s (`_clock`), the
    story's own local time, so the first occurrence is the chosen time."""
    rule = command.args.get("repeat")
    if rule is None:
        return None
    if not isinstance(rule, dict) or not set(rule) <= {"unit", "every", "days"}:
        raise _bad_repeat("repeat is an object of unit, every and days")
    unit, every, days = rule.get("unit"), rule.get("every"), rule.get("days")
    every = 1 if every is None else every
    if not (
        isinstance(unit, str)
        and _smallint(every)
        and (days is None or (isinstance(days, list) and all(map(_smallint, days))))
    ):
        raise _bad_repeat(
            "repeat is a unit, a whole number every, and days as ISO weekdays"
        )
    if unit == "week":
        if days is None:
            days = _on_its_day(kept_days or [wall.isoweekday()], wall)
        elif set(days) <= _ISO_WEEKDAYS:  # a day outside the week is the CHECK's
            days = _on_its_day(days, wall, carry=False)
    return {
        "repeat_unit": unit,
        "repeat_every": every,
        "repeat_days": None if days is None else sorted(set(days)),
        "repeat_at": _clock(wall),
    }


async def _write_rule(session, sql: str, **params: Any) -> Optional[dict[str, Any]]:
    """One statement that writes a repeat rule. A `_REPEAT_CHECKS` violation is
    the caller's rule being wrong, and is refused by the constraint's name. Any
    other error, the terminal guard's same SQLSTATE among them, is not."""
    try:
        return await readers.row(session, sql, **params)
    except DBAPIError as exc:
        for name in _REPEAT_CHECKS:
            if _dbapi.constraint_violated(exc, name):
                raise _bad_repeat(f"the repeat rule is not valid ({name})") from exc
        raise


async def _stop_repeat(session, intent: dict[str, Any], **detail: Any) -> bool:
    """End *intent*'s series: its rule cleared and the clear audited, *detail*
    naming how, so its ending makes no successor. False, with nothing written,
    when it has no rule."""
    if intent.get("repeat_unit") is None:
        return False
    await session.execute(
        text(_WRITE_REPEAT),
        {**_NO_REPEAT, "id": str(intent["id"]), "ws": str(intent["workspace_id"])},
    )
    await _audit_intent(
        session,
        intent,
        from_state=intent["state"],
        to_state=intent["state"],
        detail={"event": "repeat_cleared", **detail},
    )
    return True


async def _local_wall(session, *, at: datetime, tz: str) -> datetime:
    """*at* as a wall time in *tz*, as the database reads the zone: where a
    rule set on a story without moving it repeats."""
    found = await readers.row(
        session,
        "SELECT CAST(:at AS timestamptz) AT TIME ZONE CAST(:tz AS text) AS wall",
        at=at,
        tz=tz,
    )
    return found["wall"]


async def schedule_item(session, command: Command) -> CommandResult:
    """A planned story: this item, to this account, at this wall time in the
    account's zone. It is born `scheduled` with `origin = 'planned'`,
    is served at its time or missed out loud, and only a person
    approves it (`post_intents.origin`).

    The database decides what it can: the account must be live here, and
    its row is held (`FOR SHARE`) until the story is in, so a removal that
    disables it either waits and then flags the story, or lands first and the
    account reads as gone; the time must exist in its zone, and
    `uq_intent_live_subject` — the same item waiting on the same account — is
    the INSERT's to refuse, with no read before it. The lock and item rule is the one decision made
    here (`vocabulary.BLOCKING_LOCKS`: an item that cannot post, or a lock
    that would miss it at its time), and `override_locks` gets past only its
    warnings.

    A `repeat` rule rides on the story (repeat rules). Nothing is computed
    here: the first occurrence is the chosen time, and each one's ending
    makes the next (`tg_intent_repeat`)."""
    account_id = _id_arg(command, "ig_account_id")
    media_id = _id_arg(command, "media_item_id")
    local_at = _local_at(command)
    override = _flag_arg(command, "override_locks")
    rule = _repeat_arg(command, wall=datetime.fromisoformat(local_at)) or _NO_REPEAT
    from src.services.target import source_policy

    context = await source_policy.suitability_context(
        session, workspace_id=command.workspace_id, ig_account_ids=[account_id]
    )
    account = await readers.row(
        session,
        "SELECT a.provider_account_ref, COALESCE(a.tz, w.tz) AS eff_tz"
        "  FROM ig_accounts a JOIN workspaces w ON w.id = a.workspace_id"
        " WHERE a.id = :acct AND a.workspace_id = :ws"
        f"   AND {intent_ledger.LIVE_ACCOUNT}"
        " FOR SHARE OF a",
        acct=account_id,
        ws=command.workspace_id,
    )
    if account is None:
        raise CommandRefused(
            "not_found", f"account {account_id}", facts={"missing": "account"}
        )
    item = await readers.row(
        session,
        "SELECT m.state FROM media_items m WHERE m.id = :media AND m.workspace_id = :ws FOR UPDATE",
        acct=account_id,
        media=media_id,
        ws=command.workspace_id,
    )
    if item is None:
        raise CommandRefused("not_found", f"item {media_id}", facts={"missing": "item"})
    tz = _tz(account)
    at = await _planned_instant(session, local_at=local_at, tz=tz)
    verdicts = await source_policy.suitability(
        session,
        workspace_id=command.workspace_id,
        ig_account_id=account_id,
        media_item_id=media_id,
        require_validation=False,
        context=context,
    )
    verdict = verdicts[0] if verdicts else None
    if verdict is None or not verdict["base_suitable"]:
        if (
            verdict
            and verdict["provider"] == "gdrive"
            and verdict["reason"] == "unavailable"
        ):
            raise CommandRefused(
                "locked",
                "media is unavailable",
                facts={
                    "in_the_way": [
                        f"item_{item['state']}",
                        *verdict["blocking_locks"],
                        *verdict["warning_locks"],
                    ],
                    "overridable": False,
                },
            )
        raise CommandRefused(
            "media_unsuitable",
            "media cannot be selected",
            facts={"source_reason": verdict["reason"] if verdict else "not_found"},
        )
    blockers = list(verdict["blocking_locks"])
    warnings = list(verdict["warning_locks"])
    if blockers or (warnings and not override):
        in_the_way = blockers + warnings
        raise CommandRefused(
            "locked",
            f"item {media_id}: {', '.join(in_the_way)}"
            + ("" if blockers else " — override_locks schedules it anyway"),
            facts={"in_the_way": in_the_way, "overridable": not blockers},
        )
    born = await _write_rule(
        session,
        "INSERT INTO post_intents (workspace_id, ig_account_id, media_item_id,"
        " provider_account_ref, approval_mode, schedule_slot_at, state, origin,"
        " scheduled_by_user_id, repeat_unit, repeat_every, repeat_days, repeat_at)"
        " VALUES (:ws, :acct, :media, :ref, 'manual', :at, 'scheduled',"
        "         'planned', :by, :repeat_unit, :repeat_every,"
        "         CAST(:repeat_days AS smallint[]), CAST(:repeat_at AS time))"
        # `uq_intent_live_subject` is the arbiter, named by its predicate: the
        # database refuses the duplicate, with no read before it
        " ON CONFLICT (workspace_id, media_item_id, ig_account_id)"
        f" WHERE {intent_ledger.NOT_TERMINAL} DO NOTHING"
        f" RETURNING id, workspace_id, {intent_ledger.repeat_sql()} AS repeat",
        ws=command.workspace_id,
        acct=account_id,
        media=media_id,
        ref=account["provider_account_ref"],
        at=at,
        by=command.actor_user_id,
        **rule,
    )
    if born is None:
        # The same item already waits on this account; the database said so,
        # and the story in the way is named so a person can find it — with its
        # cancel flag, since a story cancelled a moment ago still waits.
        existing = await readers.row(
            session,
            "SELECT id, state, origin, cancel_requested FROM post_intents"
            f" WHERE {intent_ledger.LIVE_SUBJECT}",
            ws=command.workspace_id,
            media=media_id,
            acct=account_id,
        )
        raise CommandRefused(
            "illegal_transition",
            f"item {media_id} is already waiting to post on account {account_id}",
            facts={
                "existing": {
                    "intent_id": str(existing["id"]),
                    "state": existing["state"],
                    "origin": existing["origin"],
                    "cancel_requested": existing["cancel_requested"],
                }
            }
            if existing
            else {},
        )
    detail: dict[str, Any] = {
        "event": "scheduled",
        "at": at.isoformat(),
        "tz": tz,
        "local_at": local_at,
    }
    if warnings:
        detail["override"] = warnings
    if born["repeat"]:
        detail["repeat"] = born["repeat"]
    await _audit_intent(
        session, born, from_state=None, to_state="scheduled", detail=detail
    )
    bound = await prompts.push_bindings(session, command.workspace_id)
    return _result(
        born,
        "scheduled",
        schedule_slot_at=at.isoformat(),
        tz=tz,
        local_at=local_at,
        repeat=born["repeat"],
        overridden=warnings,
        # Nothing is served where no chat is bound: said, not refused.
        warnings=[] if bound else [vocabulary.NO_PUSH_BINDING],
    )


async def reschedule_item(session, command: Command) -> CommandResult:
    """A planned story's new time, its repeat rule, or both, in place:
    the same row and id, so nothing else about it can change here — a
    different item or account is a cancel and a new schedule. The row is read
    under its lock, so that read decides, as it does for every lever on a
    story.

    `local_at` moves the story and a `repeat` object sets its rule; both need
    it still waiting for its time. A new time moves a repeating story's time
    of day with it, so the series follows the moved occurrence, and
    a weekly series' day: one of its days, or its only day carried along
    (`_on_its_day`). A weekly rule set without days keeps a weekly series'
    days. `repeat: null` clears the rule in any live state, a story being
    cancelled included, and the story stays where it is."""
    moving = command.args.get("local_at") is not None
    ruling = "repeat" in command.args
    setting = ruling and command.args["repeat"] is not None
    if not (moving or ruling):
        raise CommandRefused("invalid_args", "local_at or repeat is required")
    intent = await _intent_row(session, command)
    waiting = moving or setting
    if waiting:
        allowed = intent["state"] == "scheduled"
        only = "only a planned story still waiting for its time moves or takes a repeat"
    else:
        allowed = intent["state"] not in TERMINAL_STATES
        only = "only a live planned story's repeat is cleared"
    if intent["origin"] != "planned" or not allowed:
        raise CommandRefused(
            "illegal_transition",
            f"{only} (this one is {intent['origin']}, {intent['state']})",
        )
    if waiting:
        # Clearing a rule is no lever on the story: it posts nothing.
        _refuse_if_cancelling(intent)
        # Nor does a cleared rule ask the item's media to be postable.
        await _admit_media(session, command, intent, require_validation=False)
    tz = _tz(intent)
    ids = {"id": str(intent["id"]), "ws": command.workspace_id}
    weekly = intent.get("repeat_days") if intent.get("repeat_unit") == "week" else None
    at, answer = intent["schedule_slot_at"], {}
    if moving:
        local_at = _local_at(command)
        at = await _planned_instant(session, local_at=local_at, tz=tz)
        wall = datetime.fromisoformat(local_at)
        answer = {
            "previous_slot_at": intent["schedule_slot_at"].isoformat(),
            "local_at": local_at,
        }
    if setting:
        if not moving:
            wall = await _local_wall(session, at=at, tz=tz)
        rule = _repeat_arg(command, wall=wall, kept_days=weekly)
    repeat = None
    if moving:
        # The series follows the story it moves: its time of day, and a weekly
        # series' day. A rule set alongside is written next, and decides both.
        days = _on_its_day(weekly, wall) if weekly and not setting else None
        repeat = (
            await readers.row(
                session,
                "UPDATE post_intents SET schedule_slot_at = :at,"
                " repeat_at = CASE WHEN repeat_unit IS NULL THEN NULL"
                "                  ELSE CAST(:wall AS time) END,"
                " repeat_days = COALESCE(CAST(:days AS smallint[]), repeat_days)"
                " WHERE id = :id AND workspace_id = :ws"
                f" RETURNING {intent_ledger.repeat_sql()} AS repeat",
                at=at,
                wall=_clock(wall),
                days=days,
                **ids,
            )
        )["repeat"]
        detail = {
            "event": "rescheduled",
            "from": answer["previous_slot_at"],
            "to": at.isoformat(),
            "tz": tz,
            "local_at": local_at,
        }
        if repeat:
            detail["repeat_at"] = repeat["at"]
            if days is not None and days != sorted(set(weekly)):
                detail["repeat_days"] = days
        await _audit_intent(
            session, intent, from_state="scheduled", to_state="scheduled", detail=detail
        )
    if setting:
        repeat = (await _write_rule(session, _WRITE_REPEAT, **rule, **ids))["repeat"]
        await _audit_intent(
            session,
            intent,
            from_state=intent["state"],
            to_state=intent["state"],
            detail={"event": "repeat_set", "repeat": repeat},
        )
    elif ruling:
        answer["repeat_stopped"] = await _stop_repeat(session, intent)
        repeat = None
    return _result(
        intent,
        intent["state"],
        schedule_slot_at=at.isoformat(),
        tz=tz,
        repeat=repeat,
        **answer,
    )


def _link_url(command: Command) -> Optional[str]:
    """`link_url`: an `https://` address of at most `LINK_URL_MAX` characters
    with no space, control character, user name or password in it, or `null`
    to clear the link.
    It reaches a card as a line of text and the web as an anchor, so only
    https is taken; a missing key is refused, never read as a clear."""
    if "link_url" not in command.args:
        raise CommandRefused(
            "invalid_args", "link_url is required: an https link, or null to clear it"
        )
    value = command.args["link_url"]
    if value is None:
        return None
    if not isinstance(value, str):
        raise CommandRefused(
            "invalid_args", "link_url is an https link, or null to clear it"
        )
    link = value.strip()
    if len(link) > vocabulary.LINK_URL_MAX:
        raise CommandRefused(
            "invalid_args",
            f"link_url is longer than {vocabulary.LINK_URL_MAX} characters",
        )
    if any(ch.isspace() or not ch.isprintable() for ch in link):
        raise CommandRefused(
            "invalid_args", "link_url has a space or a control character in it"
        )
    try:
        parts = urlsplit(link)
    except ValueError:
        raise CommandRefused("invalid_args", "link_url is not an https link") from None
    if parts.scheme != "https" or not parts.hostname:
        raise CommandRefused("invalid_args", "link_url is not an https link")
    if "@" in parts.netloc:
        # A user name or a password would sit on the card in the clear.
        raise CommandRefused(
            "invalid_args", "link_url has a user name or password in it"
        )
    return link


async def set_item_link(session, command: Command) -> CommandResult:
    """The link a story of this item asks a person to add by hand.
    A story published through the API cannot carry a
    link sticker, so the card names the link instead. The link is the
    item's: every story of it, planned or from the cadence, asks for the
    same one. `null` clears it.

    One UPDATE of the workspace's own item, with the tenant's policy as the
    second fence, then an audit row naming the link set or cleared. A link
    is not a state, so the row carries the item's own state on both sides."""
    media_id = _id_arg(command, "media_item_id")
    link = _link_url(command)
    result = await session.execute(
        text(
            "UPDATE media_items SET link_url = :link"
            " WHERE id = :item AND workspace_id = :ws"
            " RETURNING state"
        ),
        {"link": link, "item": media_id, "ws": command.workspace_id},
    )
    row = result.mappings().first()
    if row is None:
        raise CommandRefused("not_found", f"item {media_id}", facts={"missing": "item"})
    await _audit_same_state(
        session,
        command,
        entity_kind="media_item",
        entity_id_sql="CAST(:item AS uuid)",
        state=row["state"],
        detail={
            "v": 1,
            "event": "link_set" if link else "link_cleared",
            "link_url": link,
        },
        item=media_id,
    )
    return CommandResult("executed", {"media_item_id": media_id, "link_url": link})


async def set_item_auto_approve(session, command: Command) -> CommandResult:
    """The item's Auto-approve flag: a member's standing Approve.
    While it is on, a story of the item is approved when it is served, unless
    `auto_approval.decide` finds the story not eligible (a pending cancel, API
    posting off, no working login outside dry run, a link to add). It applies
    from the next serve and decides no card already waiting.

    One UPDATE of the workspace's own item, with the tenant's policy as the
    second fence. It skips a removed item, so a refusal writes nothing: when
    no row comes back, one read names the refusal, `not_found` for an item
    that is not the workspace's and `locked` for the removed item the UPDATE
    skipped. Otherwise one audit row names the flag set or cleared, since
    `media_items` has no governance trigger. A flag is not a state, so the
    row carries the item's own state on both sides."""
    media_id = _id_arg(command, "media_item_id")
    on = command.args.get("auto_approve")
    if not isinstance(on, bool):
        raise CommandRefused("invalid_args", "auto_approve is true or false")
    result = await session.execute(
        text(
            "UPDATE media_items SET auto_approve = :on"
            " WHERE id = :m AND workspace_id = :ws"
            "   AND state <> 'removed'"
            " RETURNING state"
        ),
        {"on": on, "m": media_id, "ws": command.workspace_id},
    )
    row = result.mappings().first()
    if row is None:
        current = await readers.row(
            session,
            "SELECT state FROM media_items WHERE id = :m AND workspace_id = :ws",
            m=media_id,
            ws=command.workspace_id,
        )
        if current is None:
            raise CommandRefused(
                "not_found", f"item {media_id}", facts={"missing": "item"}
            )
        # Found, so the UPDATE skipped it as removed; its state is not checked
        # again (a revive committed since is the one race that could show it
        # live, and it is answered as if the toggle came first). Out of the
        # library until its folder is picked again, and its card offers no
        # Configure. The closed reasons hold no `item_removed`, so it is
        # refused as `schedule_item` refuses the same item: `locked`, with the
        # item's own state in the way.
        raise CommandRefused(
            "locked",
            f"item {media_id}: item_removed",
            facts={"in_the_way": ["item_removed"], "overridable": False},
        )
    await _audit_same_state(
        session,
        command,
        entity_kind="media_item",
        entity_id_sql="CAST(:item AS uuid)",
        state=row["state"],
        detail={"v": 1, "event": "auto_approve", "on": on},
        item=media_id,
    )
    return CommandResult("executed", {"media_item_id": media_id, "auto_approve": on})


async def set_source_approval(session, command: Command) -> CommandResult:
    """A folder's own approval position: `manual` (ask every
    time), `returning`, `auto` (YOLO), or None for the workspace's. The serve
    step reads the folder's position over the workspace's
    (`auto_approval.decide`), from the next serve on; it decides no card
    already waiting.

    The value is checked here, by name, and `ck_source_approval_mode` (approval rules)
    bounds it again. One UPDATE of the workspace's own CONNECTED source, with
    the tenant's policy as the second fence: a removed folder's position went
    with its removal (`provisioning.pause_media_source`), and a stale page
    must not set it again, so it is `not_found`. Governance audit covers
    `workspaces`, not `media_sources`, so one audit row names the change,
    from and to; a position is not a state, so the row carries the source's
    own state on both sides. The key is required: a missing one would clear
    a position by accident."""
    source_id = _id_arg(command, "source_id")
    if "approval_mode" not in command.args:
        raise CommandRefused(
            "invalid_args", "approval_mode is required (null for the workspace's)"
        )
    mode = command.args["approval_mode"]
    if mode is not None and mode not in vocabulary.APPROVAL_POSITIONS:
        raise CommandRefused(
            "invalid_args",
            f"approval_mode is one of {', '.join(vocabulary.APPROVAL_POSITIONS)},"
            " or null for the workspace's",
        )
    result = await session.execute(
        text(
            # The row, locked, as it stands before this write: the audit's
            # from. A plain self-join would read it stale when two writes to
            # one folder overlap (READ COMMITTED re-reads only the target).
            "WITH o AS ("
            "  SELECT s.id, s.approval_mode FROM media_sources s"
            "   WHERE s.id = :s AND s.workspace_id = :ws"
            f"    AND {workspaces.CONNECTED_SQL}"
            "     FOR UPDATE"
            ")"
            " UPDATE media_sources s SET approval_mode = :m"
            "  FROM o WHERE s.id = o.id"
            " RETURNING s.state, o.approval_mode AS was"
        ),
        {"m": mode, "s": source_id, "ws": command.workspace_id},
    )
    row = result.mappings().first()
    if row is None:
        raise CommandRefused(
            "not_found", f"source {source_id}", facts={"missing": "source"}
        )
    await _audit_same_state(
        session,
        command,
        entity_kind="media_source",
        entity_id_sql="CAST(:source AS uuid)",
        state=row["state"],
        detail={"v": 1, "event": "approval_mode", "from": row["was"], "to": mode},
        source=source_id,
    )
    return CommandResult("executed", {"source_id": source_id, "approval_mode": mode})


async def sync_now(session, command: Command, *, capability=None) -> CommandResult:
    # The command port passes no capability: the session carries what the
    # composition root injected (`principal`, `work_loop`), None by default.
    if capability is None:
        capability = session.info.get("past_posts_capability")
    source_id = _arg(command, "source_id")
    source = (
        await session.execute(
            text(
                "SELECT id, state, provider FROM media_sources WHERE id = :s AND workspace_id = :ws"
            ),
            {"s": source_id, "ws": command.workspace_id},
        )
    ).first()
    if source is None:
        raise CommandRefused("not_found", f"media source {source_id}")
    payload = {"v": 1, "source_id": source_id, "reason": "demand"}
    if source[2] == "instagram_past_posts":
        from src.services.target import source_read_admission

        try:
            capture = await source_read_admission.capture_source_read(
                session,
                workspace_id=command.workspace_id,
                source_id=source_id,
                capability=capability,
            )
        except source_read_admission.SourceReadRefused as exc:
            raise CommandRefused(
                "illegal_transition",
                "this source cannot be read",
                facts={"source_id": source_id, "source_read_reason": exc.reason},
            ) from None
        payload["source_read_capture"] = capture.payload()
    job_id = await jobs.enqueue(
        session,
        kind="sync_media_source",
        workspace_id=command.workspace_id,
        serialization_key=f"src:{source_id}",
        payload=payload,
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


#: The purpose each Drive link command mints: `CONNECT_PURPOSE_KIND` read
#: backwards, so the chat door and the web legs follow one table.
_LINK_PURPOSE = {kind: purpose for purpose, kind in CONNECT_PURPOSE_KIND.items()}


async def _begin_drive_link(session, command: Command) -> CommandResult:
    """Shared body of `connect_account` / `reconnect_account` — the chat-side
    start of the WORKSPACE's Drive grant (workspace Drive grant: one Google grant per
    workspace, folders picked under it).

    THIN BY DESIGN. The OAuth leg is the API route's; this door only
    initiates and records. It returns the state, not a URL, because composing
    the URL needs `(client_id, redirect_uri)`, which `src/api/google_client.py`
    owns — the adapter renders, as it already does for sign-in. The result
    carries the state's PKCE challenge for that URL.

    The connect/reconnect split is the SCHEMA's answer, not the caller's:
    `connect_purpose` reports which one this workspace is in, and a command
    that disagrees is refused by name rather than quietly doing the other. A
    reconnect that ran as a connect would skip `issue_state`'s
    invalidate-prior-states step ("last issued wins") and leave two
    live callbacks for one workspace.
    """
    expect = _LINK_PURPOSE[command.kind]
    purpose = await google_drive_oauth.connect_purpose(
        session, workspace_id=command.workspace_id
    )
    if purpose != expect:
        raise CommandRefused(
            "illegal_transition",
            f"this workspace's Drive needs {purpose}, not {expect}",
        )
    state, challenge = await google_drive_oauth.issue_connect_state(
        session,
        purpose=purpose,
        user_id=command.actor_user_id,
        workspace_id=command.workspace_id,
    )
    return CommandResult(
        "executed",
        {
            "provider": google_drive_oauth.PROVIDER,
            "purpose": purpose,
            "state": state,
            "code_challenge": challenge,
        },
    )


async def connect_account(session, command: Command) -> CommandResult:
    """Begin the workspace's Drive connect — it holds no grant yet."""
    return await _begin_drive_link(session, command)


async def reconnect_account(session, command: Command) -> CommandResult:
    """Begin the workspace's Drive reconnect — it holds a grant already."""
    return await _begin_drive_link(session, command)


async def remove_member(session, command: Command) -> CommandResult:
    """An admin removes membership explicitly. The revoke for every
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
    """The active ↔ disabled edge (a user command, audited), the disabling
    half — what the web calls Remove.

    The destination leaves the clock's scan (`fn_clock_tick` reads
    `state = 'active'` only), its Instagram credential is revoked locally,
    and its live intents are flagged `cancel_requested` — as `cancel` flags
    one; the user never writes a terminal state — for the worker
    to finish, with the Queue offering a flagged card no action meanwhile.
    The row stays: the history hangs off it, and connecting the
    same account again is what brings it back (`connect_destination` adopts
    a `disabled` row and `attach_connected_identity` flips it `active`).
    The audit row is the governance trigger's, under this unit of work's
    actor. No provider call — the same scope statement as `disconnect_account`.

    The account's past-posts source goes first: the canonical source
    prefix, then the past-posts source it originates retired through the epoch writer
    (`past_posts_lifecycle.remove_account_past_posts`), and only then the account,
    credential and intent rows below. Every other source is untouched, and the
    source stays the account's to restore on reconnect.
    """
    account_id = _arg(command, "ig_account_id")
    await past_posts_lifecycle.remove_account_past_posts(
        session, workspace_id=command.workspace_id, ig_account_id=account_id
    )
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
    # Its live cards lose their decision buttons, keeping ⚙️ Configure
    # the flagged intents are the ones the worker will
    # terminalize.
    live = await readers.rows(
        session,
        "SELECT i.id, i.state, COALESCE(a.tz, w.tz) AS eff_tz FROM post_intents i"
        "  JOIN workspaces w ON w.id = i.workspace_id"
        "  JOIN ig_accounts a ON a.id = i.ig_account_id"
        " WHERE i.workspace_id = :ws AND i.ig_account_id = :acct"
        "   AND i.cancel_requested"
        "   AND i.state <> ALL(CAST(:terminal AS text[]))",
        ws=command.workspace_id,
        acct=account_id,
        terminal=list(TERMINAL_STATES),
    )
    for row in live:
        line = prompts.outcome_line(
            "account_disabled", by=None, at=utcnow(), tz=_tz(row)
        )
        if row["state"] == "review_required":
            # A parked row has no worker checkpoint to finish it at: the
            # removal is its give-up, and its card loses the review buttons
            # (by ref — the approve tap superseded it long ago).
            intent_id = str(row["id"])
            if not await publish_cap.resolve_cancel(session, intent_id=intent_id):
                # A member resolved it between the flag and this read: their
                # line stands, and the op was ended by their verdict.
                continue
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
    """For the WORKSPACE's Drive grant (workspace Drive grant): revoke the one
    credential, KEEP the row, and pause every folder under it.

    `paused`, deliberately not `error`. A disconnect is a decision, not a
    fault, and reserving `error` for faults is what keeps the disconnect
    alert meaningful — if a user disconnecting produced `error`, that beat
    would re-alert them every day about something they chose, and someone
    would eventually silence it, taking the real faults with it. `paused` is
    outside the beat's scan by construction.

    The rows are kept rather than deleted: the media and its history hang off
    the sources, and the credential row is an audit trail nobody misses until
    they need it.

    **The best-effort Google revoke is NOT performed here, and that is a
    scope statement rather than an oversight.** A provider call inside a unit
    of work violates this codebase's checkpoint discipline, and the remote
    revoke is best-effort precisely because it may fail and must not
    block the local state change. What must be atomic is the pair below; the
    remote call rides a job outside this transaction.
    """
    revoked = (
        await session.execute(
            text(
                "UPDATE oauth_credentials SET state = 'revoked'"
                " WHERE "
                + google_drive_oauth.WORKSPACE_GRANT_WHERE
                + "   AND state <> 'revoked'"
                " RETURNING id"
            ),
            {"ws": command.workspace_id, "provider": google_drive_oauth.PROVIDER},
        )
    ).first()
    paused = await session.execute(
        text(
            "UPDATE media_sources SET state = 'paused', alerted_at = NULL"
            " WHERE workspace_id = :ws AND provider = :provider"
            "   AND state <> 'paused'"
        ),
        {"ws": command.workspace_id, "provider": vocabulary.PROVIDER_GDRIVE},
    )
    # The remote half, enqueued rather than called. Everything above
    # is the atomic pair; this rides a separate transaction so a Google that
    # is slow, angry or absent cannot roll back a disconnect the user has
    # already been told succeeded — which is what "best-effort"
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
    """One account's schedule overrides.

    `accounts_sources_media.py` gives every account its own `posts_per_day`,
    `posting_hours_start/end` and `tz`, the account is the
    unit of scheduling, and `fn_clock_tick` resolves the ladder per
    row on every tick. Those four columns are written
    here; provisioning uses a supplied schedule ONLY to compute the opening
    `next_slot_at` — never to store it.

    **A separate kind rather than a scope on `settings_change`** — the gate
    reads `ROLE_FLOOR[command.kind]` with no scope parameter, so the scoped
    form would have to move authorization into this function, against the
    one central authorization gate — one place, not per handler.

    **Nothing here recomputes `next_slot_at`.** The cursor advances through
    `fn_next_slot(a.next_slot_at, eff_tz, eff_start, eff_end, eff_ppd)` with
    the effective values re-read each tick, so a change lands on the NEXT
    advance by itself. The slot already on the row was computed under the old
    settings and still fires at its old time; the new cadence governs from the
    one after. Recomputing here would be a second scheduling authority beside
    the clock, and scheduling is the clock's alone.
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
    """Invite someone to this workspace by email. Returns the invitation and,
    once, its link; the email itself goes after the commit.

    **The accept half is the reference** — `fn_invitation_accept` and
    `/join/[token]` — so this is built to the acceptor's
    contract: the token is hashed with `sessions.token_hash` because that door
    resolves by `token_hash` alone, and `invitations.create` writes every column
    its identity check reads.

    **The token is returned, once,** with `join_url`. It is the credential —
    possession accepts — and only its hash is stored, so this answer and the
    email are the only places it exists in full. The email rides
    `after_commit` (`invitations.InvitationEmail` says why it is not a job);
    the web adapter runs it once this transaction has committed.

    **Per person, by email.** `delivery_channel` must be `email`: a Telegram or
    hint-only invitation names no address, and the web's accept takes it with
    no identity proof (`invitations.accept` passes Google, no Telegram id) — an
    open link by another name. That arm has no caller, though
    `invitations.create` accepts the channel.

    `role` defaults to `member` and is a CEILING, never a grant: the acceptor
    downgrades an unmatched admin invite to `member` plus an elevation-pending
    notification, which is why this command's floor is `admin`.
    """
    role = command.args.get("role", "member")
    if not isinstance(role, str):
        raise CommandRefused("invalid_args", "role must be a string")
    if command.args.get("delivery_channel", "email") != "email":
        raise CommandRefused("invalid_args", "invitations are sent by email")
    # NOT `_arg`: `invitations.create` refuses a missing or malformed address
    # by name (`email_required`, `email_invalid`), and owns that rule.
    email = command.args.get("email")
    if email is not None and not isinstance(email, str):
        raise CommandRefused("invalid_args", "email must be a string")
    try:
        invitation_id, token = await invitations.create(
            session,
            workspace_id=command.workspace_id,
            invited_by_user_id=command.actor_user_id,
            role=role,
            delivery_channel="email",
            email=email,
        )
    except invitations.InvitationRefused as exc:
        # The port's closed vocabulary, not this module's: every one of these
        # is the caller's input being wrong; the detail carries which.
        raise CommandRefused("invalid_args", str(exc)) from exc

    expires_at, delivery, email_after_commit = await invitations.prepare_email(
        session,
        workspace_id=command.workspace_id,
        invitation_id=invitation_id,
        token=token,
        web_app_origin=settings.web_app_origin,
    )
    return CommandResult(
        "executed",
        {
            "invitation_id": invitation_id,
            "invite_token": token,
            "join_url": invitations.join_url(settings.web_app_origin, token),
            "expires_at": expires_at.isoformat(),
            "role": role,
            "delivery": delivery,
        },
        after_commit=email_after_commit,
    )


async def revoke_invitation(session, command: Command) -> CommandResult:
    """An admin withdraws a pending invitation before anyone accepts it — the
    one way to stop a link sent to the wrong address, which a re-invite cannot
    do (it mints a new link for the same address) and which an emailed link
    makes urgent: the address's owner holds a working credential.

    `not_found` answers every invitation that is no longer pending, so a
    second Revoke of the same row says what an unknown id does: the web mints
    a fresh idempotency key per click, and the compare-and-set answers it."""
    invitation_id = _id_arg(command, "invitation_id")
    if not await invitations.revoke(
        session, workspace_id=command.workspace_id, invitation_id=invitation_id
    ):
        raise CommandRefused("not_found", f"pending invitation {invitation_id}")
    return CommandResult("executed", {"invitation_id": invitation_id})


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
        protocol_version=command.args.get("protocol_version"),
        conversion_evidence=getattr(session, "info", {}).get("mix_conversion_evidence"),
    )
    return CommandResult("executed", {"workspace_id": ws_id})


async def restore_workspace(session, command: Command) -> CommandResult:
    """The way back: `offboarding → active`, **within the grace window
    only**, owner-only (`ROLE_FLOOR`).

    Two writes, and the second one is the whole point.

    **THE CLOCK NEVER CONSULTS CREDENTIAL STATE.** `fn_clock_tick`'s due-scan
    (`04_functions.sql`) selects `WHERE a.state = 'active' AND... AND w.state = 'active'`,
    and `ix_ig_accounts_due` is partial on the same account predicate. Nothing
    in that path looks at `oauth_credentials`. Offboarding, for its part, never
    touches `ig_accounts.state` at all — leg 2 revokes credentials and stops.

    So flipping the workspace back is NOT sufficient, and the failure it leaves
    is quiet: accounts are still `active`, their credentials are `revoked`, and
    the clock resumes planning slots that every publish then fails. Posting
    resumes as reconnects land, a PRECONDITION — posting must not
    resume before them — and `ig_accounts.state = 'reauth_required'` is the only
    mechanism in the schema that expresses it, because it is the one the
    dispatcher predicate actually reads.

    A derived `credential_status` badge renders `revoked` as
    reconnect-needed and is genuinely useful, but it is a DISPLAY fact. **A badge
    does not stop the clock.**

    **Scoped to accounts holding a revoked credential, deliberately.** A manual
    destination has no `oauth_credentials` row by construction (`provisioning`:
    "a destination needs no credential, no OAuth round trip and no Meta call to
    exist"), so there is nothing for its owner to reconnect. A blanket freeze
    would strand exactly the destinations that were never broken.

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
    """The entry edge, owner-only (`ROLE_FLOOR`). Two writes and a job.

    The flip and the job are one transaction on purpose: a workspace left
    `offboarding` with nothing scheduled to finish the job would sit invisible
    to the clock forever, which is worse than not having started.

    **`confirm` is required, and it is the port's half of an explicit,
    confirmed owner request.** The dialog is the front end's; what the port can
    enforce is that the destructive intent was stated rather than arrived at.
    This is the one command in the vocabulary whose effect is irreversible
    after the grace window, and a `POST` with an empty body should not start
    it.

    **A second offboard is refused rather than absorbed.** The state table
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


async def _admit_media(session, command, intent, *, require_validation):
    from src.services.target import source_policy

    verdict = await source_policy.intent_suitability(
        session,
        workspace_id=command.workspace_id,
        intent_id=str(intent["id"]),
        require_validation=require_validation,
        context=intent["_suitability_context"],
    )
    override = command.args.get("override_locks", False)
    if type(override) is not bool:
        raise CommandRefused("invalid_args", "override_locks is true or false")
    allowed = verdict and (
        verdict["suitable"]
        or (
            not require_validation
            and override
            and verdict["base_suitable"]
            and not verdict["live_intent_conflict"]
            and not verdict["blocking_locks"]
        )
    )
    if not allowed:
        raise CommandRefused(
            "media_unsuitable",
            "media admission refused",
            facts={"source_reason": verdict["reason"] if verdict else "not_found"},
        )
