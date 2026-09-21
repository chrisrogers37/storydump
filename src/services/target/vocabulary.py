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

#: The closed inbound vocabulary — `01` §Interaction-layer port, verbatim
#: order. Set-equality with the doc is asserted by the unit gate.
COMMANDS: tuple[str, ...] = (
    "approve",
    "skip",
    "reject",
    "mark_posted",
    "cancel",
    "autopost_now",
    "sync_now",
    "settings_change",
    "account_settings_change",
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
)

#: `post_intents.state` (055 ``ck_intent_state``), in the migration's order.
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

#: `post_intents.publish_step` (055 ``ck_intent_step``).
PUBLISH_STEPS: tuple[str, ...] = (
    "none",
    "transit_uploaded",
    "container_created",
    "container_ready",
    "publish_called",
    "effect_confirmed",
)

#: `audit_events.actor_kind` (055 ``ck_audit_actor``).
AUDIT_ACTOR_KINDS: tuple[str, ...] = (
    "user",
    "system",
    "clock",
    "reaper",
    "reconciler",
    "operator",
    "migration",
)

#: `audit_events.channel` (055 ``ck_audit_channel``); the admission channels
#: are the first three (``webhook_ingress.CHANNELS``).
AUDIT_CHANNELS: tuple[str, ...] = ("telegram", "web", "cli", "system")

#: `service_tokens.role` (060 ``ck_service_token_role``).
TOKEN_ROLES: tuple[str, ...] = ("operator", "readonly")

#: The bounds every adapter enforces before the table does: a token's name
#: (`service_tokens.name`), and its expiry in whole days — the API's default
#: and ceiling, the web form's range, the CLI's `tokens` verbs' words.
TOKEN_NAME_MAX = 80
TOKEN_EXPIRY_DAYS_MIN = 1
TOKEN_EXPIRY_DAYS_DEFAULT = 90
TOKEN_EXPIRY_DAYS_MAX = 365

#: Every API token starts with this; the resolver routes on it and secret
#: scanners recognise it. The rest is 32 url-safe random bytes (43 chars).
TOKEN_PREFIX = "sdt_"

#: The refusals a token principal gets WITH a ``reason`` (403): the route is
#: session-only, the token cannot write, or it belongs to another workspace.
TOKEN_REFUSALS: tuple[str, ...] = (
    "session_required",
    "readonly_token",
    "wrong_workspace",
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
#: module, because seven modules had a hand copy each (#1325 audit, TD-B6).
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
    "nothing_to_confirm": (
        "Instagram was never asked to post this story, so there is nothing to confirm"
    ),
    "may_have_posted": (
        "the last publish answer was lost — say whether the story is on Instagram:"
        " resolve <story> retry --not-posted, or resolve <story> posted"
    ),
    "session_required": "this needs a signed-in web session, not a token",
    "readonly_token": "this token is read-only",
    "wrong_workspace": "this token belongs to another workspace",
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
    # state (F2 (a) of the tap plan) — nothing changed
    "answered": "nothing changed — the story had already answered",
}

#: Words that belong to the Telegram adapter and never to a terminal.
TAP_WORDS: tuple[str, ...] = ("tap", "button", "card", "keyboard")

#: What the CLI says when a write lands, by the port's command and outcome;
#: anything else falls back to :data:`OUTCOME_SENTENCES` (phase 03).
WRITE_SENTENCES: Mapping[tuple[str, str], str] = {
    ("approve", "enqueued"): "approved — posting shortly",
    ("approve", "executed"): "approved",
    ("skip", "executed"): "skipped",
    ("reject", "executed"): "rejected",
    ("mark_posted", "executed"): "marked as posted by hand",
    ("cancel", "executed"): "cancel requested",
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


#: The command port's idempotency reference: required on every command under
#: this header and bounded; the CLI refuses a longer key as usage.
IDEMPOTENCY_HEADER = "Idempotency-Key"
IDEMPOTENCY_KEY_MAX = 200

#: The review card's resolutions (`02` §4's `review_required` exits a member
#: may take; `failed` — a refund — stays the operator's) and the one verdict a
#: resolution may carry: the member looked, and the story is not on Instagram.
RESOLUTIONS: tuple[str, ...] = ("retry", "posted", "cancel")
NOT_POSTED = "not_posted"


# --- the deployment's identities -----------------------------------------------
# Spelled once: the API's public host (the CLI's default, the webhook's door)
# and the Railway project the `deploys` seam refuses to read past.
API_URL = "https://api.storydump.app"
RAILWAY_PROJECT_NAME = "storydump"
RAILWAY_PROJECT_ID = "33d1ccca-353c-4236-8d39-0d8fd916f054"

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
#: was dropped before it reached the route (the 2026-09-09 plan's first blocker).
ALLOWED_UPDATES: list[str] = ["message", "callback_query"]
#: `setWebhook`'s `max_connections`: how many simultaneous deliveries Telegram
#: opens against the route (default 40, at most 100) — set to the ingress's
#: connection budget: one process × `POOL_SIZE_SEAM` (10) today.
MAX_CONNECTIONS_VAR = "TARGET_TELEGRAM_WEBHOOK_MAX_CONNECTIONS"
DEFAULT_MAX_CONNECTIONS = 10
#: Railway names the deployment's environment here. The API's autoregister
#: guard and the transport's production guard ask the same question of it,
#: and two literals is how a renamed environment is edited in one of them.
RAILWAY_ENVIRONMENT_VAR = "RAILWAY_ENVIRONMENT_NAME"
#: The one environment that owns the bot's webhook.
PRODUCTION_ENVIRONMENT = "production"
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
