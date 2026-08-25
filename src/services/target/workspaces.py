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
- **Every read is workspace-keyed except one, and that one is disclosed.**
  :func:`list_for_user` reads `workspace_members` by user, across tenants.
  Under the printed `058` policies `svc_ingress` has no pre-context read of
  that table, so as ingress it returns NOTHING — the same class as the chat
  resolver's missing door (`tenant_resolution`), on the user-plane side. It
  is written as the plan reads (`/me` lists memberships) and the gap is
  measured by the test, not papered over here; the sanctioned fix is a door
  or a user-plane policy on `workspace_members`, which is a plan amendment.

Settings writes go through :func:`change_settings`, whose allowlist is the
typed-column list of `02` §1's materialization contract — no JSONB, no
free-form keys — and whose validation is a floor: the database's CHECKs
(`ck_ws_posts_per_day`, `ck_ws_tz_valid`, …) remain the authority, and a
`check_violation` surfaces as `invalid_args` rather than a 500.
"""

from __future__ import annotations

import uuid
from typing import Any, Mapping, Optional

from sqlalchemy import text

from src.exceptions.base import StorydumpError
from src.services.target.unit_of_work import apply_gucs

#: `workspaces.name` is VARCHAR(100).
NAME_MAX = 100

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
    """A caller-supplied value the boundary refuses before the database sees it."""


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

    Pre-assigns the id (or takes the caller's — the unit of work is
    unconstructible without a tenant, so an adapter that opened one for the
    new workspace already holds the id; it is never client-supplied), claims
    it as the transaction's tenant with the actor GUCs the audit triggers
    require, then inserts the workspace and its owner row — all inside the
    CALLER's transaction, which is what makes the deferred owner constraint
    pass at commit.
    """
    name = _clean_name(name)
    ws_id = str(workspace_id) if workspace_id else str(uuid.uuid4())
    await apply_gucs(
        executor,
        tenant_id=ws_id,
        actor_kind="user",
        actor_user_id=str(owner_user_id),
        channel=channel,
    )
    if tz is None:
        await executor.execute(
            text("INSERT INTO workspaces (id, name) VALUES (:id, :name)"),
            {"id": ws_id, "name": name},
        )
    else:
        await executor.execute(
            text("INSERT INTO workspaces (id, name, tz) VALUES (:id, :name, :tz)"),
            {"id": ws_id, "name": name, "tz": tz},
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
    nature (see the module docstring for what that costs under RLS)."""
    rows = await executor.execute(
        text(
            "SELECT w.id, w.name, w.state, m.role"
            "  FROM workspace_members m JOIN workspaces w ON w.id = m.workspace_id"
            " WHERE m.user_id = :u"
            " ORDER BY w.created_at, w.id"
        ),
        {"u": str(user_id)},
    )
    return [dict(r) for r in rows.mappings()]


async def get_workspace(executor, *, workspace_id: str) -> Optional[dict]:
    """The config row (`02` §1's typed columns) plus state, or None."""
    row = (
        (
            await executor.execute(
                text(f"SELECT {_CONFIG_COLUMNS} FROM workspaces WHERE id = :ws"),
                {"ws": str(workspace_id)},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def list_members(executor, *, workspace_id: str) -> list[dict]:
    rows = await executor.execute(
        text(
            "SELECT m.user_id, m.role, m.added_by_user_id, m.created_at,"
            "       u.primary_email, u.state AS user_state"
            "  FROM workspace_members m JOIN users u ON u.id = m.user_id"
            " WHERE m.workspace_id = :ws"
            " ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END,"
            "          m.created_at"
        ),
        {"ws": str(workspace_id)},
    )
    return [dict(r) for r in rows.mappings()]


async def list_accounts(executor, *, workspace_id: str) -> list[dict]:
    rows = await executor.execute(
        text(
            "SELECT id, provider_account_ref, handle, display_name, state,"
            "       posts_per_day, posting_hours_start, posting_hours_end, tz,"
            "       next_slot_at, last_posted_at, created_at"
            "  FROM ig_accounts WHERE workspace_id = :ws ORDER BY created_at, id"
        ),
        {"ws": str(workspace_id)},
    )
    return [dict(r) for r in rows.mappings()]


async def list_sources(executor, *, workspace_id: str) -> list[dict]:
    rows = await executor.execute(
        text(
            "SELECT id, provider, state, next_sync_at, last_sync_success_at,"
            "       alerted_at, created_at"
            "  FROM media_sources WHERE workspace_id = :ws ORDER BY created_at, id"
        ),
        {"ws": str(workspace_id)},
    )
    return [dict(r) for r in rows.mappings()]


async def list_bindings(executor, *, workspace_id: str) -> list[dict]:
    rows = await executor.execute(
        text(
            "SELECT id, channel, external_ref, state, settings, created_at"
            "  FROM channel_bindings WHERE workspace_id = :ws ORDER BY created_at, id"
        ),
        {"ws": str(workspace_id)},
    )
    return [dict(r) for r in rows.mappings()]


async def list_invitations(executor, *, workspace_id: str) -> list[dict]:
    """Pending invitations only. The token is never read back — only its hash
    is stored, and the row exposes nothing a caller could present."""
    rows = await executor.execute(
        text(
            "SELECT id, delivery_channel, email, role, state, expires_at,"
            "       invited_by_user_id, created_at"
            "  FROM workspace_invitations"
            " WHERE workspace_id = :ws AND state = 'pending' AND expires_at > now()"
            " ORDER BY created_at, id"
        ),
        {"ws": str(workspace_id)},
    )
    return [dict(r) for r in rows.mappings()]


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
    rows = await executor.execute(
        text(
            f"SELECT {_INTENT_COLUMNS}"
            "  FROM post_intents i"
            "  JOIN media_items m ON m.workspace_id = i.workspace_id AND m.id = i.media_item_id"
            f" WHERE {where}"
            " ORDER BY i.schedule_slot_at, i.id LIMIT :lim"
        ),
        params,
    )
    return [dict(r) for r in rows.mappings()]


async def get_intent(executor, *, workspace_id: str, intent_id: str) -> Optional[dict]:
    """One intent — always its CURRENT state (R6: terminal-state-first)."""
    row = (
        (
            await executor.execute(
                text(
                    f"SELECT {_INTENT_COLUMNS}"
                    "  FROM post_intents i"
                    "  JOIN media_items m ON m.workspace_id = i.workspace_id"
                    "   AND m.id = i.media_item_id"
                    " WHERE i.workspace_id = :ws AND i.id = :id"
                ),
                {"ws": str(workspace_id), "id": str(intent_id)},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def rename(executor, *, workspace_id: str, name: str) -> bool:
    name = _clean_name(name)
    result = await executor.execute(
        text("UPDATE workspaces SET name = :name WHERE id = :ws"),
        {"name": name, "ws": str(workspace_id)},
    )
    return result.rowcount == 1


async def set_paused(
    executor, *, workspace_id: str, paused: bool, by_user_id: str
) -> bool:
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
    result = await executor.execute(
        text(stmt), {"u": str(by_user_id), "ws": str(workspace_id)}
    )
    return result.rowcount == 1


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
) -> bool:
    """Apply a validated settings map. Column names come from the allowlist
    (never from the caller's string), values are bound parameters."""
    cleaned = validate_settings(changes)
    assignments = ", ".join(f"{k} = :{k}" for k in cleaned)
    result = await executor.execute(
        text(f"UPDATE workspaces SET {assignments} WHERE id = :ws"),
        {**cleaned, "ws": str(workspace_id)},
    )
    return result.rowcount == 1
