"""Service tokens (`07` §6; phase 01 of the v2 CLI): the mint's shape and the
resolver's gate order.

The resolver is the ONE ingress gate for a bearer token, so its refusals are
checked in the order that discloses least (an unknown hash reads like a
revoked one to the caller) and a disabled person's token dies here exactly as
their session would — a token must never outlive the account it acts for.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from src.exceptions.tenancy import TenantResolutionError
from src.services.target import service_tokens, vocabulary

USER = str(uuid.uuid4())
WS = str(uuid.uuid4())
TOKEN = str(uuid.uuid4())
LATER = datetime.now(timezone.utc) + timedelta(days=30)


class _Rows:
    def __init__(self, rows, rowcount=None):
        self._rows = rows
        self.rowcount = len(rows) if rowcount is None else rowcount

    def mappings(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class _Executor:
    """Records every (sql, params) and answers from a queue of row lists."""

    def __init__(self, *answers):
        self.answers = [a if isinstance(a, _Rows) else _Rows(a) for a in answers]
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), dict(params or {})))
        return self.answers.pop(0) if self.answers else _Rows([])


def _live_row(**over):
    row = {
        "id": TOKEN,
        "name": "claude-code",
        "role": "operator",
        "user_id": USER,
        "workspace_id": None,
        "expires_at": LATER,
        "revoked": False,
        "expired": False,
        "user_state": "active",
    }
    row.update(over)
    return row


class TestTheSecret:
    def test_a_secret_carries_the_prefix_and_43_url_safe_characters(self):
        secret = service_tokens.new_secret()
        assert secret.startswith(vocabulary.TOKEN_PREFIX)
        assert len(secret) == len(vocabulary.TOKEN_PREFIX) + 43
        assert service_tokens.is_token(secret)
        assert not service_tokens.is_token("opaque-session-value")

    def test_two_secrets_differ(self):
        assert service_tokens.new_secret() != service_tokens.new_secret()

    def test_the_hash_is_sha256_of_the_whole_value(self):
        assert (
            service_tokens.token_hash("sdt_abc")
            == hashlib.sha256(b"sdt_abc").hexdigest()
        )


class TestMint:
    @pytest.mark.asyncio
    async def test_stores_only_the_hash_and_returns_the_secret_once(self):
        ex = _Executor(
            [
                {
                    "id": TOKEN,
                    "name": "claude-code",
                    "role": "operator",
                    "user_id": USER,
                    "workspace_id": None,
                    "expires_at": LATER,
                    "created_at": LATER,
                }
            ]
        )
        secret, row = await service_tokens.mint(
            ex, name="  claude-code ", role="operator", user_id=USER
        )
        assert secret.startswith("sdt_")
        (call,) = ex.calls
        sql, params = call
        assert "INSERT INTO service_tokens" in sql
        assert params["h"] == service_tokens.token_hash(secret)
        assert secret not in sql and secret not in params.values()
        assert params["name"] == "claude-code"
        assert params["days"] == service_tokens.DEFAULT_EXPIRY_DAYS
        assert params["uid"] == USER and params["ws"] is None
        assert row["id"] == TOKEN and "token_hash" not in row

    @pytest.mark.asyncio
    async def test_a_service_identity_carries_its_workspace(self):
        ex = _Executor([{"id": TOKEN, "name": "ops", "role": "readonly"}])
        await service_tokens.mint(
            ex, name="ops", role="readonly", workspace_id=WS, expires_in_days=7
        )
        (_, params) = ex.calls[0]
        assert params["uid"] is None and params["ws"] == WS and params["days"] == 7

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "kwargs",
        [
            dict(name="x", role="operator"),  # no subject
            dict(name="x", role="operator", user_id=USER, workspace_id=WS),  # two
            dict(name="   ", role="operator", user_id=USER),
            dict(name="n" * 81, role="operator", user_id=USER),
            dict(name="x", role="root", user_id=USER),
            dict(name="x", role="operator", user_id=USER, expires_in_days=0),
            dict(name="x", role="operator", user_id=USER, expires_in_days=366),
            dict(name="x", role="operator", user_id=USER, expires_in_days="90"),
        ],
    )
    async def test_refuses_bad_arguments_before_any_sql(self, kwargs):
        ex = _Executor()
        with pytest.raises(service_tokens.TokenArgsInvalid):
            await service_tokens.mint(ex, **kwargs)
        assert ex.calls == []


class TestResolve:
    @pytest.mark.asyncio
    async def test_unknown_hash_is_invalid_token(self):
        with pytest.raises(TenantResolutionError) as exc:
            await service_tokens.resolve(_Executor([]), token_hash="h")
        assert exc.value.reason == "invalid_token"

    @pytest.mark.asyncio
    async def test_revoked_wins_over_expired(self):
        ex = _Executor([_live_row(revoked=True, expired=True)])
        with pytest.raises(TenantResolutionError) as exc:
            await service_tokens.resolve(ex, token_hash="h")
        assert exc.value.reason == "revoked_token"

    @pytest.mark.asyncio
    async def test_expired_is_expired_token(self):
        ex = _Executor([_live_row(expired=True)])
        with pytest.raises(TenantResolutionError) as exc:
            await service_tokens.resolve(ex, token_hash="h")
        assert exc.value.reason == "expired_token"

    @pytest.mark.asyncio
    async def test_a_disabled_person_cannot_act_through_a_token(self):
        ex = _Executor([_live_row(user_state="disabled")])
        with pytest.raises(TenantResolutionError) as exc:
            await service_tokens.resolve(ex, token_hash="h")
        assert exc.value.reason == "disabled_user"

    @pytest.mark.asyncio
    async def test_a_service_identity_needs_an_active_workspace(self):
        ex = _Executor(
            [
                _live_row(
                    user_id=None, user_state=None, workspace_id=WS, role="readonly"
                )
            ],
            [],
            [{"state": "offboarding"}],
        )
        with pytest.raises(TenantResolutionError) as exc:
            await service_tokens.resolve(ex, token_hash="h")
        assert exc.value.reason == "invalid_token"

    @pytest.mark.asyncio
    async def test_a_service_identity_whose_workspace_is_invisible_is_refused(self):
        """No row under the tenant claim reads as not active, never as ok."""
        ex = _Executor(
            [
                _live_row(
                    user_id=None, user_state=None, workspace_id=WS, role="readonly"
                )
            ],
            [],
            [],
        )
        with pytest.raises(TenantResolutionError) as exc:
            await service_tokens.resolve(ex, token_hash="h")
        assert exc.value.reason == "invalid_token"

    @pytest.mark.asyncio
    async def test_a_live_person_bound_token_resolves_and_is_stamped(self):
        ex = _Executor([_live_row()])
        principal = await service_tokens.resolve(ex, token_hash="h")
        assert principal == service_tokens.TokenPrincipal(
            token_id=TOKEN,
            name="claude-code",
            role="operator",
            user_id=USER,
            workspace_id=None,
            expires_at=LATER,
        )
        (sql, params) = ex.calls[0]
        assert params["h"] == "h"
        assert "last_used_at" in sql, "every use stamps last_used_at (07 §6)"

    @pytest.mark.asyncio
    async def test_a_live_service_identity_resolves_with_no_user(self):
        ex = _Executor(
            [
                _live_row(
                    user_id=None,
                    user_state=None,
                    workspace_id=WS,
                    role="readonly",
                    name="ops",
                )
            ],
            [],
            [{"state": "active"}],
        )
        principal = await service_tokens.resolve(ex, token_hash="h")
        assert principal.user_id is None
        assert principal.workspace_id == WS and principal.role == "readonly"
        assert principal.name == "ops"
        assert len(ex.calls) == 4
        assert "set_config('app.tenant_id'" in ex.calls[1][0]
        assert ex.calls[1][1] == {"ws": WS} and ex.calls[2][1] == {"ws": WS}
        assert "workspaces" not in ex.calls[0][0], (
            "the token lookup never joins the tenant plane"
        )
        stamp_sql, stamp_params = ex.calls[3]
        assert "SET last_used_at = now()" in stamp_sql
        assert stamp_params == {"id": TOKEN, "throttle": 60}, (
            "a service identity is stamped only once its workspace read passed"
        )

    @pytest.mark.asyncio
    async def test_a_refused_service_identity_is_never_stamped(self):
        ex = _Executor(
            [
                _live_row(
                    user_id=None, user_state=None, workspace_id=WS, role="readonly"
                )
            ],
            [],
            [{"state": "suspended"}],
        )
        with pytest.raises(TenantResolutionError):
            await service_tokens.resolve(ex, token_hash="h")
        assert len(ex.calls) == 3
        assert not any("last_used_at = now()" in sql for sql, _ in ex.calls[1:])

    def test_the_person_bound_stamp_is_gated_on_the_person_being_active(self):
        """`last_used_at` means "authenticated": the one-statement stamp for
        a person-bound token must not fire for a disabled person's token."""
        sql = str(service_tokens._RESOLVE)
        stamp = sql[sql.index("UPDATE service_tokens") :]
        assert "NOT t.revoked AND NOT t.expired" in stamp
        assert "t.user_id IS NOT NULL AND t.user_state = 'active'" in stamp

    @pytest.mark.asyncio
    async def test_a_person_bound_lookup_is_one_statement(self):
        ex = _Executor([_live_row()])
        await service_tokens.resolve(ex, token_hash="h")
        assert len(ex.calls) == 1


class TestListAndRevoke:
    @pytest.mark.asyncio
    async def test_list_for_user_filters_on_the_person(self):
        ex = _Executor([{"id": TOKEN, "name": "a"}])
        rows = await service_tokens.list_for_user(ex, user_id=USER)
        assert rows == [{"id": TOKEN, "name": "a"}]
        sql, params = ex.calls[0]
        assert "user_id = :u" in sql and params == {"u": USER}
        assert "token_hash" not in sql

    @pytest.mark.asyncio
    async def test_list_for_workspace_filters_on_the_workspace(self):
        ex = _Executor([])
        assert await service_tokens.list_for_workspace(ex, workspace_id=WS) == []
        sql, params = ex.calls[0]
        assert "workspace_id = :ws" in sql and params == {"ws": WS}

    @pytest.mark.asyncio
    async def test_revoke_is_scoped_to_the_subject_and_idempotent(self):
        ex = _Executor(_Rows([], rowcount=1), _Rows([], rowcount=0))
        assert await service_tokens.revoke(ex, token_id=TOKEN, user_id=USER) is True
        assert await service_tokens.revoke(ex, token_id=TOKEN, user_id=USER) is False
        sql, params = ex.calls[0]
        assert "revoked_at IS NULL" in sql and "user_id = :u" in sql
        assert params == {"id": TOKEN, "u": USER}

    @pytest.mark.asyncio
    async def test_revoke_for_a_workspace_names_the_workspace(self):
        ex = _Executor(_Rows([], rowcount=1))
        assert await service_tokens.revoke(ex, token_id=TOKEN, workspace_id=WS) is True
        sql, params = ex.calls[0]
        assert "workspace_id = :ws" in sql and params == {"id": TOKEN, "ws": WS}

    @pytest.mark.asyncio
    async def test_revoke_needs_exactly_one_subject(self):
        with pytest.raises(service_tokens.TokenArgsInvalid):
            await service_tokens.revoke(_Executor(), token_id=TOKEN)
        with pytest.raises(service_tokens.TokenArgsInvalid):
            await service_tokens.revoke(
                _Executor(), token_id=TOKEN, user_id=USER, workspace_id=WS
            )


class TestVocabularyParity:
    def test_the_token_roles_are_the_migrations_check_list(self):
        assert set(vocabulary.TOKEN_ROLES) == {"operator", "readonly"}

    def test_the_resolution_reasons_are_tenant_resolution_reasons(self):
        for reason in vocabulary.TOKEN_RESOLUTION_REASONS:
            assert reason in TenantResolutionError.REASONS
