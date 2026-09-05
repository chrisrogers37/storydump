"""The `/api/v1` surface: reads as resources, writes as the closed vocabulary.

The shape is #1015's router design (§1–§3), built rather than described:

- **Reads are resources** because `01` says the web surface is *pull* — it
  reads ledger state, it does not receive interaction requests. Every read is
  workspace-keyed in the URL and passes the ONE gate
  (`tenant_resolution.authorize_member`) before touching a row; a workspace
  the caller is not a member of answers **404, never 403** (`07` §5, no
  existence oracle — the same 404 a workspace that does not exist gets).
- **Writes are the `01` vocabulary made literal**: one route,
  ``POST /workspaces/{ws}/commands/{command}``, whose path segment the port
  validates against `commands.VOCABULARY`. The route table therefore cannot
  drift from the vocabulary, and W4's Telegram adapter will hand the same
  `Command` objects to the same `commands.ingest`. ``create_workspace`` — the
  one command with no workspace yet — has its own route.
- **The order is the port's** (`commands.ingest`: refuse cold → admit →
  execute, one transaction). What this adapter owns is the shape of its key —
  the ``Idempotency-Key`` header, admitted into `command_dedup` keyed on
  ``(web, session id, key)`` — and the status each outcome renders: a true
  replay is ``200 {"outcome": "replayed"}`` without running anything; the
  same key with a different body is 409; a refusal anywhere rolled the dedup
  row back with everything else, so a retry re-executes.

The tenant-less reads (``/me``, ``/workspaces``) run on a raw connection: the
unit of work is unconstructible without a tenant by design, and these are
user-plane reads — the membership list goes through the
`fn_memberships_for_caller()` door (`064`, #1037), which reads the caller
from `app.actor_user_id` and answers under the production role.
"""

from __future__ import annotations

import re
import json
import uuid
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from src.api.principal import Principal, current_principal, require_engine
from src.api import google_client, instagram_client
from src.config.settings import settings
from src.services.target.drive_adapter import (
    DriveLostResponse,
    DriveRetryableError,
    DriveTerminalError,
)
from src.services.target import (
    channel_bind,
    commands,
    drive_credentials,
    google_drive_adapter,
    google_drive_oauth,
    identity,
    identity_link,
    ig_login_oauth,
    invitations,
    media_sync,
    provisioning,
    tenant_resolution,
    workspaces,
)
from src.services.target.commands import Command, CommandResult
from src.services.target.ig_login_oauth import STATE_TTL_SECONDS, issue_state
from src.services.target.unit_of_work import unit_of_work


#: Telegram's username shape for a BOT: 5-32 characters of letters, digits and
#: underscores, ending in "bot" (case-insensitive). Both Telegram routes refuse
#: a `TARGET_TELEGRAM_BOT_USERNAME` outside it rather than minting a link to a
#: bot that cannot exist.
TELEGRAM_BOT_USERNAME_RE = re.compile(r"(?i)[a-z][a-z0-9_]{1,28}bot")


def _target_bot_username() -> str:
    """The bot the deep links name, validated once for both Telegram routes."""
    # A username, not a handle: an operator who pastes `@storydump_app_bot`
    # must not mint `t.me/@…`, which Telegram cannot open.
    bot_username = (settings.TARGET_TELEGRAM_BOT_USERNAME or "").strip().lstrip("@")
    if not bot_username:
        raise HTTPException(
            status_code=503,
            detail="telegram linking not configured: set TARGET_TELEGRAM_BOT_USERNAME",
        )
    # …and a username with Telegram's shape. A stray trailing character —
    # `storydump_app_bot.` in production on 2026-09-04 — mints a link the site
    # refuses as a different bot, with nothing on either side saying why.
    # Refuse the setting's shape here, naming the setting and the value, so
    # the mistake is visible where it was made.
    if not TELEGRAM_BOT_USERNAME_RE.fullmatch(bot_username):
        raise HTTPException(
            status_code=503,
            detail=(
                "TARGET_TELEGRAM_BOT_USERNAME is not a Telegram bot username:"
                f" {bot_username!r} (5-32 letters, digits or underscores,"
                " ending in 'bot')"
            ),
        )
    return bot_username


router = APIRouter(tags=["v1"])

#: `command_dedup.channel` for this adapter (`webhook_ingress.CHANNELS`).
CHANNEL = "web"
#: Required on every command. The client owns the value — the web front end
#: mints it as ``<command>:<intent_id>`` in its route handler, so a repeated
#: click replays rather than re-executes; the server owns nothing but the
#: dedup row.
IDEMPOTENCY_HEADER = "Idempotency-Key"
IDEMPOTENCY_KEY_MAX = 200
#: `01` H5: every list is bounded. The clamp is a Query constraint, so an
#: out-of-range value is a 422 rather than silently narrowed.
LIST_LIMIT_DEFAULT = 50
LIST_LIMIT_MAX = 200


# --- seams ---------------------------------------------------------------


def _open_tenant(request: Request, workspace_id: str, principal: Principal):
    """The request's tenant-scoped unit of work: tenant + actor GUCs applied,
    one transaction. The one seam the unit gate replaces."""
    return unit_of_work(
        require_engine(request),
        workspace_id,
        actor_kind="user",
        actor_user_id=principal.user_id,
        channel=CHANNEL,
    ).begin()


@asynccontextmanager
async def _member(request: Request, workspace_id: str, principal: Principal):
    """Open the tenant's unit of work and run the ONE gate — every read."""
    async with _open_tenant(request, workspace_id, principal) as session:
        await tenant_resolution.authorize_member(
            session, workspace_id, principal.user_id, minimum_role="member"
        )
        yield session


async def _collection(
    request: Request, ws: uuid.UUID, principal: Principal, reader, name: str
):
    async with _member(request, str(ws), principal) as session:
        items = await reader(session, workspace_id=str(ws))
    return {name: items}


def _idempotency_key(request: Request) -> str:
    key = request.headers.get(IDEMPOTENCY_HEADER, "").strip()
    if not key:
        raise HTTPException(
            status_code=400,
            detail=f"{IDEMPOTENCY_HEADER} header is required on every command",
        )
    if len(key) > IDEMPOTENCY_KEY_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"{IDEMPOTENCY_HEADER} exceeds {IDEMPOTENCY_KEY_MAX} characters",
        )
    return key


async def _json_object(request: Request) -> dict[str, Any]:
    """The body as a JSON object; an empty body is an empty object."""
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        body = json.loads(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="body is not JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    return body


async def _dispatch(
    request: Request,
    principal: Principal,
    *,
    tenant: str,
    kind: str,
    workspace_id: Optional[str],
    extra: Optional[dict[str, Any]] = None,
) -> CommandResult:
    """Key, body, one unit of work on *tenant*, then the port's `ingest`.

    The key is checked before the body is read, so a keyless request buffers
    nothing. The dedup fingerprint is taken over the raw body — never over
    *extra*, which is what this adapter adds (the pre-assigned workspace id).
    """
    key = _idempotency_key(request)
    body = await _json_object(request)
    command = Command(
        kind=kind,
        workspace_id=workspace_id,
        actor_user_id=principal.user_id,
        channel=CHANNEL,
        args={**body, **(extra or {})},
    )
    async with _open_tenant(request, tenant, principal) as session:
        return await commands.ingest(
            session,
            command,
            external_ref=key,
            principal=principal.session_id,
            payload=body,
        )


def _render(result: CommandResult, *, status: Optional[int] = None) -> JSONResponse:
    code = status or (202 if result.outcome == "enqueued" else 200)
    return JSONResponse(
        status_code=code,
        content=jsonable_encoder({"outcome": result.outcome, **result.data}),
    )


# --- tenant-less: the principal's own view -------------------------------


@router.get("/me")
async def me(request: Request, principal: Principal = Depends(current_principal)):
    """The user, their identities, and their memberships (through the
    memberships door, `064`). A user with zero workspaces is the normal first
    state on the greenfield, not an error."""
    engine = require_engine(request)
    async with engine.connect() as conn:
        user = await identity.get_user(conn, user_id=principal.user_id)
        memberships = await workspaces.list_for_user(conn, user_id=principal.user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="not found")
    return {"user": user, "workspaces": memberships}


@router.post("/me/telegram/link")
async def telegram_link(
    request: Request, principal: Principal = Depends(current_principal)
):
    """The link a signed-in user taps to attach their Telegram identity
    (`07` §2 `link`: only from an authenticated session; the row pins the
    user, and the bot's `/start` door attaches the tapping identity to exactly
    that user — D35). The service half is #1180; this route is what the X.3
    drive was missing (#1172, #1157).

    Tenant-less, like `/me`: an identity belongs to a user, not a workspace.
    One transaction — the state row commits before the link is handed back,
    so a tap can never arrive ahead of the row it consumes. The link is good
    for `STATE_TTL_SECONDS`; a second click mints a fresh one and retires the
    user's earlier live link (one live link per user — #1224 review).
    """
    bot_username = _target_bot_username()
    engine = require_engine(request)
    async with engine.begin() as conn:
        link = await identity_link.issue_link_state(
            conn, user_id=principal.user_id, bot_username=bot_username
        )
    return {"link": link, "expires_in_seconds": STATE_TTL_SECONDS}


@router.post("/workspaces/{ws}/telegram/bind-link")
async def telegram_group_bind_link(
    ws: uuid.UUID, request: Request, principal: Principal = Depends(current_principal)
):
    """The admin's one-shot link that binds a Telegram group to THIS workspace
    (`07` §13; owner ruling 2026-09-05). Opens Telegram's group picker; the
    `/start` door does the rest. Admin floor: where a workspace's cards go is
    an admin act (`06` §4). One live link per workspace."""
    bot_username = _target_bot_username()
    async with _admin(request, str(ws), principal) as session:
        # The link only works for the admin who minted it, proven by their
        # linked Telegram identity — so an admin who has not linked would be
        # sent into a flow that can only refuse. Say so here instead.
        if (
            await identity.identity_for_user(
                session, user_id=principal.user_id, provider=channel_bind.PROVIDER
            )
            is None
        ):
            raise HTTPException(status_code=409, detail="link_telegram_first")
        link = await channel_bind.issue_bind_state(
            session,
            user_id=principal.user_id,
            workspace_id=str(ws),
            bot_username=bot_username,
        )
    return {"link": link, "expires_in_seconds": STATE_TTL_SECONDS}


@router.get("/workspaces")
async def list_workspaces(
    request: Request, principal: Principal = Depends(current_principal)
):
    engine = require_engine(request)
    async with engine.connect() as conn:
        memberships = await workspaces.list_for_user(conn, user_id=principal.user_id)
    return {"workspaces": memberships}


@router.post("/workspaces", status_code=201)
async def create_workspace(
    request: Request, principal: Principal = Depends(current_principal)
):
    """`create_workspace`: workspace + owner membership in ONE transaction.

    The id is pre-assigned HERE and never client-supplied: the unit of work
    needs a tenant to exist at all, and `p_tenant_workspaces` keys on the
    row's own id, so the claim must precede the insert (`02` §7).
    """
    workspace_id = str(uuid.uuid4())
    result = await _dispatch(
        request,
        principal,
        tenant=workspace_id,
        kind="create_workspace",
        workspace_id=None,
        extra={"workspace_id": workspace_id},
    )
    return _render(result, status=201)


# --- workspace-scoped reads ---------------------------------------------


@router.get("/workspaces/{ws}")
async def get_workspace(
    ws: uuid.UUID, request: Request, principal: Principal = Depends(current_principal)
):
    async with _member(request, str(ws), principal) as session:
        row = await workspaces.get_workspace(session, workspace_id=str(ws))
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    return row


@router.get("/workspaces/{ws}/members")
async def list_members(
    ws: uuid.UUID, request: Request, principal: Principal = Depends(current_principal)
):
    return await _collection(request, ws, principal, workspaces.list_members, "members")


@router.get("/workspaces/{ws}/accounts")
async def list_accounts(
    ws: uuid.UUID, request: Request, principal: Principal = Depends(current_principal)
):
    return await _collection(
        request, ws, principal, workspaces.list_accounts, "accounts"
    )


@router.get("/workspaces/{ws}/sources")
async def list_sources(
    ws: uuid.UUID, request: Request, principal: Principal = Depends(current_principal)
):
    return await _collection(request, ws, principal, workspaces.list_sources, "sources")


@router.get("/workspaces/{ws}/bindings")
async def list_bindings(
    ws: uuid.UUID, request: Request, principal: Principal = Depends(current_principal)
):
    return await _collection(
        request, ws, principal, workspaces.list_bindings, "bindings"
    )


@router.get("/workspaces/{ws}/invitations")
async def list_invitations(
    ws: uuid.UUID, request: Request, principal: Principal = Depends(current_principal)
):
    return await _collection(
        request, ws, principal, workspaces.list_invitations, "invitations"
    )


def _states(state: Optional[str]) -> list[str]:
    """``?state=a,b,c`` → the closed set's members, or a 422 naming the typo."""
    if not state:
        return []
    wanted = [s.strip() for s in state.split(",") if s.strip()]
    unknown = [s for s in wanted if s not in workspaces.INTENT_STATES]
    if unknown:
        raise HTTPException(
            status_code=422, detail=f"unknown intent state(s): {unknown}"
        )
    return wanted


@router.get("/workspaces/{ws}/intents")
async def list_intents(
    ws: uuid.UUID,
    request: Request,
    principal: Principal = Depends(current_principal),
    state: Optional[str] = Query(None),
    limit: int = Query(LIST_LIMIT_DEFAULT, ge=1, le=LIST_LIMIT_MAX),
):
    """The ledger read model — X.2's "reads pending approvals from the ledger"
    is ``?state=awaiting_approval``; a history tab is
    ``?state=posted,skipped,rejected`` (one call, several states)."""
    states = _states(state)
    async with _member(request, str(ws), principal) as session:
        rows = await workspaces.list_intents(
            session, workspace_id=str(ws), states=states, limit=limit
        )
    return {"intents": rows, "limit": limit}


@router.get("/workspaces/{ws}/media")
async def list_media(
    ws: uuid.UUID,
    request: Request,
    principal: Principal = Depends(current_principal),
    state: Optional[str] = Query(None),
    never_posted: bool = Query(False),
    limit: int = Query(LIST_LIMIT_DEFAULT, ge=1, le=LIST_LIMIT_MAX),
):
    """The media pool (#1044): the workspace's library, whether or not an
    intent exists for an item."""
    if state is not None and state not in workspaces.MEDIA_STATES:
        raise HTTPException(status_code=422, detail=f"unknown media state {state!r}")
    async with _member(request, str(ws), principal) as session:
        rows = await workspaces.list_media(
            session,
            workspace_id=str(ws),
            state=state,
            never_posted=never_posted,
            limit=limit,
        )
    return {"media": rows, "limit": limit}


@router.get("/workspaces/{ws}/media/{media_id}")
async def get_media(
    ws: uuid.UUID,
    media_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(current_principal),
):
    async with _member(request, str(ws), principal) as session:
        row = await workspaces.get_media(
            session, workspace_id=str(ws), media_id=str(media_id)
        )
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    return row


@router.get("/workspaces/{ws}/stats")
async def get_stats(
    ws: uuid.UUID, request: Request, principal: Principal = Depends(current_principal)
):
    """Server-side aggregates (#1044). A bounded list cannot answer an
    aggregate question, so these are counted where the rows are."""
    async with _member(request, str(ws), principal) as session:
        return await workspaces.stats(session, workspace_id=str(ws))


@router.get("/workspaces/{ws}/intents/{intent_id}")
async def get_intent(
    ws: uuid.UUID,
    intent_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(current_principal),
):
    async with _member(request, str(ws), principal) as session:
        row = await workspaces.get_intent(
            session, workspace_id=str(ws), intent_id=str(intent_id)
        )
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    return row


# --- provisioning: a destination and a source (#1041, destination half) ---
#
# RESOURCE-shaped and NOT `POST …/commands/{command}`, because the vocabulary
# is closed — `01` §Interaction-layer port is its normative home and has no
# name for either of these (`connect_account` there is the Drive SOURCE's
# grant, F1 (a)). The Instagram grant is the resource routes below: per
# workspace to ADD a destination by connecting (owner ruling 2026-09-04), per
# destination to connect or reconnect one that already exists. See
# `provisioning`'s docstring for why only the destination is on the path to
# a closed loop.
#
# Admin floor on both (`06` §4). A member may look at the accounts and sources
# a workspace posts to and from; deciding what they are is an admin act.


@asynccontextmanager
async def _admin(request: Request, workspace_id: str, principal: Principal):
    """`_member`, at the admin floor."""
    async with _open_tenant(request, workspace_id, principal) as session:
        await tenant_resolution.authorize_member(
            session, workspace_id, principal.user_id, minimum_role="admin"
        )
        yield session


@router.post("/workspaces/{ws}/accounts", status_code=201)
async def create_account(
    ws: uuid.UUID, request: Request, principal: Principal = Depends(current_principal)
):
    """Add a destination — the Instagram handle this workspace schedules for.

    No credential and no Meta call: `ig_accounts` needs only `workspace_id` and
    `provider_account_ref`, and a workspace with `api_publishing_enabled` false
    (the default) publishes through a human rather than through the API.

    **The web no longer adds destinations this way** (owner ruling 2026-09-04:
    a destination is added by CONNECTING — `connect_workspace_account` below
    — so the handle is Instagram's word, never a second source of truth). The
    route stays as the API's typed path: the CLI, tests, and a destination
    that is deliberately parked without a login.

    **Two bodies, one row (#1089).** ``{"handle": "..."}`` is the typed path the
    CLI and tests use: there is no Meta id to send, so `create_destination`
    derives a provisional ``manual:<handle>`` reference. ``{"provider_account_ref":
    "..."}`` is the OAuth path for when a real id exists, and it still wins if
    both are sent. A request carrying NEITHER is refused as
    `account_ref_required`, unchanged.

    Creating a destination SCHEDULES it: the posting cursor is seeded so the
    clock can see the row at all (`provisioning.create_destination` explains
    why nothing else ever seeds it). What that starts is intents awaiting
    approval, not posts. Pass ``{"schedule": false}`` for a parked destination.

    Idempotent on the handle: adding one that is already a destination returns
    the existing row with ``created: false`` rather than a second schedule
    against one real feed.
    """
    body = await _json_object(request)
    schedule = body.get("schedule", True)
    if not isinstance(schedule, bool):
        raise HTTPException(status_code=400, detail="schedule must be a boolean")
    # Both values pass through RAW. Coercing a blank handle to None here would
    # be this route holding a second copy of "what counts as a handle" — the
    # thing the sibling `sources` route's comment forbids — and the copy already
    # disagreed: `{"handle": "   "}` answered `account_ref_required` while
    # `{"handle": "@"}` answered `handle_required`, one user error with two
    # reasons. `provisioning` owns presence for both columns.
    async with _admin(request, str(ws), principal) as session:
        account_id, created = await provisioning.create_destination(
            session,
            workspace_id=str(ws),
            provider_account_ref=body.get("provider_account_ref"),
            handle=body.get("handle"),
            schedule=schedule,
        )
    return JSONResponse(
        status_code=201 if created else 200,
        content={"account_id": account_id, "created": created, "scheduled": schedule},
    )


@router.post("/workspaces/{ws}/sources", status_code=201)
async def create_source(
    ws: uuid.UUID, request: Request, principal: Principal = Depends(current_principal)
):
    """Pick a Drive folder UNDER the workspace's grant (owner ruling
    2026-09-05, #1165 lean (b); `07` §15).

    `ck_sources_provider` is closed to `gdrive` and `media_items.source_id` is
    NOT NULL, so every media item hangs off a source of this shape. The folder
    is a resource (F1 (b)), idempotent under an advisory lock — see
    `provisioning.get_or_create_media_source` — and, since the grant is the
    workspace's, refused by name when there is no usable grant to read it
    with (`drive_not_connected`, 409): a folder nobody can list is not a
    source, it is a promise. With a grant the row is ARMED for its first sync
    in this transaction (`media_sync.rearm_after_connect`), which also revives
    a folder that was removed and picked again.
    """
    body = await _json_object(request)
    name = body.get("root_name")
    folder_name = body.get("folder_name")
    async with _admin(request, str(ws), principal) as session:
        grant = await workspaces.drive_status(session, workspace_id=str(ws))
        if grant["status"] != "active":
            raise HTTPException(status_code=409, detail="drive_not_connected")
        source_id, created = await provisioning.get_or_create_media_source(
            session,
            workspace_id=str(ws),
            folder_ref=body.get("folder_ref"),
            root_name=name if isinstance(name, str) and name.strip() else None,
            folder_name=(
                folder_name.strip()
                if isinstance(folder_name, str) and folder_name.strip()
                else None
            ),
        )
        await media_sync.rearm_after_connect(
            session, workspace_id=str(ws), source_id=source_id
        )
    return JSONResponse(
        status_code=201 if created else 200,
        content={"source_id": source_id, "created": created},
    )


@router.delete("/workspaces/{ws}/sources/{source_id}")
async def remove_source(
    ws: uuid.UUID,
    source_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(current_principal),
):
    """Remove a folder from the workspace's sync — a PAUSE, never a delete
    (`provisioning.pause_media_source`): the media and its history stay, and
    picking the folder again revives it. Admin floor, like adding one."""
    async with _admin(request, str(ws), principal) as session:
        paused = await provisioning.pause_media_source(
            session, workspace_id=str(ws), source_id=str(source_id)
        )
    if not paused:
        raise HTTPException(status_code=404, detail="not found")
    return {"source_id": str(source_id), "state": "paused"}


@router.get("/workspaces/{ws}/drive")
async def drive_status(
    ws: uuid.UUID, request: Request, principal: Principal = Depends(current_principal)
):
    """The workspace's Google Drive grant — presence and freshness, never a
    token (`workspaces.drive_status`). Member floor: it says whether the
    library can sync, which every member's screens render."""
    async with _member(request, str(ws), principal) as session:
        status = await workspaces.drive_status(session, workspace_id=str(ws))
    return {"drive": status}


@router.post("/workspaces/{ws}/drive/connect")
async def connect_drive(
    ws: uuid.UUID, request: Request, principal: Principal = Depends(current_principal)
):
    """Start the Drive grant for the WORKSPACE: mint the state the callback
    will consume and hand back where the browser goes (owner ruling
    2026-09-05, #1165 lean (b); `07` §15 — one Google grant per workspace,
    folders picked under it).

    An OAuth leg is a browser redirect, which the command port cannot express,
    so it lives here as a resource route at the admin floor — the floor
    `commands.ROLE_FLOOR["connect_account"]` names — and the executor stays
    the thin chat-side door (F1 (a)). The state pins the workspace as its own
    `reconnect_target`: `connect` for a workspace that has never held a grant
    and `reconnect` after that, so a stale state is retired by the next one
    (last issued wins, per workspace).
    """
    client_id, _, redirect_uri = google_client.configured(
        google_client.DRIVE_CALLBACK_PATH
    )
    async with _admin(request, str(ws), principal) as session:
        purpose = await google_drive_oauth.connect_purpose(
            session, workspace_id=str(ws)
        )
        state = await issue_state(
            session,
            purpose=purpose,
            provider=google_drive_oauth.PROVIDER,
            user_id=principal.user_id,
            workspace_id=str(ws),
            reconnect_target=str(ws),
        )
    return {
        "authorization_url": google_drive_oauth.authorization_url(
            client_id=client_id, redirect_uri=redirect_uri, state=state
        )
    }


def _drive_adapter(request: Request):
    """The Drive transport for the folder browser, reading tokens through the
    same door the worker does (`drive_credentials`). Built per request: the
    adapter is a per-call client with no lifecycle to manage."""
    return google_drive_adapter.GoogleDriveAdapter(
        token_provider=drive_credentials.provider_from_engine(require_engine(request))
    )


@router.get("/workspaces/{ws}/drive/folders")
async def list_drive_folders(
    ws: uuid.UUID,
    request: Request,
    principal: Principal = Depends(current_principal),
    parent: Optional[str] = Query(default=None),
):
    """The folders under `parent` (the Drive root when absent), read through
    the workspace's grant — what the picker shows (#1165 lean (b)).

    Admin floor, deliberately above `drive_status`'s: the grant is the
    workspace's, but the tree it lists is a PERSON's Drive, and members have
    no business browsing it. The provider call sits outside the unit of work
    (the checkpoint discipline every provider door keeps); admission and the
    status read happen first. Refusals by name: `invalid_parent` (400) before
    any request, `drive_not_connected` (409) when the workspace never
    connected, `drive_reconnect_needed` (409) when its grant is expired or
    revoked, `drive_grant_refused` (409) when Google refused a grant the
    projection thought live, `drive_unavailable` (503) when Google gave no
    usable answer, `drive_refused` (502) for a terminal answer. `SHARED_ROOT`
    as the parent lists the folders shared to the account.
    """
    async with _admin(request, str(ws), principal) as session:
        grant = await workspaces.drive_status(session, workspace_id=str(ws))
    # Admission first, then the shape check: a non-admin learns nothing about
    # the parent it sent. Then the grant, by its projected status, so "never
    # connected" and "Google now refuses the grant" are different answers
    # (review of #1246): the former is `drive_not_connected`, the latter
    # `drive_grant_refused` below, and a grant the projection already knows
    # is dead is `drive_reconnect_needed` before any request.
    if parent is not None and not (
        parent == google_drive_adapter.SHARED_ROOT
        or google_drive_adapter.is_folder_id(parent)
    ):
        raise HTTPException(status_code=400, detail="invalid_parent")
    if grant["status"] == "none":
        raise HTTPException(status_code=409, detail="drive_not_connected")
    if grant["status"] != "active":
        raise HTTPException(status_code=409, detail="drive_reconnect_needed")
    try:
        page = await _drive_adapter(request).list_folders(
            parent=parent, workspace_id=str(ws)
        )
    except media_sync.DriveCredentialDead as exc:
        raise HTTPException(status_code=409, detail="drive_grant_refused") from exc
    except (DriveRetryableError, DriveLostResponse) as exc:
        raise HTTPException(status_code=503, detail="drive_unavailable") from exc
    except DriveTerminalError as exc:
        raise HTTPException(status_code=502, detail="drive_refused") from exc
    return {
        "parent": parent or "root",
        "folders": page.folders,
        "truncated": page.truncated,
    }


@router.post("/workspaces/{ws}/accounts/connect")
async def connect_workspace_account(
    ws: uuid.UUID, request: Request, principal: Principal = Depends(current_principal)
):
    """Start the Instagram Login grant for an account that is NOT yet a
    destination here — the way a destination is ADDED (owner ruling
    2026-09-04: connecting is the add; the handle comes from Instagram, so
    nothing is typed and there is one source of truth).

    The per-destination route's shape, minus the target: the state pins the
    user and the workspace and no `ig_accounts` row, and the callback adopts
    the row this account already has here or creates a scheduled one from
    the identity Instagram returns (`provisioning.connect_destination`).
    `connect` always — with no row to be credentialed there is no reconnect
    to name, and an untargeted state retires nothing (states with no target
    are independent one-shots).
    """
    app_id, _, redirect_uri = instagram_client.configured()
    async with _admin(request, str(ws), principal) as session:
        return await _instagram_grant(
            session,
            principal=principal,
            ws=ws,
            purpose="connect",
            reconnect_target=None,
            app_id=app_id,
            redirect_uri=redirect_uri,
        )


async def _instagram_grant(
    session, *, principal, ws, purpose, reconnect_target, app_id, redirect_uri
) -> dict:
    """Mint the state and say where the browser goes — the tail both Instagram
    connect routes share. `configured()` stays with the caller so a 503 lands
    before any seam is touched."""
    state = await issue_state(
        session,
        purpose=purpose,
        provider=ig_login_oauth.PROVIDER,
        user_id=principal.user_id,
        workspace_id=str(ws),
        reconnect_target=reconnect_target,
    )
    return {
        "authorization_url": ig_login_oauth.authorization_url(
            state, redirect_uri=redirect_uri, client_id=app_id
        )
    }


@router.post("/workspaces/{ws}/accounts/{account_id}/connect")
async def connect_account(
    ws: uuid.UUID,
    account_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(current_principal),
):
    """Start the Instagram Login grant for ONE destination: mint the state the
    callback will consume and hand back where the browser goes (#1220 step 2,
    #1041). The Drive connect route's shape, exactly.

    An OAuth leg is a browser redirect, which the command port cannot express,
    so it lives here as a resource route at the admin floor — the floor
    `commands.ROLE_FLOOR["connect_account"]` names. Per-DESTINATION: the state
    pins the `ig_accounts` row in `reconnect_target`, `connect` for a row that
    has never been credentialed and `reconnect` after that, so a stale
    reconnect state is retired by the next one (last issued wins). The
    callback attaches the credential to THIS row and flips its provisional
    `manual:<handle>` reference to the real Meta id.
    """
    app_id, _, redirect_uri = instagram_client.configured()
    async with _admin(request, str(ws), principal) as session:
        purpose = await ig_login_oauth.connect_purpose(
            session, workspace_id=str(ws), ig_account_id=str(account_id)
        )
        if purpose is None:
            raise HTTPException(status_code=404, detail="not found")
        return await _instagram_grant(
            session,
            principal=principal,
            ws=ws,
            purpose=purpose,
            reconnect_target=str(account_id),
            app_id=app_id,
            redirect_uri=redirect_uri,
        )


# --- commands -----------------------------------------------------------


@router.post("/workspaces/{ws}/commands/{command}")
async def run_command(
    ws: uuid.UUID,
    command: str,
    request: Request,
    principal: Principal = Depends(current_principal),
):
    if command == "create_workspace":
        # Adapter-local: this command has its own URL, and that is the one
        # fact the port cannot know. Everything else is the port's order.
        raise HTTPException(status_code=404, detail=f"unknown command {command!r}")
    result = await _dispatch(
        request, principal, tenant=str(ws), kind=command, workspace_id=str(ws)
    )
    return _render(result)


# --- invitations: user-scoped by token ----------------------------------


@router.post("/invitations/{token}/accept")
async def accept_invitation(
    token: str, request: Request, principal: Principal = Depends(current_principal)
):
    """Possession of the one-shot token accepts; the door resolves the
    workspace itself, so this runs tenant-less (`invitations.accept`)."""
    engine = require_engine(request)
    async with engine.begin() as conn:
        return await invitations.accept(
            conn, token=token, user_id=principal.user_id, channel=CHANNEL
        )
