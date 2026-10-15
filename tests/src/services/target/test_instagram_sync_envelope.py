"""The archive sync's saved checkpoint, and the one validator both its readers use.

`instagram_sync._envelope` resumes from a stored checkpoint and `status`
displays one. Each refuses a malformed envelope by name
(`ArchiveCheckpointRefused`), never with the `TypeError` or `AttributeError` an
unvalidated read would raise, so the mix resource's degrade can catch it. This
pins the shapes through the worker's entry point; the same shapes through
`status`, and the GET that must still answer, are
`tests/scripts/test_destination_readiness_gate.py`.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from src.services.target import instagram_sync
from src.services.target.instagram_archive_adapter import LISTING_CEILING

GENERATION = "11111111-1111-4111-8111-111111111111"
CAPTURE = SimpleNamespace(generation_id=GENERATION)


def saved(**fields):
    """A checkpoint as `_land` writes one, with *fields* replacing whole
    top-level fields of it."""
    return {
        "v": 1,
        "type": "instagram_archive_sync",
        "generation_id": GENERATION,
        "revision": 3,
        "next_lane": "history",
        "next_metadata": "history",
        "recent": {"complete": True, "checkpoint": {"v": 2}},
        "history": {"complete": False, "checkpoint": {"v": 2}},
        "pending_helpers": ["124", "125"],
        "deferred_helpers": ["126"],
        "progress": {"recent": {"last_committed_at": "2026-10-12T10:00:00+00:00"}},
        **fields,
    }


#: Each malformed shape of a checkpoint that names the counted generation.
MALFORMED = {
    "a_null_helper_queue": {"pending_helpers": None},
    "a_null_deferred_queue": {"deferred_helpers": None},
    "a_queue_that_is_a_string": {"pending_helpers": "124"},
    "a_queue_entry_that_is_not_decimal": {"pending_helpers": ["12a"]},
    "a_queue_entry_that_is_not_a_string": {"deferred_helpers": [126]},
    "a_queue_entry_over_64_characters": {"pending_helpers": ["1" * 65]},
    "a_queue_with_a_repeat": {"pending_helpers": ["124", "124"]},
    "a_lane_that_is_not_an_object": {"recent": []},
    "the_other_lane_a_string": {"history": "done"},
    "a_lane_with_an_extra_key": {
        "recent": {"complete": True, "checkpoint": {}, "more": True}
    },
    "a_lane_whose_cursor_is_null": {"recent": {"complete": True, "checkpoint": None}},
    "a_lane_flag_that_is_not_a_boolean": {
        "history": {"complete": "yes", "checkpoint": {}}
    },
    "a_progress_record_that_is_a_list": {"progress": []},
    "a_progress_record_that_is_null": {"progress": None},
    "a_revision_below_zero": {"revision": -1},
    "a_revision_that_is_a_boolean": {"revision": True},
    "a_version_other_than_one": {"v": 2},
    "a_version_that_is_a_boolean": {"v": True},
    "a_next_lane_that_is_unknown": {"next_lane": "sideways"},
    "a_next_metadata_lane_of_helper": {"next_metadata": "helper"},
    "a_scan_counter_past_the_ceiling": {"scan_seen": LISTING_CEILING + 1},
    "a_scan_counter_below_zero": {"scan_seen": -1},
    "a_scan_counter_that_is_a_boolean": {"scan_seen": True},
    "a_field_the_sync_never_writes": {"cursor": "abc"},
}


class TestTheSharedValidator:
    def test_a_well_formed_envelope_is_resumed_as_a_copy(self):
        stored = saved()

        resumed = instagram_sync._envelope(stored, CAPTURE)

        assert resumed == stored
        assert resumed is not stored
        resumed["pending_helpers"].append("999")
        assert stored["pending_helpers"] == ["124", "125"]

    def test_a_summary_and_a_scan_counter_may_ride_along(self):
        stored = saved(summary={"observed": 4}, scan_seen=LISTING_CEILING)

        assert instagram_sync._envelope(stored, CAPTURE) == stored

    @pytest.mark.parametrize("fields", MALFORMED.values(), ids=MALFORMED.keys())
    def test_each_malformed_shape_is_refused_by_name(self, fields):
        with pytest.raises(instagram_sync.ArchiveCheckpointRefused) as refused:
            instagram_sync._envelope(saved(**fields), CAPTURE)

        assert refused.value.reason == "unsupported_archive_sync_checkpoint"

    # Not `type`: it decides which reader a dict reaches. Without it the dict is
    # provider state for the adapter to interpret (`TestAProviderCursor…`).
    @pytest.mark.parametrize("missing", sorted(set(saved()) - {"type"}))
    def test_an_envelope_missing_any_field_is_refused(self, missing):
        stored = saved()
        del stored[missing]

        with pytest.raises(instagram_sync.ArchiveCheckpointRefused):
            instagram_sync._envelope(stored, CAPTURE)

    @pytest.mark.parametrize("stored", [[], "abc", 7, True, ["recent"]])
    def test_a_checkpoint_that_is_not_an_object_is_refused(self, stored):
        with pytest.raises(instagram_sync.ArchiveCheckpointRefused):
            instagram_sync._envelope(stored, CAPTURE)

    def test_validity_is_judged_before_the_generation_is_compared(self):
        """An envelope of another generation is replaced by a fresh one, but a
        malformed one is refused whichever generation it names."""
        other = "22222222-2222-4222-8222-222222222222"

        fresh = instagram_sync._envelope(saved(generation_id=other), CAPTURE)

        assert fresh["generation_id"] == GENERATION
        assert fresh["revision"] == 0 and fresh["pending_helpers"] == []
        with pytest.raises(instagram_sync.ArchiveCheckpointRefused):
            instagram_sync._envelope(
                saved(generation_id=other, pending_helpers=None), CAPTURE
            )

    def test_nothing_stored_is_a_fresh_envelope_that_is_itself_valid(self):
        fresh = instagram_sync._envelope(None, CAPTURE)

        # What the sync starts from is what it can read back.
        assert instagram_sync._envelope(copy.deepcopy(fresh), CAPTURE) == fresh


class TestAProviderCursorIsNotAnEnvelope:
    """The dispatch the validator sits behind: provider state of a known
    version is wrapped for the adapter to interpret, anything else refused."""

    @pytest.mark.parametrize("version", [1, 2])
    def test_a_known_cursor_version_is_wrapped_as_the_history_lane(self, version):
        cursor = {"v": version, "upper": 10, "until": 10, "seen": 4, "more": True}

        wrapped = instagram_sync._envelope(cursor, CAPTURE)

        assert wrapped["history"] == {"checkpoint": cursor, "complete": False}
        assert wrapped["type"] == "instagram_archive_sync"
        assert "scan_seen" not in wrapped

    @pytest.mark.parametrize("cursor", [{}, {"v": 3}, {"v": True}, {"v": "2"}])
    def test_an_unknown_cursor_version_is_refused(self, cursor):
        with pytest.raises(instagram_sync.ArchiveCheckpointRefused):
            instagram_sync._envelope(cursor, CAPTURE)
