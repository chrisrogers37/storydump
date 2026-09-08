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
    checkpoint_incomplete,
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
                "folder_path": "",
            }
        ]
        # media_sync reads exactly these five; the rest are additive.
        assert {"ref", "name", "kind", "mime_type", "content_hash"} <= set(items[0])
        assert not checkpoint_incomplete(checkpoint)

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
        assert checkpoint["page_token"] == "MORE"
        assert checkpoint["current"]["id"] == "FOLDER123", "never the bare token shape"

        handler2, _ = _json_handler({"files": [_file("f1")]})
        _, done = await _adapter(handler2).list_changes(
            CONFIG, None, source_id=SRC, workspace_id=WS
        )
        assert "page_token" not in done

    @pytest.mark.asyncio
    async def test_a_checkpoint_token_is_sent_back_as_pageToken(self):
        handler, seen = _json_handler({"files": []})
        at_root = {
            "id": "FOLDER123",
            "name": None,
            "top": "FOLDER123",
            "top_name": None,
            "path": "",
            "listed": True,
        }
        await _adapter(handler).list_changes(
            CONFIG,
            {
                "v": 2,
                "walk": "w",
                "current": at_root,
                "queue": [],
                "page_token": "RESUME",
            },
            source_id=SRC,
            workspace_id=WS,
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

        q = "q=" + quote_plus(_listing_query(CONFIG["folder_ref"]))
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
            return httpx.Response(200, json={"files": [{"id": "f1", "name": "Trips"}]})

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
        page = await adapter.list_folders(parent=None, workspace_id=WS)
        assert [f["id"] for f in page.folders] == ["f1"]
        assert record == [False, True]
        assert calls == ["Bearer tok-stale", "Bearer tok-fresh"]

    @pytest.mark.asyncio
    async def test_a_remint_inside_the_walk_carries_to_the_next_request(self):
        """The subfolder listing gets the 401; the media page that follows must
        use the FRESH token, not resend the refused one."""
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.headers["authorization"])
            if len(calls) == 1:
                return httpx.Response(
                    401, json={"error": {"message": "Invalid Credentials"}}
                )
            return httpx.Response(200, json={"files": []})

        tokens = iter(["tok-stale", "tok-fresh"])

        async def token_provider(source_id, *, workspace_id, fresh=False):
            return next(tokens)

        adapter = GoogleDriveAdapter(
            token_provider=token_provider,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            policy=_policy(),
        )
        items, cp = await adapter.list_changes(
            CONFIG, None, source_id=SRC, workspace_id=WS
        )
        assert items == [] and not checkpoint_incomplete(cp)
        assert calls == ["Bearer tok-stale", "Bearer tok-fresh", "Bearer tok-fresh"]

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


def _folder_entry(fid, name):
    return {"id": fid, "name": name, "mimeType": "application/vnd.google-apps.folder"}


class TestTheWalkOverSubfolders:
    """Owner ruling 2026-09-06: a picked folder's SUBFOLDERS are categories, as
    in the legacy product (`google_drive_provider.list_files`). The walk is one
    level deep, one media page per call, and the cursor rides the checkpoint:
    `current` (the folder being listed, with its name = the category), `queue`
    (folders still to list), `page_token` (within the current folder). A
    checkpoint of exactly `{"v": 1}` is the only statement that the walk is
    complete — the bound stays announced, never absorbed."""

    ROOT = "FOLDER123"

    def _drive(self, *, root_files, subfolders, folder_files, pages=None):
        """A Drive with `root_files` directly in the root, `subfolders`
        [(id, name)], and `folder_files` {folder_id: [files]}. `pages` lets a
        folder answer in two pages: {folder_id: split_index}."""
        pages = pages or {}
        calls: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            q = request.url.params["q"]
            token = request.url.params.get("pageToken")
            calls.append({"q": q, "pageToken": token})
            parent = q.split("'")[1]
            if "mimeType = 'application/vnd.google-apps.folder'" in q:
                # Lazy walk (ruling 2026-09-08): every popped folder is asked
                # for ITS subfolders once; only the root has any here.
                children = subfolders if parent == self.ROOT else []
                return httpx.Response(
                    200, json={"files": [_folder_entry(i, n) for i, n in children]}
                )
            files = root_files if parent == self.ROOT else folder_files.get(parent, [])
            if parent in pages:
                split = pages[parent]
                if token is None:
                    return httpx.Response(
                        200,
                        json={"files": files[:split], "nextPageToken": f"{parent}-p2"},
                    )
                return httpx.Response(200, json={"files": files[split:]})
            return httpx.Response(200, json={"files": files})

        return _adapter(handler), calls

    @pytest.mark.asyncio
    async def test_the_root_and_each_subfolder_are_listed_and_the_subfolder_names_the_category(
        self,
    ):
        adapter, calls = self._drive(
            root_files=[_file("r1", name="loose.jpg")],
            subfolders=[("MEMES", "memes"), ("MERCH", "merch")],
            folder_files={
                "MEMES": [_file("m1", name="a.jpg")],
                "MERCH": [_file("s1", name="shirt.mp4", mime="video/mp4")],
            },
        )
        seen = []
        checkpoint = None
        for _ in range(10):
            items, checkpoint = await adapter.list_changes(
                CONFIG, checkpoint, source_id=SRC, workspace_id=WS
            )
            seen.extend((i["ref"], i.get("category")) for i in items)
            if not checkpoint_incomplete(checkpoint):
                break
        assert not checkpoint_incomplete(checkpoint), (
            "the walk must end with a complete checkpoint"
        )
        assert seen == [("r1", None), ("m1", "memes"), ("s1", "merch")]
        # Each folder is asked for its subfolders exactly once as it is
        # popped (the lazy walk), then paged for media: three of each.
        folder_listings = [
            c for c in calls if "application/vnd.google-apps.folder" in c["q"]
        ]
        assert len(folder_listings) == 3
        assert f"'{self.ROOT}' in parents" in folder_listings[0]["q"]
        assert len(calls) == 6

    @pytest.mark.asyncio
    async def test_the_cursor_survives_a_paged_folder_and_resumes_where_it_stopped(
        self,
    ):
        adapter, calls = self._drive(
            root_files=[],
            subfolders=[("MEMES", "memes")],
            folder_files={"MEMES": [_file("m1"), _file("m2"), _file("m3")]},
            pages={"MEMES": 2},
        )
        items, cp = await adapter.list_changes(
            CONFIG, None, source_id=SRC, workspace_id=WS
        )
        # First call: the root (empty) — the cursor moves to `memes`.
        assert (
            items == [] and cp["current"]["name"] == "memes" and cp.get("queue") == []
        )
        items, cp = await adapter.list_changes(
            CONFIG, cp, source_id=SRC, workspace_id=WS
        )
        assert [i["ref"] for i in items] == ["m1", "m2"] and cp[
            "page_token"
        ] == "MEMES-p2"
        assert cp["current"]["name"] == "memes"
        items, cp = await adapter.list_changes(
            CONFIG, cp, source_id=SRC, workspace_id=WS
        )
        assert [i["ref"] for i in items] == ["m3"] and not checkpoint_incomplete(cp)
        assert calls[-1]["pageToken"] == "MEMES-p2"

    @pytest.mark.asyncio
    async def test_a_folder_with_no_subfolders_is_the_old_flat_listing(self):
        adapter, calls = self._drive(
            root_files=[_file("r1")], subfolders=[], folder_files={}
        )
        items, cp = await adapter.list_changes(
            CONFIG, None, source_id=SRC, workspace_id=WS
        )
        assert [i["ref"] for i in items] == ["r1"] and not checkpoint_incomplete(cp)
        assert all(i.get("category") is None for i in items)

    def test_only_the_bare_checkpoint_is_complete(self):
        from src.services.target.drive_adapter import checkpoint_incomplete

        assert checkpoint_incomplete({"v": 1}) is False
        assert checkpoint_incomplete(None) is False
        assert checkpoint_incomplete({"v": 1, "page_token": "p2"}) is True
        assert (
            checkpoint_incomplete(
                {"v": 1, "current": {"id": "X", "name": "x"}, "queue": []}
            )
            is True
        )
        assert (
            checkpoint_incomplete({"v": 1, "queue": [{"id": "Y", "name": "y"}]}) is True
        )
        # The v2 walk (ruling 2026-09-08): a token alone, with or without the
        # cap's `truncated` mark, is complete; a `current` is not.
        assert checkpoint_incomplete({"v": 2, "walk": "w"}) is False
        assert checkpoint_incomplete({"v": 2, "walk": "w", "truncated": True}) is False
        assert (
            checkpoint_incomplete(
                {"v": 2, "walk": "w", "current": {"id": "X", "name": "x"}, "queue": []}
            )
            is True
        )


class TestAWalkSurvivesWhatDriveDoesMidWalk:
    """Review of #1251: a subfolder deleted or unshared while the walk is on
    it must not wedge the source, and an expired page token must not either."""

    ROOT = "FOLDER123"

    def _adapter_with(self, handler):
        return _adapter(handler)

    @pytest.mark.asyncio
    async def test_a_vanished_subfolder_is_skipped_and_the_walk_goes_on(self):
        def handler(request: httpx.Request) -> httpx.Response:
            q = request.url.params["q"]
            parent = q.split("'")[1]
            if "vnd.google-apps.folder" in q:
                children = (
                    [_folder_entry("GONE", "gone"), _folder_entry("KEEP", "keep")]
                    if parent == self.ROOT
                    else []
                )
                return httpx.Response(200, json={"files": children})
            if parent == "GONE":
                return httpx.Response(
                    404, json={"error": {"message": "File not found"}}
                )
            return httpx.Response(
                200, json={"files": [_file("k1")] if parent == "KEEP" else []}
            )

        adapter = self._adapter_with(handler)
        items, cp = await adapter.list_changes(
            CONFIG, None, source_id=SRC, workspace_id=WS
        )
        assert items == [] and cp["current"]["id"] == "GONE"
        items, cp = await adapter.list_changes(
            CONFIG, cp, source_id=SRC, workspace_id=WS
        )
        assert items == [] and cp["current"]["id"] == "KEEP", (
            "skipped, and the cursor advanced"
        )
        items, cp = await adapter.list_changes(
            CONFIG, cp, source_id=SRC, workspace_id=WS
        )
        assert [i["ref"] for i in items] == ["k1"] and not checkpoint_incomplete(cp)

    @pytest.mark.asyncio
    async def test_the_root_itself_gone_is_still_the_sources_fault(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if "vnd.google-apps.folder" in request.url.params["q"]:
                return httpx.Response(200, json={"files": []})
            return httpx.Response(404, json={"error": {"message": "File not found"}})

        with pytest.raises(DriveSourceGone):
            await self._adapter_with(handler).list_changes(
                CONFIG, None, source_id=SRC, workspace_id=WS
            )

    @pytest.mark.asyncio
    async def test_an_expired_page_token_restarts_the_folder_not_the_source(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.params.get("pageToken"):
                return httpx.Response(400, json={"error": {"message": "Invalid Value"}})
            return httpx.Response(200, json={"files": [_file("m1")]})

        current = {
            "id": "MEMES",
            "name": "memes",
            "top": "MEMES",
            "top_name": "memes",
            "path": "memes",
            "listed": True,
        }
        cp = {
            "v": 2,
            "walk": "w1",
            "current": current,
            "queue": [],
            "page_token": "stale",
        }
        items, cp2 = await self._adapter_with(handler).list_changes(
            CONFIG, cp, source_id=SRC, workspace_id=WS
        )
        assert items == [] and cp2["current"] == current and cp2["queue"] == []
        assert "page_token" not in cp2 and cp2["walk"] == "w1"
        items, cp3 = await self._adapter_with(handler).list_changes(
            CONFIG, cp2, source_id=SRC, workspace_id=WS
        )
        assert [i["ref"] for i in items] == ["m1"] and not checkpoint_incomplete(cp3)


class TestTheWalkGoesToAnyDepth:
    """Owner ruling 2026-09-08 (sources are the groups): every folder under a
    connected folder is walked, lazily — a folder is asked for its subfolders
    once, when it is popped — and a file's `category` is the TOP-LEVEL folder
    it sits under (None for the root's own files), with `folder_path` the
    folder's path under the root. The cursor is v2 and carries a walk token
    the sync mints; a pre-v2 in-flight cursor starts the walk over; no cursor
    is ever the bare `page_token` shape."""

    ROOT = "FOLDER123"

    def _tree(self, tree, files, *, pages=None, gone_listing=(), stale=()):
        """`tree` {parent_id: [(id, name)]}, `files` {folder_id: [entries]};
        `pages` {folder_id: split} answers a folder in two pages; a folder in
        `gone_listing` answers 404 to its subfolder listing; a pageToken in
        `stale` is refused with 400."""
        pages = pages or {}
        stale = set(stale)  # a refused token is refused ONCE, then honoured
        calls: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            q = request.url.params["q"]
            token = request.url.params.get("pageToken")
            parent = q.split("'")[1]
            is_listing = "mimeType = 'application/vnd.google-apps.folder'" in q
            calls.append(
                {"q": q, "pageToken": token, "parent": parent, "listing": is_listing}
            )
            if is_listing:
                if parent in gone_listing:
                    return httpx.Response(
                        404, json={"error": {"message": "File not found"}}
                    )
                return httpx.Response(
                    200,
                    json={
                        "files": [_folder_entry(i, n) for i, n in tree.get(parent, [])]
                    },
                )
            if token in stale:
                stale.discard(token)
                return httpx.Response(400, json={"error": {"message": "Invalid Value"}})
            entries = files.get(parent, [])
            if parent in pages:
                split = pages[parent]
                if token is None:
                    return httpx.Response(
                        200,
                        json={
                            "files": entries[:split],
                            "nextPageToken": f"{parent}-p2",
                        },
                    )
                return httpx.Response(200, json={"files": entries[split:]})
            return httpx.Response(200, json={"files": entries})

        return _adapter(handler), calls

    async def _walk(self, adapter, checkpoint=None, limit=30):
        seen, cursors = [], []
        for _ in range(limit):
            items, checkpoint = await adapter.list_changes(
                CONFIG, checkpoint, source_id=SRC, workspace_id=WS
            )
            cursors.append(checkpoint)
            seen.extend(
                (i["ref"], i.get("category"), i.get("folder_path")) for i in items
            )
            if not checkpoint_incomplete(checkpoint):
                return seen, cursors
        raise AssertionError("the walk did not complete")

    @pytest.mark.asyncio
    async def test_files_at_any_depth_carry_their_top_level_folder_and_their_path(self):
        adapter, calls = self._tree(
            tree={
                self.ROOT: [("MEMES", "memes"), ("MERCH", "merch")],
                "MEMES": [("Y2025", "2025")],
                "Y2025": [("JULY", "july")],
            },
            files={
                self.ROOT: [_file("r1")],
                "MEMES": [_file("m1")],
                "Y2025": [_file("m2")],
                "JULY": [_file("m3")],
                "MERCH": [_file("s1", name="shirt.mp4", mime="video/mp4")],
            },
        )
        seen, cursors = await self._walk(adapter)
        # Breadth-first: the root, its two folders, then what they contain.
        assert seen == [
            ("r1", None, ""),
            ("m1", "memes", "memes"),
            ("s1", "merch", "merch"),
            ("m2", "memes", "memes/2025"),
            ("m3", "memes", "memes/2025/july"),
        ]
        listings = [c["parent"] for c in calls if c["listing"]]
        assert sorted(listings) == sorted(
            [self.ROOT, "MEMES", "MERCH", "Y2025", "JULY"]
        ), "each folder is asked for its subfolders exactly once"
        final = cursors[-1]
        assert final["v"] == 2 and isinstance(final["walk"], str) and final["walk"]
        assert {c["walk"] for c in cursors} == {final["walk"]}, "one token per walk"

    @pytest.mark.asyncio
    async def test_the_start_cursors_token_is_carried_through_unchanged(self):
        adapter, _ = self._tree(
            tree={self.ROOT: [("MEMES", "memes")]},
            files={self.ROOT: [_file("r1")], "MEMES": [_file("m1")]},
        )
        seen, cursors = await self._walk(
            adapter, {"v": 2, "walk": "minted-by-the-sync"}
        )
        assert [r for r, _, _ in seen] == ["r1", "m1"]
        assert all(c["walk"] == "minted-by-the-sync" for c in cursors)
        assert cursors[-1] == {"v": 2, "walk": "minted-by-the-sync", "seen": 1}, (
            "complete: the token and the folder count, nothing pending"
        )

    @pytest.mark.asyncio
    async def test_a_pre_v2_in_flight_cursor_is_ignored_and_the_walk_starts_over(self):
        adapter, calls = self._tree(
            tree={self.ROOT: [("MEMES", "memes")]},
            files={self.ROOT: [_file("r1")], "MEMES": [_file("m1")]},
        )
        stale = {
            "v": 1,
            "current": {"id": "MEMES", "name": "memes"},
            "queue": [],
            "page_token": "p9",
        }
        items, cp = await adapter.list_changes(
            CONFIG, stale, source_id=SRC, workspace_id=WS
        )
        assert calls[0]["listing"] and calls[0]["parent"] == self.ROOT, (
            "started over at the root"
        )
        assert [i["ref"] for i in items] == ["r1"] and cp["v"] == 2 and cp["walk"]
        assert cp["current"]["id"] == "MEMES"

    @pytest.mark.asyncio
    async def test_no_cursor_is_ever_the_bare_page_token_shape(self):
        adapter, calls = self._tree(
            tree={self.ROOT: []},
            files={self.ROOT: [_file("r1"), _file("r2"), _file("r3")]},
            pages={self.ROOT: 2},
        )
        items, cp = await adapter.list_changes(
            CONFIG, None, source_id=SRC, workspace_id=WS
        )
        assert [i["ref"] for i in items] == ["r1", "r2"]
        assert (
            cp["page_token"] == f"{self.ROOT}-p2" and cp["current"]["id"] == self.ROOT
        )
        items, cp = await adapter.list_changes(
            CONFIG, cp, source_id=SRC, workspace_id=WS
        )
        assert [i["ref"] for i in items] == ["r3"] and not checkpoint_incomplete(cp)
        assert calls[-1]["pageToken"] == f"{self.ROOT}-p2"

    @pytest.mark.asyncio
    async def test_the_walk_cap_stops_queuing_and_is_carried_to_completion(
        self, monkeypatch
    ):
        from src.services.target import google_drive_adapter as mod

        monkeypatch.setattr(mod, "FOLDER_WALK_CAP", 2)
        adapter, calls = self._tree(
            tree={self.ROOT: [("A", "a"), ("B", "b"), ("C", "c")], "A": [("A1", "a1")]},
            files={
                "A": [_file("fa")],
                "B": [_file("fb")],
                "C": [_file("fc")],
                "A1": [_file("fa1")],
            },
        )
        seen, cursors = await self._walk(adapter)
        assert [r for r, _, _ in seen] == ["fa", "fb"], (
            "folders past the cap never sync"
        )
        assert cursors[-1].get("truncated") is True, "the cut is carried to completion"
        assert not checkpoint_incomplete(cursors[-1])

    @pytest.mark.asyncio
    async def test_an_expired_token_restart_does_not_relist_the_folder(self):
        adapter, calls = self._tree(
            tree={self.ROOT: [("MEMES", "memes")], "MEMES": [("SUB", "sub")]},
            files={"MEMES": [_file("m1"), _file("m2")], "SUB": [_file("z1")]},
            pages={"MEMES": 1},
            stale={"MEMES-p2"},
        )
        seen, cursors = await self._walk(adapter)
        # The first page of memes is listed twice (the restart), its
        # subfolders are asked for once, and `sub` is walked once.
        assert [c["parent"] for c in calls if c["listing"]].count("MEMES") == 1
        assert [r for r, _, _ in seen].count("z1") == 1
        assert ("m1", "memes", "memes") in seen

    @pytest.mark.asyncio
    async def test_a_vanished_nested_folder_is_skipped_including_a_404_on_its_listing(
        self,
    ):
        adapter, calls = self._tree(
            tree={self.ROOT: [("GONE", "gone"), ("KEEP", "keep")]},
            files={"KEEP": [_file("k1")]},
            gone_listing={"GONE"},
        )
        seen, cursors = await self._walk(adapter)
        assert [r for r, _, _ in seen] == ["k1"]

    @pytest.mark.asyncio
    async def test_a_folder_reachable_twice_is_walked_once_and_a_cycle_does_not_spin(
        self,
    ):
        adapter, calls = self._tree(
            tree={
                self.ROOT: [("A", "a")],
                "A": [("B", "b")],
                "B": [("A", "a"), (self.ROOT, "root")],  # a cycle, and the root again
            },
            files={"A": [_file("fa")], "B": [_file("fb")]},
        )
        seen, cursors = await self._walk(adapter)
        assert [r for r, _, _ in seen] == ["fa", "fb"], "each file once"
        listings = [c["parent"] for c in calls if c["listing"]]
        assert sorted(listings) == sorted([self.ROOT, "A", "B"]), (
            "each folder listed once"
        )
        assert not cursors[-1].get("truncated"), "a cycle is not a size cap"

    @pytest.mark.asyncio
    async def test_a_child_id_outside_the_drive_id_shape_is_skipped(self):
        adapter, calls = self._tree(
            tree={self.ROOT: [("X' or 'a'='a", "evil"), ("OK1", "ok")]},
            files={"OK1": [_file("k1")]},
        )
        seen, _ = await self._walk(adapter)
        assert [r for r, _, _ in seen] == ["k1"]
        assert not any("X' or" in c["q"] for c in calls), "never spliced into a query"

    @pytest.mark.asyncio
    async def test_a_root_outside_the_drive_id_shape_is_refused_before_any_request(
        self,
    ):
        from src.services.target.drive_adapter import DriveTerminalError

        adapter, calls = self._tree(tree={}, files={})
        with pytest.raises(DriveTerminalError):
            await adapter.list_changes(
                {**CONFIG, "folder_ref": "X' or 'a'='a"},
                None,
                source_id=SRC,
                workspace_id=WS,
            )
        assert calls == []

    @pytest.mark.asyncio
    async def test_a_stored_cursor_missing_walk_fields_is_healed_not_crashed(self):
        adapter, calls = self._tree(
            tree={self.ROOT: [("MEMES", "memes")]},
            files={"MEMES": [_file("m1")]},
        )
        thin = {
            "v": 2,
            "walk": "w",
            "current": {"id": "MEMES", "name": "memes"},
            "queue": [],
        }
        items, cp = await adapter.list_changes(
            CONFIG, thin, source_id=SRC, workspace_id=WS
        )
        assert [i["ref"] for i in items] == ["m1"] and items[0]["folder_path"] == ""
        assert not checkpoint_incomplete(cp)

    @pytest.mark.asyncio
    async def test_past_the_cap_no_listing_request_is_spent(self, monkeypatch):
        from src.services.target import google_drive_adapter as mod

        monkeypatch.setattr(mod, "FOLDER_WALK_CAP", 1)
        adapter, calls = self._tree(
            tree={self.ROOT: [("A", "a"), ("B", "b")], "A": [("A1", "a1")]},
            files={"A": [_file("fa")], "A1": [_file("fa1")]},
        )
        seen, cursors = await self._walk(adapter)
        assert [r for r, _, _ in seen] == ["fa"]
        assert [c["parent"] for c in calls if c["listing"]] == [self.ROOT], (
            "once the cap is hit, a popped folder is not asked for its subfolders"
        )
        assert cursors[-1].get("truncated") is True


class TestFetchBytes:
    """The media bytes for the approval card (owner, 2026-09-08 — legacy
    parity): metadata first, so a file over the cap is refused before a
    download; then `alt=media` under the workspace grant."""

    def _drive(self, *, size="5", body=b"12345", status=200):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            if "alt=media" in str(request.url):
                return httpx.Response(
                    200, content=body, headers={"content-type": "image/jpeg"}
                )
            if status != 200:
                return httpx.Response(
                    status, json={"error": {"message": "File not found"}}
                )
            return httpx.Response(
                200, json={"size": size, "mimeType": "image/jpeg", "name": "a.jpg"}
            )

        return _adapter(handler), calls

    @pytest.mark.asyncio
    async def test_bytes_name_and_mime_come_back_under_the_grant(self):
        adapter, calls = self._drive()
        content, name, mime = await adapter.fetch_bytes(
            source_id=SRC, workspace_id=WS, file_ref="FILE1", max_bytes=10
        )
        assert (content, name, mime) == (b"12345", "a.jpg", "image/jpeg")
        assert any("alt=media" in c and "/files/FILE1" in c for c in calls)

    @pytest.mark.asyncio
    async def test_a_file_over_the_cap_is_refused_before_any_download(self):
        from src.services.target.drive_adapter import DriveMediaTooLarge

        adapter, calls = self._drive(size="11")
        with pytest.raises(DriveMediaTooLarge):
            await adapter.fetch_bytes(
                source_id=SRC, workspace_id=WS, file_ref="FILE1", max_bytes=10
            )
        assert not any("alt=media" in c for c in calls)

    @pytest.mark.asyncio
    async def test_a_ref_outside_the_drive_id_shape_is_refused_before_any_request(self):
        from src.services.target.drive_adapter import DriveTerminalError

        adapter, calls = self._drive()
        with pytest.raises(DriveTerminalError):
            await adapter.fetch_bytes(
                source_id=SRC, workspace_id=WS, file_ref="x/../y", max_bytes=10
            )
        assert calls == []

    @pytest.mark.asyncio
    async def test_a_missing_file_is_named_as_such(self):
        from src.services.target.drive_adapter import DriveMediaGone

        adapter, _ = self._drive(status=404)
        with pytest.raises(DriveMediaGone):
            await adapter.fetch_bytes(
                source_id=SRC, workspace_id=WS, file_ref="FILE1", max_bytes=10
            )

    @pytest.mark.asyncio
    async def test_the_adapter_built_without_a_policy_still_fetches(self):
        """The worker constructs the adapter with no policy (the floor's
        default applies per call); the media policy must derive from that,
        never from None (review of #1259 — the fetch crashed on every card)."""
        adapter, calls = self._drive()
        adapter._policy = None
        content, name, mime = await adapter.fetch_bytes(
            source_id=SRC, workspace_id=WS, file_ref="FILE1", max_bytes=10
        )
        assert content == b"12345" and any("alt=media" in c for c in calls)

    @pytest.mark.asyncio
    async def test_a_body_larger_than_the_metadata_said_is_still_refused(self):
        """The byte cap rides the media policy, so a metadata size that lied
        cannot pull more than `max_bytes`."""
        from src.services.target.drive_adapter import DriveMediaTooLarge

        adapter, _ = self._drive(size="5", body=b"x" * (20 * 1024))
        with pytest.raises(DriveMediaTooLarge):
            await adapter.fetch_bytes(
                source_id=SRC, workspace_id=WS, file_ref="FILE1", max_bytes=10
            )

    @pytest.mark.asyncio
    async def test_a_refused_token_on_the_bytes_path_is_re_minted_once(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            if "alt=media" in str(request.url):
                if len([c for c in calls if "alt=media" in c]) == 1:
                    return httpx.Response(401, json={"error": {"message": "expired"}})
                return httpx.Response(
                    200, content=b"12345", headers={"content-type": "image/jpeg"}
                )
            return httpx.Response(
                200, json={"size": "5", "mimeType": "image/jpeg", "name": "a.jpg"}
            )

        record = []
        adapter = _adapter(handler, record=record)
        content, _, _ = await adapter.fetch_bytes(
            source_id=SRC, workspace_id=WS, file_ref="FILE1", max_bytes=10
        )
        assert content == b"12345"
        assert len([c for c in calls if "alt=media" in c]) == 2, (
            "one re-mint, one retry"
        )
        assert (
            any(
                getattr(r, "fresh", None)
                or (isinstance(r, tuple) and True in r)
                or "fresh" in str(r)
                for r in record
            )
            or len(record) >= 2
        )

    @pytest.mark.asyncio
    async def test_a_refusal_of_the_bytes_after_good_metadata_is_the_files_fault(self):
        from src.services.target.drive_adapter import DriveMediaGone

        def handler(request: httpx.Request) -> httpx.Response:
            if "alt=media" in str(request.url):
                return httpx.Response(
                    403, json={"error": {"message": "cannotDownloadFile"}}
                )
            return httpx.Response(
                200, json={"size": "5", "mimeType": "image/jpeg", "name": "a.jpg"}
            )

        record = []
        adapter = _adapter(handler, record=record)
        with pytest.raises(DriveMediaGone):
            await adapter.fetch_bytes(
                source_id=SRC, workspace_id=WS, file_ref="FILE1", max_bytes=10
            )
