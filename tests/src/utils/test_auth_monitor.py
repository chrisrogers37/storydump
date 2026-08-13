"""Tests for authentication failure monitoring and alerting."""

import time
from unittest.mock import patch, MagicMock

import pytest

from src.utils import auth_monitor


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset monitor state before each test."""
    auth_monitor.reset()
    yield
    auth_monitor.reset()


class TestRecordFailure:
    def test_single_failure_logs_warning(self):
        with patch.object(auth_monitor, "logger") as mock_logger:
            auth_monitor.record_failure("192.168.1.1", "Invalid signature")
            mock_logger.warning.assert_called_once()
            args = mock_logger.warning.call_args[0]
            assert "192.168.1.1" in args[2]
            assert "Invalid signature" in args[3]

    def test_failures_below_threshold_no_alert(self):
        with patch.object(auth_monitor, "_send_alert") as mock_alert:
            for _ in range(auth_monitor.FAILURE_THRESHOLD - 1):
                auth_monitor.record_failure("10.0.0.1", "bad token")
            mock_alert.assert_not_called()

    def test_threshold_reached_triggers_alert(self):
        with patch.object(auth_monitor, "_send_alert") as mock_alert:
            for _ in range(auth_monitor.FAILURE_THRESHOLD):
                auth_monitor.record_failure("10.0.0.1", "bad token")
            mock_alert.assert_called_once_with(
                "10.0.0.1", auth_monitor.FAILURE_THRESHOLD
            )

    def test_alert_fires_only_once_at_threshold(self):
        with patch.object(auth_monitor, "_send_alert") as mock_alert:
            for _ in range(auth_monitor.FAILURE_THRESHOLD + 3):
                auth_monitor.record_failure("10.0.0.1", "bad token")
            # Alert fires at exactly the threshold count, not on subsequent failures
            mock_alert.assert_called_once()

    def test_different_sources_tracked_independently(self):
        with patch.object(auth_monitor, "_send_alert") as mock_alert:
            for _ in range(auth_monitor.FAILURE_THRESHOLD - 1):
                auth_monitor.record_failure("10.0.0.1", "bad token")
            for _ in range(auth_monitor.FAILURE_THRESHOLD - 1):
                auth_monitor.record_failure("10.0.0.2", "bad token")
            mock_alert.assert_not_called()

    def test_expired_failures_pruned(self):
        with patch.object(auth_monitor, "_send_alert") as mock_alert:
            # Record old failures outside the window
            old_time = time.time() - auth_monitor.WINDOW_SECONDS - 1
            with patch("src.utils.auth_monitor.time") as mock_time:
                mock_time.time.return_value = old_time
                for _ in range(auth_monitor.FAILURE_THRESHOLD - 1):
                    auth_monitor.record_failure("10.0.0.1", "bad token")

            # Record one more at current time — should not trigger since old ones expired
            auth_monitor.record_failure("10.0.0.1", "bad token")
            mock_alert.assert_not_called()

    def test_reset_clears_all_state(self):
        with patch.object(auth_monitor, "_send_alert") as mock_alert:
            for _ in range(auth_monitor.FAILURE_THRESHOLD - 1):
                auth_monitor.record_failure("10.0.0.1", "bad token")
            auth_monitor.reset()
            # After reset, need full threshold again
            for _ in range(auth_monitor.FAILURE_THRESHOLD - 1):
                auth_monitor.record_failure("10.0.0.1", "bad token")
            mock_alert.assert_not_called()


class TestStaleKeyEviction:
    """#760. Pruning happened only for the key being written, so a source that
    never recurred kept its entry for the life of the process. The map grew
    with the number of *distinct* sources ever seen rather than with the number
    currently active — and it grew precisely in the case that alerts nothing,
    since a source seen once never approaches ``FAILURE_THRESHOLD``.
    """

    def _record_at(self, when, sources, reason="bad token"):
        """Record one failure from each source, with the clock pinned."""
        with patch("src.utils.auth_monitor.time") as mock_time:
            mock_time.time.return_value = when
            for source in sources:
                auth_monitor.record_failure(source, reason)

    def test_keys_whose_failures_all_aged_out_are_evicted(self):
        """THE REGRESSION, and the clock advance is what makes it discriminate:
        without it the same assertions pass against the unfixed code."""
        old = time.time() - auth_monitor.WINDOW_SECONDS - 1
        stale = [f"10.0.0.{i}" for i in range(50)]

        with patch.object(auth_monitor, "_send_alert"):
            self._record_at(old, stale)
            # The setup itself is asserted: if these never landed, a later
            # "the map is small" assertion would pass for the wrong reason.
            assert len(auth_monitor._failures) == 50

            auth_monitor.record_failure("10.9.9.9", "bad token")

        assert "10.9.9.9" in auth_monitor._failures
        assert len(auth_monitor._failures) == 1, sorted(auth_monitor._failures)

    def test_a_source_still_inside_the_window_is_not_evicted(self):
        """The counting window is what the map is FOR. Eviction that dropped a
        live source would silently reset its count and defeat the alert."""
        old = time.time() - auth_monitor.WINDOW_SECONDS - 1

        with patch.object(auth_monitor, "_send_alert"):
            self._record_at(old, [f"10.0.0.{i}" for i in range(10)])
            auth_monitor.record_failure("10.5.5.5", "bad token")
            auth_monitor.record_failure("10.9.9.9", "bad token")

        assert "10.5.5.5" in auth_monitor._failures
        assert "10.9.9.9" in auth_monitor._failures

    def test_eviction_does_not_disarm_an_alert_in_progress(self):
        """A source partway to the threshold must still alert after a sweep has
        run. Dropping it would turn a memory fix into an alerting outage — the
        map exists to fire this alert."""
        old = time.time() - auth_monitor.WINDOW_SECONDS - 1

        with patch.object(auth_monitor, "_send_alert") as mock_alert:
            self._record_at(old, [f"10.0.0.{i}" for i in range(50)])

            for _ in range(auth_monitor.FAILURE_THRESHOLD):
                auth_monitor.record_failure("10.9.9.9", "bad token")

            mock_alert.assert_called_once_with(
                "10.9.9.9", auth_monitor.FAILURE_THRESHOLD
            )


class TestSourceCap:
    """#760. The sweep bounds the steady state but only between its runs, and a
    distributed caller mints keys far faster than the interval. The cap is what
    holds during a burst — every source below is inside the window, so the
    sweep can remove none of them and only the cap can.
    """

    def test_the_map_never_exceeds_the_cap_when_no_source_is_stale(self):
        with (
            patch.object(auth_monitor, "MAX_SOURCES", 10),
            patch.object(auth_monitor, "_send_alert"),
            patch.object(auth_monitor, "logger"),
        ):
            for i in range(50):
                auth_monitor.record_failure(f"10.0.0.{i}", "bad token")

        assert len(auth_monitor._failures) <= 10
        # The source just written is the most recently active, so it can never
        # be its own victim.
        assert "10.0.0.49" in auth_monitor._failures

    def test_eviction_keeps_the_source_closest_to_alerting(self):
        """THE ORDERING, and the two candidate policies invert right here.

        ``10.9.9.9`` has the OLDEST first-seen of any source in the map and the
        NEWEST last-seen. Evicting by first-seen drops it — the one source
        actually accumulating failures — while keeping nine one-shot scanners.
        Evicting by last-seen drops the scanners and keeps it.

        The excess is kept below the number of disposable scanners on purpose.
        Eviction is lossy at the cap by construction: force more evictions than
        there are colder sources and the active one goes too, whatever the
        ordering. That is a property of the ceiling, not of the policy, and
        asserting against it would test something no ordering can deliver.
        """
        base = time.time()
        with (
            patch.object(auth_monitor, "MAX_SOURCES", 10),
            patch.object(auth_monitor, "_send_alert"),
            patch.object(auth_monitor, "logger"),
            patch("src.utils.auth_monitor.time") as mock_time,
        ):
            mock_time.time.return_value = base
            auth_monitor.record_failure("10.9.9.9", "bad token")

            for i in range(9):
                mock_time.time.return_value = base + 1 + i
                auth_monitor.record_failure(f"10.0.0.{i}", "bad token")

            # Still failing: newest last-seen, oldest first-seen.
            mock_time.time.return_value = base + 100
            auth_monitor.record_failure("10.9.9.9", "bad token")

            # Five, against nine disposable scanners — see the docstring.
            for i in range(5):
                mock_time.time.return_value = base + 101 + i
                auth_monitor.record_failure(f"10.1.1.{i}", "bad token")

            # Nothing aged out — everything above is inside WINDOW_SECONDS, so
            # this is the cap's doing and not the sweep's.
            assert 106 < auth_monitor.WINDOW_SECONDS

        assert len(auth_monitor._failures) <= 10
        assert "10.9.9.9" in auth_monitor._failures, (
            "evicted the source closest to FAILURE_THRESHOLD"
        )


class TestSendAlert:
    def test_sends_telegram_message(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()

        with (
            patch(
                "src.utils.auth_monitor.httpx.post", return_value=mock_resp
            ) as mock_post,
            patch("src.utils.auth_monitor.settings") as mock_settings,
        ):
            mock_settings.TELEGRAM_BOT_TOKEN = "test-token"
            mock_settings.ADMIN_TELEGRAM_CHAT_ID = 12345

            auth_monitor._send_alert("10.0.0.1", 5)

            mock_post.assert_called_once()
            call_kwargs = mock_post.call_args
            assert "api.telegram.org" in call_kwargs[0][0]
            assert call_kwargs[1]["json"]["chat_id"] == 12345
            assert "10.0.0.1" in call_kwargs[1]["json"]["text"]
            assert "5 failures" in call_kwargs[1]["json"]["text"]

    def test_alert_failure_logs_error(self):
        import httpx

        with (
            patch(
                "src.utils.auth_monitor.httpx.post",
                side_effect=httpx.ConnectError("down"),
            ),
            patch("src.utils.auth_monitor.settings") as mock_settings,
            patch.object(auth_monitor, "logger") as mock_logger,
        ):
            mock_settings.TELEGRAM_BOT_TOKEN = "test-token"
            mock_settings.ADMIN_TELEGRAM_CHAT_ID = 12345

            auth_monitor._send_alert("10.0.0.1", 5)

            mock_logger.error.assert_called_once()
