"""The read views' Python-side logic against a scripted executor (the SQL
itself is proven by the gate): `posture`'s three ledger answers, `story`'s
empty answer, `burst`'s ordering and its outcome rows, and the bounds."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from src.services.target import ops_views

WS = str(uuid.uuid4())
NOW = dt.datetime(2026, 9, 15, 12, 0, tzinfo=dt.timezone.utc)


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class _Executor:
    """Answers each statement from a queue; records the SQL and its params."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), dict(params or {})))
        return _Rows(self.answers.pop(0) if self.answers else [])


class TestPosture:
    @pytest.mark.asyncio
    async def test_an_absent_ledger_is_reported_and_never_read(self):
        ex = _Executor(
            [{"user": "svc_ingress", "bypassrls": False}],
            [],  # no ledger table in the catalogs
            [],
            [],
        )
        data = await ops_views.posture(ex)
        assert data["ledger"] == "absent" and data["migrations"] == []
        assert not any("schema_migrations ORDER BY" in sql for sql, _ in ex.calls)
        assert "pg_class" in ex.calls[1][0] and "to_regclass" not in ex.calls[1][0], (
            "presence is read from the catalogs by oid — a name lookup in a"
            " schema without USAGE is itself a permission error"
        )

    @pytest.mark.asyncio
    async def test_a_present_ledger_without_the_grant_is_unreadable_not_guessed(self):
        ex = _Executor(
            [{"user": "svc_ingress", "bypassrls": False}],
            [{"ok": False}],
            [],
            [],
        )
        data = await ops_views.posture(ex)
        assert data["ledger"] == "unreadable" and data["migrations"] == []
        assert not any("schema_migrations ORDER BY" in sql for sql, _ in ex.calls)

    @pytest.mark.asyncio
    async def test_a_readable_ledger_lists_its_migrations(self):
        ex = _Executor(
            [{"user": "neondb_owner", "bypassrls": True}],
            [{"ok": True}],
            [
                {
                    "version": 77,
                    "checksum": "abc",
                    "applied_at": NOW,
                    "status": "applied",
                }
            ],
            [{"table": "post_intents", "enabled": True, "forced": False}],
            [{"name": "fn_reaper_stale_approved", "owner": "svc_maintenance"}],
        )
        data = await ops_views.posture(ex)
        assert data["ledger"] == "present"
        assert [m["version"] for m in data["migrations"]] == [77]
        assert data["role"] == {"user": "neondb_owner", "bypassrls": True}
        assert data["rls"][0]["table"] == "post_intents"
        assert data["doors"][0]["name"] == "fn_reaper_stale_approved"


class TestStory:
    @pytest.mark.asyncio
    async def test_an_unknown_story_answers_no_rows_and_reads_nothing_more(self):
        ex = _Executor([])
        assert await ops_views.story(ex, workspace_id=WS, intent_id=WS) == []
        assert len(ex.calls) == 1
        assert ex.calls[0][1] == {"ws": WS, "id": WS}

    @pytest.mark.asyncio
    async def test_a_story_is_one_row_with_its_three_lists(self):
        ex = _Executor(
            [{"id": WS, "state": "approved"}],
            [{"at": NOW, "detail": {"event": "float_wait"}}],
            [{"generation": 1}],
            [{"external_message_ref": "7587"}],
        )
        (row,) = await ops_views.story(ex, workspace_id=WS, intent_id=WS)
        assert row["workspace_id"] == WS and row["intent"]["state"] == "approved"
        assert row["audit"][0]["detail"]["event"] == "float_wait"
        assert row["operations"] == [{"generation": 1}]
        assert row["cards"] == [{"external_message_ref": "7587"}]
        assert all(p == {"ws": WS, "id": WS} for _, p in ex.calls)


class TestBurst:
    @pytest.mark.asyncio
    async def test_sections_are_merged_in_time_order_with_outcomes_last(self):
        early, late = NOW, NOW + dt.timedelta(seconds=5)
        other = str(uuid.uuid4())
        ex = _Executor(
            [
                {
                    "workspace_id": WS,
                    "at": late,
                    "intent_id": "i1",
                    "to_state": "skipped",
                }
            ],
            [{"workspace_id": other, "at": early, "intent_id": "i1", "generation": 1}],
            [],  # waits
            [],  # siblings
            [],  # reviews
            [{"workspace_id": WS, "state": "skipped", "count": 1}],  # outcomes
        )
        rows = await ops_views.burst(ex, workspace_id=WS, since=NOW)
        assert [r["section"] for r in rows] == ["permit", "tap", "outcome"]
        assert rows[-1] == {
            "section": "outcome",
            "at": None,
            "intent_id": None,
            "workspace_id": WS,
            "state": "skipped",
            "count": 1,
        }
        # the row's workspace is the TABLE's, never a stamp from the parameter:
        # a crossed row would betray itself to the gate's bypass arm
        assert rows[0]["workspace_id"] == other
        assert all(p == {"ws": WS, "since": NOW} for _, p in ex.calls)


class TestBounds:
    @pytest.mark.asyncio
    async def test_floating_clamps_its_limit_to_the_ceiling(self):
        ex = _Executor([])
        await ops_views.floating(ex, workspace_id=WS, limit=10_000)
        assert ex.calls[0][1] == {"ws": WS, "lim": ops_views.FLOATING_LIMIT_MAX}
        await ops_views.floating(ex, workspace_id=WS, limit=0)
        assert ex.calls[1][1]["lim"] == 1

    def test_every_list_statement_is_bounded(self):
        for name in ("_AUDIT", "_OPERATIONS", "_STORY_CARDS", "_CARDS", "_FLOATING"):
            assert "LIMIT" in getattr(ops_views, name), name
        for name in ("_JOBS", "_OUTBOX", "_TAPS", "_PERMITS", "_WAITS", "_SIBLINGS"):
            assert ":since" in getattr(ops_views, name), name

    def test_every_tenant_table_carries_the_workspace_predicate(self):
        """The braces beside the belt: the statement text itself names the
        tenant on every tenant-plane table it reads."""
        for name in (
            "_INTENT",
            "_AUDIT",
            "_OPERATIONS",
            "_STORY_CARDS",
            "_CARDS",
            "_FLOATING",
            "_ACCOUNT",
            "_JOBS",
            "_OUTBOX",
            "_TAPS",
            "_PERMITS",
            "_WAITS",
            "_SIBLINGS",
            "_REVIEWS",
            "_OUTCOMES",
        ):
            assert "workspace_id = :ws" in getattr(ops_views, name), name
