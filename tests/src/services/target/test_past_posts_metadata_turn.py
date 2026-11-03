"""Bounded past-posts turns use recording transports and closed admission tickets."""

import asyncio
from dataclasses import asdict, FrozenInstanceError
from datetime import datetime, timezone

import httpx
import pytest

from src.services.target import egress, media_adapters, vocabulary
from src.services.target import instagram_past_posts_adapter as past_posts
from src.services.target.media_adapters import (
    MediaCredentialDead,
    MediaRetryableError,
    MediaSelectedAssetGone,
    MediaTerminalError,
    MediaSourceFenceDrop,
    redacted_error,
)
from src.services.target.unit_of_work import in_transaction

ACCOUNT = "17841400000000001"
SOURCE = "22222222-2222-2222-2222-222222222222"
WS = "11111111-1111-1111-1111-111111111111"
URL = "https://scontent-test.cdninstagram.com/one.jpg?secret=never-persist"


def parent(mid, kind="IMAGE", url=URL):
    return {
        "id": str(mid),
        "timestamp": "1970-01-01T00:15:00Z",
        "media_type": kind,
        "media_url": url,
        "owner": {"id": ACCOUNT},
    }


class Admission:
    def __init__(self):
        self.epoch = 1
        self.enabled = True
        self.events = []
        self.reject_before = None

    async def before_dispatch(self):
        self.events.append("admit")
        if (
            not self.enabled
            or len([e for e in self.events if e == "admit"]) == self.reject_before
        ):
            raise MediaSourceFenceDrop()
        assert not in_transaction()
        return self.epoch

    async def after_return(self, ticket):
        self.events.append("fence")
        assert not in_transaction()
        if not self.enabled or ticket != self.epoch:
            raise MediaSourceFenceDrop()


def make(
    handler,
    *,
    admission=None,
    graph_limit=20,
    parent_limit=100,
    size=100,
    enrich=False,
    metadata_only=False,
    app_scoped_id=None,
):
    admission = admission or Admission()

    async def access(source_id, *, workspace_id):
        assert (source_id, workspace_id) == (SOURCE, WS)
        return past_posts.PastPostsAccess(
            ACCOUNT, "redacted-test-token", app_scoped_id=app_scoped_id
        )

    async def provider(source_id, *, workspace_id, access):
        return admission

    adapter = past_posts.InstagramPastPostsAdapter(
        access_provider=access,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        graph_version="v26.0",
        page_size=size,
        clock=lambda: 1000,
        policy=egress.EgressPolicy().without(enforce_private_address_block=False),
        admission_provider=provider,
        graph_limit=graph_limit,
        parent_limit=parent_limit,
        enrich_captions=enrich,
        metadata_only=metadata_only,
    )
    return adapter, admission


async def listing(a, cp=None):
    return await a.list_changes({}, cp, source_id=SOURCE, workspace_id=WS)


async def test_every_parent_survives_even_when_no_asset_is_representable():
    rows = [parent(1, url=None), parent(2, kind="UNKNOWN"), parent(3)]
    a, _ = make(lambda req: httpx.Response(200, json={"data": rows}))
    result = await listing(a)
    observations = result.observations
    assert [o.parent_media_id for o in observations] == ["1", "2", "3"]
    assert [
        any(a.availability == "available" for a in o.assets) for o in observations
    ] == [False, False, True]
    assert observations[0].availability == "unavailable"
    assert observations[1].exclusion_reason == "unsupported_media"
    assert observations[0].like_count is None
    assert "never-persist" not in repr([asdict(o) for o in observations])
    assert len(result.candidates) == 1
    assert result.counters.observed_parents == 3


@pytest.mark.parametrize("kind, media_kind", [("IMAGE", "image"), ("VIDEO", "video")])
async def test_a_candidate_is_named_neutrally_and_the_graph_id_is_only_its_reference(
    kind, media_kind
):
    """The candidate is the shape the Drive sync port stores an item from, so its
    name is the one the catalog stores: never the Graph id, which is its `ref`."""
    a, _ = make(
        lambda req: httpx.Response(200, json={"data": [parent(17841400, kind=kind)]})
    )
    [candidate] = (await listing(a)).candidates
    assert candidate["kind"] == media_kind
    assert candidate["ref"] == "17841400"
    assert candidate["name"] == vocabulary.PAST_POSTS_ITEM_NAME == "Instagram post"
    assert "17841400" not in candidate["name"]


async def test_carousel_heavy_page_never_dispatches_twenty_first_request():
    calls = []

    def handler(req):
        calls.append(req.url.path)
        if req.url.path.endswith("/children"):
            return httpx.Response(200, json={"data": [parent(999)]})
        return httpx.Response(
            200, json={"data": [parent(i, "CAROUSEL_ALBUM") for i in range(1, 101)]}
        )

    a, _ = make(handler)
    result = await listing(a)
    assert len(calls) <= 20, "children and listing must share the same dispatch budget"
    assert result.counters.graph_attempts == len(calls)
    assert result.checkpoint["pending_parent"] == "20"
    assert result.checkpoint["more"]
    assert len(result.observations) == 19
    assert result.observations[0].assets[0].selected_asset_id == "999"
    assert result.candidates[0]["content_hash"] == past_posts.identity("999")


@pytest.mark.parametrize(
    "caption,classification",
    [
        (None, "unknown"),
        ("", "unknown"),
        ("   ", "unknown"),
        (123, "unknown"),
        ("Original words", "not_detected"),
        ("via @PrivateHandle", "credited"),
    ],
)
async def test_optional_caption_classifies_without_retaining_text_or_handles(
    caption, classification
):
    calls = []

    def handler(req):
        calls.append(req)
        if req.url.path.endswith("/media"):
            assert "caption" not in req.url.params["fields"]
            return httpx.Response(200, json={"data": [parent(1)]})
        return httpx.Response(200, json={"id": "1", "caption": caption})

    a, _ = make(handler, enrich=True)
    result = await listing(a)
    assert len(calls) == 2, "caption belongs to optional per-parent enrichment"
    o = result.observations[0]
    assert o.attribution == classification
    assert "PrivateHandle" not in repr(asdict(o))
    assert "caption" not in repr(result.candidates)


async def test_caption_field_refusal_keeps_base_parent_and_unknown_attribution():
    def handler(req):
        if req.url.path.endswith("/media"):
            return httpx.Response(200, json={"data": [parent(1)]})
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": 100,
                    "message": "Tried accessing nonexisting field (caption) on node type (IGMedia)",
                }
            },
        )

    a, _ = make(handler, enrich=True)
    result = await listing(a)
    assert result.observations[0].attribution == "unknown"
    assert result.diagnostics == ("caption_field_unsupported",)


async def test_authentication_failure_in_enrichment_is_never_unknown():
    def handler(req):
        if req.url.path.endswith("/media"):
            return httpx.Response(200, json={"data": [parent(1)]})
        return httpx.Response(
            400, json={"error": {"code": 190, "message": "secret-token"}}
        )

    a, _ = make(handler, enrich=True)
    with pytest.raises(MediaCredentialDead):
        await listing(a)


async def test_stable_parent_resume_uses_no_durable_cursor_or_provider_url():
    calls = []

    def handler(req):
        calls.append(req.url.path)
        if req.url.path.endswith("/media"):
            return httpx.Response(200, json={"data": [parent(1, "CAROUSEL_ALBUM")]})
        if req.url.path.endswith("/children"):
            return httpx.Response(200, json={"data": [parent(2)]})
        if req.url.params["fields"] == "id,caption":
            return httpx.Response(200, json={"id": "1", "caption": "Original"})
        return httpx.Response(200, json=parent(1, "CAROUSEL_ALBUM"))

    a, _ = make(handler, graph_limit=2, enrich=True)
    first = await listing(a)
    assert first.observations == ()
    assert first.checkpoint["pending_parent"] == "1"
    assert "never-persist" not in repr(first.checkpoint)
    b, _ = make(handler, graph_limit=4, enrich=True)
    second = await listing(b, first.checkpoint)
    assert second.observations[0].attribution == "not_detected"
    assert calls[1].endswith("/1")
    assert second.checkpoint["seen"] == 1


async def test_each_retry_is_admitted_and_counts_against_total_graph_budget():
    calls = []

    def handler(req):
        calls.append(req.url.path)
        raise httpx.ReadTimeout("private-provider-details", request=req)

    a, admission = make(handler, graph_limit=2)
    with pytest.raises(MediaRetryableError):
        await listing(a)
    assert len(calls) == 2
    assert admission.events == ["admit", "fence", "admit", "fence"]


@pytest.mark.parametrize("change", ["off", "off_on", "replacement"])
async def test_response_held_across_source_change_is_fenced_before_children(change):
    calls = []
    admission = Admission()

    def handler(req):
        calls.append(req.url.path)
        if change == "off":
            admission.enabled = False
        else:
            admission.epoch += 1
        return httpx.Response(200, json={"data": [parent(1, "CAROUSEL_ALBUM")]})

    a, _ = make(handler, admission=admission)
    with pytest.raises(MediaRetryableError, match="source_fence_drop"):
        await listing(a)
    assert len(calls) == 1
    assert admission.events == ["admit", "fence"]


async def test_off_at_second_dispatch_does_not_chain_child_request():
    calls = []
    admission = Admission()
    admission.reject_before = 2

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(200, json={"data": [parent(1, "CAROUSEL_ALBUM")]})

    a, _ = make(handler, admission=admission)
    with pytest.raises(MediaRetryableError, match="source_fence_drop"):
        await listing(a)
    assert len(calls) == 1


def test_past_posts_has_no_permissive_admission_fallback():
    with pytest.raises(MediaTerminalError, match="admission"):
        past_posts.InstagramPastPostsAdapter(
            access_provider=None, client=httpx.AsyncClient(), graph_version="v26.0"
        )


@pytest.mark.parametrize("version", [0, 3, 999, True])
async def test_unsupported_checkpoint_version_dispatches_nothing(version):
    calls = []
    a, _ = make(lambda req: calls.append(req) or httpx.Response(200, json={"data": []}))
    with pytest.raises(MediaTerminalError):
        await listing(a, {"v": version})
    assert calls == []


def test_redaction_preserves_the_typed_stale_drop_and_safe_work_counters():
    error = MediaSourceFenceDrop()
    cleaned = redacted_error(error)
    assert isinstance(cleaned, MediaSourceFenceDrop)
    assert cleaned.counters.source_fence_drop == 1


async def test_oversized_timestamp_group_reports_bounded_replay_not_false_completion():
    cp = {
        "v": 2,
        "upper": 1001,
        "until": 901,
        "window_start": 0,
        "boundary": [str(i) for i in range(1, 101)],
        "seen": 100,
        "more": True,
        "ceiling_reached": False,
    }
    original = cp.copy()
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(
            200,
            json={
                "data": [parent(i) for i in range(1, 101)],
                "paging": {"next": "ignored", "cursors": {"after": "transient-secret"}},
            },
        )

    a, _ = make(handler)
    with pytest.raises(MediaRetryableError) as caught:
        await listing(a, cp)
    assert cp == original
    assert len(calls) == 1
    assert getattr(caught.value, "reason", None) == "parent_replay_budget"
    assert caught.value.counters.listed_parents == 100
    assert caught.value.counters.observed_parents == 0
    assert "transient-secret" not in str(caught.value)


async def test_actual_ten_thousand_parent_fixture_stops_without_inventory_claim():
    now = 1_000_000
    rows = [
        {
            **parent(i),
            "timestamp": datetime.fromtimestamp(now - i, timezone.utc).isoformat(),
            "media_url": None if i % 3 == 0 else URL,
            "media_type": "UNKNOWN" if i % 7 == 0 else "IMAGE",
        }
        for i in range(1, 10_002)
    ]
    calls = []

    def handler(req):
        calls.append(req)
        until = int(req.url.params["until"])
        since = int(req.url.params["since"])
        eligible = [
            r for r in rows if since <= past_posts._timestamp(r["timestamp"]) < until
        ]
        after = int(req.url.params.get("after", "0"))
        limit = int(req.url.params["limit"])
        page = eligible[after : after + limit]
        body = {"data": page}
        if after + limit < len(eligible):
            body["paging"] = {
                "next": "ignored",
                "cursors": {"after": str(after + limit)},
            }
        return httpx.Response(200, json=body)

    a, _ = make(handler)
    a._clock = lambda: now
    cp = None
    observations = []
    for _ in range(110):
        result = await listing(a, cp)
        cp = result.checkpoint
        assert result.counters.graph_attempts <= 20
        assert result.counters.listed_parents <= 100
        observations.extend(result.observations)
        if not cp["more"]:
            break
    assert len(observations) == len({o.parent_media_id for o in observations}) == 10_000
    assert any(
        not any(a.availability == "available" for a in o.assets) for o in observations
    )
    assert cp["ceiling_reached"] and cp["seen"] == 10_000
    assert not a.checkpoint_whole(cp)
    assert "cursor" not in repr(cp) and "never-persist" not in repr(cp)
    assert len(calls) <= 110


@pytest.mark.parametrize("limit", [101, 1000, True, 0])
def test_owner_injection_cannot_loosen_parent_security_ceiling(limit):
    with pytest.raises(ValueError):
        make(lambda req: httpx.Response(200), parent_limit=limit)


@pytest.mark.parametrize("limit", [21, 1000, True, 0])
def test_owner_injection_cannot_loosen_graph_security_ceiling(limit):
    with pytest.raises(ValueError):
        make(lambda req: httpx.Response(200), graph_limit=limit)


async def test_index_only_parent_is_later_represented_with_same_parent_identity():
    a, _ = make(lambda req: httpx.Response(200, json={"data": [parent(1, url=None)]}))
    index = await listing(a)
    b, _ = make(lambda req: httpx.Response(200, json={"data": [parent(1)]}))
    represented = await listing(b)
    assert (
        index.observations[0].parent_media_id
        == represented.observations[0].parent_media_id
    )
    assert index.observations[0].assets[0].availability == "unavailable"
    assert represented.candidates[0]["content_hash"] == past_posts.identity("1")


@pytest.mark.parametrize(
    "body",
    [
        {"data": [{"id": "1"}]},
        {"data": [parent(1), {"id": "2"}]},
        {"data": ["raw-provider-text"]},
    ],
)
async def test_partial_parent_response_produces_no_complete_bundle(body):
    a, _ = make(lambda req: httpx.Response(200, json=body))
    with pytest.raises((MediaRetryableError, MediaTerminalError)):
        await listing(a)


# Seven dispatches: the parent, `/me`, the child page and a refused CDN read, then
# the retry's parent, child page and CDN read. The turn proves ownership at `/me`
# once, so the retry does not read it again.
@pytest.mark.parametrize("dispatch", range(1, 8))
async def test_off_at_every_fetch_dispatch_covers_owner_child_and_cdn_retry(dispatch):
    admission = Admission()
    admission.reject_before = dispatch
    calls = []
    cdn = 0

    def handler(req):
        nonlocal cdn
        calls.append(req.url.path)
        if req.url.host != "graph.instagram.com":
            cdn += 1
            return (
                httpx.Response(403)
                if cdn == 1
                else httpx.Response(
                    200, content=b"jpeg", headers={"content-type": "image/jpeg"}
                )
            )
        if req.url.path.endswith("/me"):
            return httpx.Response(200, json={"id": "555", "user_id": ACCOUNT})
        if req.url.path.endswith("/children"):
            return httpx.Response(200, json={"data": [parent(2)]})
        return httpx.Response(
            200, json={**parent(1, "CAROUSEL_ALBUM"), "owner": {"id": "555"}}
        )

    a, _ = make(handler, admission=admission)
    with pytest.raises(MediaSourceFenceDrop) as caught:
        await a.fetch_bytes({}, "1", source_id=SOURCE, workspace_id=WS, max_bytes=100)
    assert len(calls) == dispatch - 1
    assert caught.value.counters.source_fence_drop == 1
    assert "never-persist" not in str(caught.value)


@pytest.mark.parametrize("change", ["off", "off_on", "replacement"])
async def test_held_cdn_bytes_are_never_served_after_source_change(change):
    admission = Admission()
    calls = []

    def handler(req):
        calls.append(req)
        if req.url.host == "graph.instagram.com":
            return httpx.Response(200, json=parent(1))
        if change == "off":
            admission.enabled = False
        else:
            admission.epoch += 1
        return httpx.Response(
            200, content=b"private-stale-bytes", headers={"content-type": "image/jpeg"}
        )

    a, _ = make(handler, admission=admission)
    with pytest.raises(MediaSourceFenceDrop):
        await a.fetch_bytes({}, "1", source_id=SOURCE, workspace_id=WS, max_bytes=100)
    assert len(calls) == 2


async def test_same_host_redirect_is_admitted_at_each_actual_transmission():
    events = []

    class Guard:
        async def before_dispatch(self):
            events.append("admit")
            return 1

        async def after_return(self, ticket):
            assert ticket == 1
            events.append("fence")

    def handler(req):
        events.append("dispatch")
        return (
            httpx.Response(302, headers={"location": "/next"})
            if req.url.path == "/first"
            else httpx.Response(200, content=b"ok")
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await egress.request(
            client,
            "GET",
            "https://graph.instagram.com/first",
            policy=egress.EgressPolicy().without(enforce_private_address_block=False),
            admission=Guard(),
        )
    assert result.content == b"ok"
    assert events == ["admit", "dispatch", "fence"] * 2


async def test_admission_transaction_must_close_before_actual_dispatch():
    from src.services.target.unit_of_work import (
        transaction_discipline,
        TransactionDisciplineError,
    )

    calls = []
    manager = transaction_discipline()

    class Leaky:
        async def before_dispatch(self):
            manager.__enter__()
            return 1

        async def after_return(self, ticket):
            pass

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: calls.append(req) or httpx.Response(200)
        )
    ) as client:
        try:
            with pytest.raises(TransactionDisciplineError):
                await egress.request(
                    client,
                    "GET",
                    "https://graph.instagram.com/first",
                    policy=egress.EgressPolicy().without(
                        enforce_private_address_block=False
                    ),
                    admission=Leaky(),
                )
        finally:
            manager.__exit__(None, None, None)
    assert calls == []


async def test_closed_short_admission_transaction_does_not_span_provider_io():
    from src.services.target.unit_of_work import transaction_discipline

    events = []

    class Guard:
        async def before_dispatch(self):
            with transaction_discipline():
                assert in_transaction()
                events.append("transaction")
            assert not in_transaction()
            events.append("closed")
            return 1

        async def after_return(self, ticket):
            assert not in_transaction()
            events.append("fence")

    def handler(req):
        assert not in_transaction()
        events.append("dispatch")
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await egress.request(
            client,
            "GET",
            "https://graph.instagram.com/first",
            policy=egress.EgressPolicy().without(enforce_private_address_block=False),
            admission=Guard(),
        )
    assert events == ["transaction", "closed", "dispatch", "fence"]


async def test_transient_enrichment_resumes_by_id_and_keeps_previous_complete_parent():
    calls = []
    retry = True

    def handler(req):
        nonlocal retry
        calls.append(req.url.path)
        if req.url.path.endswith("/media"):
            return httpx.Response(200, json={"data": [parent(1), parent(2)]})
        if req.url.params["fields"] == "id,caption":
            if req.url.path.endswith("/2") and retry:
                retry = False
                return httpx.Response(
                    429, json={"error": {"code": 4, "message": "do-not-retain"}}
                )
            return httpx.Response(
                200, json={"id": req.url.path.rsplit("/", 1)[1], "caption": "Original"}
            )
        return httpx.Response(200, json=parent(2))

    a, _ = make(handler, enrich=True)
    first = await listing(a)
    assert [o.parent_media_id for o in first.observations] == ["1"]
    assert first.checkpoint["pending_parent"] == "2" and first.checkpoint["seen"] == 1
    second = await listing(a, first.checkpoint)
    assert [o.parent_media_id for o in second.observations] == ["2"]
    assert second.checkpoint["seen"] == 2
    assert calls == [
        f"/v26.0/{ACCOUNT}/media",
        "/v26.0/1",
        "/v26.0/2",
        "/v26.0/2",
        "/v26.0/2",
    ]
    assert "do-not-retain" not in repr([first, second])


def test_safe_source_fence_reason_survives_redaction():
    fault = MediaSourceFenceDrop(reason="source_epoch_changed")
    assert fault.reason == "source_epoch_changed"
    assert redacted_error(fault).reason == "source_epoch_changed"
    with pytest.raises(ValueError):
        MediaSourceFenceDrop(reason="private-provider-url")


@pytest.mark.parametrize("selected,matches", [("11", False), ("12", True)])
async def test_validation_fetch_fences_changed_selected_child_before_cdn(
    selected, matches
):
    calls = []

    def handler(req):
        calls.append(req.url.path)
        if req.url.host != "graph.instagram.com":
            assert matches
            return httpx.Response(
                200, content=b"jpeg", headers={"content-type": "image/jpeg"}
            )
        if req.url.path.endswith("/children"):
            return httpx.Response(200, json={"data": [parent(12)]})
        return httpx.Response(200, json=parent(1, "CAROUSEL_ALBUM"))

    a, _ = make(handler)

    async def read():
        return await a.fetch_bytes(
            {},
            "1",
            source_id=SOURCE,
            workspace_id=WS,
            max_bytes=100,
            selected_asset_id=selected,
            expected_slide_count=2,
        )

    if matches:
        assert (await read())[0] == b"jpeg"
        assert len(calls) == 3
    else:
        with pytest.raises(
            MediaSelectedAssetGone, match="selected_asset_gone"
        ) as caught:
            await read()
        assert caught.value.reason == "selected_asset_gone"
        assert type(redacted_error(caught.value)) is type(caught.value)
        # The first read lacked the slide, so the parent and its children were
        # read again before the slide was judged gone.
        assert len(calls) == 4


# Absence of a required kind is incomplete evidence, not exclusion.
@pytest.mark.parametrize(
    "bad_kind", [None, True, 17, [], {}, "", " ", "\x00", " VIDEO", "video"]
)
@pytest.mark.parametrize("where", ["parent", "child"])
async def test_incomplete_required_media_kind_never_replaces_a_complete_observation(
    where, bad_kind
):
    original = parent(1)

    def complete(req):
        return httpx.Response(200, json={"data": [original]})

    a, _ = make(complete)
    previous = await listing(a)
    held = previous.observations[0]

    def partial(req):
        if req.url.path.endswith("/children"):
            return httpx.Response(
                200, json={"data": [{**parent(2), "media_type": bad_kind}]}
            )
        row = (
            {**parent(1), "media_type": bad_kind}
            if where == "parent"
            else parent(1, "CAROUSEL_ALBUM")
        )
        return httpx.Response(200, json={"data": [row]})

    b, _ = make(partial)
    result = await listing(b)
    assert result.observations == ()
    assert result.candidates == []
    assert result.counters.observed_parents == 0
    assert result.checkpoint["seen"] == 0
    assert result.checkpoint["pending_parent"] == "1"
    assert result.checkpoint["upper"] == result.checkpoint["until"] == 1001
    assert result.checkpoint["more"] is True
    assert (
        held.assets[0].availability == "available" and held.availability == "available"
    )


@pytest.mark.parametrize("where", ["parent", "child"])
async def test_absent_required_media_kind_yields_stable_parent_without_coverage(where):
    def handler(req):
        if req.url.path.endswith("/children"):
            return httpx.Response(200, json={"data": [{"id": "2", "media_url": URL}]})
        row = (
            parent(1, "CAROUSEL_ALBUM")
            if where == "child"
            else {k: v for k, v in parent(1).items() if k != "media_type"}
        )
        return httpx.Response(200, json={"data": [row]})

    a, _ = make(handler)
    result = await listing(a)
    assert result.observations == () and result.checkpoint["seen"] == 0
    assert result.checkpoint["pending_parent"] == "1"
    assert result.outcome == "stable_parent_pending"


@pytest.mark.parametrize("where", ["parent", "child"])
async def test_explicit_unknown_kind_remains_complete_excluded_coverage(where):
    def handler(req):
        if req.url.path.endswith("/children"):
            return httpx.Response(200, json={"data": [parent(2, "FUTURE_MEDIA_TYPE")]})
        return httpx.Response(
            200,
            json={
                "data": [
                    parent(
                        1, "CAROUSEL_ALBUM" if where == "child" else "FUTURE_MEDIA_TYPE"
                    )
                ]
            },
        )

    a, _ = make(handler)
    result = await listing(a)
    assert len(result.observations) == 1
    assert result.observations[0].exclusion_reason == "unsupported_media"
    assert result.observations[0].availability == "excluded"
    assert result.checkpoint["seen"] == 1


@pytest.mark.parametrize(
    "form", ["missing_parent_url", "missing_child_url", "empty_children"]
)
async def test_complete_missing_url_and_empty_child_responses_keep_index_only_coverage(
    form,
):
    def handler(req):
        if req.url.path.endswith("/children"):
            return httpx.Response(
                200,
                json={
                    "data": []
                    if form == "empty_children"
                    else [{k: v for k, v in parent(2).items() if k != "media_url"}]
                },
            )
        row = (
            {k: v for k, v in parent(1).items() if k != "media_url"}
            if form == "missing_parent_url"
            else parent(1, "CAROUSEL_ALBUM")
        )
        return httpx.Response(200, json={"data": [row]})

    a, _ = make(handler)
    result = await listing(a)
    assert len(result.observations) == 1 and result.checkpoint["seen"] == 1
    observation = result.observations[0]
    assert not any(a.availability == "available" for a in observation.assets)
    if form == "empty_children":
        # A carousel read that lists no slides proves nothing about them: the
        # post lands as one not yet read, so the index keeps what it holds.
        assert (
            observation.availability,
            observation.exclusion_reason,
            observation.slide_count,
            observation.slides_complete,
        ) == ("available", "incomplete_observation", 0, False)
    else:
        assert observation.availability == "unavailable"
        assert observation.exclusion_reason == "media_unavailable"


def resumed_checkpoint():
    return {
        "v": 2,
        "upper": 1001,
        "until": 901,
        "window_start": 0,
        "seen": 1,
        "boundary": ["1"],
        "more": True,
        "ceiling_reached": False,
    }


# A resumed v2 payload cannot mint replacement frozen bounds.
@pytest.mark.parametrize(
    "missing",
    ["upper", "until", "window_start", "boundary", "seen", "more", "ceiling_reached"],
)
async def test_resumed_v2_checkpoint_requires_every_durable_progress_field_before_dispatch(
    missing,
):
    cp = resumed_checkpoint()
    cp.pop(missing)
    original = cp.copy()
    calls = []
    a, _ = make(lambda req: calls.append(req) or httpx.Response(200, json={"data": []}))
    a._clock = lambda: 2000
    with pytest.raises(MediaTerminalError, match="watermark"):
        await listing(a, cp)
    assert cp == original and calls == []


@pytest.mark.parametrize(
    "progress",
    [
        {"seen": 1},
        {"seen": 0},
        {"boundary": ["1"]},
        {"boundary": []},
        {"more": True},
        {"more": False},
        {"ceiling_reached": False},
        {"pending_parent": "1", "pending_at": 900},
    ],
)
async def test_v2_partial_progress_cannot_regenerate_bounds(progress):
    calls = []
    cp = {"v": 2, **progress}
    original = cp.copy()
    a, _ = make(lambda req: calls.append(req) or httpx.Response(200, json={"data": []}))
    with pytest.raises(MediaTerminalError):
        await listing(a, cp)
    assert calls == [] and cp == original


@pytest.mark.parametrize(
    "changes",
    [
        {"seen": 0, "boundary": ["1"]},
        {"boundary": ["1", "1"]},
        {"boundary": ["1", "2"]},
        {"ceiling_reached": True},
        {"seen": 10000, "ceiling_reached": True, "more": True},
        {"pending_parent": "2", "pending_at": 900, "more": False},
        {"pending_parent": "2"},
        {"pending_at": 900},
        {"walk": True, "started_at": "2026-10-11T00:00:00Z"},
        {"walk": "carrier"},
        {"started_at": "2026-10-11T00:00:00Z"},
        {"walk": "carrier", "started_at": True},
        {"walk": "carrier", "started_at": "not-a-time"},
        {"walk": "carrier", "started_at": "2026-10-11T00:00:00"},
        {
            "walk": "carrier",
            "started_at": "2026-10-11T00:00:00Z",
            "last_whole_started_at": {},
        },
    ],
)
async def test_resumed_checkpoint_rejects_inconsistent_progress_and_reserved_fields(
    changes,
):
    cp = {**resumed_checkpoint(), **changes}
    original = cp.copy()
    calls = []
    a, _ = make(
        lambda req: calls.append(req) or httpx.Response(200, json={"data": [parent(2)]})
    )
    with pytest.raises(MediaTerminalError):
        await listing(a, cp)
    assert calls == [] and cp == original


@pytest.mark.parametrize(
    "initial",
    [
        {},
        {"v": 1},
        {"v": 2},
        {"v": 2, "walk": "carrier", "started_at": "2026-10-11T00:00:00Z"},
    ],
)
async def test_valid_initial_checkpoint_can_freeze_a_new_upper_bound(initial):
    calls = []
    a, _ = make(lambda req: calls.append(req) or httpx.Response(200, json={"data": []}))
    result = await listing(a, initial)
    assert result.checkpoint["upper"] == 1001
    assert result.checkpoint["seen"] == 0
    assert len(calls) == 1


@pytest.mark.parametrize("version", [None, 1, 2])
async def test_valid_resume_preserves_frozen_upper_after_clock_changes(version):
    cp = resumed_checkpoint()
    if version is None:
        cp.pop("v")
    else:
        cp["v"] = version
    cp.update(
        walk="carrier",
        started_at="2026-10-11T00:00:00Z",
        last_whole_started_at="2026-10-10T00:00:00Z",
    )
    requests = []
    a, _ = make(
        lambda req: (
            requests.append(req) or httpx.Response(200, json={"data": [parent(2)]})
        )
    )
    a._clock = lambda: 2000
    result = await listing(a, cp)
    assert requests[0].url.params["until"] == "901"
    assert result.checkpoint["upper"] == 1001 and result.checkpoint["seen"] == 2
    assert result.checkpoint["v"] == 2


async def test_documented_legacy_partial_watermark_remains_compatible():
    a, _ = make(lambda req: httpx.Response(200, json={"data": [parent(2)]}))
    result = await listing(a, {"v": 1, "until": 901, "seen": 1, "boundary": ["1"]})
    assert result.checkpoint["upper"] == 1001 and result.checkpoint["seen"] == 2


@pytest.mark.parametrize("invalid", [[], 0, False, "", [("v", 2)]])
async def test_nonmapping_checkpoint_cannot_impersonate_an_empty_initial_state(invalid):
    calls = []
    a, _ = make(lambda req: calls.append(req) or httpx.Response(200, json={"data": []}))
    with pytest.raises(MediaTerminalError):
        await listing(a, invalid)
    assert calls == []


@pytest.mark.parametrize("initial", [False, True])
async def test_last_whole_reserved_timestamp_requires_its_carrier_pair(initial):
    cp = {"v": 2} if initial else resumed_checkpoint()
    cp["last_whole_started_at"] = "2026-10-10T00:00:00Z"
    calls = []
    a, _ = make(lambda req: calls.append(req) or httpx.Response(200, json={"data": []}))
    with pytest.raises(MediaTerminalError):
        await listing(a, cp)
    assert calls == []


async def targeted(a, mid="1"):
    port = getattr(a, "enrich_parents", None)
    assert callable(port), (
        "past-posts source requires an independent targeted stable-parent enrichment port"
    )
    return await port([mid], source_id=SOURCE, workspace_id=WS)


async def test_metadata_only_page_defers_all_recent_carousels_and_captions():
    calls = []
    rows = [parent(i, "CAROUSEL_ALBUM" if i % 2 else "IMAGE") for i in range(1, 26)]

    def handler(req):
        calls.append(req)
        if req.url.path.endswith("/children"):
            return httpx.Response(200, json={"data": [parent(999)]})
        if req.url.path.endswith("/media"):
            assert "caption" not in req.url.params["fields"]
            return httpx.Response(200, json={"data": rows})
        return httpx.Response(
            200,
            json={
                "id": req.url.path.rsplit("/", 1)[1],
                "caption": "via @private-author",
            },
        )

    a, admission = make(handler, enrich=True, metadata_only=True, size=25)
    result = await listing(a)
    assert len(calls) == result.counters.graph_attempts == 1
    assert len(result.observations) == result.checkpoint["seen"] == 25
    assert result.outcome == "metadata_page"
    assert admission.events == ["admit", "fence"]
    assert "pending_parent" not in result.checkpoint
    for o in result.observations:
        assert o.attribution == "unknown"
        if int(o.parent_media_id) % 2:
            assert o.assets == () and o.slide_count == 0
            assert (
                o.availability == "available"
                and o.exclusion_reason == "incomplete_observation"
            )
        else:
            assert o.assets[0].media_kind == "image"
    assert "private-author" not in repr(result)
    assert "never-persist" not in repr(result)


async def test_default_eager_compatibility_still_shares_twenty_attempt_budget():
    calls = []

    def handler(req):
        calls.append(req)
        if req.url.path.endswith("/media"):
            return httpx.Response(200, json={"data": [parent(i) for i in range(1, 26)]})
        return httpx.Response(
            200, json={"id": req.url.path.rsplit("/", 1)[1], "caption": "Original"}
        )

    a, _ = make(handler, enrich=True, size=25)
    result = await listing(a)
    assert len(calls) == 20 and result.checkpoint["pending_parent"] == "20"
    assert len(result.observations) == 19


@pytest.mark.parametrize("bad", [None, True, {}, ""])
async def test_metadata_only_mode_does_not_turn_incomplete_types_into_coverage(bad):
    a, _ = make(
        lambda req: httpx.Response(200, json={"data": [parent(1, bad)]}),
        metadata_only=True,
    )
    result = await listing(a)
    assert result.observations == () and result.checkpoint["seen"] == 0
    assert result.checkpoint["pending_parent"] == "1"


@pytest.mark.parametrize("bad", [None, 1, 0, "yes"])
def test_metadata_only_constructor_requires_an_explicit_boolean(bad):
    with pytest.raises(ValueError):
        make(lambda req: httpx.Response(200), metadata_only=bad)


@pytest.mark.parametrize(
    "caption,state",
    [
        (None, "unknown"),
        ("", "unknown"),
        (123, "unknown"),
        ("Original", "not_detected"),
        ("via @private-author", "credited"),
    ],
)
@pytest.mark.parametrize("eager_flag", [False, True])
async def test_targeted_combined_parent_caption_read_is_one_request_and_no_scan_advance(
    caption, state, eager_flag
):
    calls = []

    def handler(req):
        calls.append(req)
        assert req.url.params["fields"] == past_posts._FIELDS + ",owner,caption"
        return httpx.Response(
            200, json={**parent(1), "caption": caption, "like_count": 5}
        )

    a, admission = make(handler, enrich=eager_flag, metadata_only=True)
    a._clock = lambda: (_ for _ in ()).throw(
        AssertionError("targeted read must not mint scan bounds")
    )
    result = await targeted(a)
    assert len(calls) == result.counters.graph_attempts == 1
    assert result.counters.observed_parents == result.counters.listed_parents == 1
    assert result.checkpoint == {} and result.outcome == "parent_enriched"
    assert result.observations[0].attribution == state
    assert result.observations[0].like_count == 5
    assert result.candidates[0]["content_hash"] == past_posts.identity("1")
    assert admission.events == ["admit", "fence"]
    assert "private-author" not in repr(result) and "never-persist" not in repr(result)


async def test_targeted_caption_field_refusal_uses_counted_owner_base_fallback():
    requests = []

    def handler(req):
        requests.append(req)
        if "caption" in req.url.params["fields"]:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "code": 100,
                        "message": "Tried accessing nonexisting field (caption) on node type (IGMedia)",
                    }
                },
            )
        assert req.url.params["fields"] == past_posts._FIELDS + ",owner"
        return httpx.Response(200, json=parent(1))

    a, _ = make(handler, metadata_only=True)
    result = await targeted(a)
    assert len(requests) == result.counters.graph_attempts == 2
    assert result.observations[0].attribution == "unknown"
    assert result.diagnostics == ("caption_field_unsupported",)
    assert result.outcome == "parent_enriched" and result.checkpoint == {}


@pytest.mark.parametrize("status,code", [(400, 190), (401, 100)])
async def test_targeted_caption_auth_failure_is_not_downgraded_or_fallback(
    status, code
):
    requests = []

    def handler(req):
        requests.append(req)
        return httpx.Response(
            status,
            json={
                "error": {
                    "code": code,
                    "message": "Tried accessing nonexisting field (caption) with private-grant",
                }
            },
        )

    a, _ = make(handler, metadata_only=True)
    with pytest.raises(MediaCredentialDead):
        await targeted(a)
    assert len(requests) == 1


async def test_unrecognized_targeted_field_refusal_does_not_become_unknown():
    a, _ = make(
        lambda req: httpx.Response(
            400,
            json={
                "error": {
                    "code": 100,
                    "message": "Tried accessing nonexisting field (owner)",
                }
            },
        ),
        metadata_only=True,
    )
    result = await targeted(a)
    assert result.failed == ("1",) and result.deferred == ()
    assert result.observations == () and result.outcome == "parent_enrichment_pending"


@pytest.mark.parametrize("scope,limit,expected", [(False, 1, 1), (True, 2, 2)])
async def test_targeted_identity_proof_is_counted_and_not_skipped(
    scope, limit, expected
):
    requests = []

    def handler(req):
        requests.append(req)
        if req.url.path.endswith("/me"):
            return httpx.Response(200, json={"id": "555", "user_id": ACCOUNT})
        return httpx.Response(
            200,
            json={
                **parent(1),
                "owner": {"id": "555" if scope else ACCOUNT},
                "caption": "Original",
            },
        )

    a, _ = make(handler, metadata_only=True, graph_limit=limit)
    result = await targeted(a)
    assert (
        result.outcome == "parent_enriched"
        and result.observations[0].attribution == "not_detected"
    )
    assert len(requests) == result.counters.graph_attempts == expected


@pytest.mark.parametrize("fault", ["owner", "type", "timestamp", "identity", "child"])
async def test_targeted_partial_or_unowned_parent_does_not_create_a_complete_bundle(
    fault,
):
    def handler(req):
        if req.url.path.endswith("/children"):
            return httpx.Response(200, json={"data": [{"id": "2"}]})
        body = {
            **parent(1, "CAROUSEL_ALBUM" if fault == "child" else "IMAGE"),
            "caption": "Original",
        }
        if fault == "owner":
            body["owner"] = None
        if fault == "type":
            body.pop("media_type")
        if fault == "timestamp":
            body.pop("timestamp")
        if fault == "identity":
            body["id"] = "999"
        return httpx.Response(200, json=body)

    a, _ = make(handler, metadata_only=True)
    result = await targeted(a)
    assert result.observations == () and result.candidates == []
    assert result.outcome == "parent_enrichment_pending" and result.checkpoint == {}
    assert result.counters.observed_parents == 0
    # An unowned parent is refused for good; the others wait for a retry.
    assert (result.failed, result.deferred) == (
        (("1",), ()) if fault == "owner" else ((), ("1",))
    )


@pytest.mark.parametrize("bad_id", [None, "bad-id", True])
async def test_malformed_later_child_identity_is_retryable_and_never_fetches_cdn(
    bad_id,
):
    requests = []

    def handler(req):
        requests.append(req)
        if req.url.path.endswith("/children"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "2", "media_type": "IMAGE", "media_url": URL},
                        {"id": bad_id, "media_type": "IMAGE", "media_url": URL},
                    ]
                },
            )
        return httpx.Response(
            200, json={**parent(1, "CAROUSEL_ALBUM"), "caption": "Original"}
        )

    a, _ = make(handler, metadata_only=True)
    result = await targeted(a)
    assert result.observations == () and result.candidates == []
    assert result.failed == () and result.deferred == ("1",)
    assert [req.url.path for req in requests] == ["/v26.0/1", "/v26.0/1/children"]
    assert all(req.url.host == "graph.instagram.com" for req in requests)


async def test_targeted_carousel_materializes_only_first_child_after_metadata_defer():
    requests = []

    def handler(req):
        requests.append(req)
        if req.url.path.endswith("/media"):
            return httpx.Response(200, json={"data": [parent(1, "CAROUSEL_ALBUM")]})
        if req.url.path.endswith("/children"):
            return httpx.Response(200, json={"data": [parent(11), parent(12)]})
        return httpx.Response(
            200, json={**parent(1, "CAROUSEL_ALBUM"), "caption": "Original"}
        )

    a, _ = make(handler, metadata_only=True)
    metadata = await listing(a)
    assert metadata.observations[0].assets == () and len(requests) == 1
    enriched = await targeted(a)
    assert enriched.observations[0].assets[0].selected_asset_id == "11"
    assert len(enriched.observations[0].assets) == 2
    assert [c["content_hash"] for c in enriched.candidates] == [
        past_posts.identity("11"),
        past_posts.identity("12"),
    ]
    assert enriched.checkpoint == {} and metadata.checkpoint["seen"] == 1
    assert enriched.counters.graph_attempts == 2


@pytest.mark.parametrize("limit,scope,carousel", [(1, True, False), (1, False, True)])
async def test_targeted_turn_yields_before_unfittable_owner_or_child_helper(
    limit, scope, carousel
):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(
            200,
            json={
                **parent(1, "CAROUSEL_ALBUM" if carousel else "IMAGE"),
                "owner": {"id": "555" if scope else ACCOUNT},
                "caption": "Original",
            },
        )

    a, _ = make(handler, metadata_only=True, graph_limit=limit)
    result = await targeted(a)
    assert result.outcome == "parent_enrichment_pending" and result.observations == ()
    assert result.checkpoint == {} and len(calls) == result.counters.graph_attempts == 1


@pytest.mark.parametrize("failure", ["429", "timeout"])
async def test_targeted_transient_failures_keep_stable_id_work_pending(failure):
    calls = []

    def handler(req):
        calls.append(req)
        if failure == "timeout":
            raise httpx.ReadTimeout("private-details", request=req)
        return httpx.Response(
            429, json={"error": {"code": 4, "message": "private-details"}}
        )

    a, _ = make(handler, metadata_only=True)
    result = await targeted(a)
    assert result.outcome == "parent_enrichment_pending" and result.observations == ()
    assert (
        result.counters.graph_attempts
        == len(calls)
        == (3 if failure == "timeout" else 1)
    )
    assert "private-details" not in repr(result)


@pytest.mark.parametrize("dispatch", range(1, 5))
async def test_targeted_all_fallback_owner_child_dispatches_require_admission(dispatch):
    admission = Admission()
    admission.reject_before = dispatch
    calls = []

    def handler(req):
        calls.append(req)
        if req.url.path.endswith("/me"):
            return httpx.Response(200, json={"id": "555", "user_id": ACCOUNT})
        if req.url.path.endswith("/children"):
            return httpx.Response(200, json={"data": [parent(2)]})
        if "caption" in req.url.params["fields"]:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "code": 100,
                        "message": "Tried accessing nonexisting field (caption)",
                    }
                },
            )
        return httpx.Response(
            200, json={**parent(1, "CAROUSEL_ALBUM"), "owner": {"id": "555"}}
        )

    a, _ = make(handler, metadata_only=True, admission=admission)
    with pytest.raises(MediaSourceFenceDrop):
        await targeted(a)
    assert len(calls) == dispatch - 1


@pytest.mark.parametrize("stage", ["combined", "owner", "child"])
@pytest.mark.parametrize("change", ["off", "off_on", "replacement"])
async def test_targeted_held_results_are_fenced_before_another_helper_or_bundle(
    stage, change
):
    admission = Admission()
    calls = []

    def handler(req):
        calls.append(req)
        current = (
            "owner"
            if req.url.path.endswith("/me")
            else "child"
            if req.url.path.endswith("/children")
            else "combined"
        )
        if current == stage:
            if change == "off":
                admission.enabled = False
            else:
                admission.epoch += 1
        if current == "owner":
            return httpx.Response(200, json={"id": "555", "user_id": ACCOUNT})
        if current == "child":
            return httpx.Response(200, json={"data": [parent(2)]})
        return httpx.Response(
            200,
            json={
                **parent(1, "CAROUSEL_ALBUM"),
                "owner": {"id": "555"},
                "caption": "Original",
            },
        )

    a, _ = make(handler, metadata_only=True, admission=admission)
    with pytest.raises(MediaSourceFenceDrop):
        await targeted(a)
    assert len(calls) == {"combined": 1, "owner": 2, "child": 3}[stage]


async def test_metadata_then_one_targeted_image_has_feasible_first_read_budget():
    # Transport arithmetic only, not age policy, validation or card proof.
    n = 500
    recent = n // 10
    now = 10_000_000
    calls = []
    rows = [
        {
            **parent(i, "CAROUSEL_ALBUM" if i <= recent else "IMAGE"),
            "timestamp": datetime.fromtimestamp(now - i * 2, timezone.utc).isoformat(),
        }
        for i in range(1, n + 1)
    ]
    selected = str(recent + 1)

    def handler(req):
        calls.append(req)
        if req.url.host != "graph.instagram.com":
            return httpx.Response(
                200, content=b"jpeg", headers={"content-type": "image/jpeg"}
            )
        if req.url.path.endswith("/media"):
            until = int(req.url.params["until"])
            size = int(req.url.params["limit"])
            eligible = [
                r for r in rows if past_posts._timestamp(r["timestamp"]) < until
            ]
            page = eligible[:size]
            return httpx.Response(
                200, json={"data": page, "paging": {"next": "ignored"}}
            )
        assert req.url.path.endswith("/" + selected)
        return httpx.Response(200, json={**rows[recent], "caption": "Original"})

    a, _ = make(handler, metadata_only=True, enrich=True, size=25)
    a._clock = lambda: now
    cp = None
    seen = 0
    while seen <= recent:
        page = await listing(a, cp)
        cp = page.checkpoint
        seen = cp["seen"]
    enriched = await targeted(a, selected)
    assert enriched.observations[0].attribution == "not_detected"
    for _ in range(2):
        await a.fetch_bytes(
            {},
            selected,
            source_id=SOURCE,
            workspace_id=WS,
            max_bytes=100,
            selected_asset_id=selected,
        )
    graph = sum(req.url.host == "graph.instagram.com" for req in calls)
    cdn = len(calls) - graph
    assert graph == 6 and graph <= ((recent + 24) // 25) + 16
    assert cdn == 2 and cdn <= 4
    # Each resumed page overlaps its prior timestamp boundary by one parent.
    assert seen == 73 and seen <= n // 4


@pytest.mark.parametrize("cp", [None, {}, resumed_checkpoint()])
def test_public_checkpoint_normalizer_is_pure_and_preserves_the_single_parser(cp):
    calls = []
    a, _ = make(lambda req: calls.append(req) or httpx.Response(200))
    port = getattr(a, "normalize_checkpoint", None)
    assert callable(port), "D must share the sole past-posts checkpoint parser"
    result = port(cp)
    assert result == a._checkpoint(cp) and result["v"] == 2
    assert calls == []
    if cp and "upper" in cp:
        assert result["upper"] == cp["upper"]


@pytest.mark.parametrize(
    "cp", [{"v": 2, "seen": 1}, {"v": 999}, False, {"v": 2, "walk": "carrier"}]
)
def test_public_normalizer_refuses_bad_unused_lane_checkpoints_before_dispatch(cp):
    calls = []
    a, _ = make(lambda req: calls.append(req) or httpx.Response(200))
    port = getattr(a, "normalize_checkpoint", None)
    assert callable(port), "D must share the sole past-posts checkpoint parser"
    with pytest.raises(MediaTerminalError):
        port(cp)
    assert calls == []


@pytest.mark.parametrize(
    "case,graph,cdn",
    [
        ("success", 1, 1),
        ("cdn_retry", 1, 2),
        ("expired", 2, 2),
        ("graph_retry", 2, 1),
        ("terminal", 1, 0),
        ("fence", 1, 0),
        ("cancel", 1, 1),
    ],
)
async def test_fetch_final_counters_report_actual_dispatches_without_changing_return(
    case, graph, cdn
):
    calls, snapshots = [], []
    admission = Admission()
    cancelled = asyncio.CancelledError("test cancellation")

    async def handler(req):
        calls.append(req)
        is_graph = req.url.host == "graph.instagram.com"
        if is_graph:
            if case == "graph_retry" and len(calls) == 1:
                raise httpx.ReadTimeout("test", request=req)
            if case == "terminal":
                return httpx.Response(400, json={"error": {"code": 100}})
            if case == "fence":
                admission.enabled = False
            return httpx.Response(200, json=parent(1))
        if case == "cancel":
            raise cancelled
        if len(calls) == 2:
            if case == "cdn_retry":
                raise httpx.ReadTimeout("test", request=req)
            if case == "expired":
                return httpx.Response(403)
        return httpx.Response(
            200, content=b"jpeg", headers={"content-type": "image/jpeg"}
        )

    a, _ = make(handler, admission=admission)
    kwargs = dict(source_id=SOURCE, workspace_id=WS, max_bytes=100)
    if case in {"terminal", "fence", "cancel"}:
        expected = {
            "terminal": MediaTerminalError,
            "fence": MediaSourceFenceDrop,
            "cancel": asyncio.CancelledError,
        }[case]
        with pytest.raises(expected) as caught:
            await a.fetch_bytes({}, "1", **kwargs, on_counters=snapshots.append)
        if case == "cancel":
            assert caught.value is cancelled
    else:
        result = await a.fetch_bytes({}, "1", **kwargs, on_counters=snapshots.append)
        assert result == (b"jpeg", "Instagram 1.jpg", "image/jpeg")
        assert len(result) == 3
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert (snapshot.graph_attempts, snapshot.cdn_attempts) == (graph, cdn)
    assert snapshot.source_fence_drop == int(case == "fence")
    assert len(calls) == graph + cdn
    with pytest.raises(FrozenInstanceError):
        snapshot.graph_attempts = 999


@pytest.mark.parametrize("fail_provider", [False, True])
async def test_fetch_counter_observer_failure_cannot_mask_provider_result(
    fail_provider,
):
    def handler(req):
        if req.url.host == "graph.instagram.com":
            return httpx.Response(200, json=parent(1))
        return httpx.Response(
            403 if fail_provider else 200,
            content=b"jpeg",
            headers={"content-type": "image/jpeg"},
        )

    def broken(snapshot):
        raise ValueError("observer failure")

    a, _ = make(handler)
    kwargs = dict(source_id=SOURCE, workspace_id=WS, max_bytes=100, on_counters=broken)
    if fail_provider:
        with pytest.raises(MediaTerminalError, match="unavailable"):
            await a.fetch_bytes({}, "1", **kwargs)
    else:
        assert (await a.fetch_bytes({}, "1", **kwargs))[0] == b"jpeg"


def test_appended_cdn_counter_preserves_existing_positional_fields():
    from src.services.target.media_adapters import MediaTurnCounters

    counters = MediaTurnCounters(2, 3, 1, 4)
    assert (
        counters.graph_attempts,
        counters.observed_parents,
        counters.source_fence_drop,
        counters.listed_parents,
    ) == (2, 3, 1, 4)
    assert counters.cdn_attempts == 0


@pytest.mark.parametrize("operation", ["listing", "enrichment", "continuation"])
@pytest.mark.parametrize("code,status", [(190, 400), (102, 400), (100, 401)])
async def test_actual_grant_death_has_complete_safe_turn_counters(
    operation, code, status
):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(
            status,
            json={"error": {"code": code, "message": "private-grant-caption-url"}},
        )

    a, _ = make(handler)
    if operation == "listing":
        call = listing(a)
    elif operation == "enrichment":
        call = targeted(a)
    else:
        cp = resumed_checkpoint()
        cp.update(pending_parent="1", pending_at=900, more=True, boundary=["99"])
        call = listing(a, cp)
    with pytest.raises(MediaCredentialDead) as caught:
        await call
    snapshot = getattr(caught.value, "counters", None)
    assert snapshot == media_adapters.MediaTurnCounters(graph_attempts=len(calls))
    assert len(calls) == 1
    clean = redacted_error(caught.value)
    assert type(clean) is MediaCredentialDead and clean.counters == snapshot
    assert "private" not in str(clean) and "private" not in repr(clean.counters)


async def test_caption_fallback_then_grant_death_counts_both_dispatches():
    calls = []

    def handler(req):
        calls.append(req)
        code = 100 if len(calls) == 1 else 190
        message = (
            "Tried accessing nonexisting field (caption)"
            if code == 100
            else "private-grant"
        )
        return httpx.Response(400, json={"error": {"code": code, "message": message}})

    a, _ = make(handler, metadata_only=True)
    with pytest.raises(MediaCredentialDead) as caught:
        await targeted(a)
    assert caught.value.counters.graph_attempts == len(calls) == 2
    assert redacted_error(caught.value).counters == caught.value.counters


async def test_late_grant_death_retains_earlier_observed_parent_work():
    calls = []

    def handler(req):
        calls.append(req)
        if req.url.path.endswith("/media"):
            return httpx.Response(200, json={"data": [parent(1), parent(2)]})
        if req.url.path.endswith("/1"):
            return httpx.Response(200, json={"id": "1", "caption": "Original"})
        return httpx.Response(
            400, json={"error": {"code": 190, "message": "private-grant"}}
        )

    a, _ = make(handler, enrich=True)
    with pytest.raises(MediaCredentialDead) as caught:
        await listing(a)
    assert caught.value.counters == media_adapters.MediaTurnCounters(
        graph_attempts=3, listed_parents=2, observed_parents=1
    )
    assert (
        len(calls) == 3
        and redacted_error(caught.value).counters == caught.value.counters
    )


@pytest.mark.parametrize("operation", ["listing", "enrichment"])
@pytest.mark.parametrize(
    "error_type", [media_adapters.MediaSourceGone, MediaTerminalError]
)
async def test_local_typed_exit_keeps_original_exception_and_actual_counters(
    operation, error_type
):
    calls = []

    def handler(req):
        calls.append(req)
        body = {"data": [parent(1)]} if operation == "listing" else parent(1)
        return httpx.Response(200, json=body)

    a, _ = make(handler)
    fault = error_type("private-error-body")

    async def refused(*args):
        raise fault

    a._children = refused
    if operation == "enrichment" and error_type is MediaTerminalError:
        # A terminal read of one parent is that parent's failure, not the turn's.
        result = await targeted(a)
        assert result.failed == ("1",) and result.observations == ()
        assert result.counters == media_adapters.MediaTurnCounters(
            graph_attempts=1, listed_parents=1
        )
        return
    with pytest.raises(error_type) as caught:
        await (listing(a) if operation == "listing" else targeted(a))
    assert caught.value is fault
    assert fault.counters == media_adapters.MediaTurnCounters(
        graph_attempts=1, listed_parents=1
    )
    clean = redacted_error(fault)
    assert type(clean) is error_type and clean.counters == fault.counters
    assert str(clean) == "media provider read failed"


@pytest.mark.parametrize(
    "error_type",
    [
        media_adapters.MediaCredentialDead,
        media_adapters.MediaSourceGone,
        media_adapters.MediaTerminalError,
        media_adapters.MediaRetryableError,
        media_adapters.MediaLostResponse,
        media_adapters.MediaError,
        media_adapters.MediaSelectedAssetChanged,
        media_adapters.MediaSourceFenceDrop,
        media_adapters.MediaTurnBudgetExhausted,
    ],
)
def test_redaction_preserves_only_safe_closed_counter_snapshot(error_type):
    counters = media_adapters.MediaTurnCounters(3, 1, 0, 2, 4)
    if error_type is media_adapters.MediaTurnBudgetExhausted:
        fault = error_type(counters, reason="graph_attempt_budget")
    elif error_type in (
        media_adapters.MediaSelectedAssetChanged,
        media_adapters.MediaSourceFenceDrop,
    ):
        fault = error_type()
    else:
        fault = error_type("private-provider-body")
    fault.counters = counters
    clean = redacted_error(fault)
    assert type(clean) is error_type and clean.counters == counters
    assert "private" not in str(clean)
    with pytest.raises(FrozenInstanceError):
        clean.counters.graph_attempts = 99


@pytest.mark.parametrize(
    "unsafe",
    [
        "private-url",
        {"graph_attempts": "private-caption"},
        media_adapters.MediaTurnCounters(graph_attempts="private-token"),
    ],
)
def test_redaction_never_carries_untyped_or_nonnumeric_counter_payloads(
    unsafe,
):
    fault = MediaCredentialDead("private-provider-body")
    fault.counters = unsafe
    clean = redacted_error(fault)
    assert getattr(clean, "counters", None) is None
    assert "private" not in str(clean)


# --- the walk: the launch floor and the gap probe --------------------------------

FLOOR, WIDTH = past_posts.LAUNCH_FLOOR, past_posts.WINDOW_SECONDS
APP_ID = "555"


def row(mid, at):
    return {
        **parent(mid),
        "timestamp": datetime.fromtimestamp(at, timezone.utc).isoformat(),
    }


def window(start, until, **extra):
    """A saved scan lane at one window, its frozen upper bound the window's top."""
    lane = dict(
        v=2,
        upper=until,
        until=until,
        window_start=start,
        boundary=[],
        seen=0,
        more=True,
        ceiling_reached=False,
    )
    return {**lane, **extra}


def probing(rows_below=(), rows_in_window=()):
    """A Graph whose windowed listing answers *rows_in_window* and whose probe
    (a listing with `until` alone) answers *rows_below*; every request is kept."""
    seen = []

    def handler(req):
        seen.append(dict(req.url.params))
        rows = rows_in_window if "since" in req.url.params else rows_below
        return httpx.Response(200, json={"data": list(rows)})

    return handler, seen


async def test_an_empty_window_costs_one_probe_and_no_post_below_ends_history():
    handler, requests = probing()
    a, admission = make(handler)
    start, until = FLOOR + 3 * WIDTH, FLOOR + 4 * WIDTH
    result = await listing(a, window(start, until))
    assert result.checkpoint["more"] is False and result.observations == ()
    # History is done, and that is not a claim about what was skipped: absence
    # from a windowed scan never certifies a deletion.
    assert not a.checkpoint_incomplete(result.checkpoint)
    assert not a.checkpoint_whole(result.checkpoint)
    assert requests == [
        {
            "fields": past_posts._FIELDS,
            "since": str(start - 1),
            "until": str(until),
            "limit": "100",
        },
        # The probe: the newest post below the window, one row, no `since`.
        {"fields": "id,timestamp", "until": str(start), "limit": "1"},
    ]
    # Both requests went through admission, and both count in the turn.
    assert admission.events == ["admit", "fence", "admit", "fence"]
    assert result.counters.graph_attempts == 2


async def test_a_probe_hit_ends_the_next_window_just_after_that_post():
    start, until = FLOOR + 3 * WIDTH, FLOOR + 4 * WIDTH
    found = FLOOR + WIDTH + 12345
    handler, requests = probing(rows_below=[row(7, found)])
    a, _ = make(handler)
    result = await listing(a, window(start, until, seen=3))
    cp = result.checkpoint
    # The two empty windows between are skipped; the frozen bound, the count and
    # the (reset) boundary follow the watermark rules.
    assert (cp["until"], cp["window_start"]) == (found + 1, found + 1 - WIDTH)
    assert cp["more"] is True and cp["boundary"] == [] and cp["seen"] == 3
    assert cp["upper"] == until and result.observations == ()
    assert result.counters.listed_parents == 1
    # Positive control: the post the probe named is listed by the next turn.
    handler, requests = probing(rows_in_window=[row(7, found)])
    a, _ = make(handler)
    second = await listing(a, cp)
    assert [o.parent_media_id for o in second.observations] == ["7"]
    assert requests[0]["until"] == str(found + 1)
    assert requests[0]["since"] == str(found - WIDTH)


async def test_the_window_below_a_probe_hit_stops_at_the_launch_floor():
    found = FLOOR + 100
    handler, _ = probing(rows_below=[row(7, found)])
    a, _ = make(handler)
    result = await listing(a, window(FLOOR + WIDTH, FLOOR + 2 * WIDTH))
    assert (result.checkpoint["until"], result.checkpoint["window_start"]) == (
        found + 1,
        FLOOR,
    )


async def test_a_window_that_landed_a_parent_takes_its_neighbour_without_a_probe():
    start, until = FLOOR + 3 * WIDTH, FLOOR + 4 * WIDTH
    handler, requests = probing(rows_in_window=[row(1, start + 5)])
    a, _ = make(handler)
    result = await listing(a, window(start, until))
    assert len(requests) == 1 and [o.parent_media_id for o in result.observations] == [
        "1"
    ]
    assert (result.checkpoint["until"], result.checkpoint["window_start"]) == (
        start,
        start - WIDTH,
    )


@pytest.mark.parametrize("start", [FLOOR, FLOOR - 1, 0])
async def test_no_window_ends_below_the_launch_floor_and_the_floor_needs_no_probe(
    start,
):
    """The lowest window ends the lane at its own edge: one request, no probe,
    and a checkpoint already under the floor ends rather than go lower."""
    handler, requests = probing(rows_below=[row(7, FLOOR + 5)])
    a, _ = make(handler)
    result = await listing(a, window(start, start + 100))
    assert len(requests) == 1 and result.checkpoint["more"] is False


async def test_the_last_window_before_the_floor_is_clamped_to_it():
    start = FLOOR + WIDTH // 2
    handler, requests = probing(rows_in_window=[row(1, start + 5)])
    a, _ = make(handler)
    result = await listing(a, window(start, start + 100))
    assert len(requests) == 1
    # Half a window of history is left above the floor: the window is that short.
    assert (result.checkpoint["until"], result.checkpoint["window_start"]) == (
        start,
        FLOOR,
    )


async def test_a_turn_that_cannot_afford_the_probe_takes_the_adjacent_window():
    start, until = FLOOR + 3 * WIDTH, FLOOR + 4 * WIDTH
    handler, requests = probing(rows_below=[row(7, FLOOR + 5)])
    a, admission = make(handler, graph_limit=1)
    result = await listing(a, window(start, until))
    assert len(requests) == 1 and admission.events == ["admit", "fence"]
    assert (result.checkpoint["until"], result.checkpoint["window_start"]) == (
        start,
        start - WIDTH,
    )
    # Positive control: with a second request in the budget the probe is made.
    handler, requests = probing(rows_below=[row(7, FLOOR + 5)])
    a, _ = make(handler, graph_limit=2)
    jumped = await listing(a, window(start, until))
    assert len(requests) == 2 and jumped.checkpoint["until"] == FLOOR + 6


async def test_a_spent_parent_budget_takes_the_adjacent_window_and_the_probe_row_counts():
    start, until = FLOOR + 3 * WIDTH, FLOOR + 4 * WIDTH
    # The window's replay lists two parents already landed: no budget remains.
    handler, requests = probing(
        rows_in_window=[row(1, start + 9), row(2, start + 8)],
        rows_below=[row(7, FLOOR + 5)],
    )
    a, _ = make(handler, parent_limit=2)
    spent = await listing(a, window(start, until, seen=2, boundary=["1", "2"]))
    assert len(requests) == 1 and spent.counters.listed_parents == 2
    assert spent.checkpoint["window_start"] == start - WIDTH
    # With room, the probe's one row counts as a listed parent.
    handler, requests = probing(rows_below=[row(7, FLOOR + 5)])
    a, _ = make(handler, parent_limit=2)
    probed = await listing(a, window(start, until))
    assert len(requests) == 2 and probed.counters.listed_parents == 1


@pytest.mark.parametrize("refused", [1, 2])
async def test_a_refused_request_advances_nothing_and_the_replay_asks_the_same_again(
    refused,
):
    start, until = FLOOR + 3 * WIDTH, FLOOR + 4 * WIDTH
    saved = window(start, until)
    handler, requests = probing(rows_below=[row(7, FLOOR + 5)])
    admission = Admission()
    admission.reject_before = refused
    a, _ = make(handler, admission=admission)
    with pytest.raises(MediaSourceFenceDrop):
        await listing(a, saved)
    assert saved == window(start, until), "the saved lane is not touched"
    assert len(requests) == refused - 1
    # The refused probe leaves the window to list again next time, then probe.
    handler, requests = probing(rows_below=[row(7, FLOOR + 5)])
    a, _ = make(handler)
    replay = await listing(a, saved)
    assert len(requests) == 2 and replay.checkpoint["until"] == FLOOR + 6


async def test_an_inclusive_bound_that_answers_with_the_windows_edge_takes_the_neighbour():
    start, until = FLOOR + 3 * WIDTH, FLOOR + 4 * WIDTH
    handler, _ = probing(rows_below=[row(7, start)])
    a, _ = make(handler)
    result = await listing(a, window(start, until))
    assert (result.checkpoint["until"], result.checkpoint["window_start"]) == (
        start,
        start - WIDTH,
    )


async def test_a_probe_that_names_only_time_before_the_launch_ends_history():
    handler, _ = probing(rows_below=[row(7, FLOOR - 1)])
    a, _ = make(handler)
    result = await listing(a, window(FLOOR + WIDTH, FLOOR + 2 * WIDTH))
    assert result.checkpoint["more"] is False


@pytest.mark.parametrize(
    "answer",
    [
        [row(7, FLOOR + WIDTH + 1)],
        [row(7, FLOOR + 1), row(8, FLOOR)],
        [{"id": "x", "timestamp": "2020-01-01T00:00:00+0000"}],
        [{"id": "7"}],
        ["raw-provider-text"],
    ],
    ids=["above-its-bound", "two-rows", "bad-id", "no-timestamp", "not-a-row"],
)
async def test_a_probe_answer_that_breaks_its_contract_is_refused_not_trusted(answer):
    start, until = FLOOR + 3 * WIDTH, FLOOR + 4 * WIDTH
    handler, _ = probing(rows_below=answer)
    a, _ = make(handler)
    with pytest.raises((MediaRetryableError, MediaTerminalError)):
        await listing(a, window(start - 2 * WIDTH, until - 2 * WIDTH))


# --- batched enrichment ------------------------------------------------------------


def batch_graph(
    calls, *, owner=ACCOUNT, carousels=(), terminal=(), throttled=(), dead=(), me=None
):
    """A Graph of queued parents: each parent read answers its caption, a
    carousel its child page, `/me` the *me* answer, and a named id its fault."""

    def handler(req):
        path = req.url.path
        calls.append(path)
        if path.endswith("/me"):
            return httpx.Response(me[0], json=me[1])
        if path.endswith("/children"):
            return httpx.Response(200, json={"data": [parent(5000 + len(calls))]})
        mid = path.rsplit("/", 1)[1]
        if mid in dead:
            return httpx.Response(
                401, json={"error": {"code": 190, "message": "private-grant"}}
            )
        if mid in terminal:
            return httpx.Response(
                400, json={"error": {"code": 100, "message": "private-parent"}}
            )
        if mid in throttled:
            return httpx.Response(
                429, json={"error": {"code": 4, "message": "private-throttle"}}
            )
        kind = "CAROUSEL_ALBUM" if mid in carousels else "IMAGE"
        return httpx.Response(
            200,
            json={**parent(mid, kind), "owner": {"id": owner}, "caption": "Original"},
        )

    return handler


async def batch(a, ids):
    return await a.enrich_parents(ids, source_id=SOURCE, workspace_id=WS)


async def test_a_batch_reads_as_many_parents_as_the_turn_budget_affords_in_order():
    calls = []
    ids = [str(i) for i in range(1, 26)]
    a, admission = make(batch_graph(calls), metadata_only=True)
    result = await batch(a, ids)
    assert [o.parent_media_id for o in result.observations] == ids[:20]
    assert calls == [f"/v26.0/{i}" for i in ids[:20]]
    assert result.counters.graph_attempts == 20 and result.outcome == "parent_enriched"
    assert result.deferred == () and result.failed == ()
    assert len(result.candidates) == 20 and admission.events.count("admit") == 20
    # The rest are simply not read yet: a second turn takes them.
    again = await batch(a, ids[20:])
    assert [o.parent_media_id for o in again.observations] == ids[20:]


async def test_a_batch_yields_before_a_request_it_cannot_afford():
    calls = []
    ids = ["1", "2", "3", "4"]
    # Budget 5: two carousels cost four, the third's read is the fifth, and its
    # child page would be the sixth.
    a, _ = make(batch_graph(calls, carousels=ids), metadata_only=True, graph_limit=5)
    result = await batch(a, ids)
    assert [o.parent_media_id for o in result.observations] == ["1", "2"]
    assert len(calls) == result.counters.graph_attempts == 5
    # The yielded parent stays queued for a turn with a whole budget.
    assert result.deferred == () and result.failed == ()
    assert result.counters.observed_parents == 2 and result.outcome == "parent_enriched"


async def test_a_first_parent_that_cannot_fit_a_whole_budget_is_deferred():
    calls = []
    a, _ = make(batch_graph(calls, carousels=["1"]), metadata_only=True, graph_limit=1)
    result = await batch(a, ["1", "2"])
    assert result.observations == () and result.deferred == ("1",)
    assert result.outcome == "parent_enrichment_pending" and len(calls) == 1


async def test_a_parent_the_provider_refuses_for_good_does_not_end_the_batch():
    calls = []
    a, _ = make(batch_graph(calls, terminal=["2"]), metadata_only=True)
    result = await batch(a, ["1", "2", "3"])
    assert [o.parent_media_id for o in result.observations] == ["1", "3"]
    assert result.failed == ("2",) and result.deferred == ()
    assert calls == ["/v26.0/1", "/v26.0/2", "/v26.0/3"]
    assert result.counters.graph_attempts == 3 and result.counters.observed_parents == 2
    assert "private-parent" not in repr(result)


async def test_a_retryable_read_defers_its_parent_and_asks_for_no_more():
    calls = []
    a, _ = make(batch_graph(calls, throttled=["2"]), metadata_only=True)
    result = await batch(a, ["1", "2", "3"])
    assert [o.parent_media_id for o in result.observations] == ["1"]
    assert result.deferred == ("2",) and result.failed == ()
    assert calls == ["/v26.0/1", "/v26.0/2"], "the throttled provider is left alone"
    assert result.diagnostics == ("parent_read_retry_pending",)
    assert result.outcome == "parent_enriched"


async def test_grant_death_in_a_batch_raises_with_what_it_actually_did():
    calls = []
    a, _ = make(batch_graph(calls, dead=["2"]), metadata_only=True)
    with pytest.raises(MediaCredentialDead) as caught:
        await batch(a, ["1", "2", "3"])
    assert caught.value.counters == media_adapters.MediaTurnCounters(
        graph_attempts=2, listed_parents=1, observed_parents=1
    )
    assert calls == ["/v26.0/1", "/v26.0/2"]


async def test_a_source_fence_drop_in_a_batch_raises_and_the_batch_ends():
    calls = []
    admission = Admission()
    admission.reject_before = 3
    a, _ = make(batch_graph(calls), metadata_only=True, admission=admission)
    with pytest.raises(MediaSourceFenceDrop) as caught:
        await batch(a, ["1", "2", "3", "4"])
    assert len(calls) == 2 and caught.value.counters.source_fence_drop == 1


async def test_a_batch_stays_within_the_turns_parent_ceiling():
    calls = []
    a, _ = make(batch_graph(calls), metadata_only=True, parent_limit=2)
    result = await batch(a, ["1", "2", "3"])
    assert [o.parent_media_id for o in result.observations] == ["1", "2"]
    assert len(calls) == 2


async def test_a_batch_names_valid_parent_ids_before_it_reads_anything():
    calls = []
    a, _ = make(batch_graph(calls), metadata_only=True)
    with pytest.raises(MediaTerminalError):
        await batch(a, ["1", "not-an-id"])
    assert calls == []


# --- ownership: the recorded id, then `/me` once a turn -----------------------------


async def test_a_recorded_app_scoped_id_proves_ownership_with_no_me_read():
    calls = []
    a, _ = make(
        batch_graph(calls, owner=APP_ID),
        metadata_only=True,
        app_scoped_id=APP_ID,
    )
    result = await batch(a, ["1", "2", "3"])
    assert [o.parent_media_id for o in result.observations] == ["1", "2", "3"]
    assert "/v26.0/me" not in calls and result.counters.graph_attempts == 3


async def test_an_owner_that_is_neither_the_account_nor_the_recorded_id_is_refused():
    calls = []
    a, _ = make(
        batch_graph(calls, owner="999"),
        metadata_only=True,
        app_scoped_id=APP_ID,
        graph_limit=20,
    )
    result = await batch(a, ["1", "2"])
    assert result.observations == () and result.failed == ("1", "2")
    # The recorded id is the proof: a mismatch is not retried through `/me`.
    assert "/v26.0/me" not in calls and len(calls) == 2


async def test_the_account_itself_as_owner_needs_neither_a_record_nor_a_me_read():
    calls = []
    a, _ = make(batch_graph(calls), metadata_only=True)
    result = await batch(a, ["1", "2"])
    assert len(result.observations) == 2 and "/v26.0/me" not in calls


async def test_with_no_recorded_id_me_is_read_once_for_the_whole_turn():
    calls = []
    me = (200, {"id": APP_ID, "user_id": ACCOUNT})
    a, _ = make(batch_graph(calls, owner=APP_ID, me=me), metadata_only=True)
    result = await batch(a, ["1", "2", "3"])
    assert len(result.observations) == 3
    assert calls.count("/v26.0/me") == 1 and result.counters.graph_attempts == 4
    # The proof lives for the turn only: nothing carries it to the next one.
    await batch(a, ["4"])
    assert calls.count("/v26.0/me") == 2


@pytest.mark.parametrize(
    "me",
    [
        (200, {"id": APP_ID, "user_id": "17841499999999999"}),
        (200, {"id": "998", "user_id": ACCOUNT}),
        (400, {"error": {"code": 100, "message": "private-profile"}}),
    ],
    ids=["another-account", "another-subject", "refused"],
)
async def test_a_me_answer_that_does_not_prove_ownership_is_asked_once_and_refuses_all(
    me,
):
    calls = []
    a, _ = make(batch_graph(calls, owner=APP_ID, me=me), metadata_only=True)
    result = await batch(a, ["1", "2", "3"])
    assert result.observations == () and result.failed == ("1", "2", "3")
    assert calls.count("/v26.0/me") == 1
    assert "private-profile" not in repr(result)


async def test_a_retryable_me_defers_its_parent_and_is_not_asked_again():
    calls = []
    me = (429, {"error": {"code": 4, "message": "private-throttle"}})
    a, _ = make(batch_graph(calls, owner=APP_ID, me=me), metadata_only=True)
    result = await batch(a, ["1", "2"])
    assert result.deferred == ("1",) and result.observations == ()
    assert calls.count("/v26.0/me") == 1 and calls == ["/v26.0/1", "/v26.0/me"]
