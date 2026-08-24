"""Telegram WebApp initData validation and URL token authentication."""

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qs

from src.config.settings import settings

INIT_DATA_TTL = 3600  # 1 hour
URL_TOKEN_TTL = 3600  # 1 hour

# How far ahead of our own clock a credential's timestamp may sit before we
# reject it. Credentials are stamped by a peer whose clock we do not control —
# Telegram stamps initData; the worker mints URL tokens that the API validates
# from a separate container — so some forward skew is normal and must be
# tolerated, or ordinary drift becomes an auth outage.
#
# It has to be bounded, though: expiry is computed from the age of the stamp,
# so an unbounded future timestamp yields a negative age that never exceeds
# the TTL and the credential never expires.
#
# The worst-case validity window is this value plus the TTL, so 60s against a
# 3600s TTL leaves the window governed by the TTL while absorbing drift far
# beyond what a synced host shows. Fernet picks the same value for the same
# rule (cryptography.fernet._MAX_CLOCK_SKEW). Skew past a minute is a clock to
# fix, not a tolerance to widen.
CLOCK_SKEW_TOLERANCE = 60  # 1 minute

# Web-session credential (#1015). A BFF-minted credential for a user who has no
# Telegram identity at all, so nothing about it can be Telegram-shaped: the
# subject is the platform-neutral `users.id`, the tenant is `chat_settings.id`,
# and the signing key is its own secret rather than the bot token.
#
# WHY A SEPARATE SECRET AND NOT TELEGRAM_BOT_TOKEN. The bot token is the crypto
# root for both Telegram credentials above. A user who never touches Telegram
# must not depend on it: rotating the bot token would invalidate web sessions,
# and a bot-token compromise would mint web credentials. Severing the root is
# most of the point of this credential existing.
WEB_TOKEN_TTL = 3600  # 1 hour, matching URL_TOKEN_TTL

# Version prefix, and a separator that CANNOT occur in the payload. UUIDs carry
# hyphens but never dots, so a dot-delimited web token and a colon-delimited URL
# token are mutually unparseable rather than merely different -- neither
# validator can partially consume the other's input and report a misleading
# reason.
# TWO SHAPES, and the tag is in the PREFIX so a parser dispatches on an exact
# string before it touches a single field:
#
#   sd1b.{user_uuid}.{tenant_uuid}.{ts}.{nonce}.{sig}   BOUND   -- names a tenant
#   sd1u.{user_uuid}.{ts}.{nonce}.{sig}                 UNBOUND -- names none
#
# The unbound shape exists because a user EXISTS BEFORE THEY OWN ANYTHING.
# Tenants are minted lazily at an explicit provisioning door (#842), so a user
# between sign-in and first workspace has no tenant to name -- and the door
# itself is reached by an authenticated call, so a credential that could not be
# minted without a tenant could never reach the door that mints one.
#
# This is not a new concept in this codebase: initData is already bound (group
# launch) or unbound (DM launch), and _validate_request already treats an
# unbound token's tenant claim as untrusted. This gives the web credential the
# same two shapes rather than inventing a third idea.
#
# THE TENANT SLOT IS ABSENT IN THE UNBOUND SHAPE, NEVER EMPTY. A sentinel -- ""
# or "-" or "null" -- is a value sitting where a tenant id goes, and every
# downstream reader then has to remember to special-case it. Absence cannot be
# coerced into a tenant id by a reader that forgot.
WEB_TOKEN_PREFIX_BOUND = "sd1b"
WEB_TOKEN_PREFIX_UNBOUND = "sd1u"
WEB_TOKEN_PARTS_BOUND = 6
WEB_TOKEN_PARTS_UNBOUND = 5


def validate_init_data(init_data: str) -> dict:
    """Validate Telegram WebApp initData and extract user info.

    The initData string is signed by Telegram using HMAC-SHA256 with a key
    derived from the bot token. This proves the request came from a real
    Telegram user via the WebApp SDK.

    Args:
        init_data: The raw initData string from Telegram.WebApp.initData

    Returns:
        dict with user_id, first_name

    Raises:
        ValueError: If signature is invalid, data is expired or future-dated,
            or hash is missing
    """
    if not init_data:
        raise ValueError("Empty initData")

    parsed = parse_qs(init_data)

    # Extract and remove hash
    received_hash = parsed.pop("hash", [None])[0]
    if not received_hash:
        raise ValueError("Missing hash in initData")

    # Sort remaining params alphabetically, join with newlines
    data_check_string = "\n".join(f"{k}={v[0]}" for k, v in sorted(parsed.items()))

    # HMAC-SHA256: secret = HMAC("WebAppData", bot_token)
    secret_key = hmac.new(
        b"WebAppData", settings.TELEGRAM_BOT_TOKEN.encode(), hashlib.sha256
    ).digest()
    computed_hash = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        raise ValueError("Invalid initData signature")

    # Check TTL — bounded in both directions, see CLOCK_SKEW_TOLERANCE
    auth_date = int(parsed.get("auth_date", [0])[0])
    age = time.time() - auth_date
    if age < -CLOCK_SKEW_TOLERANCE:
        raise ValueError("initData auth_date is in the future")
    if age > INIT_DATA_TTL:
        raise ValueError("initData expired")

    # Parse user JSON
    user_json = parsed.get("user", ["{}"])[0]
    user_data = json.loads(user_json)

    result = {
        "user_id": user_data.get("id"),
        "first_name": user_data.get("first_name"),
    }

    # Extract chat_id if present (available when opened from a group chat)
    chat_json = parsed.get("chat", [None])[0]
    if chat_json:
        chat_data = json.loads(chat_json)
        result["chat_id"] = chat_data.get("id")

    return result


def generate_url_token(chat_id: int, user_id: int) -> str:
    """Generate a signed URL token for browser-based webapp access.

    Used when WebAppInfo buttons aren't available (e.g. group chats)
    and the webapp is opened via a regular URL button instead.

    Token format: {chat_id}:{user_id}:{timestamp}:{signature}
    """
    timestamp = int(time.time())
    payload = f"{chat_id}:{user_id}:{timestamp}"
    secret_key = hmac.new(
        b"UrlToken", settings.TELEGRAM_BOT_TOKEN.encode(), hashlib.sha256
    ).digest()
    signature = hmac.new(secret_key, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{signature}"


def validate_url_token(token: str) -> dict:
    """Validate a signed URL token and extract chat/user info.

    Args:
        token: The token string from the URL parameter.

    Returns:
        dict with user_id, chat_id

    Raises:
        ValueError: If signature is invalid, or the token is expired or
            future-dated.
    """
    if not token:
        raise ValueError("Empty token")

    parts = token.split(":")
    if len(parts) != 4:
        raise ValueError("Invalid token format")

    chat_id_str, user_id_str, timestamp_str, received_sig = parts

    try:
        chat_id = int(chat_id_str)
        user_id = int(user_id_str)
        timestamp = int(timestamp_str)
    except (ValueError, TypeError):
        raise ValueError("Invalid token values")

    # Verify signature
    payload = f"{chat_id}:{user_id}:{timestamp}"
    secret_key = hmac.new(
        b"UrlToken", settings.TELEGRAM_BOT_TOKEN.encode(), hashlib.sha256
    ).digest()
    computed_sig = hmac.new(secret_key, payload.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_sig, received_sig):
        raise ValueError("Invalid token signature")

    # Check TTL — bounded in both directions, see CLOCK_SKEW_TOLERANCE
    age = time.time() - timestamp
    if age < -CLOCK_SKEW_TOLERANCE:
        raise ValueError("Token timestamp is in the future")
    if age > URL_TOKEN_TTL:
        raise ValueError("Token expired")

    return {"user_id": user_id, "chat_id": chat_id}


def _web_token_key() -> bytes:
    """Derive the web-credential signing key, or refuse.

    Fail-closed on an unset secret: an empty key would still produce a valid
    HMAC, so every deployment that forgot to configure one would silently share
    the same signing key. Refusing here means an unconfigured deployment mints
    and accepts nothing rather than accepting everything.
    """
    secret = getattr(settings, "WEB_TOKEN_SECRET", None)
    if not secret:
        raise ValueError("WEB_TOKEN_SECRET is not configured")
    return hmac.new(b"WebToken", secret.encode(), hashlib.sha256).digest()


def _web_token_payload(
    user_uuid: str, chat_settings_id: str | None, timestamp: int
) -> str:
    """The signed payload, built in ONE place.

    Mint and validate must agree byte-for-byte or every token fails, so the
    shape lives here rather than in two branches that have to be kept in step.
    The PREFIX is inside it deliberately: that is what makes the two shapes
    non-interconvertible, since a bound payload re-presented as unbound no
    longer reproduces its signature.
    """
    if chat_settings_id is None:
        return f"{WEB_TOKEN_PREFIX_UNBOUND}.{user_uuid}.{timestamp}"
    return f"{WEB_TOKEN_PREFIX_BOUND}.{user_uuid}.{chat_settings_id}.{timestamp}"


def generate_web_token(user_uuid: str, chat_settings_id: str | None, nonce: str) -> str:
    """Mint a web-session credential, bound or unbound.

    ``chat_settings_id`` names the tenant this credential is scoped to, or
    ``None`` for a user who has none yet (between sign-in and first workspace).
    The two produce structurally different tokens -- see the prefix constants.

    ``nonce`` is carried from birth for #587 (single-use replay protection).
    THIS FUNCTION DOES NOT MAKE THE TOKEN SINGLE-USE -- there is no nonce store
    yet, and within the TTL a captured token still replays exactly as the
    Telegram credentials do. The field exists so that adding the store is a
    change to the validator alone, never a format migration.
    """
    fields = [("user_uuid", user_uuid), ("nonce", nonce)]
    if chat_settings_id is not None:
        fields.append(("chat_settings_id", chat_settings_id))
    for name, value in fields:
        if not value or "." in str(value):
            raise ValueError(f"Invalid {name} for a web token")

    timestamp = int(time.time())
    payload = f"{_web_token_payload(user_uuid, chat_settings_id, timestamp)}.{nonce}"
    signature = hmac.new(_web_token_key(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def validate_web_token(token: str) -> dict:
    """Validate a web-session credential and extract its subjects.

    Returns:
        dict with ``user_uuid`` (a ``users.id`` UUID string) and ``nonce``,
        plus ``chat_settings_id`` **only when the credential is bound**.

        AN UNBOUND RESULT OMITS THE TENANT KEY RATHER THAN CARRYING A FALSY
        ONE. A caller reading ``result["chat_settings_id"]`` raises; one reading
        ``.get(...)`` receives ``None``. Both are survivable. A caller handed
        ``""`` and passing it on as a tenant id is not, and that is the only
        outcome this shape makes unavailable.

        NEITHER KEY COLLIDES WITH THE TELEGRAM VALIDATORS, and that is a safety
        property rather than a naming preference. Those return ``user_id`` and
        ``chat_id`` carrying INTEGERS. Reusing ``user_id`` for a UUID string
        would hand that string to ``is_active_member``, whose repository
        compares it against a ``BigInteger`` column. Under distinct names a
        Telegram-shaped caller reading ``user_id``/``chat_id`` off a web result
        gets ``None`` for both and fails closed, which is the outcome we want
        from a caller that has not been taught about this credential.

    Raises:
        ValueError: on a missing secret, a malformed token, a bad signature, or
            a token that is expired or future-dated.
    """
    if not token:
        raise ValueError("Empty token")

    parts = token.split(".")
    if not parts:
        raise ValueError("Invalid token format")

    # Dispatch on the exact prefix, never on field count: a shape is declared by
    # the minter, never inferred from what survived transit.
    if parts[0] == WEB_TOKEN_PREFIX_BOUND and len(parts) == WEB_TOKEN_PARTS_BOUND:
        _, user_uuid, chat_settings_id, timestamp_str, nonce, received_sig = parts
    elif parts[0] == WEB_TOKEN_PREFIX_UNBOUND and len(parts) == WEB_TOKEN_PARTS_UNBOUND:
        _, user_uuid, timestamp_str, nonce, received_sig = parts
        chat_settings_id = None
    else:
        raise ValueError("Invalid token format")

    if not user_uuid or not nonce or chat_settings_id == "":
        raise ValueError("Invalid token values")

    try:
        timestamp = int(timestamp_str)
    except (ValueError, TypeError):
        raise ValueError("Invalid token values")

    payload = f"{_web_token_payload(user_uuid, chat_settings_id, timestamp)}.{nonce}"
    computed_sig = hmac.new(
        _web_token_key(), payload.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(computed_sig, received_sig):
        raise ValueError("Invalid token signature")

    # Bounded in both directions on the same terms as the URL token; see
    # CLOCK_SKEW_TOLERANCE.
    age = time.time() - timestamp
    if age < -CLOCK_SKEW_TOLERANCE:
        raise ValueError("Token timestamp is in the future")
    if age > WEB_TOKEN_TTL:
        raise ValueError("Token expired")

    result = {"user_uuid": user_uuid, "nonce": nonce}
    if chat_settings_id is not None:
        result["chat_settings_id"] = chat_settings_id
    return result
