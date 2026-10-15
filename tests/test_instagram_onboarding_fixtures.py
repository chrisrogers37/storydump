"""The onboarding fixtures hold their hand-authored manifest by construction.

No database, no network, and nothing here asks the suitability evaluator
anything. Counts are read off the generated data itself (a post's age and the
adapter's own caption classifier, in a fixed order of reasons) and compared
with `expected_counts.json`, whose numbers are separately held to their
formulas. A last group feeds the generated pages through the real archive
adapter over a recording transport, so the adapter meets the shapes it consumes.
"""

import collections
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx
import pytest

from src.services.target import instagram_archive_adapter as archive
from src.services.target.egress import EgressPolicy
from tests.fixtures import instagram_onboarding as fx

SIZES = (100, 500, 2000, 10000)
KINDS = ("useful", "excluded")
DAY = 86_400
T0 = int(datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc).timestamp())
START_OF_2023 = int(datetime(2023, 1, 1, tzinfo=timezone.utc).timestamp())
#: The earliest t0 either generator accepts: its oldest regular post then lands
#: on the first second of 2023, so only the pinned image is dated 2022.
EARLIEST_T0 = START_OF_2023 + (fx.OLD_AGE_DAYS + 1 + fx.OLD_SPAN_DAYS) * DAY
MINIMUM_AGE_DAYS = fx.expected_counts()["settings"]["effective_minimum_age_days"]
#: What the adapter's own caption classifier must answer for each group.
CAPTION_STATE = {
    "recent": "not_detected",
    "accepted_image": "not_detected",
    "not_detected": "not_detected",
    "credited": "credited",
    "unknown": "unknown",
}
_PERMALINK = re.compile(
    r"https://(?:www\.)?instagram\.com/(?:p|reel|tv)/[A-Za-z0-9_-]+/?"
)

_built = {}


def build(kind, n):
    """Each archive once: they are never mutated."""
    if (kind, n) not in _built:
        _built[kind, n] = {"useful": fx.useful, "excluded": fx.excluded}[kind](n, T0)
    return _built[kind, n]


def age(post):
    return T0 - post.at


def conditions(post):
    """Which exclusions the post's own data invites: too young, credited and
    unknown attribution. They are disjoint, one primary reason each."""
    state = archive.attribution(post.caption)[0]
    return age(post) < MINIMUM_AGE_DAYS * DAY, state == "credited", state == "unknown"


def reason(post):
    """The primary reason, in the order above; None for a candidate."""
    young, credited, unknown = conditions(post)
    if young:
        return "too_recent"
    if credited:
        return "credited_excluded"
    return "attribution_unknown" if unknown else None


def year(post):
    return datetime.fromtimestamp(post.at, timezone.utc).year


def straddling_ties(a, size=25):
    """Tie groups the adapter's pages split: within each 90-day window, newest
    first, a page of *size* posts ends on the second the next page begins."""
    windows = collections.defaultdict(list)
    for post in a.posts:
        windows[(a.t0 - post.at) // archive.WINDOW_SECONDS].append(post.at)
    return sum(
        times[i - 1] == times[i]
        for times in windows.values()
        for i in range(size, len(times), size)
    )


# --- composition against the manifest -----------------------------------------


@pytest.mark.parametrize("n", SIZES)
@pytest.mark.parametrize("kind", KINDS)
def test_groups_candidates_and_reasons_match_the_manifest(kind, n):
    a, want = build(kind, n), fx.expected(kind, n)

    assert len(a.posts) == a.n == n
    assert want["observed"] == want["represented"] == n
    assert dict(collections.Counter(p.group for p in a.posts)) == want["groups"]
    reasons = collections.Counter(
        reason(p) for p in a.posts for _ in range(len(p.children) or 1)
    )
    assert reasons.pop(None, 0) == want["candidates"]
    assert dict(reasons) == want["candidate_exclusions"]
    # Disjoint: a post invites at most one reason, and the generator's own
    # label names the one the data shows.
    for post in a.posts:
        invited = conditions(post)
        assert sum(invited) <= 1, post.parent["id"]
        label = {0: "recent", 1: "credited", 2: "unknown"}
        shown = label[invited.index(True)] if any(invited) else None
        assert post.group == shown or (
            shown is None and post.group in ("accepted_image", "not_detected")
        ), post.parent["id"]


@pytest.mark.parametrize("n", (10, 30, 70, 110, 990))
def test_other_multiples_of_ten_keep_the_shares_and_the_two_pinned_images(n):
    """The generators are not tuned to the four manifest sizes: any multiple of
    ten splits into whole shares, and the accepted and 2022 images stay images
    whatever the position-based kind cycle would have given them."""
    a, b = fx.useful(n, T0), fx.excluded(n, T0)

    assert collections.Counter(p.group for p in a.posts) == {
        "recent": n // 10,
        "accepted_image": 1,
        "not_detected": 6 * n // 10 - 1,
        "credited": n // 5,
        "unknown": n // 10,
    }
    assert collections.Counter(p.group for p in b.posts) == {
        "recent": n // 2,
        "credited": 3 * n // 10,
        "unknown": n // 5,
    }
    accepted, oldest = a.posts[n // 10], a.posts[-1]
    assert accepted.group == "accepted_image" and oldest.at == fx.IMAGE_2022_AT
    assert accepted.parent["media_type"] == oldest.parent["media_type"] == "IMAGE"
    for archive_ in (a, b):
        times = [p.at for p in archive_.posts]
        assert times == sorted(times, reverse=True)


@pytest.mark.parametrize("n", SIZES)
def test_the_excluded_companion_has_no_candidate_and_leads_with_too_recent(n):
    a = build("excluded", n)
    want = fx.expected("excluded", n)

    assert all(reason(p) is not None for p in a.posts)
    totals = collections.Counter(
        reason(p) for p in a.posts for _ in range(len(p.children) or 1)
    )
    assert min(totals, key=lambda name: (-totals[name], name)) == "too_recent"
    assert want["destination"]["leading_exclusion"] == {
        "reason": "too_recent",
        "count": totals["too_recent"],
    }
    assert "accepted_image" not in {p.group for p in a.posts}
    assert min(year(p) for p in a.posts) >= 2023


@pytest.mark.parametrize("n", SIZES)
def test_the_useful_leading_exclusion_is_the_largest_group(n):
    totals = collections.Counter(
        reason(p) for p in build("useful", n).posts for _ in range(len(p.children) or 1)
    )
    leading = min(
        (name for name in totals if name), key=lambda name: (-totals[name], name)
    )
    assert fx.expected("useful", n)["destination"]["leading_exclusion"] == {
        "reason": leading,
        "count": totals[leading],
    }


# --- order, ages and the two pinned images ------------------------------------


@pytest.mark.parametrize("n", SIZES)
def test_useful_is_newest_first_with_the_accepted_image_right_after_the_recent_slice(
    n,
):
    a = build("useful", n)
    recent = n // 10
    times = [p.at for p in a.posts]

    assert times == sorted(times, reverse=True)
    assert [p.group for p in a.posts[:recent]] == ["recent"] * recent
    accepted = a.posts[recent]
    assert accepted.group == "accepted_image"
    assert accepted.parent["media_type"] == "IMAGE" and accepted.children == ()
    assert archive.attribution(accepted.caption) == ("not_detected", [])
    assert age(accepted) == 100 * DAY
    # Strictly between the recent slice and everything older, and alone at its
    # second, so it is the unambiguous newest old post.
    assert times.count(accepted.at) == 1
    assert min(p.at for p in a.posts[:recent]) > accepted.at
    assert accepted.at > max(p.at for p in a.posts[recent + 1 :])
    # Every other group sits clear of the 90-day line.
    assert (
        min(age(p) for p in a.posts[:recent]),
        max(age(p) for p in a.posts[:recent]),
    ) == (1 * DAY, 80 * DAY)
    assert min(age(p) for p in a.posts[recent:]) == 100 * DAY


@pytest.mark.parametrize("n", SIZES)
def test_the_accepted_image_leads_the_window_below_the_recent_one(n):
    """The first card needs the recent window's first page, then the first
    page of the window below it, which the accepted image tops. That must not
    observe more parents than the manifest's first-card ceiling: at 100 posts
    a first old window holding 25 or more would already break it."""
    a = build("useful", n)
    windows = collections.defaultdict(list)
    for post in a.posts:
        windows[(a.t0 - post.at) // archive.WINDOW_SECONDS].append(post)
    accepted = a.posts[n // 10]

    assert accepted in windows[1] and accepted.at == max(p.at for p in windows[1])
    listed = min(25, len(windows[0])) + min(25, len(windows[1]))
    assert listed <= fx.expected("useful", n)["first_card"]["max_observed"]


@pytest.mark.parametrize("n", SIZES)
def test_excluded_is_newest_first_and_clear_of_the_age_line(n):
    a = build("excluded", n)
    recent = n // 2
    times = [p.at for p in a.posts]

    assert times == sorted(times, reverse=True)
    assert [p.group for p in a.posts[:recent]] == ["recent"] * recent
    assert (
        min(age(p) for p in a.posts[:recent]),
        max(age(p) for p in a.posts[:recent]),
    ) == (1 * DAY, 80 * DAY)
    assert min(age(p) for p in a.posts[recent:]) == 100 * DAY


@pytest.mark.parametrize("n", SIZES)
def test_exactly_one_post_is_dated_2022_and_it_is_the_oldest_image(n):
    a = build("useful", n)
    oldest = a.posts[-1]
    years = collections.Counter(year(p) for p in a.posts)

    assert years[2022] == 1 and min(years) == 2022
    assert year(oldest) == 2022 and oldest.at == fx.IMAGE_2022_AT
    assert oldest.parent["timestamp"] == fx.expected("useful", n)["oldest_observed_at"]
    assert oldest.parent["media_type"] == "IMAGE" and oldest.children == ()
    assert oldest.group == "not_detected"
    assert [p.at for p in a.posts].count(oldest.at) == 1


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("n", SIZES)
def test_timestamps_repeat_in_groups_of_up_to_three(kind, n):
    a = build(kind, n)
    sizes = collections.Counter(collections.Counter(p.at for p in a.posts).values())
    recent = n // 10 if kind == "useful" else n // 2
    in_slice = collections.Counter(p.at for p in a.posts[:recent])
    in_old = collections.Counter(p.at for p in a.posts[recent:])

    assert set(sizes) == {1, 2, 3}
    assert max(in_slice.values()) > 1 and max(in_old.values()) > 1
    # A recent slice that fills two 25-post pages has a tie group across them.
    if recent > 25:
        assert straddling_ties(a) > 0, "no tie group falls across a page boundary"


# --- kinds, counts and captions -----------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("n", SIZES)
def test_every_kind_and_both_first_child_types_appear(kind, n):
    a = build(kind, n)
    carousels = [p for p in a.posts if p.parent["media_type"] == "CAROUSEL_ALBUM"]

    assert {p.parent["media_type"] for p in a.posts} == {
        "IMAGE",
        "VIDEO",
        "CAROUSEL_ALBUM",
    }
    assert {p.children[0]["media_type"] for p in carousels} == {"IMAGE", "VIDEO"}
    assert all(len(p.children) == 3 for p in carousels)
    assert all(
        p.children == () for p in a.posts if p.parent["media_type"] != "CAROUSEL_ALBUM"
    )
    # A video also answers a thumbnail, as the preview reads it.
    assert all(
        ("thumbnail_url" in p.parent) == (p.parent["media_type"] == "VIDEO")
        for p in a.posts
    )


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("n", SIZES)
def test_counts_are_present_null_or_absent(kind, n):
    def form(parent, key):
        if key not in parent:
            return "absent"
        return "null" if parent[key] is None else type(parent[key]).__name__

    posts = build(kind, n).posts
    assert {form(p.parent, "like_count") for p in posts} == {"absent", "null", "int"}
    assert {form(p.parent, "comments_count") for p in posts} == {
        "absent",
        "null",
        "int",
    }


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("n", SIZES)
def test_captions_classify_as_built_by_the_adapters_own_classifier(kind, n):
    posts = build(kind, n).posts

    for post in posts:
        state, handles = archive.attribution(post.caption)
        assert state == CAPTION_STATE[post.group], post.parent["id"]
        assert bool(handles) == (post.group == "credited"), post.parent["id"]
        # The base listing never asks for a caption.
        assert "caption" not in post.parent
    unknown = [p.caption for p in posts if p.group == "unknown"]
    assert None in unknown and "" in unknown


# --- Graph shape --------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("n", SIZES)
def test_rows_are_the_graph_shape_the_adapter_reads(kind, n):
    a = build(kind, n)
    parent_ids = [p.parent["id"] for p in a.posts]
    child_ids = [c["id"] for p in a.posts for c in p.children]

    assert len(set(parent_ids)) == n
    assert len(set(child_ids)) == len(child_ids)
    assert not set(parent_ids) & set(child_ids)
    for post in a.posts:
        row = post.parent
        assert archive._media_id(row["id"]) == row["id"]
        assert archive._media_type(row["media_type"]) == row["media_type"]
        assert archive._timestamp(row["timestamp"]) == post.at
        # The offset keeps its colon: Python 3.10's fromisoformat (CI) reads no
        # other form, though 3.11 and later also accept "+0000".
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00", row["timestamp"])
        assert row["owner"] == {"id": a.owner_id}
        assert _PERMALINK.fullmatch(row["permalink"])
        for url in (
            row["media_url"],
            row.get("thumbnail_url", row["media_url"]),
            *(c["media_url"] for c in post.children),
        ):
            parts = urlsplit(url)
            assert parts.scheme == "https" and parts.port is None
            assert parts.hostname.endswith(".cdninstagram.com")
            assert fx.URL_MARKER in parts.query
        for child in post.children:
            assert archive._media_id(child["id"]) == child["id"]
            assert child["media_type"] in ("IMAGE", "VIDEO")


# --- determinism and arguments ------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_generators_are_deterministic_and_move_with_t0(kind):
    make = {"useful": fx.useful, "excluded": fx.excluded}[kind]
    first, again = make(100, T0), make(100, T0)
    later = make(100, T0 + DAY)

    assert first == again
    assert make(100, datetime.fromtimestamp(T0, timezone.utc)) == first
    for before, after in zip(first.posts, later.posts):
        pinned = before.at == fx.IMAGE_2022_AT
        assert after.at == (before.at if pinned else before.at + DAY)
        assert (after.group, after.caption, after.children) == (
            before.group,
            before.caption,
            before.children,
        )
        assert after.parent["id"] == before.parent["id"]
        assert after.parent["media_type"] == before.parent["media_type"]
    assert first.t0 == T0 and later.t0 == T0 + DAY


@pytest.mark.parametrize("kind", KINDS)
def test_arguments_the_shares_or_the_year_cannot_honor_are_refused(kind):
    make = {"useful": fx.useful, "excluded": fx.excluded}[kind]

    for n in (0, 5, 105, 100.0, True, None):
        with pytest.raises(ValueError, match="multiple of 10"):
            make(n, T0)
    with pytest.raises(ValueError, match="decimal"):
        make(100, T0, owner_id="not-a-number")
    with pytest.raises(ValueError, match="timezone-aware"):
        make(100, datetime(2026, 10, 12, 12, 0))
    regular = [p for p in make(10, EARLIEST_T0).posts if p.at != fx.IMAGE_2022_AT]
    assert min(p.at for p in regular) >= START_OF_2023
    with pytest.raises(ValueError, match="too early"):
        make(10, EARLIEST_T0 - 1)
    custom = make(100, T0, owner_id="17841400000000042")
    assert {p.parent["owner"]["id"] for p in custom.posts} == {"17841400000000042"}


# --- the manifest itself ------------------------------------------------------


def test_the_manifest_covers_every_size_with_one_shape_per_kind():
    manifest = fx.expected_counts()

    assert manifest["schema"] == 1
    assert manifest["settings"] == {
        "page_size": 25,
        "repost_window_days": 30,
        "effective_minimum_age_days": 90,
        "daily_slots": 3,
        "share": 1,
    }
    for kind in KINDS:
        assert set(manifest[kind]) == {str(n) for n in SIZES}
        assert len({frozenset(row) for row in manifest[kind].values()}) == 1


@pytest.mark.parametrize("n", SIZES)
@pytest.mark.parametrize("kind", KINDS)
def test_the_manifest_numbers_follow_the_formulas(kind, n):
    row = fx.expected(kind, n)
    candidates = 84 * n // 100 if kind == "useful" else 0
    exclusions = (
        {
            "too_recent": 14 * n // 100,
            "credited_excluded": 28 * n // 100,
            "attribution_unknown": 14 * n // 100,
        }
        if kind == "useful"
        else {
            "too_recent": 70 * n // 100,
            "credited_excluded": 40 * n // 100,
            "attribution_unknown": 30 * n // 100,
        }
    )

    assert row["candidates"] == candidates
    assert row["candidate_exclusions"] == exclusions
    assert row["eligible_before_selection"] == 0
    # Whole days at three a day, rounded down, as content_runway.days_left does.
    assert row["runway_days"] == candidates // 3
    # Metadata traversal: 3N + 64 Graph admissions, N + 128 turns, no media I/O.
    assert row["traversal"] == {
        "max_graph": 3 * n + 64,
        "max_turns": n + 128,
        "max_cdn": 0,
        "max_downloads": 0,
        "max_decodes": 0,
    }
    if kind == "useful":
        # ceil(0.1N / 25) + 16 Graph admissions, four CDN, a quarter observed.
        assert row["first_card"] == {
            "max_graph": -(-(n // 10) // 25) + 16,
            "max_cdn": 4,
            "max_observed": n // 4,
        }
        assert row["destination"]["state"] == "ready"
        assert row["destination"]["remedy"] is None
        assert row["oldest_observed_at"] == "2022-06-15T09:30:00+00:00"
    else:
        assert "first_card" not in row
        assert (row["cards"], row["publish_attempts"]) == (0, 0)
        assert row["destination"]["state"] == "zero_eligible"
        assert row["destination"]["remedy"] == "review_policy"


def test_the_manifest_matches_the_worked_table():
    """The worked table, typed out: a typo in a size's row cannot hide behind
    a formula that is itself mistaken, nor the reverse."""
    figures = {
        # N: (first-card Graph, first-card observed, traversal Graph, turns, runway)
        100: (17, 25, 364, 228, 28),
        500: (18, 125, 1564, 628, 140),
        2000: (24, 500, 6064, 2128, 560),
        10000: (56, 2500, 30064, 10128, 2800),
    }
    for n, (graph, observed, traversal, turns, runway) in figures.items():
        row = fx.expected("useful", n)
        assert (
            row["first_card"]["max_graph"],
            row["first_card"]["max_observed"],
            row["traversal"]["max_graph"],
            row["traversal"]["max_turns"],
            row["runway_days"],
        ) == (graph, observed, traversal, turns, runway)


@pytest.mark.parametrize("n", SIZES)
@pytest.mark.parametrize("kind", KINDS)
def test_partial_coverage_is_true_exactly_where_the_scan_reaches_the_listing_ceiling(
    kind, n
):
    row = fx.expected(kind, n)

    assert row["partial_coverage"] is (n >= archive.LISTING_CEILING)
    basis = row["partial_coverage_basis"]
    if n == archive.LISTING_CEILING and kind == "useful":
        text = " ".join(basis)
        for cited in (
            "instagram_archive_adapter.py:42",
            "instagram_sync.py:693-696",
            "instagram_sync.py:419-425",
            "destination_readiness.py:109-116",
        ):
            assert cited in text
    else:
        assert isinstance(basis, str) and basis


# --- the real adapter consumes the pages --------------------------------------

SOURCE = "22222222-2222-2222-2222-222222222222"
WS = "11111111-1111-1111-1111-111111111111"


class _Admission:
    async def before_dispatch(self):
        return object()

    async def after_return(self, ticket):
        pass


def _graph(a, profile=None):
    """A recording Graph over the archive: the listing by time window and
    offset cursor (with no ``since``, the walk's one-row probe for the newest
    post below ``until``), a parent read that adds the caption when asked, the
    child page and, given *profile*, the token's own ``/me``. Nothing else is
    reachable."""
    by_id = {p.parent["id"]: p for p in a.posts}

    def handler(request):
        assert request.url.host == "graph.instagram.com"
        params, path = request.url.params, request.url.path
        if path.endswith("/media"):
            low, high = int(params.get("since", "0")), int(params["until"])
            rows = [p for p in a.posts if low <= p.at < high]
            offset, size = int(params.get("after", "0")), int(params["limit"])
            body = {"data": [p.parent for p in rows[offset : offset + size]]}
            if offset + size < len(rows):
                body["paging"] = {
                    "next": "https://ignored.example/never-followed",
                    "cursors": {"after": str(offset + size)},
                }
            return httpx.Response(200, json=body)
        if path.endswith("/children"):
            post = by_id[path.split("/")[-2]]
            return httpx.Response(
                200, json={"data": list(post.children)[: int(params["limit"])]}
            )
        if path.endswith("/me"):
            return httpx.Response(200, json=profile)
        post = by_id[path.rsplit("/", 1)[1]]
        body = dict(post.parent)
        if "caption" in params["fields"].split(","):
            body["caption"] = post.caption
        return httpx.Response(200, json=body)

    return handler


def _adapter(a, client, *, account_ref=None):
    async def access(source_id, *, workspace_id):
        return archive.ArchiveAccess(account_ref or a.owner_id, "fixture-token")

    async def admission(source_id, *, workspace_id, access):
        return _Admission()

    return archive.InstagramArchiveAdapter(
        access_provider=access,
        admission_provider=admission,
        client=client,
        graph_version="v26.0",
        page_size=25,
        clock=lambda: a.t0,
        policy=EgressPolicy().without(enforce_private_address_block=False),
        metadata_only=True,
    )


async def _walk(a, *, to_the_end=False):
    """The adapter's metadata listing over *a*, one 25-post page a turn: every
    observation and the last checkpoint, stopping once every parent is seen or,
    with *to_the_end*, when the scan itself says it has no more."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(_graph(a))) as client:
        adapter = _adapter(a, client)
        checkpoint, seen = None, []
        for _ in range(1000):
            page = await adapter.list_changes(
                {}, checkpoint, source_id=SOURCE, workspace_id=WS
            )
            seen.extend(page.observations)
            checkpoint = page.checkpoint
            if not checkpoint["more"] or (len(seen) == a.n and not to_the_end):
                break
    return seen, checkpoint


@pytest.mark.parametrize("n", (100, 500))
@pytest.mark.parametrize("kind", KINDS)
async def test_the_adapter_lists_every_parent_once_newest_first(kind, n):
    a = build(kind, n)
    seen, _ = await _walk(a)

    assert [o.parent_media_id for o in seen] == [p.parent["id"] for p in a.posts]
    for post, observed in zip(a.posts, seen):
        row = post.parent
        assert observed.original_at == datetime.fromtimestamp(post.at, timezone.utc)
        assert observed.like_count == row.get("like_count")
        assert observed.comments_count == row.get("comments_count")
        # The listing carries no caption, so attribution waits for enrichment;
        # a carousel's first part waits for it too.
        assert observed.attribution == "unknown"
        assert (not observed.assets) == (row["media_type"] == "CAROUSEL_ALBUM")


@pytest.mark.parametrize("n", (2000, 10000))
async def test_only_the_ten_thousand_archive_reaches_the_listing_ceiling(n):
    """The premise of the manifest's partial_coverage: the 10,000 archive is
    observed in full and still ends at the ceiling, the 2,000 one ends clear."""
    seen, checkpoint = await _walk(build("useful", n), to_the_end=True)

    assert len(seen) == n
    assert checkpoint["more"] is False
    assert checkpoint["ceiling_reached"] is (n >= archive.LISTING_CEILING)


@pytest.mark.parametrize("kind", KINDS)
async def test_the_adapter_enriches_each_group_and_kind_as_built(kind):
    a = build(kind, 100)
    samples = {}
    for post in a.posts:
        samples.setdefault((post.group, post.parent["media_type"]), post)
    kinds = {"IMAGE": "image", "VIDEO": "video"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(_graph(a))) as client:
        adapter = _adapter(a, client)
        for (group, media_type), post in samples.items():
            page = await adapter.enrich_parents(
                [post.parent["id"]], source_id=SOURCE, workspace_id=WS
            )
            (observed,) = page.observations
            parts = post.children or (post.parent,)
            assert observed.attribution == CAPTION_STATE[group], (group, media_type)
            assert [a.media_kind for a in observed.assets] == [
                kinds[p["media_type"]] for p in parts
            ]
            assert [a.selected_asset_id for a in observed.assets] == [
                p["id"] for p in parts
            ]
            assert observed.slide_count == (
                len(post.children) if post.children else None
            )
    assert {m for _, m in samples} == {"IMAGE", "VIDEO", "CAROUSEL_ALBUM"}


@pytest.mark.parametrize("app_scoped, requests", [(False, 2), (True, 3)])
async def test_an_app_scoped_owner_costs_a_me_read_when_no_id_is_recorded(
    app_scoped, requests
):
    """A carousel's enrichment is its parent read and its child page, plus a
    ``/me`` when the rows name the app-scoped id instead of the account's ref
    and no verified id is recorded: the third request the traversal ceiling's 3N
    allows. A turn reads it once, however many parents it enriches."""
    account = fx.OWNER_ID
    a = fx.useful(100, T0, owner_id="17841499999999999" if app_scoped else account)
    paths = []
    handler = _graph(a, profile={"id": a.owner_id, "user_id": account})

    def recording(request):
        paths.append(request.url.path)
        return handler(request)

    carousel = next(p for p in a.posts if p.children)
    parent = f"/v26.0/{carousel.parent['id']}"
    async with httpx.AsyncClient(transport=httpx.MockTransport(recording)) as client:
        page = await _adapter(a, client, account_ref=account).enrich_parents(
            [carousel.parent["id"]], source_id=SOURCE, workspace_id=WS
        )

    assert paths == [
        parent,
        *(["/v26.0/me"] if app_scoped else []),
        f"{parent}/children",
    ]
    assert len(paths) == requests
    assert [a.selected_asset_id for a in page.observations[0].assets] == [
        c["id"] for c in carousel.children
    ]


@pytest.mark.parametrize("n", SIZES)
def test_carousel_heavy_manifest_matches_independent_fixture_facts(n):
    a, want = fx.carousel(n, T0), fx.expected("carousel", n)
    totals = collections.Counter()
    slides = items = represented = truncations = carousels = 0
    ids = []
    for post in a.posts:
        if post.parent["media_type"] == "CAROUSEL_ALBUM":
            carousels += 1
            returned = post.children[:20]
            slides += len(returned)
            truncations += len(post.children) > 20
            if not returned:
                totals["unavailable"] += 1
        else:
            returned = (post.parent,)
        items += len(returned)
        represented += bool(returned)
        ids.extend(p["id"] for p in returned)
        totals[reason(post)] += len(returned)
    assert len(ids) == len(set(ids))
    assert len(a.posts) == want["observed"]
    assert carousels == want["carousels"]
    assert (items, slides, represented, truncations) == (
        want["items"],
        want["slides"],
        want["represented"],
        want["carousel_children_truncated"],
    )
    assert totals.pop(None) == want["candidates"]
    assert dict(totals) == want["candidate_exclusions"]
    assert want["runway_days"] == want["candidates"] // 3
    assert (
        a.posts[n // 10].parent["media_type"]
        == a.posts[-1].parent["media_type"]
        == "IMAGE"
    )
    ordinary = [p for p in a.posts if p.parent["media_type"] == "CAROUSEL_ALBUM"]
    for ordinal, post in enumerate(ordinary):
        if ordinal % 4 == 3:
            assert post.children[0]["media_type"] == "VIDEO"
            assert post.children[len(post.children) // 2]["media_type"] == "VIDEO"
