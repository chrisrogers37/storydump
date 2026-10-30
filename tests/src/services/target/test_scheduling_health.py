"""The worker-liveness half of the scheduling detector (#1120).

`scheduling_lag` watches the SCHEDULING CURSOR, whose population is the
clock's (an active account of an active, unpaused workspace) — empty until a
destination is active, so it correctly answers `no-signal` and correctly cannot
see anything. These tests cover the axis that is NOT empty: whether the worker
is still finishing the recurring jobs it runs regardless of how many tenants
exist, and that the cursor's population is the clock's own.

The load-bearing property is that a system which has never run must not be
reported with the same values as one that just succeeded.
"""

from __future__ import annotations

import pytest

from src.services.target import scheduling_health
from tests.src.services.target.schema_code import function_body
from tests.src.services.target.test_content_runway import (
    CLOCK_SCOPE,
    DUE_NOW,
    _plan_slot_conjuncts,
)

#: The `plan_slot` leg's conjunct the lag door keeps out of its WHERE: an
#: active account with no cursor is counted, as a provisioning fault, and
#: never stalled.
CURSOR_SET = "a.next_slot_at IS NOT NULL"


def test_the_lag_door_counts_the_accounts_the_clock_advances():
    """`fn_health_scheduling_lag`'s population is `fn_clock_tick`'s, read from
    the schema code rather than trusted to a comment: an account the tick
    would advance is never left out, and one it would not, a paused or
    inactive workspace's, never reads as stalled."""
    leg = _plan_slot_conjuncts()
    assert CURSOR_SET in leg and DUE_NOW in leg, "positive control: the leg was found"
    door = function_body("fn_health_scheduling_lag").rsplit("WHERE", 1)[1]
    assert " ".join(door.split()).split(" AND ") == [
        c for c in leg if c not in (CLOCK_SCOPE, CURSOR_SET, DUE_NOW)
    ]


class _Row:
    def __init__(self, mapping):
        self._mapping = mapping

    def mappings(self):
        return self

    def one(self):
        return self._mapping


class _Executor:
    """Answers one query with one row, and records what it was asked."""

    def __init__(self, mapping):
        self._mapping = mapping
        self.statements = []

    async def execute(self, statement, *args, **kwargs):
        self.statements.append(str(statement))
        return _Row(self._mapping)


@pytest.mark.asyncio
async def test_worker_freshness_reports_success_age_and_overdue_backlog():
    ex = _Executor(
        {
            "succeeded_ever": 78,
            "last_success_age_seconds": 4021.9,
            "overdue_ready": 0,
            "max_overdue_seconds": None,
            "worker_commit": "abc1234",
        }
    )

    out = await scheduling_health.worker_freshness(ex)

    assert out == {
        "succeeded_ever": 78,
        "last_success_age_seconds": 4021,
        "overdue_ready": 0,
        "max_overdue_seconds": None,
        "commit": "abc1234",
    }


@pytest.mark.asyncio
async def test_a_system_that_has_never_run_reports_none_rather_than_a_zero_age():
    """The reading a never-started estate produces must not be the reading a
    just-succeeded one produces.

    `0` is the most reassuring value this field has — "a job finished this
    second" — and it is what a naive `int(age)` returns for a database where
    nothing has ever succeeded. That is the defect class the whole instrument
    exists to close, so the instrument must not be an instance of it.
    """
    ex = _Executor(
        {
            "succeeded_ever": 0,
            "last_success_age_seconds": None,
            "overdue_ready": 0,
            "max_overdue_seconds": None,
            "worker_commit": "abc1234",
        }
    )

    out = await scheduling_health.worker_freshness(ex)

    assert out["last_success_age_seconds"] is None
    assert out["last_success_age_seconds"] != 0
    assert out["succeeded_ever"] == 0
