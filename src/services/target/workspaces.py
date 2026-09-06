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
- **Every read is workspace-keyed except one, and that one goes through a
  door.** :func:`list_for_user` answers "which workspaces am I in" — a
  cross-tenant question `p_tenant` on `workspace_members` cannot serve (with
  no tenant claimed the table reads EMPTY, which is indistinguishable from
  the greenfield's normal signed-in-with-no-workspace state). It reads
  through `fn_memberships_for_caller()` (`064`, #1037), the tenth `02` §7
  door, after claiming the caller in `app.actor_user_id` — the same GUC the
  audit triggers already trust, so nothing new is asserted; the door reads
  it internally and an unset value fails closed to no rows.

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
from typing import Any, Mapping, Optional, Sequence

from asyncpg.exceptions import CheckViolationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.exceptions.base import StorydumpError
from src.services.target import offboarding, readers
from src.services.target._dbapi import driver_candidates
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

#: The per-account schedule overrides an `account_settings_change` may touch —
#: `054`'s "per-account schedule overrides; NULL = inherit the workspace
#: column" — with the Python type each accepts. Deliberately NOT the account's
#: whole row, and each exclusion is a different kind of thing:
#:
#:   - `state` is a governed transition (`ck_ig_accounts_state`, the reauth and
#:     `moved` flows), not a setting. A settings write that could clear
#:     `reauth_required` would retire a credential prompt without a credential.
#:   - `handle` / `display_name` are identity. `provider_account_ref` is derived
#:     from the handle at creation (`provisioning.manual_ref_for`) and
#:     `uq_ig_account_live` keys on the ref, so editing the handle here would
#:     leave the two spellings of one account disagreeing.
#:   - `next_slot_at` / `last_posted_at` are the clock's machinery — `055`'s
#:     audit trigger names exactly those two as the columns that advance
#:     without auditing.
ACCOUNT_SETTINGS_COLUMNS: dict[str, type] = {
    "tz": str,
    "posts_per_day": int,
    "posting_hours_start": int,
    "posting_hours_end": int,
}

#: Every account override is nullable, and NULL is not "unset" — it is the
#: INHERIT arm of the ladder the clock resolves per tick (`059`'s due-scan:
#: `COALESCE(a.posts_per_day, w.posts_per_day)`). A command that could set an
#: override but never clear one would be a one-way door: an account could leave
#: the workspace default and have no way back to it.
ACCOUNT_NULLABLE_SETTINGS = frozenset(ACCOUNT_SETTINGS_COLUMNS)

_CONFIG_COLUMNS = (
    "id, name, state, tz, posts_per_day, posting_hours_start, posting_hours_end,"
    " approval_mode, auto_reapprove_returning, approval_ttl_minutes, dry_run_mode,"
    " is_paused, paused_at, repost_ttl_days, skip_ttl_days, caption_style,"
    " enable_ai_captions, api_publishing_enabled, offboarding_at, created_at, updated_at"
)


class InvalidWorkspaceArgs(StorydumpError):
    """A caller-supplied value the boundary refuses — before the database
    sees it, or because the database's CHECK refused it."""


async def _write(executor, sql: str, **params):
    """One writer: a `check_violation` is the caller's value being wrong.

    Returns the driver result, so a writer whose WHERE can match nothing reads
    `rowcount` rather than issuing a second statement to find out. Callers that
    cannot miss ignore it."""
    try:
        return await executor.execute(text(sql), params)
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
        "INSERT INTO workspaces (id, name, tz) VALUES (:id, :name, COALESCE(:tz, 'UTC'))",
        id=ws_id,
        name=name,
        tz=tz,
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
    """The caller's memberships: `[{id, name, state, role}]`, through the
    `fn_memberships_for_caller()` door (module docstring). *user_id* is the
    authenticated principal's and is claimed as `app.actor_user_id` for this
    transaction; the door never takes it as an argument."""
    await executor.execute(
        text("SELECT set_config('app.actor_user_id', :u, true)"), {"u": str(user_id)}
    )
    return await readers.rows(
        executor,
        "SELECT o_workspace_id AS id, o_name AS name, o_state AS state, o_role AS role"
        "  FROM fn_memberships_for_caller()",
    )


async def get_workspace(executor, *, workspace_id: str) -> Optional[dict]:
    """The config row (`02` §1's typed columns) plus state, or None.

    Carries `restorable_until` — when an offboarding workspace can last be
    restored — computed here from the one grace constant so the dashboard
    never derives it from a copied number (#1127)."""
    row = await readers.row(
        executor,
        f"SELECT {_CONFIG_COLUMNS} FROM workspaces WHERE id = :ws",
        ws=str(workspace_id),
    )
    if row is not None:
        row["restorable_until"] = offboarding.restorable_until(
            row.get("offboarding_at")
        )
    return row


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


#: A credential's usability as a projection — `none` (never connected) ·
#: `active` · `expired` · `revoked` — over the LEFT-JOINed row aliased `c`.
#: One definition for destinations and sources: the rule it mirrors is the one
#: `drive_credentials` / the refresh leg ENFORCE (usable iff `state = 'active'`
#: and `expires_at` has not passed), and two copies of it could drift apart.
#: The Instagram credential provider, as `ig_login_oauth.PROVIDER` spells it.
#: A local name rather than an import: that module's import graph must not
#: grow a dependency on this one.
IG_LOGIN_PROVIDER = "ig_login"

_CREDENTIAL_STATUS_SQL = (
    "CASE"
    "  WHEN c.id IS NULL THEN 'none'"
    "  WHEN c.state <> 'active' THEN c.state"
    "  WHEN c.expires_at IS NOT NULL AND c.expires_at <= now() THEN 'expired'"
    "  ELSE 'active'"
    " END AS credential_status"
)


async def list_accounts(executor, *, workspace_id: str) -> list[dict]:
    """Destinations plus their `ig_login` credential STATUS — presence and
    freshness, never a token — derived exactly as `list_sources` derives it
    (#1220 step 2): `none` (never connected) · `active` · `expired` · `revoked`.
    `uq_credential_per_account` makes the join single-row."""
    return await readers.rows(
        executor,
        "SELECT a.id, a.provider_account_ref, a.handle, a.display_name, a.state,"
        "       a.posts_per_day, a.posting_hours_start, a.posting_hours_end, a.tz,"
        "       a.next_slot_at, a.last_posted_at, a.created_at,"
        f"       {_CREDENTIAL_STATUS_SQL},"
        "       c.created_at AS credential_connected_at"
        "  FROM ig_accounts a"
        "  LEFT JOIN oauth_credentials c"
        "    ON c.workspace_id = a.workspace_id"
        "   AND c.ig_account_id = a.id"
        "   AND c.provider = :provider"
        # A `disabled` destination is a REMOVED one (owner decision 2026-09-04):
        # it leaves this list, and connecting the account again brings it back.
        " WHERE a.workspace_id = :ws AND a.state <> 'disabled'"
        " ORDER BY a.created_at, a.id",
        ws=str(workspace_id),
        provider=IG_LOGIN_PROVIDER,
    )


#: The Drive credential provider, as `google_drive_oauth.PROVIDER` spells it —
#: a local name for the same reason `IG_LOGIN_PROVIDER` is one.
GDRIVE_PROVIDER = "gdrive"


async def drive_status(executor, *, workspace_id: str) -> dict:
    """The WORKSPACE's Google Drive grant (069, `07` §15: one per workspace,
    every folder under it) — `none` (never connected) · `active` · `expired` ·
    `revoked` — plus when it was last granted. Never a token.

    Deliberately NOT `_CREDENTIAL_STATUS_SQL`: for `gdrive`, `expires_at` is the
    ACCESS token's hourly expiry and the read door refreshes it on demand (P5,
    `drive_credentials._refresh`), so a past `expires_at` says nothing about the
    grant. `state` is the whole answer — the read door writes `expired` when
    Google answers `invalid_grant`, and a disconnect writes `revoked`. The
    Instagram projection keeps its expiry clause because its refresh is the
    clock's, and a token past expiry there means the clock failed.
    """
    row = await readers.row(
        executor,
        # updated_at, not created_at: a reconnect (and every refresh) replaces
        # the row in place, and "connected since" is the LAST grant's time.
        "SELECT CASE WHEN c.state <> 'active' THEN c.state ELSE 'active' END AS status,"
        "       c.updated_at AS connected_at"
        "  FROM oauth_credentials c"
        " WHERE c.workspace_id = :ws AND c.provider = :provider"
        "   AND c.ig_account_id IS NULL AND c.media_source_id IS NULL",
        ws=str(workspace_id),
        provider=GDRIVE_PROVIDER,
    )
    if row is None:
        return {"status": "none", "connected_at": None}
    return {"status": row["status"], "connected_at": row["connected_at"]}


async def list_sources(executor, *, workspace_id: str) -> list[dict]:
    """Sources with the folder each reads — `folder_ref`, and `folder_name`
    when the picker named it. Whether Google can be reached is the
    WORKSPACE's question since 069 (`drive_status`), not a source's: one
    grant, every folder under it, so no credential is joined here."""
    return await readers.rows(
        executor,
        "SELECT s.id, s.provider, s.state, s.next_sync_at, s.last_sync_success_at,"
        "       s.alerted_at, s.created_at,"
        "       s.config->>'folder_ref' AS folder_ref,"
        "       s.config->>'folder_name' AS folder_name"
        "  FROM media_sources s"
        " WHERE s.workspace_id = :ws ORDER BY s.created_at, s.id",
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


#: `02` §4's intent states — the closed set `ck_intent_state` admits. The
#: list filter validates against this so a typo is a 422, not an empty page.
INTENT_STATES: tuple[str, ...] = (
    "scheduled",
    "prompt_pending",
    "awaiting_approval",
    "approved",
    "publishing",
    "publishing_ambiguous",
    "review_required",
    "posted",
    "skipped",
    "rejected",
    "expired",
    "failed",
    "cancelled",
)

#: `ck_media_state`.
MEDIA_STATES: tuple[str, ...] = ("available", "unsupported", "removed")

#: The intent row plus the two joins the queue renders it with: the media it
#: posts, and the account it posts to (`06` §3 — the handle is how a person
#: recognises the row; NULL when the account carries none, never absent).
_INTENT_COLUMNS = (
    "i.id, i.state, i.ig_account_id, i.media_item_id, i.schedule_slot_at,"
    " i.approval_mode, i.published_via, i.publish_step, i.cancel_requested,"
    " i.ig_permalink, i.entered_state_at, i.created_at,"
    " m.file_name, m.media_kind, m.thumbnail_url, m.caption, m.category,"
    " a.handle AS account_handle, a.display_name AS account_display_name"
)

_INTENT_FROM = (
    "  FROM post_intents i"
    "  JOIN media_items m ON m.workspace_id = i.workspace_id AND m.id = i.media_item_id"
    "  JOIN ig_accounts a ON a.workspace_id = i.workspace_id AND a.id = i.ig_account_id"
)

_MEDIA_COLUMNS = (
    "id, source_id, provider_file_ref, file_name, media_kind, mime_type, file_size,"
    " category, title, caption, tags, thumbnail_url, state, times_posted,"
    " last_posted_at, created_at"
)


async def list_intents(
    executor,
    *,
    workspace_id: str,
    states: Sequence[str] = (),
    limit: int = 50,
) -> list[dict]:
    """The ledger read model (X.2: "reads pending approvals from the ledger").
    *states* narrows to any of several states — a history tab is one call —
    and must already be validated against :data:`INTENT_STATES`. Bounded
    (`01` H5) — *limit* is applied after the caller's clamp."""
    params: dict[str, Any] = {"ws": str(workspace_id), "lim": int(limit)}
    where = "i.workspace_id = :ws"
    if states:
        where += " AND i.state = ANY(CAST(:states AS text[]))"
        params["states"] = list(states)
    return await readers.rows(
        executor,
        f"SELECT {_INTENT_COLUMNS}{_INTENT_FROM} WHERE {where}"
        " ORDER BY i.schedule_slot_at, i.id LIMIT :lim",
        **params,
    )


async def list_media(
    executor,
    *,
    workspace_id: str,
    state: Optional[str] = None,
    never_posted: bool = False,
    limit: int = 50,
) -> list[dict]:
    """The media pool — the workspace's library, not only what has an intent.
    Media surfaced only through intents until this read existed (#1044)."""
    params: dict[str, Any] = {"ws": str(workspace_id), "lim": int(limit)}
    where = "workspace_id = :ws"
    if state is not None:
        where += " AND state = :state"
        params["state"] = state
    if never_posted:
        where += " AND times_posted = 0"
    return await readers.rows(
        executor,
        f"SELECT {_MEDIA_COLUMNS} FROM media_items WHERE {where}"
        " ORDER BY created_at DESC, id LIMIT :lim",
        **params,
    )


async def get_media(executor, *, workspace_id: str, media_id: str) -> Optional[dict]:
    return await readers.row(
        executor,
        f"SELECT {_MEDIA_COLUMNS} FROM media_items WHERE workspace_id = :ws AND id = :id",
        ws=str(workspace_id),
        id=str(media_id),
    )


#: `stats.posts_by_day` looks back this many days of `daily_post_counts`.
STATS_DAYS = 30


async def stats(executor, *, workspace_id: str) -> dict[str, Any]:
    """Server-side aggregates for the dashboard headline (#1044).

    Every figure is a `count(*)`/`sum` over target tables under the claimed
    tenant, one statement per section — never a paged list re-summed, because
    every list here is bounded (`01` H5) and an aggregate derived from a
    truncated set is a confident wrong number. `posts_by_day` reads the cap
    ledger (`daily_post_counts`) itself; `cap` is that day's capacity summed
    across the workspace's accounts.
    """
    ws = str(workspace_id)

    async def by(sql: str) -> dict[str, int]:
        return {
            (row["k"] if row["k"] is not None else ""): int(row["n"])
            for row in await readers.rows(executor, sql, ws=ws)
        }

    intents_by_state = await by(
        "SELECT state AS k, count(*) AS n FROM post_intents WHERE workspace_id = :ws GROUP BY 1"
    )
    media_by_state = await by(
        "SELECT state AS k, count(*) AS n FROM media_items WHERE workspace_id = :ws GROUP BY 1"
    )
    media_by_category = await by(
        "SELECT category AS k, count(*) AS n FROM media_items WHERE workspace_id = :ws GROUP BY 1"
    )
    posted_by_category = await by(
        "SELECT m.category AS k, count(*) AS n"
        "  FROM post_intents i"
        "  JOIN media_items m ON m.workspace_id = i.workspace_id AND m.id = i.media_item_id"
        " WHERE i.workspace_id = :ws AND i.state = 'posted' GROUP BY 1"
    )
    counts = await readers.row(
        executor,
        "SELECT (SELECT count(*) FROM media_items"
        "         WHERE workspace_id = :ws AND state = 'available' AND times_posted = 0)"
        "         AS media_never_posted,"
        "       (SELECT count(*) FROM ig_accounts WHERE workspace_id = :ws) AS accounts,"
        "       (SELECT count(*) FROM media_sources WHERE workspace_id = :ws) AS sources",
        ws=ws,
    )
    posts_by_day = await readers.rows(
        executor,
        "SELECT local_date, sum(count) AS count, sum(cap_at_write) AS cap"
        "  FROM daily_post_counts"
        " WHERE workspace_id = :ws"
        "   AND local_date >= current_date - make_interval(days => :days)"
        " GROUP BY 1 ORDER BY 1",
        ws=ws,
        days=STATS_DAYS,
    )
    return {
        "intents_by_state": intents_by_state,
        "media_by_state": media_by_state,
        "media_never_posted": int(counts["media_never_posted"]),
        "media_by_category": media_by_category,
        "posted_by_category": posted_by_category,
        "posts_by_day": [
            {
                "local_date": r["local_date"],
                "count": int(r["count"]),
                "cap": int(r["cap"]),
            }
            for r in posts_by_day
        ],
        "accounts": int(counts["accounts"]),
        "sources": int(counts["sources"]),
    }


async def get_intent(executor, *, workspace_id: str, intent_id: str) -> Optional[dict]:
    """One intent — always its CURRENT state (R6: terminal-state-first)."""
    return await readers.row(
        executor,
        f"SELECT {_INTENT_COLUMNS}{_INTENT_FROM} WHERE i.workspace_id = :ws AND i.id = :id",
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


def _validate_against(
    changes: Mapping[str, Any],
    columns: Mapping[str, type],
    nullable: frozenset[str],
) -> dict[str, Any]:
    """Keys and Python types only — the DB CHECKs decide the values.

    `bool` is refused for int columns explicitly, because `True` IS an int in
    Python and would otherwise slip through as `posts_per_day = 1`.

    The two allowlists differ; the type discipline does not, and this is the
    one copy of it. A second hand-written loop would be free to disagree about
    what an integer is, and the account columns carry the same `BETWEEN 1 AND
    50` CHECK the workspace ones do.
    """
    if not changes:
        raise InvalidWorkspaceArgs("no settings supplied")
    cleaned: dict[str, Any] = {}
    for key, value in changes.items():
        if key not in columns:
            raise InvalidWorkspaceArgs(f"unknown setting {key!r}")
        expected = columns[key]
        if value is None:
            if key not in nullable:
                raise InvalidWorkspaceArgs(f"{key} cannot be null")
        elif expected is int and (
            isinstance(value, bool) or not isinstance(value, int)
        ):
            raise InvalidWorkspaceArgs(f"{key} must be an integer")
        elif not isinstance(value, expected):
            raise InvalidWorkspaceArgs(f"{key} must be {expected.__name__}")
        cleaned[key] = value
    return cleaned


def validate_settings(changes: Mapping[str, Any]) -> dict[str, Any]:
    """The workspace's typed product configuration (`02` §1)."""
    return _validate_against(changes, SETTINGS_COLUMNS, NULLABLE_SETTINGS)


def validate_account_settings(changes: Mapping[str, Any]) -> dict[str, Any]:
    """One account's schedule overrides — same rules, narrower allowlist."""
    return _validate_against(
        changes, ACCOUNT_SETTINGS_COLUMNS, ACCOUNT_NULLABLE_SETTINGS
    )


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


async def change_account_settings(
    executor, *, workspace_id: str, ig_account_id: str, changes: Mapping[str, Any]
) -> Optional[dict[str, Any]]:
    """Apply one account's schedule overrides; returns the cleaned map that was
    written, or ``None`` when no such account exists in this workspace.

    **The WHERE is workspace-bound as well as id-bound**, which is the tier's
    rule for a caller-supplied id (`command_executors._intent_row`: "Workspace-
    bound in the WHERE, not only by RLS").

    **It is defence in depth, and measured to be exactly that — not the guard.**
    `p_tenant` on `ig_accounts` (`058`:266) covers `svc_ingress`, the role this
    runs as, so the cross-tenant write is already refused with this clause
    removed: mutation-tested, and the gate's tenancy case stays green without
    it. The clause is kept because the convention is right and because
    `svc_clock` holds `USING (true)` on this table, so the role reaching a row
    is not invariant — but nothing here proves it, and a reader should not
    infer that removing RLS would still leave this write scoped.

    **A miss is ``None``, not an exception**, and the caller renders it as
    `not_found` — the same answer for "no such account" and "someone else's
    account", so the surface cannot be used to test which ids exist (`07` §5).

    **The id is PARSED, never passed through raw.** `ig_accounts.id` is `UUID`,
    so a non-UUID string reaches Postgres as a failed cast — a `DataError` this
    module does not translate, which would surface as a 500 rather than a
    refusal. `transit._workspace_folder` sets the precedent; here the parse
    result is discarded and the canonical form bound, so `{...}` and uppercase
    spellings resolve to the one row rather than missing it.
    """
    try:
        account = uuid.UUID(str(ig_account_id))
    except (ValueError, AttributeError, TypeError):
        raise InvalidWorkspaceArgs(f"not an ig_account_id: {ig_account_id!r}") from None
    cleaned = validate_account_settings(changes)
    assignments = ", ".join(f"{k} = :{k}" for k in cleaned)
    result = await _write(
        executor,
        f"UPDATE ig_accounts SET {assignments} WHERE id = :acct AND workspace_id = :ws",
        **cleaned,
        acct=str(account),
        ws=str(workspace_id),
    )
    if result.rowcount == 0:
        return None
    return cleaned


async def remove_member(
    executor, *, workspace_id: str, user_id: str, by_user_id: str
) -> str:
    """`remove_member` (`06`: "an admin removes membership explicitly" — the
    revoke for every join edge, the Telegram one included). Returns the role
    the person held. The runtime never deletes (the 057 grant matrix): the
    delete lives in the `fn_member_remove` door, and this is its one caller.
    Refusals come back by name — the owner cannot be removed
    (`transfer_ownership` is that edge), nobody removes themselves, a
    non-member is `not_found`."""
    row = (
        await executor.execute(
            text(
                "SELECT o_outcome, o_role FROM fn_member_remove("
                "CAST(:ws AS uuid), CAST(:u AS uuid), CAST(:by AS uuid))"
            ),
            {"ws": str(workspace_id), "u": str(user_id), "by": str(by_user_id)},
        )
    ).first()
    outcome = row[0] if row is not None else "not_found"
    if outcome == "removed":
        return str(row[1])
    if outcome == "not_found":
        raise LookupError("not_found")
    raise ValueError(outcome)
