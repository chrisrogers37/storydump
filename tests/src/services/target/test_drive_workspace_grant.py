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
            ("expired-at", "expired"),
        ],
    )
    async def test_every_refusal_names_the_workspace_and_its_cause(
        self, credential, row, said
    ):
        if row is None:
            credential["row"] = None
        elif row == "expired-at":
            credential["row"] = self._row(expires_in=-1)
        else:
            credential["row"] = self._row(state=row)
        with pytest.raises(DriveCredentialDead) as info:
            await drive_credentials.token_for_workspace(object(), WS)
        assert said in str(info.value) and WS in str(info.value)


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
            assert "credential_status" in sql
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
