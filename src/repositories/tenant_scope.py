"""Tenant scope vocabulary for the repository layer (F.1, #841).

The tenant boundary of the legacy schema is ``chat_settings``; tenant-scoped
repository methods take the owning ``chat_settings_id``. F.1's rule
(`documentation/planning/2026-08-11-f1-ownership-inventory/README.md` §3) is
that absent tenant context is an error at the call boundary, never a widened
query — "None means everything" is the fail-open pattern this module retires.

Tenant-less access itself is legitimate and pre-dates scoping: worker loops,
maintenance sweeps, health checks and the CLI operate across tenants by
design (the same sanctioned internal path ``write_allowed`` below has always
documented). What changes is that it must now be *chosen*, in
writing, at the call site:

    repo.get_all(chat_settings_id=SYSTEM_SCOPE)

so every cross-tenant access is a visible, greppable decision instead of a
silently inherited default. ``grep -rn SYSTEM_SCOPE src/ cli/`` is the
standing inventory of tenant-less access — the burn-down list #841 tracks.
"""

import logging
from typing import Union

from src.exceptions.base import StorydumpError

logger = logging.getLogger(__name__)


class SystemScope:
    """Singleton marker: deliberate cross-tenant (system/maintenance) access.

    Falsy on purpose: pre-existing ``if chat_settings_id:`` filter branches
    treat SYSTEM_SCOPE exactly as they treated None — no tenant filter — so
    converting a call site from silent omission to explicit SYSTEM_SCOPE is
    behavior-preserving by construction.
    """

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "SYSTEM_SCOPE"


SYSTEM_SCOPE = SystemScope()

#: What tenant-scoped repository methods accept: the owning chat_settings id
#: (stringified), or the explicit system-scope marker. Never None.
TenantScope = Union[str, SystemScope]


class TenantContextError(StorydumpError):
    """Tenant context was absent where a tenant-scoped query needed one.

    Raised at the call boundary (before any SQL executes) when a
    tenant-scoped repository path receives None/empty instead of a tenant id
    or the explicit SYSTEM_SCOPE marker. Absent context must never widen a
    query.
    """


def require_tenant_id(chat_settings_id, *, where: str) -> None:
    """Refuse everything but a real tenant id — no cross-tenant door.

    For mandatory-tenant methods (the ``*_for_chat`` family, per-instance
    audit reads): SYSTEM_SCOPE would bind the marker object into SQL, so it
    is refused alongside None. Absence of a system door is a statement, not
    an accident of the body.
    """
    if isinstance(chat_settings_id, SystemScope) or not chat_settings_id:
        raise TenantContextError(
            f"{where}: a real tenant id is required — this method has no "
            "cross-tenant door (F.1/#841)"
        )


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


def write_allowed(owner_chat_settings_id, chat_settings_id) -> bool:
    """Whether a caller acting as ``chat_settings_id`` may mutate a row owned
    by ``owner_chat_settings_id`` (#597 cross-tenant write guard).

    The tenant boundary is ``chat_settings``; a row belongs to a tenant via
    its ``chat_settings_id`` column. Rules:

    - SYSTEM_SCOPE caller (internal/worker path, e.g. the dedup CLI):
      permitted — behavior is unchanged from before scoping existed.
    - Row owned by a DIFFERENT tenant: refused. This is the hole #597 closes
      — a caller cannot mutate another tenant's row by knowing its UUID.
    - Legacy row with a NULL ``chat_settings_id`` (pre-#412 ownership
      backfill): permitted. A strict ``== tenant`` filter would exclude these
      rows and silently no-op every write on not-yet-backfilled data. Mirrors
      the NULL-owned fallback #541 established for the worker notification
      layer, and the owned-OR-NULL read rule of #895.

    Model-agnostic on purpose: the rule is a property of tenancy, not of any
    one table. It lived on ``MediaRepository`` until ``QueueRepository``
    needed the identical predicate (#841 burn-down item 3); a second copy
    would have been two rules that drift.

    A THIRD expression survives and is knowingly not absorbed here:
    ``telegram_utils.caller_may_act_on_queue_row`` (#895), the handler-door
    read gate. It differs on one input — a falsy caller is REFUSED there and
    PERMITTED here (the SYSTEM_SCOPE escape) — so delegating naively would
    flip a security gate fail-open. Consolidating it means keeping that
    guard and delegating only the comparison; that is a change to the #895
    gate and belongs in its own diff.
    """
    if not chat_settings_id:
        return True
    if owner_chat_settings_id is None:
        return True
    return str(owner_chat_settings_id) == str(chat_settings_id)


def scope_of_row(row, *, where: str) -> TenantScope:
    """The tenant scope carried by a row the caller ALREADY HOLDS.

    Second-hop reads — "fetch the media item belonging to this queue row" —
    never needed cross-tenant access. The row was ownership-checked at the
    handler door (``caller_may_act_on_queue_row``), so its own stamp is the
    scope its dependents should be read under. Passing SYSTEM_SCOPE there
    granted the whole estate in order to fetch one row's own media: a control
    that reads fine and is not looking.

    This mirrors the owned-OR-NULL rule rather than inventing a second
    tenancy semantics. A stamped row scopes to its stamp. An UNSTAMPED row
    (legacy single-tenant data that predates the #412 ownership backfill) has
    no owner to scope to and resolves to SYSTEM_SCOPE — logged at WARNING,
    because that residual is exactly what the #841 burn-down is still burning
    down, and a silent fallback would make the remaining exposure
    unobservable in production.

    ``row`` must not be None. A caller holding nothing is not doing a
    second-hop read, and returning SYSTEM_SCOPE for an absent row would
    rebuild the fail-open default one layer up — "no context means
    everything", which is the pattern F.1 retires.
    """
    if row is None:
        raise TenantContextError(
            f"{where}: scope_of_row needs the row the caller already holds; "
            "None has no scope to carry (F.1/#841)"
        )
    stamp = getattr(row, "chat_settings_id", None)
    if stamp is None:
        logger.warning(
            "%s: reading dependents of legacy UNOWNED row %s under SYSTEM_SCOPE "
            "(pre-#412 ownership backfill residual; #841 burn-down)",
            where,
            getattr(row, "id", "<no id>"),
        )
        return SYSTEM_SCOPE
    return str(stamp)
