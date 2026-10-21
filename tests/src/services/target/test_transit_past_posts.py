"""Past-posts source actual multipart sends, uncertainty and exact Drive compatibility."""

import dataclasses
from datetime import datetime, timezone
import email.parser
import email.policy
import cloudinary
import cloudinary.exceptions
import cloudinary.uploader
import cloudinary.utils
import httpx
import pytest
from src.services.target import transit

WS = "0b6ad24e-6b3e-4a86-9e6b-1a2b3c4d5e6f"
REF = f"ws/{WS}/" + "a" * 32
NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)


def affirmative(**changes):
    body = dict(
        public_id=REF,
        asset_id="b" * 32,
        version=123,
        resource_type="image",
        type="authenticated",
    )
    body.update(changes)
    body["signature"] = cloudinary.utils.api_sign_request(
        {"public_id": body["public_id"], "version": body["version"]},
        "test-secret",
        signature_version=1,
    )
    return body


def store(monkeypatch, handler, **seams):
    options = []

    def factory(**kwargs):
        options.append(kwargs)
        return httpx.MockTransport(handler)

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", factory)

    def sdk(*args, **kwargs):
        raise AssertionError("past-posts source must not use SDK connector")

    return transit.TransitStore(
        cloud_name="test-cloud",
        api_key="test-key",
        api_secret="test-secret",
        now_fn=lambda: NOW,
        upload_fn=sdk,
        past_posts_resolver=lambda host: ["93.184.216.34"],
        **seams,
    ), options


def parts(request):
    prefix = (
        "Content-Type: "
        + request.headers["content-type"]
        + "\r\nMIME-Version: 1.0\r\n\r\n"
    ).encode()
    message = email.parser.BytesParser(policy=email.policy.default).parsebytes(
        prefix + request.content
    )
    return {
        p.get_param("name", header="content-disposition"): p.get_payload(decode=True)
        for p in message.iter_parts()
    }


@pytest.mark.parametrize("kind", ["image", "video"])
async def test_actual_signed_multipart_and_matching_creation(monkeypatch, kind):
    sent = []

    async def handler(request):
        await request.aread()
        sent.append(request)
        return httpx.Response(200, json=affirmative(resource_type=kind))

    connector = cloudinary.uploader._http
    config = dict(vars(cloudinary.config()))
    upload, options = store(monkeypatch, handler)
    result = await upload.upload(
        b"selected-bytes",
        workspace_id=WS.upper(),
        media_kind=kind,
        preallocated_ref=REF,
    )
    assert isinstance(result, transit.PastPostsUploadEvidence)
    assert (
        result.public_id,
        result.asset_id,
        result.version,
        result.resource_type,
        result.delivery_type,
    ) == (REF, "b" * 32, 123, kind, "authenticated")
    assert result.signature == affirmative()["signature"]
    assert len(sent) == 1 and sent[0].method == "POST"
    assert str(sent[0].url) == f"https://93.184.216.34/v1_1/test-cloud/{kind}/upload"
    assert (
        sent[0].headers["host"] == "api.cloudinary.com"
        and "authorization" not in sent[0].headers
    )
    assert options == [{"retries": 0, "trust_env": False}]
    fields = parts(sent[0])
    assert fields.pop("file") == b"selected-bytes"
    decoded = {k: v.decode() for k, v in fields.items()}
    signature = decoded.pop("signature")
    assert decoded.pop("api_key") == "test-key"
    assert set(decoded) == {
        "timestamp",
        "public_id",
        "type",
        "overwrite",
        "eager",
        "eager_async",
    }
    assert (
        decoded["public_id"] == REF
        and decoded["type"] == "authenticated"
        and decoded["overwrite"] == "0"
    )
    assert decoded["eager_async"] == ("1" if kind == "video" else "0") and decoded[
        "timestamp"
    ] == str(int(NOW.timestamp()))
    expected = cloudinary.utils.build_eager(
        [
            {
                "transformation": transit.story_transformation(REF, media_kind=kind),
                "format": transit.STORY_FORMATS[kind],
            }
        ]
    )
    assert decoded["eager"] == expected
    assert signature == cloudinary.utils.api_sign_request(
        decoded, "test-secret", signature_version=2
    )
    assert (
        cloudinary.uploader._http is connector and vars(cloudinary.config()) == config
    )


@pytest.mark.parametrize("kind", ["image", "video"])
async def test_a_lookup_of_the_created_asset_yields_the_evidence_the_upload_returned(
    monkeypatch, kind
):
    """The two ways to learn an asset exists must agree field for field: the
    ledger refuses a second, different piece of creation evidence for one
    attempt (`creation_conflict`), so a settle by lookup after a late upload
    completion — or the other way round — must not trip it.
    Mutations that redden: compute the lookup's signature differently from the
    upload's check; build any lookup evidence field from another source."""
    details = []

    def resource(public_id, **options):
        details.append((public_id, options))
        return {
            "asset_id": "b" * 32,
            "public_id": public_id,
            "version": 123,
            "resource_type": kind,
            "type": "authenticated",
            "bytes": 1,
        }

    upload, _ = store(
        monkeypatch,
        lambda request: httpx.Response(200, json=affirmative(resource_type=kind)),
        resource_fn=resource,
    )
    created = await upload.upload(
        b"selected-bytes", workspace_id=WS, media_kind=kind, preallocated_ref=REF
    )
    found = await upload.past_posts_lookup(REF, resource_type=kind)
    assert isinstance(created, transit.PastPostsUploadEvidence)
    assert found == created
    assert dataclasses.asdict(found) == dataclasses.asdict(created)
    assert [public_id for public_id, _ in details] == [REF]


class Provider:
    """One asset's life at the provider, seen through the SDK doors."""

    def __init__(self):
        self.exists = False

    def created(self, request):
        self.exists = True
        return httpx.Response(200, json=affirmative())

    def resource(self, public_id, **options):
        if not self.exists:
            raise cloudinary.exceptions.NotFound(
                f"Error 404 - Resource not found - {public_id}"
            )
        return {
            "asset_id": "b" * 32,
            "public_id": public_id,
            "version": 123,
            "resource_type": "image",
            "type": "authenticated",
        }

    def destroy(self, public_id, **options):
        was, self.exists = self.exists, False
        return {"result": "ok" if was else "not found"}


async def test_a_late_creation_after_absent_answers_is_settled_destroyed_and_verified(
    monkeypatch,
):
    """A `not found` before a held upload lands proves nothing: the lookup and
    the destroy both said absent, then the asset appeared, and the next pass
    finds exactly what the upload returned, destroys it and verifies it gone.
    The order the cleanup follows (settle, destroy, verify) is the caller's;
    these doors only have to keep the answers apart and honest.
    Mutations that redden: answer evidence or `destroyed` while the asset does
    not exist; answer None or `not_found` while it does; answer `destroyed`
    for `not found`."""
    provider = Provider()
    transit_store, _ = store(
        monkeypatch,
        provider.created,
        resource_fn=provider.resource,
        destroy_fn=provider.destroy,
    )
    # The upload is held: absent now, and a destroy now removes nothing.
    assert await transit_store.past_posts_lookup(REF, resource_type="image") is None
    assert await transit_store.destroy_past_posts(REF, resource_type="image") == (
        "not_found"
    )
    # The held upload lands.
    created = await transit_store.upload(
        b"selected-bytes", workspace_id=WS, media_kind="image", preallocated_ref=REF
    )
    # Settle: the lookup finds exactly the evidence the upload returned.
    assert await transit_store.past_posts_lookup(REF, resource_type="image") == created
    # Destroy: this time the provider held it. Verify: now it is gone.
    assert await transit_store.destroy_past_posts(REF, resource_type="image") == (
        "destroyed"
    )
    assert await transit_store.past_posts_lookup(REF, resource_type="image") is None


@pytest.mark.parametrize(
    "fault",
    ["timeout", "connect", "read", "500", "400", "same_redirect", "cross_redirect"],
)
async def test_errors_redirects_and_timeouts_send_once_and_remain_uncertain(
    monkeypatch, fault
):
    sent = []

    def handler(request):
        sent.append(request)
        if fault == "timeout":
            raise httpx.ReadTimeout("lost answer", request=request)
        if fault == "connect":
            raise httpx.ConnectError("unknown", request=request)
        if fault == "read":
            raise httpx.ReadError("unknown", request=request)
        if fault.endswith("redirect"):
            return httpx.Response(
                307,
                headers={
                    "location": "https://api.cloudinary.com/again"
                    if fault == "same_redirect"
                    else "https://other.example/again"
                },
            )
        return httpx.Response(
            int(fault), json={"error": {"message": "private provider body"}}
        )

    upload, _ = store(monkeypatch, handler)
    with pytest.raises(transit.PastPostsUploadUncertain) as caught:
        await upload.upload(
            b"bytes", workspace_id=WS, media_kind="image", preallocated_ref=REF
        )
    assert len(sent) == 1 and not isinstance(caught.value, transit.TransitError)
    assert "private provider body" not in str(caught.value)
    assert not hasattr(caught.value, "created") and not hasattr(
        caught.value, "no_effect"
    )


@pytest.mark.parametrize(
    "change",
    [
        {"public_id": REF + "c"},
        {"asset_id": None},
        {"asset_id": "has space"},
        {"asset_id": "x" * 129},
        {"version": True},
        {"version": 0},
        {"resource_type": "video"},
        {"type": "upload"},
        {"signature": "wrong"},
        {"signature": "é" * 40},
        # Well formed, and not the one this store computes for the creation.
        {"signature": "0" * 40},
        {"version": 2**63},
    ],
)
async def test_mismatched_malformed_affirmative_response_stays_uncertain(
    monkeypatch, change
):
    sent = []

    def handler(request):
        sent.append(request)
        body = affirmative(**{k: v for k, v in change.items() if k != "signature"})
        if "signature" in change:
            body["signature"] = change["signature"]
        return httpx.Response(200, json=body)

    upload, _ = store(monkeypatch, handler)
    with pytest.raises(transit.PastPostsUploadUncertain):
        await upload.upload(
            b"bytes", workspace_id=WS, media_kind="image", preallocated_ref=REF
        )
    assert len(sent) == 1


@pytest.mark.parametrize("body", [b"not-json", b"[]", b"{}", b"x" * (64 * 1024 + 1)])
async def test_response_bytes_are_bounded_and_invalid_response_is_uncertain(
    monkeypatch, body
):
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, content=body)

    upload, _ = store(monkeypatch, handler)
    with pytest.raises(transit.PastPostsUploadUncertain):
        await upload.upload(
            b"bytes", workspace_id=WS, media_kind="image", preallocated_ref=REF
        )
    assert len(sent) == 1


@pytest.mark.parametrize(
    "ref",
    [
        "https://evil.example/file",
        REF.replace(WS, "11111111-1111-1111-1111-111111111111"),
        REF + "/nested",
        REF.upper(),
        f"ws/{WS}/../bad",
    ],
)
async def test_reference_is_canonical_random_and_workspace_owned_before_send(
    monkeypatch, ref
):
    sent = []
    upload, _ = store(monkeypatch, lambda request: sent.append(request))
    with pytest.raises(ValueError):
        await upload.upload(
            b"bytes", workspace_id=WS, media_kind="image", preallocated_ref=ref
        )
    assert sent == []


@pytest.mark.parametrize(
    "content", ["https://evil.example/media", bytearray(b"bytes"), None, b""]
)
async def test_past_posts_upload_only_accepts_nonempty_immutable_bytes(
    monkeypatch, content
):
    sent = []
    upload, _ = store(monkeypatch, lambda request: sent.append(request))
    with pytest.raises(ValueError):
        await upload.upload(
            content, workspace_id=WS, media_kind="image", preallocated_ref=REF
        )
    assert sent == []


async def test_globals_cannot_select_credentials_preset_host_or_transform_defaults(
    monkeypatch,
):
    config = cloudinary.config()
    for key, value in {
        "cloud_name": "other-cloud",
        "api_key": "other-key",
        "api_secret": "other-secret",
        "upload_preset": "unsafe-preset",
        "upload_prefix": "https://other.example",
        "oauth_token": "unsafe-bearer",
        "responsive_width": True,
        "dpr": "auto",
        "use_fetch_format": True,
    }.items():
        monkeypatch.setattr(config, key, value, raising=False)
    sent = []

    async def handler(request):
        await request.aread()
        sent.append(request)
        return httpx.Response(200, json=affirmative())

    upload, _ = store(monkeypatch, handler)
    await upload.upload(
        b"bytes", workspace_id=WS, media_kind="image", preallocated_ref=REF
    )
    fields = parts(sent[0])
    assert fields["api_key"] == b"test-key"
    assert b"dpr_auto" not in fields["eager"] and b"w_auto" not in fields["eager"]
    assert "upload_preset" not in fields and "authorization" not in sent[0].headers
    assert config.upload_preset == "unsafe-preset" and config.api_key == "other-key"


async def test_cancelled_dispatch_is_not_creation_or_no_effect(monkeypatch):
    import asyncio

    sent = []

    def handler(request):
        sent.append(request)
        raise asyncio.CancelledError()

    upload, _ = store(monkeypatch, handler)
    with pytest.raises(asyncio.CancelledError):
        await upload.upload(
            b"bytes", workspace_id=WS, media_kind="image", preallocated_ref=REF
        )
    assert len(sent) == 1


async def test_drive_no_reference_keeps_sync_sdk_and_string_return():
    calls = []

    def sdk(content, **kwargs):
        calls.append((content, kwargs))
        return {"public_id": kwargs["public_id"]}

    upload = transit.TransitStore(
        cloud_name="test-cloud",
        api_key="test-key",
        api_secret="test-secret",
        upload_fn=sdk,
    )
    result = await upload.upload(b"drive-bytes", workspace_id=WS, media_kind="image")
    assert isinstance(result, str) and result.startswith(f"ws/{WS}/")
    assert (
        len(calls) == 1
        and calls[0][0] == b"drive-bytes"
        and calls[0][1]["overwrite"] is False
    )


@pytest.mark.parametrize(
    "cloud", ["../other", "name/other", "name?query", "https://other.example"]
)
async def test_cloud_name_is_a_fixed_provider_path_component_before_send(
    monkeypatch, cloud
):
    sent = []

    def factory(**kwargs):
        return httpx.MockTransport(lambda request: sent.append(request))

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", factory)
    upload = transit.TransitStore(
        cloud_name=cloud,
        api_key="test-key",
        api_secret="test-secret",
        past_posts_resolver=lambda host: ["93.184.216.34"],
    )
    with pytest.raises(ValueError):
        await upload.upload(
            b"bytes", workspace_id=WS, media_kind="image", preallocated_ref=REF
        )
    assert sent == []
