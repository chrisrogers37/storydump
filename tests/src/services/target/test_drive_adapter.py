"""#982 — the Drive read seam, and the bound-detectability contract.

The listing leg's hazard is that **truncation looks exactly like paging**. These
tests exist because a caller handed a full page cannot tell "that is all" from
"that is the first N" unless the protocol says so structurally.
"""

import pytest

from src.services.target.drive_adapter import (
    DEFAULT_PAGE_SIZE,
    DriveFile,
    DriveLostResponse,
    DrivePage,
    DriveRetryableError,
    DriveTerminalError,
    StubDriveAdapter,
)


#: D37's source config shape — the door takes this, not a bare folder ref.
CFG = {"v": 1, "folder_ref": "drive-folder-1"}


def _files(n, prefix="f"):
    return tuple(
        DriveFile(
            file_id=f"{prefix}{i}", name=f"{prefix}{i}.jpg", mime_type="image/jpeg"
        )
        for i in range(n)
    )


class TestABoundIsAnnouncedNeverAbsorbed:
    """The contract that makes a page limit safe to hand to a caller."""

    @pytest.mark.asyncio
    async def test_a_full_page_does_not_claim_exhaustion(self):
        """The defect this protocol exists to prevent, stated as a test.

        A caller receiving exactly `page_size` files must NOT be able to read
        that as the whole listing. Only the token says that.
        """
        stub = StubDriveAdapter(
            pages=[DrivePage(files=_files(3), next_page_token="more")]
        )
        page = await stub.list_files(CFG, page_size=3)
        assert len(page.files) == 3
        assert page.next_page_token == "more"
        assert page.exhausted is False, (
            "a full page reported exhausted — a caller cannot then tell "
            "truncation from completion, which is the whole hazard"
        )

    @pytest.mark.asyncio
    async def test_capping_a_page_still_hands_back_a_token(self):
        """Enforcing the bound must not consume the remainder.

        This is the 'data loss wearing a bound's clothing' shape: returning the
        first N and claiming exhaustion is silent truncation.
        """
        stub = StubDriveAdapter(
            pages=[DrivePage(files=_files(10), next_page_token=None)]
        )
        page = await stub.list_files(CFG, page_size=4)
        assert len(page.files) == 4, "the page bound must be honoured"
        assert page.next_page_token is not None, (
            "the adapter dropped 6 files and reported exhausted — the bound "
            "absorbed data instead of announcing it"
        )
        assert page.exhausted is False

    @pytest.mark.asyncio
    async def test_exhaustion_is_the_token_and_not_the_count(self):
        """A SHORT page is not exhaustion either, unless the token says so."""
        short_but_more = StubDriveAdapter(
            pages=[DrivePage(files=_files(1), next_page_token="t")]
        )
        assert (await short_but_more.list_files(CFG)).exhausted is False
        genuinely_done = StubDriveAdapter(
            pages=[DrivePage(files=_files(1), next_page_token=None)]
        )
        assert (await genuinely_done.list_files(CFG)).exhausted is True

    @pytest.mark.asyncio
    async def test_an_empty_listing_is_exhausted_not_an_error(self):
        assert (await StubDriveAdapter().list_files(CFG)).exhausted is True

    @pytest.mark.asyncio
    async def test_the_caller_can_traverse_to_completion(self):
        """The documented loop terminates and yields every file exactly once."""
        stub = StubDriveAdapter(
            pages=[
                DrivePage(files=_files(2, "a"), next_page_token="p2"),
                DrivePage(files=_files(2, "b"), next_page_token="p3"),
                DrivePage(files=_files(1, "c"), next_page_token=None),
            ]
        )
        seen, page = [], await stub.list_files(CFG)
        seen.extend(page.files)
        while not page.exhausted:
            page = await stub.list_files(CFG, page_token=page.next_page_token)
            seen.extend(page.files)
        assert [f.file_id for f in seen] == ["a0", "a1", "b0", "b1", "c0"]
        assert stub.list_calls == [None, "p2", "p3"], (
            "each continuation must carry the PREVIOUS page's token"
        )


class TestTheDoorReceivesTheSourceConfig:
    """#987 review (astrid): a bare folder ref cannot express D37.

    `media_sources.config` is `{v, folder_ref, root_name?}` and `root_name`
    scopes listing to a SUBFOLDER. A door that could not receive it would list
    a subfolder-scoped source from the drive root — quietly, and looking like a
    correct listing whether it came back full or empty.
    """

    @pytest.mark.asyncio
    async def test_the_whole_config_reaches_the_door_including_root_name(self):
        stub = StubDriveAdapter(pages=[DrivePage(files=_files(1))])
        cfg = {"v": 1, "folder_ref": "fld-9", "root_name": "Campaigns/2026"}
        await stub.list_files(cfg)
        assert stub.configs_seen == [cfg], (
            "the door must receive the full config; anything it cannot see it "
            "cannot honour, and a dropped root_name lists the wrong folder"
        )

    @pytest.mark.asyncio
    async def test_a_config_key_the_door_does_not_know_yet_still_arrives(self):
        """The mapping is passed whole so a later D37 key needs no signature change."""
        stub = StubDriveAdapter(pages=[DrivePage(files=())])
        await stub.list_files({"v": 1, "folder_ref": "f", "shared_drive_id": "sd-1"})
        assert stub.configs_seen[0]["shared_drive_id"] == "sd-1"

    @pytest.mark.asyncio
    async def test_the_recorded_config_is_a_copy_not_a_live_reference(self):
        """A caller mutating its config afterwards must not rewrite history."""
        stub = StubDriveAdapter(pages=[DrivePage(files=())])
        cfg = {"v": 1, "folder_ref": "f"}
        await stub.list_files(cfg)
        cfg["folder_ref"] = "MUTATED"
        assert stub.configs_seen[0]["folder_ref"] == "f"


class TestAShapelessConfigIsRefused:
    """#987 review (astrid): expressible is not the same as required.

    Passing the config mapping fixed the ability to CARRY `folder_ref`. It did
    not add a refusal to ACCEPT its absence — so the wrong-answer-that-looks-
    right did not go away, it moved from *inexpressible* to *omittable*. A
    subfolder source with no `folder_ref` still lists from the drive root and
    still looks correct whether it comes back full or empty. The database
    cannot help: `ck_sources_config_v` validates only that `v` is a number.
    """

    @pytest.mark.asyncio
    async def test_a_config_without_folder_ref_is_refused(self):
        stub = StubDriveAdapter(pages=[DrivePage(files=_files(1))])
        with pytest.raises(DriveTerminalError):
            await stub.list_files({"v": 1})

    @pytest.mark.asyncio
    async def test_the_refusal_happens_BEFORE_any_listing(self):
        """A refusal after the work is a warning, not a guard."""
        stub = StubDriveAdapter(pages=[DrivePage(files=_files(1))])
        with pytest.raises(DriveTerminalError):
            await stub.list_files({"v": 1})
        assert stub.list_calls == [] and stub.configs_seen == [], (
            "the shapeless config was recorded/served before being refused"
        )

    def test_the_guard_is_the_CONTRACT_not_the_stubs_private_behaviour(self):
        """The placement is the finding, not the check.

        A refusal living only inside `StubDriveAdapter` would let a consumer go
        green against an obligation the real door never inherited — the same
        failure as a sync stub defining a shape the real implementation cannot
        keep. So it is a module-level function every implementation calls, and
        this test fails if someone later inlines it back into the stub.
        """
        from src.services.target import drive_adapter as mod

        assert callable(getattr(mod, "validate_source_config", None)), (
            "the config refusal must be reachable by the real adapter, not "
            "private to the stand-in"
        )
        with pytest.raises(DriveTerminalError):
            mod.validate_source_config({"v": 1})
        mod.validate_source_config({"v": 1, "folder_ref": "f"})  # must not raise

    def test_root_name_stays_optional(self):
        """Guard the guard: over-refusing breaks every source without a subfolder."""
        from src.services.target import drive_adapter as mod

        mod.validate_source_config({"v": 1, "folder_ref": "f"})


class TestEveryStubOutcomeCanBeSelected:
    """A stub whose error paths cannot be reached only ever proves the happy one."""

    @pytest.mark.asyncio
    async def test_fetch_returns_the_scripted_bytes(self):
        stub = StubDriveAdapter(contents={"f1": b"jpegdata"})
        assert await stub.fetch_bytes("f1") == b"jpegdata"
        assert stub.fetch_calls == ["f1"]

    @pytest.mark.parametrize(
        "exc", [DriveRetryableError("busy"), DriveTerminalError("gone")]
    )
    @pytest.mark.asyncio
    async def test_a_scripted_fetch_error_is_raised(self, exc):
        stub = StubDriveAdapter(contents={"f1": b"x"}, fetch_errors={"f1": exc})
        with pytest.raises(type(exc)):
            await stub.fetch_bytes("f1")

    @pytest.mark.asyncio
    async def test_an_unscripted_file_is_terminal_not_a_KeyError(self):
        with pytest.raises(DriveTerminalError):
            await StubDriveAdapter().fetch_bytes("nope")

    @pytest.mark.asyncio
    async def test_a_scripted_list_error_is_raised_on_the_right_call(self):
        stub = StubDriveAdapter(
            pages=[DrivePage(files=_files(1), next_page_token="t")],
            list_errors={1: DriveRetryableError("page 2 failed")},
        )
        await stub.list_files(CFG)  # call 0 succeeds
        with pytest.raises(DriveRetryableError):
            await stub.list_files(CFG, page_token="t")

    @pytest.mark.asyncio
    async def test_lost_response_is_not_a_DriveError(self):
        """ "We do not know" must not be catchable as "it failed"."""
        from src.services.target.drive_adapter import DriveError

        assert not issubclass(DriveLostResponse, DriveError)
        stub = StubDriveAdapter(fetch_errors={"f": DriveLostResponse("timeout")})
        with pytest.raises(DriveLostResponse):
            await stub.fetch_bytes("f")


class TestTheSeamParksLoudly:
    """astrid's W6 parks behind this seam; the reason must NAME it."""

    def _registry(self, **kw):
        from src.services.target.work_loop import WorkerDeps, build_registry

        return build_registry(WorkerDeps(**kw))

    @pytest.mark.parametrize("kind", ["sync_media_source", "first_ingest_chunk"])
    def test_without_the_seam_the_reason_names_it(self, kind):
        from src.services.target.work_loop import Parked

        entry = self._registry()[kind]
        assert isinstance(entry, Parked)
        assert "drive" in entry.reason.lower(), (
            f"{kind} parked without naming the seam it is blocked on — "
            f"got {entry.reason!r}"
        )
        assert "982" in entry.reason, "the park should point at its build-path item"

    def test_wiring_the_seam_un_parks_only_the_kinds_the_seam_gates(self):
        """A wired seam must not pretend an executor exists.

        The predecessor of this test asserted that with the seam wired,
        `sync_media_source` was STILL parked, and declared itself a pin that
        expires once W6 lands. It expired exactly as designed: the executor
        was written, the kind went live, and the assertion reported a correct
        un-park as a defect.

        The property is kept; only the way it is measured is replaced. This
        form is DIFFERENTIAL — the registry with the seam against the registry
        without it — so it names no kind as built or unbuilt and cannot expire
        the next time an executor lands. What it forbids is the seam reaching
        beyond the kinds it gates, in either direction, which is the failure
        the pin was really there to catch.

        The gated set IS named, and that is the distinction from what expired:
        it pins the SEAM'S CONTRACT, which changes only when someone changes
        the seam, rather than the BUILD STATE, which changes under unrelated
        work. A new kind arriving behind this door should fail here.
        """
        from src.services.target.work_loop import Parked

        wired = self._registry(drive=StubDriveAdapter())
        unwired = self._registry()

        def live(registry):
            return {k for k, e in registry.items() if not isinstance(e, Parked)}

        gated = live(wired) - live(unwired)
        assert gated == {"sync_media_source", "first_ingest_chunk"}, (
            f"wiring the drive seam moved kinds it does not gate — got {sorted(gated)}"
        )
        assert not live(unwired) - live(wired), (
            "removing the drive seam left MORE kinds live, which is backwards"
        )

        # The other half of the original guard, generalised off the single
        # kind it named. Its positive control is the test above: those reasons
        # demonstrably DO exist while the seam is absent.
        blaming = {
            k: e.reason
            for k, e in wired.items()
            if isinstance(e, Parked) and "drive" in e.reason.lower()
        }
        assert not blaming, (
            f"the seam is wired and these kinds still blame it: {blaming}"
        )

    @pytest.mark.asyncio
    async def test_the_default_page_size_is_a_page_bound_not_a_total(self):
        """Guard against a later 'tidy-up' turning the page bound into a cap."""
        stub = StubDriveAdapter(
            pages=[DrivePage(files=_files(DEFAULT_PAGE_SIZE + 5), next_page_token=None)]
        )
        page = await stub.list_files(CFG)
        assert len(page.files) == DEFAULT_PAGE_SIZE
        assert page.next_page_token is not None, (
            "the default page size silently dropped the remainder"
        )
