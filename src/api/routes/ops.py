"""The `/ops` read views (phase 02 of the v2 CLI; fork F6): one file the
security review reads.

Every route opens the tenant's unit of work under the principal — a member
through the one gate, a service identity for its own workspace and no
membership to gate on — calls one view from `ops_views`, and answers the
phase-01 envelope. The routes are admitted to tokens (`TOKEN_ROUTES`), read
only, and bounded: `floating` by a clamped limit, the others by a window.
`posture` is not tenant data and opens no tenant.
"""

from __future__ import annotations

import datetime as dt
import uuid
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder

from src.api.principal import (
    Principal,
    current_principal,
    require_engine,
    require_own_workspace,
)
from src.api import principal as principal_mod
from src.services.target import ops_views, vocabulary

router = APIRouter(tags=["ops"])


def parse_since(value: str, *, now: Optional[dt.datetime] = None) -> dt.datetime:
    """The vocabulary's one window grammar (`vocabulary.window_start`): a
    span back from now or an ISO-8601 timestamp, at most 30 days, never in
    the future. ``ValueError`` names what was wrong — the route answers 422
    rather than guessing a window."""
    return vocabulary.window_start(value, now or dt.datetime.now(dt.timezone.utc))


def _window(since: Optional[str]) -> dt.datetime:
    try:
        return parse_since(since or vocabulary.DEFAULT_WINDOW)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@asynccontextmanager
async def _scoped(request: Request, ws: uuid.UUID, principal: Principal):
    """The workspace's unit of work for this principal: a service identity
    reads its own workspace with nothing to gate on; everyone else passes
    the membership gate."""
    if principal.is_service_identity:
        require_own_workspace(principal, str(ws))
        async with principal_mod.open_tenant(request, str(ws), principal) as session:
            yield session
        return
    async with principal_mod.member_session(request, str(ws), principal) as session:
        yield session


def _envelope(kind: str, ws: uuid.UUID, rows: list[dict[str, Any]]) -> Any:
    return jsonable_encoder(
        vocabulary.envelope(kind, {"workspace_id": str(ws), "rows": rows})
    )


@router.get("/ops/workspaces/{ws}/story/{intent_id}")
async def story(
    ws: uuid.UUID,
    intent_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(current_principal),
):
    async with _scoped(request, ws, principal) as session:
        rows = await ops_views.story(
            session, workspace_id=str(ws), intent_id=str(intent_id)
        )
    return _envelope("story", ws, rows)


@router.get("/ops/workspaces/{ws}/cards/{intent_id}")
async def cards(
    ws: uuid.UUID,
    intent_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(current_principal),
):
    async with _scoped(request, ws, principal) as session:
        rows = await ops_views.cards(
            session, workspace_id=str(ws), intent_id=str(intent_id)
        )
    return _envelope("cards", ws, rows)


@router.get("/ops/workspaces/{ws}/floating")
async def floating(
    ws: uuid.UUID,
    request: Request,
    limit: int = Query(ops_views.FLOATING_LIMIT, ge=1, le=ops_views.FLOATING_LIMIT_MAX),
    principal: Principal = Depends(current_principal),
):
    async with _scoped(request, ws, principal) as session:
        rows = await ops_views.floating(session, workspace_id=str(ws), limit=limit)
    return _envelope("floating", ws, rows)


@router.get("/ops/workspaces/{ws}/account/{key}")
async def account(
    ws: uuid.UUID,
    key: str,
    request: Request,
    principal: Principal = Depends(current_principal),
):
    async with _scoped(request, ws, principal) as session:
        rows = await ops_views.account(session, workspace_id=str(ws), key=key)
    return _envelope("account", ws, rows)


@router.get("/ops/workspaces/{ws}/jobs")
async def jobs(
    ws: uuid.UUID,
    request: Request,
    since: Optional[str] = Query(None),
    principal: Principal = Depends(current_principal),
):
    window = _window(since)
    async with _scoped(request, ws, principal) as session:
        rows = await ops_views.jobs(session, workspace_id=str(ws), since=window)
    return _envelope("jobs", ws, rows)


@router.get("/ops/workspaces/{ws}/outbox")
async def outbox(
    ws: uuid.UUID,
    request: Request,
    since: Optional[str] = Query(None),
    principal: Principal = Depends(current_principal),
):
    window = _window(since)
    async with _scoped(request, ws, principal) as session:
        rows = await ops_views.outbox(session, workspace_id=str(ws), since=window)
    return _envelope("outbox", ws, rows)


@router.get("/ops/workspaces/{ws}/burst")
async def burst(
    ws: uuid.UUID,
    request: Request,
    since: Optional[str] = Query(None),
    principal: Principal = Depends(current_principal),
):
    window = _window(since)
    async with _scoped(request, ws, principal) as session:
        rows = await ops_views.burst(session, workspace_id=str(ws), since=window)
    return _envelope("burst", ws, rows)


@router.get("/ops/posture")
async def posture(request: Request, principal: Principal = Depends(current_principal)):
    """Catalogs and the runner's ledger — any principal, no tenant."""
    engine = require_engine(request)
    async with engine.connect() as conn:
        data = await ops_views.posture(conn)
    return jsonable_encoder(vocabulary.envelope("posture", data))
