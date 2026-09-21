"""`_dbapi.driver_error_is` — "which class", asked once.

Three sites were walking `driver_candidates` by hand (`invitations` twice,
`workspaces` once). The ordering case is the one that matters: a caller
discriminating between two classes must see the FIRST candidate that matches
either, not the first candidate matching the class it happens to test first.
"""

from __future__ import annotations

import pytest

from src.services.target._dbapi import driver_candidates, driver_error_is


class _Orig(Exception):
    """Stand-in for the driver exception SQLAlchemy hangs on `.orig`."""


class _Wrapped(Exception):
    """Stand-in for `DBAPIError`: carries `.orig`, whose `__cause__` is the
    real driver exception one level deeper (the lesson `_dbapi` exists for)."""

    def __init__(self, orig):
        super().__init__("wrapped")
        self.orig = orig


class AlphaError(_Orig):
    pass


class BetaError(_Orig):
    pass


class GammaError(_Orig):
    pass


def _chain(outer, inner=None):
    """`DBAPIError(orig=outer)`, with *inner* as `outer.__cause__`."""
    if inner is not None:
        outer.__cause__ = inner
    return _Wrapped(outer)


@pytest.mark.unit
class TestDriverErrorIs:
    def test_finds_the_driver_exception_on_orig(self):
        alpha = AlphaError("boom")
        assert driver_error_is(_chain(alpha), AlphaError) is alpha

    def test_finds_one_buried_a_level_deeper(self):
        alpha = AlphaError("boom")
        assert driver_error_is(_chain(BetaError("outer"), alpha), AlphaError) is alpha

    def test_no_match_is_none(self):
        assert driver_error_is(_chain(GammaError("x")), AlphaError, BetaError) is None

    def test_nothing_buried_is_none(self):
        assert driver_error_is(Exception("bare"), AlphaError) is None

    def test_a_subclass_matches(self):
        class Sub(AlphaError):
            pass

        sub = Sub("boom")
        assert driver_error_is(_chain(sub), AlphaError) is sub

    def test_the_first_candidate_matching_ANY_class_wins(self):
        """Candidate order, not argument order, is the tie-breaker — which is
        what lets `invitations.accept` ask once and discriminate afterwards."""
        outer, inner = BetaError("outer"), AlphaError("inner")
        found = driver_error_is(_chain(outer, inner), AlphaError, BetaError)
        assert found is outer
        assert driver_candidates(_chain(outer, inner)) == (outer, inner)

    def test_no_classes_matches_nothing(self):
        assert driver_error_is(_chain(AlphaError("x"))) is None
