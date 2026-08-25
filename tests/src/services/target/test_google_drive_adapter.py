"""The real Drive read leg (#982) — `list_changes` against a mock transport.

No test here reaches Google. Every request is served by `httpx.MockTransport`,
so the shapes asserted are the shapes the adapter actually builds rather than
the shapes it was described as building.

The egress floor's private-address guard resolves DNS, which a unit test must
not depend on, so the policy passed in turns that one guard off and leaves the
rest on. The host allowlist deliberately STAYS on — `www.googleapis.com` being
admitted is part of what these tests certify.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from src.services.target.drive_adapter import (
    DriveLostResponse,
    DriveRetryableError,
    DriveTerminalError,
)
from src.services.target.egress import EgressPolicy
from src.services.target.google_drive_adapter import GoogleDriveAdapter
from src.services.target.media_sync import DriveCredentialDead, DriveSourceGone
from src.utils.media_kind import INSTAGRAM_VIDEO_SUFFIXES

CONFIG = {"v": 1, "folder_ref": "FOLDER123"}
WS = "11111111-1111-1111-1111-111111111111"
SRC = "22222222-2222-2222-2222-222222222222"


def _policy() -> EgressPolicy:
    return EgressPolicy().without(enforce_private_address_block=False)


def _file(fid, name="a.jpg", mime="image/jpeg", md5="hash1", size="10"):
    return {
        "id": fid,
        "name": name,
        "mimeType": mime,
        "md5Checksum": md5,
        "size": size,
        "modifiedTime": "2026-08-01T00:00:00Z",
    }


def _adapter(handler, *, token="tok", record=None, page_size=200):
    async def token_provider(source_id, *, workspace_id):
        if record is not None:
            record.append((source_id, workspace_id))
        if isinstance(token, Exception):
            raise token
        return token

    return GoogleDriveAdapter(
        token_provider=token_provider,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        policy=_policy(),
        page_size=page_size,
    )


def _json_handler(payload, status=200, headers=None):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(
            status,
            json=payload,
            headers=headers or {"content-type": "application/json"},
        )

    return handler, seen


class TestListChanges:
    @pytest.mark.asyncio
    async def test_maps_a_page_into_media_sync_item_shape(self):
        handler, _ = _json_handler({"files": [_file("f1", name="cat.jpg")]})
        items, checkpoint = await _adapter(handler).list_changes(
            CONFIG, None, source_id=SRC, workspace_id=WS
        )
        assert items == [
            {
                "ref": "f1",
                "name": "cat.jpg",
                "kind": "image",
                "mime_type": "image/jpeg",
                "content_hash": "hash1",
                "size_bytes": 10,
                "modified_at": "2026-08-01T00:00:00Z",
            }
        ]
        # media_sync reads exactly these five; the rest are additive.
        assert {"ref", "name", "kind", "mime_type", "content_hash"} <= set(items[0])
        assert checkpoint == {"v": 1}

    @pytest.mark.asyncio
    async def test_video_mime_maps_to_the_video_kind(self):
        handler, _ = _json_handler(
            {"files": [_file("f2", name="clip.mp4", mime="video/mp4")]}
        )
        items, _ = await _adapter(handler).list_changes(
            CONFIG, None, source_id=SRC, workspace_id=WS
        )
        assert items[0]["kind"] == "video"

    @pytest.mark.asyncio
    async def test_the_provider_mime_survives_a_name_that_carries_no_extension(self):
        """The fixture is extensionless ON PURPOSE, and a normal-looking name
        would prove nothing here.

        A Drive file's name is whatever a person typed into Drive; nothing
        requires a suffix, and `media_sync` writes that name straight into
        `media_items.file_name`. So for provider media the filename is not a
        classifier input at all, and `mimeType` is the only authoritative
        signal the listing carries. Deriving `kind` from it and then dropping it
        is what left `mime_type` NULL for every Drive row.

        The control is the second assertion, coupled to the shipped suffix set
        rather than a copy of it: this name is one the IG predicate would call
        IMAGE, while the provider says video.
        """
        handler, _ = _json_handler(
            {"files": [_file("f9", name="sunset clip", mime="video/quicktime")]}
        )
        items, _ = await _adapter(handler).list_changes(
            CONFIG, None, source_id=SRC, workspace_id=WS
        )
        assert items[0]["mime_type"] == "video/quicktime"
        assert items[0]["kind"] == "video"
        # Control: the name is inert to every extension-based classifier.
        assert Path(items[0]["name"]).suffix == ""
        assert not items[0]["name"].lower().endswith(INSTAGRAM_VIDEO_SUFFIXES)

    @pytest.mark.asyncio
    async def test_a_skipped_entry_never_reaches_the_item_shape(self):
        """A positive control on the two probes above: the same helper that
        builds a carried item also drops one, so a listing coming back empty
        here is the skip firing rather than the fixture failing to arrive."""
        handler, _ = _json_handler(
            {"files": [_file("f10", name="doc", mime="application/pdf")]}
        )
        items, _ = await _adapter(handler).list_changes(
            CONFIG, None, source_id=SRC, workspace_id=WS
        )
        assert items == []

    @pytest.mark.asyncio
    async def test_exhaustion_is_the_absent_token_not_a_short_page(self):
        """A page well under `page_size` still says nothing about exhaustion —
        only the token does. Asserted in both directions."""
        handler, _ = _json_handler({"files": [_file("f1")], "nextPageToken": "MORE"})
        _, checkpoint = await _adapter(handler, page_size=200).list_changes(
            CONFIG, None, source_id=SRC, workspace_id=WS
        )
        assert checkpoint == {"v": 1, "page_token": "MORE"}

        handler2, _ = _json_handler({"files": [_file("f1")]})
        _, done = await _adapter(handler2).list_changes(
            CONFIG, None, source_id=SRC, workspace_id=WS
        )
        assert "page_token" not in done

    @pytest.mark.asyncio
    async def test_a_checkpoint_token_is_sent_back_as_pageToken(self):
        handler, seen = _json_handler({"files": []})
        await _adapter(handler).list_changes(
            CONFIG, {"v": 1, "page_token": "RESUME"}, source_id=SRC, workspace_id=WS
        )
        assert "pageToken=RESUME" in str(seen["request"].url)

    @pytest.mark.asyncio
    async def test_the_request_carries_the_bearer_and_scopes_to_the_folder(self):
        handler, seen = _json_handler({"files": []})
        await _adapter(handler, token="TOKEN-XYZ").list_changes(
            CONFIG, None, source_id=SRC, workspace_id=WS
        )
        request = seen["request"]
        assert request.headers["authorization"] == "Bearer TOKEN-XYZ"
        url = str(request.url)
        assert "FOLDER123" in url and "in+parents" in url.replace("%20", "+")
        assert "md5Checksum" in url
        assert "trashed" in url

    @pytest.mark.asyncio
    async def test_both_ids_reach_the_token_provider(self):
        """The credential is per-source AND RLS-scoped per workspace, so an
        adapter that dropped either would resolve another tenant's credential."""
        record: list = []
        handler, _ = _json_handler({"files": []})
        await _adapter(handler, record=record).list_changes(
            CONFIG, None, source_id=SRC, workspace_id=WS
        )
        assert record == [(SRC, WS)]


class TestRefusals:
    @pytest.mark.asyncio
    async def test_a_config_with_no_folder_ref_is_refused_before_any_call(self):
        called = []

        def handler(request):
            called.append(request)
            return httpx.Response(200, json={"files": []})

        with pytest.raises(DriveTerminalError, match="folder_ref"):
            await _adapter(handler).list_changes(
                {"v": 1}, None, source_id=SRC, workspace_id=WS
            )
        # The seam contract says validate_source_config runs FIRST. If it ran
        # after the request, a shapeless config would still have listed the
        # drive root once before refusing.
        assert called == []

    @pytest.mark.asyncio
    async def test_root_name_is_refused_rather_than_silently_listing_the_parent(self):
        called = []

        def handler(request):
            called.append(request)
            return httpx.Response(200, json={"files": []})

        with pytest.raises(DriveTerminalError, match="root_name"):
            await _adapter(handler).list_changes(
                {**CONFIG, "root_name": "sub"}, None, source_id=SRC, workspace_id=WS
            )
        assert called == []


class TestSkips:
    @pytest.mark.asyncio
    async def test_a_file_with_no_md5_is_skipped_and_the_rest_survive(self):
        handler, _ = _json_handler(
            {
                "files": [
                    _file("good"),
                    {"id": "nohash", "name": "x.jpg", "mimeType": "image/jpeg"},
                ]
            }
        )
        items, _ = await _adapter(handler).list_changes(
            CONFIG, None, source_id=SRC, workspace_id=WS
        )
        assert [i["ref"] for i in items] == ["good"]

    @pytest.mark.asyncio
    async def test_a_mime_the_query_excludes_is_skipped(self):
        handler, _ = _json_handler(
            {"files": [_file("doc", mime="application/pdf"), _file("good")]}
        )
        items, _ = await _adapter(handler).list_changes(
            CONFIG, None, source_id=SRC, workspace_id=WS
        )
        assert [i["ref"] for i in items] == ["good"]


class TestErrorRouting:
    """Each status selects one `02` behaviour; the executor never parses bodies."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status,body,expected",
        [
            (401, {"error": {"message": "Invalid Credentials"}}, DriveCredentialDead),
            (
                403,
                {"error": {"message": "Insufficient permission"}},
                DriveCredentialDead,
            ),
            (
                403,
                {"error": {"message": "User Rate Limit Exceeded"}},
                DriveRetryableError,
            ),
            (404, {"error": {"message": "File not found: F"}}, DriveSourceGone),
            (429, {"error": {"message": "quota"}}, DriveRetryableError),
            (500, {"error": {"message": "backend"}}, DriveRetryableError),
            (503, {"error": {"message": "unavailable"}}, DriveRetryableError),
            (400, {"error": {"message": "Invalid query"}}, DriveTerminalError),
        ],
    )
    async def test_status_selects_the_routing_type(self, status, body, expected):
        handler, _ = _json_handler(body, status=status)
        with pytest.raises(expected):
            await _adapter(handler).list_changes(
                CONFIG, None, source_id=SRC, workspace_id=WS
            )

    @pytest.mark.asyncio
    async def test_a_dead_transport_is_not_catchable_as_a_drive_error(self):
        """`DriveLostResponse` is deliberately not a `DriveError`: "no answer
        exists" must not be catchable as "it failed"."""

        def handler(request):
            raise httpx.ConnectError("boom")

        from src.services.target.drive_adapter import DriveError

        with pytest.raises(DriveLostResponse) as caught:
            await _adapter(handler).list_changes(
                CONFIG, None, source_id=SRC, workspace_id=WS
            )
        assert not isinstance(caught.value, DriveError)

    @pytest.mark.asyncio
    async def test_a_200_that_is_not_json_is_retryable_not_a_crash(self):
        def handler(request):
            return httpx.Response(
                200, content=b"<html>nope", headers={"content-type": "text/html"}
            )

        with pytest.raises(DriveRetryableError):
            await _adapter(handler).list_changes(
                CONFIG, None, source_id=SRC, workspace_id=WS
            )


class TestGzip:
    """Google serves gzip. The floor streams rather than buffering, so this is
    the one transport property a mock could hide if it were never asserted."""

    @pytest.mark.asyncio
    async def test_a_gzip_encoded_body_is_decoded(self):
        import gzip

        payload = {"files": [_file("gz")]}
        packed = gzip.compress(json.dumps(payload).encode())

        def handler(request):
            return httpx.Response(
                200,
                content=packed,
                headers={
                    "content-type": "application/json",
                    "content-encoding": "gzip",
                },
            )

        items, _ = await _adapter(handler).list_changes(
            CONFIG, None, source_id=SRC, workspace_id=WS
        )
        assert [i["ref"] for i in items] == ["gz"]


class TestCredentialFailurePropagates:
    @pytest.mark.asyncio
    async def test_a_dead_credential_reaches_the_caller_unchanged(self):
        """`media_sync` routes on this type to flip the source to `error`; the
        adapter must not re-wrap it into the ladder's vocabulary."""
        handler, _ = _json_handler({"files": []})
        adapter = _adapter(handler, token=DriveCredentialDead("no credential"))
        with pytest.raises(DriveCredentialDead):
            await adapter.list_changes(CONFIG, None, source_id=SRC, workspace_id=WS)
