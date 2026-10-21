"""The identity mapping writer's statements and refusals (#1644), without
a database: what is written, in what order, and when nothing is. The rows, the
CHECKs, the one-current index and the roles are proven in
`tests/scripts/test_meta_identity_mapping_gate.py`.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.services.target import identity_mappings
from src.services.target.instagram_sources import (
    PastPostsAuthorization,
    PastPostsRefused,
)

WS = "33333333-3333-4333-8333-333333333333"
ACCOUNT = "55555555-5555-4555-8555-555555555555"
CREDENTIAL = "66666666-6666-4666-8666-666666666666"
AUTHORIZATION = "99999999-9999-4999-8999-999999999999"
AT = datetime(2026, 10, 12, 9, 30, tzinfo=timezone.utc)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class _Scripted:
    """Records each statement and parameters; answers *answers* in order."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.statements = []

    async def execute(self, statement, params=None):
        self.statements.append((str(statement), params))
        return _Result(self.answers.pop(0))


def authorization():
    return PastPostsAuthorization(AUTHORIZATION, "4242", "17841400000000001", AT)


async def land(executor, **over):
    args = dict(
        workspace_id=WS,
        ig_account_id=ACCOUNT,
        credential_id=CREDENTIAL,
        authorization=authorization(),
        app_identity="instagram:4242",
        app_scoped_id="990000000000007",
    )
    return await identity_mappings.record_oauth_exchange(executor, **{**args, **over})


class TestRecord:
    async def record(self, executor):
        return await identity_mappings.record(
            executor,
            workspace_id=WS,
            ig_account_id=ACCOUNT,
            credential_id=CREDENTIAL,
            authorization_id=AUTHORIZATION,
            app_identity="instagram:4242",
            subject_ref="990000000000007",
            account_ref="17841400000000001",
            method="oauth_exchange",
            authorized_at=AT,
            verified_at=AT,
        )

    async def test_the_previous_current_row_falls_before_the_new_one_lands(self):
        executor = _Scripted([{"authorization_id": "old"}], [{"authorization_id": "x"}])
        assert await self.record(executor) is True
        (flip, flip_params), (insert, insert_params) = executor.statements
        assert flip.startswith("UPDATE meta_identity_mappings SET is_current = false")
        assert "is_current" in flip and "credential_id = :credential" in flip
        assert flip_params == {
            "ws": WS,
            "credential": CREDENTIAL,
            "authorization": AUTHORIZATION,
        }
        assert insert.startswith("INSERT INTO meta_identity_mappings")
        assert "ON CONFLICT (workspace_id, credential_id, authorization_id)" in insert
        assert insert_params == {
            "ws": WS,
            "account": ACCOUNT,
            "credential": CREDENTIAL,
            "authorization": AUTHORIZATION,
            "app": "instagram:4242",
            "subject": "990000000000007",
            "account_ref": "17841400000000001",
            "method": "oauth_exchange",
            "authorized_at": AT,
            "verified_at": AT,
        }

    async def test_a_first_mapping_flips_nothing_and_still_lands(self):
        executor = _Scripted([], [{"authorization_id": "x"}])
        assert await self.record(executor) is True

    async def test_an_authorization_already_recorded_writes_nothing_and_says_so(self):
        """A reconciliation retry: the conflict does nothing, and the flip
        never touches a row when this authorization is already recorded."""
        executor = _Scripted([], [])
        assert await self.record(executor) is False
        flip = executor.statements[0][0]
        assert "NOT EXISTS" in flip and "n.authorization_id = :authorization" in flip


class TestRecordOauthExchange:
    async def test_the_row_is_the_callbacks_authorization_and_the_proven_id(self):
        executor = _Scripted([], [{"authorization_id": "x"}])
        await land(executor)
        insert_params = executor.statements[1][1]
        assert insert_params == {
            "ws": WS,
            "account": ACCOUNT,
            "credential": CREDENTIAL,
            # The one id and time the past-posts generation is bound to.
            "authorization": AUTHORIZATION,
            "app": "instagram:4242",
            # The person's id from the identity read; the account's own id
            # is the authorization's subject.
            "subject": "990000000000007",
            "account_ref": "17841400000000001",
            "method": "oauth_exchange",
            "authorized_at": AT,
            "verified_at": AT,
        }

    @pytest.mark.parametrize("proven", [None, ""], ids=["none", "empty"])
    async def test_no_app_scoped_id_is_refused_before_any_statement(self, proven):
        executor = _Scripted()
        with pytest.raises(PastPostsRefused) as refused:
            await land(executor, app_scoped_id=proven)
        assert refused.value.reason == "past_posts_identity_unverified"
        assert executor.statements == []

    async def test_an_unconfigured_app_is_refused_before_any_statement(self):
        executor = _Scripted()
        with pytest.raises(PastPostsRefused) as refused:
            await land(executor, app_identity="instagram:unconfigured")
        assert refused.value.reason == "past_posts_identity_unverified"
        assert executor.statements == []
