"""The activation nudge's sweep (#1481), over a scripted session.

The SQL's truth (who the door lists, the latch, the grants) is the gate's,
`tests/scripts/test_activation_nudge_gate.py`. This pins what the sweep sends
for what it reads: the door's parameters, ONE latch UPDATE whose predicate
repeats the door's, and a `send_email` job only for a person it returned.
"""

from __future__ import annotations

import logging

import pytest

from src.services.target import activation_nudge, email_sender

ORIGIN = "https://app.example"

LATCH_CLAUSES = (
    "UPDATE users SET activation_nudge_at = now()",
    "id = ANY(CAST(:ids AS uuid[]))",
    "activation_nudge_at IS NULL",
    "state = 'active'",
    "primary_email IS NOT NULL",
    "RETURNING id, primary_email",
)


class _Rows:
    """One statement's answer, read the way `readers.rows` reads it."""

    def __init__(self, *rows):
        self._rows = list(rows)

    def mappings(self):
        return iter(self._rows)


class _Session:
    """Answers each `execute` from a queue, in order, and records it."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.statements = []

    async def execute(self, statement, params=None):
        self.statements.append((str(statement), params))
        return self.answers.pop(0)


@pytest.fixture
def enqueued(monkeypatch):
    calls = []

    async def fake_enqueue(session, **kwargs):
        calls.append(kwargs)
        return "job-id"

    monkeypatch.setattr(activation_nudge.jobs, "enqueue", fake_enqueue)
    return calls


async def _sweep(session):
    return await activation_nudge.sweep_stalled(
        session,
        since_days=30,
        stall_seconds=72 * 3600,
        limit=20,
        web_app_origin=ORIGIN,
    )


def _email(user, step, path, to):
    return {
        "kind": "send_email",
        "workspace_id": None,
        "serialization_key": f"email:nudge:{user}",
        "payload": {
            "v": 1,
            "to": to,
            "template": "activation_nudge",
            "params": {"step": step, "link": ORIGIN + path},
        },
        "lane": "bulk",
    }


async def test_it_reads_the_door_with_the_callers_numbers(enqueued):
    session = _Session(_Rows())

    assert await _sweep(session) == 0

    ((door, params),) = session.statements
    assert "FROM fn_activation_stalled(" in door
    assert "make_interval(days => :days)" in door
    assert "make_interval(secs => :stall)" in door
    assert params == {"days": 30, "stall": 72 * 3600.0, "lim": 20}
    assert enqueued == []


async def test_one_latch_for_everyone_then_one_email_each_in_the_doors_order(
    enqueued,
):
    session = _Session(
        _Rows({"user_id": "u-5", "stage": 5}, {"user_id": "u-3", "stage": 3}),
        # RETURNING has no order: the emails follow the door's, not this one.
        _Rows(
            {"id": "u-3", "primary_email": "three@example.com"},
            {"id": "u-5", "primary_email": "five@example.com"},
        ),
    )

    assert await _sweep(session) == 2

    latch, params = session.statements[1]
    for clause in LATCH_CLAUSES:
        assert clause in latch, clause
    assert params == {"ids": ["u-5", "u-3"]}
    assert enqueued == [
        _email("u-5", "approval", "/dashboard/queue", "five@example.com"),
        _email(
            "u-3", "instagram", "/dashboard/settings?tab=accounts", "three@example.com"
        ),
    ]


async def test_a_person_the_latch_did_not_return_is_not_mailed(enqueued):
    """Nudged by an overlapping sweep, disabled, or without an address since
    the door read: the UPDATE does not return them, and nothing is sent."""
    session = _Session(
        _Rows({"user_id": "u-4", "stage": 4}, {"user_id": "u-3", "stage": 3}),
        _Rows({"id": "u-3", "primary_email": "three@example.com"}),
    )

    assert await _sweep(session) == 1
    assert enqueued == [
        _email(
            "u-3", "instagram", "/dashboard/settings?tab=accounts", "three@example.com"
        )
    ]


async def test_a_stage_with_no_step_is_named_and_never_latched(enqueued, caplog):
    session = _Session(_Rows({"user_id": "u-2", "stage": 2}))

    with caplog.at_level(logging.WARNING):
        assert await _sweep(session) == 0

    assert len(session.statements) == 1, "no latch for a stage it cannot mail"
    assert enqueued == []
    assert "no step for stage 2" in caplog.text


def test_every_step_the_sweep_can_send_has_copy():
    """`render` refuses a step with no copy, so a step added to `STEPS` alone
    would be latched and then refused at send time: one person lost."""
    steps = {key for key, _path in activation_nudge.STEPS.values()}
    assert steps == set(email_sender._NUDGE_COPY)
