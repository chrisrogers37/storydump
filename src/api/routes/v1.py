"""The `/api/v1` surface: reads as resources, writes as the closed vocabulary.

The shape is #1015's router design (§1–§3), built rather than described:

- **Reads are resources** because `01` says the web surface is *pull* — it
  reads ledger state, it does not receive interaction requests. Every read is
  workspace-keyed in the URL and passes the ONE gate
  (`tenant_resolution.authorize_member`) before touching a row; a workspace
  the caller is not a member of answers **404, never 403** (`07` §5, no
  existence oracle — the same 404 a workspace that does not exist gets).
- **Writes are the `01` vocabulary made literal**: one route,
  ``POST /workspaces/{ws}/commands/{command}``, whose path segment is validated
  against `commands.VOCABULARY`. The route table therefore cannot drift from
  the vocabulary, and W4's Telegram adapter will hand the same `Command`
  objects to the same `commands.execute`. ``create_workspace`` — the one
  command with no workspace yet — has its own route.
- **Admission before execution, in one transaction** (`webhook_ingress`'s
  ordering rule, applied to the second channel it predicted): an unknown
  command is refused cold, then the ``Idempotency-Key`` is admitted into
  `command_dedup` keyed on ``(web, session id, key)``, then the port runs. A
  refusal anywhere rolls the dedup row back with everything else, so a retry
  re-executes; a true replay answers ``200 {"outcome": "replayed"}`` without
  running anything; the same key with a different body is a 409.

The tenant-less reads (``/me``, ``/workspaces``) run on a raw connection: the
unit of work is unconstructible without a tenant by design, and these are
user-plane reads. **Known, measured, not papered over:** under the production
role (`svc_ingress`) `workspace_members`/`workspaces` are tenant-keyed RLS
tables with no user-plane read path, so `list_for_user` returns nothing there
until a door or policy lands (`02` §7 amendment — see `workspaces.py` and the
strict-xfail pin in the X.2 gate test). The routes are written as the plan
reads and the gap is reported, because a route that hid it would be the
harder bug to find later.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.api.principal import Principal, current_principal, require_engine
from src.services.target import (
    commands,
    identity,
    tenant_resolution,
    webhook_ingress,
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


async def _admit(session, principal: Principal, key: str, body: dict) -> None:
    await webhook_ingress.admit(
        session,
        channel=CHANNEL,
        external_ref=key,
        payload=body,
        principal=principal.session_id,
    )


def _render(result: CommandResult, *, status: Optional[int] = None) -> JSONResponse:
    code = status or (202 if result.outcome == "enqueued" else 200)
    return JSONResponse(
        status_code=code,
        content=jsonable_encoder({"outcome": result.outcome, **result.data}),
    )


def _pgcode(exc: DBAPIError) -> Optional[str]:
    orig = exc.orig
    return getattr(orig, "pgcode", None) or getattr(
        getattr(orig, "__cause__", None), "sqlstate", None
    )


# --- tenant-less: the principal's own view -------------------------------


@router.get("/me")
async def me(request: Request, principal: Principal = Depends(current_principal)):
    """The user, their identities, and their memberships. A user with zero
    workspaces is the normal first state on the greenfield, not an error."""
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
    body = await _json_object(request)
    key = _idempotency_key(request)
    workspace_id = str(uuid.uuid4())
    async with _open_tenant(request, workspace_id, principal) as session:
        await _admit(session, principal, key, body)
        result = await commands.execute(
            session,
            Command(
                kind="create_workspace",
                workspace_id=None,
                actor_user_id=principal.user_id,
                channel=CHANNEL,
                args={**body, "workspace_id": workspace_id},
            ),
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
    async with _member(request, str(ws), principal) as session:
        rows = await workspaces.list_members(session, workspace_id=str(ws))
    return {"members": rows}


@router.get("/workspaces/{ws}/accounts")
async def list_accounts(
    ws: uuid.UUID, request: Request, principal: Principal = Depends(current_principal)
):
    async with _member(request, str(ws), principal) as session:
        rows = await workspaces.list_accounts(session, workspace_id=str(ws))
    return {"accounts": rows}


@router.get("/workspaces/{ws}/sources")
async def list_sources(
    ws: uuid.UUID, request: Request, principal: Principal = Depends(current_principal)
):
    async with _member(request, str(ws), principal) as session:
        rows = await workspaces.list_sources(session, workspace_id=str(ws))
    return {"sources": rows}


@router.get("/workspaces/{ws}/bindings")
async def list_bindings(
    ws: uuid.UUID, request: Request, principal: Principal = Depends(current_principal)
):
    async with _member(request, str(ws), principal) as session:
        rows = await workspaces.list_bindings(session, workspace_id=str(ws))
    return {"bindings": rows}


@router.get("/workspaces/{ws}/invitations")
async def list_invitations(
    ws: uuid.UUID, request: Request, principal: Principal = Depends(current_principal)
):
    async with _member(request, str(ws), principal) as session:
        rows = await workspaces.list_invitations(session, workspace_id=str(ws))
    return {"invitations": rows}


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
    """Refuse cold → admit → gate → execute, one transaction (module doc)."""
    if command not in commands.ROLE_FLOOR or command == "create_workspace":
        # Before admission on purpose: nothing about an unknown name is
        # worth a dedup row, and `create_workspace` has its own route.
        raise HTTPException(status_code=404, detail=f"unknown command {command!r}")
    body = await _json_object(request)
    key = _idempotency_key(request)
    async with _open_tenant(request, str(ws), principal) as session:
        await _admit(session, principal, key, body)
        result = await commands.execute(
            session,
            Command(
                kind=command,
                workspace_id=str(ws),
                actor_user_id=principal.user_id,
                channel=CHANNEL,
                args=body,
            ),
        )
    return _render(result)


# --- invitations: user-scoped by token ----------------------------------


@router.post("/invitations/{token}/accept")
async def accept_invitation(
    token: str, request: Request, principal: Principal = Depends(current_principal)
):
    """The `fn_invitation_accept` door (`059`): possession of the one-shot
    token accepts; the door resolves the workspace, sets its own actor GUCs,
    and evaluates D33's identity proof against the caller's verified email.
    Its two named refusals map here; anything else is a real error."""
    engine = require_engine(request)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    async with engine.begin() as conn:
        user = await identity.get_user(conn, user_id=principal.user_id)
        email = (user or {}).get("primary_email")
        try:
            row = (
                await conn.execute(
                    text(
                        "SELECT o_workspace_id, o_granted_role, o_matched"
                        "  FROM fn_invitation_accept(:h, :u, 'google', :email, NULL, :ch)"
                    ),
                    {
                        "h": token_hash,
                        "u": principal.user_id,
                        "email": email,
                        "ch": CHANNEL,
                    },
                )
            ).first()
        except DBAPIError as exc:
            code = _pgcode(exc)
            if code == "P0002":  # no_data_found: used, revoked, expired, unknown
                raise HTTPException(
                    status_code=404, detail="invitation not acceptable"
                ) from exc
            if code == "23514":  # check_violation: identity proof mismatch (D33)
                raise HTTPException(
                    status_code=403, detail="identity proof mismatch"
                ) from exc
            raise
    if row is None:
        raise HTTPException(status_code=404, detail="invitation not acceptable")
    return {"workspace_id": str(row[0]), "role": row[1], "matched": bool(row[2])}
