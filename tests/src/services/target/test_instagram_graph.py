"""The real Meta adapter (#1220 step 3): what it sends, how it maps answers
to the typed taxonomy the pipeline routes on, that an effect is attempted
ONCE, and that the token never leaks."""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest

from src.services.target.ig_credentials import IgCredentialDead
from src.services.target.instagram_graph import (
    OAUTH_ERROR_CODE,
    InstagramGraphAdapter,
)
from src.services.target.meta_adapter import (
    MetaCapDeferral,
    MetaLostResponse,
    MetaRetryableError,
    MetaTerminalError,
)

TOKEN = "IGQVJsecretTOKENvalue"
REF = "17841400000000001"
WS = "ws-1"


def _adapter(handler, *, token=TOKEN, resolver=None, seen_reads=None):
    async def token_for_account(ref, *, workspace_id=None):
        if seen_reads is not None:
            seen_reads.append((ref, workspace_id))
        if token is None:
            raise IgCredentialDead(
                f"no ig_login credential for Instagram account {ref}"
                " — connect Instagram for this destination"
            )
        return token

    return InstagramGraphAdapter(
        token_for_account=token_for_account,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        # The floor pins the host to a resolved address; a static answer keeps
        # DNS out of the test and the request's Host header names the host.
        resolver=resolver or (lambda host: ["93.184.216.34"]),
    )


def _form(request) -> dict:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


class TestWhatItSends:
    async def test_an_image_story_container_pulls_the_image_url_with_a_bearer_token(
        self,
    ):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["host"] = request.headers["host"]
            seen["path"] = request.url.path
            seen["auth"] = request.headers.get("authorization")
            seen["form"] = _form(request)
            return httpx.Response(200, json={"id": "ctr-1"})

        a = _adapter(handler)
        assert (
            await a.create_container(
                REF, media_url="https://cdn/x.jpg", media_kind="image"
            )
            == "ctr-1"
        )
        assert seen["host"] == "graph.instagram.com"
        assert seen["path"] == f"/v21.0/{REF}/media"
        assert seen["auth"] == f"Bearer {TOKEN}"
        assert seen["form"] == {
            "media_type": "STORIES",
            "image_url": "https://cdn/x.jpg",
        }
        assert TOKEN not in seen["url"]

    async def test_a_video_story_container_uses_video_url(self):
        seen = {}

        def handler(request):
            seen["form"] = _form(request)
            return httpx.Response(200, json={"id": "ctr-2"})

        await _adapter(handler).create_container(
            REF, media_url="https://cdn/x.mp4", media_kind="video", caption="ignored"
        )
        assert (
            seen["form"]["video_url"] == "https://cdn/x.mp4"
            and "caption" not in seen["form"]
        )

    async def test_status_publish_and_usage_name_their_endpoints(self):
        calls = []

        def handler(request):
            path = request.url.path
            calls.append((request.method, path))
            assert request.headers["authorization"] == f"Bearer {TOKEN}"
            assert "access_token" not in str(request.url)
            if path.endswith("/media_publish"):
                assert _form(request) == {"creation_id": "ctr-1"}
                return httpx.Response(200, json={"id": "media-9"})
            if path.endswith("/content_publishing_limit"):
                return httpx.Response(
                    200,
                    json={"data": [{"quota_usage": 3, "config": {"quota_total": 100}}]},
                )
            assert request.url.params["fields"] == "status_code,status"
            return httpx.Response(200, json={"id": "ctr-1", "status_code": "FINISHED"})

        a = _adapter(handler)
        assert await a.container_status("ctr-1", provider_account_ref=REF) == "FINISHED"
        assert await a.publish(REF, "ctr-1") == "media-9"
        assert await a.usage(REF) == {"quota_usage": 3, "quota_total": 100}
        assert calls == [
            ("GET", "/v21.0/ctr-1"),
            ("POST", f"/v21.0/{REF}/media_publish"),
            ("GET", f"/v21.0/{REF}/content_publishing_limit"),
        ]

    async def test_the_workspace_reaches_the_token_reader(self):
        reads = []

        def handler(request):
            return httpx.Response(200, json={"id": "x", "status_code": "FINISHED"})

        a = _adapter(handler, seen_reads=reads)
        await a.create_container(
            REF, media_url="u", media_kind="image", workspace_id=WS
        )
        await a.container_status("ctr-1", provider_account_ref=REF, workspace_id=WS)
        await a.publish(REF, "ctr-1", workspace_id=WS)
        assert reads == [(REF, WS)] * 3


class TestOneAttemptPerEffect:
    """The floor's default retries a transport fault; an effect must not be
    POSTed twice (#1276 review: a ReadTimeout after Meta accepted the publish
    re-sent the same creation_id)."""

    async def test_a_transport_fault_on_publish_is_lost_after_exactly_one_request(self):
        requests = []

        def handler(request):
            requests.append(request.method)
            raise httpx.ReadTimeout("slow")

        with pytest.raises(MetaLostResponse):
            await _adapter(handler).publish(REF, "ctr-1")
        assert requests == ["POST"]

    async def test_a_transport_fault_on_create_is_lost_after_exactly_one_request(self):
        requests = []

        def handler(request):
            requests.append(request.method)
            raise httpx.ConnectError("boom")

        with pytest.raises(MetaLostResponse):
            await _adapter(handler).create_container(
                REF, media_url="u", media_kind="image"
            )
        assert requests == ["POST"]

    async def test_a_read_may_retry_once(self):
        requests = []

        def handler(request):
            requests.append(request.method)
            if len(requests) == 1:
                raise httpx.ReadTimeout("slow")
            return httpx.Response(200, json={"status_code": "IN_PROGRESS"})

        assert (
            await _adapter(handler).container_status("ctr-1", provider_account_ref=REF)
            == "IN_PROGRESS"
        )
        assert requests == ["GET", "GET"]


class TestTheTaxonomy:
    @pytest.mark.parametrize(
        "code, expected",
        [
            (9, MetaCapDeferral),
            (9004, MetaTerminalError),
            (4, MetaRetryableError),
            (190, MetaRetryableError),
        ],
    )
    async def test_an_error_body_is_routed_by_code(self, code, expected):
        def handler(request):
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": f"nope {TOKEN}",
                        "code": code,
                        "error_subcode": 2207001,
                    }
                },
            )

        with pytest.raises(expected) as info:
            await _adapter(handler).create_container(
                REF, media_url="u", media_kind="image"
            )
        assert info.value.code == code and TOKEN not in str(info.value)

    async def test_a_transport_failure_is_a_lost_response_without_the_token(self):
        def handler(request):
            raise httpx.ConnectError(f"boom {TOKEN}")

        with pytest.raises(MetaLostResponse) as info:
            await _adapter(handler).publish(REF, "ctr-1")
        assert TOKEN not in str(info.value)

    async def test_a_refused_egress_is_retryable_not_lost(self):
        """The floor refusing to call (a private address here) means nothing
        left the process: the effect did not happen, so the ladder may retry.
        A park would hand a DNS blip to a human a day later."""

        def handler(request):  # pragma: no cover — the floor never calls it
            raise AssertionError("refused calls do not reach the transport")

        a = _adapter(handler, resolver=lambda host: ["10.0.0.7"])
        with pytest.raises(MetaRetryableError) as info:
            await a.publish(REF, "ctr-1")
        assert info.value.code == 0 and "refused" in str(info.value)

    async def test_a_5xx_and_a_non_json_answer_are_lost_responses(self):
        def five(request):
            return httpx.Response(503, json={"error": {"message": "down", "code": 2}})

        def html(request):
            return httpx.Response(200, text="<html>")

        with pytest.raises(MetaLostResponse):
            await _adapter(five).publish(REF, "ctr-1")
        with pytest.raises(MetaLostResponse):
            await _adapter(html).publish(REF, "ctr-1")

    async def test_an_answer_without_an_id_is_a_lost_response(self):
        def handler(request):
            return httpx.Response(200, json={"ok": True})

        with pytest.raises(MetaLostResponse):
            await _adapter(handler).create_container(
                REF, media_url="u", media_kind="image"
            )

    async def test_a_dead_credential_is_the_retryable_oauth_error(self):
        def handler(request):  # pragma: no cover — never reached
            raise AssertionError("no call without a token")

        with pytest.raises(MetaRetryableError) as info:
            await _adapter(handler, token=None).create_container(
                REF, media_url="u", media_kind="image"
            )
        assert (
            info.value.code == OAUTH_ERROR_CODE and "connect" in str(info.value).lower()
        )

    async def test_usage_tolerates_an_object_shaped_data_field(self):
        def handler(request):
            return httpx.Response(
                200, json={"data": {"quota_usage": "7", "config": {"quota_total": 25}}}
            )

        assert await _adapter(handler).usage(REF) == {
            "quota_usage": 7,
            "quota_total": 25,
        }
