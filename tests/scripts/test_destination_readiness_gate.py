"""Destination readiness on the mix resource, under the actual runtime roles.

Destination readiness and its contract, read through
`versioned_mix.resource` as `svc_ingress` and `svc_worker` against the replayed
schema: a converted mix, and the archive capability injected the way only tests
inject it. Every scenario is a world here: A's history
On beside B's history Off with no Drive, a handle-only B, a shared Drive, a
moved B, all sources Off, no source at all, a reauth-required B and a
partial-coverage archive that is also ready. Tenancy is proven rather than
assumed: another workspace is seeded beside this one.

Beyond those, states no service reaches are built as owner fixtures: an archive
also assigned to a second destination, or with its own assignment retired; a
handle reference with a grant on file; a stalled history and a failed folder.
The resource's own additions are pinned too: the exact key sets of the wire
shape, `media_count` and `indexed`, the protocol-1 shape that gained nothing,
`archive_available`, and the statement budget of one GET.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from src.exceptions.mix import MixInvalid
from src.exceptions.tenancy import TenantResolutionError
from src.services.target import (
    category_mix,
    content_runway,
    instagram_sync,
    source_policy,
    tenant_resolution,
    versioned_mix,
)
from src.services.target.instagram_sources import ensure_archive_source
from src.services.target.unit_of_work import unit_of_work
from tests.scripts import test_instagram_observations_gate as obs
from tests.scripts import test_instagram_sources_gate as foundation
from tests.scripts import test_versioned_mix_gate as mix_gate
from tests.scripts.conftest import seed_workspace_chain
from tests.scripts.test_candidate_selection_gate import metadata
from tests.scripts.test_instagram_progress_gate import RecordedExecutor
from tests.scripts.test_source_suitability_gate import activate

archive_engine = foundation.archive_engine
origin = foundation.origin
#: The API's own login (`svc_ingress`), for the test that reads over HTTP.
ingress = mix_gate.ingress
pytestmark = [pytest.mark.integration, pytest.mark.slow]

REMOVED = "UPDATE media_sources SET config=config || '{\"removed\": true}'::jsonb"


def add_account(sync_conn, chain, ref, handle, *, credential=True, display_name=None):
    """A destination in *chain*'s workspace, as the migration actor, with its
    own `ig_login` grant unless told otherwise. Committed on its own, so the
    listed order (oldest first) is the order of these calls."""
    with sync_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ig_accounts (workspace_id, provider_account_ref, handle,"
            " display_name) VALUES (%s, %s, %s, %s) RETURNING id::text",
            (chain["ws"], ref, handle, display_name),
        )
        account = cur.fetchone()[0]
        if credential:
            cur.execute(
                "INSERT INTO oauth_credentials (workspace_id, ig_account_id,"
                " provider, encrypted_payload)"
                " VALUES (%s, %s, 'ig_login', 'test-ciphertext')",
                (chain["ws"], account),
            )
    sync_conn.commit()
    return account


def owner_write(sync_conn, sql, *params):
    """A fixture change as the migration actor, for a state no service reaches
    (an assignment of one archive to a second destination, a source whose
    folder is gone). Committed on its own."""
    with sync_conn.cursor() as cur:
        cur.execute(sql, params)
    sync_conn.commit()


def age(sync_conn, source_id, *, days=3):
    """*source_id*'s progress anchors, its creation and the audit of its use
    epoch, *days* in the past: a disposable fixture aged the way the progress
    gate ages one, with no clock mocked. Three days is past the two-interval
    budget (`media_sync.BASELINE_SECONDS`), so an indexing history stalls."""
    owner_write(
        sync_conn,
        "UPDATE media_sources SET created_at = now() - make_interval(days => %s)"
        " WHERE id = %s",
        days,
        source_id,
    )
    owner_write(
        sync_conn,
        "UPDATE audit_events SET created_at = now() - make_interval(days => %s)"
        " WHERE entity_kind = 'instagram_source' AND entity_id = %s",
        days,
        source_id,
    )


async def history(engine, chain, account, *, enabled):
    """*account*'s own archive source, created Off as a new connection lands it."""
    async with obs.uow(engine, chain) as conn:
        source = await ensure_archive_source(
            conn,
            workspace_id=str(chain["ws"]),
            origin_ig_account_id=account,
            authorization=foundation.authorization(),
            capability=foundation.capability(),
        )
        if enabled:
            await ratio(conn, chain, source["source_id"], 1)
    return source


async def ratio(conn, chain, source_id, value):
    await conn.execute(
        text(
            "INSERT INTO category_post_case_mix(workspace_id,source_id,category,ratio)"
            " VALUES(:ws,:sid,'synthetic',:ratio)"
            " ON CONFLICT (workspace_id,source_id)"
            " WHERE effective_to IS NULL AND source_id IS NOT NULL"
            " DO UPDATE SET ratio=EXCLUDED.ratio"
        ),
        {"ws": str(chain["ws"]), "sid": str(source_id), "ratio": value},
    )


async def inventory(conn, chain, tag, n):
    """*n* more available items in the chain's Drive folder. The chain's own
    item has a live intent for the chain's account, so it is no candidate for
    that account, only for the others."""
    await conn.execute(
        text(
            "INSERT INTO media_items(workspace_id,source_id,content_hash,"
            "provider_file_ref,file_name,media_kind)"
            " SELECT :ws,:sid,:tag||n,:tag||n,'r.jpg','image'"
            " FROM generate_series(1,CAST(:n AS integer)) n"
        ),
        {"ws": str(chain["ws"]), "sid": str(chain["src"]), "tag": tag, "n": n},
    )


async def write(engine, chain, sql, **params):
    async with obs.uow(engine, chain) as conn:
        await conn.execute(text(sql), {"ws": str(chain["ws"]), **params})


async def read(engine, chain, *, capability=True):
    """The mix resource as the API reads it, the archive capability injected
    the way only tests inject it, unless told otherwise."""
    async with obs.uow(engine, chain) as conn:
        if capability:
            conn.info["archive_capability"] = foundation.capability()
        return await versioned_mix.resource(conn, workspace_id=str(chain["ws"]))


def by_account(view):
    return {d["account_id"]: d for d in view["destinations"]}


def verdict(destination):
    return destination["state"], destination["tone"], destination["remedy"]


def entry(view, account, source_id):
    return next(
        s for s in by_account(view)[account]["sources"] if s["source_id"] == source_id
    )


def incomplete(account, state, remedy):
    return {"account_id": account, "state": state, "remedy": remedy}


def checkpoint(generation, **fields):
    """A saved sync checkpoint as `_land` writes one: a typed envelope of the
    counted generation, whose recent lane recorded a batch committed just now.
    *fields* replaces whole top-level fields of it, so a malformed shape is that
    one well-formed envelope with one field wrong, and a finished scan is the
    same envelope with its lanes complete."""
    saved = {
        "v": 1,
        "type": "instagram_archive_sync",
        "generation_id": generation,
        "revision": 1,
        "next_lane": "recent",
        "next_metadata": "recent",
        "recent": {"complete": False, "checkpoint": {}},
        "history": {"complete": False, "checkpoint": {}},
        "pending_helpers": [],
        "deferred_helpers": [],
        "progress": {
            "recent": {"last_committed_at": datetime.now(timezone.utc).isoformat()}
        },
        **fields,
    }
    return json.dumps(saved)


async def test_a_history_on_and_b_history_off_without_drive(
    archive_engine, origin, sync_conn
):
    a = str(origin["iga"])
    mine, item = await metadata(archive_engine, origin)
    b = add_account(
        sync_conn, origin, "acct-readiness-b", "example_b", display_name="Example B"
    )
    theirs = await history(archive_engine, origin, b, enabled=False)
    await write(archive_engine, origin, REMOVED + " WHERE id=:sid", sid=origin["src"])
    view = await read(archive_engine, origin)
    assert view["archive_available"] is True
    rows = {r["source_id"]: r for r in view["rows"]}
    assert list(rows) == [mine["source_id"], theirs["source_id"]]
    assert (
        rows[mine["source_id"]]["media_count"],
        rows[mine["source_id"]]["indexed"],
    ) == (1, 1)
    # Nothing observed yet: an unknown count, not zero.
    assert (
        rows[theirs["source_id"]]["media_count"],
        rows[theirs["source_id"]]["indexed"],
    ) == (0, None)
    assert [d["account_id"] for d in view["destinations"]] == [a, b]
    da, db = by_account(view)[a], by_account(view)[b]
    assert verdict(da) == ("ready", "active", None)
    assert (da["candidates"], da["eligible"], da["handle"]) == (1, 0, None)
    assert da["display_name"] is None
    # A's archive cannot complete B's setup; B's own history is Off.
    assert verdict(db) == ("no_assignable_source", "attention", "enable_history")
    assert (db["candidates"], db["handle"]) == (0, "example_b")
    assert db["display_name"] == "Example B"
    assert [s["assignable"] for s in da["sources"]] == [True, False]
    assert [s["assignable"] for s in db["sources"]] == [False, True]
    # Metadata matches, not yet verified for posting: the two counts differ.
    held = entry(view, a, mine["source_id"])
    assert (held["candidates"], held["eligible"]) == (1, 0)
    assert held["effective_share"] == "1"
    assert entry(view, b, mine["source_id"])["effective_share"] == "0"
    assert view["guidance"] == {
        "active": 2,
        "ready": 1,
        "intentional": None,
        "incomplete": [incomplete(b, "no_assignable_source", "enable_history")],
    }

    # B's history On: indexing, with nothing observed yet. Never A's supply.
    async with obs.uow(archive_engine, origin) as conn:
        await ratio(conn, origin, theirs["source_id"], 1)
    view = await read(archive_engine, origin)
    assert verdict(by_account(view)[b]) == ("indexing", "progress", None)
    assert verdict(by_account(view)[a]) == ("ready", "active", None)
    own = entry(view, b, theirs["source_id"])
    assert own["indexing"] and own["candidates"] == 0 and not own["stalled"]
    assert entry(view, b, mine["source_id"])["candidates"] == 0

    # B's history removed: nothing of B's to enable, so a source is the remedy.
    await write(
        archive_engine, origin, REMOVED + " WHERE id=:sid", sid=theirs["source_id"]
    )
    view = await read(archive_engine, origin)
    assert verdict(by_account(view)[b]) == (
        "no_assignable_source",
        "attention",
        "add_source",
    )


async def test_a_handle_only_destination_needs_connection_and_a_dead_grant_reconnect(
    archive_engine, origin, sync_conn
):
    a = str(origin["iga"])
    b = add_account(
        sync_conn, origin, "manual:example_b", "example_b", credential=False
    )
    c = add_account(sync_conn, origin, "acct-readiness-c", "example_c")
    async with obs.uow(archive_engine, origin) as conn:
        await activate(conn, origin["ws"])
        await ratio(conn, origin, origin["src"], 1)
        await inventory(conn, origin, "handle-", 2)
    view = await read(archive_engine, origin)
    assert view["archive_available"] is True and view["rows"][0]["indexed"] is None
    assert view["rows"][0]["media_count"] == 3
    d = by_account(view)
    assert verdict(d[a]) == verdict(d[c]) == ("ready", "active", None)
    assert (d[a]["candidates"], d[c]["candidates"]) == (2, 3)
    # A connection is owed by a handle-only destination, whatever it could draw.
    assert verdict(d[b]) == ("needs_connection", "attention", "connect")
    assert d[b]["candidates"] == 3

    await write(
        archive_engine,
        origin,
        "UPDATE ig_accounts SET state='reauth_required' WHERE id=:id",
        id=c,
    )
    view = await read(archive_engine, origin)
    d = by_account(view)
    assert verdict(d[c]) == ("needs_reconnect", "attention", "reconnect")
    # Nothing can be posted to it: no supply shows.
    assert d[c]["candidates"] == d[c]["eligible"] == 0
    assert [(s["candidates"], s["effective_share"]) for s in d[c]["sources"]] == [
        (0, "0")
    ]
    assert view["guidance"] == {
        "active": 3,
        "ready": 1,
        "intentional": None,
        "incomplete": [
            incomplete(b, "needs_connection", "connect"),
            incomplete(c, "needs_reconnect", "reconnect"),
        ],
    }

    # The account active again, its own credential revoked: still its grant.
    await write(
        archive_engine,
        origin,
        "UPDATE ig_accounts SET state='active' WHERE id=:id",
        id=c,
    )
    await write(
        archive_engine,
        origin,
        "UPDATE oauth_credentials SET state='revoked' WHERE ig_account_id=:id",
        id=c,
    )
    d = by_account(await read(archive_engine, origin))
    assert verdict(d[c]) == ("needs_reconnect", "attention", "reconnect")
    assert d[c]["candidates"] == 0
    assert verdict(d[a]) == ("ready", "active", None) and d[a]["candidates"] == 2


async def test_a_shared_drive_readies_both_and_a_moved_one_leaves_the_denominator(
    archive_engine, origin, sync_conn
):
    a = str(origin["iga"])
    b = add_account(sync_conn, origin, "acct-readiness-b", "example_b")
    async with obs.uow(archive_engine, origin) as conn:
        await activate(conn, origin["ws"])
        await ratio(conn, origin, origin["src"], 1)
        await inventory(conn, origin, "shared-", 1)
    view = await read(archive_engine, origin)
    d = by_account(view)
    assert verdict(d[a]) == verdict(d[b]) == ("ready", "active", None)
    assert all(s["assignable"] for s in d[a]["sources"] + d[b]["sources"])
    assert [s["effective_share"] for s in d[a]["sources"] + d[b]["sources"]] == [
        "1",
        "1",
    ]
    assert view["guidance"] == {
        "active": 2,
        "ready": 2,
        "intentional": None,
        "incomplete": [],
    }

    await write(
        archive_engine,
        origin,
        "UPDATE ig_accounts SET state='moved' WHERE id=:id",
        id=b,
    )
    view = await read(archive_engine, origin)
    assert verdict(by_account(view)[b]) == ("inactive", "inert", None)
    assert view["guidance"] == {
        "active": 1,
        "ready": 1,
        "intentional": None,
        "incomplete": [],
    }

    await write(
        archive_engine,
        origin,
        "UPDATE ig_accounts SET state='moved' WHERE id=:id",
        id=a,
    )
    assert (await read(archive_engine, origin))["guidance"] == {
        "active": 0,
        "ready": 0,
        "intentional": "all_inactive",
        "incomplete": [],
    }


async def test_all_sources_off_is_intentional_and_no_source_at_all_needs_one(
    archive_engine, origin, sync_conn
):
    a = str(origin["iga"])
    b = add_account(sync_conn, origin, "acct-readiness-b", "example_b")
    await history(archive_engine, origin, a, enabled=False)
    async with obs.uow(archive_engine, origin) as conn:
        await activate(conn, origin["ws"])
        await ratio(conn, origin, origin["src"], 0)
    view = await read(archive_engine, origin)
    d = by_account(view)
    # A's own history is Off too, yet the choice is the workspace's: all Off.
    assert verdict(d[a]) == verdict(d[b]) == ("sources_off", "inert", None)
    assert view["guidance"] == {
        "active": 2,
        "ready": 0,
        "intentional": "all_sources_off",
        "incomplete": [],
    }

    await write(archive_engine, origin, REMOVED + " WHERE workspace_id=:ws")
    view = await read(archive_engine, origin)
    assert view["rows"] == []
    d = by_account(view)
    assert (
        verdict(d[a])
        == verdict(d[b])
        == (
            "no_assignable_source",
            "attention",
            "add_source",
        )
    )
    assert view["guidance"] == {
        "active": 2,
        "ready": 0,
        "intentional": None,
        "incomplete": [
            incomplete(a, "no_assignable_source", "add_source"),
            incomplete(b, "no_assignable_source", "add_source"),
        ],
    }


async def test_a_partial_archive_is_ready_and_the_leading_exclusion_names_a_remedy(
    archive_engine, origin
):
    a = str(origin["iga"])
    mine, item = await metadata(archive_engine, origin)
    view = await read(archive_engine, origin)
    da = by_account(view)[a]
    # Never scanned to the end: partial coverage, and ready all the same.
    assert verdict(da) == ("ready", "active", None) and da["partial_coverage"]
    own = entry(view, a, mine["source_id"])
    assert own["partial_coverage"] and own["indexing"] and not own["stalled"]
    assert not own["needs_reconnect"] and own["exclusions"] == {}
    assert datetime.fromisoformat(own["oldest_observed_at"]) == datetime(
        2020, 1, 1, tzinfo=timezone.utc
    )

    complete = checkpoint(
        mine["generation_id"],
        recent={"complete": True, "checkpoint": {}},
        history={"complete": True, "checkpoint": {}},
        progress={},
    )
    await write(
        archive_engine,
        origin,
        "UPDATE media_sources SET sync_checkpoint=CAST(:cp AS jsonb) WHERE id=:sid",
        cp=complete,
        sid=mine["source_id"],
    )
    view = await read(archive_engine, origin)
    da = by_account(view)[a]
    assert verdict(da) == ("ready", "active", None) and not da["partial_coverage"]
    assert not entry(view, a, mine["source_id"])["indexing"]

    # Its one post too recent: nothing matches, and the policy is the remedy.
    await write(
        archive_engine,
        origin,
        "UPDATE instagram_media_index SET original_at=now()-interval '1 day'"
        " WHERE source_id=:sid",
        sid=mine["source_id"],
    )
    view = await read(archive_engine, origin)
    da = by_account(view)[a]
    assert verdict(da) == ("zero_eligible", "attention", "review_policy")
    assert da["leading_exclusion"] == {"reason": "too_recent", "count": 1}
    assert entry(view, a, mine["source_id"])["exclusions"] == {"too_recent": 1}

    # Held by a lock instead: no policy leads, so no remedy is named.
    await write(
        archive_engine,
        origin,
        "UPDATE instagram_media_index SET original_at='2020-01-01T00:00:00Z'"
        " WHERE source_id=:sid",
        sid=mine["source_id"],
    )
    await write(
        archive_engine,
        origin,
        "INSERT INTO post_locks(workspace_id,media_item_id,kind)"
        " VALUES(:ws,:item,'hold')",
        item=item.media_item_id,
    )
    view = await read(archive_engine, origin)
    da = by_account(view)[a]
    assert verdict(da) == ("zero_eligible", "attention", None)
    assert da["leading_exclusion"] == {"reason": "locked", "count": 1}
    assert entry(view, a, mine["source_id"])["exclusions"] == {"locked": 1}

    # Its generation pending deletion: the origin grant is unusable and no
    # Drive folder is usable, while the destination's own grant is fine.
    await write(
        archive_engine,
        origin,
        "UPDATE instagram_archive_generations SET lifecycle='deletion_pending'"
        " WHERE id=:gid",
        gid=mine["generation_id"],
    )
    view = await read(archive_engine, origin)
    assert verdict(by_account(view)[a]) == ("needs_reconnect", "attention", "reconnect")
    assert entry(view, a, mine["source_id"])["needs_reconnect"]
    # A usable Drive folder, empty for A: waiting on content, not a reconnect.
    async with obs.uow(archive_engine, origin) as conn:
        await ratio(conn, origin, origin["src"], 1)
    da = by_account(await read(archive_engine, origin))[a]
    assert verdict(da) == ("zero_eligible", "attention", None)


async def test_a_manual_reference_needs_connection_even_with_a_grant_on_file(
    archive_engine, origin, sync_conn
):
    a = str(origin["iga"])
    # Added by handle alone, though a credential row exists for it.
    b = add_account(sync_conn, origin, "manual:example_b", "example_b")
    # Control: the same grant under a real reference connects.
    c = add_account(sync_conn, origin, "acct-readiness-c", "example_c")
    async with obs.uow(archive_engine, origin) as conn:
        await activate(conn, origin["ws"])
        await ratio(conn, origin, origin["src"], 1)
        await inventory(conn, origin, "manual-", 1)
    d = by_account(await read(archive_engine, origin))
    assert verdict(d[b]) == ("needs_connection", "attention", "connect")
    assert verdict(d[a]) == verdict(d[c]) == ("ready", "active", None)


async def test_an_archive_serves_its_origin_alone_through_an_active_assignment(
    archive_engine, origin, sync_conn
):
    a = str(origin["iga"])
    mine, _ = await metadata(archive_engine, origin)
    b = add_account(sync_conn, origin, "acct-readiness-b", "example_b")
    theirs = await history(archive_engine, origin, b, enabled=True)
    await write(archive_engine, origin, REMOVED + " WHERE id=:sid", sid=origin["src"])
    # B's history is assigned to A as well. An archive serves its own origin,
    # whatever else it is assigned to (the v1 `origin_only` rule).
    owner_write(
        sync_conn,
        "INSERT INTO media_source_destinations (workspace_id, source_id,"
        " ig_account_id) VALUES (%s, %s, %s)",
        origin["ws"],
        theirs["source_id"],
        a,
    )
    view = await read(archive_engine, origin)
    d = by_account(view)
    assert [s["assignable"] for s in d[a]["sources"]] == [True, False]
    assert [s["assignable"] for s in d[b]["sources"]] == [False, True]
    assert verdict(d[a]) == ("ready", "active", None)
    assert verdict(d[b]) == ("indexing", "progress", None)

    # B's own assignment retired: its history is On, yet serves nobody. It is
    # not B's to enable, since it is enabled, so a source is the remedy.
    owner_write(
        sync_conn,
        "UPDATE media_source_destinations SET state = 'retired'"
        " WHERE source_id = %s AND ig_account_id = %s",
        theirs["source_id"],
        b,
    )
    d = by_account(await read(archive_engine, origin))
    assert [s["assignable"] for s in d[b]["sources"]] == [False, False]
    assert verdict(d[b]) == ("no_assignable_source", "attention", "add_source")
    # Control: A, still holding its own, is unmoved.
    assert [s["assignable"] for s in d[a]["sources"]] == [True, False]
    assert verdict(d[a]) == ("ready", "active", None)


async def test_a_stalled_history_and_a_failed_folder_are_problems_to_retry(
    archive_engine, origin, sync_conn
):
    a = str(origin["iga"])
    b = add_account(sync_conn, origin, "acct-readiness-b", "example_b")
    await write(archive_engine, origin, REMOVED + " WHERE id=:sid", sid=origin["src"])
    theirs = await history(archive_engine, origin, b, enabled=True)
    async with obs.uow(archive_engine, origin) as conn:
        await activate(conn, origin["ws"])
    view = await read(archive_engine, origin)
    assert verdict(by_account(view)[b]) == ("indexing", "progress", None)
    assert not entry(view, b, theirs["source_id"])["stalled"]

    # Three days without a committed batch: past its budget. A retry is the
    # remedy, and A, with nothing to draw on, is listed before B's problem.
    age(sync_conn, theirs["source_id"])
    view = await read(archive_engine, origin)
    assert verdict(by_account(view)[b]) == ("stalled", "problem", "retry")
    assert entry(view, b, theirs["source_id"])["stalled"]
    assert view["guidance"]["incomplete"] == [
        incomplete(a, "no_assignable_source", "add_source"),
        incomplete(b, "stalled", "retry"),
    ]

    # The shared folder back, but its folder is gone in Drive: a named failure
    # for the destination waiting on it. B's stall still outranks it for B, and
    # the guidance lists B before A: state order, not account order.
    owner_write(
        sync_conn,
        "UPDATE media_sources SET state = 'error', config = config - 'removed'"
        " WHERE id = %s",
        origin["src"],
    )
    async with obs.uow(archive_engine, origin) as conn:
        await ratio(conn, origin, origin["src"], 1)
    view = await read(archive_engine, origin)
    assert verdict(by_account(view)[a]) == ("error", "problem", "retry")
    assert verdict(by_account(view)[b]) == ("stalled", "problem", "retry")
    assert view["guidance"]["incomplete"] == [
        incomplete(b, "stalled", "retry"),
        incomplete(a, "error", "retry"),
    ]


#: Every way a saved checkpoint of the counted generation can be unreadable.
#: The first is a time that does not parse. The next four would each be a
#: `TypeError` or `AttributeError` at their own line of `instagram_sync.status`
#: if indexed unchecked. The last three read without a crash yet are unusable:
#: a `complete` that is a string is truthy, and a `revision` below zero or a
#: `type` that is not the sync's is a checkpoint the sync itself will not
#: resume from. The lanes are made complete so a read reaches the bad field
#: (`all` stops at the first incomplete lane, and a scan with work remaining
#: never asks for a cursor's ceiling).
FINISHED = {"complete": True, "checkpoint": {}}
MALFORMED_CHECKPOINTS = {
    "an_unparseable_time": {
        "progress": {"recent": {"last_committed_at": "not-a-timestamp"}}
    },
    "a_null_helper_queue": {"pending_helpers": None},
    "a_lane_that_is_not_an_object": {"recent": []},
    "the_other_lane_a_string": {"recent": FINISHED, "history": "done"},
    "a_progress_record_that_is_a_list": {"progress": []},
    "a_lane_whose_cursor_is_null": {
        "recent": {"complete": True, "checkpoint": None},
        "history": FINISHED,
    },
    "a_lane_flag_that_is_not_a_boolean": {
        "history": {"complete": "yes", "checkpoint": {}}
    },
    "a_revision_below_zero": {"revision": -1},
    "a_type_that_is_not_the_syncs": {"type": "provider_cursor"},
}


@pytest.mark.parametrize(
    "corrupted",
    MALFORMED_CHECKPOINTS.values(),
    ids=MALFORMED_CHECKPOINTS.keys(),
)
async def test_a_malformed_checkpoint_reads_that_archive_as_stalled_and_the_get_still_answers(
    ingress, origin, sync_conn, caplog, corrupted
):
    """One unreadable checkpoint degrades that archive alone, not the whole mix
    GET and with it the editor an admin acts from: stalled and partial, no
    coverage, a retry the remedy, and one warning naming the source and the
    reason, never the checkpoint.

    The histories are created in the mix's row order: B's, then A's (the one
    under test), then C's. A read that gave up at the first bad checkpoint
    would lose C's facts, and one that degraded every archive once any was bad
    would show on B."""
    a = str(origin["iga"])
    b = add_account(sync_conn, origin, "acct-readiness-b", "example_b")
    c = add_account(sync_conn, origin, "acct-readiness-c", "example_c")
    first = await history(ingress, origin, b, enabled=True)
    # A's history is On with one post, too recent to match: still being indexed.
    mine, _ = await metadata(ingress, origin, recent=True)
    last = await history(ingress, origin, c, enabled=True)
    mix = f"/api/v1/workspaces/{origin['ws']}/category-mix"

    async def save(**fields):
        await write(
            ingress,
            origin,
            "UPDATE media_sources SET sync_checkpoint = CAST(:cp AS jsonb)"
            " WHERE id = :sid",
            cp=checkpoint(mine["generation_id"], **fields),
            sid=mine["source_id"],
        )

    async def get_and_listen(client):
        """The GET's answer, which must be a 200, and what the projection said."""
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=category_mix.__name__):
            response = await client.get(mix)
        assert response.status_code == 200, response.text
        return response.json(), [
            r.getMessage() for r in caplog.records if r.name == category_mix.__name__
        ]

    async with mix_gate.mix_api(
        ingress, origin["user"], capability=foundation.capability()
    ) as client:
        # Control: no checkpoint saved yet. Indexing, not stalled, and A's one
        # observation is its coverage.
        view, said = await get_and_listen(client)
        own = entry(view, a, mine["source_id"])
        assert verdict(by_account(view)[a]) == ("indexing", "progress", None)
        assert (own["indexing"], own["stalled"], own["partial_coverage"]) == (
            True,
            False,
            True,
        )
        assert own["oldest_observed_at"] is not None
        coverage = own["oldest_observed_at"]
        assert said == []

        # The checkpoint corrupted: the same read answers 200, and only A's
        # archive is degraded.
        await save(**corrupted)
        view, said = await get_and_listen(client)
        da = by_account(view)[a]
        own = entry(view, a, mine["source_id"])
        assert verdict(da) == ("stalled", "problem", "retry")
        assert da["partial_coverage"] is True
        assert (own["stalled"], own["partial_coverage"]) == (True, True)
        assert (own["indexing"], own["needs_reconnect"]) == (False, False)
        assert own["oldest_observed_at"] is None
        # The candidates' own read is untouched by it.
        assert da["leading_exclusion"] == {"reason": "too_recent", "count": 1}
        # The histories on either side, in the same read, are read as ever.
        for account, source in ((b, first), (c, last)):
            assert verdict(by_account(view)[account]) == ("indexing", "progress", None)
            unharmed = entry(view, account, source["source_id"])
            assert (unharmed["indexing"], unharmed["stalled"]) == (True, False)
        assert view["guidance"]["incomplete"] == [
            incomplete(a, "stalled", "retry"),
            incomplete(b, "indexing", None),
            incomplete(c, "indexing", None),
        ]
        # The whole message: the source and the reason, nothing of the value.
        assert said == [
            f"archive source {mine['source_id']}: checkpoint unreadable"
            " (unsupported_archive_sync_checkpoint); read as stalled"
        ]

        # Positive control: the same archive, its checkpoint well-formed and
        # fresh, is not stalled, keeps its coverage and says nothing.
        await save()
        view, said = await get_and_listen(client)
        own = entry(view, a, mine["source_id"])
        assert verdict(by_account(view)[a]) == ("indexing", "progress", None)
        assert (own["indexing"], own["stalled"]) == (True, False)
        assert own["oldest_observed_at"] == coverage
        assert said == []


async def source_status(engine, chain, source_id):
    """`instagram_sync.status` of one archive, read as the projection reads it:
    under one suitability context, the archive capability injected."""
    async with obs.uow(engine, chain) as conn:
        context = await source_policy.suitability_context(
            conn, workspace_id=str(chain["ws"])
        )
        return await instagram_sync.status(
            conn,
            context=context,
            source_id=source_id,
            capability=foundation.capability(),
        )


async def test_a_source_config_that_is_not_an_object_does_not_fail_the_get(
    archive_engine, origin, sync_conn
):
    """`media_sources.config` is an object wherever a writer builds it, but its
    CHECK lets any other JSON through (`config->'v'` is NULL for one, and a CHECK
    passes on NULL), so one such row must not fail the whole GET. "Removed" is
    the database's own expression there, as in every other statement of the
    read, so a config it cannot read is not removed."""
    a = str(origin["iga"])
    mine, _ = await metadata(archive_engine, origin)
    before = await read(archive_engine, origin)
    held = entry(before, a, mine["source_id"])
    assert (held["indexing"], held["stalled"], held["needs_reconnect"]) == (
        True,
        False,
        False,
    )

    owner_write(
        sync_conn,
        "UPDATE media_sources SET config = '[1]'::jsonb WHERE id = %s",
        mine["source_id"],
    )
    after = await read(archive_engine, origin)

    # The same read, unchanged by it: nothing degrades, nothing is retired.
    assert after == before
    live = await source_status(archive_engine, origin, mine["source_id"])
    assert (live["connected"], live["enabled"]) == (True, True)

    # Control: a `removed` it can read still retires the source in `status`.
    owner_write(
        sync_conn,
        'UPDATE media_sources SET config = \'{"v": 1, "removed": true}\'::jsonb'
        " WHERE id = %s",
        mine["source_id"],
    )
    gone = await source_status(archive_engine, origin, mine["source_id"])
    assert (gone["connected"], gone["enabled"], gone["needs_reconnect"]) == (
        False,
        False,
        False,
    )
    assert mine["source_id"] not in [
        r["source_id"] for r in (await read(archive_engine, origin))["rows"]
    ]


async def test_media_count_is_the_retained_catalog_and_indexed_the_current_generation(
    archive_engine, origin, sync_conn
):
    first, _ = await metadata(archive_engine, origin)
    await obs.observe(archive_engine, origin, first, obs.bundle(parent="456"))

    async def archive_counts():
        rows = {r["source_id"]: r for r in (await read(archive_engine, origin))["rows"]}
        mine = rows[first["source_id"]]
        return (mine["media_count"], mine["indexed"]), rows

    counts, rows = await archive_counts()
    assert counts == (2, 2)
    # The chain's own Drive item counts, whatever its intent or its folder's
    # Off, and a Drive has no observations to count.
    drive = rows[str(origin["src"])]
    assert (drive["media_count"], drive["indexed"]) == (1, None)

    # A reauthorization opens a new generation: the posts stay retained, but
    # none is observed in it yet, which is an unknown count, not the old two.
    second = await foundation.ensure(archive_engine, origin, foundation.authorization())
    assert second["source_id"] == first["source_id"]
    assert (await archive_counts())[0] == (2, None)
    await obs.observe(archive_engine, origin, second, obs.bundle(parent="789"))
    assert (await archive_counts())[0] == (3, 1)

    # Erasing the first generation tombstones its posts: they leave the
    # catalog count, and the current generation's observation is untouched.
    await obs.redact(archive_engine, origin, first)
    assert (await archive_counts())[0] == (1, 1)

    # A generation marked deleted before its posts are redacted: they are not
    # retained either, though nothing has tombstoned them yet.
    owner_write(
        sync_conn,
        "UPDATE instagram_archive_generations SET lifecycle = 'deleted' WHERE id = %s",
        second["generation_id"],
    )
    assert (await archive_counts())[0][0] == 0


async def test_a_member_reads_the_same_projection_and_no_other_workspace_appears(
    archive_engine, origin, sync_conn
):
    a = str(origin["iga"])
    mine, _ = await metadata(archive_engine, origin)
    other = seed_workspace_chain(sync_conn, "readiness-other")
    with sync_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO oauth_credentials (workspace_id, ig_account_id, provider,"
            " encrypted_payload) VALUES (%s, %s, 'ig_login', 'test-ciphertext')",
            (other["ws"], other["iga"]),
        )
        cur.execute(
            "INSERT INTO category_post_case_mix (workspace_id, source_id, category,"
            " ratio) VALUES (%s, %s, 'synthetic', 1)",
            (other["ws"], other["src"]),
        )
        cur.execute("INSERT INTO users DEFAULT VALUES RETURNING id::text")
        member = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO workspace_members (workspace_id, user_id, role)"
            " VALUES (%s, %s, 'member')",
            (origin["ws"], member),
        )
    sync_conn.commit()
    async with obs.uow(archive_engine, other) as conn:
        await activate(conn, other["ws"])

    owner = await read(archive_engine, origin)
    async with unit_of_work(
        archive_engine,
        str(origin["ws"]),
        actor_kind="user",
        actor_user_id=member,
        channel="web",
    ).begin() as conn:
        conn.info["archive_capability"] = foundation.capability()
        # The GET's own floor: a member passes it, and is no admin.
        assert (
            await tenant_resolution.authorize_member(conn, str(origin["ws"]), member)
            == "member"
        )
        with pytest.raises(TenantResolutionError):
            await tenant_resolution.authorize_member(
                conn, str(origin["ws"]), member, minimum_role="admin"
            )
        assert (
            await versioned_mix.resource(conn, workspace_id=str(origin["ws"])) == owner
        )

    sources = {str(origin["src"]), mine["source_id"]}
    assert [d["account_id"] for d in owner["destinations"]] == [a]
    assert {r["source_id"] for r in owner["rows"]} == sources
    assert {s["source_id"] for s in owner["destinations"][0]["sources"]} == sources
    theirs = await read(archive_engine, other)
    assert [d["account_id"] for d in theirs["destinations"]] == [str(other["iga"])]
    assert [r["source_id"] for r in theirs["rows"]] == [str(other["src"])]
    # Bound to the other tenant, this workspace is absent, refused by name.
    async with obs.uow(archive_engine, other) as conn:
        with pytest.raises(MixInvalid) as refused:
            await versioned_mix.resource(conn, workspace_id=str(origin["ws"]))
    assert refused.value.reason == "unknown_workspace"


#: The names the web builds against: exactly these, no more and no fewer.
RESOURCE_KEYS = {
    "protocol_version",
    "policy_version",
    "revision",
    "mode",
    "supported_actions",
    "conversion",
    "archive_available",
    "rows",
    "destinations",
    "guidance",
    "reset_explanation",
}
ROW_KEYS = {
    "source_id",
    "provider",
    "state",
    "origin_account_id",
    "origin_handle",
    "name",
    "enabled",
    "ratio",
    "target_share",
    "media_count",
    "indexed",
}
DESTINATION_KEYS = {
    "account_id",
    "handle",
    "display_name",
    "state",
    "tone",
    "partial_coverage",
    "candidates",
    "eligible",
    "leading_exclusion",
    "remedy",
    "sources",
}
DESTINATION_SOURCE_KEYS = {
    "source_id",
    "assignable",
    "candidates",
    "eligible",
    "effective_share",
    "exclusions",
    "indexing",
    "partial_coverage",
    "stalled",
    "needs_reconnect",
    "oldest_observed_at",
}


async def test_the_wire_shape_is_exactly_the_contracts(
    archive_engine, origin, sync_conn
):
    await metadata(archive_engine, origin)
    add_account(sync_conn, origin, "acct-readiness-b", "example_b")
    view = await read(archive_engine, origin)
    assert set(view) == RESOURCE_KEYS
    assert len(view["rows"]) == len(view["destinations"]) == 2
    assert all(set(row) == ROW_KEYS for row in view["rows"])
    for destination in view["destinations"]:
        assert set(destination) == DESTINATION_KEYS
        assert len(destination["sources"]) == len(view["rows"])
        for source in destination["sources"]:
            assert set(source) == DESTINATION_SOURCE_KEYS
            # A decimal string, never a float or a Decimal on the wire.
            assert isinstance(source["effective_share"], str)
    assert set(view["guidance"]) == {"active", "ready", "intentional", "incomplete"}
    # Plain JSON all the way down: no Decimal, datetime or UUID leaks out.
    assert json.loads(json.dumps(view)) == view


#: A protocol-1 row: exactly the legacy card's own keys.
LEGACY_ROW_KEYS = {
    "source_id",
    "provider",
    "name",
    "state",
    "media_count",
    "ratio",
    "effective",
}


async def test_protocol_1_is_unchanged_and_archive_available_follows_the_capability(
    archive_engine, origin
):
    # A workspace not yet converted answers protocol 1: its rows and nothing
    # else, whether or not the capability is injected.
    for capability in (True, False):
        legacy = await read(archive_engine, origin, capability=capability)
        assert set(legacy) == {
            "protocol_version",
            "rows",
            "supported_actions",
            "conversion",
        }
        assert legacy["protocol_version"] == 1
        assert legacy["supported_actions"] == ["legacy_save"]
        assert legacy["conversion"] == {"state": "pending"}
        assert [set(r) for r in legacy["rows"]] == [LEGACY_ROW_KEYS]

    # Converted, the capability alone decides `archive_available`.
    async with obs.uow(archive_engine, origin) as conn:
        await activate(conn, origin["ws"])
        await ratio(conn, origin, origin["src"], 1)
        await inventory(conn, origin, "protocol-", 1)
    on = await read(archive_engine, origin)
    off = await read(archive_engine, origin, capability=False)
    assert on["protocol_version"] == off["protocol_version"] == 2
    assert (on["archive_available"], off["archive_available"]) == (True, False)
    # A Drive-only workspace reads the same either way: ready, with its
    # supply, and nothing else tells the two apart.
    a = str(origin["iga"])
    assert verdict(by_account(off)[a]) == ("ready", "active", None)
    assert by_account(off)[a]["candidates"] == 1
    assert {**on, "archive_available": None} == {**off, "archive_available": None}


async def counted_read(engine, chain):
    async with obs.uow(engine, chain) as conn:
        conn.info["archive_capability"] = foundation.capability()
        recorded = RecordedExecutor(conn)
        view = await versioned_mix.resource(recorded, workspace_id=str(chain["ws"]))
        return view, recorded.calls


def locking(statements):
    """The statements among *statements* that take a lock the worker's writes
    contend on: a row lock or an advisory lock."""
    return [
        i for i, s in enumerate(statements) if "FOR UPDATE" in s or "pg_advisory" in s
    ]


async def test_a_protocol_1_get_takes_no_lock_and_a_converted_one_takes_the_prefix_after_its_probe(
    archive_engine, origin
):
    """A workspace not yet converted, the state every workspace starts in:
    the GET is its state and the card's rows, two plain SELECTs, and none of the
    prefix (the workspace, its accounts and its sources, row-locked). A
    converted workspace reads that state first with no lock too, then takes the
    prefix, then reads the state again under it: the probe, ten statements of
    prefix, the state."""
    async with obs.uow(archive_engine, origin) as conn:
        recorded = RecordedExecutor(conn)
        legacy = await versioned_mix.resource(recorded, workspace_id=str(origin["ws"]))
    assert legacy["protocol_version"] == 1
    statements = [" ".join(str(s).split()) for s, _ in recorded.calls]
    assert len(statements) == 2
    assert "source_mix_mode,source_mix_policy_version" in statements[0]
    assert locking(statements) == []

    async with obs.uow(archive_engine, origin) as conn:
        await activate(conn, origin["ws"])
        await ratio(conn, origin, origin["src"], 1)
    view, calls = await counted_read(archive_engine, origin)
    assert view["protocol_version"] == 2
    statements = [" ".join(str(s).split()) for s, _ in calls]
    reads = [
        i
        for i, s in enumerate(statements)
        if "source_mix_mode,source_mix_policy_version" in s
    ]
    # The resource's own two reads: the probe first, then the state again under
    # the prefix. (`pool` reads it once more per destination, later on.)
    assert reads[:2] == [0, 11]
    assert locking(statements[:1]) == [] and locking(statements[1:11]) != []


async def test_the_overviews_runway_takes_no_lock_until_the_workspace_has_converted(
    archive_engine, origin, sync_conn
):
    """The Overview loads the runway for every workspace. A suitability
    context takes the whole prefix, and the legacy pool reads none of its
    binds, so for a workspace that has not converted the runway reads no
    lock: no locking statement, and a writer holding the workspace delays it
    not at all. Converted, it takes the prefix, waits for the writer and then
    answers."""
    ws, account = str(origin["ws"]), str(origin["iga"])

    async def runway(executor):
        return await content_runway.runway(executor, workspace_id=ws, below_days=7)

    async with obs.uow(archive_engine, origin) as conn:
        recorded = RecordedExecutor(conn)
        quiet = await runway(recorded)
    assert [a["id"] for a in quiet["accounts"]] == [account]
    statements = [" ".join(str(s).split()) for s, _ in recorded.calls]
    assert locking(statements) == []

    hold_the_workspace(sync_conn, ws)
    try:

        async def held():
            async with obs.uow(archive_engine, origin) as conn:
                return await runway(conn)

        # A read that waited here would sit until the writer left.
        under_a_writer = await asyncio.wait_for(held(), timeout=10)
    finally:
        sync_conn.rollback()
    assert under_a_writer == quiet

    async with obs.uow(archive_engine, origin) as conn:
        await activate(conn, origin["ws"])
        await ratio(conn, origin, origin["src"], 1)
    hold_the_workspace(sync_conn, ws)

    async def converted():
        async with obs.uow(archive_engine, origin) as conn:
            conn.info["archive_capability"] = foundation.capability()
            return await runway(conn)

    waiting = asyncio.create_task(converted())
    try:
        await a_request_waits_on_a_lock(sync_conn)
        assert not waiting.done(), "a converted workspace's runway takes the prefix"
    finally:
        sync_conn.rollback()
    after = await asyncio.wait_for(waiting, timeout=10)
    assert [a["id"] for a in after["accounts"]] == [account]


async def test_an_unlocked_context_takes_no_lock_and_refuses_a_live_archive(
    archive_engine, origin
):
    ws = str(origin["ws"])
    async with obs.uow(archive_engine, origin) as conn:
        recorded = RecordedExecutor(conn)
        context = await source_policy.unlocked_context(
            recorded, workspace_id=ws, capability=foundation.capability()
        )
    # Not converted: nothing is activated, whatever the capability says.
    assert context.archive_enabled is False
    statements = [" ".join(str(s).split()) for s, _ in recorded.calls]
    assert len(statements) == 1 and locking(statements) == []

    async with obs.uow(archive_engine, origin) as conn:
        await activate(conn, origin["ws"])
        await ratio(conn, origin, origin["src"], 1)
    async with obs.uow(archive_engine, origin) as conn:
        with pytest.raises(RuntimeError, match="unlocked_context_for_an_activated"):
            await source_policy.unlocked_context(
                conn, workspace_id=ws, capability=foundation.capability()
            )
        # Control: the same converted workspace with no capability injected, the
        # rollback's state, activates nothing, so there is no archive fact to see.
        context = await source_policy.unlocked_context(conn, workspace_id=ws)
        assert context.archive_enabled is False


async def test_a_workspace_converted_by_an_unknown_release_is_refused_by_name_with_no_lock(
    archive_engine, origin, sync_conn
):
    """Neither the protocol this code serves nor pending: a conversion a newer
    release made (its policy version is one this reader does not know). It is
    refused by name from the first read, before the prefix, as it was after it."""
    async with obs.uow(archive_engine, origin) as conn:
        await activate(conn, origin["ws"])
        await ratio(conn, origin, origin["src"], 1)
    owner_write(
        sync_conn,
        "UPDATE workspaces SET source_mix_policy_version = 2 WHERE id = %s",
        origin["ws"],
    )
    async with obs.uow(archive_engine, origin) as conn:
        recorded = RecordedExecutor(conn)
        with pytest.raises(versioned_mix.MixProtocolRequired):
            await versioned_mix.resource(recorded, workspace_id=str(origin["ws"]))
    statements = [" ".join(str(s).split()) for s, _ in recorded.calls]
    assert len(statements) == 1 and locking(statements) == []

    # Control: the version this code knows is served.
    owner_write(
        sync_conn,
        "UPDATE workspaces SET source_mix_policy_version = 1 WHERE id = %s",
        origin["ws"],
    )
    assert (await read(archive_engine, origin))["protocol_version"] == 2


def hold_the_workspace(sync_conn, ws):
    """What a writer holds while it works: the workspace's two advisory locks
    and the row locks of the workspace, its accounts and its sources, on another
    connection that has not committed."""
    with sync_conn.cursor() as cur:
        for key in ("sources", "case_mix"):
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"{key}:{ws}",),
            )
        cur.execute("SELECT id FROM workspaces WHERE id = %s FOR UPDATE", (ws,))
        for table in ("ig_accounts", "media_sources"):
            cur.execute(
                f"SELECT id FROM {table} WHERE workspace_id = %s FOR UPDATE", (ws,)
            )


async def a_request_waits_on_a_lock(sync_conn, *, timeout=10.0):
    """Until some backend of this database waits on a lock, which is the
    evidence that a read is held by the writer and not merely slow."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with sync_conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM pg_stat_activity"
                " WHERE datname = current_database() AND wait_event_type = 'Lock'"
            )
            if cur.fetchone()[0]:
                return
        await asyncio.sleep(0.05)
    raise AssertionError("no request ever waited on the writer's lock")


async def test_a_writer_holding_the_workspace_delays_no_protocol_1_read_and_a_protocol_2_read_sees_its_commit(
    archive_engine, origin, sync_conn
):
    """Behaviour, not statements. The workspace's locks held as the worker holds
    them: a protocol-1 read answers at once. Once converted, a protocol-2 read
    waits for the same writer, which is what the prefix is for, and answers with
    what the writer committed, the saved state read again under the prefix and
    not the one it probed before waiting."""
    ws = str(origin["ws"])
    hold_the_workspace(sync_conn, ws)
    try:
        # A read that waited here would sit until the writer left.
        legacy = await asyncio.wait_for(read(archive_engine, origin), timeout=10)
    finally:
        sync_conn.rollback()
    assert legacy["protocol_version"] == 1

    async with obs.uow(archive_engine, origin) as conn:
        await activate(conn, origin["ws"])
        await ratio(conn, origin, origin["src"], 1)
    before = await read(archive_engine, origin)
    assert before["protocol_version"] == 2 and before["mode"] == "equal"

    hold_the_workspace(sync_conn, ws)
    waiting = asyncio.create_task(read(archive_engine, origin))
    try:
        await a_request_waits_on_a_lock(sync_conn)
        assert not waiting.done(), "a converted read takes the prefix, so it waits"
        with sync_conn.cursor() as cur:
            cur.execute(
                "UPDATE workspaces SET source_mix_mode = 'custom',"
                " source_mix_revision = source_mix_revision + 1 WHERE id = %s",
                (ws,),
            )
        sync_conn.commit()
    finally:
        sync_conn.rollback()
        after = await asyncio.wait_for(asyncio.gather(waiting), timeout=10)
    assert after[0]["protocol_version"] == 2
    assert (after[0]["revision"], after[0]["mode"]) == (
        before["revision"] + 1,
        "custom",
    )


#: The statements one protocol-2 GET issues for three destinations and three
#: sources, two of them archives: the conversion state read with no lock (1), the
#: resource's lock prefix (10), its saved state read again under it, rows and row
#: counts (3), the suitability context's own lock prefix and reads (13), the
#: destinations (1), each archive's `instagram_sync.status` (4 each), `pool` per
#: destination (2 each) and the conversion record (1).
THREE_BY_THREE_STATEMENTS = 1 + 10 + 3 + 13 + 1 + 2 * 4 + 3 * 2 + 1


async def test_a_get_for_three_destinations_and_three_sources_has_a_fixed_budget(
    archive_engine, origin, sync_conn
):
    mine, _ = await metadata(archive_engine, origin)
    b = add_account(sync_conn, origin, "acct-readiness-b", "example_b")
    add_account(sync_conn, origin, "acct-readiness-c", "example_c")
    theirs = await history(archive_engine, origin, b, enabled=True)
    await obs.observe(archive_engine, origin, theirs, obs.bundle(parent="456"))
    async with obs.uow(archive_engine, origin) as conn:
        await ratio(conn, origin, origin["src"], 1)
        await inventory(conn, origin, "budget-", 1)
    view, calls = await counted_read(archive_engine, origin)
    assert (len(view["destinations"]), len(view["rows"])) == (3, 3)
    assert view["guidance"]["ready"] == 3

    # More media changes no statement: every read is setwise, never per item.
    async with obs.uow(archive_engine, origin) as conn:
        await inventory(conn, origin, "more-", 25)
        await conn.execute(
            text(
                "WITH items AS (INSERT INTO media_items(workspace_id,source_id,"
                "source_data_generation_id,content_hash,provider_file_ref,file_name,"
                "media_kind) SELECT :ws,:sid,:gid,'budget-archive-'||n,"
                "'budget-archive-'||n,'x.jpg','image' FROM generate_series(1,25) n"
                " RETURNING id,provider_file_ref)"
                " INSERT INTO instagram_media_index(workspace_id,source_id,"
                "parent_media_id,data_generation_id,media_item_id,original_at,"
                "attribution) SELECT :ws,:sid,provider_file_ref,:gid,id,"
                "'2020-01-01','not_detected' FROM items"
            ),
            {
                "ws": str(origin["ws"]),
                "sid": mine["source_id"],
                "gid": mine["generation_id"],
            },
        )
    grown, more = await counted_read(archive_engine, origin)
    assert (
        next(r for r in grown["rows"] if r["source_id"] == mine["source_id"])["indexed"]
        == 26
    )
    assert len(calls) == len(more) == THREE_BY_THREE_STATEMENTS
    print(
        "READINESS_GET_STATEMENTS="
        + json.dumps({"destinations": 3, "sources": 3, "statements": len(calls)})
    )
