"""The backpressure signal (phase 3a step 6): one read, rendered for the
status line and served on `/health/scheduling`."""

from __future__ import annotations

from datetime import datetime, timezone

from src.services.target import backpressure

NOW = datetime(2030, 1, 1, 12, 0, 30, tzinfo=timezone.utc)


class _Result:
    def __init__(self, rows=None, scalar=None):
        self._rows = rows or []
        self._scalar = scalar

    def mappings(self):
        rows = self._rows

        class _M:
            def __iter__(self_inner):
                return iter(rows)

            def first(self_inner):
                return rows[0] if rows else None

        return _M()

    def scalar(self):
        return self._scalar


class _Executor:
    """Answers the four statements by what they select from."""

    def __init__(self, *, lanes, pending, paced, oldest):
        self.answers = {
            "lanes": lanes,
            "pending": pending,
            "paced": paced,
            "oldest": oldest,
        }
        self.statements = []

    async def execute(self, stmt, params=None):
        sql = " ".join(str(stmt).split())
        self.statements.append((sql, params))
        if "GROUP BY lane" in sql:
            return _Result(rows=self.answers["lanes"])
        if "FROM channel_outbox" in sql:
            return _Result(scalar=self.answers["pending"])
        if "FROM rate_counters" in sql:
            return _Result(
                rows=[self.answers["paced"]] if self.answers["paced"] else []
            )
        if "GROUP BY workspace_id" in sql:
            return _Result(
                rows=[self.answers["oldest"]] if self.answers["oldest"] else []
            )
        raise AssertionError(sql)


class TestSnapshot:
    async def test_it_reads_every_axis_in_four_statements(self):
        ex = _Executor(
            lanes=[
                {"lane": "interactive", "ready": 3, "oldest_age": 4.26},
                {"lane": "bulk", "ready": 12, "oldest_age": 130.0},
            ],
            pending=5,
            paced={"spent": 4, "held": True},
            oldest={"workspace_id": "ws-aaaaaaaa-1", "wait": 61.44},
        )
        snap = await backpressure.snapshot(
            ex, now=NOW, global_limit=30, global_window_seconds=1
        )
        assert len(ex.statements) == 4
        assert snap["lanes"]["interactive"] == {"ready": 3, "oldest_age_s": 4.3}
        assert snap["lanes"]["bulk"] == {"ready": 12, "oldest_age_s": 130.0}
        assert snap["outbox_pending"] == 5
        assert snap["tg_global"] == {
            "paced_windows_last_minute": 4,
            "hold_active": True,
        }
        assert snap["ws_oldest_wait"] == {
            "workspace_id": "ws-aaaaaaaa-1",
            "wait_s": 61.4,
        }
        paced_params = next(p for s, p in ex.statements if "rate_counters" in s)
        assert paced_params["limit"] == 30
        assert paced_params["current"] == NOW.replace(microsecond=0)
        assert paced_params["since"] == NOW.replace(second=0, microsecond=0)

    async def test_an_empty_queue_renders_zeros_not_gaps(self):
        ex = _Executor(lanes=[], pending=0, paced=None, oldest=None)
        snap = await backpressure.snapshot(
            ex, now=NOW, global_limit=30, global_window_seconds=1
        )
        assert snap["lanes"] == {
            "interactive": {"ready": 0, "oldest_age_s": 0.0},
            "bulk": {"ready": 0, "oldest_age_s": 0.0},
        }
        assert snap["outbox_pending"] == 0
        assert snap["tg_global"] == {
            "paced_windows_last_minute": 0,
            "hold_active": False,
        }
        assert snap["ws_oldest_wait"] is None


class TestRender:
    def test_the_line_carries_every_number_a_human_reads_first(self):
        line = backpressure.render(
            {
                "lanes": {
                    "interactive": {"ready": 3, "oldest_age_s": 4.3},
                    "bulk": {"ready": 12, "oldest_age_s": 130.0},
                },
                "outbox_pending": 5,
                "tg_global": {"paced_windows_last_minute": 4, "hold_active": True},
                "ws_oldest_wait": {"workspace_id": "0f3a9c2e-rest", "wait_s": 61.4},
            }
        )
        for token in (
            "interactive[ready=3 oldest_age=4.3s]",
            "bulk[ready=12 oldest_age=130.0s]",
            "outbox_pending=5",
            "tg_global_paced=4",
            "hold=y",
            "ws_oldest_wait=0f3a9c2e 61.4s",
        ):
            assert token in line, line

    def test_no_waiting_workspace_says_so(self):
        line = backpressure.render(
            {
                "lanes": {"interactive": {"ready": 0, "oldest_age_s": 0.0}},
                "outbox_pending": 0,
                "tg_global": {"paced_windows_last_minute": 0, "hold_active": False},
                "ws_oldest_wait": None,
            }
        )
        assert "hold=n" in line and "ws_oldest_wait=none" in line
