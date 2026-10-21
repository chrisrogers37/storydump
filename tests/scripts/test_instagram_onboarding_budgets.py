"""Onboarding budgets: the representative past-posts sources through the real sync composition.

The Instagram source (#1644): the generators of
``tests/fixtures/instagram_onboarding`` build past-posts sources of a known composition and
``expected_counts.json`` states, by hand, what a complete clean traversal of each
must report. This drives those past-posts sources through the sync as production composes
it: ``instagram_sync.run_sync`` over the past-posts adapter that
``past_posts_composition.adapter_factory`` builds, its access provider reading the
credential (and the verified identity) from a real database as the worker's
login, and only Instagram's Graph faked, over ``httpx.MockTransport`` in the
Graph's own wire shapes (``+0000`` timestamps, an offset cursor, no caption on a
listing). One job turn after another, each landing atomically, until the sync
claims no further job.

Nothing here asks the suitability evaluator for the expected figures. They come
from the manifest, and the manifest's ceilings (``3N+64`` Graph admissions,
``N+128`` job turns, zero CDN, downloads and decodes) are fixed:
a run over one is a budget review with an explanation, not a number edited to
fit. Traversal is metadata-only, and the spies prove it independently of the
counters: a request to any host but the Graph's fails the run on the spot, and
so does a call into the media validator.

Counted at ``before_dispatch``: every admission the adapter's requests pass is
tallied there, and that tally must equal both what the turns report and what the
transport received. The report (``$INSTAGRAM_ONBOARDING_REPORT``, never
committed) carries the measurements next to their ceilings, with the turns,
requests and seconds to the first observation and the first metadata candidate.

The first card's request budget is measured here, though not the card. Its Graph
admissions are the ones that reach the first metadata candidate plus what the
selected asset's own preparation spends, and they must stay within the manifest's
``ceil(0.1N/25)+16``, with at most four CDN downloads and the first candidate
landing before a quarter of the parents are observed. The preparation is
``instagram_preparation.SelectedPreparation``, whose one provider call is the
content adapter's ``fetch_bytes`` (its counters travel on the bytes it returns);
``prepare_selected`` makes that call, over the same fake Graph and a fake CDN
that is open for it alone. What is NOT measured: the first verified-ready item
and the card itself. They need the bytes validated (Linux's validator) and the
card rendered, which is the full-composition gate's; the report says so.

The 10,000-post past-posts source lives in ``tests/scripts/onboarding_10k/`` and is
deselected unless ``RUN_ONBOARDING_10K=1``.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.services.target import past_posts_composition as composition
from src.services.target import source_read_admission as read_admission
from src.services.target import (
    content_runway,
    destination_readiness,
    identity_mappings,
    instagram_sync,
    jobs,
    media_validation,
    oauth_states,
)
from src.services.target.egress import EgressPolicy
from src.services.target.unit_of_work import in_transaction
from tests.fixtures import instagram_onboarding as fx
from tests.scripts import test_instagram_observations_gate as obs
from tests.scripts import test_instagram_sources_gate as foundation
from tests.scripts import test_source_read_admission_gate as admission
from tests.scripts.conftest import as_user, async_url, set_test_passwords

pytestmark = [pytest.mark.integration, pytest.mark.slow]

origin = foundation.origin

GRAPH_HOST = "graph.instagram.com"
#: What the fake CDN serves for the selected asset: never decoded in this module.
IMAGE_BYTES = b"\xff\xd8\xff\xe0 synthetic bytes, not validated here"
TOKEN = "synthetic-access-token"
#: An app-scoped id that is not the account's own: what Instagram Login can name
#: as a media owner, and the reason ownership needs a proof.
APP_SCOPED_ID = "17841499999999999"
REPORT_ENV = "INSTAGRAM_ONBOARDING_REPORT"
SIZES = (100, 500, 2000)
#: ``account``: the rows name the account's own id (no proof needed). ``recorded``
#: and ``unrecorded``: they name the app-scoped id, with and without the verified
#: id recorded for the source's current authorization.
VARIANTS = ("account", "recorded", "unrecorded")


@pytest.fixture
async def worker(world_db, admin_conn):
    set_test_passwords(admin_conn)
    engine = create_async_engine(async_url(as_user(world_db, "svc_worker")))
    try:
        yield engine
    finally:
        await engine.dispose()


# --- the fake Graph, the spies and the tally ----------------------------------------


def graph_time(at):
    """Graph writes a UTC offset without its colon: ``2022-06-15T09:30:00+0000``."""
    return datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")


class FakeGraph:
    """Instagram's Graph over one past-posts source, recording every request it receives.

    The listing by time window and offset cursor (with no ``since``, the walk's
    one-row probe for the newest post below ``until``), a parent read that adds
    the caption and owner when asked for them, the child page and, given
    *profile*, the token's own ``/me``. Any request off the Graph host fails the
    run immediately: indexing never reaches the CDN. Only while ``preparing`` (the
    first card's selected asset, fetched once) is the CDN open, and what it and
    the Graph then receive is recorded apart, in ``prepared``, so the traversal's
    own tallies stay what the traversal made.
    """

    def __init__(self, past_posts, profile=None):
        self.posts = past_posts.posts
        self.by_id = {p.parent["id"]: p for p in past_posts.posts}
        self.profile = profile
        self.graph = []
        self.cdn = []
        self.me_reads = 0
        self.preparing = False
        self.prepared = {"graph": [], "cdn": []}

    def row(self, post, fields):
        row = {**post.parent, "timestamp": graph_time(post.at)}
        return {k: v for k, v in row.items() if k == "id" or k in fields}

    async def __call__(self, request):
        assert not in_transaction(), "a provider call held a tenant transaction"
        if request.url.host != GRAPH_HOST:
            if not self.preparing:
                self.cdn.append(request.url.host)
                pytest.fail("traversal reached the CDN")
            self.prepared["cdn"].append(request.url.path)
            return httpx.Response(
                200, content=IMAGE_BYTES, headers={"Content-Type": "image/jpeg"}
            )
        params, path = request.url.params, request.url.path
        (self.prepared["graph"] if self.preparing else self.graph).append(path)
        fields = set(params.get("fields", "").split(","))
        if path.endswith("/media"):
            since = int(params["since"]) if "since" in params else 0
            until, limit = int(params["until"]), int(params["limit"])
            offset = int(params.get("after", "0"))
            rows = [p for p in self.posts if since <= p.at < until]
            body = {
                "data": [self.row(p, fields) for p in rows[offset : offset + limit]]
            }
            if offset + limit < len(rows):
                body["paging"] = {
                    "next": "https://ignored.example/never-followed",
                    "cursors": {"after": str(offset + limit)},
                }
            return httpx.Response(200, json=body)
        if path.endswith("/me"):
            if not self.preparing:
                self.me_reads += 1
            return httpx.Response(200, json=self.profile)
        if path.endswith("/children"):
            post = self.by_id[path.split("/")[-2]]
            limit = int(params["limit"])
            body = {"data": list(post.children)[:limit]}
            if len(post.children) > limit:
                body["paging"] = {
                    "next": "https://ignored.example/never-followed",
                    "cursors": {"after": str(limit)},
                }
            return httpx.Response(200, json=body)
        post = self.by_id[path.rsplit("/", 1)[1]]
        body = self.row(post, fields)
        if "caption" in fields and post.caption is not None:
            body["caption"] = post.caption
        return httpx.Response(200, json=body)


class Tally:
    """Admissions counted where the adapter takes them: ``before_dispatch``."""

    def __init__(self):
        self.admissions = 0


class _Counting:
    def __init__(self, inner, tally):
        self.inner, self.tally = inner, tally

    async def before_dispatch(self):
        ticket = await self.inner.before_dispatch()
        self.tally.admissions += 1
        return ticket

    async def after_return(self, ticket):
        await self.inner.after_return(ticket)


def counting(provider, tally):
    async def counted(source_id, *, workspace_id, access):
        inner = await provider(source_id, workspace_id=workspace_id, access=access)
        return _Counting(inner, tally)

    return counted


# --- the drive -----------------------------------------------------------------------


async def start_job(worker, origin, src, capture):
    async with obs.uow(worker, origin) as conn:
        await jobs.enqueue(
            conn,
            kind="sync_media_source",
            workspace_id=str(origin["ws"]),
            serialization_key="src:" + src["source_id"],
            payload={
                "v": 1,
                "source_id": src["source_id"],
                "source_read_capture": capture.payload(),
            },
        )
    return await claim(worker)


async def claim(worker):
    async with worker.connect() as conn:
        return await jobs.claim_job(
            conn,
            lane="bulk",
            worker="onboarding-budgets",
            lease_seconds=60,
            ws_lane_cap=4,
            capability=foundation.capability(),
        )


async def next_job(worker, origin, previous):
    async with obs.uow(worker, origin) as conn:
        await jobs.finalize_job(
            conn, previous["id"], previous["lease_token"], "succeeded"
        )
    return await claim(worker)


async def read_ledger(worker, origin):
    """What the ledger holds for the workspace that a card or a post would add to."""
    async with obs.uow(worker, origin) as conn:
        return {
            table: (
                await conn.execute(
                    text(f"SELECT count(*) FROM {table} WHERE workspace_id=:ws"),
                    dict(ws=str(origin["ws"])),
                )
            ).scalar_one()
            for table in ("post_intents", "channel_outbox")
        }


async def read_summary(worker, origin, src):
    async with obs.uow(worker, origin) as conn:
        return (
            await conn.execute(
                text(
                    "SELECT sync_checkpoint->'summary' FROM media_sources WHERE id=:sid"
                ),
                dict(sid=src["source_id"]),
            )
        ).scalar_one()


async def record_mapping(worker, origin, capture, subject):
    """The verified app-scoped id of the source's current authorization, as the
    connect callback records it."""
    now = datetime.now(timezone.utc)
    async with obs.uow(worker, origin) as conn:
        await identity_mappings.record(
            conn,
            workspace_id=capture.workspace_id,
            ig_account_id=str(origin["iga"]),
            credential_id=capture.credential_id,
            authorization_id=str(uuid.uuid4()),
            app_identity="instagram:1234567890",
            subject_ref=subject,
            account_ref=capture.account_ref,
            method="oauth_exchange",
            authorized_at=now,
            verified_at=now,
        )


def milestone(turns, admissions, started, summary):
    return {
        "turns": turns,
        "graph_admissions": admissions,
        "seconds": round(time.perf_counter() - started, 3),
        "observed": summary["observed"],
        "candidates": summary["candidates"],
    }


async def prepare_selected(worker, graph, content, capture, post):
    """The first card's selected-media preparation, measured where it spends.

    ``SelectedPreparation._fetch`` (instagram_preparation.py) makes one provider
    call, the content adapter's ``fetch_bytes`` for the selected asset under the
    capability's own admission (it has no job), and keeps the counters that call
    reports. This is that call, with its arguments, through the composition's
    content adapter. Three routes count it, as they do the traversal: what the
    adapter reports, what its admission passed and what the transport received.
    The bytes are not validated here (the full-composition gate's, on Linux).
    """
    tally, reported = Tally(), []
    adapter = content(
        capture,
        counting(
            read_admission.admission_provider(
                worker, capability=foundation.capability()
            ),
            tally,
        ),
    )
    graph.preparing = True
    try:
        data, _, mime = await adapter.fetch_bytes(
            {},
            post.parent["id"],
            source_id=capture.source_id,
            workspace_id=capture.workspace_id,
            selected_asset_id=post.parent["id"],
            max_bytes=media_validation.SUPPORTED_MEDIA_MATRIX["byte_caps"]["image"],
            on_counters=reported.append,
        )
    finally:
        graph.preparing = False
    (counters,) = reported
    return {
        "parent": post.parent["id"],
        "graph": list(graph.prepared["graph"]),
        "cdn": list(graph.prepared["cdn"]),
        "graph_reported": counters.graph_attempts,
        "cdn_reported": counters.cdn_attempts,
        "admissions": tally.admissions,
        "mime": mime,
        "bytes": len(data),
    }


async def onboard(kind, n, variant, *, worker, origin, sync_conn, monkeypatch):
    """Drive one past-posts source to the end of its sync and measure it.

    Returns the report: the measurements, the final facts and what they ran
    against. Stops (so the ceilings can fail with numbers, not hang) once the
    sync has spent a turn more than its ceiling.
    """
    manifest = fx.expected(kind, n)
    ceiling = manifest["traversal"]
    t0 = int(time.time())
    owner = fx.OWNER_ID if variant == "account" else APP_SCOPED_ID
    past_posts = {
        "useful": fx.useful,
        "excluded": fx.excluded,
        "carousel": fx.carousel,
    }[kind](n, t0, owner_id=owner)
    graph = FakeGraph(past_posts, profile={"id": owner, "user_id": fx.OWNER_ID})
    tally, validator = Tally(), []

    async def forbidden(*args, **kwargs):
        validator.append(True)
        pytest.fail("metadata traversal invoked the media validator")

    monkeypatch.setattr(media_validation, "validate_media", forbidden)
    monkeypatch.setattr(media_validation, "run_bounded", forbidden)

    async with obs.uow(worker, origin) as conn:
        await conn.execute(
            text("UPDATE ig_accounts SET provider_account_ref=:ref WHERE id=:id"),
            dict(ref=fx.OWNER_ID, id=str(origin["iga"])),
        )
    src = await admission.ready(worker, origin)
    capture = await admission.capture(worker, origin, src)
    with sync_conn.cursor() as cur:
        cur.execute(
            "UPDATE oauth_credentials SET encrypted_payload = %s WHERE id = %s",
            (oauth_states.ring().encrypt(TOKEN), origin["credential"]),
        )
    sync_conn.commit()
    if variant == "recorded":
        await record_mapping(worker, origin, capture, APP_SCOPED_ID)

    client = httpx.AsyncClient(transport=httpx.MockTransport(graph))
    build = composition.adapter_factory(
        worker,
        capability=foundation.capability(),
        client=client,
        graph_version="v26.0",
        metadata_only=True,
        page_size=composition.PAGE_SIZE,
        policy=EgressPolicy().without(enforce_private_address_block=False),
    )

    # What the selected asset's preparation builds (`build_worker`'s `content`).
    content = composition.adapter_factory(
        worker,
        capability=foundation.capability(),
        client=client,
        graph_version="v26.0",
        metadata_only=False,
        page_size=composition.PAGE_SIZE,
        policy=EgressPolicy().without(enforce_private_address_block=False),
    )
    accepted = next((p for p in past_posts.posts if p.group == "accepted_image"), None)

    async def prepare(captured, provider):
        return build(captured, counting(provider, tally))

    ports = instagram_sync.PastPostsSyncPorts(
        capability=foundation.capability(),
        tombstone_keys=obs.keys(),
        prepare_adapter=prepare,
    )
    ledger_before = await read_ledger(worker, origin)
    results, first, started = [], {}, time.perf_counter()
    try:
        job = await start_job(worker, origin, src, capture)
        while job is not None and len(results) <= ceiling["max_turns"]:
            results.append(await instagram_sync.run_sync(worker, job, ports=ports))
            summary = await read_summary(worker, origin, src) or {}
            if summary.get("observed") and "observation" not in first:
                first["observation"] = milestone(
                    len(results), tally.admissions, started, summary
                )
            if summary.get("candidates") and "candidate" not in first:
                first["candidate"] = milestone(
                    len(results), tally.admissions, started, summary
                )
                # The first card prepares the first accepted image.
                if accepted is not None:
                    first["preparation"] = await prepare_selected(
                        worker, graph, content, capture, accepted
                    )
            job = await next_job(worker, origin, job)
        seconds = round(time.perf_counter() - started, 3)
        async with obs.uow(worker, origin) as conn:
            final = await instagram_sync.progress(
                conn,
                capture=capture,
                destination_id=str(origin["iga"]),
                capability=foundation.capability(),
            )
        ledger = await read_ledger(worker, origin)
    finally:
        await client.aclose()
    facts = destination_readiness.DestinationFacts(
        account_state="active",
        handle_only=False,
        credential_status="active",
        candidates=final["candidates"],
        sources=(
            destination_readiness.SourceFacts(
                provider="instagram_past_posts",
                enabled=final["enabled"],
                assignable=True,
                origin_here=True,
                exclusions=final["candidate_exclusions"],
                indexing=final["indexing"],
                partial=final["partial"],
                stalled=final["stalled"],
                needs_reconnect=final["needs_reconnect"],
            ),
        ),
    )
    state, _, remedy = destination_readiness.readiness(facts)
    lanes = [r.lane for r in results]
    helper_turns = [r for r in results if r.lane == "helper"]
    settings = fx.expected_counts()["settings"]
    return {
        "kind": kind,
        "n": n,
        "variant": variant,
        "finished": job is None,
        "turns": len(results),
        "turns_by_lane": {lane: lanes.count(lane) for lane in sorted(set(lanes))},
        "outcomes": sorted({r.outcome for r in results}),
        "graph_admissions": tally.admissions,
        "graph_reported": sum(r.graph_attempts for r in results),
        "graph_requests": len(graph.graph),
        "me_reads": graph.me_reads,
        "me_helper_turns": sum(r.graph_attempts > 0 for r in helper_turns),
        "cdn_requests": len(graph.cdn),
        "cdn_reported": sum(r.cdn_attempts for r in results),
        "validator_calls": len(validator),
        "empty_listing_turns": sum(
            r.lane in ("recent", "history") and not r.committed_observations
            for r in results
        ),
        "most_parents_in_a_helper_turn": max(
            (r.committed_observations for r in helper_turns), default=0
        ),
        "seconds": seconds,
        "first_observation": first.get("observation"),
        "first_candidate": first.get("candidate"),
        "first_preparation": first.get("preparation"),
        "first_verified_ready": None,
        "first_card": first_card(first),
        "not_measured": "first verified-ready item and the card itself: the"
        " selected asset's validation and the card need the full-composition"
        " gate; the first card's requests are measured",
        "final": {
            "observed": final["observed"],
            "represented": final["represented"],
            "items": final["items"],
            "slides": final["slides"],
            "carousel_children_truncated": final["carousel_children_truncated"],
            "candidates": final["candidates"],
            "eligible": final["eligible"],
            "candidate_exclusions": final["candidate_exclusions"],
            "coverage_from": final["coverage_from"],
            "partial": final["partial"],
            "indexing": final["indexing"],
            "pending_helpers": final["pending_helpers"],
            "state": state,
            "remedy": remedy,
            "leading_exclusion": destination_readiness.leading_exclusion(facts),
            "partial_coverage": destination_readiness.partial_coverage(facts),
            "runway_days": content_runway.days_left(
                final["candidates"], settings["daily_slots"]
            ),
        },
        "ledger_before": ledger_before,
        "ledger": ledger,
        "ceilings": {
            "graph": ceiling["max_graph"],
            "turns": ceiling["max_turns"],
            "first_card_graph": manifest.get("first_card", {}).get("max_graph"),
            "first_card_cdn": manifest.get("first_card", {}).get("max_cdn"),
            "first_card_observed": manifest.get("first_card", {}).get("max_observed"),
        },
    }


def first_card(first):
    """The first card's request budget for the report, or None where there is none:
    the Graph admissions that reach the first candidate plus the preparation's."""
    candidate, prepared = first.get("candidate"), first.get("preparation")
    if prepared is None:
        return None
    return {
        "graph": candidate["graph_admissions"] + len(prepared["graph"]),
        "indexing_graph": candidate["graph_admissions"],
        "preparation_graph": len(prepared["graph"]),
        "cdn": len(prepared["cdn"]),
        "observed": candidate["observed"],
        "turns": candidate["turns"],
    }


def write_report(report):
    """Merge this run into ``$INSTAGRAM_ONBOARDING_REPORT``, when it is set."""
    path = os.environ.get(REPORT_ENV)
    if not path:
        return
    target = Path(path)
    try:
        report_so_far = json.loads(target.read_text())
    except (OSError, ValueError):
        report_so_far = {}
    key = f"{report['kind']}/{report['n']}/{report['variant']}"
    report_so_far[key] = report
    target.write_text(json.dumps(report_so_far, indent=2, sort_keys=True))


def check(report):
    """The run against the hand-authored manifest, the ceilings and the spies."""
    kind, n = report["kind"], report["n"]
    manifest, final = fx.expected(kind, n), report["final"]
    where = f"{kind}/{n}/{report['variant']}"
    ceiling = manifest["traversal"]

    # It ended by itself, inside the budgets: unbounded work is the failure.
    assert report["finished"], f"{where}: still running after {report['turns']} turns"
    assert report["turns"] <= ceiling["max_turns"], where
    assert report["graph_admissions"] <= ceiling["max_graph"], where
    # Metadata only: no CDN, no download, no decode, by two independent routes.
    assert report["cdn_requests"] == report["cdn_reported"] == 0, where
    assert report["validator_calls"] == 0 and ceiling["max_cdn"] == 0, where
    # Every request is counted once, at admission, and reported as it was made.
    assert (
        report["graph_admissions"]
        == report["graph_reported"]
        == report["graph_requests"]
    ), where

    # What was found is what the manifest says.
    assert final["observed"] == manifest["observed"], where
    assert final["represented"] == manifest["represented"], where
    for count in ("items", "slides", "carousel_children_truncated"):
        assert final[count] == manifest[count], (where, count)
    assert final["candidates"] == manifest["candidates"], where
    assert final["eligible"] == manifest["eligible_before_selection"], where
    assert final["candidate_exclusions"] == manifest["candidate_exclusions"], where
    assert final["pending_helpers"] == 0 and not final["indexing"], where
    destination = manifest["destination"]
    assert final["state"] == destination["state"], where
    assert final["remedy"] == destination["remedy"], where
    assert final["leading_exclusion"] == destination["leading_exclusion"], where
    assert final["partial_coverage"] is manifest["partial_coverage"], where
    assert final["partial"] is manifest["partial_coverage"], where
    assert final["runway_days"] == manifest["runway_days"], where
    if "oldest_observed_at" in manifest:
        assert datetime.fromisoformat(final["coverage_from"]) == datetime.fromisoformat(
            manifest["oldest_observed_at"]
        ), where
    # Traversal adds no intent and no outbox row: no card, no publish attempt.
    assert report["ledger"] == report["ledger_before"], where
    if kind == "excluded":
        assert (manifest["cards"], manifest["publish_attempts"]) == (0, 0), where

    # The two fixes, each on its own evidence and not only through the ceilings:
    # the walk skipped the empty history (the fixtures hold two gaps), and a
    # helper turn read more than one parent.
    assert report["empty_listing_turns"] <= 4, where
    assert report["most_parents_in_a_helper_turn"] > 1, where
    if kind in ("useful", "carousel"):
        first, prepared = report["first_candidate"], report["first_preparation"]
        card = manifest["first_card"]
        assert first is not None and prepared is not None, where
        # The first candidate arrives while a quarter of the past-posts source, at most, is seen.
        assert first["observed"] <= card["max_observed"], where
        # What the first card's preparation spends is asserted, not assumed: the
        # parent read, the `/me` proof once for the turn when no verified id is
        # recorded, and one download, counted by the adapter, its admission and the
        # transport alike.
        parent = prepared["parent"]
        proof = ["/v26.0/me"] if report["variant"] == "unrecorded" else []
        assert prepared["graph"] == [f"/v26.0/{parent}", *proof], where
        assert len(prepared["cdn"]) == 1, where
        assert (prepared["graph_reported"], prepared["cdn_reported"]) == (
            len(prepared["graph"]),
            len(prepared["cdn"]),
        ), where
        assert prepared["admissions"] == len(prepared["graph"]) + len(
            prepared["cdn"]
        ), where
        assert (prepared["mime"], prepared["bytes"]) == (
            "image/jpeg",
            len(IMAGE_BYTES),
        ), where
        # The first card's own budget: the Graph admissions that reach the first
        # candidate plus its preparation's, and its downloads.
        assert (
            first["graph_admissions"] + len(prepared["graph"]) <= card["max_graph"]
        ), where
        assert len(prepared["cdn"]) <= card["max_cdn"], where

    # Identity: a recorded id costs no `/me`; with none recorded, a helper turn
    # reads it once and never more, however many parents it enriches.
    if report["variant"] == "unrecorded":
        assert 0 < report["me_reads"] == report["me_helper_turns"], where
    else:
        assert report["me_reads"] == 0, where


async def measure(kind, n, variant, *, worker, origin, sync_conn, monkeypatch):
    report = await onboard(
        kind,
        n,
        variant,
        worker=worker,
        origin=origin,
        sync_conn=sync_conn,
        monkeypatch=monkeypatch,
    )
    write_report(report)
    print("ONBOARDING_BUDGET=" + json.dumps(report, sort_keys=True))
    check(report)
    return report


CASES = [
    (kind, n, "account") for kind in ("useful", "excluded", "carousel") for n in SIZES
] + [
    ("useful", 100, "recorded"),
    ("useful", 100, "unrecorded"),
]


@pytest.mark.parametrize(
    "kind,n,variant", CASES, ids=[f"{k}-{n}-{v}" for k, n, v in CASES]
)
async def test_traversal_stays_within_the_budgets_and_finds_what_the_manifest_says(
    kind, n, variant, worker, origin, sync_conn, monkeypatch
):
    await measure(
        kind,
        n,
        variant,
        worker=worker,
        origin=origin,
        sync_conn=sync_conn,
        monkeypatch=monkeypatch,
    )
