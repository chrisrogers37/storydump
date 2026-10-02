"""`media_sync`'s judgment of a listed file, without a database (#1545).

The sync lands a file the publish could never fetch as `unsupported`, judged
from the listing's size against the publish's own cap
(`vocabulary.PUBLISH_MAX_BYTES`). What the rows do with that judgment is the
W6 gate's (`tests/scripts/test_w6_sync_gate.py`).
"""

import pytest

from src.services.target import media_sync, vocabulary


class TestTheListingDecidesWhatTheDrawMayTake:
    @pytest.mark.parametrize("kind", ["image", "video"])
    def test_at_the_cap_the_file_posts_and_past_it_it_never_can(self, kind):
        cap = vocabulary.PUBLISH_MAX_BYTES[kind]
        assert media_sync._listed_state(kind, cap) == "available"
        assert media_sync._listed_state(kind, cap + 1) == "unsupported"

    @pytest.mark.parametrize("kind", ["image", "video"])
    def test_a_listing_that_states_no_size_is_not_judged(self, kind):
        assert media_sync._listed_state(kind, None) == "available"

    def test_the_cap_is_the_publishs_own(self):
        """One spelling: the worker refuses past the same numbers the sync
        judges by, so a cap changed in one place moves both."""
        from src import worker

        assert worker.PUBLISH_MAX_BYTES is vocabulary.PUBLISH_MAX_BYTES
        assert vocabulary.PUBLISH_MAX_BYTES == {
            "image": 8 * 1024 * 1024,
            "video": 40 * 1000 * 1000,
        }
