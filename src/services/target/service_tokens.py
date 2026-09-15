"""Service tokens — `07` §6, the bearer credential of the CLI and of automation.

One table, two principal kinds (plan `2026-09-15-cli-v2`, phase 01):

* a **person-bound** token (``user_id`` set, ``workspace_id`` NULL) acts as
  that person in every workspace they belong to, never above their
  membership role, and is audited as the person with the token's name as
  the client label;
* a **workspace service identity** (``workspace_id`` set, ``user_id`` NULL)
  reads its one workspace under its own name and, in this release, never
  writes (fork F10: the command port authorizes writes by membership, and a
  service identity has none).

``ck_service_token_subject`` (077) makes "exactly one subject" a database
fact. The secret is ``sdt_`` + 32 url-safe random bytes, shown once by the
minting route and stored only as its SHA-256 — the pattern sessions use — so
a database read never yields a usable credential. Minting is the caller's
responsibility to gate (a signed-in session only; never a migration, a
script, or another token); this module does not know who is asking.

The resolver is the ONE ingress gate for a token. Its refusals are checked
in the order that discloses least, and a disabled person's token dies here
exactly as their session would (`sessions.resolve`): a token must never
outlive the account it acts for. Every authenticated use stamps ``last_used_at``,
throttled like the session slide so a chatty client is one write a minute,
not one per request.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import text

from src.exceptions import StorydumpError
from src.exceptions.tenancy import TenantResolutionError
from src.services.target import vocabulary

#: The prefix every secret carries; the resolver routes on it.
TOKEN_PREFIX = vocabulary.TOKEN_PREFIX
#: A token expires unless the minter says otherwise; the ceiling is a year.
DEFAULT_EXPIRY_DAYS = 90
MAX_EXPIRY_DAYS = 365
NAME_MAX = 80
#: `last_used_at` is stamped at most this often (the session slide's throttle).
STAMP_THROTTLE_SECONDS = 60


class TokenArgsInvalid(StorydumpError):
    """A mint or revoke with arguments the table would refuse — named before
    any SQL runs, so the API answers 400 with a reason rather than a 500."""


@dataclass(frozen=True)
class TokenPrincipal:
    """A resolved token: who it acts for, or which workspace it reads."""

    token_id: str
    name: str
    role: str
    user_id: Optional[str]
    workspace_id: Optional[str]
    expires_at: Optional[datetime]


def new_secret() -> str:
    """``sdt_`` + 32 url-safe random bytes (43 characters)."""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def token_hash(value: str) -> str:
    """SHA-256 of the whole presented value, prefix included."""
    return hashlib.sha256(value.encode()).hexdigest()


def is_token(value: str) -> bool:
    """Whether a presented bearer value is a token (else it is a session)."""
    return value.startswith(TOKEN_PREFIX)


def _one_subject(user_id: Optional[str], workspace_id: Optional[str]) -> None:
    if (user_id is None) == (workspace_id is None):
        raise TokenArgsInvalid(
            "a token has exactly one subject: a person, or a workspace"
        )


_MINT = text(
    "INSERT INTO service_tokens"
    "  (name, token_hash, role, user_id, workspace_id, expires_at)"
    " VALUES (:name, :h, :role, :uid, :ws, now() + make_interval(days => :days))"
    " RETURNING id, name, role, user_id, workspace_id, expires_at, created_at"
)


async def mint(
    executor,
    *,
    name: Any,
    role: Any,
    user_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    expires_in_days: Any = DEFAULT_EXPIRY_DAYS,
) -> tuple[str, dict]:
    """Mint one token. Returns ``(secret, row)`` — the secret exactly once.

    Validation happens here, before SQL: the table's CHECKs would refuse the
    same things, but a CHECK violation is a 500 to the caller and a named
    refusal is a 400 with a reason.
    """
    _one_subject(user_id, workspace_id)
    clean = str(name).strip() if isinstance(name, str) else ""
    if not clean or len(clean) > NAME_MAX:
        raise TokenArgsInvalid(f"name must be 1 to {NAME_MAX} characters")
    if role not in vocabulary.TOKEN_ROLES:
        raise TokenArgsInvalid(
            f"role must be one of {', '.join(vocabulary.TOKEN_ROLES)}"
        )
    if (
        isinstance(expires_in_days, bool)
        or not isinstance(expires_in_days, int)
        or not 1 <= expires_in_days <= MAX_EXPIRY_DAYS
    ):
        raise TokenArgsInvalid(f"expires_in_days must be 1 to {MAX_EXPIRY_DAYS}")
    secret = new_secret()
    row = (
        (
            await executor.execute(
                _MINT,
                {
                    "name": clean,
                    "h": token_hash(secret),
                    "role": role,
                    "uid": user_id,
                    "ws": workspace_id,
                    "days": expires_in_days,
                },
            )
        )
        .mappings()
        .first()
    )
    return secret, dict(row)


#: One statement for a person-bound token: the row with its person's state,
#: and the stamp, throttled and gated on the person being active. The stamp is a CTE over the same lookup so a
#: dead token is never stamped and a live one costs one round trip. The
#: workspace is NOT joined here: `workspaces` is tenant-plane, and on the
#: authentication connection no tenant is claimed yet, so RLS would hide the
#: row and read as "no workspace" — the service identity's state check is
#: its own tenant-scoped read below.
_RESOLVE = text(
    "WITH t AS ("
    "  SELECT s.id, s.name, s.role, s.user_id, s.workspace_id, s.expires_at,"
    "         s.revoked_at IS NOT NULL AS revoked,"
    "         (s.expires_at IS NOT NULL AND s.expires_at <= now()) AS expired,"
    "         u.state AS user_state"
    "    FROM service_tokens s"
    "    LEFT JOIN users u ON u.id = s.user_id"
    "   WHERE s.token_hash = :h"
    "), stamp AS ("
    "  UPDATE service_tokens s SET last_used_at = now()"
    "    FROM t"
    "   WHERE s.id = t.id AND NOT t.revoked AND NOT t.expired"
    "     AND t.user_id IS NOT NULL AND t.user_state = 'active'"
    "     AND (s.last_used_at IS NULL"
    "          OR s.last_used_at < now() - make_interval(secs => :throttle))"
    "  RETURNING s.id"
    ")"
    " SELECT id, name, role, user_id, workspace_id, expires_at, revoked, expired,"
    "        user_state"
    "   FROM t"
)

#: A service identity's workspace, read under its own tenant claim (the
#: transaction-local GUC `p_tenant` filters on); two statements, because a
#: `set_config` in the same WHERE would run after the policy's quals. Its
#: stamp comes after that read passes: `last_used_at` means "authenticated".
#: A refused attempt never persists a stamp either way — the resolver runs
#: inside the authentication transaction, which rolls back on refusal — and
#: the guards make that reading visible in the statements themselves.
_CLAIM_TENANT = text("SELECT set_config('app.tenant_id', :ws, true)")
_WORKSPACE_STATE = text("SELECT state FROM workspaces WHERE id = :ws")
_STAMP = text(
    "UPDATE service_tokens SET last_used_at = now()"
    " WHERE id = :id AND (last_used_at IS NULL"
    "   OR last_used_at < now() - make_interval(secs => :throttle))"
)


async def resolve(executor, *, token_hash: str) -> TokenPrincipal:
    """Authenticate a presented value (already hashed) and stamp its use.

    Raises `TenantResolutionError` with the reason that names why, in the
    order that discloses least — the API maps every one of them to the same
    401 — and refuses a person-bound token whose person is not ``active``
    and a service identity whose workspace is not ``active``.
    """
    row = (
        (
            await executor.execute(
                _RESOLVE, {"h": token_hash, "throttle": STAMP_THROTTLE_SECONDS}
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise TenantResolutionError("invalid_token")
    if row["revoked"]:
        raise TenantResolutionError("revoked_token")
    if row["expired"]:
        raise TenantResolutionError("expired_token")
    if row["user_id"] is not None and row["user_state"] != "active":
        raise TenantResolutionError("disabled_user")
    if row["workspace_id"] is not None:
        ws = str(row["workspace_id"])
        await executor.execute(_CLAIM_TENANT, {"ws": ws})
        state = (
            (await executor.execute(_WORKSPACE_STATE, {"ws": ws})).mappings().first()
        )
        if state is None or state["state"] != "active":
            raise TenantResolutionError("invalid_token", "workspace is not active")
        await executor.execute(
            _STAMP, {"id": str(row["id"]), "throttle": STAMP_THROTTLE_SECONDS}
        )
    return TokenPrincipal(
        token_id=str(row["id"]),
        name=row["name"],
        role=row["role"],
        user_id=None if row["user_id"] is None else str(row["user_id"]),
        workspace_id=(
            None if row["workspace_id"] is None else str(row["workspace_id"])
        ),
        expires_at=row["expires_at"],
    )


_COLUMNS = (
    "id, name, role, workspace_id, expires_at, revoked_at, last_used_at, created_at"
)


async def list_for_user(executor, *, user_id: str) -> list[dict]:
    """A person's own tokens, newest first; never the hash."""
    rows = (
        (
            await executor.execute(
                text(
                    f"SELECT {_COLUMNS} FROM service_tokens"
                    " WHERE user_id = :u ORDER BY created_at DESC"
                ),
                {"u": user_id},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


async def list_for_workspace(executor, *, workspace_id: str) -> list[dict]:
    """A workspace's service identities, newest first; never the hash."""
    rows = (
        (
            await executor.execute(
                text(
                    f"SELECT {_COLUMNS} FROM service_tokens"
                    " WHERE workspace_id = :ws ORDER BY created_at DESC"
                ),
                {"ws": workspace_id},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


async def revoke(
    executor,
    *,
    token_id: str,
    user_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> bool:
    """Revoke one token within its subject. True if a live row was revoked;
    False when there was nothing live to revoke under that subject (already
    revoked, unknown, or somebody else's — one answer, on purpose)."""
    _one_subject(user_id, workspace_id)
    if user_id is not None:
        statement = text(
            "UPDATE service_tokens SET revoked_at = now()"
            " WHERE id = :id AND revoked_at IS NULL AND user_id = :u"
        )
        params = {"id": token_id, "u": user_id}
    else:
        statement = text(
            "UPDATE service_tokens SET revoked_at = now()"
            " WHERE id = :id AND revoked_at IS NULL AND workspace_id = :ws"
        )
        params = {"id": token_id, "ws": workspace_id}
    result = await executor.execute(statement, params)
    return result.rowcount == 1
