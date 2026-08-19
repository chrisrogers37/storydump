"""Neutral tenant resolution (F.3, #842)."""

from src.services.tenancy.resolver import (
    ABSENT,
    PROVISION,
    REFUSE,
    TenantResolution,
    TenantResolutionError,
    resolve_tenant_from_chat_id,
    resolve_tenant_from_web_session,
)

__all__ = [
    "ABSENT",
    "PROVISION",
    "REFUSE",
    "TenantResolution",
    "TenantResolutionError",
    "resolve_tenant_from_chat_id",
    "resolve_tenant_from_web_session",
]
