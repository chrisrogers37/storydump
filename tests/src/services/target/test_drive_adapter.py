"""#982 — what survives of the Drive seam contract, and the park that names it.

`drive_adapter.py` once shipped a scripted stub (`StubDriveAdapter`, `DrivePage`,
`DriveFile`) against a `list_files` protocol no production consumer ever called:
the live door is `google_drive_adapter.list_changes`, and `media_sync` calls
that and nothing else. The stub and the bound-detectability tests around it went
with the tech-debt fold (#1325 audit, TD-B5); the paging contract they asserted
now lives where the paging happens —
`tests/src/services/target/test_google_drive_adapter.py`'s walk and cursor tests.

What is left here is the half of the contract that is genuinely shared:
:func:`validate_source_config`, the module-level refusal every implementation of
the seam must call, and `work_loop`'s park reason, which must go on naming the
seam it is blocked on.
"""

import pytest

from src.services.target.drive_adapter import DriveTerminalError


class TestAShapelessConfigIsRefused:
    """#987 review (astrid): expressible is not the same as required.

    Passing the config mapping fixed the ability to CARRY `folder_ref`. It did
    not add a refusal to ACCEPT its absence — so the wrong-answer-that-looks-
    right did not go away, it moved from *inexpressible* to *omittable*. A
    subfolder source with no `folder_ref` still lists from the drive root and
    still looks correct whether it comes back full or empty. The database
    cannot help: `ck_sources_config_v` validates only that `v` is a number.
    """

    def test_the_guard_is_the_CONTRACT_not_one_doors_private_behaviour(self):
        """The placement is the finding, not the check.

        A refusal living inside one adapter would let a consumer go green
        against an obligation another door never inherited. So it is a
        module-level function every implementation calls, and this test fails
        if someone later inlines it back into an adapter.
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

        The wired seam is a bare object: `build_registry` gates on
        `deps.drive is not None` and calls nothing, so a duck-typed stand-in
        would assert a protocol this test is not about.
        """
        from src.services.target.work_loop import Parked

        wired = self._registry(drive=object())
        unwired = self._registry()

        def live(registry):
            return {k for k, e in registry.items() if not isinstance(e, Parked)}

        gated = live(wired) - live(unwired)
        # `publish_pipeline` is NOT in this set even though its media fetch
        # reads Drive: the fetch is derived from the adapter in `worker.compose`
        # (#1220 step 3), one level above the registry this pins.
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
