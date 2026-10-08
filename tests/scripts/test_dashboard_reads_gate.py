"""The dashboard reads (#1044), measured as `svc_ingress` on the replayed schema.

Three reads the dashboard's held screens need and the merged API did not serve: the
media pool, a server-side stats aggregate, and a multi-state intents filter.
Each is asserted with exact numbers against seeded rows — a media item with
no intent (the case that made the pool invisible), two states in one intents
call, and a `posts_by_day` row read from the cap ledger rather than from a
list — and every read is tenant-bound: workspace B sees none of A's rows.
"""

from __future__ import annotations

import asyncio

import psycopg2
import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.services.target import category_mix, workspaces
from src.services.target.unit_of_work import asyncpg_url, unit_of_work
from tests.scripts.conftest import (
    _scratch,
    as_user,
    replay_advertised_stream,
    seed_intent_chain,
    seed_workspace_chain,
    set_test_passwords,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

#: The link the posted item carries, to show that both reads return it.
LINK = "https://example.com/menu"
#: The provider's thumbnail link the posted item carries, which no read returns.
THUMB = "https://lh3.googleusercontent.com/drive-storage/thumb-1=s220"


@pytest.fixture(scope="module")
def world(admin_conn, owner_actor):
    """Three workspaces. A carries: the chain's scheduled intent; one posted
    intent (category 'food'); one skipped intent (category 'travel'); one
    media item with NO intent (category NULL, never posted); one cap-ledger
    row for today. B carries only its chain. C carries the stats window's
    edges, each in its own folder: a post debited on the window's first day
    and one debited the day before it, beside the chain's folder, which has
    never posted, and a story a person planned, posted today, which spends
    no cap. Two cadence stories debited inside the window are not settled
    posts either: a dry run, and a failure whose refund keeps its day."""
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        stream = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        conn = psycopg2.connect(stream)
        try:
            a = seed_workspace_chain(conn, "reads-a")
            b = seed_workspace_chain(conn, "reads-b")
            c = seed_workspace_chain(conn, "reads-c")
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute("SET app.actor_kind = 'migration'")
                posted = seed_intent_chain(
                    cur, a["ws"], "reads-a-posted", state="awaiting_approval"
                )
                skipped = seed_intent_chain(
                    cur, a["ws"], "reads-a-skipped", state="awaiting_approval"
                )
                cur.execute(
                    "UPDATE media_items SET category = 'food', times_posted = 1, link_url = %s,"
                    " thumbnail_url = %s WHERE id = %s RETURNING content_hash, source_id",
                    (LINK, THUMB, posted["media"]),
                )
                a["posted_hash"], a["posted_source"] = cur.fetchone()
                cur.execute(
                    "UPDATE media_items SET category = 'travel' WHERE id = %s",
                    (skipped["media"],),
                )
                # legal edges only: awaiting_approval → posted needs the manual proof
                cur.execute(
                    "UPDATE post_intents SET state = 'posted', published_via = 'manual',"
                    " cap_consumed_on = current_date WHERE id = %s",
                    (posted["intent"],),
                )
                cur.execute(
                    "UPDATE post_intents SET state = 'skipped' WHERE id = %s",
                    (skipped["intent"],),
                )
                cur.execute(
                    "INSERT INTO media_items (workspace_id, source_id, content_hash, file_name,"
                    " media_kind, provider_file_ref)"
                    " VALUES (%s, %s, 'hash-orphan', 'orphan.jpg', 'image', 'ref-orphan')"
                    " RETURNING id",
                    (a["ws"], a["src"]),
                )
                a["orphan_media"] = str(cur.fetchone()[0])
                cur.execute(
                    "INSERT INTO daily_post_counts"
                    " (workspace_id, ig_account_id, local_date, count, cap_at_write)"
                    " VALUES (%s, %s, current_date, 2, 3)",
                    (a["ws"], posted["iga"]),
                )
                for edge, days_back in (
                    ("in", workspaces.STATS_DAYS),
                    ("out", workspaces.STATS_DAYS + 1),
                ):
                    chain = seed_intent_chain(
                        cur, c["ws"], f"reads-c-edge-{edge}", state="awaiting_approval"
                    )
                    cur.execute(
                        "UPDATE post_intents SET state = 'posted', published_via = 'manual',"
                        " cap_consumed_on = current_date - %s WHERE id = %s",
                        (days_back, chain["intent"]),
                    )
                    c[f"edge_{edge}"] = chain
                # Born planned (088 fixes origin at birth); posted today, so its
                # day is stamped in the window, but it never spent the cap.
                planned_chain = seed_intent_chain(
                    cur,
                    c["ws"],
                    "reads-c-planned",
                    state="awaiting_approval",
                    origin="planned",
                )
                cur.execute(
                    "UPDATE post_intents SET state = 'posted', published_via = 'manual',"
                    " cap_consumed_on = current_date WHERE id = %s",
                    (planned_chain["intent"],),
                )
                c["planned"] = planned_chain
                # Each walks the legal edges to `publishing`, which needs its
                # debit day (ck_publishing_debited), then ends unposted for the
                # count: a dry-run post, and a failure (its refund keeps the day).
                dry_run = seed_intent_chain(
                    cur, c["ws"], "reads-c-dry-run", state="awaiting_approval"
                )
                failed = seed_intent_chain(
                    cur, c["ws"], "reads-c-failed", state="awaiting_approval"
                )
                for chain in (dry_run, failed):
                    cur.execute(
                        "UPDATE post_intents SET state = 'approved' WHERE id = %s",
                        (chain["intent"],),
                    )
                    cur.execute(
                        "UPDATE post_intents SET state = 'publishing',"
                        " cap_consumed_on = current_date - 1 WHERE id = %s",
                        (chain["intent"],),
                    )
                cur.execute(
                    "UPDATE post_intents SET state = 'posted', published_via = 'dry_run',"
                    " publish_step = 'effect_confirmed' WHERE id = %s",
                    (dry_run["intent"],),
                )
                cur.execute(
                    "UPDATE post_intents SET state = 'failed', cap_refunded_at = now()"
                    " WHERE id = %s",
                    (failed["intent"],),
                )
                c["dry_run"], c["failed"] = dry_run, failed
            conn.commit()
            a["posted"] = posted
            a["skipped"] = skipped
        finally:
            conn.close()
        yield {
            "stream": stream,
            "ingress": as_user(db, "svc_ingress"),
            "a": a,
            "b": b,
            "c": c,
        }
    finally:
        gen.close()


def _read(world, ids, fn, **kwargs):
    async def main():
        engine = create_async_engine(asyncpg_url(world["ingress"]), poolclass=NullPool)
        try:
            uow = unit_of_work(
                engine,
                str(ids["ws"]),
                actor_kind="user",
                actor_user_id=str(ids["user"]),
                channel="web",
            )
            async with uow.begin() as session:
                return await fn(session, workspace_id=str(ids["ws"]), **kwargs)
        finally:
            await engine.dispose()

    return asyncio.run(main())


class TestTheMediaPool:
    def test_lists_the_whole_library_including_items_with_no_intent(self, world):
        a = world["a"]
        rows = _read(world, a, workspaces.list_media)
        ids = {r["id"] if isinstance(r["id"], str) else str(r["id"]) for r in rows}
        assert a["orphan_media"] in ids, (
            "an item with no intent is invisible — the pool was the gap"
        )
        assert len(rows) == 4  # chain + posted + skipped + orphan

    def test_never_posted_and_state_narrow_it(self, world):
        a = world["a"]
        never = _read(world, a, workspaces.list_media, never_posted=True)
        assert all(r["times_posted"] == 0 for r in never) and len(never) == 3
        assert _read(world, a, workspaces.list_media, state="removed") == []

    def test_get_is_workspace_bound(self, world):
        a, b = world["a"], world["b"]
        assert (
            _read(world, a, workspaces.get_media, media_id=a["orphan_media"])[
                "file_name"
            ]
            == "orphan.jpg"
        )
        assert _read(world, b, workspaces.get_media, media_id=a["orphan_media"]) is None
        assert _read(world, b, workspaces.list_media) and all(
            str(r["id"]) != a["orphan_media"]
            for r in _read(world, b, workspaces.list_media)
        )


class TestMultiStateIntents:
    def test_two_states_in_one_call(self, world):
        a = world["a"]
        rows = _read(world, a, workspaces.list_intents, states=["posted", "skipped"])
        assert sorted(r["state"] for r in rows) == ["posted", "skipped"]
        assert {str(r["id"]) for r in rows} == {
            str(a["posted"]["intent"]),
            str(a["skipped"]["intent"]),
        }
        rows = _read(world, a, workspaces.list_intents)
        assert len(rows) == 3
        # the queue's account column (#1033): present on every row, NULL when
        # the seeded account carries no handle — never a missing key
        assert all("account_handle" in r and "account_display_name" in r for r in rows)


class TestTheItemsLink:
    """An item's link to add by hand reaches the library and the queue, whose
    rows join their item. Every row carries the key, NULL when the item has no
    link, so the web never meets a missing key."""

    def test_the_library_and_the_queue_both_read_it(self, world):
        a = world["a"]
        media = {str(r["id"]): r for r in _read(world, a, workspaces.list_media)}
        assert all("link_url" in r for r in media.values())
        assert media[str(a["posted"]["media"])]["link_url"] == LINK
        assert media[a["orphan_media"]]["link_url"] is None
        one = _read(world, a, workspaces.get_media, media_id=str(a["posted"]["media"]))
        assert one["link_url"] == LINK
        intents = {str(r["id"]): r for r in _read(world, a, workspaces.list_intents)}
        assert all("link_url" in r for r in intents.values())
        assert intents[str(a["posted"]["intent"])]["link_url"] == LINK
        assert intents[str(a["skipped"]["intent"])]["link_url"] is None


class TestTheThumbnailFlag:
    """#1634: the library and the queue say whether an item has a thumbnail,
    and the version its URL carries, but never the provider's link. Only the
    thumbnail route's read selects the link, and only inside its workspace."""

    def test_the_reads_carry_the_flag_and_the_version_never_the_link(self, world):
        a = world["a"]
        version = a["posted_hash"][: workspaces.THUMBNAIL_VERSION_CHARS]
        media = {str(r["id"]): r for r in _read(world, a, workspaces.list_media)}
        assert all("thumbnail_url" not in r for r in media.values())
        posted = media[str(a["posted"]["media"])]
        assert (posted["has_thumbnail"], posted["thumbnail_version"]) == (True, version)
        assert media[a["orphan_media"]]["has_thumbnail"] is False
        one = _read(world, a, workspaces.get_media, media_id=str(a["posted"]["media"]))
        assert "thumbnail_url" not in one
        assert (one["has_thumbnail"], one["thumbnail_version"]) == (True, version)
        intents = {str(r["id"]): r for r in _read(world, a, workspaces.list_intents)}
        assert all("thumbnail_url" not in r for r in intents.values())
        row = intents[str(a["posted"]["intent"])]
        assert (row["has_thumbnail"], row["thumbnail_version"]) == (True, version)
        assert intents[str(a["skipped"]["intent"])]["has_thumbnail"] is False

    def test_the_link_is_read_only_inside_its_own_workspace(self, world):
        a, b = world["a"], world["b"]
        media = str(a["posted"]["media"])
        row = _read(world, a, workspaces.thumbnail_link, media_id=media)
        assert row["thumbnail_url"] == THUMB
        # The chain seeds its own source, so the item's is the one to match.
        assert str(row["source_id"]) == str(a["posted_source"])
        # A member of another workspace, naming this item's id, reads nothing.
        assert _read(world, b, workspaces.thumbnail_link, media_id=media) is None


class TestStats:
    def test_counts_are_exact_and_from_the_tables_not_a_list(self, world):
        a = world["a"]
        s = _read(world, a, workspaces.stats)
        assert s["intents_by_state"] == {"scheduled": 1, "posted": 1, "skipped": 1}
        assert s["media_by_state"] == {"available": 4}
        assert s["media_never_posted"] == 3
        assert s["media_by_category"] == {"": 2, "food": 1, "travel": 1}
        assert s["posted_by_source"] == {str(a["posted"]["src"]): 1}
        assert s["accounts"] == 3 and s["sources"] == 3
        (day,) = s["posts_by_day"]
        assert (day["count"], day["cap"]) == (2, 3)

    def test_the_other_tenant_counts_only_its_own(self, world):
        s = _read(world, world["b"], workspaces.stats)
        assert s["intents_by_state"] == {"scheduled": 1}
        assert s["media_by_state"] == {"available": 1}
        assert s["posts_by_day"] == [] and s["posted_by_source"] == {}

    def test_posted_by_source_counts_the_window_and_stops_at_its_edge(self, world):
        # Both edge rows are dated against `current_date` when the world is
        # seeded, so a run that crosses the database's midnight before this
        # read moves the edge by a day.
        c = world["c"]
        s = _read(world, c, workspaces.stats)
        assert s["intents_by_state"] == {"scheduled": 1, "posted": 4, "failed": 1}
        # The window's first day counts; the day before it does not, so that
        # folder has no post in the window and no key, like the chain's own.
        # The planned story is in the window but spends no cap, so it is not
        # counted: the mix draws the cadence, and `posts_by_day` skips it too.
        # The dry run and the failure spent the cap inside the window, and
        # neither is a settled post.
        assert s["posted_by_source"] == {str(c["edge_in"]["src"]): 1}

    def test_posted_by_source_keys_on_the_mix_routes_folder_ids(self, world):
        c = world["c"]
        folders = {r["source_id"] for r in _read(world, c, category_mix.mix_view)}
        posted = set(_read(world, c, workspaces.stats)["posted_by_source"])
        assert posted == {str(c["edge_in"]["src"])} and posted <= folders
        # A folder with no post in the window is still one of the mix's rows.
        assert {
            str(c["src"]),
            str(c["edge_out"]["src"]),
            str(c["planned"]["src"]),
        } <= folders - posted
