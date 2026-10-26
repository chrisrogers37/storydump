"""The closed vocabularies and wire shapes every adapter of the target tier shares.

Dependency-free on purpose: the CLI package imports this module and nothing
else from ``src`` (its import-boundary test pins that), so nothing here may
import a database driver, a framework, or another service module. The command
port re-exports :data:`COMMANDS` and :data:`REASONS` from here so the two
tiers agree by construction; the CHECK lists copied from the migrations are
pinned against the migration files by a unit test.

Three kinds of thing live here:

* closed sets — command kinds, refusal reasons, intent states and steps, the
  audit actor kinds and channels, token roles, principal kinds, the token
  refusals the API answers with a ``reason``;
* the CLI's wire contract — exit codes and the JSON envelope, with the
  checker the CLI's tests run over every emitted document;
* the CLI's own sentences — one per reason and outcome, in the CLI's words
  (never the Telegram adapter's tap words: no "tap", "button", "card").
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Mapping, Optional

#: The closed inbound vocabulary. Set-equality with the listed commands
#: is asserted by the unit gate.
COMMANDS: tuple[str, ...] = (
    "approve",
    "skip",
    "reject",
    "mark_posted",
    "cancel",
    "schedule_item",
    "reschedule_item",
    "set_item_link",
    "set_item_auto_approve",
    "autopost_now",
    "sync_now",
    "settings_change",
    "account_settings_change",
    "set_source_approval",
    "pause_workspace",
    "resume_workspace",
    "connect_account",
    "reconnect_account",
    "disconnect_account",
    "move_account",
    "disable_account",
    "create_workspace",
    "rename_workspace",
    "offboard_workspace",
    "restore_workspace",
    "invite_member",
    "revoke_invitation",
    "remove_member",
    "change_role",
    "transfer_ownership",
    "resolve_review",
    "clear_quarantine",
)

#: `CommandRefused.reason`, closed. Adapters map it without parsing prose, and
#: the web adapter's status table is pinned TOTAL over this tuple.
REASONS: tuple[str, ...] = (
    "unknown_command",
    "not_built",
    "workspace_required",
    "invalid_args",
    "illegal_transition",
    "not_found",
    "manual_mode",
    "cancelling",
    "not_connected",
    "nothing_to_confirm",
    "may_have_posted",
    "locked",
    "media_unsuitable",
)

#: `post_intents.state` (`intent_ledger.py` ``ck_intent_state``), in the CHECK's order.
INTENT_STATES: tuple[str, ...] = (
    "scheduled",
    "prompt_pending",
    "awaiting_approval",
    "approved",
    "publishing",
    "publishing_ambiguous",
    "review_required",
    "posted",
    "skipped",
    "rejected",
    "expired",
    "failed",
    "cancelled",
)

#: The states a story ends in, which no edge leaves (`trg_intent_guard`'s
#: terminal set). One spelling: `intent_ledger.TERMINAL_STATES` names it for
#: the services, and the CLI reads it here.
TERMINAL_STATES: tuple[str, ...] = (
    "posted",
    "skipped",
    "rejected",
    "expired",
    "failed",
    "cancelled",
)

#: The states a story waits in before it is approved: before its card is
#: served (``UNSERVED_STATES``, which the reaper's cadence leg expires a story
#: from once its slot is past), and while its card waits for a decision.
UNSERVED_STATES: tuple[str, ...] = ("scheduled", "prompt_pending")
WAITING_STATES: tuple[str, ...] = (*UNSERVED_STATES, "awaiting_approval")

#: `post_intents.origin` (`post_intents.origin` ``ck_intent_origin``): a ``cadence`` story is
#: minted by the clock for a slot, a ``planned`` one by a person who chose the
#: item, the account and the time (`schedule_item`).
INTENT_ORIGINS: tuple[str, ...] = ("cadence", "planned")

#: An approval position (``ck_ws_approval_mode`` and ``ck_source_approval_mode``):
#: ``manual`` asks every time, ``returning`` approves an item that has posted
#: through the API before, and ``auto`` (YOLO) approves every story. A folder's
#: own position is one of these, or NULL for the workspace's.
APPROVAL_POSITIONS: tuple[str, ...] = ("manual", "returning", "auto")

#: The rule that approved a story (``ck_intent_approval_rule``); NULL when a
#: person did. ``auto`` is YOLO, ``item`` the item's flag, ``returning`` an item
#: posted through the API before.
APPROVAL_RULES: tuple[str, ...] = ("auto", "item", "returning")

#: How each rule is named where a person's name would stand ("Approved by YOLO"):
#: the one spelling for the card, the Queue and the CLI.
RULE_LABELS: Mapping[str, str] = {
    "auto": "YOLO",
    "item": "Auto-approve",
    "returning": "Returning items",
}


def rule_label(rule: str) -> str:
    """A rule's label; an unknown rule shows as itself, never as nothing."""
    return RULE_LABELS.get(rule, rule)


#: A planned story's repeat units (``ck_intent_repeat_unit``): every N
#: days, or every N weeks on chosen weekdays.
REPEAT_UNITS: tuple[str, ...] = ("day", "week")

#: The weekdays a weekly repeat names, in ISO order: a name's index plus one is
#: its ISO weekday, as ``repeat_days`` stores it (1 is Monday).
WEEKDAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def describe_repeat(rule: Mapping[str, Any]) -> str:
    """A repeat rule in words, the CLI's and the card's alike: "every day at
    09:00", "every 3 days at 09:00", "every week on Mon and Thu at 16:07",
    "every 2 weeks on Tue at 16:07". *rule* is the rule's one shape,
    ``{unit, every, days, at}`` with ``at`` as ``HH:MM``
    (`intent_ledger.repeat_sql`). It is one the CHECKs admitted (repeat rules), so an
    unknown unit is a bug, raised."""
    unit, every, at = rule["unit"], rule["every"], rule["at"]
    if unit == "day":
        period = "every day" if every == 1 else f"every {every} days"
        return f"{period} at {at}"
    if unit == "week":
        period = "every week" if every == 1 else f"every {every} weeks"
        names = [WEEKDAYS[d - 1].capitalize() for d in sorted(set(rule["days"] or ()))]
        on = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
        return f"{period} on {on} at {at}"
    raise ValueError(f"unknown repeat unit {unit!r}")


#: The `ig_accounts.state`s a story is served on (`accounts_sources_media.py` ``ck_ig_accounts_state`` less
#: `disabled` and `moved`): a planned story is scheduled only on one of
#: these, and served only while its account is still in one.
LIVE_ACCOUNT_STATES: tuple[str, ...] = ("active", "reauth_required")

#: A listed destination's readiness on the mix resource,
#: each with the badge tone it renders in, in
#: precedence order: `destination_readiness.readiness` takes the
#: first state whose rule matches. The web renders `state`, `tone` and `remedy`
#: as the API answers them and derives none of its own, so these are its
#: spellings.
READINESS_TONES: Mapping[str, str] = {
    "inactive": "inert",
    "needs_connection": "attention",
    "needs_reconnect": "attention",
    "sources_off": "inert",
    "no_assignable_source": "attention",
    "ready": "active",
    "stalled": "problem",
    "error": "problem",
    "indexing": "progress",
    "zero_eligible": "attention",
}
#: What can be done for a destination that is not ready. A state with nothing
#: to do answers `remedy: null`.
READINESS_REMEDIES: tuple[str, ...] = (
    "connect",
    "reconnect",
    "enable_history",
    "add_source",
    "review_policy",
    "retry",
)
#: The order the workspace's guidance lists its incomplete destinations in; the
#: listed account order breaks ties. `inactive` and `sources_off` are never
#: incomplete: both are deliberate.
READINESS_INCOMPLETE_ORDER: tuple[str, ...] = (
    "needs_connection",
    "needs_reconnect",
    "no_assignable_source",
    "stalled",
    "error",
    "indexing",
    "zero_eligible",
)
#: A workspace whose setup is idle by choice rather than unfinished.
READINESS_INTENTIONAL: tuple[str, ...] = ("all_inactive", "all_sources_off")
#: The `fn_source_asset_suitability` reasons a source's own policy decides
#: (`fn_source_asset_suitability`): its minimum age, and the opt-ins for credited and for
#: unknown-attribution posts.
POLICY_EXCLUSIONS: tuple[str, ...] = (
    "too_recent",
    "credited_excluded",
    "attribution_unknown",
    "slide_not_first",
)

#: How far ahead a story may be planned.
PLAN_HORIZON_DAYS = 365

#: The longest link an item may carry (`set_item_link`), in
#: characters: an address that long is no longer one a person adds by hand.
LINK_URL_MAX = 2048

#: `post_intents.last_error.class` on a planned story that ended unserved
#: at its time (`prompts.sweep_planned_misses`); its `message` is the reason.
PLANNED_MISSED = "planned_missed"

#: What a scheduled story's answer may warn of, without refusing it.
NO_PUSH_BINDING = "no_push_binding"

#: What an invitation's email came to, as the web reads `delivery.state`
#: (the email goes after the invitation commits), and whose
#: day refused one when it is `over_budget` (`delivery.reason`).
INVITATION_DELIVERY_STATES: tuple[str, ...] = (
    "sent",
    "not_configured",
    "over_budget",
    "failed",
)
EMAIL_BUDGET_REASONS: tuple[str, ...] = ("person_limit", "daily_limit")

#: The longest email address a row may hold, the bound of the waitlist's
#: CHECK (`ck_waitlist_email`) and of an invitation's address.
MAX_EMAIL_LENGTH = 254

#: What admitting an address came to (`signup_admissions.admit`), which
#: the CLI prints: admitted, with its email queued; an email queued again to
#: an address admitted before (the owner's send-again), whether this call
#: queued it or one was already waiting; already admitted, nothing queued;
#: the spot already used: an account was created at or holds the address, or
#: its claim link was used; an address the admissions table cannot hold; no
#: web origin for the email's claim link. Only the first two leave an email
#: queued, and only `admitted` writes an admission.
ADMIT_OUTCOMES: tuple[str, ...] = (
    "admitted",
    "resent",
    "admitted_before",
    "signed_up",
    "invalid_email",
    "not_configured",
)

#: The hex characters of an address's SHA-256 that stand for it where the
#: address itself must not travel: `fn_waitlist`'s `p_ref` (signup admission functions), and
#: the waitlist ping's Admit button. One spelling for `signup_admissions.ref`,
#: which cuts it, and `callback_tokens`, which checks it.
WAITLIST_REF_HEX = 32

#: `post_locks.kind` (`accounts_sources_media.py` ``ck_locks_kind``), split by the lock and item
#: rule. A blocking kind refuses a planned story, and at its
#: time the serve door turns it into a miss; a warning kind refuses a
#: schedule only until the person overrides it, and never causes a miss.
BLOCKING_LOCKS: tuple[str, ...] = ("reject", "unsupported", "hold", "seasonal")
WARNING_LOCKS: tuple[str, ...] = ("skip", "recent")

#: `media_items.state` (``ck_media_state``). Only `available` is drawn.
#: `unsupported` is the sync's judgment that the publish could never fetch the
#: file (past its byte cap); `removed` is the folder's retirement, which a
#: re-pick undoes; `missing` is the file's own absence from its folder, which
#: only a listing undoes.
MEDIA_STATES: tuple[str, ...] = ("available", "unsupported", "removed", "missing")

#: The image types a thumbnail may be: raster only. The API serves no other and
#: the web tier relays no other (`landing/src/lib/thumbnails.ts`, held to this
#: by `wire-contract.test.ts`). An SVG is a document that can carry script, so
#: it is never a thumbnail, whatever a provider calls it.
THUMBNAIL_TYPES: tuple[str, ...] = (
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "image/avif",
)

#: What an item of an Instagram past-posts source is called where a file name
#: would be: stored (`instagram_catalog._project`, so the Telegram card and the
#: missed-story notice say it) and on every read that carries it
#: (`workspaces._past_posts_row`). A post is labelled by where it came from, never
#: by Instagram's id. The web says the same words
#: (`landing/src/lib/past-posts-media.ts`, held to this by `wire-contract.test.ts`).
PAST_POSTS_ITEM_NAME = "Instagram post"
PAST_POSTS_SLIDE_NAME = "Instagram post, slide {position} of {count}"
PAST_POSTS_SLIDE_NAME_UNKNOWN = "Instagram post, slide {position}"

#: What a story may weigh on the way to Meta, by `media_items.media_kind`
#: (Meta's own limits: 8 MB for a story image, 100 MB for a story video pulled
#: by URL). Distinct from the Telegram card's caps (`MEDIA_CARD_MAX_BYTES`): a
#: file too large for a Telegram preview may still be a fine story.
#: The video cap is Cloudinary's, not Meta's: a story video is framed on the
#: fly at its delivery URL (`transit.story_transformation`), and Cloudinary
#: transforms a video synchronously only up to 40 MB on the free plan (100 MB
#: on paid); above that the URL answers 400 and Meta's fetch can never
#: succeed. A file over the cap is refused by name at the fetch rung
#: (`DriveMediaTooLarge` → failed + refund, the reason on the intent) rather
#: than burning five attempts. Raising it would need eager, asynchronous
#: framing at upload. The sync judges a listed file against the same
#: numbers and lands one past them `unsupported`, so the draw never takes a
#: file the publish could never fetch (`media_sync._listed_state`).
PUBLISH_MAX_BYTES: Mapping[str, int] = {
    "image": 8 * 1024 * 1024,
    "video": 40 * 1000 * 1000,
}

#: `post_intents.publish_step` (`intent_ledger.py` ``ck_intent_step``).
PUBLISH_STEPS: tuple[str, ...] = (
    "none",
    "transit_uploaded",
    "container_created",
    "container_ready",
    "publish_called",
    "effect_confirmed",
)

#: `audit_events.actor_kind` (`intent_ledger.py` ``ck_audit_actor``).
AUDIT_ACTOR_KINDS: tuple[str, ...] = (
    "user",
    "system",
    "clock",
    "reaper",
    "reconciler",
    "operator",
    "migration",
)

#: `audit_events.channel` (`intent_ledger.py` ``ck_audit_channel``); the admission channels
#: are the first three (``webhook_ingress.CHANNELS``).
AUDIT_CHANNELS: tuple[str, ...] = ("telegram", "web", "cli", "system")

#: `service_tokens.role` (`auth_plane.py` ``ck_service_token_role``).
TOKEN_ROLES: tuple[str, ...] = ("operator", "readonly")

#: `channel_outbox.last_failure_class` (``ck_outbox_failure_class``): why a
#: row's last send failed. `rate_limited` is a 429 (a deferral, never counted as
#: a failure); `destination_gone`, `refused` and `credential_dead` (a dead
#: token's 401) are the definitive answers, which fail the row; `ambiguous` is a
#: send whose outcome is unknown (no answer, a 5xx, a reply the transport does
#: not classify, a dead predecessor's stranded row).
OUTBOX_FAILURE_CLASSES: tuple[str, ...] = (
    "rate_limited",
    "destination_gone",
    "refused",
    "credential_dead",
    "ambiguous",
)

#: The bounds every adapter enforces before the table does: a token's name
#: (`service_tokens.name`), and its expiry in whole days — the API's default
#: and ceiling, the web form's range, the CLI's `tokens` verbs' words.
TOKEN_NAME_MAX = 80
TOKEN_EXPIRY_DAYS_MIN = 1
TOKEN_EXPIRY_DAYS_DEFAULT = 90
TOKEN_EXPIRY_DAYS_MAX = 365

#: The range of a workspace's TTL settings (`workspaces.SETTINGS_RANGES`): at
#: least one of the setting's unit, at most a year.
SETTINGS_TTL_MIN = 1
SETTINGS_TTL_DAYS_MAX = 365
SETTINGS_APPROVAL_TTL_MINUTES_MAX = SETTINGS_TTL_DAYS_MAX * 24 * 60

#: Every API token starts with this; the resolver routes on it and secret
#: scanners recognise it. The rest is 32 url-safe random bytes (43 chars).
TOKEN_PREFIX = "sdt_"

#: The refusals a token principal gets WITH a ``reason`` (403): the route is
#: session-only, the token cannot write, it belongs to another workspace, or
#: its person is not one of the deployment's operators (`OPS_USER_IDS`).
TOKEN_REFUSALS: tuple[str, ...] = (
    "session_required",
    "readonly_token",
    "wrong_workspace",
    "not_ops",
)

#: Why a presented token did not resolve (the API answers 401 without saying
#: which; the resolver's reason is for logs and the gate).
TOKEN_RESOLUTION_REASONS: tuple[str, ...] = (
    "invalid_token",
    "expired_token",
    "revoked_token",
    "disabled_user",
)

#: `credentials.provider` / `media_sources.provider` / `oauth_states.provider`
#: — the values `ck_credentials_provider`, `ck_sources_provider` and
#: `ck_oauth_state_provider` admit. Spelled here, the one dependency-free
#: module, so no module keeps a hand copy.
PROVIDER_IG_LOGIN = "ig_login"
PROVIDER_GDRIVE = "gdrive"
#: `identities.provider` — who verified the person.
PROVIDER_GOOGLE = "google"
PROVIDER_TELEGRAM = "telegram"

# --- the CLI's exit codes -------------------------------------------------

EXIT_OK = 0
EXIT_NOT_FOUND = 1
EXIT_REFUSED = 2
EXIT_NOT_AUTHORIZED = 3
EXIT_API_UNREACHABLE = 4
EXIT_RAILWAY_UNREACHABLE = 5
EXIT_WATCH_FAILED = 6
EXIT_USAGE = 64

#: Name → code, the documented contract (``storydump --help`` prints it).
EXIT_CODES: Mapping[str, int] = {
    "ok": EXIT_OK,
    "not_found": EXIT_NOT_FOUND,
    "refused": EXIT_REFUSED,
    "not_authorized": EXIT_NOT_AUTHORIZED,
    "api_unreachable": EXIT_API_UNREACHABLE,
    "railway_unreachable": EXIT_RAILWAY_UNREACHABLE,
    "watch_failed": EXIT_WATCH_FAILED,
    "usage": EXIT_USAGE,
}


def exit_code_for(status: int, reason: Optional[str] = None) -> int:
    """The exit code for an API answer: *status* is the HTTP status (0 when
    the API was unreachable), *reason* the body's ``reason`` when it has one.

    401 and 403 are "not authorized"; a 404 is "not found" only when the port
    said so (``reason == "not_found"``) — a workspace the principal cannot see
    also reads as 404, and that is an authorization answer; every other 4xx
    is a refusal with a reason; 5xx and no answer at all are "unreachable".
    """
    if 200 <= status < 300:
        return EXIT_OK
    if status in (401, 403):
        return EXIT_NOT_AUTHORIZED
    if status == 404:
        return EXIT_NOT_FOUND if reason == "not_found" else EXIT_NOT_AUTHORIZED
    if status == 429:
        # an edge in front of the API rate-limiting: a transient failure to
        # answer, not a refusal of what was asked (the API's own shedding is
        # a 503 naming `pool_saturated`, already "unreachable" as a 5xx)
        return EXIT_API_UNREACHABLE
    if 400 <= status < 500:
        return EXIT_REFUSED
    return EXIT_API_UNREACHABLE


# --- the JSON envelope ------------------------------------------------------

ENVELOPE_VERSION = 1
ENVELOPE_KEYS = ("v", "kind", "data", "error")
ERROR_KEYS = ("code", "reason", "detail", "fix")


def envelope(kind: str, data: Any) -> dict[str, Any]:
    """A successful document: ``{"v": 1, "kind": kind, "data": data, "error": null}``."""
    return {"v": ENVELOPE_VERSION, "kind": kind, "data": data, "error": None}


def error_envelope(
    kind: str, *, code: int, reason: str, detail: str, fix: str
) -> dict[str, Any]:
    """A failed document: ``data`` is null and ``error`` names the exit code,
    the reason, what happened and what fixes it."""
    return {
        "v": ENVELOPE_VERSION,
        "kind": kind,
        "data": None,
        "error": {"code": code, "reason": reason, "detail": detail, "fix": fix},
    }


def check_envelope(document: Any) -> None:
    """Raise ``ValueError`` unless *document* is a well-formed envelope: the
    four keys and no others, version 1, a non-empty kind, exactly one of
    ``data``/``error`` present, and an error with exactly the four error keys
    whose ``code`` is a documented failure exit code."""
    if not isinstance(document, dict):
        raise ValueError("envelope is not an object")
    if tuple(document.keys()) != ENVELOPE_KEYS:
        raise ValueError(
            f"envelope keys are {list(document.keys())}, want {list(ENVELOPE_KEYS)}"
        )
    if document["v"] != ENVELOPE_VERSION:
        raise ValueError(f"envelope version {document['v']!r}")
    if not isinstance(document["kind"], str) or not document["kind"]:
        raise ValueError("envelope kind is not a non-empty string")
    data, error = document["data"], document["error"]
    if (data is None) == (error is None):
        raise ValueError("exactly one of data/error must be present")
    if error is not None:
        if not isinstance(error, dict) or tuple(error.keys()) != ERROR_KEYS:
            raise ValueError("error keys must be exactly code, reason, detail, fix")
        if error["code"] not in EXIT_CODES.values() or error["code"] == EXIT_OK:
            raise ValueError(f"error code {error['code']!r} is not a failure exit code")
        for key in ("reason", "detail", "fix"):
            if not isinstance(error[key], str):
                raise ValueError(f"error {key} is not a string")
        if error["reason"] not in CLI_REASONS:
            raise ValueError(
                f"error reason {error['reason']!r} is not a documented reason"
            )


# --- the CLI's own sentences -----------------------------------------------

#: The CLI's OWN reasons — the answers no port refusal names: a usage error,
#: a thing not found, an API that did not answer, a watch's failure, a store
#: or Railway that cannot be used, an interrupt, and the bare refusal a
#: reason-less 4xx maps to. Every other reason an envelope carries is the
#: port's (:data:`REASON_SENTENCES`); :data:`CLI_REASONS` below is the closed
#: set an agent may switch on, and :func:`check_envelope` refuses the rest.
CLI_OWN_REASONS: tuple[str, ...] = (
    "usage",
    "not_found",
    "api_unreachable",
    "watch_failed",
    "storage_unavailable",
    "railway_unreachable",
    "interrupted",
    "refused",
)

#: One sentence per refusal reason, in the CLI's words, naming the fixing verb
#: where one exists. Never the Telegram adapter's wording.
REASON_SENTENCES: Mapping[str, str] = {
    "unknown_command": "no such command",
    "not_built": "that command is not available yet",
    "workspace_required": "this command needs a workspace — pass --workspace",
    "invalid_args": "the arguments were refused",
    "illegal_transition": "the story is not in a state this command applies to",
    "not_found": "no such story in this workspace",
    "manual_mode": (
        "Instagram API posting is off for this workspace — turn it on in"
        " Settings, or approve on the web"
    ),
    "cancelling": "the story is being cancelled",
    "not_connected": "this workspace has no connected Instagram account",
    "media_unsuitable": "the selected media is not currently authorized or ready",
    "slide_not_first": "this source is set to use only the first slide of each carousel",
    "nothing_to_confirm": (
        "Instagram was never asked to post this story, so there is nothing to confirm"
    ),
    "may_have_posted": (
        "the last publish answer was lost — say whether the story is on Instagram:"
        " resolve <story> retry --not-posted, or resolve <story> posted"
    ),
    "locked": "a lock, or the item itself, keeps it from being scheduled",
    "session_required": "this needs a signed-in web session, not a token",
    "readonly_token": "this token is read-only",
    "wrong_workspace": "this token belongs to another workspace",
    "not_ops": "only the people in OPS_USER_IDS may use this",
    # an admission's refusals (`POST /admissions`; `ADMIT_OUTCOMES`)
    "signed_up": (
        "already signed up — an account was created at or holds that address,"
        " or its link was used"
    ),
    "invalid_email": "that is not an address an admission can hold",
    # and the deployment's 503: no web origin to build the email's link from
    "not_configured": (
        "the API cannot build the email's sign-up link: it has no WEB_APP_URL"
    ),
    "not_authorized": "not authorized — run storydump login with a valid token",
    "not_a_member": "no such workspace for this token",
    # a member below the verb's floor: the API's bare 403 (the token is fine)
    "insufficient_role": "your role in this workspace does not allow this",
    # the API shedding load (a 429 with Retry-After): try again, not a refusal
    "pool_saturated": "the API is busy — try again in a moment",
    # the ingress's own refusal: the same idempotency key, a different command
    "admission_conflict": (
        "a different command was already sent under this idempotency key"
    ),
}

#: Every reason an error envelope may carry: the port's refusals (each with
#: a sentence above) and the CLI's own.
CLI_REASONS: tuple[str, ...] = tuple(REASON_SENTENCES) + CLI_OWN_REASONS

#: The port's outcomes as the CLI reports them.
OUTCOME_SENTENCES: Mapping[str, str] = {
    "executed": "done",
    "enqueued": "queued",
    "replayed": "already done",
    # a command on a story past awaiting_approval answers with the story's
    # state — nothing changed
    "answered": "nothing changed — the story had already answered",
}

#: What stands in the way of scheduling an item, in the CLI's words: the
#: blockers and warnings a `locked` refusal names — the item's own state,
#: then the lock kinds.
IN_THE_WAY: Mapping[str, str] = {
    "item_removed": "it was removed from the library",
    "item_unsupported": "Instagram cannot post it",
    "item_missing": "its file is no longer in its Drive folder",
    "reject": "it was rejected",
    "unsupported": "it is marked as one that cannot be posted",
    "hold": "it is on hold",
    "seasonal": "it is out of season",
    "skip": "it was skipped recently",
    "recent": "it was posted on this account recently",
}

#: Why a planned story's time was refused (`invalid_args` with an `at_rule`
#: fact), in the CLI's words.
AT_RULE_SENTENCES: Mapping[str, str] = {
    "shape": "give the time as YYYY-MM-DD HH:MM, with no offset",
    "not_a_date": "that is not a real date and time",
    "skipped": "that time does not happen in the account's zone: the clocks skip it",
    "past": "that time is not in the future",
    "horizon": f"that time is more than {PLAN_HORIZON_DAYS} days ahead",
}

#: Why the port refused a repeat rule (its `repeat` fact), in the CLI's
#: words: the CHECKs judged its shape (repeat rules), and the port the story's day,
#: which a weekly series' days must include.
REPEAT_SENTENCES: Mapping[str, str] = {
    "invalid": "that repeat is not one a story can keep",
    "off_day": "the story would fall on a day that is not one of the series' days",
}

#: What a `not_found` from `schedule_item` could not find (its `missing`
#: fact), in the CLI's words.
MISSING_SENTENCES: Mapping[str, str] = {
    "account": "no live account by that id in this workspace",
    "item": "no such item in this workspace",
}

#: A write's warnings, in the CLI's words (the answer carries the codes).
WARNING_SENTENCES: Mapping[str, str] = {
    NO_PUSH_BINDING: "no chat is bound to this workspace: nothing is asked until one is",
}

#: Words that belong to the Telegram adapter and never to a terminal.
TAP_WORDS: tuple[str, ...] = ("tap", "button", "card", "keyboard")

#: What the CLI says when a write lands, by the port's command and outcome;
#: anything else falls back to :data:`OUTCOME_SENTENCES`.
WRITE_SENTENCES: Mapping[tuple[str, str], str] = {
    ("approve", "enqueued"): "approved — posting shortly",
    ("approve", "executed"): "approved",
    ("skip", "executed"): "skipped",
    ("reject", "executed"): "rejected",
    ("mark_posted", "executed"): "marked as posted by hand",
    ("cancel", "executed"): "cancel requested",
    ("schedule_item", "executed"): "scheduled",
    ("reschedule_item", "executed"): "rescheduled",
    ("set_item_link", "executed"): "link updated",
    ("resolve_review", "executed"): "resolved",
    ("resolve_review", "enqueued"): "resolved — posting again shortly",
    ("pause_workspace", "executed"): "posting paused for the workspace",
    ("resume_workspace", "executed"): "posting resumed for the workspace",
    ("sync_now", "enqueued"): "sync queued",
    ("sync_now", "executed"): "synced",
}


def write_sentence(command: str, outcome: str) -> str:
    """The CLI's sentence for a write's answer: the verb's own when it has
    one, else the outcome's."""
    return WRITE_SENTENCES.get((command, outcome)) or OUTCOME_SENTENCES.get(
        outcome, outcome
    )


#: What `storydump admit` says for each of :data:`ADMIT_OUTCOMES` that leaves
#: the address admitted, the only answers it takes as one; a refusal
#: is the API's, in :data:`REASON_SENTENCES`.
ADMIT_SENTENCES: Mapping[str, str] = {
    "admitted": "admitted — the email is queued",
    "resent": "an email is queued",
    "admitted_before": (
        "already admitted — run it with --resend to send the email again"
    ),
}

#: The command port's idempotency reference: required on every command under
#: this header and bounded; the CLI refuses a longer key as usage.
IDEMPOTENCY_HEADER = "Idempotency-Key"
IDEMPOTENCY_KEY_MAX = 200

#: The review card's resolutions (the `review_required` exits a member
#: may take; `failed` — a refund — stays the operator's) and the one verdict a
#: resolution may carry: the member looked, and the story is not on Instagram.
RESOLUTIONS: tuple[str, ...] = ("retry", "posted", "cancel")
NOT_POSTED = "not_posted"


# --- the deployment's identities -----------------------------------------------
# Spelled once: the API's public host (the CLI's default, the webhook's door),
# the name of the Railway project the `deploys` seam refuses to read past, and
# the variable that holds that project's id.
API_URL = "https://api.storydump.app"
RAILWAY_PROJECT_NAME = "storydump"
#: The CLI's variable for the id of that project: a linked project whose id is
#: not this variable's value is refused (`storydump_cli.railway`).
RAILWAY_PROJECT_ID_VAR = "STORYDUMP_RAILWAY_PROJECT_ID"

# --- the deployed roots' database --------------------------------------------------
#: The one variable the worker and the API take their database from, read
#: from the process environment at RUN time (`unit_of_work.engine_url_from_env`).
#: Absent, the worker refuses to boot (exit 2) and the API answers 503 on every
#: data route; the CLI's `webhook` verb names it when it reads that 503.
DATABASE_URL_VAR = "TARGET_DATABASE_URL"

# --- the Telegram webhook's spellings --------------------------------------------
# One spelling of the deployment's names, shared by the API's startup
# self-registration (`src/channels/telegram_webhook_registration.py`) and the
# CLI's `webhook` verb — the CLI reaches `src` only through this module.

TELEGRAM_TOKEN_VAR = "TARGET_TELEGRAM_BOT_TOKEN"
TELEGRAM_SECRET_VAR = "TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN"
TELEGRAM_BOT_VAR = "TARGET_TELEGRAM_BOT_USERNAME"
WEBHOOK_URL_VAR = "TARGET_TELEGRAM_WEBHOOK_URL"
DEFAULT_WEBHOOK_URL = f"{API_URL}/webhooks/telegram"
WEBHOOK_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
#: The update kinds the target ingress serves: `/start` taps and group messages
#: ride `message`; a button tap on an approval card is a `callback_query`.
#: Telegram delivers ONLY what is asked for — with `message` alone every tap
#: is dropped before it reaches the route.
ALLOWED_UPDATES: list[str] = ["message", "callback_query"]
#: `setWebhook`'s `max_connections`: how many simultaneous deliveries Telegram
#: opens against the route (default 40, at most 100) — set to the ingress's
#: connection budget: one process × `POOL_SIZE_SEAM` (10).
MAX_CONNECTIONS_VAR = "TARGET_TELEGRAM_WEBHOOK_MAX_CONNECTIONS"
DEFAULT_MAX_CONNECTIONS = 10
#: Railway names the deployment's environment here. The API's autoregister
#: guard and the transport's production guard ask the same question of it,
#: and two literals is how a renamed environment is edited in one of them.
RAILWAY_ENVIRONMENT_VAR = "RAILWAY_ENVIRONMENT_NAME"
#: The one environment that owns the bot's webhook.
PRODUCTION_ENVIRONMENT = "production"


def is_production(env: Mapping[str, str]) -> bool:
    """Whether *env* is Railway's production environment."""
    value = env.get(RAILWAY_ENVIRONMENT_VAR) or ""
    return value.strip().lower() == PRODUCTION_ENVIRONMENT


#: Telegram's Bot API. Spelled here because the CLI's `webhook` verb and the
#: worker's transport both speak to it, and the CLI reaches `src` only here.
TELEGRAM_BOT_API_BASE = "https://api.telegram.org"


class BadMaxConnections(ValueError):
    """The connection cap is outside Telegram's 1..100."""


def max_connections_from(raw: Optional[str]) -> int:
    """The connection cap, from an environment value, within Telegram's 1..100."""
    if raw is None or not raw.strip():
        return DEFAULT_MAX_CONNECTIONS
    try:
        value = int(raw.strip())
    except ValueError:
        value = 0
    if not 1 <= value <= 100:
        raise BadMaxConnections(
            f"{MAX_CONNECTIONS_VAR} must be an integer from 1 to 100 (got {raw!r})"
        )
    return value


def bot_matches(username: str, expected: Optional[str]) -> bool:
    """The configured bot, if any, must be the token's bot — registering the
    door on the wrong bot is the one mistake this refuses by construction."""
    if not expected:
        return True
    return username.lstrip("@").lower() == expected.lstrip("@").lower()


# --- the read views' windows ------------------------------------------------

#: A span back from now: ``45m``, ``3h``, ``2d`` (at most six digits — the cap
#: below refuses anything wide long before the digits run out).
WINDOW_SPAN = re.compile(r"^(\d{1,6})([mhd])$")
WINDOW_UNITS: Mapping[str, str] = {"m": "minutes", "h": "hours", "d": "days"}
#: The widest window any read view answers; wider is refused (the API 422,
#: the CLI a usage error) rather than scanned.
MAX_WINDOW_DAYS = 30
#: `jobs`, `outbox` and `burst` look back this far by default.
DEFAULT_WINDOW = "3h"
#: `floating`'s default and ceiling (`ops_views.floating`, the route's 422 and
#: the CLI's `--limit`); every other list is windowed by `since`.
FLOATING_LIMIT = 100
FLOATING_LIMIT_MAX = 500
#: The Queue read's default page and its ceiling (every list is
#: bounded) — the API's clamp and the CLI's `planned --limit`.
LIST_LIMIT_DEFAULT = 50
LIST_LIMIT_MAX = 200
#: The widest range of local days a date-ranged read takes: a calendar month
#: drawn in whole weeks is at most six of them. The API's 422, and
#: the bound a read that walks its range holds a direct caller to.
RANGE_MAX_DAYS = 45
#: Two clocks judge one window — the CLI computes a span's start, the API
#: measures it against its own now — so a start this close to a bound is
#: clamped to the bound rather than refused (a `30d` from a client one second
#: behind, a timestamp from a clock one second ahead).
WINDOW_SLACK = dt.timedelta(minutes=5)


def window_start(value: str, now: dt.datetime) -> dt.datetime:
    """The start of a window: a span back from *now*, or an ISO-8601
    timestamp (``Z`` or an offset; a naive one is read as UTC; a bare date
    is its midnight UTC). Always an
    aware UTC datetime no wider than :data:`MAX_WINDOW_DAYS` and not in the
    future. Anything else raises ``ValueError`` with the sentence to show —
    the one grammar the API's ``since`` and the CLI's ``--since`` share.

    **Not** `rate_counters.window_start`, which shares the name and nothing
    else: that one truncates a datetime to its fixed counter bucket
    (`(now, window_seconds)`), this one PARSES a caller's string. They are
    never interchangeable, and renaming either moves a name the API routes,
    the CLI and the `rate_counters` PK all spell.
    """
    text = value.strip()
    anchor = now.astimezone(dt.timezone.utc).replace(microsecond=0)
    span = WINDOW_SPAN.match(text)
    try:
        if span:
            amount, unit = span.groups()
            start = anchor - dt.timedelta(**{WINDOW_UNITS[unit]: int(amount)})
        else:
            # Python 3.10's fromisoformat does not know the Z suffix
            parsed = dt.datetime.fromisoformat(
                text[:-1] + "+00:00" if text.endswith("Z") else text
            )
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            start = parsed.astimezone(dt.timezone.utc)
    except (ValueError, OverflowError):
        raise ValueError(
            f"not a window: {text!r} — give a span back from now (15m, 3h, 1d)"
            " or an ISO-8601 timestamp"
        ) from None
    widest = anchor - dt.timedelta(days=MAX_WINDOW_DAYS)
    if start < widest - WINDOW_SLACK:
        raise ValueError(f"a window is at most {MAX_WINDOW_DAYS} days: {text!r}")
    if start > anchor + WINDOW_SLACK:
        raise ValueError(f"a window cannot start in the future: {text!r}")
    return min(max(start, widest), anchor)
