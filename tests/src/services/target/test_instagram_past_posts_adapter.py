"""Past-posts contract proofs: only MockTransport; no Instagram credentials."""

import json
from datetime import datetime, timezone

import httpx
import pytest

from src.services.target import instagram_past_posts_adapter as past_posts
from src.services.target import refusal_evidence
from src.services.target.instagram_catalog import (
    PastPostsAsset,
    PastPostsObservation,
    _validate,
)
from src.services.target.instagram_sources import PastPostsRefused
from src.services.target.egress import EgressPolicy, EgressRefused
from src.services.target.media_adapters import (
    MediaCredentialDead,
    MediaRetryableError,
    MediaSelectedAssetChanged,
    MediaSelectedAssetGone,
    MediaGraphResponseTooLarge,
    MediaSourceFenceDrop,
    MediaTerminalError,
    redacted_error,
)

WS = "11111111-1111-1111-1111-111111111111"
SOURCE = "22222222-2222-2222-2222-222222222222"
ACCOUNT = "17841400000000001"
URL = "https://scontent-test.cdninstagram.com/one.jpg?ephemeral=secret"
JPEG = b"\xff\xd8\xff\xe0image"


def post(mid, ts, **extra):
    return {
        "id": str(mid),
        "timestamp": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
        "media_type": "IMAGE",
        "media_url": URL,
        "owner": {"id": ACCOUNT},
        "like_count": 9,
        "comments_count": 2,
        **extra,
    }


def adapter(handler, *, size=2, access_calls=None, now=1000, app_scoped_id=None):
    async def access(source_id, *, workspace_id):
        if access_calls is not None:
            access_calls.append((source_id, workspace_id))
        return past_posts.PastPostsAccess(
            ACCOUNT, "token-never-in-url", app_scoped_id=app_scoped_id
        )

    class Admission:
        async def before_dispatch(self):
            return object()

        async def after_return(self, ticket):
            pass

    async def admission_provider(source_id, *, workspace_id, access):
        return Admission()

    return past_posts.InstagramPastPostsAdapter(
        admission_provider=admission_provider,
        access_provider=access,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        graph_version="v26.0",
        page_size=size,
        clock=lambda: now,
        policy=EgressPolicy().without(enforce_private_address_block=False),
    )


async def listing(a, cp=None):
    return await a.list_changes({"v": 1}, cp, source_id=SOURCE, workspace_id=WS)


async def fetch(a, ref="1", cap=100):
    return await a.fetch_bytes(
        {"v": 1},
        ref,
        source_id=SOURCE,
        workspace_id=WS,
        max_bytes=cap,
    )


def feed_handler(rows, *, inclusive=False, requests=None):
    def handler(req):
        if requests is not None:
            requests.append(req)
        q = req.url.params
        assert req.url.host == "graph.instagram.com"
        assert req.url.path == f"/v26.0/{ACCOUNT}/media"
        assert "access_token" not in q
        bound = int(q["until"])
        # The gap probe asks for `until` alone: nothing below it, newest first.
        since = int(q["since"]) if "since" in q else 0
        filtered = [
            r
            for r in rows
            if since <= past_posts._timestamp(r["timestamp"]) < bound + int(inclusive)
        ]
        offset = int(q.get("after", "0"))
        size = int(q["limit"])
        page = filtered[offset : offset + size]
        more = offset + size < len(filtered)
        body = {"data": page}
        if more:
            body["paging"] = {
                "next": "https://ignored.example/never-followed",
                "cursors": {"after": str(offset + size)},
            }
        return httpx.Response(200, json=body)

    return handler


@pytest.mark.parametrize("inclusive", [True, False])
async def test_watermark_pages_ties_without_persisting_a_cursor_or_url(inclusive):
    rows = [post(i, ts) for i, ts in enumerate([990, 980, 980, 980, 980, 979, 970], 1)]
    requests = []
    a = adapter(feed_handler(rows, inclusive=inclusive, requests=requests))
    cp = None
    found = []
    for _ in range(10):
        items, cp = await listing(a, cp)
        found.extend(i["ref"] for i in items)
        encoded = json.dumps([items, cp])
        assert URL not in encoded
        assert "token-never-in-url" not in encoded
        assert not {"after", "before", "cursor", "paging", "next"} & cp.keys()
        assert not a.checkpoint_whole(cp)
        if not a.checkpoint_incomplete(cp):
            break
    assert found == [str(i) for i in range(1, 8)]
    assert cp["seen"] == 7
    assert any("after" in r.url.params for r in requests)


async def test_interruption_replays_same_page_with_same_identities():
    a = adapter(feed_handler([post(1, 990), post(2, 980), post(3, 970)]))
    first, cp = await listing(a)
    replay, replay_cp = await listing(a)
    assert (first, cp) == (replay, replay_cp)
    second, _ = await listing(a, cp)
    assert {i["content_hash"] for i in first}.isdisjoint(
        i["content_hash"] for i in second
    )
    assert first[0]["content_hash"] == "instagram_past_posts:media:1"
    assert len(first[0]["content_hash"]) != 32  # Cannot equal Drive's MD5.


@pytest.mark.parametrize("inclusive", [True, False])
async def test_short_windows_include_both_edges_and_cross_empty_periods(inclusive):
    width, floor = past_posts.WINDOW_SECONDS, past_posts.LAUNCH_FLOOR
    now = floor + width * 4
    rows = [
        post(1, now),
        post(2, floor + width * 3 + 1),
        post(3, floor + width * 3),
        post(4, floor),
    ]
    requests = []
    a = adapter(feed_handler(rows, inclusive=inclusive, requests=requests), now=now)
    cp, found = None, []
    for _ in range(12):
        items, cp = await listing(a, cp)
        found.extend(i["ref"] for i in items)
        if not cp["more"]:
            break
    assert found == ["1", "2", "3", "4"]
    assert cp["more"] is False
    windows = [r for r in requests if "since" in r.url.params]
    assert all(
        0 < int(r.url.params["until"]) - int(r.url.params["since"]) <= width + 1
        for r in windows
    )
    # Post 3 sits on the first window's lower edge, so that window's replay finds
    # nothing new and one probe finds post 3. The empty period between post 3
    # and post 4 then costs one probe, not its three empty windows.
    probes = [r for r in requests if "since" not in r.url.params]
    assert [dict(r.url.params) for r in probes] == [
        {"fields": "id,timestamp", "until": str(until), "limit": "1"}
        for until in (floor + width * 3 + 1, floor + width + 1)
    ]
    assert len(windows) == 5


async def test_frozen_scan_excludes_new_posts_and_survives_deleted_boundary():
    rows = [post(1, 990), post(2, 980), post(3, 970)]
    a = adapter(feed_handler(rows))
    first, cp = await listing(a)
    rows.insert(0, post(4, 1100))
    rows.remove(rows[2])  # The second item's deletion invalidates no stored cursor.
    second, end = await listing(a, cp)
    assert [i["ref"] for i in first + second] == ["1", "2", "3"]
    assert end["upper"] == cp["upper"] == 1001


async def test_empty_page_uses_transient_cursor_instead_of_ending_scan():
    def handler(req):
        if "after" not in req.url.params:
            return httpx.Response(
                200,
                json={
                    "data": [],
                    "paging": {"next": "ignored", "cursors": {"after": "transient"}},
                },
            )
        return httpx.Response(200, json={"data": [post(1, 900)]})

    items, cp = await listing(adapter(handler))
    assert [i["ref"] for i in items] == ["1"]
    assert "transient" not in json.dumps(cp)


async def test_empty_page_uses_only_time_from_trusted_next_link():
    requests = []

    def handler(req):
        requests.append(req)
        if int(req.url.params["until"]) == 1001:
            return httpx.Response(
                200,
                json={
                    "data": [],
                    "paging": {
                        "next": f"https://graph.instagram.com/v26.0/{ACCOUNT}/media?until=950&access_token=secret"
                    },
                },
            )
        assert req.url.params["until"] == "951"
        assert "access_token" not in req.url.params
        return httpx.Response(200, json={"data": [post(1, 900)]})

    items, cp = await listing(adapter(handler))
    assert [i["ref"] for i in items] == ["1"]
    assert "secret" not in json.dumps(cp)
    assert len(requests) == 2


async def test_ambiguous_time_only_overlap_refuses_instead_of_skipping_peers():
    def handler(req):
        return httpx.Response(
            200,
            json={
                "data": [post(1, 900)],
                "paging": {
                    "next": f"https://graph.instagram.com/v26.0/{ACCOUNT}/media?until=899"
                },
            },
        )

    with pytest.raises(MediaRetryableError, match="no progress"):
        await listing(adapter(handler), {"until": 901, "seen": 1, "boundary": ["1"]})


async def test_10k_is_a_hard_bound_and_never_a_whole_inventory():
    a = adapter(feed_handler([post(1, 900), post(2, 890), post(3, 880)]), size=3)
    items, cp = await listing(a, {"upper": 1000, "until": 1000, "seen": 9998})
    assert len(items) == 2
    assert cp["seen"] == 10000
    assert cp["ceiling_reached"] and not cp["more"]
    assert not a.checkpoint_whole(cp)
    assert (await listing(a, cp))[0] == []


@pytest.mark.parametrize("extra", [{"media_url": None}, {"media_type": "UNKNOWN"}])
async def test_unavailable_items_advance_the_watermark_but_never_become_candidates(
    extra,
):
    a = adapter(feed_handler([post(1, 900, **extra)]))
    items, cp = await listing(a)
    assert items == []
    assert cp["seen"] == 1 and not cp["more"]


async def test_each_carousel_child_has_its_own_identity_and_no_media_urls():
    requests = []

    def handler(req):
        requests.append(req)
        if req.url.path.endswith("/children"):
            assert req.url.params["limit"] == str(past_posts.CAROUSEL_SLIDE_LIMIT)
            return httpx.Response(200, json={"data": [post(11, 900), post(12, 900)]})
        return httpx.Response(
            200, json={"data": [post(1, 900, media_type="CAROUSEL_ALBUM")]}
        )

    items, cp = await listing(adapter(handler))
    assert [item["ref"] for item in items] == ["11", "12"]
    assert [item["content_hash"] for item in items] == [
        past_posts.identity("11"),
        past_posts.identity("12"),
    ]
    assert [item["past_posts"]["position"] for item in items] == [1, 2]
    assert all(item["past_posts"]["media_id"] == "1" for item in items)
    assert URL not in json.dumps([items, cp])
    assert len(requests) == 2


async def test_unavailable_first_carousel_part_leaves_second_available():
    def handler(req):
        if req.url.path.endswith("/children"):
            return httpx.Response(
                200, json={"data": [post(11, 900, media_url=None), post(12, 900)]}
            )
        return httpx.Response(
            200, json={"data": [post(1, 900, media_type="CAROUSEL_ALBUM")]}
        )

    assert [item["ref"] for item in (await listing(adapter(handler)))[0]] == ["12"]


async def test_ten_mixed_slides_are_observed_in_order_without_cdn_or_validation(
    monkeypatch,
):
    requests = []
    originals = past_posts.egress.request

    async def graph_only(client, method, url, **kwargs):
        assert str(url).startswith("https://graph.instagram.com/")
        return await originals(client, method, url, **kwargs)

    monkeypatch.setattr(past_posts.egress, "request", graph_only)

    def handler(req):
        requests.append(req)
        if req.url.path.endswith("/children"):
            assert req.url.params["limit"] == "20"
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": str(i),
                            "media_type": "VIDEO" if i == 15 else "IMAGE",
                            "media_url": URL,
                        }
                        for i in range(11, 21)
                    ]
                },
            )
        return httpx.Response(
            200, json={"data": [post(1, 900, media_type="CAROUSEL_ALBUM")]}
        )

    page = await listing(adapter(handler))
    assert [c["ref"] for c in page.candidates] == [str(i) for i in range(11, 21)]
    assert page.candidates[4]["kind"] == "video"
    observation = page.observations[0]
    assert observation.slide_count == 10 and observation.slides_complete
    assert [asset.position for asset in observation.assets] == list(range(1, 11))
    assert len(requests) == page.counters.graph_attempts == 2
    assert page.counters.cdn_attempts == 0
    _validate(observation)


def test_observation_validation_rejects_duplicate_slide_identity_and_position():
    base = PastPostsObservation(
        "1",
        datetime.fromtimestamp(900, timezone.utc),
        assets=(
            PastPostsAsset("11", "image", position=1),
            PastPostsAsset("12", "video", position=2),
        ),
        slide_count=2,
    )
    _validate(base)
    for second in (
        PastPostsAsset("11", "video", position=2),
        PastPostsAsset("12", "video", position=1),
    ):
        with pytest.raises(PastPostsRefused, match="invalid_past_posts_observation"):
            _validate(past_posts.replace(base, assets=(base.assets[0], second)))
    with pytest.raises(PastPostsRefused, match="invalid_past_posts_observation"):
        _validate(past_posts.replace(base, assets=list(base.assets)))


@pytest.mark.parametrize(
    "children,paging,expected_complete",
    [
        ([], None, True),
        (
            [{"id": "11", "media_type": "IMAGE", "media_url": URL}],
            {"next": "opaque"},
            False,
        ),
    ],
)
async def test_empty_and_truncated_children_keep_one_request(
    children, paging, expected_complete
):
    requests = []

    def handler(req):
        requests.append(req)
        if req.url.path.endswith("/children"):
            body = {"data": children}
            if paging is not None:
                body["paging"] = paging
            return httpx.Response(200, json=body)
        return httpx.Response(
            200, json={"data": [post(1, 900, media_type="CAROUSEL_ALBUM")]}
        )

    page = await listing(adapter(handler))
    observation = page.observations[0]
    assert observation.slide_count == len(children)
    assert observation.slides_complete is expected_complete
    assert len(page.candidates) == len(children)
    assert len(requests) == 2


async def test_oversized_graph_reply_is_typed_and_retryable(monkeypatch):
    async def oversized(*args, **kwargs):
        raise past_posts.egress.ResponseTooLarge("fake oversized reply")

    monkeypatch.setattr(past_posts.egress, "request", oversized)
    with pytest.raises(MediaGraphResponseTooLarge):
        await listing(adapter(lambda req: httpx.Response(200, json={})))
    assert isinstance(
        redacted_error(MediaGraphResponseTooLarge()), MediaGraphResponseTooLarge
    )


async def test_absent_captions_and_hidden_counts_are_unknown_not_zero():
    items, _ = await listing(
        adapter(feed_handler([post(1, 900, like_count=None, comments_count=None)]))
    )
    meta = items[0]["past_posts"]
    assert meta["attribution"] == "unknown"
    assert meta["like_count"] is None and meta["comments_count"] is None
    assert "caption" not in meta


def test_attribution_is_derived_and_does_not_assert_ownership():
    assert past_posts.attribution(None) == ("unknown", [])
    assert past_posts.attribution("via @Author cr: @Someone credit: @AUTHOR") == (
        "credited",
        ["author", "someone"],
    )
    assert past_posts.attribution("my own caption") == ("not_detected", [])
    assert "token-never-in-url" not in repr(
        past_posts.PastPostsAccess(ACCOUNT, "token-never-in-url")
    )


@pytest.mark.parametrize(
    "data", [[post(1, 900, timestamp="bad")], [post(1, 900), post(2, 910)]]
)
async def test_invalid_or_unordered_timestamps_cannot_advance_the_watermark(data):
    with pytest.raises(MediaRetryableError):
        await listing(adapter(lambda req: httpx.Response(200, json={"data": data})))


async def test_repeated_cursor_refuses_without_advancing_checkpoint():
    cp = {"upper": 1000, "until": 901, "boundary": ["1"], "seen": 1, "more": True}
    original = cp.copy()

    def handler(req):
        return httpx.Response(
            200,
            json={
                "data": [post(1, 900)],
                "paging": {"next": "ignored", "cursors": {"after": "same"}},
            },
        )

    with pytest.raises(MediaRetryableError, match="no progress"):
        await listing(adapter(handler), cp)
    assert cp == original


async def test_timestamp_overlap_is_bounded_without_skipping_unseen_posts(monkeypatch):
    monkeypatch.setattr(past_posts, "MAX_PAGE_REQUESTS", 2)
    count = 0

    def handler(req):
        nonlocal count
        count += 1
        return httpx.Response(
            200,
            json={
                "data": [post(1, 900)],
                "paging": {"next": "ignored", "cursors": {"after": str(count)}},
            },
        )

    with pytest.raises(MediaRetryableError, match="page budget"):
        await listing(adapter(handler), {"until": 901, "boundary": ["1"], "seen": 1})
    assert count == 2


async def test_fetch_rereads_expired_url_and_never_sends_token_to_cdn():
    reads = []
    access_calls = []

    def handler(req):
        reads.append(req)
        if req.url.host == "graph.instagram.com":
            url = URL if len(reads) == 1 else URL.replace("one.jpg", "fresh.jpg")
            assert "access_token" not in req.url.params
            return httpx.Response(200, json=post(1, 900, media_url=url))
        assert "authorization" not in req.headers
        if "one.jpg" in req.url.path:
            return httpx.Response(403)
        return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})

    data, name, mime = await fetch(adapter(handler, access_calls=access_calls))
    assert (data, name, mime) == (JPEG, "Instagram 1.jpg", "image/jpeg")
    assert access_calls == [(SOURCE, WS)]
    assert [r.url.host for r in reads] == [
        "graph.instagram.com",
        "scontent-test.cdninstagram.com",
    ] * 2


@pytest.mark.parametrize(
    "profile, succeeds",
    [
        ({"id": "555", "user_id": ACCOUNT}, True),
        ({"id": "555", "user_id": "999"}, False),
        ({"id": "999", "user_id": ACCOUNT}, False),
        ({"user_id": ACCOUNT}, False),
    ],
)
async def test_login_scoped_owner_must_map_to_the_same_token_account(profile, succeeds):
    requests = []

    def handler(req):
        requests.append(req)
        if req.url.host != "graph.instagram.com":
            assert succeeds and "authorization" not in req.headers
            return httpx.Response(
                200, content=JPEG, headers={"content-type": "image/jpeg"}
            )
        if req.url.path.endswith("/me"):
            assert req.url.params["fields"] == "id,user_id"
            return httpx.Response(200, json=profile)
        return httpx.Response(200, json=post(1, 900, owner={"id": "555"}))

    a = adapter(handler)
    if succeeds:
        assert (await fetch(a))[0] == JPEG
    else:
        with pytest.raises(MediaTerminalError, match="ownership"):
            await fetch(a)
        assert all(r.url.host == "graph.instagram.com" for r in requests)


@pytest.mark.parametrize(
    "extra",
    [{"media_url": None}, {"owner": {"id": "999"}}, {"owner": None}, {"id": "2"}],
)
async def test_unavailable_or_foreign_media_is_not_downloaded(extra):
    requests = []

    def handler(req):
        requests.append(req)
        return httpx.Response(200, json=post(1, 900, **extra))

    with pytest.raises(MediaTerminalError):
        await fetch(adapter(handler))
    assert all(r.url.host == "graph.instagram.com" for r in requests)


@pytest.mark.parametrize(
    "status,code,exception",
    [
        (400, 190, MediaCredentialDead),
        (429, 80002, MediaRetryableError),
        (404, 100, MediaTerminalError),
    ],
)
async def test_graph_refusals_do_not_echo_provider_messages(status, code, exception):
    a = adapter(
        lambda req: httpx.Response(
            status, json={"error": {"code": code, "message": "secret-provider-data"}}
        )
    )
    with pytest.raises(exception) as error:
        await fetch(a)
    assert "secret-provider-data" not in str(error.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://s.cdninstagram.com/x",
        "https://cdninstagram.com.evil.test/x",
        "https://127.0.0.1/x",
        "https://user:pass@s.fbcdn.net/x",
        "https://s.fbcdn.net:444/x",
    ],
)
async def test_untrusted_media_urls_are_refused_before_download(url):
    requests = []

    def handler(req):
        requests.append(req)
        return httpx.Response(200, json=post(1, 900, media_url=url))

    with pytest.raises(MediaTerminalError, match="untrusted"):
        await fetch(adapter(handler))
    assert len(requests) == 1


async def test_download_enforces_byte_cap():
    def handler(req):
        if req.url.host == "graph.instagram.com":
            return httpx.Response(200, json=post(1, 900))
        return httpx.Response(
            200, content=b"x" * 200, headers={"content-type": "image/jpeg"}
        )

    with pytest.raises(MediaTerminalError, match="byte cap"):
        await fetch(adapter(handler), cap=100)


async def test_download_does_not_follow_a_cross_host_redirect():
    def handler(req):
        if req.url.host == "graph.instagram.com":
            return httpx.Response(200, json=post(1, 900))
        return httpx.Response(302, headers={"location": "https://other.fbcdn.net/x"})

    with pytest.raises(EgressRefused, match="cross-host"):
        await fetch(adapter(handler))


@pytest.mark.parametrize(
    "defaults",
    [
        {"headers": {"Authorization": "Bearer secret"}},
        {"headers": {"Proxy-Authorization": "Basic secret"}},
        {"auth": ("user", "secret")},
        {"params": {"access_token": "secret"}},
        {"cookies": {"session": "secret"}},
    ],
)
def test_client_credentials_cannot_be_inherited_by_cdn(defaults):
    with pytest.raises(MediaTerminalError, match="without credentials"):
        past_posts.InstagramPastPostsAdapter(
            access_provider=None,
            client=httpx.AsyncClient(**defaults),
            graph_version="v26.0",
        )


async def test_client_mutation_after_construction_is_refused_before_cdn():
    calls = []

    def handler(req):
        calls.append(req)
        a._client.headers["Authorization"] = "Bearer secret"
        return httpx.Response(200, json=post(1, 900))

    a = adapter(handler)
    with pytest.raises(MediaTerminalError, match="without credentials"):
        await fetch(a)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "target",
    [
        "http://scontent-test.cdninstagram.com/x",
        "https://scontent-test.cdninstagram.com:8080/x",
        "https://user:secret@scontent-test.cdninstagram.com/x",
        "https://scontent-test.cdninstagram.com/another.jpg",
    ],
)
async def test_ephemeral_media_refuses_even_same_host_redirects(target):
    calls = []

    def handler(req):
        calls.append(req)
        if req.url.host == "graph.instagram.com":
            return httpx.Response(200, json=post(1, 900))
        return httpx.Response(302, headers={"location": target})

    with pytest.raises(EgressRefused, match="redirect refused"):
        await fetch(adapter(handler))
    assert len(calls) == 2


# --- the explicit preview: its own bounded budget -------------------------------

APP_SCOPED = "555"
IMAGE_URL = "https://scontent-image.cdninstagram.com/preview.jpg?ephemeral=secret"
VIDEO_URL = "https://scontent-video.cdninstagram.com/clip.mp4?ephemeral=secret"
THUMB_URL = "https://scontent-thumb.fbcdn.net/cover.jpg?ephemeral=secret"
CHILD_URL = "https://scontent-child.cdninstagram.com/first.jpg?ephemeral=secret"


def media(mid="1", **extra):
    return {
        "id": mid,
        "media_type": "IMAGE",
        "media_url": IMAGE_URL,
        "owner": {"id": ACCOUNT},
        **extra,
    }


def preview_handler(
    parent, *, child=None, profile=None, cdn=None, requests=None, paging=None
):
    """Graph answers for one parent, its first child and the token's profile;
    the CDN answers *cdn* (default a JPEG). Every request is recorded."""

    def handler(req):
        if requests is not None:
            requests.append(req)
        if req.url.host == "graph.instagram.com":
            assert req.headers["authorization"] == "Bearer token-never-in-url"
            assert "access_token" not in req.url.params
            if req.url.path.endswith("/me"):
                return httpx.Response(200, json=profile)
            if req.url.path.endswith("/children"):
                body = {"data": child if isinstance(child, list) else [child]}
                if paging is not None:
                    body["paging"] = paging
                return httpx.Response(200, json=body)
            return httpx.Response(200, json=parent)
        assert "authorization" not in req.headers, "the token never reaches the CDN"
        assert "access_token" not in req.url.params
        if cdn is not None:
            return cdn(req) if callable(cdn) else cdn
        return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})

    return handler


async def preview(a, ref="1", selected="1"):
    return await a.fetch_preview(
        ref, source_id=SOURCE, workspace_id=WS, selected_asset_id=selected
    )


def hosts(requests):
    return [(r.url.host, r.url.path) for r in requests]


async def test_preview_of_an_image_is_one_lookup_and_one_download():
    requests = []
    a = adapter(preview_handler(media(), requests=requests))
    assert await preview(a) == JPEG
    assert hosts(requests) == [
        ("graph.instagram.com", "/v26.0/1"),
        ("scontent-image.cdninstagram.com", "/preview.jpg"),
    ]
    assert requests[0].url.params["fields"] == (
        "id,owner,media_type,media_url,thumbnail_url"
    )


async def test_preview_proves_an_app_scoped_owner_with_one_more_lookup():
    requests = []
    a = adapter(
        preview_handler(
            media(owner={"id": APP_SCOPED}),
            profile={"id": APP_SCOPED, "user_id": ACCOUNT},
            requests=requests,
        )
    )
    assert await preview(a) == JPEG
    assert [path for _, path in hosts(requests)] == [
        "/v26.0/1",
        "/v26.0/me",
        "/preview.jpg",
    ]


async def test_preview_of_a_carousel_downloads_its_first_part():
    requests = []
    a = adapter(
        preview_handler(
            media(media_type="CAROUSEL_ALBUM", media_url=None),
            child={"id": "11", "media_type": "IMAGE", "media_url": CHILD_URL},
            requests=requests,
        )
    )
    assert await preview(a, selected="11") == JPEG
    assert hosts(requests) == [
        ("graph.instagram.com", "/v26.0/1"),
        ("graph.instagram.com", "/v26.0/1/children"),
        ("scontent-child.cdninstagram.com", "/first.jpg"),
    ]
    assert requests[1].url.params["fields"] == "id,media_type,media_url,thumbnail_url"
    assert requests[1].url.params["limit"] == str(past_posts.CAROUSEL_SLIDE_LIMIT)


async def test_preview_selects_slide_by_id_after_reordering():
    requests = []
    children = [
        {"id": "12", "media_type": "IMAGE", "media_url": CHILD_URL},
        {"id": "11", "media_type": "IMAGE", "media_url": IMAGE_URL},
    ]
    a = adapter(
        preview_handler(
            media(media_type="CAROUSEL_ALBUM"), child=children, requests=requests
        )
    )
    assert await preview(a, selected="11") == JPEG
    assert hosts(requests)[-1] == ("scontent-image.cdninstagram.com", "/preview.jpg")


async def test_fetch_selects_slide_by_id_after_reordering():
    requests = []
    children = [
        {"id": "12", "media_type": "IMAGE", "media_url": CHILD_URL},
        {"id": "11", "media_type": "IMAGE", "media_url": IMAGE_URL},
    ]
    a = adapter(
        preview_handler(
            media(media_type="CAROUSEL_ALBUM"), child=children, requests=requests
        )
    )
    data, _, mime = await a.fetch_bytes(
        {},
        "1",
        source_id=SOURCE,
        workspace_id=WS,
        max_bytes=100,
        selected_asset_id="11",
    )
    assert (data, mime) == (JPEG, "image/jpeg")
    assert hosts(requests)[-1] == ("scontent-image.cdninstagram.com", "/preview.jpg")


@pytest.mark.parametrize(
    "paging,error",
    [(None, MediaSelectedAssetGone), ({"next": "opaque"}, MediaSelectedAssetChanged)],
)
async def test_missing_selected_slide_uses_completeness_to_classify(paging, error):
    requests = []
    a = adapter(
        preview_handler(
            media(media_type="CAROUSEL_ALBUM"),
            child={"id": "12", "media_type": "IMAGE", "media_url": CHILD_URL},
            paging=paging,
            requests=requests,
        )
    )
    with pytest.raises(error):
        await preview(a, selected="11")
    assert len(requests) == 2
    assert isinstance(redacted_error(error()), error)


async def test_verified_app_scoped_owner_previews_carousel_without_me_lookup():
    requests = []
    a = adapter(
        preview_handler(
            media(media_type="CAROUSEL_ALBUM", owner={"id": APP_SCOPED}),
            child={"id": "11", "media_type": "IMAGE", "media_url": CHILD_URL},
            requests=requests,
        ),
        app_scoped_id=APP_SCOPED,
    )
    assert await preview(a, selected="11") == JPEG
    assert [path for _, path in hosts(requests)] == [
        "/v26.0/1",
        "/v26.0/1/children",
        "/first.jpg",
    ]


async def test_a_carousel_of_an_app_scoped_owner_fails_closed_before_a_third_lookup():
    """Proving the owner takes `me`, reaching the first part takes `children`:
    with the parent that is three, so neither is asked and nothing downloads."""
    requests = []
    a = adapter(
        preview_handler(
            media(media_type="CAROUSEL_ALBUM", owner={"id": APP_SCOPED}),
            child={"id": "11", "media_type": "IMAGE", "media_url": CHILD_URL},
            profile={"id": APP_SCOPED, "user_id": ACCOUNT},
            requests=requests,
        )
    )
    with pytest.raises(MediaTerminalError, match="third Graph request"):
        await preview(a, selected="11")
    assert hosts(requests) == [("graph.instagram.com", "/v26.0/1")]


async def test_the_preview_turn_carries_the_graph_budget(monkeypatch):
    """The dispatch guard holds the preview to its budget, not the scan's."""
    monkeypatch.setattr(past_posts, "PREVIEW_GRAPH_REQUESTS", 1)
    requests = []
    a = adapter(
        preview_handler(
            media(owner={"id": APP_SCOPED}),
            profile={"id": APP_SCOPED, "user_id": ACCOUNT},
            requests=requests,
        )
    )
    with pytest.raises(MediaRetryableError, match="page budget"):
        await preview(a)
    assert hosts(requests) == [("graph.instagram.com", "/v26.0/1")]


@pytest.mark.parametrize("carousel", [False, True])
async def test_a_videos_preview_is_its_graph_thumbnail_never_the_video(carousel):
    requests = []
    video = {"media_type": "VIDEO", "media_url": VIDEO_URL, "thumbnail_url": THUMB_URL}
    parent = (
        media(media_type="CAROUSEL_ALBUM", media_url=None)
        if carousel
        else media(**video)
    )
    a = adapter(preview_handler(parent, child={"id": "11", **video}, requests=requests))
    assert await preview(a, selected="11" if carousel else "1") == JPEG
    assert hosts(requests)[-1] == ("scontent-thumb.fbcdn.net", "/cover.jpg")
    assert all(r.url.host != "scontent-video.cdninstagram.com" for r in requests)


async def test_a_video_without_a_graph_thumbnail_downloads_nothing():
    requests = []
    a = adapter(
        preview_handler(
            media(media_type="VIDEO", media_url=VIDEO_URL), requests=requests
        )
    )
    with pytest.raises(MediaTerminalError, match="no preview"):
        await preview(a)
    assert hosts(requests) == [("graph.instagram.com", "/v26.0/1")]


@pytest.mark.parametrize("over", [0, 1])
async def test_the_preview_byte_cap_admits_exactly_two_mebibytes(over):
    body = b"\xff" * (past_posts.PREVIEW_MAX_BYTES + over)
    a = adapter(
        preview_handler(
            media(),
            cdn=httpx.Response(
                200, content=body, headers={"content-type": "image/jpeg"}
            ),
        )
    )
    if over:
        with pytest.raises(MediaTerminalError, match="byte cap"):
            await preview(a)
    else:
        assert await preview(a) == body


async def test_an_expired_preview_link_is_refused_without_a_reread():
    requests = []
    a = adapter(preview_handler(media(), cdn=httpx.Response(403), requests=requests))
    with pytest.raises(MediaTerminalError, match="link is unavailable"):
        await preview(a)
    assert [host for host, _ in hosts(requests)] == [
        "graph.instagram.com",
        "scontent-image.cdninstagram.com",
    ]


@pytest.mark.parametrize("leg", ["graph", "cdn"])
async def test_a_failed_preview_request_is_never_retried(leg):
    requests = []

    def cdn(req):
        raise httpx.ConnectError("refused", request=req)

    handler = preview_handler(media(), cdn=cdn, requests=requests)

    def failing(req):
        if leg == "graph" and req.url.host == "graph.instagram.com":
            requests.append(req)
            raise httpx.ConnectError("refused", request=req)
        return handler(req)

    with pytest.raises(MediaRetryableError):
        await preview(adapter(failing))
    assert len(requests) == (1 if leg == "graph" else 2)


@pytest.mark.parametrize(
    "url",
    [
        "https://cdninstagram.com.evil.test/x.jpg",
        "http://scontent-image.cdninstagram.com/x.jpg",
        "https://127.0.0.1/x.jpg",
    ],
)
async def test_an_untrusted_preview_host_is_refused_before_download(url):
    requests = []
    a = adapter(preview_handler(media(media_url=url), requests=requests))
    with pytest.raises(MediaTerminalError, match="untrusted"):
        await preview(a)
    assert hosts(requests) == [("graph.instagram.com", "/v26.0/1")]


async def test_a_preview_redirect_is_not_followed():
    requests = []
    a = adapter(
        preview_handler(
            media(),
            cdn=httpx.Response(302, headers={"location": IMAGE_URL + "&next=1"}),
            requests=requests,
        )
    )
    with pytest.raises(EgressRefused, match="redirect refused"):
        await preview(a)
    assert len(requests) == 2


@pytest.mark.parametrize(
    "content_type,body",
    [
        ("image/png", JPEG),
        ("video/mp4", JPEG),
        ("text/html", b"<html>"),
        ("image/jpeg", b""),
    ],
)
async def test_a_preview_that_is_not_jpeg_bytes_is_refused(content_type, body):
    a = adapter(
        preview_handler(
            media(),
            cdn=httpx.Response(
                200, content=body, headers={"content-type": content_type}
            ),
        )
    )
    with pytest.raises(MediaTerminalError, match="unsupported preview"):
        await preview(a)


async def test_a_gone_slide_is_not_previewed_under_the_old_observation():
    requests = []
    a = adapter(
        preview_handler(
            media(media_type="CAROUSEL_ALBUM"),
            child={"id": "12", "media_type": "IMAGE", "media_url": CHILD_URL},
            requests=requests,
        )
    )
    with pytest.raises(MediaSelectedAssetGone):
        await preview(a, selected="11")
    assert all(r.url.host == "graph.instagram.com" for r in requests)


async def test_every_preview_request_is_admitted_and_a_drop_stops_the_download():
    events = []

    class Admission:
        def __init__(self, refuse_at):
            self.refuse_at = refuse_at

        async def before_dispatch(self):
            events.append("before")
            if events.count("before") == self.refuse_at:
                raise MediaSourceFenceDrop(reason="source_off")
            return object()

        async def after_return(self, ticket):
            events.append("after")

    requests = []

    def build(refuse_at):
        async def admission_provider(source_id, *, workspace_id, access):
            return Admission(refuse_at)

        async def access(source_id, *, workspace_id):
            return past_posts.PastPostsAccess(ACCOUNT, "token-never-in-url")

        return past_posts.InstagramPastPostsAdapter(
            admission_provider=admission_provider,
            access_provider=access,
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(
                    preview_handler(media(), requests=requests)
                )
            ),
            graph_version="v26.0",
            policy=EgressPolicy().without(enforce_private_address_block=False),
        )

    assert await preview(build(refuse_at=0)) == JPEG
    assert events == ["before", "after", "before", "after"]
    events.clear()
    requests.clear()
    with pytest.raises(MediaSourceFenceDrop):
        await preview(build(refuse_at=2))
    assert events == ["before", "after", "before"]
    assert hosts(requests) == [("graph.instagram.com", "/v26.0/1")]


@pytest.mark.parametrize(
    "value",
    [
        "2022-12-31T12:00:00+0000",
        "2022-12-31T12:00:00+00:00",
        "2022-12-31T12:00:00Z",
        "2022-12-31T17:30:00+0530",
        "2022-12-31T07:00:00-0500",
    ],
)
def test_graph_timestamps_parse_with_or_without_the_offset_colon(value):
    """Graph's own form is `+0000`, which Python 3.10's `fromisoformat`
    rejects without the adapter's normalization (CI runs 3.10)."""
    assert past_posts._timestamp(value) == 1672488000


@pytest.mark.parametrize("value", ["2022-12-31T12:00:00", "not a time", None])
def test_a_timestamp_without_a_usable_zone_is_refused(value):
    with pytest.raises(MediaRetryableError):
        past_posts._timestamp(value)


# --- the evidence a refused fetch keeps -----------------------------------------
# A refusal raises the class and the constant message it always has, with its
# facts on `evidence` (`refusal_evidence.of`). Each test below pins one kind: the
# facts it keeps and, through `assert_redacted`, what no kind keeps.

TOKEN = "token-never-in-url"  # the access token `adapter()` resolves
FOREIGN = "17890000000000001"  # a media owner that is not ACCOUNT
OTHER = "17890000000000002"  # an id that is neither ACCOUNT nor FOREIGN
ASKED = "17890000000000003"  # the media a fetch asks for
CDN_HOST = "scontent-evidence.cdninstagram.com"
EXPIRY = "6A1B2C3D"
TAIL = f"/x.jpg?oh=00_signature-value&oe={EXPIRY}"
SIGNED_URL = (
    f"https://{CDN_HOST}/v/t51.2885-15/signed-example.jpg"
    "?stp=dst-jpg_e35&_nc_cat=100&_nc_sid=sid-value-1&ccb=1-7"
    f"&oh=00_signature-value&oe={EXPIRY}"
)
# What a signed link carries besides its host and expiry: the link itself, its
# query string and their pieces.
SIGNATURE = (
    SIGNED_URL,
    SIGNED_URL.partition("?")[2],
    "signed-example",
    "stp=",
    "_nc_",
    "sid-value-1",
    "ccb=",
    "oh=",
    "00_signature-value",
    "oe=",
)
# What a Graph request carries besides its path.
GRAPH_QUERY = ("fields=", "limit=", past_posts._FETCH_FIELDS)


async def refusal_of(call, error, message):
    """The evidence on the refusal `call` raises: exactly `error`, carrying
    exactly `message`, which is constant and holds no provider text."""
    with pytest.raises(error) as caught:
        await call
    assert type(caught.value) is error
    assert str(caught.value) == message
    found = refusal_evidence.of(caught.value)
    assert found is not None, "the refusal carries no evidence"
    return found


def assert_redacted(evidence, *also):
    """`evidence` names no token, no query string, no signature and none of `also`."""
    encoded = json.dumps(evidence)
    for fragment in (
        TOKEN,
        "access_token",
        "Bearer",
        "?",
        *SIGNATURE,
        *GRAPH_QUERY,
        *also,
    ):
        assert fragment not in encoded, f"{fragment!r} reached the evidence"


@pytest.mark.parametrize(
    "leg,path",
    [("parent", f"/v26.0/{ASKED}"), ("children", f"/v26.0/{ASKED}/children")],
)
async def test_a_graph_refusal_keeps_its_numbers_a_scrubbed_message_and_the_path(
    leg, path
):
    """graph_error: Graph's status, numbers and trace id; its message with the
    token, the token parameter and every link removed; and the path of the
    request it refused, never that request's query."""
    said = (
        f"Invalid OAuth access token - Cannot parse access token {TOKEN}. "
        f"Retry https://graph.instagram.com/v26.0/{ASKED}?fields=id&access_token={TOKEN} "
        f"or download {SIGNED_URL} instead."
    )

    def handler(req):
        assert req.headers["authorization"] == f"Bearer {TOKEN}"
        if leg == "children" and not req.url.path.endswith("/children"):
            return httpx.Response(200, json=media(ASKED, media_type="CAROUSEL_ALBUM"))
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": said,
                    "type": "OAuthException",
                    "code": 100,
                    "error_subcode": 33,
                    "fbtrace_id": "AbC123fakeTrace",
                }
            },
        )

    evidence = await refusal_of(
        fetch(adapter(handler), ref=ASKED),
        MediaTerminalError,
        "Instagram media is unavailable to this account",
    )
    assert evidence == {
        "v": 1,
        "kind": "graph_error",
        "http_status": 400,
        "code": 100,
        "error_subcode": 33,
        "type": "OAuthException",
        "message": (
            "Invalid OAuth access token - Cannot parse access token <token>. "
            "Retry <link graph.instagram.com> or download "
            f"<link {CDN_HOST}> instead."
        ),
        "fbtrace_id": "AbC123fakeTrace",
        "path": path,
    }
    assert_redacted(evidence)


async def test_a_graph_refusal_without_an_error_object_keeps_the_status_and_the_path():
    """graph_error: a reply that is not an error object keeps what the transport
    knows, the status and the path, and none of the reply's text."""
    a = adapter(lambda req: httpx.Response(404, json={"error": "plain provider text"}))
    evidence = await refusal_of(
        fetch(a, ref=ASKED),
        MediaTerminalError,
        "Instagram media is unavailable to this account",
    )
    assert evidence == {
        "v": 1,
        "kind": "graph_error",
        "http_status": 404,
        "code": None,
        "error_subcode": None,
        "type": None,
        "message": None,
        "fbtrace_id": None,
        "path": f"/v26.0/{ASKED}",
    }
    assert_redacted(evidence, "plain provider text")


@pytest.mark.parametrize(
    "owner,recorded,profile,which,match",
    [
        pytest.param(None, None, None, "no_owner", {}, id="no-owner"),
        pytest.param(
            {"id": "not-an-id"}, None, None, "malformed_owner_id", {}, id="malformed-id"
        ),
        pytest.param(
            {"id": int(FOREIGN)}, None, None, "malformed_owner_id", {}, id="numeric-id"
        ),
        pytest.param({}, None, None, "malformed_owner_id", {}, id="missing-id"),
        pytest.param(
            {"id": FOREIGN},
            OTHER,
            None,
            "app_scoped_id",
            {"account_matches": False, "app_scoped_id_matches": False},
            id="recorded-mapping",
        ),
        pytest.param(
            {"id": FOREIGN},
            None,
            {"id": FOREIGN, "user_id": OTHER},
            "profile",
            {
                "account_matches": False,
                "profile_user_id_matches": False,
                "profile_id_matches": True,
            },
            id="profile-user-differs",
        ),
        pytest.param(
            {"id": FOREIGN},
            None,
            {"id": OTHER, "user_id": ACCOUNT},
            "profile",
            {
                "account_matches": False,
                "profile_user_id_matches": True,
                "profile_id_matches": False,
            },
            id="profile-id-differs",
        ),
        pytest.param(
            {"id": FOREIGN},
            None,
            {"id": OTHER, "user_id": OTHER},
            "profile",
            {
                "account_matches": False,
                "profile_user_id_matches": False,
                "profile_id_matches": False,
            },
            id="profile-both-differ",
        ),
    ],
)
async def test_an_unproven_owner_keeps_which_test_failed_and_flags_never_an_id(
    owner, recorded, profile, which, match
):
    """ownership_unproven: the test that decided and its outcomes as booleans;
    no owner, account or profile id reaches the record."""
    a = adapter(
        preview_handler(media(owner=owner), profile=profile), app_scoped_id=recorded
    )
    evidence = await refusal_of(
        fetch(a),
        MediaTerminalError,
        "Instagram media ownership could not be established",
    )
    assert evidence == {
        "v": 1,
        "kind": "ownership_unproven",
        "test": which,
        "match": match,
    }
    assert all(type(flag) is bool for flag in evidence["match"].values())
    assert_redacted(evidence, ACCOUNT, FOREIGN, OTHER, "not-an-id")


async def test_a_failed_profile_read_is_not_repeated_and_the_verdict_names_it():
    """ownership_unproven: once a turn's one profile read has failed the owner is
    unproven by `profile_read_failed`, with no outcome flags and no second read.
    A failed read ends a fetch, so no fetch reaches this state: the verdict is
    pinned where it is decided."""

    def no_request(req):
        raise AssertionError("a second profile read was made")

    turn = past_posts._Turn(
        past_posts.PastPostsAccess(ACCOUNT, TOKEN), None, 1, 1, profile_read=True
    )
    verdict = await adapter(no_request)._ownership({"id": FOREIGN}, turn)
    assert (verdict.ok, verdict.test, verdict.match) == (
        False,
        "profile_read_failed",
        {},
    )


async def test_a_different_media_item_keeps_that_the_id_differed_and_neither_id():
    """identity_mismatch: only that the returned id was not the one asked for."""
    a = adapter(preview_handler(media(OTHER)))
    evidence = await refusal_of(
        fetch(a, ref=ASKED),
        MediaTerminalError,
        "Instagram returned a different media item",
    )
    assert evidence == {"v": 1, "kind": "identity_mismatch", "id_matches": False}
    assert_redacted(evidence, ASKED, OTHER, ACCOUNT)


@pytest.mark.parametrize(
    "paging,error,message,kind,complete",
    [
        (None, MediaSelectedAssetGone, "selected_asset_gone", "slide_gone", True),
        (
            {"next": "opaque"},
            MediaSelectedAssetChanged,
            "selected_asset_changed",
            "slide_changed",
            False,
        ),
    ],
    ids=["gone", "changed"],
)
async def test_a_missing_slide_keeps_the_slide_count_and_whether_it_was_complete(
    paging, error, message, kind, complete
):
    """slide_gone and slide_changed: how many slides the read returned and whether
    that was the whole carousel; no slide id, asked for or listed."""
    slides = ["17890000000000011", "17890000000000012", "17890000000000013"]
    asked, *listed = slides
    children = [
        {"id": slide, "media_type": "IMAGE", "media_url": SIGNED_URL}
        for slide in listed
    ]
    a = adapter(
        preview_handler(
            media(media_type="CAROUSEL_ALBUM"), child=children, paging=paging
        )
    )
    evidence = await refusal_of(
        a.fetch_bytes(
            {},
            "1",
            source_id=SOURCE,
            workspace_id=WS,
            max_bytes=100,
            selected_asset_id=asked,
        ),
        error,
        message,
    )
    assert evidence == {"v": 1, "kind": kind, "children": 2, "complete": complete}
    assert_redacted(evidence, *slides)


@pytest.mark.parametrize(
    "extra,media_type,has_link",
    [
        ({"media_url": None}, "IMAGE", False),
        ({"media_url": ""}, "IMAGE", False),
        ({"media_type": "UNKNOWN", "media_url": SIGNED_URL}, "UNKNOWN", True),
    ],
    ids=["no-link", "empty-link", "unsupported-type"],
)
async def test_a_media_item_without_a_file_keeps_its_type_and_whether_it_had_a_link(
    extra, media_type, has_link
):
    """no_media_link: the media type and whether a link was present; a link that
    was present is not recorded at all, not even its host."""
    a = adapter(preview_handler(media(**extra)))
    evidence = await refusal_of(
        fetch(a), MediaTerminalError, "Instagram does not provide this media file"
    )
    assert evidence == {
        "v": 1,
        "kind": "no_media_link",
        "media_type": media_type,
        "has_link": has_link,
    }
    assert_redacted(evidence, CDN_HOST)


@pytest.mark.parametrize(
    "url,host",
    [
        (f"http://{CDN_HOST}{TAIL}", CDN_HOST),
        (
            f"https://cdninstagram.com.untrusted.test{TAIL}",
            "cdninstagram.com.untrusted.test",
        ),
        (f"https://fake-user@{CDN_HOST}{TAIL}", CDN_HOST),
        (f"https://{CDN_HOST}:8443{TAIL}", CDN_HOST),
        (f"https://127.0.0.1{TAIL}", "127.0.0.1"),
    ],
    ids=["plain-http", "lookalike-suffix", "user-info", "port", "address"],
)
async def test_an_untrusted_host_keeps_the_host_and_the_expiry_only(url, host):
    """untrusted_host: the host the link named and its expiry; no scheme,
    user info, port, path or signature, and no download was attempted."""
    requests = []
    a = adapter(preview_handler(media(media_url=url), requests=requests))
    evidence = await refusal_of(
        fetch(a), MediaTerminalError, "Instagram returned an untrusted media host"
    )
    assert evidence == {
        "v": 1,
        "kind": "untrusted_host",
        "host": host,
        "expiry": EXPIRY,
    }
    assert_redacted(evidence, "fake-user", "8443")
    assert all(r.url.host == "graph.instagram.com" for r in requests)


async def test_an_oversized_download_keeps_the_cap_the_bytes_read_and_the_host():
    """over_byte_cap: the cap, the bytes read when the read was abandoned (the
    body arrives whole, so all 200) and the host; no path, parameter or bytes."""
    body = httpx.Response(
        200, content=b"x" * 200, headers={"content-type": "image/jpeg"}
    )
    a = adapter(preview_handler(media(media_url=SIGNED_URL), cdn=body))
    evidence = await refusal_of(
        fetch(a, cap=100), MediaTerminalError, "Instagram media exceeds the byte cap"
    )
    assert evidence == {
        "v": 1,
        "kind": "over_byte_cap",
        "cap": 100,
        "received": 200,
        "host": CDN_HOST,
    }
    assert_redacted(evidence)


async def test_a_refused_download_keeps_the_status_of_both_attempts():
    """cdn_refused: the status each of the two downloads was answered with, so the
    second refusal does not hide the first, with the link's host and expiry."""
    answers = iter([403, 404])
    requests = []
    a = adapter(
        preview_handler(
            media(media_url=SIGNED_URL),
            cdn=lambda req: httpx.Response(next(answers)),
            requests=requests,
        )
    )
    evidence = await refusal_of(
        fetch(a), MediaTerminalError, "Instagram media file is unavailable"
    )
    assert evidence == {
        "v": 1,
        "kind": "cdn_refused",
        "statuses": [403, 404],
        "host": CDN_HOST,
        "expiry": EXPIRY,
    }
    assert [r.url.host for r in requests] == ["graph.instagram.com", CDN_HOST] * 2
    assert_redacted(evidence)


@pytest.mark.parametrize(
    "statuses",
    [[503], [403, 503]],
    ids=["first-download", "after-an-expired-link"],
)
async def test_a_failed_download_keeps_every_status_the_cdn_answered(statuses):
    """cdn_error, retryable: the statuses of the downloads so far, ending in the
    failure, with the link's host and expiry."""
    answers = iter(statuses)
    a = adapter(
        preview_handler(
            media(media_url=SIGNED_URL), cdn=lambda req: httpx.Response(next(answers))
        )
    )
    evidence = await refusal_of(
        fetch(a), MediaRetryableError, "Instagram media download failed"
    )
    assert evidence == {
        "v": 1,
        "kind": "cdn_error",
        "statuses": statuses,
        "host": CDN_HOST,
        "expiry": EXPIRY,
    }
    assert_redacted(evidence)


@pytest.mark.parametrize(
    "content_type,body,kept",
    [
        ("text/html; charset=utf-8", b"<html>BODY-MARKER</html>", "text/html"),
        ("image/jpeg", b"", "image/jpeg"),
    ],
    ids=["not-an-image", "empty-image"],
)
async def test_unsupported_bytes_keep_their_type_length_status_and_host(
    content_type, body, kept
):
    """wrong_bytes: the content type without its parameters, the length, the status
    and the host; never the bytes."""
    served = httpx.Response(200, content=body, headers={"content-type": content_type})
    a = adapter(preview_handler(media(media_url=SIGNED_URL), cdn=served))
    evidence = await refusal_of(
        fetch(a), MediaTerminalError, "Instagram returned unsupported media bytes"
    )
    assert evidence == {
        "v": 1,
        "kind": "wrong_bytes",
        "content_type": kept,
        "length": len(body),
        "http_status": 200,
        "host": CDN_HOST,
    }
    assert_redacted(evidence, "BODY-MARKER")
