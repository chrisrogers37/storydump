"""`scheduler.ClockElection` without a database: the statement it sends.

The election's truth is PostgreSQL's (re-entrancy, sessions), and
`tests/scripts/test_scheduler_clock_gate.py` proves it there. This pins the
shape that keeps the hold count at one, so a change back to a bare
`pg_try_advisory_lock` per tick fails without a database too.
"""

from __future__ import annotations

from types import SimpleNamespace

from src.services.target.scheduler import CLOCK_ELECTION_KEY, ClockElection


class _Conn:
    """Answers each `execute` from a queue and records what was sent."""

    def __init__(self, *answers):
        self.sent = []
        self._answers = list(answers)

    async def execute(self, statement, params=None):
        self.sent.append((str(statement), params))
        answer = self._answers.pop(0) if self._answers else None
        return SimpleNamespace(scalar=lambda: answer)


async def test_every_election_is_one_statement_that_confirms_before_it_takes():
    conn = _Conn(True, True, True)
    election = ClockElection(conn)
    for _ in range(3):
        assert await election.try_acquire() is True
    assert len(conn.sent) == 3, "one statement per tick, whether it takes or confirms"
    sql, params = conn.sent[0]
    assert "pg_locks" in sql and "pg_backend_pid()" in sql, (
        "it must confirm its own session's hold"
    )
    assert "pg_try_advisory_lock" in sql, "and take the lock only when it holds none"
    assert params == {"k": CLOCK_ELECTION_KEY}


async def test_the_database_answer_is_the_flag():
    conn = _Conn(True, False)
    election = ClockElection(conn)
    assert await election.try_acquire() is True and election.held is True
    assert await election.try_acquire() is False and election.held is False


async def test_release_unlocks_once_and_only_when_held():
    conn = _Conn(True, True)
    election = ClockElection(conn)
    await election.release()
    assert conn.sent == [], "nothing held, nothing sent"
    await election.try_acquire()
    await election.release()
    await election.release()  # idempotent
    unlocks = [sql for sql, _ in conn.sent if "pg_advisory_unlock" in sql]
    assert len(unlocks) == 1
