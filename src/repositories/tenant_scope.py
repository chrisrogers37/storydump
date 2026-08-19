"""Tenant scope vocabulary for the repository layer (F.1, #841).

The tenant boundary of the legacy schema is ``chat_settings``; tenant-scoped
repository methods take the owning ``chat_settings_id``. F.1's rule
(`documentation/planning/2026-08-11-f1-ownership-inventory/README.md` §3) is
that absent tenant context is an error at the call boundary, never a widened
query — "None means everything" is the fail-open pattern this module retires.

Tenant-less access itself is legitimate and pre-dates scoping: worker loops,
maintenance sweeps, health checks and the CLI operate across tenants by
design (the same sanctioned internal path ``MediaRepository._write_allowed``
has always documented). What changes is that it must now be *chosen*, in
writing, at the call site:

    repo.get_all(chat_settings_id=SYSTEM_SCOPE)

so every cross-tenant access is a visible, greppable decision instead of a
silently inherited default. ``grep -rn SYSTEM_SCOPE src/ cli/`` is the
standing inventory of tenant-less access — the burn-down list #841 tracks.
"""

from typing import Union


class SystemScope:
    """Singleton marker: deliberate cross-tenant (system/maintenance) access.

    Falsy on purpose: pre-existing ``if chat_settings_id:`` filter branches
    treat SYSTEM_SCOPE exactly as they treated None — no tenant filter — so
    converting a call site from silent omission to explicit SYSTEM_SCOPE is
    behavior-preserving by construction.
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "SYSTEM_SCOPE"


SYSTEM_SCOPE = SystemScope()

#: What tenant-scoped repository methods accept: the owning chat_settings id
#: (stringified), or the explicit system-scope marker. Never None.
TenantScope = Union[str, SystemScope]


class TenantContextError(RuntimeError):
    """Tenant context was absent where a tenant-scoped query needed one.

    Raised at the call boundary (before any SQL executes) when a
    tenant-scoped repository path receives None/empty instead of a tenant id
    or the explicit SYSTEM_SCOPE marker. Absent context must never widen a
    query.
    """


def tenant_value(chat_settings_id):
    """The column value for a tenant scope: SYSTEM_SCOPE stamps NULL.

    Row-construction sites write ownership from the tenant param. A real
    tenant id is the owner; deliberate system-scope writes carry no owner
    (exactly what silent omission produced before F.1), so the marker must
    never leak into a column.
    """
    if isinstance(chat_settings_id, SystemScope):
        return None
    return chat_settings_id


def require_tenant_context(chat_settings_id, *, where: str) -> None:
    """Refuse absent tenant context; accept a tenant id or SYSTEM_SCOPE.

    ``where`` names the refusing method so the error is actionable at a
    glance in a worker log.
    """
    if isinstance(chat_settings_id, SystemScope):
        return
    if chat_settings_id:
        return
    raise TenantContextError(
        f"{where}: tenant context is required — pass the owning "
        "chat_settings_id, or SYSTEM_SCOPE for deliberate cross-tenant "
        "access (F.1/#841; absent context never widens a query)"
    )
