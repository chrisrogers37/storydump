"""The token routes and the principal's own profile (phase 01 of the v2 CLI).

Minting is session-only, on purpose and by dependency: `require_session`
refuses a token before a handler runs, so a token can never mint a token,
whatever its role. A person lists and revokes their own; an admin or owner
mints, lists and revokes the workspace's service identities (``readonly`` in
this release — F10); a service identity may list its own workspace's
identities and revoke itself, nothing else.

`GET /me/principal` is the one profile route every principal kind can read:
`GET /me` is user-shaped and a service identity has no user.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from src.api.principal import (
    Principal,
    current_principal,
    require_engine,
    require_own_workspace,
    require_session,
)
from src.api.routes import v1
from src.exceptions.tenancy import TokenRefused
from src.services.target import service_tokens, workspaces

router = APIRouter(tags=["tokens"])


def _created(payload: dict[str, Any]) -> JSONResponse:
    return JSONResponse(status_code=201, content=jsonable_encoder(payload))


def _minted(row: dict, secret: str, *, workspace: bool) -> dict[str, Any]:
    payload = {
        "id": str(row["id"]),
        "name": row["name"],
        "role": row["role"],
        "expires_at": row["expires_at"],
        "secret": secret,
    }
    if workspace:
        payload["workspace_id"] = str(row["workspace_id"])
    return payload


def _days(body: dict[str, Any]) -> Any:
    return body.get("expires_in_days", service_tokens.DEFAULT_EXPIRY_DAYS)


def _token_may_revoke(principal: Principal, token_id: str) -> None:
    """A revoke is a write. A token revokes itself freely — a kill switch —
    and anything else only with the operator role: a ``readonly`` token that
    could revoke the person's other tokens, or an admin's readonly token the
    workspace's identities, would be exactly the lever ``readonly`` denies."""
    if (
        principal.is_token
        and principal.token_role != "operator"
        and token_id != principal.token_id
    ):
        raise TokenRefused("readonly_token", "a read-only token may revoke only itself")


# --- the principal's own profile ----------------------------------------------


@router.get("/me/principal")
async def me_principal(
    request: Request, principal: Principal = Depends(current_principal)
):
    """Who is calling, for any principal kind: the kind, the person (when
    there is one), the token (when there is one) and the readable
    workspaces with the MEMBERSHIP role in each — a ``readonly`` token's
    writes are refused whatever that role says, and the token block carries
    the role that decides it."""
    token: Optional[dict[str, Any]] = None
    if principal.is_token:
        token = {
            "id": principal.token_id,
            "name": principal.token_name,
            "role": principal.token_role,
            "workspace_id": principal.token_workspace_id,
            "expires_at": principal.token_expires_at,
        }
    if principal.is_service_identity:
        ws = principal.token_workspace_id
        async with v1._open_tenant(request, ws, principal) as session:
            row = await workspaces.get_workspace(session, workspace_id=ws)
        memberships = (
            []
            if row is None
            else [{"id": ws, "name": row["name"], "role": principal.token_role}]
        )
    else:
        engine = require_engine(request)
        async with engine.connect() as conn:
            rows = await workspaces.list_for_user(conn, user_id=principal.user_id)
        memberships = [
            {"id": str(r["id"]), "name": r["name"], "role": r["role"]} for r in rows
        ]
    return jsonable_encoder(
        {
            "kind": principal.kind,
            "user_id": principal.user_id,
            "token": token,
            "workspaces": memberships,
        }
    )


# --- a person's own tokens ------------------------------------------------------


@router.post("/me/tokens")
async def mint_my_token(
    request: Request, principal: Principal = Depends(require_session)
):
    """Mint a person-bound token: the secret is in this answer and nowhere
    else, ever."""
    body = await v1._json_object(request)
    engine = require_engine(request)
    async with engine.begin() as conn:
        secret, row = await service_tokens.mint(
            conn,
            name=body.get("name"),
            role=body.get("role"),
            user_id=principal.user_id,
            expires_in_days=_days(body),
        )
    return _created(_minted(row, secret, workspace=False))


@router.get("/me/tokens")
async def list_my_tokens(
    request: Request, principal: Principal = Depends(current_principal)
):
    """A person's tokens — a session or a person-bound token; a service
    identity has no person and therefore no personal tokens."""
    if principal.is_service_identity:
        raise TokenRefused("session_required", "a service identity has no person")
    engine = require_engine(request)
    async with engine.connect() as conn:
        rows = await service_tokens.list_for_user(conn, user_id=principal.user_id)
    return jsonable_encoder({"tokens": rows})


@router.delete("/me/tokens/{token_id}")
async def revoke_my_token(
    token_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(current_principal),
):
    """Revoke one of the person's own tokens. Somebody else's reads as not
    found — the same answer as an unknown id, on purpose."""
    if principal.is_service_identity:
        raise TokenRefused("session_required", "a service identity has no person")
    _token_may_revoke(principal, str(token_id))
    engine = require_engine(request)
    async with engine.begin() as conn:
        revoked = await service_tokens.revoke(
            conn, token_id=str(token_id), user_id=principal.user_id
        )
    if not revoked:
        raise HTTPException(status_code=404, detail="not found")
    return {"revoked": True}


# --- a workspace's service identities ---------------------------------------


@router.post("/workspaces/{ws}/tokens")
async def mint_service_identity(
    ws: uuid.UUID, request: Request, principal: Principal = Depends(require_session)
):
    """An admin or owner mints a workspace service identity. The role is
    ``readonly`` whatever the body says (F10): a service identity reads."""
    body = await v1._json_object(request)
    async with v1._admin(request, str(ws), principal) as session:
        secret, row = await service_tokens.mint(
            session,
            name=body.get("name"),
            role="readonly",
            workspace_id=str(ws),
            expires_in_days=_days(body),
        )
    return _created(_minted(row, secret, workspace=True))


@router.get("/workspaces/{ws}/tokens")
async def list_service_identities(
    ws: uuid.UUID, request: Request, principal: Principal = Depends(current_principal)
):
    """The workspace's service identities: an admin or owner through the
    gate; a service identity for its own workspace only, and nothing to gate
    on because it has no membership."""
    if principal.is_service_identity:
        require_own_workspace(principal, str(ws))
        async with v1._open_tenant(request, str(ws), principal) as session:
            rows = await service_tokens.list_for_workspace(
                session, workspace_id=str(ws)
            )
    else:
        async with v1._admin(request, str(ws), principal) as session:
            rows = await service_tokens.list_for_workspace(
                session, workspace_id=str(ws)
            )
    return jsonable_encoder({"tokens": rows})


@router.delete("/workspaces/{ws}/tokens/{token_id}")
async def revoke_service_identity(
    ws: uuid.UUID,
    token_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(current_principal),
):
    """Revoke a workspace service identity: an admin or owner any of them; a
    service identity only itself (a kill switch, never a lever over the
    others)."""
    token_id = str(token_id)
    if principal.is_service_identity:
        require_own_workspace(principal, str(ws))
        if token_id != principal.token_id:
            raise TokenRefused(
                "readonly_token", "a service identity may revoke only itself"
            )
        async with v1._open_tenant(request, str(ws), principal) as session:
            revoked = await service_tokens.revoke(
                session, token_id=token_id, workspace_id=str(ws)
            )
    else:
        _token_may_revoke(principal, token_id)
        async with v1._admin(request, str(ws), principal) as session:
            revoked = await service_tokens.revoke(
                session, token_id=token_id, workspace_id=str(ws)
            )
    if not revoked:
        raise HTTPException(status_code=404, detail="not found")
    return {"revoked": True}
