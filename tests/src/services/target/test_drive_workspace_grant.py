"""The workspace-level Drive grant (069, `07` §15; owner ruling 2026-09-05, #1165).

One Google grant per WORKSPACE, every folder under it. Pinned here, against
scripted executors: the connect leg reads and writes the workspace's ownerless
credential row (no `media_source_id` ever), the read door resolves a source's
token by its WORKSPACE, a reconnect re-arms every folder, the status projection
is the workspace's, and a folder is paused — never deleted — when removed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.services.target import (
    drive_credentials,
    google_drive_oauth,
    media_sync,
    provisioning,
    workspaces,
)
from src.services.target.drive_adapter import DriveRetryableError
from src.services.target.media_sync import DriveCredentialDead
from tests.src.services.target.conftest import drive_grant

WS = "11111111-1111-1111-1111-111111111111"
SRC = "22222222-2222-2222-2222-222222222222"


class _Exec:
    """Answers every statement with one scalar / one row, and records it."""

    def __init__(self, scalar=None, row=None, rowcount=1):
        self.scalar_value, self.row, self.rowcount, self.calls = (
            scalar,
            row,
            rowcount,
            [],
        )

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params))
        scalar, row, rc = self.scalar_value, self.row, self.rowcount

        class _M:
            def first(self_inner):
                return row

        class _R:
            rowcount = rc

            def scalar(self_inner):
                return scalar

            def scalar_one(self_inner):
                return scalar

            def first(self_inner):
                return row

            def mappings(self_inner):
                return _M()

        return _R()


class _Ring:
    def encrypt(self, plaintext):
        return "ct:" + plaintext

    def decrypt(self, ciphertext):
        assert ciphertext.startswith("ct:")
        return ciphertext[3:]


class TestTheConnectLegOwnsTheWorkspaceRow:
    async def test_connect_purpose_asks_for_the_ownerless_row_of_this_workspace(self):
        ex = _Exec(scalar=False)
        assert (
            await google_drive_oauth.connect_purpose(ex, workspace_id=WS) == "connect"
        )
        ((sql, params),) = ex.calls
        assert "media_source_id IS NULL" in sql and "ig_account_id IS NULL" in sql
        assert "workspace_id = :ws" in sql
        assert params == {"ws": WS, "provider": "gdrive"}

    async def test_a_workspace_that_already_holds_a_grant_reconnects(self):
        assert (
            await google_drive_oauth.connect_purpose(
                _Exec(scalar=True), workspace_id=WS
            )
            == "reconnect"
        )

    async def test_store_credential_upserts_the_workspace_row_and_names_no_source(
        self, monkeypatch
    ):
        monkeypatch.setattr(google_drive_oauth, "ring", lambda: _Ring())
        ex = _Exec(scalar="cred-1")
        assert (
            await google_drive_oauth.store_credential(
                ex, workspace_id=WS, grant=drive_grant()
            )
            == "cred-1"
        )
        ((sql, params),) = ex.calls
        columns = sql.split("INSERT INTO oauth_credentials")[1].split("VALUES")[0]
        assert "media_source_id" not in columns and "ig_account_id" not in columns
        assert "ON CONFLICT (workspace_id, provider)" in sql
        assert "WHERE ig_account_id IS NULL AND media_source_id IS NULL" in sql
        assert "DO UPDATE SET encrypted_payload = EXCLUDED.encrypted_payload" in sql
        assert params["ws"] == WS and params["provider"] == "gdrive"
        assert "src" not in params
        assert params["payload"].startswith("ct:")


class TestTheReadDoorResolvesByWorkspace:
    @pytest.fixture
    def credential(self, monkeypatch):
        """The workspace-scoped session the door opens, answering one row."""
        holder = {"row": None, "asked": []}

        def poller_session_factory(engine, workspace_id):
            holder["asked"].append(workspace_id)
            ex = _Exec(row=holder["row"])

            class _Factory:
                def __call__(self_inner):
                    class _Ctx:
                        async def __aenter__(self_ctx):
                            holder["session"] = ex
                            return ex

                        async def __aexit__(self_ctx, *exc):
                            return False

                    return _Ctx()

            return _Factory()

        from src.services.target import work_loop

        monkeypatch.setattr(work_loop, "poller_session_factory", poller_session_factory)
        monkeypatch.setattr(drive_credentials, "ring", lambda: _Ring())
        return holder

    def _row(self, state="active", expires_in=3600, payload=None):
        plaintext = payload or google_drive_oauth.encode_payload(drive_grant())
        return {
            "encrypted_payload": "ct:" + plaintext,
            "state": state,
            "expires_at": datetime.now(timezone.utc) + timedelta(seconds=expires_in),
        }

    async def test_a_sources_token_is_its_workspaces_grant(self, credential):
        credential["row"] = self._row()
        token = await drive_credentials.token_for_source(object(), SRC, workspace_id=WS)
        assert token == "ya29.access"
        assert credential["asked"] == [WS]
        ((sql, params),) = credential["session"].calls
        assert "media_source_id IS NULL" in sql and "ig_account_id IS NULL" in sql
        assert "media_source_id = " not in sql
        # The WHERE binds the row to its workspace — production connects as
        # the owner role with BYPASSRLS, so the tenant GUC alone binds nothing.
        assert "workspace_id = :ws" in sql
        assert params == {"ws": WS, "provider": "gdrive"}

    async def test_the_workspace_door_needs_no_source(self, credential):
        credential["row"] = self._row()
        assert (
            await drive_credentials.token_for_workspace(object(), WS) == "ya29.access"
        )

    @pytest.mark.parametrize(
        "row, said",
        [
            (None, "never connected"),
            ("revoked", "'revoked'"),
            ("expired", "'expired'"),
        ],
    )
    async def test_every_refusal_names_the_workspace_and_its_cause(
        self, credential, row, said
    ):
        credential["row"] = None if row is None else self._row(state=row)
        with pytest.raises(DriveCredentialDead) as info:
            await drive_credentials.token_for_workspace(object(), WS)
        assert said in str(info.value) and WS in str(info.value)


class TestAnExpiredAccessTokenIsMintedOnTheReadPath:
    """P5, F3 (b) (#1247): the stored grant's ACCESS token expires hourly; the
    read door refreshes it from the refresh token, writes the new payload in
    place, and hands back the fresh token — so "connects once" is true."""

    @pytest.fixture
    def world(self, monkeypatch):
        holder = {"row": None, "refreshed": [], "writes": [], "answer": None}

        def poller_session_factory(engine, workspace_id):
            ex = _Exec(row=holder["row"])

            class _Factory:
                def __call__(self_inner):
                    class _Ctx:
                        async def __aenter__(self_ctx):
                            return ex

                        async def __aexit__(self_ctx, *exc):
                            return False

                    return _Ctx()

            return _Factory()

        async def refresh_access_token(
            client, *, refresh_token, client_id, client_secret
        ):
            holder["refreshed"].append((refresh_token, client_id, client_secret))
            answer = holder["answer"]
            if isinstance(answer, Exception):
                raise answer
            return answer

        class _Uow:
            def __init__(self, engine, tenant_id, **gucs):
                holder["writes"].append(("uow", tenant_id, gucs))
                self.ex = _Exec(rowcount=1)

            def begin(self):
                ex = self.ex

                class _Ctx:
                    async def __aenter__(self_ctx):
                        return ex

                    async def __aexit__(self_ctx, *exc):
                        holder["writes"].extend(ex.calls)
                        return False

                return _Ctx()

        from src.services.target import work_loop

        monkeypatch.setattr(work_loop, "poller_session_factory", poller_session_factory)
        monkeypatch.setattr(drive_credentials, "ring", lambda: _Ring())
        monkeypatch.setattr(
            google_drive_oauth, "refresh_access_token", refresh_access_token
        )
        monkeypatch.setattr(drive_credentials, "unit_of_work", _Uow)
        monkeypatch.setattr(
            drive_credentials.settings, "GOOGLE_CLIENT_ID", "gid", raising=False
        )
        monkeypatch.setattr(
            drive_credentials.settings, "GOOGLE_CLIENT_SECRET", "sec", raising=False
        )
        return holder

    def _expired_row(self):
        return {
            "encrypted_payload": "ct:"
            + google_drive_oauth.encode_payload(drive_grant()),
            "state": "active",
            "expires_at": datetime.now(timezone.utc) - timedelta(minutes=5),
        }

    async def test_an_expired_token_is_refreshed_written_in_place_and_returned(
        self, world
    ):
        world["row"] = self._expired_row()
        world["answer"] = drive_grant(access_token="ya29.fresh")
        token = await drive_credentials.token_for_workspace(object(), WS)
        assert token == "ya29.fresh"
        assert world["refreshed"] == [("1//refresh", "gid", "sec")]
        (uow, (sql, params)) = world["writes"]
        assert uow == ("uow", WS, {"actor_kind": "system"})
        assert (
            "UPDATE oauth_credentials" in sql and "encrypted_payload = :payload" in sql
        )
        assert "expires_at = :exp" in sql
        assert "workspace_id = :ws" in sql and "state = 'active'" in sql
        assert "ig_account_id IS NULL AND media_source_id IS NULL" in sql
        # Compare-and-swap on the ciphertext the read saw: a reconnect that
        # landed during the Google round-trip is never overwritten.
        assert "encrypted_payload = :seen" in sql
        assert params["seen"] == world["row"]["encrypted_payload"]
        assert params["ws"] == WS and params["payload"].startswith("ct:")
        assert "ya29.fresh" in _Ring().decrypt(params["payload"])

    async def test_a_token_about_to_expire_is_refreshed_early(self, world):
        row = self._expired_row()
        row["expires_at"] = datetime.now(timezone.utc) + timedelta(seconds=20)
        world["row"] = row
        world["answer"] = drive_grant(access_token="ya29.early")
        assert await drive_credentials.token_for_workspace(object(), WS) == "ya29.early"

    async def test_invalid_grant_marks_the_row_expired_and_says_reconnect(self, world):
        world["row"] = self._expired_row()
        world["answer"] = google_drive_oauth.DriveOAuthRefused(
            "grant_revoked", "invalid_grant"
        )
        with pytest.raises(DriveCredentialDead) as info:
            await drive_credentials.token_for_workspace(object(), WS)
        assert "reconnect" in str(info.value).lower() and WS in str(info.value)
        (uow, (sql, params)) = world["writes"]
        assert "SET state = 'expired'" in sql and "state = 'active'" in sql
        assert "encrypted_payload = :seen" in sql
        assert params == {
            "ws": WS,
            "provider": "gdrive",
            "seen": world["row"]["encrypted_payload"],
        }

    async def test_a_transient_refresh_failure_is_retryable_and_changes_nothing(
        self, world
    ):
        """One bad minute at Google must not strand a folder: the sync's
        persistent branch reads `DriveCredentialDead`; this is NOT that."""
        world["row"] = self._expired_row()
        world["answer"] = google_drive_oauth.DriveOAuthRefused("refresh_failed", "503")
        with pytest.raises(DriveRetryableError) as info:
            await drive_credentials.token_for_workspace(object(), WS)
        assert "transient" in str(info.value).lower()
        assert world["writes"] == []

    async def test_our_misconfiguration_is_retryable_and_names_the_fix(self, world):
        world["row"] = self._expired_row()
        world["answer"] = google_drive_oauth.DriveOAuthRefused(
            "client_misconfigured", "x"
        )
        with pytest.raises(DriveRetryableError) as info:
            await drive_credentials.token_for_workspace(object(), WS)
        assert "GOOGLE_CLIENT_ID" in str(info.value) and world["writes"] == []

    async def test_without_client_credentials_the_refusal_names_the_variables(
        self, world
    ):
        """Retryable, in the log, never to a tenant: configuration is the
        operator's to fix, and a stranded folder would not fix it."""
        world["row"] = self._expired_row()
        drive_credentials.settings.GOOGLE_CLIENT_SECRET = None
        with pytest.raises(DriveRetryableError) as info:
            await drive_credentials.token_for_workspace(object(), WS)
        assert "GOOGLE_CLIENT_SECRET" in str(info.value)
        assert world["refreshed"] == [] and world["writes"] == []

    async def test_fresh_forces_a_refresh_of_a_live_token(self, world):
        """The adapter's one retry after Google refused a token the door
        thought live."""
        row = self._expired_row()
        row["expires_at"] = datetime.now(timezone.utc) + timedelta(hours=1)
        world["row"] = row
        world["answer"] = drive_grant(access_token="ya29.forced")
        assert (
            await drive_credentials.token_for_workspace(object(), WS, fresh=True)
            == "ya29.forced"
        )
        assert len(world["refreshed"]) == 1

    async def test_a_live_token_is_handed_back_without_a_refresh(self, world):
        row = self._expired_row()
        row["expires_at"] = datetime.now(timezone.utc) + timedelta(hours=1)
        world["row"] = row
        assert (
            await drive_credentials.token_for_workspace(object(), WS) == "ya29.access"
        )
        assert world["refreshed"] == [] and world["writes"] == []


class TestAReconnectRearmsEveryFolder:
    async def test_without_a_source_every_gdrive_source_of_the_workspace_is_rearmed(
        self,
    ):
        ex = _Exec(rowcount=3)
        assert await media_sync.rearm_after_connect(ex, workspace_id=WS) == 3
        ((sql, params),) = ex.calls
        assert "SET state = 'active', alerted_at = NULL, next_sync_at = now()" in sql
        assert "provider = 'gdrive'" in sql and "workspace_id = :ws" in sql
        assert "id = :s" not in sql and params == {"ws": WS}
        # A REMOVED folder stays removed: only what a dead grant or a
        # disconnect paused comes back.
        assert "NOT COALESCE((config->>'removed')::boolean, false)" in sql

    async def test_one_source_still_rearms_alone(self):
        ex = _Exec(rowcount=1)
        assert (
            await media_sync.rearm_after_connect(ex, workspace_id=WS, source_id=SRC)
            == 1
        )
        ((sql, params),) = ex.calls
        assert "id = :s" in sql and params == {"s": SRC, "ws": WS}
        assert "config = config - 'removed'" in sql, "a pick clears the removal marker"


class TestTheWorkspaceStatusProjection:
    async def test_no_grant_is_none(self, monkeypatch):
        async def row(executor, sql, **params):
            assert "media_source_id IS NULL" in sql and params == {
                "ws": WS,
                "provider": "gdrive",
            }
            return None

        monkeypatch.setattr(workspaces.readers, "row", row)
        assert await workspaces.drive_status(object(), workspace_id=WS) == {
            "status": "none",
            "connected_at": None,
        }

    async def test_the_row_is_projected_as_the_destinations_are(self, monkeypatch):
        async def row(executor, sql, **params):
            # `state` alone: a past access-token expiry says nothing about a gdrive
            # grant, which the read door refreshes on demand (P5).
            assert "expires_at" not in sql and "c.state" in sql
            return {"status": "expired", "connected_at": "2026-09-05T00:00:00+00:00"}

        monkeypatch.setattr(workspaces.readers, "row", row)
        assert await workspaces.drive_status(object(), workspace_id=WS) == {
            "status": "expired",
            "connected_at": "2026-09-05T00:00:00+00:00",
        }


class TestRemovingAFolderPausesIt:
    async def test_pause_is_an_update_never_a_delete(self):
        ex = _Exec(row=(SRC,))
        assert (
            await provisioning.pause_media_source(ex, workspace_id=WS, source_id=SRC)
            is True
        )
        ((sql, params),) = ex.calls
        assert sql.lstrip().upper().startswith("UPDATE media_sources".upper())
        assert "state = 'paused'" in sql and "DELETE" not in sql.upper()
        assert '"removed": true' in sql, "a removal is marked, so a reconnect skips it"
        assert "workspace_id = :ws" in sql and params == {"s": SRC, "ws": WS}

    async def test_a_source_that_is_not_here_is_false(self):
        assert (
            await provisioning.pause_media_source(
                _Exec(row=None), workspace_id=WS, source_id=SRC
            )
            is False
        )
