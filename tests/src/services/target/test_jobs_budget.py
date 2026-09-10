"""`05:38` row 8 (phase 3a step 3): the retry budget per lane — a backoff
ladder with jitter, and a ceiling of attempts or a deadline."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.services.target import jobs

NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)


class TestBackoff:
    def test_the_ladder_climbs_per_lane_and_the_last_rung_repeats(self):
        no_jitter = lambda: 0.5  # noqa: E731 — the midpoint: rung × 1.0
        assert [
            jobs.backoff_seconds("interactive", n, jitter=no_jitter)
            for n in (1, 2, 3, 4)
        ] == [10, 30, 60, 60]
        assert [
            jobs.backoff_seconds("bulk", n, jitter=no_jitter) for n in (1, 2, 3, 4, 9)
        ] == [60, 300, 900, 3600, 3600]

    def test_jitter_stays_within_twenty_percent(self):
        low = jobs.backoff_seconds("bulk", 2, jitter=lambda: 0.0)
        high = jobs.backoff_seconds("bulk", 2, jitter=lambda: 0.999999)
        assert low == 240 and 359 < high < 360

    def test_an_unknown_lane_takes_the_bulk_ladder(self):
        assert jobs.backoff_seconds("weird", 1, jitter=lambda: 0.5) == 60

    def test_a_random_draw_lands_inside_the_band(self):
        for _ in range(200):
            v = jobs.backoff_seconds("interactive", 1)
            assert 8.0 <= v <= 12.0


class TestTheCeiling:
    def test_attempts_at_the_ceiling_are_exhausted(self):
        assert jobs.budget_exhausted({"attempts": 3, "max_attempts": 3}, now=NOW)
        assert not jobs.budget_exhausted({"attempts": 2, "max_attempts": 3}, now=NOW)

    def test_a_passed_deadline_is_exhausted_whatever_the_attempts(self):
        job = {
            "attempts": 1,
            "max_attempts": 5,
            "deadline_at": NOW - timedelta(seconds=1),
        }
        assert jobs.budget_exhausted(job, now=NOW)
        job["deadline_at"] = NOW + timedelta(minutes=1)
        assert not jobs.budget_exhausted(job, now=NOW)

    def test_no_deadline_and_no_ceiling_never_exhausts(self):
        assert not jobs.budget_exhausted({"attempts": 99, "max_attempts": 0}, now=NOW)
        assert not jobs.budget_exhausted({"attempts": 99}, now=NOW)

    def test_the_lane_budgets_are_the_numbers_05_names(self):
        assert jobs.LANE_BUDGETS == {"interactive": (3, 600), "bulk": (5, 6 * 3600)}
