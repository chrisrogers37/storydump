"""The backpressure signal (phase 3a of the 2026-09-09 tap plan, step 6;
`01-target-architecture.md:88`; #716): what the worker's status line and
`/health/scheduling` say about the queue, from one read.

- per lane: ready depth and the age of the oldest runnable job;
- the outbox's pending rows;
- the `tg_global` pacing row: how many 1 s windows in the last minute were
  spent to the limit, and whether a hold is active NOW (a 429's durable
  hold, step 2);
- the workspace whose oldest ready job has waited longest — with per-lane
  depth, the two numbers fairness (F9, phase 4) is gated on.

Read-only, one connection, four statements; never a verdict.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import text

from src.services.target.rate_counters import window_start

LANES = ("interactive", "bulk")


async def snapshot(
    executor, *, now: datetime, global_limit: int, global_window_seconds: int
) -> dict[str, Any]:
    lanes: dict[str, dict[str, Any]] = {
        lane: {"ready": 0, "oldest_age_s": 0.0} for lane in LANES
    }
    rows = (
        await executor.execute(
            text(
                "SELECT lane, count(*) AS ready,"
                "       EXTRACT(EPOCH FROM max(now() - run_at)) AS oldest_age"
                "  FROM jobs WHERE state = 'ready' AND run_at <= now()"
                " GROUP BY lane"
            )
        )
    ).mappings()
    for r in rows:
        lanes[str(r["lane"])] = {
            "ready": int(r["ready"]),
            "oldest_age_s": round(float(r["oldest_age"] or 0.0), 1),
        }
    pending = (
        await executor.execute(
            text("SELECT count(*) FROM channel_outbox WHERE state = 'pending'")
        )
    ).scalar()
    current = window_start(now, global_window_seconds)
    paced = (
        (
            await executor.execute(
                text(
                    "SELECT count(*) FILTER (WHERE window_start >= :since) AS spent,"
                    "       bool_or(window_start = :current) AS held"
                    "  FROM rate_counters"
                    " WHERE scope = 'tg_global' AND key = ''"
                    "   AND window_start >= :since AND count >= :limit"
                ),
                {
                    "since": window_start(now, global_window_seconds).replace(
                        second=0, microsecond=0
                    ),
                    "current": current,
                    "limit": global_limit,
                },
            )
        )
        .mappings()
        .first()
    )
    oldest = (
        (
            await executor.execute(
                text(
                    "SELECT workspace_id, EXTRACT(EPOCH FROM now() - min(run_at)) AS wait"
                    "  FROM jobs"
                    " WHERE state = 'ready' AND run_at <= now() AND workspace_id IS NOT NULL"
                    " GROUP BY workspace_id ORDER BY wait DESC LIMIT 1"
                )
            )
        )
        .mappings()
        .first()
    )
    return {
        "lanes": lanes,
        "outbox_pending": int(pending or 0),
        "tg_global": {
            "paced_windows_last_minute": int((paced or {}).get("spent") or 0),
            "hold_active": bool((paced or {}).get("held") or False),
        },
        "ws_oldest_wait": (
            None
            if oldest is None
            else {
                "workspace_id": str(oldest["workspace_id"]),
                "wait_s": round(float(oldest["wait"] or 0.0), 1),
            }
        ),
    }


def render(snap: dict[str, Any]) -> str:
    """The status line's spelling of a snapshot."""
    lanes = " ".join(
        f"{lane}[ready={v['ready']} oldest_age={v['oldest_age_s']}s]"
        for lane, v in snap["lanes"].items()
    )
    tg = snap["tg_global"]
    oldest = snap.get("ws_oldest_wait")
    ws = (
        "ws_oldest_wait=none"
        if oldest is None
        else f"ws_oldest_wait={oldest['workspace_id'][:8]} {oldest['wait_s']}s"
    )
    return (
        f"queue {lanes} outbox_pending={snap['outbox_pending']}"
        f" tg_global_paced={tg['paced_windows_last_minute']}"
        f" hold={'y' if tg['hold_active'] else 'n'} {ws}"
    )
