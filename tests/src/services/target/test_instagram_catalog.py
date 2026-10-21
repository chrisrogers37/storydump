"""Pure bundle rules for the past-posts source's per-slide landing."""

from dataclasses import replace
from datetime import datetime, timezone

from src.services.target.instagram_catalog import (
    PastPostsAsset,
    PastPostsObservation,
    _bundle_assets,
    _validate,
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


def test_truncated_reorder_keeps_unseen_identities_in_unique_trailing_positions():
    old = [
        {
            "asset_media_id": mid,
            "position": i,
            "media_kind": "image",
            "availability": "available",
            "exclusion_reason": None,
            "mime_type": "image/jpeg",
            "file_size": 42,
        }
        for i, mid in enumerate(("101", "102", "103"), 1)
    ]
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


def test_single_post_keeps_parent_identity_and_position_one():
    single = PastPostsObservation(
        "900",
        datetime(2020, 1, 1, tzinfo=timezone.utc),
        assets=(PastPostsAsset("900", "image"),),
    )
    _validate(single)
    assert _bundle_assets(single, ()) == list(single.assets)
