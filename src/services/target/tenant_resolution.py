"""F.3 — neutral tenant resolution: the ONE resolver (#842, `04` §F.3).

Inbound identity (chat id | web session) resolves HERE, once, to the triple
`01` §1 names — (user_id, workspace_id, channel_binding_id) — and everything
inland speaks ``tenant_id == workspaces.id``. Chat ids die at this boundary:
no signature past this module accepts one (the F.6 ratchet rule's semantic
half, installed at birth rather than retrofitted).

Relation to the F.1 chokepoint, stated because the two surfaces must agree
rather than drift (#842 dispatch): they are SEPARATE BY DESIGN and never call
each other. F.1's ``tenant_scope`` guards the LEGACY repository layer — its
tenant key is ``chat_settings_id``, its lifetime ends at M.3. This module
mints TARGET tenant identity — ``workspaces.id`` — consumed by the L.0 unit
of work, which sets the ``app.tenant_id`` GUC that F.2's policies enforce.
What they share is the contract, not the code: absent or unresolvable
context is a typed refusal, never a widened default, and both refusal types
descend from ``StorydumpError``.

Privilege reality, measured in the tests rather than assumed (`02` §7):

- The WEB half runs as ``svc_ingress`` today, in two steps the router keeps
  apart on purpose (a principal with no workspace is a normal state):
  `sessions.resolve` authenticates on the auth-plane (``session_tokens`` —
  "the door tenant context walks through", readable before any
  ``app.tenant_id`` exists), then :func:`authorize_member` sets the claimed
  tenant with ``SET LOCAL`` before reading ``workspace_members``, so an
  absent membership is an empty read → refusal, fail-closed under RLS.
- The CHAT half CANNOT run as ``svc_ingress`` under the printed policies:
  ``channel_bindings`` is tenant-RLS'd with no pre-context read path, and
  the §7 door list is closed at nine. The resolver still fail-closes there
  (an RLS-filtered empty read is a refusal, never a fallback) — the tests
  pin that — and the missing sanctioned path is filed as a decision fork,
  since adding a resolver door is a plan amendment (`02` §7-DDL + a
  migration), not an engineering call.

## Shape: async, executor-first, the tier's own (#1028)

This module was written against sync DB-API cursors and had no caller in
`src/`; the router that now calls it is async, like every other module in
`src/services/target/`. It was ported in place rather than duplicated — a
second async copy of "the one central authorization gate" would be two
gates. Every function takes the caller's async executor (an `AsyncConnection`
or `AsyncSession`) and runs in the caller's transaction, so `SET LOCAL` never
outlives it. The tenant claim is applied through `unit_of_work.apply_gucs`,
the one spelling of the GUC statement, for the reason that module states.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import text

from src.exceptions.tenancy import TenantResolutionError
from src.services.target.unit_of_work import apply_gucs

#: Channel vocabulary — must match ck_bindings_channel (`02` §1).
CHAT_CHANNELS = ("telegram_group", "telegram_dm")

#: workspace_members role ladder, least to greatest (`02` §1).
ROLE_ORDER = ("member", "admin", "owner")


@dataclass(frozen=True)
class ResolvedTenant:
    """The `01` §1 triple for a CHAT resolution: ``workspace_id`` always, the
    binding that resolved it, and no user until the command's actor is
    separately identified. The web half composes `sessions.resolve` with
    :func:`authorize_member` and needs no triple."""

    workspace_id: str
    user_id: Optional[str] = None
    channel_binding_id: Optional[str] = None
    via: str = ""


async def resolve_chat(executor, channel: str, external_ref: str) -> ResolvedTenant:
    """Chat inbound → workspace, via the binding table's unique key.

    Only ``state='active'`` bindings resolve: a revoked binding refuses
    (distinct reason, because "this chat WAS bound" routes differently from
    "never seen"). An RLS-filtered empty read and a genuinely absent row are
    both refusals. Since 068 the read goes through the `fn_resolve_binding`
    door, so `svc_ingress` resolves a bound chat pre-context (#854 closed);
    an unknown chat still refuses by name.
    """
    if channel not in CHAT_CHANNELS:
        raise TenantResolutionError("unknown_channel", channel)
    # Through the `07` §14 door (#854 (a)): svc_ingress has no pre-context read
    # of channel_bindings, and the door exposes exactly the one row asked for.
    row = (
        await executor.execute(
            text(
                "SELECT o_binding_id, o_workspace_id, o_state"
                "  FROM fn_resolve_binding(:channel, :ref)"
            ),
            {"channel": channel, "ref": external_ref},
        )
    ).first()
    if row is None:
        raise TenantResolutionError("unknown_binding")
    binding_id, workspace_id, state = row
    if state != "active":
        raise TenantResolutionError("revoked_binding")
    return ResolvedTenant(
        workspace_id=str(workspace_id),
        channel_binding_id=str(binding_id),
        via="chat",
    )


async def authorize_member(
    executor, workspace_id: str, user_id: str, minimum_role: str = "member"
) -> str:
    """The one central authorization gate (`01` §1): workspace_members role
    check, in one place, never per handler.

    Sets the CLAIMED workspace as transaction-local tenant context first —
    "the door tenant context walks through": under RLS the membership row is
    visible iff the claim is the row's own workspace, so a false claim reads
    empty and refuses. Fail-closed by construction, and safe to call on a
    privileged connection too (the read is then unfiltered but the WHERE
    still binds both keys).
    """
    if minimum_role not in ROLE_ORDER:
        raise TenantResolutionError("insufficient_role", f"unknown role {minimum_role}")
    await apply_gucs(executor, tenant_id=str(workspace_id))
    row = (
        await executor.execute(
            text(
                "SELECT role FROM workspace_members"
                " WHERE workspace_id = :ws AND user_id = :u"
            ),
            {"ws": str(workspace_id), "u": str(user_id)},
        )
    ).first()
    if row is None:
        raise TenantResolutionError("not_a_member")
    role = row[0]
    if ROLE_ORDER.index(role) < ROLE_ORDER.index(minimum_role):
        raise TenantResolutionError(
            "insufficient_role", f"{role} < required {minimum_role}"
        )
    return role
