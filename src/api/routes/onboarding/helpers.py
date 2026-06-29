"""Shared helpers for onboarding API routes."""

import re
from contextlib import contextmanager

from fastapi import HTTPException, Request

from src.services.core.membership_service import MembershipService
from src.services.core.setup_state_service import SetupStateService
from src.utils import auth_monitor
from src.utils.logger import logger
from src.utils.webapp_auth import validate_init_data, validate_url_token

# Google Drive folder URL pattern
GDRIVE_FOLDER_RE = re.compile(
    r"https?://drive\.google\.com/drive/folders/([a-zA-Z0-9_-]+)"
)


def _client_ip(request: Request | None) -> str:
    """Extract client IP from a FastAPI request, or 'unknown'."""
    if request and request.client:
        return request.client.host
    return "unknown"


def _validate_auth(init_data: str, request: Request | None = None) -> dict:
    """Validate initData or URL token — auth only, no chat_id check.

    Accepts either Telegram WebApp initData (from Mini App) or a signed
    URL token (from browser links). Returns user info dict on success.

    Raises HTTPException(401) on auth failure.
    """
    try:
        return validate_init_data(init_data)
    except ValueError:
        try:
            return validate_url_token(init_data)
        except ValueError as e:
            ip = _client_ip(request)
            auth_monitor.record_failure(ip, str(e))
            raise HTTPException(status_code=401, detail=str(e))


def _validate_request(
    init_data: str, chat_id: int, request: Request | None = None
) -> dict:
    """Validate initData or URL token and authorize the caller for ``chat_id``.

    Two authorization paths, by whether the token binds a chat:

    * **Bound token** — a signed URL token, or initData launched from a group,
      carries a ``chat_id``. The cryptographic binding *is* the authorization
      (the server only ever issues it to a legitimate user of that chat); we
      reject only its reuse against a different ``chat_id``.
    * **Unbound token** — initData launched from a DM has no ``chat`` field, so
      the request-supplied ``chat_id`` is attacker-suppliable. We authorize via
      a server-side active-membership lookup instead of trusting it. This closes
      the cross-tenant IDOR where a DM-launched token is replayed against an
      arbitrary ``chat_id``.

    Raises HTTPException(401) on auth failure, HTTPException(403) on a chat_id
    mismatch or a missing/inactive membership.
    """
    user_info = _validate_auth(init_data, request)

    signed_chat_id = user_info.get("chat_id")

    # Bound token: the binding authorizes; reject only its reuse against another chat.
    if signed_chat_id is not None and signed_chat_id != chat_id:
        ip = _client_ip(request)
        logger.warning(
            "Chat ID mismatch: auth has %s, request has %s (user_id=%s, ip=%s)",
            signed_chat_id,
            chat_id,
            user_info.get("user_id"),
            ip,
        )
        auth_monitor.record_failure(
            ip, f"chat_id mismatch: signed={signed_chat_id} req={chat_id}"
        )
        raise HTTPException(status_code=403, detail="Chat ID mismatch")

    # Unbound token: the request-supplied chat_id is untrusted, so require a
    # server-side active membership for the chat.
    if signed_chat_id is None:
        user_id = user_info.get("user_id")
        with MembershipService() as membership_service:
            authorized = membership_service.is_active_member(user_id, chat_id)
        if not authorized:
            ip = _client_ip(request)
            logger.warning(
                "Membership denied: user_id=%s is not an active member of chat %s (ip=%s)",
                user_id,
                chat_id,
                ip,
            )
            auth_monitor.record_failure(ip, "membership denied")
            raise HTTPException(status_code=403, detail="Not a member of this instance")

    return user_info


def _get_setup_state(telegram_chat_id: int) -> dict:
    """Build the current setup state for a chat."""
    with SetupStateService() as service:
        return service.get_setup_state(telegram_chat_id)


@contextmanager
def service_error_handler():
    """Convert service ValueError exceptions to HTTP 400 responses."""
    try:
        yield
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
