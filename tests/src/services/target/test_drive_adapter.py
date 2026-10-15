"""#982 — the shared Drive seam contract, and the park that names it.

The live door is `google_drive_adapter.list_changes`, and `media_sync` calls
that and nothing else; the paging contract lives where the paging happens —
`tests/src/services/target/test_google_drive_adapter.py`'s walk and cursor tests.

What is here is the half of the contract that is genuinely shared:
:func:`validate_source_config`, the module-level refusal every implementation of
the seam must call, and `work_loop`'s park reason, which must go on naming the
seam it is blocked on.
"""

import pytest

from src.services.target.drive_adapter import DriveTerminalError


class TestAShapelessConfigIsRefused:
    """#987: expressible is not the same as required.

    Passing the config mapping lets a source CARRY `folder_ref`, but nothing
    in the mapping refuses its ABSENCE: the wrong-answer-that-looks-right is
    *omittable*, not *inexpressible*. A subfolder source with no `folder_ref`
    lists from the drive root and looks correct whether it comes back full or
    empty. The database cannot help: `ck_sources_config_v` validates only that
    `v` is a number.
    """

    def test_the_guard_is_the_CONTRACT_not_one_doors_private_behaviour(self):
        """What is pinned is the placement, not the check.

        A refusal living inside one adapter would let a consumer go green
        against an obligation another door never inherited. So it is a
        module-level function every implementation calls, and this test fails
        if it moves into an adapter.
        """
        from src.services.target import drive_adapter as mod

        assert callable(getattr(mod, "validate_source_config", None)), (
            "the config refusal must be reachable by every implementation, not "
            "private to one of them"
        )
        with pytest.raises(DriveTerminalError):
            mod.validate_source_config({"v": 1})
        mod.validate_source_config({"v": 1, "folder_ref": "f"})  # must not raise

    def test_root_name_stays_optional(self):
        """Guard the guard: over-refusing breaks every source without a subfolder."""
        from src.services.target import drive_adapter as mod

        mod.validate_source_config({"v": 1, "folder_ref": "f"})


class TestTheSeamParksLoudly:
    """The media sync parks behind this seam; the reason must NAME it."""

    def _registry(self, **kw):
        from src.services.target.work_loop import WorkerDeps, build_registry

        return build_registry(WorkerDeps(**kw))

    @pytest.mark.parametrize("kind", ["sync_media_source", "first_ingest_chunk"])
    def test_without_the_seam_the_reason_names_it(self, kind):
        from src.services.target.work_loop import Parked

        entry = self._registry()[kind]
        assert isinstance(entry, Parked)
        assert "media adapters" in entry.reason.lower(), (
            f"{kind} parked without naming the seam it is blocked on — "
            f"got {entry.reason!r}"
        )
        assert "1644" in entry.reason, "the park should name its issue (#1644)"

    def test_wiring_the_seam_un_parks_only_the_kinds_the_seam_gates(self):
        """A wired seam must not pretend an executor exists.

        This form is DIFFERENTIAL — the registry with the seam against the
        registry without it — so it names no kind as built or unbuilt and
        holds whichever executors exist. What it forbids is the seam
        reaching beyond the kinds it gates, in either direction.

        The gated set IS named: it pins the SEAM'S CONTRACT, which changes only
        when someone changes the seam, rather than the BUILD STATE, which
        changes under unrelated work. A new kind arriving behind this door
        should fail here.

        The wired seam is a bare object: `build_registry` gates on
        `deps.adapters` and calls nothing, so a duck-typed stand-in
        would assert a protocol this test is not about.
        """
        from src.services.target.work_loop import Parked

        wired = self._registry(adapters={"gdrive": object()})
        unwired = self._registry()

        def live(registry):
            return {k for k, e in registry.items() if not isinstance(e, Parked)}

        gated = live(wired) - live(unwired)
        # `publish_pipeline` is NOT in this set even though its media fetch
        # reads Drive: the fetch is derived from the adapter in `worker.compose`
        # (#1220), one level above the registry this pins.
        assert gated == {"sync_media_source", "first_ingest_chunk"}, (
            f"wiring the drive seam moved kinds it does not gate — got {sorted(gated)}"
        )
        assert not live(unwired) - live(wired), (
            "removing the drive seam left MORE kinds live, which is backwards"
        )

        # The other half: with the seam wired, no parked kind blames it,
        # whichever kind that is. Its positive control is the test above:
        # those reasons demonstrably DO exist while the seam is absent.
        blaming = {
            k: e.reason
            for k, e in wired.items()
            if isinstance(e, Parked) and "drive" in e.reason.lower()
        }
        assert not blaming, (
            f"the seam is wired and these kinds still blame it: {blaming}"
        )


class TestOnlyAWalkThatSaysWholeIsJudged:
    """#1545: the sync tombstones what a walk did not list, so the one fact it
    acts on is positive: an adapter that does not say `whole` (a fake, an
    adapter that predates the key, a lister that returns only changes) reads
    as partial, and nothing is judged."""

    def test_a_completed_walk_that_says_whole_is_whole(self):
        from src.services.target.drive_adapter import walk_saw_whole_tree

        assert walk_saw_whole_tree({"v": 2, "walk": "w", "whole": True}) is True

    @pytest.mark.parametrize(
        "checkpoint",
        [
            None,
            {},
            {"v": 2, "walk": "w"},
            {"v": 2, "walk": "w", "whole": "yes"},
            {"v": 2, "walk": "w", "whole": True, "truncated": True},
            {"v": 2, "walk": "w", "whole": True, "partial": True},
            {"v": 2, "walk": "w", "whole": True, "page_token": "p2"},
        ],
        ids=[
            "none",
            "empty",
            "silent",
            "not-true",
            "truncated",
            "partial",
            "unfinished",
        ],
    )
    def test_anything_else_is_not(self, checkpoint):
        from src.services.target.drive_adapter import walk_saw_whole_tree

        assert walk_saw_whole_tree(checkpoint) is False
