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
from urllib.parse import quote_plus

import httpx
import pytest

from src.services.target.drive_adapter import (
    DriveLostResponse,
    DriveRetryableError,
    DriveTerminalError,
)
from src.services.target.egress import EgressPolicy
from src.services.target.google_drive_adapter import (
    SHARED_ROOT,
    GoogleDriveAdapter,
    _listing_query,
)
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
    async def token_provider(source_id, *, workspace_id, fresh=False):
        if record is not None:
            record.append(
                (source_id, workspace_id)
                if not fresh
                else (source_id, workspace_id, "fresh")
            )
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
        """The workspace id is the credential's key (069) and the source id
        names the folder; an adapter that dropped either would resolve the
        wrong tenant's grant or mislabel the folder it was syncing."""
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


class TestProbe:
    """`01`:78's `probe(config) -> ok | error-class`.

    One case per error class plus a healthy folder, per the epic's test plan.
    The classes are the transport's existing taxonomy — `probe` adds no
    vocabulary of its own, so these assertions are on the same types
    `media_sync` already routes on.
    """

    @pytest.mark.asyncio
    async def test_a_reachable_folder_probes_ok(self):
        handler, _ = _json_handler({"files": [_file("f1")]})
        result = await _adapter(handler).probe(CONFIG, source_id=SRC, workspace_id=WS)
        assert result.ok is True
        assert result.error is None and result.error_class is None

    @pytest.mark.asyncio
    async def test_an_empty_folder_probes_ok(self):
        """Zero files is a reachable folder nobody has filled yet.

        The state machine asks whether the source can be LISTED. Reading empty
        as a failure would refuse every correctly-configured new source, which
        is the connect flow's most common state.
        """
        handler, _ = _json_handler({"files": []})
        result = await _adapter(handler).probe(CONFIG, source_id=SRC, workspace_id=WS)
        assert result.ok is True

    @pytest.mark.asyncio
    async def test_it_asks_the_same_question_list_changes_asks(self):
        """The probe must exercise the LISTING door, not a cheaper one.

        A `files.get` on the folder id would pass a source whose listing query
        is broken — precisely the config error this verb exists to catch. So
        the `q` is asserted identical to the one `list_changes` builds, and the
        only intended difference is the page size.
        """
        handler, seen = _json_handler({"files": []})
        await _adapter(handler).probe(CONFIG, source_id=SRC, workspace_id=WS)
        probe_url = str(seen["request"].url)

        handler2, seen2 = _json_handler({"files": []})
        await _adapter(handler2).list_changes(
            CONFIG, None, source_id=SRC, workspace_id=WS
        )
        listing_url = str(seen2["request"].url)

        q = "q=" + quote_plus(_listing_query(CONFIG))
        assert q in probe_url and q in listing_url
        assert "pageSize=1" in probe_url
        assert seen["request"].headers["authorization"] == "Bearer tok"

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
            (404, {"error": {"message": "File not found"}}, DriveSourceGone),
            (429, {"error": {"message": "Too many requests"}}, DriveRetryableError),
            (503, {"error": {"message": "Backend Error"}}, DriveRetryableError),
            (400, {"error": {"message": "Invalid query"}}, DriveTerminalError),
        ],
    )
    @pytest.mark.asyncio
    async def test_each_status_comes_back_as_its_error_class(
        self, status, body, expected
    ):
        handler, _ = _json_handler(body, status=status)
        result = await _adapter(handler).probe(CONFIG, source_id=SRC, workspace_id=WS)
        assert result.ok is False
        assert isinstance(result.error, expected)
        assert result.error_class == expected.__name__

    @pytest.mark.asyncio
    async def test_a_lost_response_propagates_rather_than_becoming_a_verdict(self):
        """ "We do not know" must not be catchable as "it failed".

        If a dead transport came back as ``ok=False``, a caller branching on
        `ok` — which is the obvious way to use this — would flip a healthy
        source to `error` on a network blip. There is no verdict to return when
        the provider never answered, so this raises.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        with pytest.raises(DriveLostResponse):
            await _adapter(handler).probe(CONFIG, source_id=SRC, workspace_id=WS)

    @pytest.mark.asyncio
    async def test_a_config_with_no_folder_ref_is_a_result_not_a_raise(self):
        """A bad config is exactly what connect validation is asked about."""
        handler, seen = _json_handler({"files": []})
        result = await _adapter(handler).probe({"v": 1}, source_id=SRC, workspace_id=WS)
        assert result.ok is False
        assert isinstance(result.error, DriveTerminalError)
        assert "request" not in seen, "a refused config must cost no provider call"

    @pytest.mark.asyncio
    async def test_probe_and_list_changes_refuse_the_same_config(self):
        """The shared-preflight property, asserted rather than assumed.

        A probe that accepted a config the sync then refuses would green-light
        a source into `active` that cannot list, and the failure would surface
        later as a sync error nobody connects back to the connect form. The
        two must refuse the same set; the only difference permitted is the
        SHAPE of the refusal — probe returns it, list_changes raises it.
        """
        rooted = {"v": 1, "folder_ref": "FOLDER123", "root_name": "Stories"}
        handler, _ = _json_handler({"files": []})

        result = await _adapter(handler).probe(rooted, source_id=SRC, workspace_id=WS)
        assert result.ok is False
        assert isinstance(result.error, DriveTerminalError)

        handler2, _ = _json_handler({"files": []})
        with pytest.raises(DriveTerminalError):
            await _adapter(handler2).list_changes(
                rooted, None, source_id=SRC, workspace_id=WS
            )

    @pytest.mark.asyncio
    async def test_a_dead_credential_is_a_result_not_a_raise(self):
        """Today's real state: nothing writes a gdrive credential, so the token
        provider refuses. `probe` must answer with that rather than raise —
        it is the reading a connect form needs to show."""
        handler, _ = _json_handler({"files": []})
        adapter = _adapter(handler, token=DriveCredentialDead("no credential"))
        result = await adapter.probe(CONFIG, source_id=SRC, workspace_id=WS)
        assert result.ok is False
        assert result.error_class == "DriveCredentialDead"


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


class TestListFolders:
    """The folder browser (#1165 lean (b)): the same `drive.readonly` grant,
    the same floored transport, one more `q`."""

    FOLDER_MIME = "application/vnd.google-apps.folder"

    def _folder(self, fid, name):
        return {"id": fid, "name": name}

    @pytest.mark.asyncio
    async def test_the_root_lists_folders_only_and_asks_the_workspace_token(self):
        record = []
        handler, seen = _json_handler({"files": [self._folder("f1", "Trips")]})
        page = await _adapter(handler, record=record).list_folders(
            parent=None, workspace_id=WS
        )
        assert (
            page.folders == [{"id": "f1", "name": "Trips"}] and page.truncated is False
        )
        assert record == [(None, WS)]
        q = seen["request"].url.params["q"]
        assert "'root' in parents" in q and f"mimeType = '{self.FOLDER_MIME}'" in q
        assert "trashed = false" in q
        assert seen["request"].url.params["fields"] == "nextPageToken,files(id,name)"
        assert seen["request"].headers["authorization"] == "Bearer tok"

    @pytest.mark.asyncio
    async def test_a_parent_narrows_the_query(self):
        handler, seen = _json_handler({"files": []})
        page = await _adapter(handler).list_folders(parent="abc_-123", workspace_id=WS)
        assert page.folders == []
        assert "'abc_-123' in parents" in seen["request"].url.params["q"]

    @pytest.mark.asyncio
    async def test_the_shared_root_lists_what_was_shared_to_the_account(self):
        handler, seen = _json_handler(
            {"files": [self._folder("s1", "From the client")]}
        )
        page = await _adapter(handler).list_folders(parent=SHARED_ROOT, workspace_id=WS)
        assert page.folders == [{"id": "s1", "name": "From the client"}]
        q = seen["request"].url.params["q"]
        assert "sharedWithMe = true" in q and "in parents" not in q

    @pytest.mark.asyncio
    async def test_pages_are_followed_to_the_end(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.params.get("pageToken"))
            if len(calls) == 1:
                return httpx.Response(
                    200,
                    json={"files": [self._folder("f1", "A")], "nextPageToken": "p2"},
                )
            return httpx.Response(200, json={"files": [self._folder("f2", "B")]})

        page = await _adapter(handler).list_folders(parent=None, workspace_id=WS)
        assert [f["id"] for f in page.folders] == ["f1", "f2"]
        assert calls == [None, "p2"] and page.truncated is False

    @pytest.mark.asyncio
    async def test_the_cap_cuts_the_listing_and_says_so(self, monkeypatch):
        from src.services.target import google_drive_adapter as mod

        monkeypatch.setattr(mod, "FOLDER_LIST_CAP", 3)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.params.get("pageToken"):
                return httpx.Response(200, json={"files": [self._folder("f4", "D")]})
            return httpx.Response(
                200,
                json={
                    "files": [self._folder(f"f{i}", n) for i, n in enumerate("ABC", 1)],
                    "nextPageToken": "p2",
                },
            )

        page = await _adapter(handler).list_folders(parent=None, workspace_id=WS)
        assert [f["id"] for f in page.folders] == ["f1", "f2", "f3"]
        assert page.truncated is True

    @pytest.mark.asyncio
    async def test_a_parent_that_is_not_a_drive_id_is_refused_before_any_request(self):
        handler, seen = _json_handler({"files": []})
        for bad in ("x' or 1=1", "abc\n", ""):
            with pytest.raises(DriveTerminalError):
                await _adapter(handler).list_folders(parent=bad, workspace_id=WS)
        assert "request" not in seen

    @pytest.mark.asyncio
    async def test_a_dead_grant_is_named_as_such(self):
        handler, _ = _json_handler(
            {"error": {"message": "Invalid Credentials"}}, status=401
        )
        with pytest.raises(DriveCredentialDead):
            await _adapter(handler).list_folders(parent=None, workspace_id=WS)


class TestOneRemintOnARefusedToken:
    """P5 (#1247): a token the door handed out can die inside the request it
    was minted for. Google's 401 buys exactly one re-mint (`fresh=True`) and
    one retry; a second refusal is the grant's and propagates."""

    @pytest.mark.asyncio
    async def test_a_401_is_retried_once_with_a_fresh_token(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.headers["authorization"])
            if len(calls) == 1:
                return httpx.Response(
                    401, json={"error": {"message": "Invalid Credentials"}}
                )
            return httpx.Response(200, json={"files": [_file("f1")]})

        record: list = []
        tokens = iter(["tok-stale", "tok-fresh"])

        async def token_provider(source_id, *, workspace_id, fresh=False):
            record.append(fresh)
            return next(tokens)

        adapter = GoogleDriveAdapter(
            token_provider=token_provider,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            policy=_policy(),
        )
        items, _ = await adapter.list_changes(
            CONFIG, None, source_id=SRC, workspace_id=WS
        )
        assert [i["ref"] for i in items] == ["f1"]
        assert record == [False, True]
        assert calls == ["Bearer tok-stale", "Bearer tok-fresh"]

    @pytest.mark.asyncio
    async def test_a_second_refusal_is_the_grants(self):
        handler, _ = _json_handler(
            {"error": {"message": "Invalid Credentials"}}, status=401
        )
        record: list = []
        with pytest.raises(DriveCredentialDead):
            await _adapter(handler, record=record).list_folders(
                parent=None, workspace_id=WS
            )
        assert record == [(None, WS), (None, WS, "fresh")]
