"""The worker-liveness half of the scheduling detector (#1120).

`scheduling_lag` watches the SCHEDULING CURSOR, whose population is
`ig_accounts WHERE state = 'active'` — empty on production today, so it
correctly answers `no-signal` and correctly cannot see anything. These tests
cover the axis that is NOT empty: whether the worker is still finishing the
recurring jobs it runs regardless of how many tenants exist.

The load-bearing property is that a system which has never run must not be
reported with the same values as one that just succeeded.
"""

from __future__ import annotations

import pytest

from src.services.target import scheduling_health


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
        }
    )

    out = await scheduling_health.worker_freshness(ex)

    assert out == {
        "succeeded_ever": 78,
        "last_success_age_seconds": 4021,
        "overdue_ready": 0,
        "max_overdue_seconds": None,
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
        }
    )

    out = await scheduling_health.worker_freshness(ex)

    assert out["last_success_age_seconds"] is None
    assert out["last_success_age_seconds"] != 0
    assert out["succeeded_ever"] == 0
