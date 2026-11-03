"""Pure bundle rules for the past-posts source's per-slide landing."""

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from src.services.target.instagram_catalog import (
    PastPostsAsset,
    PastPostsObservation,
    _bundle_assets,
    _item_state,
    _lacking,
    _validate,
    expected_slide_count,
)


def observation(*ids, complete=True):
    return PastPostsObservation(
        "900",
        datetime(2020, 1, 1, tzinfo=timezone.utc),
        assets=tuple(
            PastPostsAsset(mid, "image", position=i) for i, mid in enumerate(ids, 1)
        ),
        slide_count=len(ids),
        slides_complete=complete,
    )


def previous(*ids):
    """The stored asset rows of slides `ids`, at positions 1, 2, ..."""
    return [
        {
            "asset_media_id": mid,
            "position": i,
            "media_kind": "image",
            "availability": "available",
            "exclusion_reason": None,
            "mime_type": "image/jpeg",
            "file_size": 42,
        }
        for i, mid in enumerate(ids, 1)
    ]


def test_truncated_reorder_keeps_unseen_identities_in_unique_trailing_positions():
    old = previous("101", "102", "103")
    incoming = observation("103", complete=False)
    _validate(incoming)
    merged = _bundle_assets(incoming, old)
    assert [(a.selected_asset_id, a.position) for a in merged] == [
        ("103", 1),
        ("101", 2),
        ("102", 3),
    ]
    assert len({a.position for a in merged}) == len(merged)
    assert all(a.mime_type == "image/jpeg" and a.file_size == 42 for a in merged[1:])
    assert [
        a.selected_asset_id
        for a in _bundle_assets(replace(incoming, slides_complete=True), old)
    ] == ["103"]
    assert [
        (a.selected_asset_id, a.position)
        for a in _bundle_assets(observation(complete=False), old)
    ] == [("101", 1), ("102", 2), ("103", 3)]


def test_a_complete_read_carries_only_the_held_slides_it_lacks_after_its_own():
    incoming = observation("103")
    _validate(incoming)
    merged = _bundle_assets(incoming, previous("101", "102", "103"), held={"102"})
    assert [(a.selected_asset_id, a.position) for a in merged] == [
        ("103", 1),
        ("102", 2),
    ]
    assert merged[1].availability == "available"


def test_a_complete_read_carries_the_unlisted_slides_it_lacks_out_of_the_draw():
    merged = _bundle_assets(
        observation("103"), previous("101", "102", "103"), unlisted={"101"}
    )
    assert [
        (a.selected_asset_id, a.position, a.availability, a.exclusion_reason)
        for a in merged
    ] == [("103", 1, "available", None), ("101", 2, "unavailable", "unlisted")]


def test_a_complete_read_holds_out_a_slide_first_and_retires_it_second():
    """The first complete read that lacks a drawable slide no story holds
    holds it out; the next one retires it. A held slide is neither."""
    stored = previous("101", "102", "103")
    missing = {"101": "item-1", "102": "item-2"}
    assert _lacking(observation("103"), stored, missing, {"102"}) == (
        frozenset({"101"}),
        {},
    )
    stored[0] = {
        **stored[0],
        "availability": "unavailable",
        "exclusion_reason": "unlisted",
    }
    assert _lacking(observation("103"), stored, missing, {"102"}) == (
        frozenset(),
        {"101": "item-1"},
    )


@pytest.mark.parametrize(
    "incoming,availability",
    [(observation("103"), "excluded"), (observation("103"), "available")],
    ids=["excluded-slide", "single-post"],
)
def test_what_is_never_drawn_or_is_the_post_itself_retires_on_one_read(
    incoming, availability
):
    """An excluded slide is never drawn, and a single post's asset is the post
    itself: a complete read that lacks either retires it at once."""
    if availability == "available":
        incoming = replace(incoming, slide_count=None)
    stored = previous("101", "103")
    stored[0] = {**stored[0], "availability": availability}
    assert _lacking(incoming, stored, {"101": "item-1"}, frozenset()) == (
        frozenset(),
        {"101": "item-1"},
    )


@pytest.mark.parametrize(
    "availability,reason,conflict,expected",
    [
        ("available", None, False, "available"),
        ("unavailable", "unlisted", False, "missing"),
        ("unavailable", "media_unavailable", False, "unsupported"),
        ("available", None, True, "unsupported"),
    ],
    ids=["drawable", "unlisted", "unavailable", "identity-conflict"],
)
def test_a_landed_slide_gives_its_item_one_state(
    availability, reason, conflict, expected
):
    asset = PastPostsAsset(
        "101", "image", availability=availability, exclusion_reason=reason
    )
    assert _item_state(asset, conflict) == expected


@pytest.mark.parametrize(
    "count,position,expected",
    [(3, 2, 3), (3, 4, 4), (0, 1, 1), (None, 1, None)],
    ids=["listed", "carried", "metadata-only", "single-post"],
)
def test_a_slide_is_judged_against_the_read_that_listed_it(count, position, expected):
    """A listed slide keeps the count of the read that listed it. A slide
    carried past that count, held after a complete read or unseen after a
    partial one, is judged against the count plus itself. A single post has
    no count."""
    assert expected_slide_count(count, position) == expected


def test_single_post_keeps_parent_identity_and_position_one():
    single = PastPostsObservation(
        "900",
        datetime(2020, 1, 1, tzinfo=timezone.utc),
        assets=(PastPostsAsset("900", "image"),),
    )
    _validate(single)
    assert _bundle_assets(single, ()) == list(single.assets)
