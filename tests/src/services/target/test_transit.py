"""The transit store (#915).

Every assertion here is a named requirement, asserted at the
injected SDK boundary (a recording fake), because the property under test IS
the parameter set that rides in the signed request. Scope stated honestly:
Cloudinary's signature computation is the SDK's contract and is not re-tested
here; what is ours — and what these tests pin — is WHICH parameters are in
the signed request and WHICH SDK door is called.

Mutation discipline (the green-is-not-coverage rule): every test
names, in its docstring, the production mutation that reddens it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import socket
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import cloudinary.exceptions
import pytest

from src.services.target.transit import (
    PastPostsUploadEvidence,
    PastPostsUploadUncertain,
    TransitDestroyRefused,
    TransitStore,
)
from tests.src.services.target.test_media_validation import (
    audio_entry,
    derivation,
    moov_end,
)

WS = "0b6ad24e-6b3e-4a86-9e6b-1a2b3c4d5e6f"  # a real UUID — folder scoping is validated
NOW = datetime(2026, 8, 19, 22, 0, 0, tzinfo=timezone.utc)
#: A past-posts reference — the one canonical shape the upload sends and the
#: database admits — and the version the provider gave its asset.
REF = f"ws/{WS}/" + "a" * 32
VERSION = 1719304173


class RecordingSdk:
    """A recorder standing where the cloudinary SDK callables stand.

    Returns SDK-shaped payloads so the store's parsing is the real code path;
    records every call's kwargs so the signed-parameter set is assertable.
    *resource_result* is the Admin API's answer for one asset's details and
    *resource_error* what that door raises instead (the SDK raises on a 404).
    """

    def __init__(
        self,
        *,
        resources_pages=None,
        destroy_result=None,
        resource_result=None,
        resource_error=None,
    ):
        self.upload_calls: list[dict] = []
        self.destroy_calls: list[dict] = []
        self.resources_calls: list[dict] = []
        self.resource_calls: list[dict] = []
        self.url_calls: list[dict] = []
        self._resources_pages = resources_pages or {}
        self._destroy_result = destroy_result or {"result": "ok"}
        self._resource_result = resource_result
        self._resource_error = resource_error

    def upload(self, source, **options):
        self.upload_calls.append({"source": source, **options})
        folder = options.get("folder", "")
        if options.get("public_id"):
            return {"public_id": options["public_id"], "secure_url": "https://x/y"}
        return {
            "public_id": f"{folder}/r4nd0m1d" if folder else "r4nd0m1d",
            "secure_url": "https://res.cloudinary.com/x/raw",
            "resource_type": options.get("resource_type", "image"),
        }

    def destroy(self, public_id, **options):
        self.destroy_calls.append({"public_id": public_id, **options})
        return dict(self._destroy_result)

    def resources(self, **options):
        self.resources_calls.append(dict(options))
        pages = self._resources_pages.get(options.get("resource_type", "image"), [{}])
        idx = 0
        if options.get("next_cursor"):
            idx = int(options["next_cursor"])
        page = dict(pages[idx]) if idx < len(pages) else {}
        page.setdefault("resources", [])
        if idx + 1 < len(pages):
            page["next_cursor"] = str(idx + 1)
        return page

    def resource(self, public_id, **options):
        self.resource_calls.append({"public_id": public_id, **options})
        if self._resource_error is not None:
            raise self._resource_error
        return dict(self._resource_result)

    def url(self, public_id, **options):
        self.url_calls.append({"public_id": public_id, **options})
        return (f"https://res.cloudinary.com/x/{public_id}?sig=s1gned", options)


def _store(sdk: RecordingSdk, *, now_fn=None, **overrides) -> TransitStore:
    kwargs = dict(
        cloud_name="test-cloud",
        api_key="test-key",
        api_secret="test-secret",
        now_fn=now_fn or (lambda: NOW),
        upload_fn=sdk.upload,
        destroy_fn=sdk.destroy,
        resources_fn=sdk.resources,
        resource_fn=sdk.resource,
        url_fn=sdk.url,
    )
    kwargs.update(overrides)
    return TransitStore(**kwargs)


async def _stale(sdk: RecordingSdk) -> list:
    return await _store(sdk).list_stale(older_than_seconds=24 * 3600)


class TestConstruction:
    def test_construction_requires_the_full_credential_set(self):
        """A store missing any credential must fail at CONSTRUCTION, naming the
        missing key — not mid-pipeline on the first upload (the UoW
        unconstructible-without-a-tenant precedent, applied to credentials).
        Mutation that reddens: drop the constructor guard."""
        for missing in ("cloud_name", "api_key", "api_secret"):
            kwargs = {
                "cloud_name": "c",
                "api_key": "k",
                "api_secret": "s",
                missing: "",
            }
            with pytest.raises(ValueError, match=missing):
                TransitStore(**kwargs)

    def test_the_default_callables_are_the_real_sdk_doors(self):
        """The injected-fn defaults must bind to the actual cloudinary SDK
        callables — the thin default wiring is production code too, and an
        identity assert is the one test that reddens if someone rebinds a
        default to a stub or the wrong door.
        Mutation that reddens: swap any default binding."""
        import cloudinary.api
        import cloudinary.uploader
        import cloudinary.utils

        store = TransitStore(cloud_name="c", api_key="k", api_secret="s")
        assert store._upload_fn is cloudinary.uploader.upload
        assert store._destroy_fn is cloudinary.uploader.destroy
        assert store._resources_fn is cloudinary.api.resources
        assert store._resource_fn is cloudinary.api.resource
        assert store._url_fn is cloudinary.utils.cloudinary_url

    def test_the_lookup_door_binds_its_default_when_every_other_door_is_injected(
        self,
    ):
        """Each default binds on its own: a store whose other four SDK
        callables are injected (as the past-posts upload's tests build one) still
        holds the real `cloudinary.api.resource`, not None.
        Mutation that reddens: leave `resource_fn` out of the condition that
        binds the defaults."""
        import cloudinary.api

        sdk = RecordingSdk()
        store = TransitStore(
            cloud_name="c",
            api_key="k",
            api_secret="s",
            upload_fn=sdk.upload,
            destroy_fn=sdk.destroy,
            resources_fn=sdk.resources,
            url_fn=sdk.url,
        )
        assert store._resource_fn is cloudinary.api.resource


class TestUpload:
    @pytest.mark.asyncio
    async def test_upload_scopes_the_asset_under_the_workspace_folder(self):
        """Every transit asset uploads under ws/{workspace_id}/…
        Mutation that reddens: drop or flatten the folder option."""
        sdk = RecordingSdk()
        await _store(sdk).upload(b"bytes", workspace_id=WS, media_kind="image")
        # The public id is minted by the store under the workspace folder
        # (the eager story frame needs the id before the upload).
        public_id = sdk.upload_calls[0]["public_id"]
        assert public_id.startswith(f"ws/{WS}/") and "folder" not in sdk.upload_calls[0]
        assert len(public_id.rsplit("/", 1)[1]) == 20

    @pytest.mark.asyncio
    async def test_upload_is_authenticated_type_never_public(self):
        """Assets upload as type=authenticated — never public-by-default.
        Mutation that reddens: drop the type option (SDK default is public
        'upload')."""
        sdk = RecordingSdk()
        await _store(sdk).upload(b"bytes", workspace_id=WS, media_kind="image")
        assert sdk.upload_calls[0]["type"] == "authenticated"

    @pytest.mark.asyncio
    async def test_upload_uses_no_preset_and_carries_per_call_credentials(self):
        """Zero preset objects — the request itself is signed per-call with
        explicit credentials, so no global cloudinary.config() mutation and no
        upload_preset anywhere.
        Mutations that redden: add an upload_preset; drop any per-call
        credential (falling back to process-global config)."""
        sdk = RecordingSdk()
        await _store(sdk).upload(b"bytes", workspace_id=WS, media_kind="image")
        call = sdk.upload_calls[0]
        assert "upload_preset" not in call
        assert call["cloud_name"] == "test-cloud"
        assert call["api_key"] == "test-key"
        assert call["api_secret"] == "test-secret"

    @pytest.mark.asyncio
    async def test_upload_returns_the_provider_public_id_as_the_ref(self):
        """The returned ref IS post_intents.transit_asset_ref — the provider's
        public_id verbatim, folder prefix included (the destroy uses it).
        Mutation that reddens: return anything but result['public_id']."""
        sdk = RecordingSdk()
        ref = await _store(sdk).upload(b"bytes", workspace_id=WS, media_kind="image")
        assert ref == sdk.upload_calls[0]["public_id"]
        assert ref.startswith(f"ws/{WS}/")

    @pytest.mark.asyncio
    async def test_upload_never_overwrites_and_bounds_its_time(self):
        """A transit upload must never clobber an existing asset
        (overwrite=False) and must carry the bounded timeout — the SDK default
        is unbounded-ish and the pipeline's lease is 120 s.
        Mutations that redden: drop either option."""
        sdk = RecordingSdk()
        await _store(sdk).upload(b"bytes", workspace_id=WS, media_kind="image")
        call = sdk.upload_calls[0]
        assert call["overwrite"] is False
        assert call["timeout"] == 60.0

    @pytest.mark.asyncio
    async def test_video_uploads_ride_the_video_resource_type(self):
        """media_kind maps to the provider resource_type — a video uploaded as
        image is undeliverable to Meta.
        Mutation that reddens: hardcode resource_type."""
        sdk = RecordingSdk()
        await _store(sdk).upload(b"bytes", workspace_id=WS, media_kind="video")
        assert sdk.upload_calls[0]["resource_type"] == "video"

    @pytest.mark.asyncio
    async def test_upload_rejects_an_unknown_media_kind(self):
        """media_kind is the ck_media_kind closed set ('image','video') — an
        unknown kind is refused before any provider call.
        Mutation that reddens: drop the closed-set guard."""
        sdk = RecordingSdk()
        with pytest.raises(ValueError, match="media_kind"):
            await _store(sdk).upload(b"b", workspace_id=WS, media_kind="gif")
        assert sdk.upload_calls == []

    @pytest.mark.asyncio
    async def test_the_folder_uses_the_canonical_uuid_form(self):
        """Folder identity must be UNIQUE per workspace: uuid.UUID accepts
        uppercase, braced and urn: forms, and composing the folder from the
        RAW string would split one workspace across sibling folders. The
        folder carries the parsed, canonical (lowercase-dashed) form.
        Mutation that reddens: interpolate the raw workspace_id."""
        sdk = RecordingSdk()
        await _store(sdk).upload(b"b", workspace_id=WS.upper(), media_kind="image")
        assert sdk.upload_calls[0]["public_id"].startswith(f"ws/{WS}/")

    @pytest.mark.asyncio
    async def test_a_workspace_id_that_is_not_a_uuid_never_reaches_the_provider(self):
        """Workspace isolation is only as strong as the folder string: a
        workspace_id containing path syntax would escape ws/{id}/. The store
        accepts only a UUID-shaped id (which is what workspaces.id is) and
        refuses everything else BEFORE any provider call — with the valid-id
        upload above as the positive control that the guard is not refusing
        everything.
        Mutation that reddens: drop the UUID validation."""
        sdk = RecordingSdk()
        for hostile in ("../other-tenant", "ws2/nested", "", "not-a-uuid"):
            with pytest.raises(ValueError, match="workspace_id"):
                await _store(sdk).upload(b"b", workspace_id=hostile, media_kind="image")
        assert sdk.upload_calls == []


class TestDeliveryUrl:
    @pytest.mark.parametrize("kind,rtype", [("image", "image"), ("video", "video")])
    def test_delivery_url_is_signed_authenticated_and_matches_the_kind(
        self, kind, rtype
    ):
        """Delivery is SIGNED, against the authenticated
        type, on the right resource_type, over https.
        Mutations that redden: drop sign_url; drop type; hardcode
        resource_type; drop secure."""
        sdk = RecordingSdk()
        url = _store(sdk).delivery_url(f"ws/{WS}/asset1", media_kind=kind)
        call = sdk.url_calls[0]
        assert call["sign_url"] is True
        assert call["type"] == "authenticated"
        assert call["resource_type"] == rtype
        assert call["secure"] is True
        assert url.startswith("https://")

    def test_delivery_url_is_non_expiring_the_ttl_is_the_assets(self):
        """The time-limit property attaches to the asset, not the URL: no
        auth_token, no expires_at — asset destruction is the
        expiry ceiling.
        Mutation that reddens: pass an auth_token/expires_at."""
        sdk = RecordingSdk()
        _store(sdk).delivery_url(f"ws/{WS}/asset1", media_kind="image")
        call = sdk.url_calls[0]
        assert "auth_token" not in call
        assert "expires_at" not in call

    def test_delivery_url_carries_per_call_credentials(self):
        """Same no-global-config rule as upload: the signature must be
        computed from THIS store's secret, not whatever process-global config
        happens to hold.
        Mutation that reddens: drop the per-call credentials."""
        sdk = RecordingSdk()
        _store(sdk).delivery_url(f"ws/{WS}/asset1", media_kind="image")
        call = sdk.url_calls[0]
        assert call["cloud_name"] == "test-cloud"
        assert call["api_secret"] == "test-secret"


class TestDestroy:
    @pytest.mark.asyncio
    async def test_destroy_names_the_authenticated_type_and_invalidates(self):
        """The mechanism: a destroy that omits type=authenticated targets
        the PUBLIC namespace and silently misses the asset; one that omits
        invalidate leaves CDN-cached copies alive, breaking 'destruction
        404s every URL'.
        Mutations that redden: drop type; drop invalidate."""
        sdk = RecordingSdk()
        gone = await _store(sdk).destroy(f"ws/{WS}/asset1", media_kind="image")
        call = sdk.destroy_calls[0]
        assert gone is True
        assert call["public_id"] == f"ws/{WS}/asset1"
        assert call["type"] == "authenticated"
        assert call["invalidate"] is True
        assert call["resource_type"] == "image"
        assert call["timeout"] == 60.0, (
            "a hung destroy stalls the pipeline terminal step"
        )

    @pytest.mark.asyncio
    async def test_destroy_treats_not_found_as_already_gone(self):
        """Destroy is idempotent toward its goal state: 'not found' means the
        asset is gone (a prior destroy or the sweep won), which is
        success for the destroy. Anything else is False — the sweep retries.
        Mutation that reddens: treat every result as gone (or none)."""
        assert (
            await _store(RecordingSdk(destroy_result={"result": "not found"})).destroy(
                "ws/x/y", media_kind="image"
            )
            is True
        )
        assert (
            await _store(RecordingSdk(destroy_result={"result": "error"})).destroy(
                "ws/x/y", media_kind="image"
            )
            is False
        )


# -- the past-posts reference's typed cleanup doors (#1644) -------------------------


@pytest.fixture
def no_network(monkeypatch):
    """The cleanup doors run against fakes only. A name lookup or a connect
    from one of these tests is a bug in the test, so it fails loudly instead
    of reaching a provider."""

    def refuse(*args, **kwargs):
        raise AssertionError("the transit tests must not reach the network")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


def test_the_no_network_guard_refuses_a_name_lookup_and_a_connect(no_network):
    """The guard the classes below stand on bites: a test that cannot fail
    proves nothing.
    Mutation that reddens: make the guard's patches no-ops."""
    with pytest.raises(AssertionError, match="must not reach the network"):
        socket.getaddrinfo("api.cloudinary.com", 443)
    with socket.socket() as probe:
        with pytest.raises(AssertionError, match="must not reach the network"):
            probe.connect(("127.0.0.1", 9))


_MISSING = object()


def _admin_resource(kind: str = "image", **changes) -> dict:
    """The Admin API's answer for one authenticated asset's details (the
    fields it documents), with *changes* applied; a value of `_MISSING` drops
    the key."""
    body = {
        "asset_id": "b" * 32,
        "public_id": REF,
        "format": "jpg" if kind == "image" else "mp4",
        "version": VERSION,
        "resource_type": kind,
        "type": "authenticated",
        "created_at": "2026-08-19T21:00:00Z",
        "bytes": 109669,
        "access_mode": "authenticated",
        "derived": [],
    }
    body.update(changes)
    return {key: value for key, value in body.items() if value is not _MISSING}


def _v1_signature(public_id: str, version: int, secret: str) -> str:
    """The provider's signature over a created asset's identity — sha1 of
    `public_id=…&version=…` followed by the api secret — spelled here without
    the SDK, so the store's own computation is checked against the scheme and
    not against itself."""
    return hashlib.sha1(
        f"public_id={public_id}&version={version}{secret}".encode()
    ).hexdigest()


#: References no past-posts upload can have created: path syntax, another shape, a
#: spelling the upload would not send, not a string at all.
_HOSTILE_REFS = [
    f"ws/{WS}/../ws/{WS}/" + "a" * 32,
    "../other/" + "a" * 32,
    f"ws/{WS}/" + "a" * 32 + "/nested",
    f"ws/{WS}/" + "a" * 32 + "?x=1",
    f"ws/{WS}/" + "a" * 32 + "\n",
    f"ws/{WS}/" + "a" * 19,
    f"ws/{WS}/" + "a" * 65,
    f"ws/{WS}/" + "A" * 32,
    f"ws/{WS.upper()}/" + "a" * 32,
    f"upload/{WS}/" + "a" * 32,
    "ws/not-a-uuid/" + "a" * 32,
    "https://evil.example/" + "a" * 32,
    "",
    None,
    7,
    b"ws/bytes",
]
_HOSTILE_TYPES = ["raw", "auto", "IMAGE", "image/../video", "", None, 7]

#: What the SDK raises instead of answering: every class of its own family, and
#: the failures it does not type (`Exception("Must supply api_key")`, a wire's
#: `OSError`).
_SDK_ERRORS = [
    cls("provider words")
    for cls in vars(cloudinary.exceptions).values()
    if isinstance(cls, type) and issubclass(cls, Exception)
] + [TimeoutError("hung"), OSError("reset"), Exception("Must supply api_key")]


def _error_id(error: Exception) -> str:
    return type(error).__name__


def _is_the_same_error(raised: Exception, scripted: Exception) -> bool:
    """Whether *raised* is *scripted*, propagated and not wrapped. Compared by
    type and arguments, not identity: from Python 3.11 a thread's builtin
    `TimeoutError` comes back through asyncio as a copy of itself."""
    return type(raised) is type(scripted) and raised.args == scripted.args


@pytest.mark.usefixtures("no_network")
class TestDestroyPastPosts:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["image", "video"])
    async def test_ok_is_destroyed_through_the_one_authenticated_invalidating_call(
        self, kind
    ):
        """`ok` is 'destroyed'. The call is the module's one destroy spelling —
        the authenticated type (without it the destroy targets the public
        namespace and answers `not found` for an asset that exists),
        `invalidate`, the bound and this store's own credentials — and
        nothing else: no upload, no listing, no lookup.
        Mutations that redden: drop type, invalidate, the bound or the
        credentials from the call; map `ok` to 'not_found'; read the asset's
        details as well."""
        sdk = RecordingSdk()
        answer = await _store(sdk).destroy_past_posts(REF, resource_type=kind)
        assert answer == "destroyed"
        assert sdk.destroy_calls == [
            {
                "public_id": REF,
                "type": "authenticated",
                "resource_type": kind,
                "invalidate": True,
                "timeout": 60.0,
                "cloud_name": "test-cloud",
                "api_key": "test-key",
                "api_secret": "test-secret",
            }
        ]
        assert (sdk.upload_calls, sdk.resources_calls, sdk.resource_calls) == (
            [],
            [],
            [],
        )

    @pytest.mark.asyncio
    async def test_not_found_is_kept_apart_from_destroyed(self):
        """The reason this door exists: `destroy` counts `not found` as gone,
        so it cannot tell destroyed from never created. Here `not found` is its
        own answer, from one call.
        Mutations that redden: map `not found` to 'destroyed'; call again on
        `not found`."""
        sdk = RecordingSdk(destroy_result={"result": "not found"})
        answer = await _store(sdk).destroy_past_posts(REF, resource_type="image")
        assert answer == "not_found"
        assert len(sdk.destroy_calls) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "answer",
        [
            {"result": "error"},
            {"result": "OK"},
            {"result": "Not Found"},
            {"result": "deleted"},
            {"result": ""},
            {"result": None},
            {"result": ["ok"]},
            {},
            None,
            "ok",
            ["not found"],
        ],
        ids=repr,
    )
    async def test_any_other_answer_is_a_named_refusal(self, answer):
        """Anything but exactly `ok` or `not found` is TransitDestroyRefused —
        never 'destroyed', never 'not_found', never an unnamed crash — and that
        includes an answer that is not a mapping at all. The message names the
        reference.
        Mutations that redden: accept `OK` case-insensitively; return
        'not_found' for any non-ok answer; read the answer without the
        mapping guard; raise a different type."""
        store = _store(RecordingSdk(), destroy_fn=lambda *args, **kwargs: answer)
        with pytest.raises(TransitDestroyRefused) as caught:
            await store.destroy_past_posts(REF, resource_type="image")
        assert REF in str(caught.value)

    @pytest.mark.asyncio
    async def test_a_refusal_summarises_the_provider_and_never_quotes_it(self):
        """A provider's words are summarised, never quoted: the message is
        read in logs and on the ledger.
        Mutation that reddens: interpolate the answer into the message."""
        store = _store(
            RecordingSdk(), destroy_fn=lambda *a, **k: {"result": "private words"}
        )
        with pytest.raises(TransitDestroyRefused) as caught:
            await store.destroy_past_posts(REF, resource_type="image")
        assert "private words" not in str(caught.value)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("error", _SDK_ERRORS, ids=_error_id)
    async def test_an_sdk_error_is_not_an_answer(self, error):
        """A timeout, an auth failure, a rate limit or a 5xx raised by the SDK
        propagates as itself: it is neither 'destroyed' nor 'not_found' nor a
        refusal, so no caller can record a destroy it never saw. Not even the
        SDK's own NotFound counts: the polite `not found` is a 200 answer, the
        only way to 'not_found'.
        Mutations that redden: catch the error and answer 'not_found'; map the
        SDK's NotFound to 'not_found'; wrap the error in TransitDestroyRefused."""

        def boom(*args, **kwargs):
            raise error

        store = _store(RecordingSdk(), destroy_fn=boom)
        with pytest.raises(type(error)) as caught:
            await store.destroy_past_posts(REF, resource_type="image")
        assert _is_the_same_error(caught.value, error)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("public_id", _HOSTILE_REFS, ids=repr)
    async def test_a_reference_no_past_posts_upload_made_is_refused_before_any_call(
        self, public_id
    ):
        """A reference outside `ws/{uuid}/{hex}` never reaches the provider.
        Mutation that reddens: drop the reference guard."""
        sdk = RecordingSdk()
        with pytest.raises(ValueError, match="past-posts reference"):
            await _store(sdk).destroy_past_posts(public_id, resource_type="image")
        assert sdk.destroy_calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("resource_type", _HOSTILE_TYPES, ids=repr)
    async def test_a_resource_type_outside_image_and_video_is_refused_before_any_call(
        self, resource_type
    ):
        """A destroy aimed at the wrong namespace answers `not found` for an
        asset that exists, which a caller would read as absence.
        Mutation that reddens: drop the resource_type guard."""
        sdk = RecordingSdk()
        with pytest.raises(ValueError, match="resource_type"):
            await _store(sdk).destroy_past_posts(REF, resource_type=resource_type)
        assert sdk.destroy_calls == []


@pytest.mark.usefixtures("no_network")
class TestPastPostsLookup:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("secret", ["test-secret", "another-secret"])
    @pytest.mark.parametrize("kind", ["image", "video"])
    async def test_found_is_the_creation_evidence_signed_with_this_stores_secret(
        self, kind, secret
    ):
        """A found asset answers exactly the evidence the upload returns: the
        asked reference and kind, the provider's asset id and version, the
        `authenticated` delivery type and the signature the provider's v1
        scheme gives, from THIS store's secret. An exact type: the ledger's
        completion refuses a subclass.
        Mutations that redden: sign with sha256, with the api key or over
        another version; build the version or the asset id from another
        field; hard-code the resource_type; change the delivery type."""
        sdk = RecordingSdk(resource_result=_admin_resource(kind))
        evidence = await _store(sdk, api_secret=secret).past_posts_lookup(
            REF, resource_type=kind
        )
        assert type(evidence) is PastPostsUploadEvidence
        assert evidence == PastPostsUploadEvidence(
            public_id=REF,
            asset_id="b" * 32,
            version=VERSION,
            resource_type=kind,
            delivery_type="authenticated",
            signature=_v1_signature(REF, VERSION, secret),
        )

    @pytest.mark.asyncio
    async def test_the_read_is_authenticated_bounded_and_a_read_only(self):
        """The read is authenticated too: without that type the lookup would
        target the public namespace and answer `not found` for every asset. The
        bound and this store's credentials ride per call, and the lookup
        touches no other door — it never uploads, destroys or lists.
        Mutations that redden: drop type, the bound or the credentials; read
        through the listing door; destroy what it found."""
        sdk = RecordingSdk(resource_result=_admin_resource("video"))
        await _store(sdk).past_posts_lookup(REF, resource_type="video")
        assert sdk.resource_calls == [
            {
                "public_id": REF,
                "type": "authenticated",
                "resource_type": "video",
                "timeout": 60.0,
                "cloud_name": "test-cloud",
                "api_key": "test-key",
                "api_secret": "test-secret",
            }
        ]
        assert (sdk.upload_calls, sdk.destroy_calls, sdk.resources_calls) == (
            [],
            [],
            [],
        )

    @pytest.mark.asyncio
    async def test_only_the_providers_not_found_is_none(self):
        """None is the provider's own `not found` — and the positive control,
        the same store reading a found asset, is not None.
        Mutations that redden: let the SDK's NotFound propagate; answer None
        for a found asset."""
        gone = RecordingSdk(
            resource_error=cloudinary.exceptions.NotFound(
                f"Error 404 - Resource not found - {REF}"
            )
        )
        assert await _store(gone).past_posts_lookup(REF, resource_type="image") is None
        assert len(gone.resource_calls) == 1
        here = RecordingSdk(resource_result=_admin_resource("image"))
        assert (
            await _store(here).past_posts_lookup(REF, resource_type="image") is not None
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            error
            for error in _SDK_ERRORS
            if not isinstance(error, cloudinary.exceptions.NotFound)
        ],
        ids=_error_id,
    )
    async def test_no_other_failure_is_absence(self, error):
        """Everything but `not found` raises as itself — an auth failure, a
        rate limit, a 5xx, a timeout, an untyped crash — because a cleanup
        reads None as 'gone' and none of these is that.
        Mutations that redden: catch Exception and answer None; wrap the error
        in another type."""

        def boom(*args, **kwargs):
            raise error

        store = _store(RecordingSdk(), resource_fn=boom)
        with pytest.raises(type(error)) as caught:
            await store.past_posts_lookup(REF, resource_type="image")
        assert _is_the_same_error(caught.value, error)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "change",
        [
            {"public_id": REF + "0"},
            {"public_id": None},
            {"public_id": _MISSING},
            {"resource_type": "video"},
            {"resource_type": "raw"},
            {"resource_type": _MISSING},
            {"type": "upload"},
            {"type": "private"},
            {"type": _MISSING},
            {"asset_id": None},
            {"asset_id": ""},
            {"asset_id": "has space"},
            {"asset_id": "x" * 129},
            {"asset_id": 7},
            {"asset_id": _MISSING},
            {"version": True},
            {"version": 0},
            {"version": -1},
            {"version": 2**63},
            {"version": str(VERSION)},
            {"version": 1.5},
            {"version": None},
            {"version": _MISSING},
        ],
        ids=lambda change: ",".join(
            f"{key}={'MISSING' if value is _MISSING else repr(value)}"
            for key, value in change.items()
        ),
    )
    async def test_an_answer_that_does_not_identify_the_asset_is_uncertain(
        self, change
    ):
        """A 200 that is not the asked asset, not authenticated, or carries no
        usable asset id or version is neither absence nor evidence: it raises
        PastPostsUploadUncertain, never None and never evidence built from it.
        Mutations that redden: drop any one of the identity checks (public id,
        resource_type, delivery type, asset id shape, version type, version
        range)."""
        sdk = RecordingSdk(resource_result=_admin_resource("image", **change))
        with pytest.raises(PastPostsUploadUncertain):
            await _store(sdk).past_posts_lookup(REF, resource_type="image")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("answer", [None, [], "found", 7], ids=repr)
    async def test_an_answer_that_is_not_a_mapping_is_uncertain(self, answer):
        """Mutation that reddens: read the answer without the mapping guard."""
        store = _store(RecordingSdk(), resource_fn=lambda *args, **kwargs: answer)
        with pytest.raises(PastPostsUploadUncertain):
            await store.past_posts_lookup(REF, resource_type="image")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "changes",
        [
            {"version": 1},
            {"version": 2**63 - 1},
            {"asset_id": "a" * 128},
            {"asset_id": "A_b-9"},
        ],
        ids=repr,
    )
    async def test_the_values_the_database_admits_are_evidence(self, changes):
        """The positive control at the edges: a check stricter than the table
        would leave a real asset 'uncertain' forever.
        Mutation that reddens: tighten the version range or the asset id
        length."""
        sdk = RecordingSdk(resource_result=_admin_resource("image", **changes))
        evidence = await _store(sdk).past_posts_lookup(REF, resource_type="image")
        assert evidence is not None
        assert (evidence.version, evidence.asset_id) == (
            changes.get("version", VERSION),
            changes.get("asset_id", "b" * 32),
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("public_id", _HOSTILE_REFS, ids=repr)
    async def test_a_reference_no_past_posts_upload_made_is_refused_before_any_call(
        self, public_id
    ):
        """The Admin API puts the public id in the request path, so a
        reference carrying path syntax would name another endpoint.
        Mutation that reddens: drop the reference guard."""
        sdk = RecordingSdk(resource_result=_admin_resource("image"))
        with pytest.raises(ValueError, match="past-posts reference"):
            await _store(sdk).past_posts_lookup(public_id, resource_type="image")
        assert sdk.resource_calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("resource_type", _HOSTILE_TYPES, ids=repr)
    async def test_a_resource_type_outside_image_and_video_is_refused_before_any_call(
        self, resource_type
    ):
        """Mutation that reddens: drop the resource_type guard."""
        sdk = RecordingSdk(resource_result=_admin_resource("image"))
        with pytest.raises(ValueError, match="resource_type"):
            await _store(sdk).past_posts_lookup(REF, resource_type=resource_type)
        assert sdk.resource_calls == []


class _StubWire:
    """Stands where urllib3's pool stands INSIDE the SDK (`_http`): answers
    from a script and records the request, so the real SDK door parses a
    scripted wire. Nothing leaves the process."""

    def __init__(self):
        self.requests: list[dict] = []
        self._answers: list[tuple[int, bytes]] = []

    def answer(self, status: int, body) -> None:
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self._answers.append((status, raw))

    def request(self, method, url, fields=None, headers=None, **kwargs):
        self.requests.append(
            {"method": method, "url": url, "fields": fields, "headers": headers}
            | kwargs
        )
        status, raw = self._answers.pop(0)
        return SimpleNamespace(status=status, data=raw, headers={})


@pytest.fixture
def wire(monkeypatch):
    """The real Admin API client with a scripted wire. The process-global
    settings that would redirect or re-authenticate a call (`oauth_token`,
    `upload_prefix`) are cleared, so the test world is the per-call
    credentials alone."""
    import cloudinary
    import cloudinary.api_client.call_api as admin_client

    config = cloudinary.config()
    monkeypatch.setattr(config, "oauth_token", None, raising=False)
    monkeypatch.setattr(config, "upload_prefix", None, raising=False)
    stub = _StubWire()
    monkeypatch.setattr(admin_client, "_http", stub)
    return stub


@pytest.mark.usefixtures("no_network")
class TestPastPostsLookupOnTheRealSdk:
    """The DEFAULT `resource_fn` is the real `cloudinary.api.resource`; only its
    wire is scripted. What the SDK raises for a 404, what it sends and how it
    parses a found asset are the SDK's contract, pinned here against the
    installed version — a fake that raises `NotFound` cannot show that the real
    door does."""

    @staticmethod
    def _store() -> TransitStore:
        return TransitStore(
            cloud_name="test-cloud", api_key="test-key", api_secret="test-secret"
        )

    @pytest.mark.asyncio
    async def test_a_404_is_none_and_the_request_is_the_authenticated_details_read(
        self, wire
    ):
        """A real 404 reaches the store as the SDK's NotFound and answers None;
        the request is a GET of THIS asset's details under the authenticated
        type, bounded, authenticated with the per-call key and secret.
        Mutations that redden: drop type; drop the bound; stop mapping NotFound
        to None."""
        wire.answer(404, {"error": {"message": f"Resource not found - {REF}"}})
        assert await self._store().past_posts_lookup(REF, resource_type="image") is None
        [sent] = wire.requests
        assert sent["method"] == "GET"
        assert sent["url"] == (
            f"https://api.cloudinary.com/v1_1/test-cloud/resources/image/authenticated/{REF}"
        )
        assert sent["timeout"] == 60.0
        basic = sent["headers"]["authorization"].split()
        assert basic[0] == "Basic"
        assert base64.b64decode(basic[1]) == b"test-key:test-secret"

    @pytest.mark.asyncio
    async def test_a_found_asset_parses_into_the_evidence(self, wire):
        """The positive control on the real door: a 200 with the documented
        fields is evidence for the asked kind.
        Mutation that reddens: read the evidence from another answer field."""
        wire.answer(200, _admin_resource("video"))
        evidence = await self._store().past_posts_lookup(REF, resource_type="video")
        assert evidence == PastPostsUploadEvidence(
            public_id=REF,
            asset_id="b" * 32,
            version=VERSION,
            resource_type="video",
            delivery_type="authenticated",
            signature=_v1_signature(REF, VERSION, "test-secret"),
        )
        assert wire.requests[0]["url"].endswith(f"/resources/video/authenticated/{REF}")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [400, 401, 403, 409, 420, 429, 500, 502])
    async def test_every_other_status_raises_and_is_never_none(self, wire, status):
        """An auth failure, a rate limit, a 5xx or an unmapped status is the
        SDK's error and not absence.
        Mutation that reddens: catch Exception and answer None."""
        wire.answer(status, {"error": {"message": "provider words"}})
        with pytest.raises(Exception) as caught:
            await self._store().past_posts_lookup(REF, resource_type="image")
        assert not isinstance(caught.value, cloudinary.exceptions.NotFound)

    @pytest.mark.asyncio
    async def test_an_unreadable_answer_raises_and_is_never_none(self, wire):
        """A 200 that is not JSON is the SDK's GeneralError.
        Mutation that reddens: catch Exception and answer None."""
        wire.answer(200, b"<html>maintenance</html>")
        with pytest.raises(cloudinary.exceptions.GeneralError):
            await self._store().past_posts_lookup(REF, resource_type="image")


def _aged(public_id: str, hours_old: float, now: datetime) -> dict:
    created = now - timedelta(hours=hours_old)
    return {
        "public_id": public_id,
        "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


#: An in-flight and a past-TTL upload instant, each with a non-zero fraction so
#: every fractional width below spells a real value (FRESH at width 5 is
#: `.05024`).
FRESH = NOW - timedelta(hours=1) + timedelta(microseconds=50240)
STALE = NOW - timedelta(hours=30) + timedelta(microseconds=123456)


def _spelled(t: datetime, *, digits: int = 0, sign: str = ".", offset: str = "Z"):
    """*t* (UTC) in ISO-8601 with *digits* fractional digits (up to 9) after
    *sign*, closed by *offset*."""
    fraction = f"{sign}{t.microsecond:06d}000"[: digits + 1] if digits else ""
    return t.strftime("%Y-%m-%dT%H:%M:%S") + fraction + offset


#: ISO-8601 spellings a provider can render ``created_at`` in. Python 3.10's
#: ``fromisoformat`` accepts only fraction widths 0, 3 and 6 and the ``Z`` /
#: ``±HH:MM`` offsets; every other entry is a format shift the sweep must read
#: on every supported interpreter. A spelling with no offset parses NAIVE on
#: every interpreter, and the sweep must still read it — as UTC — rather than
#: compare it raw against an aware cutoff.
_SPELLINGS = {
    "seconds": {},
    "width-1": {"digits": 1},
    "width-2": {"digits": 2},
    "width-3": {"digits": 3},
    "width-4": {"digits": 4},
    "width-5": {"digits": 5},
    "width-6": {"digits": 6},
    "width-7": {"digits": 7},
    "width-9": {"digits": 9},
    "decimal-comma": {"digits": 3, "sign": ","},
    "offset-colon": {"offset": "+00:00"},
    "offset-basic": {"offset": "+0000"},
    "offset-hours": {"offset": "+00"},
    "offset-lowercase-z": {"offset": "z"},
    "offset-dropped": {"offset": ""},
}

#: ``created_at`` values no age can be read from: absent, empty, not a string,
#: or not ISO-8601 at all. The field is provider-rendered, so each one stands
#: for a whole corpus in that shape.
_UNREADABLE = {
    "absent": {},
    "null": {"created_at": None},
    "empty": {"created_at": ""},
    "prose": {"created_at": "yesterday-ish"},
    "rfc-2822": {"created_at": "Wed, 19 Aug 2026 21:00:00 GMT"},
    "epoch-text": {"created_at": "1787000000"},
    "epoch-number": {"created_at": 1787000000},
}


class TestListStale:
    def _sdk(self):
        return RecordingSdk(
            resources_pages={
                "image": [
                    {
                        "resources": [
                            _aged("ws/a/old-img", 30, NOW),
                            _aged("ws/a/fresh-img", 1, NOW),
                        ]
                    },
                    {"resources": [_aged("ws/b/old-img2", 25, NOW)]},
                ],
                "video": [
                    {
                        "resources": [
                            _aged("ws/c/old-vid", 26, NOW),
                            _aged("ws/c/fresh-vid", 2, NOW),
                        ]
                    },
                ],
            }
        )

    @pytest.mark.asyncio
    async def test_list_stale_returns_only_assets_past_the_ttl(self):
        """The sweep destroys assets OLDER than the TTL regardless of
        pipeline state — and must not touch in-flight (fresh) transit assets.
        Mutation that reddens: invert or drop the age comparison."""
        ids = {a["public_id"] for a in await _stale(self._sdk())}
        assert ids == {"ws/a/old-img", "ws/b/old-img2", "ws/c/old-vid"}

    @pytest.mark.asyncio
    async def test_list_stale_walks_every_page_and_both_resource_types(self):
        """One GLOBAL age-filtered listing per sweep still means
        walking the full pagination and both provider resource_types — a sweep
        that reads page one only, or images only, silently strands the rest
        past the hard TTL forever.
        Mutations that redden: stop at the first page; drop the video walk."""
        sdk = self._sdk()
        await _stale(sdk)
        rtypes = [c.get("resource_type") for c in sdk.resources_calls]
        assert rtypes.count("image") == 2, "image pagination must continue past page 1"
        assert rtypes.count("video") == 1
        cursors = [
            c.get("next_cursor")
            for c in sdk.resources_calls
            if c.get("resource_type") == "image"
        ]
        assert cursors == [None, "1"], (
            "page 2 must be requested with exactly the cursor page 1 returned"
        )

    @pytest.mark.asyncio
    async def test_list_stale_scopes_to_the_transit_namespace(self):
        """The walk targets exactly the transit corpus: prefix ws/ under
        type=authenticated, with per-call credentials. Anything else lists a
        namespace the sweep must not touch (or nothing at all).
        Mutations that redden: drop the prefix; drop the type."""
        sdk = self._sdk()
        await _stale(sdk)
        for call in sdk.resources_calls:
            assert call["prefix"] == "ws/"
            assert call["type"] == "authenticated"
            assert call["cloud_name"] == "test-cloud"
            assert call["timeout"] == 60.0, "a hung listing stalls the sweep tick"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "spelling", list(_SPELLINGS.values()), ids=list(_SPELLINGS)
    )
    async def test_a_provider_format_shift_does_not_reap_the_corpus(self, spelling):
        """The provider renders ``created_at`` with one serializer, so a
        format shift reaches EVERY row at once. Each case is a whole corpus
        re-spelled: the in-flight asset must survive AND the past-TTL one must
        still go — a sweep that stops reaping is safe only by having stopped.
        Mutations that redden: parse with the interpreter's bare
        ``fromisoformat`` — on 3.10, CI's interpreter, every width but 0/3/6
        and every offset but ``Z``/``±HH:MM`` then reads as no age at all (on
        3.11+, ``z`` alone does); compare the parse raw, without
        ``ensure_utc`` — the offset-less spelling then crashes the sweep."""
        sdk = RecordingSdk(
            resources_pages={
                "image": [
                    {
                        "resources": [
                            {
                                "public_id": "ws/a/in-flight",
                                "created_at": _spelled(FRESH, **spelling),
                            },
                            {
                                "public_id": "ws/a/past-ttl",
                                "created_at": _spelled(STALE, **spelling),
                            },
                        ]
                    }
                ],
                "video": [{"resources": []}],
            }
        )
        assert [a["public_id"] for a in await _stale(sdk)] == ["ws/a/past-ttl"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "unreadable", list(_UNREADABLE.values()), ids=list(_UNREADABLE)
    )
    async def test_an_unreadable_age_is_kept_and_logged_never_reaped(
        self, unreadable, caplog
    ):
        """Reaping needs a READ age. An asset whose ``created_at`` cannot be
        read may be mid-publish, and the field is provider-rendered, so an
        unreadable age is the shape of a corpus-wide format shift far more
        often than of one garbled row — failing toward reaping would destroy
        every in-flight asset on the next tick. The asset is kept and named in
        ONE error per sweep until the parse reads it; a readable past-TTL
        asset in the same walk still goes.
        Mutations that redden: reap a row whose age cannot be read; drop the
        error; log it once per row instead of once per sweep; let a non-string
        age reach the parse (the sweep dies on it)."""
        sdk = RecordingSdk(
            resources_pages={
                "image": [
                    {
                        "resources": [
                            {"public_id": "ws/a/unreadable", **unreadable},
                            _aged("ws/a/past-ttl", 30, NOW),
                        ]
                    }
                ],
                "video": [{"resources": [{"public_id": "ws/b/also", **unreadable}]}],
            }
        )
        with caplog.at_level("ERROR", logger="src.services.target.transit"):
            reaped = await _stale(sdk)
        assert [a["public_id"] for a in reaped] == ["ws/a/past-ttl"]
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(errors) == 1, "one error per sweep, not one per row"
        message = errors[0].getMessage()
        assert "2 transit asset(s)" in message
        assert "ws/a/unreadable" in message and "ws/b/also" in message

    @pytest.mark.asyncio
    async def test_the_error_counts_every_unreadable_row_but_names_a_sample(
        self, caplog
    ):
        """A corpus-wide shift makes every row unreadable, so the error gives
        the WHOLE count but names a bounded sample — naming every row would
        print the corpus on every tick, and counting only the sample would
        understate the outage.
        Mutations that redden: name every row; count only the named rows."""
        rows = [{"public_id": f"ws/a/row-{i}"} for i in range(7)]
        sdk = RecordingSdk(
            resources_pages={
                "image": [{"resources": rows}],
                "video": [{"resources": []}],
            }
        )
        with caplog.at_level("ERROR", logger="src.services.target.transit"):
            assert await _stale(sdk) == []
        (error,) = [r for r in caplog.records if r.levelname == "ERROR"]
        message = error.getMessage()
        assert "7 transit asset(s)" in message
        named = [i for i in range(7) if f"ws/a/row-{i} " in message]
        assert named == [0, 1, 2, 3, 4]

    @pytest.mark.asyncio
    async def test_each_stale_asset_carries_what_the_deleter_needs(self):
        """The lister's rows feed destroy_asset: public_id + resource_type.
        Mutation that reddens: drop resource_type from the row."""
        vid = next(
            a for a in await _stale(self._sdk()) if a["public_id"] == "ws/c/old-vid"
        )
        assert vid["resource_type"] == "video"


class TestTheSweepComposes:
    @pytest.mark.asyncio
    async def test_the_store_drives_the_reap_executor_end_to_end(self):
        """The sweep assembled: scheduler.execute_reap_transit_assets driven by
        THIS store's lister and deleter
        destroys exactly the stale set, with the right per-asset parameters.
        Mutation that reddens: any break in the lister→deleter dict contract."""
        from src.services.target.scheduler import execute_reap_transit_assets

        sdk = RecordingSdk(
            resources_pages={
                "image": [
                    {
                        "resources": [
                            _aged("ws/a/old1", 30, NOW),
                            _aged("ws/a/new1", 1, NOW),
                        ]
                    }
                ],
                "video": [{"resources": [_aged("ws/b/old2", 26, NOW)]}],
            }
        )
        store = _store(sdk)
        reaped = await execute_reap_transit_assets(
            None,
            lister=store.list_stale,
            deleter=store.destroy_asset,
            older_than_seconds=24 * 3600,
        )
        assert reaped == 2
        destroyed = {
            (c["public_id"], c["resource_type"], c["type"]) for c in sdk.destroy_calls
        }
        assert destroyed == {
            ("ws/a/old1", "image", "authenticated"),
            ("ws/b/old2", "video", "authenticated"),
        }

    @pytest.mark.asyncio
    async def test_a_provider_format_shift_destroys_nothing_in_flight(self):
        """The invariant at the door that deletes: a sweep over an in-flight
        corpus the provider has re-spelled — fractions at widths 3.10 rejects,
        an age missing, an age in a non-ISO format — destroys NOTHING, on
        every page and both resource types.
        Mutation that reddens: reap a row whose age cannot be read."""
        from src.services.target.scheduler import execute_reap_transit_assets

        sdk = RecordingSdk(
            resources_pages={
                "image": [
                    {
                        "resources": [
                            {
                                "public_id": "ws/a/in-flight-1",
                                "created_at": _spelled(FRESH, digits=5),
                            },
                            {"public_id": "ws/a/in-flight-2"},
                        ]
                    },
                    {
                        "resources": [
                            {"public_id": "ws/b/in-flight-3", **_UNREADABLE["rfc-2822"]}
                        ]
                    },
                ],
                "video": [
                    {
                        "resources": [
                            {
                                "public_id": "ws/c/in-flight-4",
                                "created_at": _spelled(FRESH, digits=9),
                            }
                        ]
                    }
                ],
            }
        )
        store = _store(sdk)
        reaped = await execute_reap_transit_assets(
            None,
            lister=store.list_stale,
            deleter=store.destroy_asset,
            older_than_seconds=24 * 3600,
        )
        assert sdk.destroy_calls == []
        assert reaped == 0
        assert len(sdk.resources_calls) == 3, "every page of both types was walked"

    @pytest.mark.asyncio
    async def test_a_refused_destroy_leaves_the_rest_of_the_sweep_intact(self):
        """The executor's contract ('an asset the deleter refuses is left for
        the next sweep') composed with a REAL refusing deleter — the positive
        control that the happy-path count above is not vacuous.
        Mutation that reddens: destroy_asset swallowing its own errors."""
        from src.services.target.scheduler import execute_reap_transit_assets

        sdk = RecordingSdk(
            resources_pages={
                "image": [
                    {
                        "resources": [
                            _aged("ws/a/old1", 30, NOW),
                            _aged("ws/a/old2", 28, NOW),
                        ]
                    }
                ],
                "video": [{"resources": []}],
            }
        )
        boom = {"armed": True}

        def failing_once(public_id, **options):
            if boom.pop("armed", False):
                raise RuntimeError("provider refused")
            return sdk.destroy(public_id, **options)

        store = _store(sdk, destroy_fn=failing_once)
        reaped = await execute_reap_transit_assets(
            None,
            lister=store.list_stale,
            deleter=store.destroy_asset,
            older_than_seconds=24 * 3600,
        )
        assert reaped == 1

    @pytest.mark.asyncio
    async def test_a_polite_destroy_refusal_is_not_counted_reaped(self):
        """destroy_asset's contract: the executor counts only completed
        destroys. A provider that answers politely with a non-ok result
        ({'result': 'error'}) has NOT destroyed the asset — counting it
        reaped overstates the sweep and strands the asset uncounted.
        Mutation that reddens: destroy_asset returning False instead of
        raising on a polite refusal."""
        from src.services.target.scheduler import execute_reap_transit_assets

        sdk = RecordingSdk(
            resources_pages={
                "image": [
                    {
                        "resources": [
                            _aged("ws/a/old1", 30, NOW),
                            _aged("ws/a/old2", 28, NOW),
                        ]
                    }
                ],
                "video": [{"resources": []}],
            },
            destroy_result={"result": "error"},
        )
        store = _store(sdk)
        reaped = await execute_reap_transit_assets(
            None,
            lister=store.list_stale,
            deleter=store.destroy_asset,
            older_than_seconds=24 * 3600,
        )
        assert len(sdk.destroy_calls) == 2, "both destroys must have been attempted"
        assert reaped == 0


class TestClosedSetsTrackTheSchema:
    def test_the_media_kind_map_tracks_ck_media_kind(self):
        """_RESOURCE_TYPES' key set must equal the model's ck_media_kind
        closed set — a new media kind added to the schema without a provider
        resource_type DECISION here would otherwise surface as a mid-pipeline
        upload refusal instead of a test failure.
        Mutation that reddens: any drift between the two sets."""
        from src.models.target.accounts_sources_media import MediaItem
        from src.services.target.transit import _RESOURCE_TYPES

        ck = next(
            c
            for c in MediaItem.__table_args__
            if getattr(c, "name", None) == "ck_media_kind"
        )
        allowed = set(re.findall(r"'([a-z_]+)'", str(ck.sqltext)))
        assert allowed == set(_RESOURCE_TYPES)


class TestUploadFailuresAreTyped:
    """#1276: the SDK raises its own family; the pipeline routes on
    typed failures only, so an untyped one would crash the job into the
    loop's unbounded reschedule. One type, the cause chained."""

    async def test_an_sdk_failure_becomes_a_transit_error_with_the_cause(self):
        from src.services.target.transit import TransitError

        def exploding_upload(content, **kw):
            raise OSError("connection reset by peer")

        store = _store(RecordingSdk(), upload_fn=exploding_upload)
        with pytest.raises(TransitError, match="OSError: connection reset") as info:
            await store.upload(b"bytes", workspace_id=WS, media_kind="image")
        assert isinstance(info.value.__cause__, OSError)


class TestTheStoryFrame:
    """The delivery URL frames the asset for a story: 1080 × 1920 over a
    blurred copy of itself."""

    def test_an_image_is_padded_over_a_blurred_underlay_of_itself(self):
        from src.services.target.transit import story_transformation

        chain = story_transformation(f"ws/{WS}/asset1", media_kind="image")
        # The picture is fitted into the frame BEFORE the underlay is laid:
        # a layer's canvas is the base's size, and a phone photo wider than
        # 1080 would otherwise hide the underlay and leave white bars.
        assert chain[0] == {"crop": "limit", "width": 1080, "height": 1920}
        assert chain[1] == {"underlay": f"authenticated:ws:{WS}:asset1"}
        assert chain[2] == {
            "crop": "fill",
            "width": 1080,
            "height": 1920,
            "effect": "blur:2000",
        }
        assert chain[3] == {"flags": "layer_apply"}
        assert chain[4] == {"crop": "limit", "width": 1080}
        assert chain[5] == {
            "crop": "pad",
            "width": 1080,
            "height": 1920,
            "gravity": "center",
        }

    def test_a_video_is_trimmed_padded_then_encoded_to_the_story(self):
        from src.services.target.transit import story_transformation

        chain = story_transformation(f"ws/{WS}/clip1", media_kind="video")
        assert chain == [
            {"duration": 60},
            {"crop": "limit", "width": 1080, "height": 1920},
            {
                "crop": "pad",
                "width": 1080,
                "height": 1920,
                "background": "blurred:2000:15",
            },
            {
                "video_codec": "h264:high:4.2",
                "audio_codec": "aac",
                "audio_frequency": 48000,
                "fps": "23-60",
                "bit_rate": 13_000_000,
            },
        ]

    def test_the_codec_level_holds_the_story_frame_at_the_top_frame_rate(self):
        """H.264 Annex A, level 4.2: at most 8,704 macroblocks a frame and
        522,240 a second."""
        from src.services.target import media_validation
        from src.services.target.transit import STORY_HEIGHT, STORY_WIDTH

        matrix = media_validation.SUPPORTED_MEDIA_MATRIX
        assert matrix["video_codec"] == ("h264", "high", "4.2")
        frame = -(-STORY_WIDTH // 16) * -(-STORY_HEIGHT // 16)
        assert frame <= 8_704 and frame * matrix["fps"][1] <= 522_240

    def test_delivery_url_carries_the_frame(self):
        sdk = RecordingSdk()
        _store(sdk).delivery_url(f"ws/{WS}/asset1", media_kind="image")
        call = sdk.url_calls[0]
        assert call["transformation"][1] == {
            "underlay": f"authenticated:ws:{WS}:asset1"
        }
        assert call["sign_url"] is True and call["type"] == "authenticated"

    def test_the_real_sdk_renders_the_legacy_chain_and_signs_it(self):
        """The SDK's own URL builder, no network: the chain renders as
        the transformation string below, and the signature covers it."""
        import cloudinary.utils

        from src.services.target.transit import story_transformation

        ref = f"ws/{WS}/asset1"
        url, _ = cloudinary.utils.cloudinary_url(
            ref,
            transformation=story_transformation(ref, media_kind="image"),
            sign_url=True,
            type="authenticated",
            resource_type="image",
            secure=True,
            cloud_name="c",
            api_key="k",
            api_secret="s",
        )
        assert (
            f"/c_limit,h_1920,w_1080/u_authenticated:ws:{WS}:asset1"
            "/c_fill,e_blur:2000,h_1920,w_1080/fl_layer_apply/c_limit,w_1080"
            "/c_pad,g_center,h_1920,w_1080/"
        ) in url
        assert "/s--" in url, "signed"
        assert "/image/authenticated/" in url

    def test_the_real_sdk_renders_the_video_chain_as_mp4(self):
        import cloudinary.utils

        from src.services.target.transit import story_transformation

        ref = f"ws/{WS}/clip1"
        url, _ = cloudinary.utils.cloudinary_url(
            ref,
            transformation=story_transformation(ref, media_kind="video"),
            format="mp4",
            sign_url=True,
            type="authenticated",
            resource_type="video",
            secure=True,
            cloud_name="c",
            api_key="k",
            api_secret="s",
        )
        steps = re.search(r"/s--[^/]+--/(.+?)/(?:v\d+/)?ws/", url).group(1)
        assert steps == (
            "du_60/c_limit,h_1920,w_1080/b_blurred:2000:15,c_pad,h_1920,w_1080/"
            "ac_aac,af_48000,br_13000000,fps_23-60,vc_h264:high:4.2"
        ), "every step, in order, and no named transformation"
        assert url.endswith(".mp4") and "/video/authenticated/" in url

    def test_delivery_is_in_the_format_meta_accepts(self):
        sdk = RecordingSdk()
        store = _store(sdk)
        store.delivery_url(f"ws/{WS}/asset1", media_kind="image")
        store.delivery_url(f"ws/{WS}/clip1", media_kind="video")
        assert [c["format"] for c in sdk.url_calls] == ["jpg", "mp4"]

    def test_an_unknown_kind_is_refused_before_any_url(self):
        from src.services.target.transit import story_transformation

        with pytest.raises(ValueError, match="media_kind"):
            story_transformation("ws/x/y", media_kind="gif")


class TestTheStoryFrameIsDerivedEagerly:
    """Meta fetches the frame within a second of the container call, and a
    frame derived on demand can answer that first fetch with an error image.
    The upload derives the frame eagerly, so it exists before anyone asks."""

    @pytest.mark.asyncio
    async def test_an_image_upload_derives_the_story_frame_synchronously(self):
        from src.services.target.transit import STORY_FORMATS, story_transformation

        sdk = RecordingSdk()
        ref = await _store(sdk).upload(b"bytes", workspace_id=WS, media_kind="image")
        call = sdk.upload_calls[0]
        assert call["eager"] == [
            {
                "transformation": story_transformation(ref, media_kind="image"),
                "format": STORY_FORMATS["image"],
            }
        ]
        assert call["eager_async"] is False, "an image frame derives in the upload"

    @pytest.mark.asyncio
    async def test_a_video_frame_derives_in_the_background(self):
        from src.services.target.transit import STORY_FORMATS

        sdk = RecordingSdk()
        await _store(sdk).upload(b"bytes", workspace_id=WS, media_kind="video")
        call = sdk.upload_calls[0]
        assert call["eager"][0]["format"] == STORY_FORMATS["video"]
        assert call["eager_async"] is True, "a video frame may outlast a sync eager"


class TestReadiness:
    """`ready()` — the check the pipeline makes before handing Meta the URL."""

    def _store_with(self, script, *, now_step_s=1.0):
        from datetime import datetime, timedelta, timezone

        sdk = RecordingSdk()
        probes = []
        clock = {"t": datetime(2030, 1, 1, tzinfo=timezone.utc)}

        async def probe(url):
            probes.append(url)
            answer = script.pop(0) if script else script_default
            if isinstance(answer, Exception):
                raise answer
            return answer

        def now_fn():
            clock["t"] += timedelta(seconds=now_step_s)
            return clock["t"]

        script_default = (200, "image/jpeg")
        store = _store(sdk, probe_fn=probe, now_fn=now_fn)
        return store, probes

    @pytest.mark.asyncio
    async def test_ready_when_the_frame_serves_as_an_image(self):
        store, probes = self._store_with([(206, "image/jpeg")])
        assert await store.ready(f"ws/{WS}/abc", media_kind="image", sleep=_no_sleep)
        assert len(probes) == 1 and "abc" in probes[0]

    @pytest.mark.asyncio
    async def test_an_error_image_is_not_ready_until_the_frame_serves(self):
        store, probes = self._store_with(
            [(404, "image/gif"), (200, "text/html"), (200, "image/jpeg")]
        )
        assert await store.ready(f"ws/{WS}/abc", media_kind="image", sleep=_no_sleep)
        assert len(probes) == 3, "polled until a real image answered"

    @pytest.mark.asyncio
    async def test_a_frame_that_never_serves_is_not_ready_within_the_budget(self):
        store, probes = self._store_with([], now_step_s=7.0)
        # Every probe answers the error image; the clock advances 7 s per read.
        store._probe_fn = _always((404, "image/gif"))
        assert not await store.ready(
            f"ws/{WS}/abc", media_kind="image", budget_s=20.0, sleep=_no_sleep
        )

    @pytest.mark.asyncio
    async def test_a_probe_failure_is_not_yet_never_an_exception(self):
        store, probes = self._store_with([RuntimeError("dns"), (200, "video/mp4")])
        assert await store.ready(f"ws/{WS}/abc", media_kind="video", sleep=_no_sleep)

    # -- the bytes decide, and the answer says what it saw ---------------------

    @pytest.mark.asyncio
    async def test_a_placeholder_labelled_as_an_image_is_not_ready_until_the_bytes_are(
        self,
    ):
        """Cloudinary answers a miss with a 404 GIF; a relabelled or truncated
        body would pass a headers-only check. The first bytes decide."""
        from src.services.target.transit import ProbeAnswer

        store, probes = self._store_with(
            [
                ProbeAnswer(200, "image/jpeg", head=b"GIF89a\x01\x00"),
                ProbeAnswer(206, "image/jpeg", head=b"\xff\xd8\xff\xe0\x00\x10JFIF"),
            ]
        )
        ready = await store.ready(f"ws/{WS}/abc", media_kind="image", sleep=_no_sleep)
        assert ready and len(probes) == 2, "polled until the bytes were a JPEG"
        assert ready.observation["polls"] == 2 and ready.observation["status"] == 206

    @pytest.mark.asyncio
    async def test_the_answer_carries_what_the_probe_saw_for_the_ledger(self):
        from src.services.target.transit import ProbeAnswer

        store, _ = self._store_with(
            [
                ProbeAnswer(
                    206,
                    "image/jpeg",
                    head=b"\xff\xd8\xff",
                    length=137673,
                    request_id="b094d14f",
                    elapsed_ms=190,
                )
            ]
        )
        ready = await store.ready(f"ws/{WS}/abc", media_kind="image", sleep=_no_sleep)
        assert bool(ready) is True
        assert ready.observation == {
            "status": 206,
            "content_type": "image/jpeg",
            "length": 137673,
            "request_id": "b094d14f",
            "elapsed_ms": 190,
            "polls": 1,
            "bytes_read": True,
            "magic": "jpeg",
        }

    @pytest.mark.asyncio
    async def test_a_body_that_was_read_but_is_empty_is_not_the_frame(self):
        """An edge still filling the object can answer 200 image/jpeg with no
        bytes yet; a read that returned nothing proves nothing. Only a
        headers-only answer is judged on headers."""
        from src.services.target.transit import ProbeAnswer

        store, probes = self._store_with(
            [
                ProbeAnswer(200, "image/jpeg", head=b"", bytes_read=True),
                ProbeAnswer(200, "image/jpeg", head=b"", bytes_read=False),
            ]
        )
        ready = await store.ready(f"ws/{WS}/abc", media_kind="image", sleep=_no_sleep)
        assert ready and len(probes) == 2
        assert ready.observation["bytes_read"] is False

    @pytest.mark.asyncio
    async def test_an_answer_of_no_known_shape_is_not_yet_never_an_exception(self):
        store, probes = self._store_with([None, (200, "x", "y"), (206, "image/jpeg")])
        assert await store.ready(f"ws/{WS}/abc", media_kind="image", sleep=_no_sleep)
        assert len(probes) == 3

    @pytest.mark.asyncio
    async def test_a_frame_that_never_serves_reports_the_last_answer(self):
        store, _ = self._store_with([], now_step_s=7.0)
        store._probe_fn = _always((404, "image/gif"))
        ready = await store.ready(
            f"ws/{WS}/abc", media_kind="image", budget_s=20.0, sleep=_no_sleep
        )
        assert not ready
        assert ready.observation["status"] == 404
        assert ready.observation["content_type"] == "image/gif"
        assert ready.observation["polls"] >= 2 and ready.observation["magic"] is None

    @pytest.mark.asyncio
    async def test_a_video_frame_needs_its_container_signature(self):
        from src.services.target.transit import ProbeAnswer

        store, probes = self._store_with(
            [
                ProbeAnswer(206, "video/mp4", head=b"\x00" * 16),
                ProbeAnswer(206, "video/mp4", head=b"\x00\x00\x00\x18ftypisom"),
            ]
        )
        assert await store.ready(f"ws/{WS}/abc", media_kind="video", sleep=_no_sleep)
        assert len(probes) == 2

    def test_every_legal_first_box_of_a_video_container_is_its_signature(self):
        """`ftyp` is usual; `free`/`skip`/`wide`/`mdat`/`moov` first are legal
        ISO-BMFF too. A video that opened with one would otherwise poll its
        whole budget on every attempt."""
        from src.services.target.transit import magic_of

        for box in (b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide"):
            assert magic_of(b"\x00\x00\x00\x08" + box + b"isom") == "mp4", box
        assert magic_of(b"\x00\x00\x00\x08junk") is None
        assert magic_of(b"ftyp") is None, "a box needs its 4-byte size first"

    def test_the_total_length_is_read_only_from_a_range_that_served(self):
        from src.services.target.transit import _total_length

        assert _total_length({"content-range": "bytes 0-1023/137673"}) == 137673
        assert _total_length({"content-range": "bytes */137673"}) is None, "a 416"
        assert _total_length({"content-range": "bytes 0-1023/*"}) is None
        assert _total_length({}) is None

    def _floor_store(self, handler):
        """The default probe against the REAL egress floor: a mock transport
        answers, a static resolver keeps DNS out, the floor's cap is live."""
        import httpx

        store = _store(RecordingSdk(), probe_resolver=lambda host: ["93.184.216.34"])
        store._probe_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return store

    FRAME_URL = "https://res.cloudinary.com/c/image/authenticated/s--sig--/c_limit/v1/ws/a/b.jpg"

    @pytest.mark.asyncio
    async def test_the_default_probe_reads_the_first_bytes_under_the_floor(self):
        """A ranged GET, not a HEAD: headers cannot prove bytes."""
        import httpx

        from src.services.target import transit

        calls = []

        def handler(request):
            calls.append((request.method, request.headers.get("range")))
            return httpx.Response(
                206,
                headers={
                    "content-type": "image/jpeg",
                    "content-range": "bytes 0-1023/137673",
                    "x-request-id": "b094d14f79c6",
                },
                content=b"\xff\xd8\xff\xe0" + b"\x00" * 1020,
            )

        answer = await self._floor_store(handler)._default_probe(self.FRAME_URL)
        assert calls == [("GET", f"bytes=0-{transit.PROBE_RANGE_BYTES - 1}")]
        assert answer.status == 206 and answer.content_type == "image/jpeg"
        assert answer.bytes_read is True and answer.head[:2] == b"\xff\xd8"
        assert len(answer.head) <= transit.PROBE_RANGE_BYTES
        assert answer.length == 137673 and answer.request_id == "b094d14f79c6"
        assert isinstance(answer.elapsed_ms, int)

    @pytest.mark.asyncio
    async def test_a_frame_answered_whole_past_the_cap_falls_back_to_its_headers(
        self,
    ):
        """A 200 carrying the whole frame (the range ignored) must not read as
        "never ready" — the floor's cap trips, a HEAD answers the headers, and
        `bytes_read=False` on the ledger says the bytes were not judged."""
        import httpx

        from src.services.target import transit
        from src.services.target.transit import serves_media

        calls = []

        def handler(request):
            calls.append(request.method)
            if request.method == "HEAD":
                return httpx.Response(200, headers={"content-type": "image/jpeg"})
            return httpx.Response(
                200,
                headers={"content-type": "image/jpeg"},
                content=b"\xff\xd8"
                + b"\x00" * (transit.PROBE_MAX_RESPONSE_BYTES + 4096),
            )

        answer = await self._floor_store(handler)._default_probe(self.FRAME_URL)
        assert calls == ["GET", "HEAD"]
        assert answer.status == 200 and answer.bytes_read is False
        assert answer.head == b"" and answer.length is None
        assert serves_media(answer, "image"), "headers decide, as the HEAD did"

    @pytest.mark.asyncio
    async def test_a_whole_frame_under_the_cap_is_read_and_measured(self):
        import httpx

        def handler(request):
            return httpx.Response(
                200,
                headers={"content-type": "image/jpeg"},
                content=b"\xff\xd8\xff\xe0" + b"\x00" * 2000,
            )

        answer = await self._floor_store(handler)._default_probe(self.FRAME_URL)
        assert answer.status == 200 and answer.bytes_read is True
        assert answer.length == 2004 and answer.head[:2] == b"\xff\xd8"

    @pytest.mark.asyncio
    async def test_the_default_probe_reads_as_much_head_as_it_is_asked_for(self):
        """The container read asks for `moov`'s whole extent, past the poll's
        cap: the range names it, and the cap does not cut it short."""
        import httpx

        from src.services.target import transit

        calls = []
        wanted = transit.PROBE_MAX_RESPONSE_BYTES + 4096

        def handler(request):
            calls.append(request.headers.get("range"))
            return httpx.Response(
                206,
                headers={
                    "content-type": "video/mp4",
                    "content-range": f"bytes 0-{wanted - 1}/9000000",
                },
                content=b"\x00\x00\x00\x10ftypisom" + bytes(wanted - 12),
            )

        answer = await self._floor_store(handler)._default_probe(
            self.FRAME_URL, head_bytes=wanted
        )
        assert calls == [f"bytes=0-{wanted - 1}"]
        assert answer.bytes_read is True and len(answer.head) == wanted
        assert answer.length == 9_000_000

    # -- what a video's head says of Meta's container line -----------------

    def _video_store(self, data, *, container_read=None):
        """A store whose probe serves *data*'s head, as many bytes of it as
        each call asks for. *container_read*, when given, answers every read
        past the poll's first bytes instead: an exception to raise, or the
        bytes to serve."""
        from src.services.target.transit import PROBE_RANGE_BYTES, ProbeAnswer

        reads = []

        async def probe(url, *, head_bytes=PROBE_RANGE_BYTES):
            reads.append(head_bytes)
            head = data[:head_bytes]
            if head_bytes != PROBE_RANGE_BYTES and container_read is not None:
                if isinstance(container_read, Exception):
                    raise container_read
                head = container_read
            return ProbeAnswer(206, "video/mp4", head=head, length=len(data))

        return _store(RecordingSdk(), probe_fn=probe), reads

    @pytest.mark.asyncio
    async def test_a_video_whose_moov_comes_first_is_read_to_moov_s_end(self):
        """The first bytes say where `moov` ends; one more ranged read takes it
        whole, and what it holds rides the observation to the permit."""
        from src.services.target import media_validation, transit

        data = derivation(pad=4096)
        end = moov_end(data)
        store, reads = self._video_store(data)
        ready = await store.ready(f"ws/{WS}/abc", media_kind="video", sleep=_no_sleep)
        assert ready and reads == [transit.PROBE_RANGE_BYTES, end]
        container = ready.observation["container"]
        assert container == media_validation.derivation_container(data[:end])
        assert container["moov_first"] is True and container["moov_end"] == end
        assert container["edit_lists"] == 0 and "unread" not in container

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "frequency_index,check",
        [(3, None), (0, "derivation_audio_rate")],
        ids=["48-kHz", "96-kHz"],
    )
    async def test_the_judge_reads_what_ready_records(self, frequency_index, check):
        """`judge_derivation` reads `ready`'s own observation: AAC at 96 kHz
        is refused from it, and at 48 kHz it is not."""
        from src.services.target import media_validation

        data = derivation(audio=audio_entry(frequency_index=frequency_index), pad=4096)
        store, _ = self._video_store(data)
        ready = await store.ready(f"ws/{WS}/abc", media_kind="video", sleep=_no_sleep)
        if check is None:
            media_validation.judge_derivation(ready.observation, "video")
        else:
            with pytest.raises(media_validation.UnsupportedMedia, match=check):
                media_validation.judge_derivation(ready.observation, "video")

    @pytest.mark.asyncio
    async def test_a_video_whose_media_comes_first_is_read_no_further(self):
        from src.services.target import transit

        store, reads = self._video_store(derivation(late=True))
        ready = await store.ready(f"ws/{WS}/abc", media_kind="video", sleep=_no_sleep)
        assert ready and reads == [transit.PROBE_RANGE_BYTES]
        assert ready.observation["container"]["moov_first"] is False

    @pytest.mark.asyncio
    async def test_a_moov_past_the_read_cap_is_left_unread(self):
        from src.services.target import transit

        store, reads = self._video_store(
            derivation(pad=transit.CONTAINER_READ_MAX_BYTES)
        )
        ready = await store.ready(f"ws/{WS}/abc", media_kind="video", sleep=_no_sleep)
        assert ready and reads == [transit.PROBE_RANGE_BYTES]
        container = ready.observation["container"]
        assert container["unread"] == "moov_past_read_cap"
        assert container["moov_end"] > transit.CONTAINER_READ_MAX_BYTES
        assert container["edit_lists"] is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "container_read,unread",
        [
            (RuntimeError("connection reset"), ("read_failed", "RuntimeError")),
            (b"\x00\x00\x00\x10ftypisom", ("short_read", None)),
        ],
        ids=["failed", "short"],
    )
    async def test_a_container_read_that_misses_leaves_its_facts_unread(
        self, container_read, unread
    ):
        """The frame serves either way: the ledger says what the read missed,
        and Meta's answer judges it."""
        store, _ = self._video_store(
            derivation(pad=4096), container_read=container_read
        )
        ready = await store.ready(f"ws/{WS}/abc", media_kind="video", sleep=_no_sleep)
        assert ready
        container = ready.observation["container"]
        assert (container["unread"], container.get("read_error")) == unread
        assert container["moov_first"] is True and container["edit_lists"] is None

    @pytest.mark.asyncio
    async def test_an_image_frame_reads_no_container(self):
        store, probes = self._store_with([(206, "image/jpeg")])
        ready = await store.ready(f"ws/{WS}/abc", media_kind="image", sleep=_no_sleep)
        assert ready and len(probes) == 1 and "container" not in ready.observation


class TestFreshUrls:
    """The float: a refused fetch is retried with a url Meta has never seen
    for this story — the same original through the same chain plus `n` no-op
    steps (`dpr_1.0`), a distinct signed url and a distinct derived asset with
    identical bytes for every `n`."""

    def test_variant_n_appends_n_no_op_steps_and_stays_signed(self):
        from src.services.target.transit import (
            URL_VARIANT_STEP,
            story_transformation,
        )

        sdk = RecordingSdk()
        store = _store(sdk)
        ref = f"ws/{WS}/abc"
        store.delivery_url(ref, media_kind="image", variant=3)
        call = sdk.url_calls[-1]
        assert (
            call["transformation"]
            == story_transformation(ref, media_kind="image") + [URL_VARIANT_STEP] * 3
        )
        assert call["sign_url"] is True and call["type"] == "authenticated"

    def test_variant_zero_is_the_plain_url(self):
        from src.services.target.transit import story_transformation

        sdk = RecordingSdk()
        store = _store(sdk)
        ref = f"ws/{WS}/abc"
        plain = store.delivery_url(ref, media_kind="image")
        zero = store.delivery_url(ref, media_kind="image", variant=0)
        assert plain == zero
        assert sdk.url_calls[-1]["transformation"] == story_transformation(
            ref, media_kind="image"
        )

    def test_every_variant_is_a_distinct_signed_url_through_the_real_sdk(self):
        """No network: `cloudinary_url` signs locally. Five variants, five urls,
        each carrying its `dpr_1.0` steps and its own signature."""
        import cloudinary.utils

        store = _store(RecordingSdk(), url_fn=cloudinary.utils.cloudinary_url)
        ref = f"ws/{WS}/abc"
        urls = [
            store.delivery_url(ref, media_kind="image", variant=n) for n in range(6)
        ]
        assert len(set(urls)) == 6
        for n, url in enumerate(urls):
            assert url.count("dpr_1.0") == n
            assert "/s--" in url, "a variant is signed like the plain url"

    def test_a_negative_variant_is_refused(self):
        store = _store(RecordingSdk())
        with pytest.raises(ValueError):
            store.delivery_url(f"ws/{WS}/abc", media_kind="image", variant=-1)

    @pytest.mark.asyncio
    async def test_ready_probes_the_variant_url(self):
        sdk = RecordingSdk()
        probes = []

        async def probe(url):
            probes.append(url)
            return (206, "image/jpeg")

        # The recording sdk's url ignores the transformation, so distinctness is
        # proven above; here the probe must be handed the VARIANT's url call.
        store = _store(sdk, probe_fn=probe)
        assert await store.ready(
            f"ws/{WS}/abc", media_kind="image", variant=2, sleep=_no_sleep
        )
        assert len(probes) == 1
        from src.services.target.transit import URL_VARIANT_STEP, story_transformation

        assert (
            sdk.url_calls[-1]["transformation"]
            == story_transformation(f"ws/{WS}/abc", media_kind="image")
            + [URL_VARIANT_STEP] * 2
        )


def _always(answer):
    async def probe(url):
        return answer

    return probe


async def _no_sleep(_s):
    return None
