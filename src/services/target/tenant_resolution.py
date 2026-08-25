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

- The WEB half runs as ``svc_ingress`` today: ``session_tokens`` is
  auth-plane ("the door tenant context walks through" — readable before any
  ``app.tenant_id`` exists), and the membership gate sets its claimed tenant
  with ``SET LOCAL`` before reading ``workspace_members``, so an absent
  membership is an empty read → refusal, fail-closed under RLS.
- The CHAT half CANNOT run as ``svc_ingress`` under the printed policies:
  ``channel_bindings`` is tenant-RLS'd with no pre-context read path, and
  the §7 door list is closed at nine. The resolver still fail-closes there
  (an RLS-filtered empty read is a refusal, never a fallback) — the tests
  pin that — and the missing sanctioned path is filed as a decision fork,
  since adding a resolver door is a plan amendment (`02` §7-DDL + a
  migration), not an engineering call.

Connections are caller-supplied DB-API connections; SET LOCAL is
transaction-scoped, so the gate's claim never outlives the caller's
transaction. The L.0 async unit of work adapts this seam; nothing here
touches ContextVars.
"""

from dataclasses import dataclass
from typing import Optional

from src.exceptions.tenancy import TenantResolutionError
from src.services.target.web_sessions import authenticate_session

#: Channel vocabulary — must match ck_bindings_channel (`02` §1).
CHAT_CHANNELS = ("telegram_group", "telegram_dm")

#: workspace_members role ladder, least to greatest (`02` §1).
ROLE_ORDER = ("member", "admin", "owner")


@dataclass(frozen=True)
class ResolvedTenant:
    """The `01` §1 triple. ``workspace_id`` is always present; the other two
    depend on the inbound kind (a chat resolution has no user until the
    command's actor is separately identified; a web resolution has no
    binding)."""

    workspace_id: str
    user_id: Optional[str] = None
    channel_binding_id: Optional[str] = None
    via: str = ""


@dataclass(frozen=True)
class Membership:
    """One row of a user's membership list. A typed pair rather than a bare
    tuple, matching every other result this tier returns."""

    workspace_id: str
    role: str


def resolve_chat(conn, channel: str, external_ref: str) -> ResolvedTenant:
    """Chat inbound → workspace, via the binding table's unique key.

    Only ``state='active'`` bindings resolve: a revoked binding refuses
    (distinct reason, because "this chat WAS bound" routes differently from
    "never seen"). An RLS-filtered empty read and a genuinely absent row are
    both refusals — under-privileged execution fails CLOSED here, which the
    harness pins as a measured fact rather than a hope.
    """
    if channel not in CHAT_CHANNELS:
        raise TenantResolutionError("unknown_channel", channel)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, workspace_id, state FROM channel_bindings"
            " WHERE channel = %s AND external_ref = %s",
            (channel, external_ref),
        )
        row = cur.fetchone()
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


def resolve_web_session(
    conn, token_hash: str, claimed_workspace_id: str, minimum_role: str = "member"
) -> ResolvedTenant:
    """Web inbound → workspace: authenticate the session, then authorize the
    CLAIMED workspace through the central gate.

    A web request names its workspace explicitly (a session is user-scoped
    and a user may belong to many workspaces) — the claim is validated,
    never trusted: membership decides, under the claimed tenant's own RLS
    context.

    The authentication half is `web_sessions.authenticate_session`, not a
    second copy of the same three-column read. It is the same seam from the
    other side: that module mints the row, so it owns what the row proves,
    and a user-plane surface that needs the user WITHOUT claiming a workspace
    (a freshly signed-in user has none to claim) calls it directly.
    """
    session = authenticate_session(conn, token_hash)
    role = authorize_member(
        conn, str(claimed_workspace_id), session.user_id, minimum_role=minimum_role
    )
    return ResolvedTenant(
        workspace_id=str(claimed_workspace_id),
        user_id=session.user_id,
        via=f"session:{role}",
    )


def authorize_member(
    conn, workspace_id: str, user_id: str, minimum_role: str = "member"
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
    with conn.cursor() as cur:
        cur.execute("SET LOCAL app.tenant_id = %s", (str(workspace_id),))
        cur.execute(
            "SELECT role FROM workspace_members"
            " WHERE workspace_id = %s AND user_id = %s",
            (str(workspace_id), str(user_id)),
        )
        row = cur.fetchone()
    if row is None:
        raise TenantResolutionError("not_a_member")
    role = row[0]
    if ROLE_ORDER.index(role) < ROLE_ORDER.index(minimum_role):
        raise TenantResolutionError(
            "insufficient_role", f"{role} < required {minimum_role}"
        )
    return role


def workspaces_for_user(conn, user_id: str) -> list:
    """Every workspace *user_id* belongs to, as a list of `Membership`.

    THE READ THE WEB SURFACE NEEDS AND THE PRINTED POLICIES DO NOT SERVE, so
    it refuses rather than answering wrongly. `p_tenant` on
    ``workspace_members`` is ``workspace_id = app.tenant_id``: with no tenant
    set the table reads empty, and with one set it reads exactly one row. Both
    are *filtered* answers to an *unfiltered* question, and the first is the
    dangerous one — an empty list is indistinguishable from the greenfield's
    normal "signed in, no workspace yet" state, so a user who owns three
    workspaces would be routed to first-run onboarding and told to create
    their first. A fail-open that looks like correct behaviour.

    So the blindness is DETECTED rather than reasoned about:
    ``row_security_active('workspace_members')`` answers, for this connection's
    own role, whether the policy applies. If it does, this refuses with
    ``membership_list_unreadable``; the caller can then say "we cannot list
    your workspaces" instead of "you have none". It answers truthfully only on
    a connection RLS does not filter — today an owner/admin connection, and in
    future a sanctioned door.

    **The missing door is a plan amendment, not an engineering call**, and it
    is the WEB twin of the chat-half gap this module's header already files:
    the `02` §7 door list is closed at nine (verified against production), and
    none of the nine enumerates a user's memberships. Adding a tenth, or
    widening `p_tenant` with a user-plane branch, is a `02` §7-DDL change plus
    a migration. This function exists now so the gap has one address and one
    typed refusal rather than nine call sites each discovering it.
    """
    with conn.cursor() as cur:
        # `row_security_active` answers for THIS connection's own role — false
        # for an owner, a superuser or BYPASSRLS — which is exactly the
        # question. The general property, if a second such read ever appears:
        # a read whose empty result is a legitimate answer cannot infer RLS
        # filtering from emptiness, so it has to detect it. One instance is
        # not yet a helper.
        cur.execute("SELECT row_security_active('workspace_members')")
        (filtered,) = cur.fetchone()
        if filtered:
            raise TenantResolutionError(
                "membership_list_unreadable",
                "workspace_members is RLS-filtered for this role, so a list"
                " read here would be silently partial — no sanctioned door"
                " enumerates a user's memberships (02 §7 closes at nine)",
            )
        cur.execute(
            "SELECT workspace_id, role FROM workspace_members"
            " WHERE user_id = %s ORDER BY created_at, workspace_id",
            (str(user_id),),
        )
        return [Membership(str(ws), role) for ws, role in cur.fetchall()]
