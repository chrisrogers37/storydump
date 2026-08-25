"""X.3 — the `create_workspace` command: a workspace and its owner, together
(`02` §1, `06` §1).

The second of sign-up's mints, and the only sanctioned way a tenant comes into
existence on the web surface. **Tenant minting is an explicit act at a door.**
It does not happen in auth, in middleware, or as a side effect of resolving —
so this is a command a user invokes, not an `ensure_*` helper something calls
on their behalf. That distinction is why there is no idempotent
`ensure_personal_tenant` here: an implicit get-or-create door is the shape the
legacy `chat_settings` world needed, where a Telegram chat id was both the
identity and the tenant key. A web user has no such key, and under lazy
minting "signed in with no workspace" is a legitimate resting state rather
than something to be repaired on sight.

## The pre-assigned id is a requirement, not a style choice

`p_tenant_workspaces` is `WITH CHECK (id = app.tenant_id)`. The GUC therefore
has to be set BEFORE the INSERT, which means the id has to be known before the
INSERT — so it is generated here rather than by the column default. `02` §7's
own policy comment says exactly this: *"signup pre-assigns the new id and sets
the GUC before INSERT"*.

## The ownership invariant belongs to the database, and is left there

`ct_workspaces_owner_at_insert` is a DEFERRABLE INITIALLY DEFERRED constraint
trigger: a workspace that reaches commit with no `role='owner'` member row
fails, and `uq_members_one_owner` stops a second one. Together they are
exactly-one-owner-at-every-commit. This module does not re-check that, and
deliberately does not force it early with `SET CONSTRAINTS ... IMMEDIATE`:
that is transaction-wide, so it would also fire for pending rows the CALLER
wrote, raising their violation from inside this call and attributing it here.
A cheaper wrong answer than the one it prevents.

**What that costs the caller is worth stating plainly: the invariant is
checked at YOUR commit, not at this function's return.** Both rows are written
in the caller's transaction, so a caller that never commits creates nothing,
and a caller that commits gets the check. The integration suite proves the
trigger is live rather than assuming it — a workspace inserted with no owner
row raises at commit, and that positive control is what makes "the database is
doing your invariant for you" evidence instead of a claim.

## Two GUCs beyond the tenant, both required by triggers rather than by taste

`workspaces` and `workspace_members` carry `trg_governance_audit`, which
RAISES on a NULL `app.actor_kind` — anonymous governance writes are forbidden
— and then INSERTs an `audit_events` row keyed on the workspace. That audit
insert is itself under `p_audit_ins`, whose `WITH CHECK` is the same
`app.tenant_id` this door already set, so the audit trail lands inside the new
tenant's own scope with nothing extra to arrange. All four names come from
`sync_tx.guc_pairs`, which is the one place the vocabulary lives.
"""

import uuid
from dataclasses import dataclass
from typing import Optional

from src.exceptions.tenancy import TenantProvisioningError
from src.services.target.sync_tx import apply_gucs, require_transaction

#: `02` §1 `ck_members_role`, owner rung. The creator is always the owner —
#: `ck_invite_role` forbids inviting one, so ownership can only originate here
#: or move by an audited transfer.
OWNER_ROLE = "owner"

#: `audit_events.ck_audit_actor` / `ck_audit_channel`. A person on the web
#: surface; this door is not reachable by the clock, a reaper or a migration.
ACTOR_KIND = "user"
CHANNEL = "web"


@dataclass(frozen=True)
class ProvisionedWorkspace:
    """The new tenant. ``workspace_id`` is the tenant id everywhere inland —
    `tenant_id == workspaces.id`.

    Returning it is not a convenience: under the printed policies a caller
    cannot enumerate its own memberships (see
    `tenant_resolution.workspaces_for_user`), so this value is the only handle
    on the workspace that has just been created.
    """

    workspace_id: str


def create_workspace(
    conn, *, name: str, owner_user_id: str, tz: Optional[str] = None
) -> ProvisionedWorkspace:
    """Create *name* owned by *owner_user_id*, in the caller's transaction.

    *tz* is an IANA name. It is validated by the database (`ck_ws_tz_valid`
    calls `fn_safe_tz`), not here — a second copy of that vocabulary would
    drift from the one the schema enforces, and the failure mode of the copy
    being stale is a workspace the app accepts and the database refuses.

    Raises `TenantProvisioningError` on a missing or malformed input; anything
    the database refuses (an unknown owner, an invalid timezone) surfaces as
    the driver's own integrity error, because those are the schema's own
    vocabulary and restating them here would be a third place to keep them.
    """
    require_transaction(conn)
    if not name or not name.strip():
        raise TenantProvisioningError("invalid_name", "workspace name is required")
    if not owner_user_id or not str(owner_user_id).strip():
        raise TenantProvisioningError("missing_owner", "an owner is required")

    workspace_id = str(uuid.uuid4())
    owner = str(owner_user_id)
    with conn.cursor() as cur:
        # Tenant context first: the WITH CHECK on the very next statement
        # reads it, and the audit trigger's own insert rides the same value.
        apply_gucs(
            cur,
            tenant_id=workspace_id,
            actor_kind=ACTOR_KIND,
            actor_user_id=owner,
            channel=CHANNEL,
        )
        if tz is None:
            # Two spellings rather than one with a default copied into app
            # code: NULL is not a legal value for the column, and hardcoding
            # 'UTC' here would be a second home for a default the schema owns.
            cur.execute(
                "INSERT INTO workspaces (id, name) VALUES (%s, %s)",
                (workspace_id, name.strip()),
            )
        else:
            cur.execute(
                "INSERT INTO workspaces (id, name, tz) VALUES (%s, %s, %s)",
                (workspace_id, name.strip(), tz),
            )
        cur.execute(
            "INSERT INTO workspace_members (workspace_id, user_id, role)"
            " VALUES (%s, %s, %s)",
            (workspace_id, owner, OWNER_ROLE),
        )
    return ProvisionedWorkspace(workspace_id=workspace_id)
