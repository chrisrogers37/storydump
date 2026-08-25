"""Workspaces — the tenant root's writers and the web surface's read models.

`02` §1 is the schema's normative home; this module is the tier's conn-first
raw-SQL access to it. Two rules decide its shape:

- **Creation is one transaction, and the tenant claim precedes the row.**
  `ct_workspaces_owner_at_insert` is a deferred constraint trigger, so a
  workspace and its owner `workspace_members` row must both exist by COMMIT —
  a two-transaction create fails at commit, not at the insert, which is a
  confusing error to debug. And `p_tenant_workspaces` (`058`) keys on the
  row's own id, so signup pre-assigns the id and sets `app.tenant_id` to it
  BEFORE the insert (`02` §7: "signup pre-assigns the new id and sets the GUC
  before INSERT"). :func:`create_workspace` does both, in that order.
- **Every read is workspace-keyed except one, and that one REFUSES rather
  than answering wrongly.** :func:`list_for_user` reads `workspace_members`
  by user, across tenants. Under the printed `058` policies `svc_ingress`
  has no pre-context read of that table, so as ingress the table reads EMPTY
  — and an empty list is indistinguishable from the greenfield's normal
  "signed in, no workspace yet" state, so a user who owns three workspaces
  would be routed to first-run onboarding (alex's finding, #1031, adopted
  here in the async lane). The blindness is DETECTED, not reasoned about:
  `row_security_active('workspace_members')` answers for this connection's
  own role, and if the policy applies the read refuses with
  `membership_list_unreadable`. The sanctioned fix is the `02` §7 door
  proposed on #1015 (`fn_memberships_for_caller`); the day it lands, this
  reads through it and the refusal goes.

Settings writes go through :func:`change_settings`, whose allowlist is the
typed-column list of `02` §1's materialization contract — no JSONB, no
free-form keys — and whose validation is a floor: the database's CHECKs
(`ck_ws_posts_per_day`, `ck_ws_tz_valid`, …) remain the authority, and a
`check_violation` from any writer here surfaces as `InvalidWorkspaceArgs`
(translated once, in :func:`_write`, through the tier's one unwrap —
`_dbapi.driver_candidates`), which the port maps to `invalid_args`.
"""

from __future__ import annotations

import uuid
from typing import Any, Mapping, Optional

from asyncpg.exceptions import CheckViolationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.exceptions.base import StorydumpError
from src.exceptions.tenancy import TenantResolutionError
from src.services.target import readers
from src.services.target._dbapi import driver_candidates
from src.services.target.unit_of_work import apply_gucs

#: `workspaces.name` is VARCHAR(100).
NAME_MAX = 100

#: INTERIM, ratified by Chris (#1033): a new workspace starts in
#: `approval_mode = 'auto'` so the core loop closes without an approvals
#: surface — the column's own default is 'manual' (`053:136`), and a
#: Google-only workspace has no channel to approve on until the web approvals
#: surface lands. #1033 records that this must be REVERTED when it does; the
#: X.2 gate pins the value so the revert is a deliberate test flip. A
#: workspace can still be switched to 'manual' per `settings_change`.
INTERIM_APPROVAL_MODE = "auto"

#: The typed product-configuration columns (`02` §1) a `settings_change` may
#: touch, with the Python type each accepts. The database CHECKs bound the
#: values; this table bounds the KEYS.
SETTINGS_COLUMNS: dict[str, type] = {
    "tz": str,
    "posts_per_day": int,
    "posting_hours_start": int,
    "posting_hours_end": int,
    "approval_mode": str,
    "auto_reapprove_returning": bool,
    "approval_ttl_minutes": int,
    "dry_run_mode": bool,
    "repost_ttl_days": int,
    "skip_ttl_days": int,
    "caption_style": str,
    "enable_ai_captions": bool,
    "api_publishing_enabled": bool,
}

#: Columns a settings_change may set to NULL (= inherit the app default per
#: the materialization contract). The NOT NULL ones refuse NULL at the DB.
NULLABLE_SETTINGS = frozenset(
    {"approval_ttl_minutes", "repost_ttl_days", "skip_ttl_days", "caption_style"}
)

_CONFIG_COLUMNS = (
    "id, name, state, tz, posts_per_day, posting_hours_start, posting_hours_end,"
    " approval_mode, auto_reapprove_returning, approval_ttl_minutes, dry_run_mode,"
    " is_paused, paused_at, repost_ttl_days, skip_ttl_days, caption_style,"
    " enable_ai_captions, api_publishing_enabled, offboarding_at, created_at, updated_at"
)


class InvalidWorkspaceArgs(StorydumpError):
    """A caller-supplied value the boundary refuses — before the database
    sees it, or because the database's CHECK refused it."""


async def _write(executor, sql: str, **params) -> None:
    """One writer: a `check_violation` is the caller's value being wrong."""
    try:
        await executor.execute(text(sql), params)
    except DBAPIError as exc:
        for cause in driver_candidates(exc):
            if isinstance(cause, CheckViolationError):
                name = getattr(cause, "constraint_name", None) or "check"
                raise InvalidWorkspaceArgs(f"invalid value ({name})") from exc
        raise


def _clean_name(name: Any) -> str:
    if not isinstance(name, str) or not name.strip():
        raise InvalidWorkspaceArgs("name is required")
    name = name.strip()
    if len(name) > NAME_MAX:
        raise InvalidWorkspaceArgs(f"name exceeds {NAME_MAX} characters")
    return name


async def create_workspace(
    executor,
    *,
    owner_user_id: str,
    name: str,
    tz: Optional[str] = None,
    channel: str = "web",
    workspace_id: Optional[str] = None,
) -> str:
    """Create a workspace owned by *owner_user_id*. Returns the new id.

    A caller that passes *workspace_id* has already claimed it: the unit of
    work is unconstructible without a tenant, so an adapter that opened one
    for the new workspace holds the id and set the GUCs (it is never
    client-supplied). Only when THIS function assigns the id does it claim
    it — with the actor GUCs the audit triggers require — before the insert.
    Workspace and owner row land in the CALLER's transaction, which is what
    makes the deferred owner constraint pass at commit.
    """
    name = _clean_name(name)
    if workspace_id:
        ws_id = str(workspace_id)
    else:
        ws_id = str(uuid.uuid4())
        await apply_gucs(
            executor,
            tenant_id=ws_id,
            actor_kind="user",
            actor_user_id=str(owner_user_id),
            channel=channel,
        )
    await _write(
        executor,
        "INSERT INTO workspaces (id, name, tz, approval_mode)"
        " VALUES (:id, :name, COALESCE(:tz, 'UTC'), :mode)",
        id=ws_id,
        name=name,
        tz=tz,
        mode=INTERIM_APPROVAL_MODE,
    )
    await executor.execute(
        text(
            "INSERT INTO workspace_members (workspace_id, user_id, role)"
            " VALUES (:ws, :u, 'owner')"
        ),
        {"ws": ws_id, "u": str(owner_user_id)},
    )
    return ws_id


async def list_for_user(executor, *, user_id: str) -> list[dict]:
    """The caller's memberships: `[{id, name, state, role}]`. Cross-tenant by
    nature; refuses by name on a connection whose RLS would make the answer
    silently partial (module docstring)."""
    filtered = (
        await executor.execute(text("SELECT row_security_active('workspace_members')"))
    ).scalar()
    if filtered:
        raise TenantResolutionError(
            "membership_list_unreadable",
            "workspace_members is RLS-filtered for this role, so a list read"
            " here would be silently partial — no sanctioned door enumerates a"
            " user's memberships yet (02 §7; the door is proposed on #1015)",
        )
    return await readers.rows(
        executor,
        "SELECT w.id, w.name, w.state, m.role"
        "  FROM workspace_members m JOIN workspaces w ON w.id = m.workspace_id"
        " WHERE m.user_id = :u"
        " ORDER BY w.created_at, w.id",
        u=str(user_id),
    )


async def get_workspace(executor, *, workspace_id: str) -> Optional[dict]:
    """The config row (`02` §1's typed columns) plus state, or None."""
    return await readers.row(
        executor,
        f"SELECT {_CONFIG_COLUMNS} FROM workspaces WHERE id = :ws",
        ws=str(workspace_id),
    )


async def list_members(executor, *, workspace_id: str) -> list[dict]:
    return await readers.rows(
        executor,
        "SELECT m.user_id, m.role, m.added_by_user_id, m.created_at,"
        "       u.primary_email, u.state AS user_state"
        "  FROM workspace_members m JOIN users u ON u.id = m.user_id"
        " WHERE m.workspace_id = :ws"
        " ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END,"
        "          m.created_at",
        ws=str(workspace_id),
    )


async def list_accounts(executor, *, workspace_id: str) -> list[dict]:
    return await readers.rows(
        executor,
        "SELECT id, provider_account_ref, handle, display_name, state,"
        "       posts_per_day, posting_hours_start, posting_hours_end, tz,"
        "       next_slot_at, last_posted_at, created_at"
        "  FROM ig_accounts WHERE workspace_id = :ws ORDER BY created_at, id",
        ws=str(workspace_id),
    )


async def list_sources(executor, *, workspace_id: str) -> list[dict]:
    return await readers.rows(
        executor,
        "SELECT id, provider, state, next_sync_at, last_sync_success_at,"
        "       alerted_at, created_at"
        "  FROM media_sources WHERE workspace_id = :ws ORDER BY created_at, id",
        ws=str(workspace_id),
    )


async def list_bindings(executor, *, workspace_id: str) -> list[dict]:
    return await readers.rows(
        executor,
        "SELECT id, channel, external_ref, state, settings, created_at"
        "  FROM channel_bindings WHERE workspace_id = :ws ORDER BY created_at, id",
        ws=str(workspace_id),
    )


async def list_invitations(executor, *, workspace_id: str) -> list[dict]:
    """Pending invitations only. The token is never read back — only its hash
    is stored, and the row exposes nothing a caller could present."""
    return await readers.rows(
        executor,
        "SELECT id, delivery_channel, email, role, state, expires_at,"
        "       invited_by_user_id, created_at"
        "  FROM workspace_invitations"
        " WHERE workspace_id = :ws AND state = 'pending' AND expires_at > now()"
        " ORDER BY created_at, id",
        ws=str(workspace_id),
    )


_INTENT_COLUMNS = (
    "i.id, i.state, i.ig_account_id, i.media_item_id, i.schedule_slot_at,"
    " i.approval_mode, i.published_via, i.publish_step, i.cancel_requested,"
    " i.ig_permalink, i.entered_state_at, i.created_at,"
    " m.file_name, m.media_kind, m.thumbnail_url, m.caption, m.category"
)


async def list_intents(
    executor, *, workspace_id: str, state: Optional[str] = None, limit: int = 50
) -> list[dict]:
    """The ledger read model (X.2: "reads pending approvals from the ledger").
    Bounded (`01` H5) — *limit* is applied after the caller's clamp."""
    params: dict[str, Any] = {"ws": str(workspace_id), "lim": int(limit)}
    where = "i.workspace_id = :ws"
    if state is not None:
        where += " AND i.state = :state"
        params["state"] = state
    return await readers.rows(
        executor,
        f"SELECT {_INTENT_COLUMNS}"
        "  FROM post_intents i"
        "  JOIN media_items m ON m.workspace_id = i.workspace_id AND m.id = i.media_item_id"
        f" WHERE {where}"
        " ORDER BY i.schedule_slot_at, i.id LIMIT :lim",
        **params,
    )


async def get_intent(executor, *, workspace_id: str, intent_id: str) -> Optional[dict]:
    """One intent — always its CURRENT state (R6: terminal-state-first)."""
    return await readers.row(
        executor,
        f"SELECT {_INTENT_COLUMNS}"
        "  FROM post_intents i"
        "  JOIN media_items m ON m.workspace_id = i.workspace_id"
        "   AND m.id = i.media_item_id"
        " WHERE i.workspace_id = :ws AND i.id = :id",
        ws=str(workspace_id),
        id=str(intent_id),
    )


async def rename(executor, *, workspace_id: str, name: str) -> str:
    """Returns the cleaned name that was written."""
    name = _clean_name(name)
    await _write(
        executor,
        "UPDATE workspaces SET name = :name WHERE id = :ws",
        name=name,
        ws=str(workspace_id),
    )
    return name


async def set_paused(
    executor, *, workspace_id: str, paused: bool, by_user_id: str
) -> None:
    """`pause_workspace` / `resume_workspace`: the three paused columns move
    together, and resume clears both attribution columns."""
    if paused:
        stmt = (
            "UPDATE workspaces SET is_paused = true, paused_at = now(),"
            " paused_by_user_id = :u WHERE id = :ws"
        )
    else:
        stmt = (
            "UPDATE workspaces SET is_paused = false, paused_at = NULL,"
            " paused_by_user_id = NULL WHERE id = :ws"
        )
    await executor.execute(text(stmt), {"u": str(by_user_id), "ws": str(workspace_id)})


def validate_settings(changes: Mapping[str, Any]) -> dict[str, Any]:
    """Keys and Python types only — the DB CHECKs decide the values.

    `bool` is refused for int columns explicitly, because `True` IS an int in
    Python and would otherwise slip through as `posts_per_day = 1`.
    """
    if not changes:
        raise InvalidWorkspaceArgs("no settings supplied")
    cleaned: dict[str, Any] = {}
    for key, value in changes.items():
        if key not in SETTINGS_COLUMNS:
            raise InvalidWorkspaceArgs(f"unknown setting {key!r}")
        expected = SETTINGS_COLUMNS[key]
        if value is None:
            if key not in NULLABLE_SETTINGS:
                raise InvalidWorkspaceArgs(f"{key} cannot be null")
        elif expected is int and (
            isinstance(value, bool) or not isinstance(value, int)
        ):
            raise InvalidWorkspaceArgs(f"{key} must be an integer")
        elif not isinstance(value, expected):
            raise InvalidWorkspaceArgs(f"{key} must be {expected.__name__}")
        cleaned[key] = value
    return cleaned


async def change_settings(
    executor, *, workspace_id: str, changes: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate and apply a settings map; returns the cleaned map that was
    written. Column names come from the allowlist (never from the caller's
    string), values are bound parameters, and the CHECKs decide the values."""
    cleaned = validate_settings(changes)
    assignments = ", ".join(f"{k} = :{k}" for k in cleaned)
    await _write(
        executor,
        f"UPDATE workspaces SET {assignments} WHERE id = :ws",
        **cleaned,
        ws=str(workspace_id),
    )
    return cleaned
