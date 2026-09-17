"""`ensure_utc` and `naive_utc` — one convention with two boundaries (#909).

This file held the #909 population gate too: a static scan of every `Compare`
under `src/` whose one side was a `Column(DateTime)` declared in `src/models/`,
asserting no aware datetime reached a naive column. Its whole subject — the
legacy ORM models and the repositories that compared against them — went with
the legacy tier (the tear-out, phase 01; #1216): the scan found 0 columns and
0 sites on the tree that remained, so a gate over that population would have
been green for the wrong reason, and it was retired with the models. The
target tier's timestamps are `timestamptz` and its SQL binds through asyncpg;
the hazard has no shape to take there. The two helpers the fix introduced are
still the convention at every Python boundary, and their mirror tests stay.
"""

from __future__ import annotations


class TestTheTwoHelpersAreMirrors:
    """`ensure_utc` and `naive_utc` are one convention with two boundaries.

    Pinned because the obvious mistake is reaching for `ensure_utc` at the SQL
    boundary — #909's own issue suggested it — where it is a no-op on an
    already-aware value and leaves the defect in place.
    """

    def test_ensure_utc_is_a_noop_on_an_aware_value_which_is_why_it_did_not_fit(
        self,
    ):
        from datetime import datetime, timezone

        from src.utils.datetime_utils import ensure_utc

        aware = datetime.now(timezone.utc)
        assert ensure_utc(aware) is aware, (
            "ensure_utc changed an aware value — then the #909 reasoning about"
            " why it does not fit the SQL boundary would need revisiting"
        )

    def test_naive_utc_strips_tzinfo_after_converting_not_before(self):
        """The trap in the mirror: dropping tzinfo from a non-UTC offset
        without converting shifts the value silently — the same failure the
        helper exists to prevent, one layer down."""
        from datetime import datetime, timedelta, timezone

        from src.utils.datetime_utils import naive_utc

        offset = timezone(timedelta(hours=-5))
        aware = datetime(2026, 1, 1, 12, 0, tzinfo=offset)
        got = naive_utc(aware)
        assert got.tzinfo is None
        assert got == datetime(2026, 1, 1, 17, 0), (
            f"got {got} — tzinfo was dropped without converting to UTC, which"
            " shifts the value by the offset"
        )

    def test_naive_utc_passes_a_naive_value_through_so_it_is_idempotent(self):
        from datetime import datetime

        from src.utils.datetime_utils import naive_utc

        naive = datetime(2026, 1, 1, 12, 0)
        assert naive_utc(naive) is naive
        assert naive_utc(naive_utc(naive)) is naive

    def test_both_helpers_return_none_unchanged(self):
        from src.utils.datetime_utils import ensure_utc, naive_utc

        assert ensure_utc(None) is None
        assert naive_utc(None) is None
