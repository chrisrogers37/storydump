"""The activation nudge's sweep (#1481), over a scripted session.

The SQL's truth (who the door lists, the latch, the grants) is the gate's,
`tests/scripts/test_activation_nudge_gate.py`. This pins what the sweep sends
for what it reads: the door's parameters, one latch UPDATE per person whose
predicate repeats the door's, and a `send_email` job only for a person the
UPDATE returned.
"""

from __future__ import annotations

import logging

import pytest

from src.services.target import activation_nudge, email_sender


class _Result:
    def __init__(self, rows=(), scalar=None):
        self._rows = list(rows)
        self._scalar = scalar

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._scalar


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
        web_app_origin="https://app.example/",
    )


async def test_it_reads_the_door_with_the_callers_numbers(enqueued):
    session = _Session(_Result(rows=[]))

    assert await _sweep(session) == 0

    ((door, params),) = session.statements
    assert "FROM fn_activation_stalled(" in door
    assert "make_interval(days => :days)" in door
    assert "make_interval(secs => :stall)" in door
    assert params == {"days": 30, "stall": 72 * 3600.0, "lim": 20}
    assert enqueued == []


async def test_each_person_is_latched_then_sent_one_email(enqueued):
    session = _Session(
        _Result(rows=[{"user_id": "u-3", "stage": 3}, {"user_id": "u-5", "stage": 5}]),
        _Result(scalar="three@example.com"),
        _Result(scalar="five@example.com"),
    )

    assert await _sweep(session) == 2

    latch = session.statements[1]
    assert "UPDATE users SET activation_nudge_at = now()" in latch[0]
    for clause in (
        "activation_nudge_at IS NULL",
        "state = 'active'",
        "primary_email IS NOT NULL",
        "RETURNING primary_email",
    ):
        assert clause in latch[0], clause
    assert latch[1] == {"u": "u-3"}
    assert session.statements[2][1] == {"u": "u-5"}

    assert enqueued == [
        {
            "kind": "send_email",
            "workspace_id": None,
            "serialization_key": "email:nudge:u-3",
            "payload": {
                "v": 1,
                "to": "three@example.com",
                "template": "activation_nudge",
                "params": {
                    "step": "instagram",
                    "link": "https://app.example/dashboard/settings?tab=accounts",
                },
            },
            "lane": "bulk",
        },
        {
            "kind": "send_email",
            "workspace_id": None,
            "serialization_key": "email:nudge:u-5",
            "payload": {
                "v": 1,
                "to": "five@example.com",
                "template": "activation_nudge",
                "params": {
                    "step": "approval",
                    "link": "https://app.example/dashboard/queue",
                },
            },
            "lane": "bulk",
        },
    ]


async def test_a_person_the_latch_did_not_return_is_not_mailed(enqueued):
    """Nudged by an overlapping sweep, disabled, or without an address since
    the door read: the UPDATE returns nothing, and nothing is sent."""
    session = _Session(
        _Result(rows=[{"user_id": "u-4", "stage": 4}]), _Result(scalar=None)
    )

    assert await _sweep(session) == 0
    assert len(session.statements) == 2
    assert enqueued == []


async def test_a_stage_with_no_step_is_named_and_skipped(enqueued, caplog):
    session = _Session(_Result(rows=[{"user_id": "u-2", "stage": 2}]))

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
