"""Representative Instagram onboarding archives, shaped as the Graph pages them.

The Instagram source gate (#1644) proves onboarding against archives of a
known composition: N posts, a stated share of them usable, a stated share
excluded for each reason, and the first usable one reachable early. Two pure
generators build those archives, and ``expected_counts.json`` states what a run
over them must report.

* ``useful(n, t0)``: 10% recent posts, then the newest old post (a plain image
  the first card can use), then old posts that make 60% of the archive
  ``not_detected`` overall, 20% credited and 10% unknown, interleaved, ending
  in the archive's single 2022 image.
* ``excluded(n, t0)``: 50% recent posts, 30% old credited, 20% old unknown.
  Nothing is a candidate and the leading reason is ``too_recent``.

The generators and the manifest are independent on purpose. Nothing here asks
the suitability evaluator anything, and the manifest is typed by hand, never
produced by running the code it judges.
``tests/test_instagram_onboarding_fixtures.py`` holds the two against each
other. A changed number needs an explanation and a review of the manifest
before a run is accepted.

Pure and deterministic: no clock (``t0`` is the caller's, captured once), no
randomness, no I/O beyond reading the manifest. A ``Post`` carries what the
Graph serves for it: ``parent`` is the listing row (the base listing never asks
for a caption), ``caption`` is what the per-parent enrichment read adds, and
``children`` is the carousel's child page. Every time is ``t0`` minus an age,
except one: the 2022 image has an absolute date, because "dated 2022" is a
calendar fact and not an age.

Ages sit well clear of the default policy's 90-day minimum (the larger of the
saved 90 and the 30-day repost window plus one day): a recent post is at most
80 days old and an old one at least 100, so no post is near the line and a run
that evaluates a few minutes after ``t0`` still sees the same split.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

__all__ = [
    "Archive",
    "IMAGE_2022_AT",
    "OWNER_ID",
    "Post",
    "URL_MARKER",
    "excluded",
    "carousel",
    "expected",
    "expected_counts",
    "useful",
]

DAY = 86_400
#: The id every row's ``owner`` names: by default an account's ``user_id`` as
#: OAuth stores it, so the adapter accepts each owner as it stands. Pass
#: ``owner_id`` to match a seeded account, or an app-scoped id that is not the
#: account's ref: the adapter then proves ownership with the verified id the
#: source's authorization recorded or, with none recorded, through ``/me``: one
#: more Graph request for each turn that reads parents by id (the identity
#: enrichment the traversal ceiling counts).
OWNER_ID = "17841400000000001"
#: Parent ids count up from this, newest post last, as Instagram's roughly do.
MEDIA_ID_BASE = 17_900_000_000_000_000
#: Child ids never collide with a parent's: they count up from here.
CHILD_ID_BASE = 18_000_000_000_000_000
#: Every media URL carries this query, so a test can assert that no durable
#: row ever holds one ("never retained as a durable media URL").
URL_MARKER = "ephemeral=never-persist"
_CDN = "https://scontent-fixture.cdninstagram.com/v/t51.2885-15"

#: Newest and oldest recent post: all inside the 90-day minimum age.
RECENT_NEWEST_DAYS, RECENT_OLDEST_DAYS = 1, 80
#: The newest old post, alone at its second. In ``useful`` it is the accepted
#: image; in ``excluded`` the old posts start here.
OLD_AGE_DAYS = 100
#: The other old posts spread over twelve 90-day windows below the newest one.
OLD_SPAN_DAYS = 1_080
#: The single 2022 post: an absolute mid-year date, so no timezone moves it to
#: another year.
IMAGE_2022_AT = int(datetime(2022, 6, 15, 9, 30, tzinfo=timezone.utc).timestamp())
_START_OF_2023 = int(datetime(2023, 1, 1, tzinfo=timezone.utc).timestamp())

#: Posts sharing one second come in groups of three, two and one, so an
#: overlapping timestamp is common and sometimes straddles a 25-post page:
#: when the recent slice fills two pages, its 25th and 26th posts share one.
_TIE_SIZES = (3, 2, 1)

_ND, _CR, _UN = "not_detected", "credited", "unknown"
#: Six not_detected, two credited and one unknown in every nine old posts, so
#: the shares are exact whenever 0.9n is a multiple of nine. Indexed by position
#: among the old posts: position 0 is the accepted image, and the last position,
#: 0.9n - 1, lands on the ninth entry, so the oldest post (the 2022 image) is
#: not_detected too.
_USEFUL_OLD = (_ND, _ND, _CR, _ND, _ND, _UN, _ND, _CR, _ND)
#: Three credited and two unknown in five.
_EXCLUDED_OLD = (_CR, _CR, _UN, _CR, _UN)

#: By position in the newest-first list: half images, three in ten videos and
#: two in ten carousels. The accepted and 2022 images are forced to images.
_KINDS = (
    "IMAGE",
    "VIDEO",
    "IMAGE",
    "CAROUSEL_ALBUM",
    "IMAGE",
    "VIDEO",
    "IMAGE",
    "CAROUSEL_ALBUM",
    "IMAGE",
    "VIDEO",
)
_CHILDREN = 3

#: Captions with no detected credit. Several carry words or marks a careless
#: classifier would trip on (a mention, "via", "credit") without the
#: ``<keyword> @handle`` shape the credit pattern needs.
_PLAIN = (
    "Golden hour at the pier #{serial}",
    "Sunday reset, slow coffee and good light",
    "Behind the scenes from this week's shoot",
    "Thanks for coming out, @fixture_friend",
    "Route notes: via the old coast road, then north",
    "Credit cards stayed home, cash only at the market",
    "Crème brûlée season",
)
_CREDITED = (
    "Great light. Photo via @fixture_source_{serial}",
    "Credit: @fixture_source_{serial} #repost",
    "cr @fixture_source_{serial} for the clip",
    "CREDITS - @Fixture.Source_{serial}",
)

MANIFEST = Path(__file__).with_name("expected_counts.json")


@dataclass(frozen=True)
class Post:
    """One parent as the Graph serves it."""

    #: ``recent``, ``accepted_image``, ``not_detected``, ``credited`` or
    #: ``unknown``: what the generator built, for a test to check against the
    #: data itself.
    group: str
    #: ``parent["timestamp"]`` as epoch seconds.
    at: int
    #: The listing row: id, media_type, media_url, timestamp, permalink, owner,
    #: counts that may be null or absent, and a thumbnail_url on videos.
    parent: dict
    #: ``None`` or ``""`` for an unknown post. Served only by the enrichment
    #: read, never in the listing.
    caption: Optional[str]
    #: A carousel's child rows, first part first; empty for any other post.
    children: tuple = ()


@dataclass(frozen=True)
class Archive:
    """The posts of one generated archive, with what built them."""

    kind: str
    n: int
    #: The captured clock every age is measured from, epoch seconds.
    t0: int
    owner_id: str
    #: Newest first, never out of timestamp order.
    posts: tuple


def expected_counts() -> dict:
    """The hand-authored manifest, parsed afresh."""
    return json.loads(MANIFEST.read_text())


def expected(kind: str, n: int) -> dict:
    """The manifest's row for ``useful`` or ``excluded`` at *n* posts."""
    if kind == "carousel":
        manifest = MANIFEST.with_name("expected_counts_carousel.json")
        return json.loads(manifest.read_text())[kind][str(n)]
    return expected_counts()[kind][str(n)]


def useful(n: int, t0: int | float | datetime, *, owner_id: str = OWNER_ID) -> Archive:
    """*n* posts, 0.6n of them candidates; see the module docstring.

    Newest first: 0.1n recent posts, the accepted image, then 0.9n - 1 old
    posts on the nine-post cycle, the last of them the 2022 image.
    """
    now = _checked(n, t0, owner_id)
    recent, old = n // 10, n - n // 10
    groups = (
        ["recent"] * recent
        + ["accepted_image"]
        + [_USEFUL_OLD[i % len(_USEFUL_OLD)] for i in range(1, old)]
    )
    times = (
        _spread(recent, now - RECENT_NEWEST_DAYS * DAY, now - RECENT_OLDEST_DAYS * DAY)
        + [now - OLD_AGE_DAYS * DAY]
        + _spread(
            old - 2,
            now - (OLD_AGE_DAYS + 1) * DAY,
            now - (OLD_AGE_DAYS + 1 + OLD_SPAN_DAYS) * DAY,
        )
        + [IMAGE_2022_AT]
    )
    return _archive("useful", n, now, owner_id, groups, times, {recent, n - 1})


def carousel(
    n: int, t0: int | float | datetime, *, owner_id: str = OWNER_ID
) -> Archive:
    """Useful's ages and attribution, with eight carousel positions per ten.

    The independent manifest records the position arithmetic. The first
    carousel is empty, the fifth has a twenty-first child outside the one
    allowed page, and the two pinned images remain singles.
    """
    base = useful(n, t0, owner_id=owner_id)
    posts, ordinal = [], 0
    for index, post in enumerate(base.posts):
        serial = n - index
        is_carousel = index % 10 >= 2 and index not in (n // 10, n - 1)
        kind = "CAROUSEL_ALBUM" if is_carousel else "IMAGE"
        children = []
        if is_carousel:
            count = (2, 3, 5, 10, 20)[ordinal % 5]
            if ordinal == 0:
                count = 0
            elif ordinal == 4:
                count = 21
            for part in range(count):
                video = ordinal % 4 == 3 and part in (0, count // 2)
                stem = f"{_CDN}/heavy{serial}_{part}"
                row = {
                    "id": str(CHILD_ID_BASE + serial * 100 + part),
                    "media_type": "VIDEO" if video else "IMAGE",
                    "media_url": f"{stem}.{'mp4' if video else 'jpg'}?{URL_MARKER}",
                }
                if video:
                    row["thumbnail_url"] = f"{stem}_thumb.jpg?{URL_MARKER}"
                children.append(row)
            ordinal += 1
        parent = _parent(
            serial, kind, post.at, owner_id, _counts(index, serial, not is_carousel)
        )
        posts.append(Post(post.group, post.at, parent, post.caption, tuple(children)))
    return Archive("carousel", n, base.t0, owner_id, tuple(posts))


def excluded(
    n: int, t0: int | float | datetime, *, owner_id: str = OWNER_ID
) -> Archive:
    """*n* posts, none a candidate: half recent, 0.3n old credited, 0.2n old
    unknown. Newest first, the old posts on the five-post cycle."""
    now = _checked(n, t0, owner_id)
    recent, old = n // 2, n - n // 2
    groups = ["recent"] * recent + [
        _EXCLUDED_OLD[i % len(_EXCLUDED_OLD)] for i in range(old)
    ]
    times = _spread(
        recent, now - RECENT_NEWEST_DAYS * DAY, now - RECENT_OLDEST_DAYS * DAY
    ) + _spread(
        old, now - OLD_AGE_DAYS * DAY, now - (OLD_AGE_DAYS + OLD_SPAN_DAYS) * DAY
    )
    return _archive("excluded", n, now, owner_id, groups, times, set())


def _checked(n, t0, owner_id) -> int:
    """The epoch seconds of *t0*, once every argument can build an archive."""
    if type(n) is not int or n < 10 or n % 10:
        raise ValueError(
            "n must be a positive multiple of 10: every share is a whole number"
            " of posts"
        )
    if not re.fullmatch(r"[0-9]{1,64}", owner_id):
        raise ValueError("owner_id must be a decimal Instagram id")
    if isinstance(t0, datetime):
        if t0.tzinfo is None:
            raise ValueError("t0 must be timezone-aware")
        now = int(t0.timestamp())
    else:
        now = int(t0)
    if now - (OLD_AGE_DAYS + 1 + OLD_SPAN_DAYS) * DAY < _START_OF_2023:
        raise ValueError(
            "t0 is too early: the old posts would reach back into 2022, the one"
            " image's year"
        )
    return now


def _spread(count: int, newest: int, oldest: int) -> list:
    """*count* timestamps, newest first, spread evenly from *newest* down to
    *oldest* in tie groups of three, two and one posts."""
    sizes, left = [], count
    while left:
        sizes.append(min(_TIE_SIZES[len(sizes) % len(_TIE_SIZES)], left))
        left -= sizes[-1]
    times = []
    for i, size in enumerate(sizes):
        step = (newest - oldest) * i // (len(sizes) - 1) if len(sizes) > 1 else 0
        times.extend([newest - step] * size)
    return times


def _archive(kind, n, now, owner_id, groups, times, plain) -> Archive:
    """The posts for *groups* and *times*, newest first. Positions in *plain*
    are images with both counts present."""
    posts, carousels = [], 0
    for index, (group, at) in enumerate(zip(groups, times)):
        serial = n - index
        media_type = "IMAGE" if index in plain else _KINDS[index % len(_KINDS)]
        children = ()
        if media_type == "CAROUSEL_ALBUM":
            children = _children(serial, video_first=carousels % 4 == 3)
            carousels += 1
        parent = _parent(
            serial, media_type, at, owner_id, _counts(index, serial, index in plain)
        )
        posts.append(Post(group, at, parent, _caption(group, serial), children))
    return Archive(kind, n, now, owner_id, tuple(posts))


def _parent(serial, media_type, at, owner_id, counts) -> dict:
    video = media_type == "VIDEO"
    row = {
        "id": str(MEDIA_ID_BASE + serial),
        "media_type": media_type,
        "media_url": f"{_CDN}/{serial}.{'mp4' if video else 'jpg'}?{URL_MARKER}",
        "timestamp": datetime.fromtimestamp(at, timezone.utc).isoformat(),
        "permalink": f"https://www.instagram.com/{'reel' if video else 'p'}/Fx{serial:010d}/",
        "owner": {"id": owner_id},
    }
    if video:
        row["thumbnail_url"] = f"{_CDN}/{serial}_thumb.jpg?{URL_MARKER}"
    row.update(counts)
    return row


def _counts(index, serial, complete) -> dict:
    """Both counts on two posts in three; on the rest neither, a null like
    count, a missing comment count or a null one. Counts are nullable and never
    gate selection. Both are present where *complete*."""
    likes, comments = serial * 37 % 4000 + 5, serial * 11 % 150
    shape = 4 if complete else min(index % 12, 4)
    if shape == 0:
        return {}
    if shape == 1:
        return {"like_count": None, "comments_count": comments}
    if shape == 2:
        return {"like_count": likes}
    if shape == 3:
        return {"like_count": likes, "comments_count": None}
    return {"like_count": likes, "comments_count": comments}


def _children(serial, *, video_first) -> tuple:
    rows = []
    for part in range(_CHILDREN):
        video = video_first and part == 0
        stem = f"{_CDN}/c{serial}_{part}"
        row = {
            "id": str(CHILD_ID_BASE + serial * 10 + part),
            "media_type": "VIDEO" if video else "IMAGE",
            "media_url": f"{stem}.{'mp4' if video else 'jpg'}?{URL_MARKER}",
        }
        if video:
            row["thumbnail_url"] = f"{stem}_thumb.jpg?{URL_MARKER}"
        rows.append(row)
    return tuple(rows)


def _caption(group, serial) -> Optional[str]:
    if group == "credited":
        return _CREDITED[serial % len(_CREDITED)].format(serial=serial)
    if group == "unknown":
        return None if serial % 2 else ""
    return _PLAIN[serial % len(_PLAIN)].format(serial=serial)
