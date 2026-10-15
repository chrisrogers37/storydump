"""The calendar's reads over the ledger (#1634), over a scripted executor.

What PostgreSQL decides (the local day in the workspace's zone, the index, the
tenant) is the gate's, `tests/scripts/test_intent_slot_index_gate.py`. This
pins the statements: the range clauses, the states and their parameters. It
also pins what every read that draws an item's picture does to its row: an
archive item's provenance and name (#1644), the same on all of them.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable, NamedTuple

import pytest

from src.services.target import vocabulary, workspaces

WS = "8d1d3a3e-0000-4000-8000-000000000001"

FROM_CLAUSE = (
    "i.schedule_slot_at >= (CAST(:from_date AS date)::timestamp AT TIME ZONE"
    " (SELECT tz FROM workspaces WHERE id = :ws))"
)
TO_CLAUSE = (
    "i.schedule_slot_at < (CAST(:to_date AS date)::timestamp AT TIME ZONE"
    " (SELECT tz FROM workspaces WHERE id = :ws))"
)


class _Mappings(list):
    """`.mappings()`: iterated by `readers.rows`, asked `.first()` by `readers.row`."""

    def first(self):
        return self[0] if self else None


class _Rows:
    def __init__(self, *rows):
        self._rows = list(rows)

    def mappings(self):
        return _Mappings(self._rows)


class _Executor:
    def __init__(self, *rows):
        self.rows = list(rows)
        self.statements = []

    async def execute(self, statement, params=None):
        self.statements.append((str(statement), params))
        return _Rows(*self.rows)


class TestTheDayRange:
    async def test_a_range_bounds_the_slot_to_the_workspaces_local_days(self):
        ex = _Executor()
        await workspaces.list_intents(
            ex, workspace_id=WS, from_date=date(2026, 10, 3), to_date=date(2026, 10, 4)
        )
        ((sql, params),) = ex.statements
        assert FROM_CLAUSE in sql and TO_CLAUSE in sql
        assert params["from_date"] == date(2026, 10, 3)
        assert params["to_date"] == date(2026, 10, 4)
        assert params["ws"] == WS

    async def test_no_range_reads_without_a_slot_bound(self):
        ex = _Executor()
        await workspaces.list_intents(ex, workspace_id=WS)
        ((sql, params),) = ex.statements
        assert "schedule_slot_at >=" not in sql and "schedule_slot_at <" not in sql
        assert "from_date" not in params and "to_date" not in params


class TestTheMonthRead:
    def _row(self, day, total, name, slot):
        return {
            "day": day,
            "total": total,
            "id": f"id-{name}",
            "state": "posted",
            "schedule_slot_at": slot,
            "file_name": name,
            "category": "events",
            "media_item_id": f"media-{name}",
            "media_kind": "image",
            "has_thumbnail": True,
            "thumbnail_version": "0123456789abcdef",
            # What the provenance is nested from: a Drive source has no origin.
            "source_provider": "gdrive",
            "origin_handle": None,
            "original_at": None,
            "slide_post_ref": None,
            "slide_position": None,
            "slide_count": None,
            # A column the read never selects, so a link cannot ride along.
            "thumbnail_url": "https://drive.example/thumbnail",
        }

    async def _days(self, ex, states=("posted",)):
        return await workspaces.intent_days(
            ex,
            workspace_id=WS,
            states=list(states),
            from_date=date(2026, 9, 28),
            to_date=date(2026, 11, 2),
            per_day=3,
        )

    async def test_states_must_be_named(self):
        ex = _Executor()
        with pytest.raises(ValueError):
            await self._days(ex, ())
        assert ex.statements == [], "refused before any SQL ran"

    async def test_the_states_are_bound_as_the_intents_read_binds_them(self):
        ex = _Executor()
        await self._days(ex, ("posted", "skipped"))
        ((sql, params),) = ex.statements
        assert "i.state = ANY(CAST(:states AS text[]))" in sql
        assert FROM_CLAUSE in sql and TO_CLAUSE in sql
        assert "AT TIME ZONE w.tz" in sql, "the day is the workspace's local day"
        assert params == {
            "ws": WS,
            "per_day": 3,
            "states": ["posted", "skipped"],
            "from_date": date(2026, 9, 28),
            "to_date": date(2026, 11, 2),
        }

    async def test_rows_become_days_with_the_counts_the_read_counted(self):
        ex = _Executor(
            self._row("2026-10-03", 15, "c.jpg", "2026-10-03T20:00:00+00:00"),
            self._row("2026-10-03", 15, "b.jpg", "2026-10-03T19:00:00+00:00"),
            self._row("2026-10-03", 15, "a.jpg", "2026-10-03T18:00:00+00:00"),
            self._row("2026-10-04", 1, "d.jpg", "2026-10-04T09:00:00+00:00"),
        )
        days = await self._days(ex)
        assert [(d["date"], d["count"]) for d in days] == [
            ("2026-10-03", 15),
            ("2026-10-04", 1),
        ]
        assert [n["file_name"] for n in days[0]["newest"]] == [
            "c.jpg",
            "b.jpg",
            "a.jpg",
        ]
        assert days[0]["newest"][0] == {
            "id": "id-c.jpg",
            "state": "posted",
            "schedule_slot_at": "2026-10-03T20:00:00+00:00",
            "file_name": "c.jpg",
            "category": "events",
            "media_item_id": "media-c.jpg",
            "media_kind": "image",
            "has_thumbnail": True,
            "thumbnail_version": "0123456789abcdef",
            "provenance": None,
        }

    async def test_each_newest_carries_a_thumbnail_flag_never_its_link(self):
        """The calendar draws a thumbnail through the route (#1634),
        from the flag and version the intents read carries, never the link:
        a Drive item's from its stored link, an archive item's from its
        current observation (#1644), over the same joins."""
        ex = _Executor()
        await self._days(ex)
        ((sql, _),) = ex.statements
        assert "ELSE m.thumbnail_url IS NOT NULL END AS has_thumbnail" in sql
        assert "THEN mi.data_generation_id IS NOT NULL" in sql
        assert workspaces.thumbnail_joins("m.") in sql
        assert f"THEN {workspaces.ARCHIVE_THUMBNAIL_VERSION_SQL}" in sql
        assert "AS thumbnail_version" in sql
        assert "thumbnail_url" not in workspaces.INTENT_DAY_FIELDS


#: An archive item as it is stored (`instagram_catalog._project`): named
#: `vocabulary.ARCHIVE_ITEM_NAME`, with the Graph id for its provider reference.
#: The id is distinctive, so a leak is found by a plain substring. A row whose
#: stored name is `Instagram <Graph id>` (`LEGACY_NAME`) reads the same.
GRAPH_ID = "17841400000000771"
LEGACY_NAME = f"Instagram {GRAPH_ID}"
POSTED_AT = datetime(2020, 1, 1, 12, 30, tzinfo=timezone.utc)
ORIGIN_HANDLE = "example_handle"
PROVENANCE_COLUMNS = {
    "source_provider",
    "origin_handle",
    "original_at",
    "slide_post_ref",
    "slide_position",
    "slide_count",
}
POST_REF = "11111111-2222-4333-8444-555555555555"


def _stored(provider, **over):
    """What the database answers for one item of a source of *provider*, in the
    columns the archive projection reads."""
    archive = provider == "instagram_archive"
    return {
        "id": "id-1",
        "file_name": vocabulary.ARCHIVE_ITEM_NAME if archive else "f.jpg",
        "provider_file_ref": GRAPH_ID if archive else "drive-file-ref",
        "source_provider": provider,
        "origin_handle": ORIGIN_HANDLE if archive else None,
        "original_at": POSTED_AT if archive else None,
        "slide_post_ref": None,
        "slide_position": None,
        "slide_count": None,
        **over,
    }


class _Read(NamedTuple):
    """One read that draws an item's picture: how to run it over an executor
    and answer the one row it read, the columns it takes besides the
    projection's, and whether its row has a `provider_file_ref`."""

    run: Callable[[Any], Awaitable[dict]]
    columns: dict
    has_ref: bool


async def _list_intents(ex):
    return (await workspaces.list_intents(ex, workspace_id=WS))[0]


async def _get_intent(ex):
    return await workspaces.get_intent(ex, workspace_id=WS, intent_id="id-1")


async def _list_media(ex):
    return (await workspaces.list_media(ex, workspace_id=WS))[0]


async def _get_media(ex):
    return await workspaces.get_media(ex, workspace_id=WS, media_id="id-1")


async def _intent_days(ex):
    days = await workspaces.intent_days(
        ex,
        workspace_id=WS,
        states=["posted"],
        from_date=date(2026, 9, 28),
        to_date=date(2026, 11, 2),
        per_day=3,
    )
    return days[0]["newest"][0]


#: Every read of `workspaces` that draws an item's picture. `upcoming`'s planned
#: stories are the sixth, in `test_upcoming.py`.
PICTURE_READS = {
    "list_media": _Read(_list_media, {}, True),
    "get_media": _Read(_get_media, {}, True),
    "list_intents": _Read(_list_intents, {}, False),
    "get_intent": _Read(_get_intent, {}, False),
    "intent_days": _Read(
        _intent_days,
        {
            "day": "2026-10-03",
            "total": 1,
            "state": "posted",
            "schedule_slot_at": "2026-10-03T20:00:00+00:00",
            "category": None,
            "media_item_id": "media-1",
            "media_kind": "image",
            "has_thumbnail": True,
            "thumbnail_version": "v1",
        },
        False,
    ),
}


def _scripted(read, row):
    """*row* as the database answers *read* with it: the read's other columns
    beside it, and no `provider_file_ref` for a read that never selects one."""
    spec = PICTURE_READS[read]
    answer = {**spec.columns, **row}
    if not spec.has_ref:
        del answer["provider_file_ref"]
    return answer


async def _read(read, row):
    """The row *read* answers when the database gives it *row*."""
    return await PICTURE_READS[read].run(_Executor(_scripted(read, row)))


class TestAnArchiveItemOnEveryRead:
    """An archive item is named for where it came from and carries its
    provenance on each read the same way, so that the page can tell its picture
    from a Drive picture and never labels a post by Instagram's id. Each read
    is proven here over a scripted row; PostgreSQL's answer to all six is
    `tests/scripts/test_archive_preview_gate.py`."""

    def test_the_name_is_the_wire_spelling_the_web_says_too(self):
        assert vocabulary.ARCHIVE_ITEM_NAME == "Instagram post"

    @pytest.mark.parametrize("read", PICTURE_READS)
    async def test_it_reads_under_its_own_name_with_its_provenance(self, read):
        row = await _read(read, _stored("instagram_archive"))
        assert row["file_name"] == "Instagram post"
        assert row["provenance"] == {
            "provider": "instagram_archive",
            "origin_handle": ORIGIN_HANDLE,
            "original_at": POSTED_AT,
            "slide": None,
        }
        if PICTURE_READS[read].has_ref:
            assert row["provider_file_ref"] is None
        else:
            assert "provider_file_ref" not in row, "a read adds no key it never had"
        # The columns the provenance was nested from are not on the wire, and
        # nothing of the Graph id is anywhere in the row.
        assert not PROVENANCE_COLUMNS & set(row)
        assert GRAPH_ID not in repr(row)

    @pytest.mark.parametrize("read", PICTURE_READS)
    async def test_a_row_stored_as_instagram_graph_id_reads_under_the_neutral_name(
        self, read
    ):
        """A row can hold an archive item stored as `Instagram <Graph id>`. The
        projection hides that name, so a row stored that way never says the id
        to the page."""
        row = await _read(read, _stored("instagram_archive", file_name=LEGACY_NAME))
        assert row["file_name"] == vocabulary.ARCHIVE_ITEM_NAME
        assert GRAPH_ID not in repr(row)

    @pytest.mark.parametrize("read", PICTURE_READS)
    async def test_a_drive_item_keeps_its_own_name_and_no_provenance(self, read):
        """The positive control: the projection is the archive's alone."""
        row = await _read(read, _stored("gdrive"))
        assert row["file_name"] == "f.jpg"
        assert row["provenance"] is None, "every row carries the key"
        if PICTURE_READS[read].has_ref:
            assert row["provider_file_ref"] == "drive-file-ref"
        assert not PROVENANCE_COLUMNS & set(row)

    @pytest.mark.parametrize("read", PICTURE_READS)
    async def test_an_item_awaiting_observation_again_names_its_origin_alone(
        self, read
    ):
        """After a reauthorization the current generation has no observation of
        it: no time, but the account's handle, and still no Graph id."""
        row = await _read(read, _stored("instagram_archive", original_at=None))
        assert row["provenance"] == {
            "provider": "instagram_archive",
            "origin_handle": ORIGIN_HANDLE,
            "original_at": None,
            "slide": None,
        }
        assert row["file_name"] == "Instagram post"

    @pytest.mark.parametrize("read", PICTURE_READS)
    @pytest.mark.parametrize("count", [4, None])
    async def test_slide_name_and_provenance_use_only_public_position_and_count(
        self, read, count
    ):
        row = await _read(
            read,
            _stored(
                "instagram_archive",
                slide_post_ref=POST_REF,
                slide_position=2,
                slide_count=count,
            ),
        )
        assert row["file_name"] == (
            "Instagram post, slide 2 of 4" if count == 4 else "Instagram post, slide 2"
        )
        assert row["provenance"] == {
            "provider": "instagram_archive",
            "origin_handle": ORIGIN_HANDLE,
            "original_at": POSTED_AT,
            "slide": {"post_ref": POST_REF, "position": 2, "count": count},
        }
        assert not PROVENANCE_COLUMNS & set(row)
        assert GRAPH_ID not in repr(row)

    @pytest.mark.parametrize("read", PICTURE_READS)
    async def test_a_row_that_lacks_the_provenance_columns_is_refused(self, read):
        """A read that forgot to select them would otherwise read every archive
        item as a Drive item, and be drawn on the Drive path."""
        row = _stored("instagram_archive")
        del row["source_provider"]
        with pytest.raises(KeyError):
            await _read(read, row)

    @pytest.mark.parametrize("read", PICTURE_READS)
    async def test_every_read_selects_one_set_of_columns_over_one_set_of_joins(
        self, read
    ):
        ex = _Executor(_scripted(read, _stored("gdrive")))
        await PICTURE_READS[read].run(ex)
        ((sql, _),) = ex.statements
        assert workspaces._picture_sql("m.") in sql
        assert workspaces.picture_joins("m.") in sql
        assert "LEFT JOIN instagram_media_assets ma" in sql
        assert "mi.parent_media_id = ma.parent_media_id" in sql
        for column in PROVENANCE_COLUMNS:
            assert f"AS {column}" in sql
