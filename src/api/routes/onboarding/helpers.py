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

    Authorization is a server-side **active membership** for ``(user, chat_id)``
    — the token alone never authorizes:

    * A **bound token** (a signed URL token, or initData launched from a group)
      carries a ``chat_id``. The cryptographic binding fixes *which* chat the
      request targets — reuse against a different ``chat_id`` is rejected — but
      it does not prove the user may still act on that chat: a revoked member,
      or a group member who was never provisioned, keeps a usable token until
      TTL expiry. The binding narrows; it does not authorize.
    * An **unbound token** (initData launched from a DM) has no ``chat`` field,
      so the request-supplied ``chat_id`` is attacker-suppliable.

    Either way the caller must be an active member of ``chat_id``. This closes
    both the cross-tenant IDOR (a DM-launched token replayed against an arbitrary
    ``chat_id``) and the stale-access hole (a bound token outliving the user's
    membership).

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

    # Every path requires a server-side active membership for chat_id. A bound
    # token proves the user once belonged to this chat, not that they still do;
    # an unbound token's request chat_id is untrusted. The active
    # UserChatMembership is the authorization.
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


def _validate_admin(init_data: str, request: Request | None = None) -> dict:
    """Validate auth and authorize the caller as a SYSTEM administrator.

    For endpoints exposing deployment-wide operational telemetry rather than
    tenant data. Authentication proves who the caller is; it does not make
    them an operator. Every *authenticated* tenant user reaching a fleet-wide
    view is precisely the gap this closes — the data carries no tenant rows,
    so it is an authorization gap rather than a data leak, but a tenant still
    has no business reading the deployment's health.

    The gate is the system-level ``users.role``. It is deliberately NOT
    ``UserChatMembership.instance_role``: that role makes someone an owner of
    their own instance, which is not the same authority, and accepting it
    here would re-open the endpoint one tenant at a time.

    Raises HTTPException(401) on auth failure, HTTPException(403) if the
    caller is authenticated but not a system administrator.
    """
    user_info = _validate_auth(init_data, request)

    user_id = user_info.get("user_id")
    with MembershipService() as membership_service:
        authorized = membership_service.is_system_admin(user_id)

    if not authorized:
        ip = _client_ip(request)
        logger.warning(
            "Admin denied: user_id=%s is not a system administrator (ip=%s)",
            user_id,
            ip,
        )
        auth_monitor.record_failure(ip, "admin role required")
        raise HTTPException(status_code=403, detail="Administrator access required")

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
