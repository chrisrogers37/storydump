"""Workspaces — the tenant root's writers and the web surface's read models.

This module is the tier's conn-first raw-SQL access to the tenant root. Two
rules decide its shape:

- **Creation is one transaction, and the tenant claim precedes the row.**
  `ct_workspaces_owner_at_insert` is a deferred constraint trigger, so a
  workspace and its owner `workspace_members` row must both exist by COMMIT —
  a two-transaction create fails at commit, not at the insert, which is a
  confusing error to debug. And `p_tenant_workspaces` (`07_row_security.sql`) keys on the
  row's own id, so signup pre-assigns the id and sets `app.tenant_id` to it
  BEFORE the insert. :func:`create_workspace` does both, in that order.
- **Every read is workspace-keyed except one, and that one goes through a
  door.** :func:`list_for_user` answers "which workspaces am I in" — a
  cross-tenant question `p_tenant` on `workspace_members` cannot serve (with
  no tenant claimed the table reads EMPTY, which is indistinguishable from
  the greenfield's normal signed-in-with-no-workspace state). It reads
  through the `fn_memberships_for_caller()` door (`fn_memberships_for_caller`), after
  claiming the caller in `app.actor_user_id` — the same GUC the
  audit triggers already trust, so nothing new is asserted; the door reads
  it internally and an unset value fails closed to no rows.

Settings writes go through :func:`change_settings`, whose allowlist is the
typed-column list — no JSONB, no
free-form keys — and whose validation is a floor: the database's CHECKs
(`ck_ws_posts_per_day`, `ck_ws_tz_valid`, …) remain the authority, and a
`check_violation` from any writer here surfaces as `InvalidWorkspaceArgs`
(translated once, in :func:`_write`, through the tier's one unwrap —
`_dbapi.driver_error_is`), which the port maps to `invalid_args`.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any, Mapping, Optional, Sequence

from asyncpg.exceptions import CheckViolationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.config.defaults import DEFAULT_REPOST_TTL_DAYS, DEFAULT_SKIP_TTL_DAYS
from src.exceptions.base import StorydumpError
from src.services.target import vocabulary
from src.services.target import (
    google_drive_oauth,
    identity,
    intent_ledger,
    invitations,
    oauth_states,
    offboarding,
    readers,
    service_tokens,
    source_policy,
)
from src.services.target._dbapi import driver_error_is
from src.services.target.publish_cap import _SPENDS_CAP_SQL
from src.services.target.unit_of_work import apply_gucs

#: `workspaces.name` is VARCHAR(100).
NAME_MAX = 100

#: The typed product-configuration columns a `settings_change` may
#: touch, with the Python type each accepts. The database CHECKs bound the
#: values; this table bounds the KEYS.
SETTINGS_COLUMNS: dict[str, type] = {
    "tz": str,
    "posts_per_day": int,
    "posting_hours_start": int,
    "posting_hours_end": int,
    "approval_mode": str,
    "approval_ttl_minutes": int,
    "dry_run_mode": bool,
    "repost_ttl_days": int,
    "skip_ttl_days": int,
    "caption_style": str,
    "api_publishing_enabled": bool,
}

#: Columns a settings_change may set to NULL (= inherit the app default per
#: the materialization contract). The NOT NULL ones refuse NULL at the DB.
NULLABLE_SETTINGS = frozenset(
    {"approval_ttl_minutes", "repost_ttl_days", "skip_ttl_days", "caption_style"}
)

#: The inclusive range each bounded setting accepts. The three TTL columns
#: carry no CHECK; these are their bounds.
SETTINGS_RANGES: dict[str, tuple[int, int]] = {
    "approval_ttl_minutes": (
        vocabulary.SETTINGS_TTL_MIN,
        vocabulary.SETTINGS_APPROVAL_TTL_MINUTES_MAX,
    ),
    "repost_ttl_days": (vocabulary.SETTINGS_TTL_MIN, vocabulary.SETTINGS_TTL_DAYS_MAX),
    "skip_ttl_days": (vocabulary.SETTINGS_TTL_MIN, vocabulary.SETTINGS_TTL_DAYS_MAX),
}

#: The per-account schedule overrides an `account_settings_change` may touch —
#: `accounts_sources_media.py`'s "per-account schedule overrides; NULL = inherit the workspace
#: column" — with the Python type each accepts. Deliberately NOT the account's
#: whole row, and each exclusion is a different kind of thing:
#:
#: - `state` is a governed transition (`ck_ig_accounts_state`, the reauth and
#: `moved` flows), not a setting. A settings write that could clear
#: `reauth_required` would retire a credential prompt without a credential.
#: - `handle` / `display_name` are identity. `provider_account_ref` is derived
#: from the handle at creation (`provisioning.manual_ref_for`) and
#: `uq_ig_account_live` keys on the ref, so editing the handle here would
#: leave the two spellings of one account disagreeing.
#: - `next_slot_at` / `last_posted_at` are the clock's machinery — `intent_ledger.py`'s
#: audit trigger names exactly those two as the columns that advance
#: without auditing.
ACCOUNT_SETTINGS_COLUMNS: dict[str, type] = {
    "tz": str,
    "posts_per_day": int,
    "posting_hours_start": int,
    "posting_hours_end": int,
}

#: Every account override is nullable, and NULL is not "unset" — it is the
#: INHERIT arm of the ladder the clock resolves per tick (`04_functions.sql`'s due-scan:
#: `COALESCE(a.posts_per_day, w.posts_per_day)`). A command that could set an
#: override but never clear one would be a one-way door: an account could leave
#: the workspace default and have no way back to it.
ACCOUNT_NULLABLE_SETTINGS = frozenset(ACCOUNT_SETTINGS_COLUMNS)

_CONFIG_COLUMNS = (
    "id, name, state, tz, posts_per_day, posting_hours_start, posting_hours_end,"
    " approval_mode, approval_ttl_minutes, dry_run_mode,"
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
        cause = driver_error_is(exc, CheckViolationError)
        if cause is not None:
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
    tz=None,
    channel="web",
    workspace_id=None,
    protocol_version=None,
    conversion_evidence=None,
) -> str:
    """Create legacy-compatible state until trusted conversion evidence is present."""
    from src.services.target import versioned_mix, source_state
    from src.services.target.mix_conversion import MixConversionEvidence

    if protocol_version not in (None, 1, 2) or isinstance(protocol_version, bool):
        raise versioned_mix.MixProtocolRequired()
    trusted = isinstance(conversion_evidence, MixConversionEvidence)
    if protocol_version == 2 and not trusted:
        raise versioned_mix.MixConversionUnavailable()
    args = dict(
        owner_user_id=owner_user_id,
        name=name,
        tz=tz,
        channel=channel,
        workspace_id=workspace_id,
    )
    if not trusted:
        return await _create_workspace(executor, **args)
    async with source_state.source_savepoint(executor):
        ws = await _create_workspace(executor, **args)
        await versioned_mix.initialize(
            executor,
            workspace_id=ws,
            evidence=conversion_evidence,
            protocol_version=protocol_version,
        )
        return ws


async def _create_workspace(
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
    # Not `apply_gucs`: that door's `tenant_id` is mandatory and this is the
    # user-plane read, which has no tenant yet.
    await executor.execute(
        text("SELECT set_config('app.actor_user_id', :u, true)"), {"u": str(user_id)}
    )
    return await readers.rows(
        executor,
        "SELECT o_workspace_id AS id, o_name AS name, o_state AS state, o_role AS role"
        "  FROM fn_memberships_for_caller()",
    )


async def get_workspace(executor, *, workspace_id: str) -> Optional[dict]:
    """The config row (its typed columns) plus state, or None.

    Carries `restorable_until` — when an offboarding workspace can last be
    restored — computed here from the one grace constant so the dashboard
    never derives it from a copied number.

    Carries `defaults` for the same reason. `repost_ttl_days` and
    `skip_ttl_days` are NULL on purpose, so the settings cards must show what
    leaving them alone means; a second copy of the numbers in TSX would drift
    from the worker's own. Served, it cannot."""
    row = await readers.row(
        executor,
        f"SELECT {_CONFIG_COLUMNS} FROM workspaces WHERE id = :ws",
        ws=str(workspace_id),
    )
    if row is not None:
        row["restorable_until"] = offboarding.restorable_until(
            row.get("offboarding_at")
        )
        # The deployment's, not this workspace's — present whether or not the
        # columns are set, so the client never branches on the payload's shape.
        row["defaults"] = {
            "repost_ttl_days": DEFAULT_REPOST_TTL_DAYS,
            "skip_ttl_days": DEFAULT_SKIP_TTL_DAYS,
        }
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


#: The Instagram credential provider, as `ig_login_oauth.PROVIDER` spells it.
IG_LOGIN_PROVIDER = vocabulary.PROVIDER_IG_LOGIN

#: A credential's usability as a projection — `none` (never connected) ·
#: `active` · `expired` · `revoked` — over the LEFT-JOINed row aliased `c`.
#: One definition for destinations and sources: the rule it mirrors is the one
#: `drive_credentials` / the refresh leg ENFORCE (usable iff `state = 'active'`
#: and `expires_at` has not passed), and two copies of it could drift apart.
#: The mix resource's destination readiness reads it too
#: (`category_mix.destination_projection`).
CREDENTIAL_STATUS_SQL = (
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
    `none` (never connected) · `active` · `expired` · `revoked`.
    `uq_credential_per_account` makes the join single-row."""
    return await readers.rows(
        executor,
        "SELECT a.id, a.provider_account_ref, a.handle, a.display_name, a.state,"
        "       a.posts_per_day, a.posting_hours_start, a.posting_hours_end, a.tz,"
        "       a.next_slot_at, a.last_posted_at, a.created_at,"
        f"       {CREDENTIAL_STATUS_SQL},"
        "       c.created_at AS credential_connected_at"
        "  FROM ig_accounts a"
        "  LEFT JOIN oauth_credentials c"
        "    ON c.workspace_id = a.workspace_id"
        "   AND c.ig_account_id = a.id"
        "   AND c.provider = :provider"
        # A `disabled` destination is a REMOVED one:
        # it leaves this list, and connecting the account again brings it back.
        f" WHERE a.workspace_id = :ws AND {LISTED_ACCOUNT_SQL}"
        f" {LISTED_ACCOUNT_ORDER_SQL}",
        ws=str(workspace_id),
        provider=IG_LOGIN_PROVIDER,
    )


#: The Drive credential provider, as `google_drive_oauth.PROVIDER` spells it.
GDRIVE_PROVIDER = vocabulary.PROVIDER_GDRIVE


async def drive_status(executor, *, workspace_id: str) -> dict:
    """The WORKSPACE's Google Drive grant (`workspace Drive grant`: one per workspace,
    every folder under it) — `none` (never connected) · `active` · `expired` ·
    `revoked` — plus when it was last granted. Never a token.

    Deliberately NOT `CREDENTIAL_STATUS_SQL`: for `gdrive`, `expires_at` is the
    ACCESS token's hourly expiry and the read door refreshes it on demand
    (`drive_credentials._refresh`), so a past `expires_at` says nothing about
    the grant. `state` is the whole answer — the read door writes `expired` when
    Google answers `invalid_grant`, and a disconnect writes `revoked`. The
    Instagram projection keeps its expiry clause because its refresh is the
    clock's, and a token past expiry there means the clock failed.
    """
    row = await readers.row(
        executor,
        # updated_at, not created_at: a reconnect (and every refresh) replaces
        # the row in place, and "connected since" is the LAST grant's time.
        "SELECT CASE WHEN state <> 'active' THEN state ELSE 'active' END AS status,"
        "       updated_at AS connected_at"
        "  FROM oauth_credentials"
        " WHERE " + google_drive_oauth.WORKSPACE_GRANT_WHERE,
        ws=str(workspace_id),
        provider=GDRIVE_PROVIDER,
    )
    if row is None:
        return {"status": "none", "connected_at": None}
    return {"status": row["status"], "connected_at": row["connected_at"]}


async def may_browse_drive(executor, *, workspace_id: str, user_id: str) -> bool:
    """Whether *user_id* may browse the workspace's Drive and pick a folder
    from it (`oauth_credentials.granted_by_user_id`): the grant is the workspace's, but what it reads is
    the Drive of the person who granted it, so only they may walk it.

    A grant with no recorded granter, including a deleted granter
    (`ON DELETE SET NULL`), is the owner's to browse
    until a reconnect records one. A granter removed or demoted since fails
    the admin floor before this is asked, so nobody browses until someone
    reconnects. False with no grant at all.
    """
    row = await readers.row(
        executor,
        "SELECT granted_by_user_id = :u"
        "       OR (granted_by_user_id IS NULL AND EXISTS ("
        "             SELECT 1 FROM workspace_members m"
        "              WHERE m.workspace_id = :ws AND m.user_id = :u"
        "                AND m.role = 'owner')) AS mine"
        "  FROM oauth_credentials"
        " WHERE " + google_drive_oauth.WORKSPACE_GRANT_WHERE,
        ws=str(workspace_id),
        u=str(user_id),
        provider=GDRIVE_PROVIDER,
    )
    return bool(row and row["mine"])


#: A `media_sources` row's `config.removed`, as a boolean — the flag Remove
#: sets. Alias the table `s` wherever these are spliced.
CONNECTED_FLAG_SQL = "COALESCE((s.config->>'removed')::boolean, false)"
#: The predicate for a CONNECTED folder: not removed. State is not part of it
#: — a folder whose grant died is paused, still connected, still weighted.
CONNECTED_SQL = "NOT " + CONNECTED_FLAG_SQL

#: The predicate for a LISTED destination, over `ig_accounts` aliased `a`: a
#: `disabled` destination is a removed one, so it leaves the list, and
#: connecting the account again brings it back. The Accounts tab
#: (:func:`list_accounts`) and the Overview's runway (`content_runway.runway`)
#: both list with it and :data:`LISTED_ACCOUNT_ORDER_SQL`, so the two show the
#: same accounts in the same order.
LISTED_ACCOUNT_SQL = "a.state <> 'disabled'"
#: The order the destinations are listed in: oldest first, the id breaking ties.
LISTED_ACCOUNT_ORDER_SQL = "ORDER BY a.created_at, a.id"

#: An archive source's coverage, one row per source with observations, binding
#: `:ws`: what the CURRENT authorization generation observed (`observed`, of
#: which `represented` have a media item; `oldest` and `refreshed` bound it),
#: and what an older generation left `stale`, awaiting reobservation.
#:func:`list_sources` and the mix rows' `indexed` (`versioned_mix.resource`)
#: both count it, so the two cannot disagree.
ARCHIVE_COVERAGE_SQL = (
    "SELECT i.workspace_id,i.source_id,"
    "count(*) FILTER (WHERE i.data_generation_id=s.current_authorization_generation_id) AS observed,"
    "count(*) FILTER (WHERE i.data_generation_id=s.current_authorization_generation_id "
    "AND EXISTS (SELECT 1 FROM instagram_media_assets a WHERE a.workspace_id=i.workspace_id "
    "AND a.source_id=i.source_id AND a.parent_media_id=i.parent_media_id "
    "AND a.data_generation_id=i.data_generation_id AND a.media_item_id IS NOT NULL)) AS represented,"
    "(SELECT count(*) FROM instagram_media_assets a WHERE a.workspace_id=i.workspace_id "
    "AND a.source_id=i.source_id AND a.data_generation_id=s.current_authorization_generation_id "
    "AND a.media_item_id IS NOT NULL) AS items,"
    "(SELECT count(*) FROM instagram_media_assets a JOIN instagram_media_index p "
    "ON p.workspace_id=a.workspace_id AND p.source_id=a.source_id "
    "AND p.parent_media_id=a.parent_media_id WHERE a.workspace_id=i.workspace_id "
    "AND a.source_id=i.source_id AND a.data_generation_id=s.current_authorization_generation_id "
    "AND p.slide_count IS NOT NULL) AS slides,"
    "count(*) FILTER (WHERE i.data_generation_id=s.current_authorization_generation_id "
    "AND i.slide_count IS NOT NULL AND NOT i.slides_complete) AS carousel_children_truncated,"
    "count(*) FILTER (WHERE i.data_generation_id IS DISTINCT FROM s.current_authorization_generation_id) AS stale,"
    "min(i.original_at) FILTER (WHERE i.data_generation_id=s.current_authorization_generation_id) AS oldest,"
    "max(i.observed_at) FILTER (WHERE i.data_generation_id=s.current_authorization_generation_id) AS refreshed "
    "FROM instagram_media_index i JOIN media_sources s ON s.workspace_id=i.workspace_id AND s.id=i.source_id "
    "WHERE i.workspace_id=:ws GROUP BY i.workspace_id,i.source_id,s.current_authorization_generation_id"
)


def observed_count(observed: Optional[int]) -> Optional[int]:
    """A coverage count, or None without any observation evidence: an
    archive nothing has been observed in has an unknown count, not zero."""
    return int(observed) if observed else None


async def list_sources(executor, *, workspace_id: str) -> list[dict]:
    """Safe provider projections in one batched read, preserving Drive fields.

    Current authorization scopes coverage. Retained G1 metadata under G2 is only
    awaiting reobservation, never represented as current usable content. Counts
    without any observation/scan evidence remain unknown.
    """
    rows = await readers.rows(
        executor,
        "WITH coverage AS (" + ARCHIVE_COVERAGE_SQL + ") "
        "SELECT s.id,s.provider,s.state,s.next_sync_at,s.last_sync_success_at,s.alerted_at,s.created_at,"
        # The folder's own approval position, NULL for the workspace's.
        "       s.approval_mode,"
        "CASE WHEN s.provider='gdrive' THEN s.config->>'folder_ref' END AS folder_ref,"
        "CASE WHEN s.provider='gdrive' THEN s.config->>'folder_name' END AS folder_name,"
        + CONNECTED_FLAG_SQL
        + " AS removed,"
        "CASE WHEN s.provider='gdrive' THEN coalesce(s.config->>'folder_name',s.config->>'folder_ref','folder') "
        "ELSE coalesce(a.display_name,a.handle,'Instagram archive') END AS display_name,"
        "s.origin_ig_account_id,a.handle AS origin_handle,a.display_name AS origin_display_name,"
        "s.current_authorization_generation_id,g.lifecycle AS generation_lifecycle,"
        "p.schema_version,p.revision,p.minimum_age_days,p.include_credited,p.include_unknown,p.carousel_slides,"
        "p.eligibility,p.ranking,p.destination_scope,w.repost_ttl_days,"
        "c.observed,c.represented,c.items,c.slides,c.carousel_children_truncated,c.stale,c.oldest,c.refreshed "
        "FROM media_sources s JOIN workspaces w ON w.id=s.workspace_id "
        "LEFT JOIN ig_accounts a ON a.workspace_id=s.workspace_id AND a.id=s.origin_ig_account_id "
        "LEFT JOIN instagram_archive_generations g ON g.workspace_id=s.workspace_id AND g.source_id=s.id AND g.id=s.current_authorization_generation_id "
        "LEFT JOIN instagram_source_policies p ON p.workspace_id=s.workspace_id AND p.source_id=s.id "
        "LEFT JOIN coverage c ON c.workspace_id=s.workspace_id AND c.source_id=s.id "
        "WHERE s.workspace_id=:ws ORDER BY s.created_at,s.id",
        ws=str(workspace_id),
    )
    result = []
    base = (
        "id",
        "provider",
        "state",
        "next_sync_at",
        "last_sync_success_at",
        "alerted_at",
        "created_at",
        "approval_mode",
        "folder_ref",
        "folder_name",
        "removed",
        "display_name",
    )
    for row in rows:
        out = {k: row[k] for k in base}
        archive = row["provider"] == "instagram_archive"
        out["origin"] = (
            None
            if not archive
            else {
                "id": row["origin_ig_account_id"],
                "handle": row["origin_handle"],
                "display_name": row["origin_display_name"],
            }
        )
        out["destination_scope"] = (
            "workspace_wide" if not archive else row["destination_scope"]
        )
        out["policy_capabilities"] = {"editable": []}
        out["policy"] = None
        out["coverage"] = None
        if archive:
            try:
                out["policy"] = source_policy.project_policy(
                    row, row["repost_ttl_days"]
                )
                out["policy_capabilities"] = out["policy"]["capabilities"]
            except source_policy.PolicyRefused:
                out["policy_reason"] = "unsupported_source_policy"
            observed = observed_count(row["observed"])
            out["coverage"] = {
                "generation_id": row["current_authorization_generation_id"],
                "generation_state": row["generation_lifecycle"],
                "observed": observed,
                "represented": None if observed is None else row["represented"] or 0,
                "items": None if observed is None else row["items"] or 0,
                "slides": None if observed is None else row["slides"] or 0,
                "carousel_children_truncated": None
                if observed is None
                else row["carousel_children_truncated"] or 0,
                "awaiting_reobservation": row["stale"] or 0,
                "oldest_observed_at": row["oldest"],
                "last_observed_at": row["refreshed"],
            }
        result.append(out)
    return result


async def list_bindings(executor, *, workspace_id: str) -> list[dict]:
    return await readers.rows(
        executor,
        "SELECT id, channel, external_ref, state, settings, created_at"
        "  FROM channel_bindings WHERE workspace_id = :ws ORDER BY created_at, id",
        ws=str(workspace_id),
    )


async def list_invitations(executor, *, workspace_id: str) -> list[dict]:
    """Pending invitations the doors would still honour (`invitation legitimacy checks`): the workspace
    is active and the sender is still an owner or admin there with an active
    account. The others are dead links that age out with their expiry, so they
    are not listed as pending. The predicate mirrors `invitation legitimacy checks`'s two doors by hand.

    The doors' third check, a removal newer than the invitation, is not
    repeated: the runtime cannot read the removal record, and it needs
    none. A removal revokes the invitations addressed to the person in its own
    transaction (`invitations.revoke_on_removal`), and `invitation legitimacy checks` revoked the ones
    older removals left pending, so the state filter already leaves them out.
    The token is never read back — only its hash is stored, and the row
    exposes nothing a caller could present."""
    return await readers.rows(
        executor,
        "SELECT i.id, i.delivery_channel, i.email, i.role, i.state, i.expires_at,"
        "       i.invited_by_user_id, i.created_at"
        "  FROM workspace_invitations i"
        "  JOIN workspaces w ON w.id = i.workspace_id AND w.state = 'active'"
        "  JOIN workspace_members m ON m.workspace_id = i.workspace_id"
        "                          AND m.user_id = i.invited_by_user_id"
        "                          AND m.role IN ('owner', 'admin')"
        "  JOIN users u ON u.id = i.invited_by_user_id AND u.state = 'active'"
        " WHERE i.workspace_id = :ws AND i.state = 'pending'"
        "   AND i.expires_at > now()"
        " ORDER BY i.created_at, i.id",
        ws=str(workspace_id),
    )


#: The intent states — the closed set `ck_intent_state` admits, owned
#: by the vocabulary module (pinned against the migration there). The list
#: filter validates against this so a typo is a 422, not an empty page.
INTENT_STATES: tuple[str, ...] = vocabulary.INTENT_STATES

#: `ck_intent_origin`: the list filter's other closed set.
INTENT_ORIGINS: tuple[str, ...] = vocabulary.INTENT_ORIGINS

#: `ck_media_state`, owned by the vocabulary module (pinned against the
#: migration there).
MEDIA_STATES: tuple[str, ...] = vocabulary.MEDIA_STATES

#: The characters of a media row's content hash its `thumbnail_version` carries.
THUMBNAIL_VERSION_CHARS = 16

#: An archive item's `thumbnail_version`, `<generation_id>.<source_use_epoch>`
#: over :func:`thumbnail_joins`: the generation of its observation, which the
#: joins hold to the source's current one, and the source's use epoch. A
#: reauthorization or an Off and On again names a new URL, so a picture answered
#: under one pair never stands for another. NULL without a current observation.
#: The thumbnail route admits only the current pair (`archive_preview`).
ARCHIVE_THUMBNAIL_VERSION_SQL = (
    "mi.data_generation_id::text || '.' || ms.source_use_epoch::text"
)


def thumbnail_joins(m: str) -> str:
    """The joins a media row's thumbnail is read through, the row's columns
    prefixed by *m*: its source (`ms`), and its observation in the source's
    current authorization generation (`mi`) — none for a Drive row, nor for
    an archive item awaiting reobservation. An item has at most one
    asset (`uq_archive_asset_item`), so no row repeats."""
    return (
        f" JOIN media_sources ms ON ms.workspace_id = {m}workspace_id"
        f" AND ms.id = {m}source_id"
        " LEFT JOIN instagram_media_assets ma ON ma.workspace_id = ms.workspace_id"
        f" AND ma.source_id = ms.id AND ma.media_item_id = {m}id"
        " LEFT JOIN instagram_media_index mi ON mi.workspace_id = ma.workspace_id"
        " AND mi.source_id = ma.source_id AND mi.parent_media_id = ma.parent_media_id"
        " AND mi.data_generation_id = ma.data_generation_id"
        " AND mi.observation_revision = ma.observation_revision"
        " AND mi.data_generation_id = ms.current_authorization_generation_id"
    )


def _thumbnail_sql(m: str) -> str:
    """A media row's thumbnail as the reads return it, the row's columns
    prefixed by *m*, over :func:`thumbnail_joins`: whether it has one, and the
    version the page puts in the image URL, never a link. A Drive row has one
    while its listing left a link, and its version is a prefix of the content
    hash, which a row keeps for life, so one URL always names one picture. An
    archive item's picture is looked up fresh for each request, so it has one
    exactly when it has a current observation, versioned by
    :data:`ARCHIVE_THUMBNAIL_VERSION_SQL`."""
    return (
        "CASE WHEN ms.provider = 'instagram_archive'"
        " THEN mi.data_generation_id IS NOT NULL"
        f" ELSE {m}thumbnail_url IS NOT NULL END AS has_thumbnail,"
        " CASE WHEN ms.provider = 'instagram_archive'"
        f" THEN {ARCHIVE_THUMBNAIL_VERSION_SQL}"
        f" ELSE left({m}content_hash, {THUMBNAIL_VERSION_CHARS})"
        " END AS thumbnail_version"
    )


#: The account an item's source belongs to (`mo`), whose handle says where an
#: archive item came from, joined after :func:`thumbnail_joins`, which defines
#: the source (`ms`) it hangs from. A Drive source has no such account.
_ORIGIN_JOIN = (
    " LEFT JOIN ig_accounts mo ON mo.workspace_id = ms.workspace_id"
    " AND mo.id = ms.origin_ig_account_id"
)

#: The three facts :func:`_archive_row` nests as an item's provenance, by the
#: column each read selects them as: the source's provider, the origin
#: account's handle and the current observation's original time.
_PROVENANCE_COLUMNS = {
    "source_provider": "ms.provider",
    "origin_handle": "mo.handle",
    "original_at": "mi.original_at",
    "slide_post_ref": "CASE WHEN mi.slide_count IS NOT NULL THEN mi.post_ref END",
    "slide_position": "CASE WHEN mi.slide_count IS NOT NULL THEN ma.position END",
    "slide_count": "CASE WHEN mi.slide_count IS NOT NULL AND mi.slides_complete THEN mi.slide_count END",
}


def picture_joins(m: str) -> str:
    """The joins every read that draws an item's picture is made over, the
    row's columns prefixed by *m*: :func:`thumbnail_joins` and the origin
    account. One definition, so no read can tell Drive from an archive in
    another way than the picture it draws does."""
    return thumbnail_joins(m) + _ORIGIN_JOIN


def _picture_sql(m: str) -> str:
    """What every read that draws an item's picture selects of it, the row's
    columns prefixed by *m*, over :func:`picture_joins`: the thumbnail's flag
    and version (:func:`_thumbnail_sql`), and the three facts
    :func:`_archive_row` nests as its provenance."""
    provenance = ", ".join(
        f"{sql} AS {name}" for name, sql in _PROVENANCE_COLUMNS.items()
    )
    return f"{_thumbnail_sql(m)}, {provenance}"


def _archive_row(row: dict) -> dict:
    """A picture read's row as the browser may have it (:func:`_picture_sql`).

    An archive item's provenance goes under one key — the provider, the handle
    of the account that posted it and when — and a Drive row's is NULL, so
    every row carries the key. A slide is named from its current position and
    complete count; a single post uses :data:`vocabulary.ARCHIVE_ITEM_NAME`.
    The row has no `provider_file_ref`, where the read selects one: stored,
    the reference is the Graph id itself, and a row whose stored name is
    `Instagram <Graph id>` must not show it either, the web labelling a post
    by where it came from, never by Instagram's id. A projection only;
    nothing stored is rewritten. Never a Graph id, a permalink or a caption."""
    provider = row.pop("source_provider")
    handle, original_at = row.pop("origin_handle"), row.pop("original_at")
    post_ref = row.pop("slide_post_ref")
    position, count = row.pop("slide_position"), row.pop("slide_count")
    if provider != "instagram_archive":
        row["provenance"] = None
        return row
    row["provenance"] = {
        "provider": provider,
        "origin_handle": handle,
        "original_at": original_at,
        "slide": (
            {"post_ref": str(post_ref), "position": position, "count": count}
            if post_ref is not None and position is not None
            else None
        ),
    }
    if position is not None:
        row["file_name"] = (
            vocabulary.ARCHIVE_SLIDE_NAME.format(position=position, count=count)
            if count is not None
            else vocabulary.ARCHIVE_SLIDE_NAME_UNKNOWN.format(position=position)
        )
    elif not str(row.get("file_name", "")).startswith("Instagram post, slide "):
        row["file_name"] = vocabulary.ARCHIVE_ITEM_NAME
    if "provider_file_ref" in row:
        row["provider_file_ref"] = None
    if "caption" in row:
        row["caption"] = None
    return row


#: The calendar's local day, as a bound and as a label. A day is the
#: WORKSPACE's: one zone for the whole calendar, whatever zone an account's
#: times read in.
#:
#: Midnight of a local date in the workspace's own zone, as the instant a slot
#: is compared with. A scalar subquery rather than the joined `w.tz`, so the
#: bound is one value the planner can scan an index range by
#: (`ix_intents_workspace_slot`).
LOCAL_MIDNIGHT_SQL = (
    "(CAST(:{name} AS date)::timestamp AT TIME ZONE"
    " (SELECT tz FROM workspaces WHERE id = :ws))"
)

#: The day a slot falls on: its date in the workspace's zone, *tz* being the
#: workspace's own as the statement names it.
LOCAL_DAY_SQL = "to_char({slot} AT TIME ZONE {tz}, 'YYYY-MM-DD') AS day"

#: The intent row plus the two joins the queue renders it with: the media it
#: posts, and the account it posts to (the handle is how a person
#: recognises the row; NULL when the account carries none, never absent). A
#: picture read like the media's: every row is read through :func:`_archive_row`.
_INTENT_COLUMNS = (
    "i.id, i.state, i.ig_account_id, i.media_item_id, i.schedule_slot_at,"
    " i.approval_mode, i.published_via, i.publish_step, i.cancel_requested,"
    " i.ig_permalink, i.entered_state_at, i.created_at,"
    " i.origin, i.scheduled_by_user_id,"
    # what approved the story, NULL for a person: the Queue's by-line
    " i.approval_rule,"
    # who planned it, while they are still a member
    f" {identity.member_name_sql('i.scheduled_by_user_id', 'i.workspace_id')}"
    " AS scheduled_by,"
    # the zone the story's times read in: the account's, else the workspace's
    # (the one `schedule_item` resolved its wall time in)
    " COALESCE(a.tz, w.tz) AS tz,"
    f" CASE WHEN i.last_error->>'class' = '{vocabulary.PLANNED_MISSED}'"
    "      THEN i.last_error->>'message' END AS miss_reason,"
    # a planned story's repeat rule as one value, NULL for none (`intent repeat constraints`)
    f" {intent_ledger.repeat_sql('i.')} AS repeat,"
    " m.file_name, m.media_kind, m.caption, m.category, m.link_url,"
    f" {_picture_sql('m.')},"
    " a.handle AS account_handle, a.display_name AS account_display_name"
)

_INTENT_FROM = (
    "  FROM post_intents i"
    "  JOIN media_items m ON m.workspace_id = i.workspace_id AND m.id = i.media_item_id"
    "  JOIN ig_accounts a ON a.workspace_id = i.workspace_id AND a.id = i.ig_account_id"
    "  JOIN workspaces w ON w.id = i.workspace_id" + picture_joins("m.")
)

#: A media read's columns: every row is read through :func:`_archive_row`.
_MEDIA_COLUMNS = (
    "m.id, m.source_id, m.provider_file_ref, m.file_name, m.media_kind,"
    " m.mime_type, m.file_size, m.category, m.title, m.caption, m.tags,"
    " m.link_url, m.state, m.times_posted, m.last_posted_at, m.created_at,"
    " m.auto_approve,"
    f" {_picture_sql('m.')}"
)

_MEDIA_FROM = " FROM media_items m" + picture_joins("m.")


def _slot_range(
    params: dict[str, Any], from_date: Optional[date], to_date: Optional[date]
) -> str:
    """`AND` clauses bounding the slot to the workspace's local days
    ``[from_date, to_date)``; either end may be absent."""
    sql = ""
    for name, value, op in (("from_date", from_date, ">="), ("to_date", to_date, "<")):
        if value is not None:
            sql += f" AND i.schedule_slot_at {op} " + LOCAL_MIDNIGHT_SQL.format(
                name=name
            )
            params[name] = value
    return sql


async def list_intents(
    executor,
    *,
    workspace_id: str,
    states: Sequence[str] = (),
    origin: Optional[str] = None,
    newest_first: bool = False,
    limit: int = 50,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
) -> list[dict]:
    """The ledger read model ("reads pending approvals from the ledger").
    *states* narrows to any of several states — a history tab is one call —
    and must already be validated against :data:`INTENT_STATES`; *origin*
    (one of :data:`INTENT_ORIGINS`) to the planned stories or the cadence's —
    "what is coming" is the planned ones still `scheduled`. *from_date* and
    *to_date* bound the slot to the workspace's local days ``[from_date,
    to_date)``: the calendar's day view. Soonest first, or
    *newest_first* for a history (the latest misses, not the oldest). Bounded
    — *limit* is applied after the caller's clamp."""
    params: dict[str, Any] = {"ws": str(workspace_id), "lim": int(limit)}
    where = "i.workspace_id = :ws"
    if states:
        where += " AND i.state = ANY(CAST(:states AS text[]))"
        params["states"] = list(states)
    if origin is not None:
        where += " AND i.origin = :origin"
        params["origin"] = origin
    where += _slot_range(params, from_date, to_date)
    order = "DESC" if newest_first else "ASC"
    rows = await readers.rows(
        executor,
        f"SELECT {_INTENT_COLUMNS}{_INTENT_FROM} WHERE {where}"
        f" ORDER BY i.schedule_slot_at {order}, i.id {order} LIMIT :lim",
        **params,
    )
    return [_archive_row(row) for row in rows]


#: What the month read selects of each of a day's newest, bar the provenance.
_INTENT_DAY_COLUMNS = (
    "id",
    "state",
    "schedule_slot_at",
    "file_name",
    "category",
    "media_item_id",
    "media_kind",
    "has_thumbnail",
    "thumbnail_version",
)

#: What each of a month day's newest carries: enough to name it and to draw its
#: thumbnail through the route (`has_thumbnail` and its version, never the
#: link), as the intents read does, and its provenance, which is also
#: how the page tells an archive item's picture from a Drive picture.
INTENT_DAY_FIELDS = (*_INTENT_DAY_COLUMNS, "provenance")


async def intent_days(
    executor,
    *,
    workspace_id: str,
    states: Sequence[str],
    from_date: date,
    to_date: date,
    per_day: int,
) -> list[dict]:
    """The calendar's month: for each of the workspace's local days in
    ``[from_date, to_date)`` that holds an intent in *states*, how many it
    holds and its *per_day* newest. The count is a COUNT, never re-summed
    from a bounded list; a day with none is absent.

    *states* must be named, and validated against :data:`INTENT_STATES` as
    :func:`list_intents`'s are."""
    if not states:
        raise ValueError("intent states must be named")
    params: dict[str, Any] = {
        "ws": str(workspace_id),
        "per_day": int(per_day),
        "states": list(states),
    }
    where = "i.workspace_id = :ws AND i.state = ANY(CAST(:states AS text[]))"
    where += _slot_range(params, from_date, to_date)
    rows = await readers.rows(
        executor,
        # The ledger alone is counted, ranked and cut (`r`); the media and its
        # picture are joined to the kept rows after. Each join is to one row
        # (the intent's media and its source are NOT NULL keys, the rest at most
        # one), so no count or row changes, and a load draws `per_day` pictures
        # a day, never the month's.
        "SELECT r.day, r.total, r.id, r.state, r.schedule_slot_at, r.media_item_id,"
        "       m.file_name, m.category, m.media_kind,"
        f"      {_picture_sql('m.')}"
        "  FROM (SELECT d.*, count(*) OVER (PARTITION BY d.day) AS total,"
        "               row_number() OVER (PARTITION BY d.day"
        "                 ORDER BY d.schedule_slot_at DESC, d.id DESC) AS rn"
        "          FROM (SELECT"
        f"                 {LOCAL_DAY_SQL.format(slot='i.schedule_slot_at', tz='w.tz')},"
        "                       i.workspace_id, i.id, i.state, i.schedule_slot_at,"
        "                       i.media_item_id"
        "                  FROM post_intents i"
        "                  JOIN workspaces w ON w.id = i.workspace_id"
        f"                WHERE {where}) d) r"
        "  JOIN media_items m ON m.workspace_id = r.workspace_id"
        "                    AND m.id = r.media_item_id"
        f"{picture_joins('m.')}"
        " WHERE r.rn <= :per_day ORDER BY r.day, r.rn",
        **params,
    )
    days: dict[str, dict] = {}
    for row in map(_archive_row, rows):
        day = days.setdefault(
            row["day"], {"date": row["day"], "count": int(row["total"]), "newest": []}
        )
        day["newest"].append({k: row[k] for k in INTENT_DAY_FIELDS})
    return list(days.values())


async def list_media(
    executor,
    *,
    workspace_id: str,
    state: Optional[str] = None,
    never_posted: bool = False,
    limit: int = 50,
) -> list[dict]:
    """The media pool — the workspace's library, not only what has an
    intent."""
    params: dict[str, Any] = {"ws": str(workspace_id), "lim": int(limit)}
    where = "m.workspace_id = :ws"
    # No default: the web asks for `available` (the library shows what is
    # connected; a removed folder's rows are `removed`), and
    # an operator may ask for any state.
    if state is not None:
        where += " AND m.state = :state"
        params["state"] = state
    if never_posted:
        where += " AND m.times_posted = 0"
    rows = await readers.rows(
        executor,
        # Cut first, then join the pictures to the kept rows: each of the joins
        # is to one row at most (`picture_joins`), so no row, order or count
        # changes, and a load draws `lim` pictures, never the library's.
        f"SELECT {_MEDIA_COLUMNS}"
        " FROM (SELECT * FROM media_items m"
        f"       WHERE {where} ORDER BY m.created_at DESC, m.id LIMIT :lim) m"
        f"{picture_joins('m.')}"
        " ORDER BY m.created_at DESC, m.id",
        **params,
    )
    return [_archive_row(row) for row in rows]


async def get_media(executor, *, workspace_id: str, media_id: str) -> Optional[dict]:
    row = await readers.row(
        executor,
        f"SELECT {_MEDIA_COLUMNS}{_MEDIA_FROM} WHERE m.workspace_id = :ws AND m.id = :id",
        ws=str(workspace_id),
        id=str(media_id),
    )
    return _archive_row(row) if row is not None else None


async def thumbnail_link(
    executor, *, workspace_id: str, media_id: str
) -> Optional[dict]:
    """What the thumbnail route fetches through: the row's source and its
    provider, which decides how the picture is fetched, the row's provider
    link and the file it names, or None for a media id this workspace does
    not hold. The one read that selects the link."""
    return await readers.row(
        executor,
        "SELECT m.source_id, s.provider, m.provider_file_ref, m.thumbnail_url"
        "  FROM media_items m"
        "  JOIN media_sources s ON s.workspace_id = m.workspace_id AND s.id = m.source_id"
        " WHERE m.workspace_id = :ws AND m.id = :id",
        ws=str(workspace_id),
        id=str(media_id),
    )


#: The stats window: `posts_by_day` and `posted_by_source` count today and the
#: `STATS_DAYS` local dates before it, on the date a post debited the daily cap.
STATS_DAYS = 30


async def stats(executor, *, workspace_id: str) -> dict[str, Any]:
    """Server-side aggregates for the dashboard headline.

    Every figure is a `count(*)`/`sum` over target tables under the claimed
    tenant, one statement per section — never a paged list re-summed, because
    every list here is bounded and an aggregate derived from a
    truncated set is a confident wrong number. `posts_by_day` reads the cap
    ledger (`daily_post_counts`) itself; `cap` is that day's capacity summed
    across the workspace's accounts.
    """
    ws = str(workspace_id)

    async def by(sql: str, **params: Any) -> dict[str, int]:
        return {
            (row["k"] if row["k"] is not None else ""): int(row["n"])
            for row in await readers.rows(executor, sql, ws=ws, **params)
        }

    intents_by_state = await by(
        "SELECT state AS k, count(*) AS n FROM post_intents WHERE workspace_id = :ws GROUP BY 1"
    )
    media_by_state = await by(
        "SELECT state AS k, count(*) AS n FROM media_items WHERE workspace_id = :ws GROUP BY 1"
    )
    media_by_category = await by(
        # Available only: a removed folder's retired rows are out of the
        # library, and the card must agree with the pool it sits beside.
        "SELECT category AS k, count(*) AS n FROM media_items"
        " WHERE workspace_id = :ws AND state = 'available' GROUP BY 1"
    )
    posted_by_source = await by(
        # Keyed on the connected folder the post's media came from, as the
        # category-mix route keys its weights (`source_id`, the text of
        # `media_sources.id`); `category` is a display label no weight keys on.
        # Counted as `posts_by_day` counts: only stories that spend the cap
        # (`_SPENDS_CAP_SQL`), since a story a person planned is outside the
        # cadence the mix draws, and windowed on the local date the post
        # debited the cap, the date `daily_post_counts` counts it on. A
        # `legacy_backfill` row never debited the cap, so it is outside the
        # window here as it is in the ledger.
        "SELECT m.source_id::text AS k, count(*) AS n"
        "  FROM post_intents i"
        "  JOIN media_items m ON m.workspace_id = i.workspace_id AND m.id = i.media_item_id"
        " WHERE i.workspace_id = :ws AND i.state = 'posted'"
        "   AND i.published_via <> 'dry_run'"
        "   AND i.cap_consumed_on >= current_date - make_interval(days => :days)"
        "   AND " + _SPENDS_CAP_SQL + " GROUP BY 1",
        days=STATS_DAYS,
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
        "posted_by_source": posted_by_source,
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
    """One intent — always its CURRENT state (terminal-state-first)."""
    row = await readers.row(
        executor,
        f"SELECT {_INTENT_COLUMNS}{_INTENT_FROM} WHERE i.workspace_id = :ws AND i.id = :id",
        ws=str(workspace_id),
        id=str(intent_id),
    )
    return _archive_row(row) if row is not None else None


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
    ranges: Mapping[str, tuple[int, int]],
) -> dict[str, Any]:
    """Keys, Python types, and the inclusive *ranges* — the DB CHECKs decide
    every other value.

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
        elif key in ranges:
            low, high = ranges[key]
            if not low <= value <= high:
                raise InvalidWorkspaceArgs(f"{key} must be {low} to {high}")
        cleaned[key] = value
    return cleaned


def validate_settings(changes: Mapping[str, Any]) -> dict[str, Any]:
    """The workspace's typed product configuration."""
    return _validate_against(
        changes, SETTINGS_COLUMNS, NULLABLE_SETTINGS, SETTINGS_RANGES
    )


def validate_account_settings(changes: Mapping[str, Any]) -> dict[str, Any]:
    """One account's schedule overrides — same rules, narrower allowlist."""
    return _validate_against(
        changes, ACCOUNT_SETTINGS_COLUMNS, ACCOUNT_NULLABLE_SETTINGS, {}
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

    **It is defence in depth — not the guard.** `p_tenant` on `ig_accounts`
    (`07_row_security.sql`:266) covers `svc_ingress`, the role this runs as, so the cross-tenant
    write is refused even with this clause removed, and the gate's tenancy case
    stays green without it. The clause is there because the convention is right
    and because `svc_clock` holds `USING (true)` on this table, so the role
    reaching a row is not invariant — but nothing here proves it, and a reader
    should not infer that removing RLS would still leave this write scoped.

    **A miss is ``None``, not an exception**, and the caller renders it as
    `not_found` — the same answer for "no such account" and "someone else's
    account", so the surface cannot be used to test which ids exist.

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
    """`remove_member` ("an admin removes membership explicitly" — the
    revoke for every join edge, the Telegram one included). Returns the role
    the person held. The runtime never deletes (the `09_grants.sql` grant matrix): the
    delete lives in the `fn_member_remove` door, and this is its one caller.
    Refusals come back by name — the owner cannot be removed
    (`transfer_ownership` is that edge), nobody removes themselves, a
    non-member is `not_found`. The door also checks its caller: the workspace
    must be the claimed tenant and *by_user_id* an owner or admin of it, or it
    raises. The door records the removal, which the Telegram join
    path honours until the person is invited back. In the same transaction,
    the workspace service identities they minted are revoked (`workspace member removal`), as are the
    pending invitations in the workspace that they sent or that are addressed
    to them, so none of them lets anyone in and none blocks the fresh
    invitation that brings them back, and every live link state they hold for
    this workspace is retired."""
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
        # The door recorded the removal, which the Telegram join path honours
        # (`workspace member removal`); the service identities this person minted go with them, and so
        # do the pending invitations they sent or were sent and every bind or
        # connect link they hold for this workspace.
        await service_tokens.revoke_minted_by(
            executor, workspace_id=str(workspace_id), user_id=str(user_id)
        )
        await invitations.revoke_on_removal(
            executor, workspace_id=str(workspace_id), user_id=str(user_id)
        )
        await oauth_states.retire_live_states(
            executor, user_id=user_id, workspace_id=workspace_id
        )
        return str(row[1])
    if outcome == "not_found":
        raise LookupError("not_found")
    raise ValueError(outcome)
