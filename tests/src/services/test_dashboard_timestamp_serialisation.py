"""#918 — timestamps leaving the dashboard query layer must carry an offset.

`posting_history.posted_at` and `posting_queue.scheduled_for` are
``timestamp without time zone``. Serialising them with a bare ``.isoformat()``
emits no offset, and ECMA-262 says a date-time form *without* an offset is
parsed as **local time** — so the browser shifted every timestamp by the
viewer's UTC offset and `_formatRelativeTime` rendered already-posted items as
"in 3h".

The contract these tests pin is narrow and checkable: **the serialised string
must denote an instant, not a wall-clock reading.** A consumer in any timezone
must recover the same moment.

The process timezone is pinned per-test rather than inherited. Inheriting it is
what let this survive: on a machine whose timezone happens to match the database
session's, the two errors cancel and the rendered value looks right.
"""

import os
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from src.services.core.dashboard_history_queries import HistoryDashboardQueries
from src.services.core.dashboard_queue_queries import QueueDashboardQueries


# Deliberately spread across the sign of the UTC offset, so a test that passes
# only because the local zone is UTC cannot look green.
TIMEZONES = ["UTC", "America/New_York", "Asia/Tokyo", "America/Los_Angeles"]


@pytest.fixture
def pinned_tz(request):
    """Pin the process timezone for one test, then restore it."""
    tz = request.param
    previous = os.environ.get("TZ")
    os.environ["TZ"] = tz
    time.tzset()
    yield tz
    if previous is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = previous
    time.tzset()


def _history_queries(posted_at_naive):
    service = MagicMock()
    row = MagicMock()
    row.posted_at = posted_at_naive
    row.status = "posted"
    row.posting_method = "telegram_manual"
    service.history_repo.get_all_with_media.return_value = [(row, "hat.png", "merch")]
    return HistoryDashboardQueries(service)


def _queue_queries(scheduled_for_naive):
    service = MagicMock()
    row = MagicMock()
    row.scheduled_for = scheduled_for_naive
    row.status = "pending"
    service.queue_repo.get_all_with_media.return_value = [(row, "hat.png", "merch")]
    service.queue_repo.count_by_status.return_value = 0
    service.history_repo.get_posts_today.return_value = []
    service.history_repo.get_recent_posts.return_value = []
    return QueueDashboardQueries(service)


@pytest.mark.unit
class TestSerialisedTimestampsDenoteAnInstant:
    """A naive column must leave the query layer with an explicit offset."""

    @pytest.mark.parametrize("pinned_tz", TIMEZONES, indirect=True)
    def test_history_posted_at_carries_an_offset(self, pinned_tz):
        naive_utc = datetime(2026, 8, 19, 18, 42, 57)
        out = _history_queries(naive_utc).get_history_detail("tenant-1")
        serialised = out["items"][0]["posted_at"]

        parsed = datetime.fromisoformat(serialised)
        assert parsed.tzinfo is not None, (
            f"{serialised!r} has no offset — a browser parses this as LOCAL time"
        )
        assert parsed == naive_utc.replace(tzinfo=timezone.utc), (
            f"under TZ={pinned_tz} the value moved: {parsed}"
        )

    @pytest.mark.parametrize("pinned_tz", TIMEZONES, indirect=True)
    def test_queue_scheduled_for_carries_an_offset(self, pinned_tz):
        naive_utc = datetime(2026, 8, 19, 20, 15, 0)
        out = _queue_queries(naive_utc).get_queue_detail("tenant-1")
        serialised = out["items"][0]["scheduled_for"]

        parsed = datetime.fromisoformat(serialised)
        assert parsed.tzinfo is not None, f"{serialised!r} has no offset"
        assert parsed == naive_utc.replace(tzinfo=timezone.utc)

    @pytest.mark.parametrize("pinned_tz", TIMEZONES, indirect=True)
    def test_a_past_post_never_reads_as_future(self, pinned_tz):
        """The user-visible symptom, modelled the way the browser produces it.

        ECMA-262: a date-time string *without* an offset is interpreted as local
        time. So this parses the serialised value exactly as `new Date()` would
        — naive means local — and asserts the resulting instant is still in the
        past. Without an offset a post younger than the viewer's UTC offset
        lands in the future and `_formatRelativeTime` renders "in 3h".
        """
        posted_20_min_ago = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            minutes=20
        )
        out = _history_queries(posted_20_min_ago).get_history_detail("tenant-1")
        serialised = out["items"][0]["posted_at"]

        parsed = datetime.fromisoformat(serialised)
        if parsed.tzinfo is None:
            # what a browser does with an offset-less string
            parsed = parsed.astimezone()

        assert parsed < datetime.now(timezone.utc), (
            f"under TZ={pinned_tz}, {serialised!r} read the way a browser reads "
            f"it is {parsed}, which is in the FUTURE for a post made 20 minutes "
            f"ago — this is the #918 symptom"
        )
