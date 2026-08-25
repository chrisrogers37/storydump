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

import json
import uuid
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from src.api.principal import Principal, current_principal, require_engine
from src.services.target import (
    commands,
    identity,
    invitations,
    tenant_resolution,
    workspaces,
)
from src.services.target.commands import Command, CommandResult
from src.services.target.unit_of_work import unit_of_work

router = APIRouter(tags=["v1"])

#: `command_dedup.channel` for this adapter (`webhook_ingress.CHANNELS`).
CHANNEL = "web"
#: Required on every command. The client owns the value (a UUID per intent to
#: act); the server owns nothing but the dedup row.
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


@router.get("/workspaces/{ws}/intents")
async def list_intents(
    ws: uuid.UUID,
    request: Request,
    principal: Principal = Depends(current_principal),
    state: Optional[str] = Query(None),
    limit: int = Query(LIST_LIMIT_DEFAULT, ge=1, le=LIST_LIMIT_MAX),
):
    """The ledger read model — X.2's "reads pending approvals from the ledger"
    is ``?state=awaiting_approval``."""
    async with _member(request, str(ws), principal) as session:
        rows = await workspaces.list_intents(
            session, workspace_id=str(ws), state=state, limit=limit
        )
    return {"intents": rows, "limit": limit}


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
