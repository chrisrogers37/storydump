"""L.5 slice 2, PR-A — the FC-3 transit store (#915, `00` FC-3, `03` D28/D38).

Every assertion here is a named FC-3/D28/D38 requirement, asserted at the
injected SDK boundary (a recording fake), because the property under test IS
the parameter set that rides in the signed request. Scope stated honestly:
Cloudinary's signature computation is the SDK's contract and is not re-tested
here; what is ours — and what these tests pin — is WHICH parameters are in
the signed request and WHICH SDK door is called.

Mutation discipline (the 2026-08-19 green-is-not-coverage rule): every test
names, in its docstring, the production mutation that reddens it. The battery
is run post-GREEN and the test→mutation map recorded in the PR body.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.services.target.transit import TransitStore

WS = "0b6ad24e-6b3e-4a86-9e6b-1a2b3c4d5e6f"  # a real UUID — folder scoping is validated
NOW = datetime(2026, 8, 19, 22, 0, 0, tzinfo=timezone.utc)


class RecordingSdk:
    """A recorder standing where the cloudinary SDK callables stand.

    Returns SDK-shaped payloads so the store's parsing is the real code path;
    records every call's kwargs so the signed-parameter set is assertable.
    """

    def __init__(self, *, resources_pages=None, destroy_result=None):
        self.upload_calls: list[dict] = []
        self.destroy_calls: list[dict] = []
        self.resources_calls: list[dict] = []
        self.url_calls: list[dict] = []
        self._resources_pages = resources_pages or {}
        self._destroy_result = destroy_result or {"result": "ok"}

    def upload(self, source, **options):
        self.upload_calls.append({"source": source, **options})
        folder = options.get("folder", "")
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
        assert store._url_fn is cloudinary.utils.cloudinary_url


class TestUploadFC3:
    @pytest.mark.asyncio
    async def test_upload_scopes_the_asset_under_the_workspace_folder(self):
        """FC-3.1: every transit asset uploads under ws/{workspace_id}/…
        Mutation that reddens: drop or flatten the folder option."""
        sdk = RecordingSdk()
        await _store(sdk).upload(b"bytes", workspace_id=WS, media_kind="image")
        assert sdk.upload_calls[0]["folder"] == f"ws/{WS}"

    @pytest.mark.asyncio
    async def test_upload_is_authenticated_type_never_public(self):
        """FC-3.3: assets upload as type=authenticated — never public-by-default.
        Mutation that reddens: drop the type option (SDK default is public
        'upload')."""
        sdk = RecordingSdk()
        await _store(sdk).upload(b"bytes", workspace_id=WS, media_kind="image")
        assert sdk.upload_calls[0]["type"] == "authenticated"

    @pytest.mark.asyncio
    async def test_upload_uses_no_preset_and_carries_per_call_credentials(self):
        """D28: zero preset objects — the request itself is signed per-call with
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
        public_id verbatim, folder prefix included (FC-3.5 destroys by it).
        Mutation that reddens: return anything but result['public_id']."""
        sdk = RecordingSdk()
        ref = await _store(sdk).upload(b"bytes", workspace_id=WS, media_kind="image")
        assert ref == f"ws/{WS}/r4nd0m1d"

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
        assert sdk.upload_calls[0]["folder"] == f"ws/{WS}"

    @pytest.mark.asyncio
    async def test_a_workspace_id_that_is_not_a_uuid_never_reaches_the_provider(self):
        """D28's isolation is only as strong as the folder string: a
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


class TestDeliveryUrlD38:
    @pytest.mark.parametrize("kind,rtype", [("image", "image"), ("video", "video")])
    def test_delivery_url_is_signed_authenticated_and_matches_the_kind(
        self, kind, rtype
    ):
        """FC-3.2 (amended, D38): delivery is SIGNED, against the authenticated
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
        """D38's amendment verbatim: 'the time-limit property attaches to the
        asset, not the URL'. No auth_token, no expires_at — asset destruction
        (FC-3.5/3.6) is the expiry ceiling.
        Mutation that reddens: pass an auth_token/expires_at."""
        sdk = RecordingSdk()
        _store(sdk).delivery_url(f"ws/{WS}/asset1", media_kind="image")
        call = sdk.url_calls[0]
        assert "auth_token" not in call
        assert "expires_at" not in call

    def test_delivery_url_carries_per_call_credentials(self):
        """Same D28 no-global-config rule as upload: the signature must be
        computed from THIS store's secret, not whatever process-global config
        happens to hold.
        Mutation that reddens: drop the per-call credentials."""
        sdk = RecordingSdk()
        _store(sdk).delivery_url(f"ws/{WS}/asset1", media_kind="image")
        call = sdk.url_calls[0]
        assert call["cloud_name"] == "test-cloud"
        assert call["api_secret"] == "test-secret"


class TestDestroyFC35:
    @pytest.mark.asyncio
    async def test_destroy_names_the_authenticated_type_and_invalidates(self):
        """FC-3.5's mechanism: a destroy that omits type=authenticated targets
        the PUBLIC namespace and silently misses the asset; one that omits
        invalidate leaves CDN-cached copies alive, breaking D38's 'destruction
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
        asset is gone (a prior destroy or the FC-3.6 sweep won), which is
        success for FC-3.5. Anything else is False — the sweep retries.
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


def _aged(public_id: str, hours_old: float, now: datetime) -> dict:
    created = now - timedelta(hours=hours_old)
    return {
        "public_id": public_id,
        "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


class TestListStaleFC36:
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
        """FC-3.6: the sweep destroys assets OLDER than the TTL regardless of
        pipeline state — and must not touch in-flight (fresh) transit assets.
        Mutation that reddens: invert or drop the age comparison."""
        ids = {a["public_id"] for a in await _stale(self._sdk())}
        assert ids == {"ws/a/old-img", "ws/b/old-img2", "ws/c/old-vid"}

    @pytest.mark.asyncio
    async def test_list_stale_walks_every_page_and_both_resource_types(self):
        """One GLOBAL age-filtered listing per sweep (04's rule) still means
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
            assert call["timeout"] == 60.0, (
                "a hung listing stalls the FC-3.6 sweep tick"
            )

    @pytest.mark.asyncio
    async def test_a_provider_format_shift_does_not_reap_the_corpus(self):
        """Fail-toward-stale is a ROW-level rule; a benign provider format
        change (fractional seconds, explicit offset) must not parse as
        garbage — under a strict single-format parse, one format shift makes
        EVERY in-flight asset 'stale' on the next tick and the backstop
        becomes the outage that destroys mid-publish media.
        Mutation that reddens: narrow the parse back to one strptime format."""
        fresh = NOW - timedelta(hours=1)
        sdk = RecordingSdk(
            resources_pages={
                "image": [
                    {
                        "resources": [
                            {
                                "public_id": "ws/a/frac",
                                "created_at": fresh.strftime(
                                    "%Y-%m-%dT%H:%M:%S.123456Z"
                                ),
                            },
                            {
                                "public_id": "ws/a/offset",
                                "created_at": fresh.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                            },
                        ]
                    }
                ],
                "video": [{"resources": []}],
            }
        )
        assert await _stale(sdk) == []

    @pytest.mark.asyncio
    async def test_an_unparseable_created_at_fails_toward_reaping(self):
        """The sweep is the BACKSTOP: an asset whose age cannot be read must be
        treated as stale (reaped), never skipped (immortalized past the hard
        TTL). Missing and malformed created_at both count.
        Mutation that reddens: skip rows whose created_at cannot be parsed."""
        sdk = RecordingSdk(
            resources_pages={
                "image": [
                    {
                        "resources": [
                            {"public_id": "ws/a/no-date"},
                            {
                                "public_id": "ws/a/bad-date",
                                "created_at": "yesterday-ish",
                            },
                            _aged("ws/a/fresh", 1, NOW),
                        ]
                    }
                ],
                "video": [{"resources": []}],
            }
        )
        assert {a["public_id"] for a in await _stale(sdk)} == {
            "ws/a/no-date",
            "ws/a/bad-date",
        }

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
        """FC-3.6 assembled: scheduler.execute_reap_transit_assets (merged at
        L.7, untested until now) driven by THIS store's lister and deleter
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
        import re

        allowed = set(re.findall(r"'([a-z_]+)'", str(ck.sqltext)))
        assert allowed == set(_RESOURCE_TYPES)
