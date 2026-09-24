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
        # The public id is minted by the store under the workspace folder
        # (the eager story frame needs the id before the upload).
        public_id = sdk.upload_calls[0]["public_id"]
        assert public_id.startswith(f"ws/{WS}/") and "folder" not in sdk.upload_calls[0]
        assert len(public_id.rsplit("/", 1)[1]) == 20

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
#: on every supported interpreter.
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
    @pytest.mark.parametrize(
        "spelling", list(_SPELLINGS.values()), ids=list(_SPELLINGS)
    )
    async def test_a_provider_format_shift_does_not_reap_the_corpus(self, spelling):
        """The provider renders ``created_at`` with one serializer, so a
        format shift reaches EVERY row at once. Each case is a whole corpus
        re-spelled: the in-flight asset must survive AND the past-TTL one must
        still go — a sweep that stops reaping is safe only by having stopped.
        Mutation that reddens: parse with the interpreter's bare
        ``fromisoformat`` — on 3.10, CI's interpreter, every width but 0/3/6
        and every offset but ``Z``/``±HH:MM`` then reads as no age at all (on
        3.11+, ``z`` alone does)."""
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
                            {
                                "public_id": "ws/b/in-flight-3",
                                "created_at": "Wed, 19 Aug 2026 21:00:00 GMT",
                            }
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
        import re

        allowed = set(re.findall(r"'([a-z_]+)'", str(ck.sqltext)))
        assert allowed == set(_RESOURCE_TYPES)


class TestUploadFailuresAreTyped:
    """#1276 review: the SDK raises its own family; the pipeline routes on
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
    """The delivery URL frames the asset for a story (owner, 2026-09-10 —
    the first real post went up unscaled): 1080 × 1920 over a blurred copy
    of itself, as the legacy `get_story_optimized_url` chain did."""

    def test_an_image_is_padded_over_a_blurred_underlay_of_itself(self):
        from src.services.target.transit import story_transformation

        chain = story_transformation(f"ws/{WS}/asset1", media_kind="image")
        # The picture is fitted into the frame BEFORE the underlay is laid:
        # a layer's canvas is the base's size, and a phone photo wider than
        # 1080 would otherwise hide the underlay (white bars — the legacy
        # defect).
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

    def test_a_video_is_padded_with_cloudinarys_blurred_background(self):
        from src.services.target.transit import story_transformation

        chain = story_transformation(f"ws/{WS}/clip1", media_kind="video")
        assert chain == [
            {"crop": "limit", "width": 1080, "height": 1920},
            {
                "crop": "pad",
                "width": 1080,
                "height": 1920,
                "background": "blurred:2000:15",
            },
        ]

    def test_delivery_url_carries_the_frame(self):
        sdk = RecordingSdk()
        _store(sdk).delivery_url(f"ws/{WS}/asset1", media_kind="image")
        call = sdk.url_calls[0]
        assert call["transformation"][1] == {
            "underlay": f"authenticated:ws:{WS}:asset1"
        }
        assert call["sign_url"] is True and call["type"] == "authenticated"

    def test_the_real_sdk_renders_the_legacy_chain_and_signs_it(self):
        """The SDK's own URL builder, no network: the chain reads as the
        legacy transformation did, and the signature covers it."""
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
        assert "/c_limit,h_1920,w_1080/b_blurred:2000:15,c_pad,h_1920,w_1080/" in url
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
    """Investigation of 2026-09-11: Meta fetches the frame within a second of
    the container call; a frame derived on demand took 2.4–3.1 s to first
    byte and the first fetch of a fresh asset could answer with an error
    image — 4 of 7 publishes lost to 9004/2207052. The upload derives the
    frame eagerly, so it exists before anyone asks."""

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

    # -- 2026-09-13: the bytes decide, and the answer says what it saw ---------

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
        bytes yet; a read that returned nothing proves nothing (adversarial
        review of the 2026-09-13 PR). Only a headers-only answer is judged on
        headers."""
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
        ISO-BMFF too (adversarial review). A video that opened with one would
        otherwise poll its whole budget on every attempt."""
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
        """Both reviews of the 2026-09-13 PR: a 200 carrying the whole frame
        (the range ignored) must not read as "never ready" — the floor's cap
        trips, a HEAD answers the headers as before, and `bytes_read=False`
        on the ledger says the bytes were not judged."""
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


class TestFreshUrls:
    """The float (plan 03, D1): a refused fetch is retried with a url Meta has
    never seen for this story — the same original through the same chain plus
    `n` no-op steps (`dpr_1.0`, validated 2026-09-13/14), a distinct signed
    url and a distinct derived asset with identical bytes for every `n`."""

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

    def test_variant_zero_is_todays_url(self):
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
