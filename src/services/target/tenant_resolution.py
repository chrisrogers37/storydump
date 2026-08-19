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

Connections are caller-supplied DB-API connections and MUST be in a
transaction block (non-autocommit): SET LOCAL is transaction-scoped, so the
gate's claim never outlives the caller's transaction — and on an autocommit
connection it would silently no-op, which the gate turns into a loud
``claim_not_set`` refusal by reading the claim back. The L.0 async unit of
work adapts this seam; nothing here touches ContextVars.

House-rule note (.claude/rules: services contain no SQL): that rule presumes
the legacy repository layer, which the target tier deliberately lacks until
L.0 builds the unit of work — this module IS the pre-L.0 seam, so it speaks
SQL directly and says so rather than growing a repository that FC-7 dooms.
``resolve_web_session`` composes ``authorize_member`` (authentication, then
authorization); already-authenticated callers enter at ``authorize_member``
directly.
"""

from dataclasses import dataclass
from typing import Optional

from src.exceptions.base import StorydumpError

#: Channel vocabulary — must match ck_bindings_channel (`02` §1).
CHAT_CHANNELS = ("telegram_group", "telegram_dm")

#: workspace_members role ladder, least to greatest (`02` §1).
ROLE_ORDER = ("member", "admin", "owner")


class TenantResolutionError(StorydumpError):
    """Inbound identity did not resolve — refused, never defaulted.

    ``reason`` is a closed vocabulary so callers can route without parsing
    prose: unknown_binding | revoked_binding | invalid_session |
    expired_session | revoked_session | not_a_member | insufficient_role |
    unknown_channel | claim_not_set | unknown_identity | user_disabled |
    workspace_suspended. A bad ``minimum_role`` argument raises ValueError —
    a caller bug is not a refusal of a request.
    """

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        super().__init__(
            f"tenant resolution refused: {reason}" + (f" — {detail}" if detail else "")
        )


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


def resolve_actor(conn, provider: str, external_id: str) -> str:
    """Inbound ACTOR identity → user_id, via ``user_identities``' unique key.

    User-plane (`02` §7: ``p_user_plane`` USING true) — readable as
    ``svc_ingress`` before any tenant context exists, so the actor half of
    the `01` §1 triple needs no door. A disabled user is refused HERE, the
    one ingress gate `02` §1 names for it."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ui.user_id, u.state FROM user_identities ui"
            " JOIN users u ON u.id = ui.user_id"
            " WHERE ui.provider = %s AND ui.external_id = %s",
            (provider, external_id),
        )
        row = cur.fetchone()
    if row is None:
        raise TenantResolutionError("unknown_identity")
    user_id, state = row
    if state != "active":
        raise TenantResolutionError("user_disabled")
    return str(user_id)


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
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT user_id, expires_at <= now(), revoked_at IS NOT NULL"
            " FROM session_tokens WHERE token_hash = %s",
            (token_hash,),
        )
        row = cur.fetchone()
    if row is None:
        raise TenantResolutionError("invalid_session")
    user_id, expired, revoked = row
    if revoked:
        raise TenantResolutionError("revoked_session")
    if expired:
        raise TenantResolutionError("expired_session")
    with conn.cursor() as cur:
        cur.execute("SELECT state FROM users WHERE id = %s", (str(user_id),))
        u = cur.fetchone()
    if u is None or u[0] != "active":
        raise TenantResolutionError("user_disabled")
    role = authorize_member(
        conn, str(claimed_workspace_id), str(user_id), minimum_role=minimum_role
    )
    return ResolvedTenant(
        workspace_id=str(claimed_workspace_id),
        user_id=str(user_id),
        via=f"session:{role}",
    )


def authorize_member(
    conn, workspace_id: str, user_id: str, minimum_role: str = "member"
) -> str:
    """The one central authorization gate (`01` §1): workspace_members role
    check, in one place, never per handler.

    Correctness truth, stated exactly (#842 review): the WHERE binding both
    keys is the mechanism that refuses a false claim — it must stay, because
    on a privileged connection (owner bypass) RLS subtracts nothing and the
    WHERE is all there is. The transaction-local claim is two other things:
    the handoff (the caller's transaction proceeds under the claimed tenant)
    and defense-in-depth under RLS (as ``svc_ingress`` the read is filtered
    TO the claim, which the positive-path test proves is load-bearing). The
    claim is READ BACK after being set and refused if absent — which also
    makes an autocommit connection (where SET LOCAL silently no-ops) a loud
    refusal instead of an unclaimed read.
    """
    if minimum_role not in ROLE_ORDER:
        raise ValueError(f"unknown minimum_role {minimum_role!r}")
    with conn.cursor() as cur:
        cur.execute("SET LOCAL app.tenant_id = %s", (str(workspace_id),))
        cur.execute("SELECT current_setting('app.tenant_id', true)")
        claimed = cur.fetchone()[0]
        if claimed != str(workspace_id):
            raise TenantResolutionError(
                "claim_not_set",
                "SET LOCAL did not take — is the connection in a transaction?",
            )
        cur.execute(
            "SELECT role FROM workspace_members"
            " WHERE workspace_id = %s AND user_id = %s",
            (str(workspace_id), str(user_id)),
        )
        row = cur.fetchone()
    if row is None:
        raise TenantResolutionError("not_a_member")
    role = row[0]
    with conn.cursor() as cur:
        cur.execute("SELECT state FROM workspaces WHERE id = %s", (str(workspace_id),))
        ws_row = cur.fetchone()
    if ws_row is not None and ws_row[0] != "active":
        # `06` §lifecycle: a suspended/offboarding workspace answers inbound
        # with a refusal, not silence. (Row invisible on a filtered read
        # cannot happen past a successful membership read — same claim.)
        raise TenantResolutionError("workspace_suspended", ws_row[0])
    if ROLE_ORDER.index(role) < ROLE_ORDER.index(minimum_role):
        raise TenantResolutionError(
            "insufficient_role", f"{role} < required {minimum_role}"
        )
    return role
