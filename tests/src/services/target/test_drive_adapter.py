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


def _files(n, prefix="f"):
    return tuple(
        DriveFile(
            file_id=f"{prefix}{i}", name=f"{prefix}{i}.jpg", mime_type="image/jpeg"
        )
        for i in range(n)
    )


class TestABoundIsAnnouncedNeverAbsorbed:
    """The contract that makes a page limit safe to hand to a caller."""

    def test_a_full_page_does_not_claim_exhaustion(self):
        """The defect this protocol exists to prevent, stated as a test.

        A caller receiving exactly `page_size` files must NOT be able to read
        that as the whole listing. Only the token says that.
        """
        stub = StubDriveAdapter(
            pages=[DrivePage(files=_files(3), next_page_token="more")]
        )
        page = stub.list_files("src-1", page_size=3)
        assert len(page.files) == 3
        assert page.next_page_token == "more"
        assert page.exhausted is False, (
            "a full page reported exhausted — a caller cannot then tell "
            "truncation from completion, which is the whole hazard"
        )

    def test_capping_a_page_still_hands_back_a_token(self):
        """Enforcing the bound must not consume the remainder.

        This is the 'data loss wearing a bound's clothing' shape: returning the
        first N and claiming exhaustion is silent truncation.
        """
        stub = StubDriveAdapter(
            pages=[DrivePage(files=_files(10), next_page_token=None)]
        )
        page = stub.list_files("src-1", page_size=4)
        assert len(page.files) == 4, "the page bound must be honoured"
        assert page.next_page_token is not None, (
            "the adapter dropped 6 files and reported exhausted — the bound "
            "absorbed data instead of announcing it"
        )
        assert page.exhausted is False

    def test_exhaustion_is_the_token_and_not_the_count(self):
        """A SHORT page is not exhaustion either, unless the token says so."""
        short_but_more = StubDriveAdapter(
            pages=[DrivePage(files=_files(1), next_page_token="t")]
        )
        assert short_but_more.list_files("s").exhausted is False
        genuinely_done = StubDriveAdapter(
            pages=[DrivePage(files=_files(1), next_page_token=None)]
        )
        assert genuinely_done.list_files("s").exhausted is True

    def test_an_empty_listing_is_exhausted_not_an_error(self):
        assert StubDriveAdapter().list_files("s").exhausted is True

    def test_the_caller_can_traverse_to_completion(self):
        """The documented loop terminates and yields every file exactly once."""
        stub = StubDriveAdapter(
            pages=[
                DrivePage(files=_files(2, "a"), next_page_token="p2"),
                DrivePage(files=_files(2, "b"), next_page_token="p3"),
                DrivePage(files=_files(1, "c"), next_page_token=None),
            ]
        )
        seen, page = [], stub.list_files("s")
        seen.extend(page.files)
        while not page.exhausted:
            page = stub.list_files("s", page_token=page.next_page_token)
            seen.extend(page.files)
        assert [f.file_id for f in seen] == ["a0", "a1", "b0", "b1", "c0"]
        assert stub.list_calls == [None, "p2", "p3"], (
            "each continuation must carry the PREVIOUS page's token"
        )


class TestEveryStubOutcomeCanBeSelected:
    """A stub whose error paths cannot be reached only ever proves the happy one."""

    def test_fetch_returns_the_scripted_bytes(self):
        stub = StubDriveAdapter(contents={"f1": b"jpegdata"})
        assert stub.fetch_bytes("f1") == b"jpegdata"
        assert stub.fetch_calls == ["f1"]

    @pytest.mark.parametrize(
        "exc", [DriveRetryableError("busy"), DriveTerminalError("gone")]
    )
    def test_a_scripted_fetch_error_is_raised(self, exc):
        stub = StubDriveAdapter(contents={"f1": b"x"}, fetch_errors={"f1": exc})
        with pytest.raises(type(exc)):
            stub.fetch_bytes("f1")

    def test_an_unscripted_file_is_terminal_not_a_KeyError(self):
        with pytest.raises(DriveTerminalError):
            StubDriveAdapter().fetch_bytes("nope")

    def test_a_scripted_list_error_is_raised_on_the_right_call(self):
        stub = StubDriveAdapter(
            pages=[DrivePage(files=_files(1), next_page_token="t")],
            list_errors={1: DriveRetryableError("page 2 failed")},
        )
        stub.list_files("s")  # call 0 succeeds
        with pytest.raises(DriveRetryableError):
            stub.list_files("s", page_token="t")

    def test_lost_response_is_not_a_DriveError(self):
        """ "We do not know" must not be catchable as "it failed"."""
        from src.services.target.drive_adapter import DriveError

        assert not issubclass(DriveLostResponse, DriveError)
        stub = StubDriveAdapter(fetch_errors={"f": DriveLostResponse("timeout")})
        with pytest.raises(DriveLostResponse):
            stub.fetch_bytes("f")


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

    def test_wiring_the_seam_does_not_pretend_the_executor_exists(self):
        """The seam is not the executor, and the two parks must differ.

        W6's executor is astrid's to write; this issue owns only the door it
        calls. So with the seam wired the kind is STILL parked — but under the
        unbuilt reason, not the seam reason. Collapsing the two would tell her
        the work was done, and telling someone their blocker cleared when it
        has not is worse than parking.
        """
        from src.services.target.work_loop import Parked

        wired = self._registry(drive=StubDriveAdapter())["sync_media_source"]
        assert isinstance(wired, Parked), (
            "a wired seam must not un-park a kind whose executor is unwritten"
        )
        assert "drive" not in wired.reason.lower(), (
            "with the seam present the park must stop blaming the seam — "
            f"got {wired.reason!r}"
        )
        assert "no executor exists" in wired.reason

    def test_the_default_page_size_is_a_page_bound_not_a_total(self):
        """Guard against a later 'tidy-up' turning the page bound into a cap."""
        stub = StubDriveAdapter(
            pages=[DrivePage(files=_files(DEFAULT_PAGE_SIZE + 5), next_page_token=None)]
        )
        page = stub.list_files("s")
        assert len(page.files) == DEFAULT_PAGE_SIZE
        assert page.next_page_token is not None, (
            "the default page size silently dropped the remainder"
        )
