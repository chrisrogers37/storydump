"""Hermetic (no-database) proof that BaseRepository sessions are task-local (#557).

BaseRepository backs its session with per-instance ``ContextVar``s, so concurrent
asyncio tasks (every PTB ``concurrent_updates`` callback) and ``to_thread``
offloads that share one singleton repo each open their OWN SQLAlchemy Session —
a Session is not safe for concurrent use.

The real-Postgres *behavioral* proof (ack latency + isolation under the real PTB
Application) lives in ``tests/src/services/test_concurrent_callback_reactivity.py``
and is skipped in CI. This file proves the same isolation MECHANISM without a
database: ``get_db`` is mocked to hand out a distinct sentinel Session per call,
so "one Session per task" is provable from object identity alone. It runs in
CI's mock-based suite.
"""

import asyncio
from unittest.mock import patch

import pytest

from src.repositories.base_repository import BaseRepository


class _IsolationRepo(BaseRepository):
    """Concrete repo standing in for a production singleton repository."""


class _FakeSession:
    """Minimal Session stand-in: BaseRepository.db returns it (is_active) without
    hitting the DB, and each instance is a distinct object so identity across
    tasks is meaningful."""

    is_active = True

    def rollback(self):  # pragma: no cover - defensive, not exercised here
        pass

    def close(self):
        pass


def _fresh_session_get_db():
    """A ``get_db`` stand-in: each call yields a NEW ``_FakeSession``.

    Mirrors the real generator contract (``_open_session`` does
    ``next(get_db())``) so ``BaseRepository`` is exercised unmodified.
    """

    def fake_get_db():
        yield _FakeSession()

    return fake_get_db


@pytest.mark.unit
@pytest.mark.asyncio
async def test_contextvar_gives_each_task_its_own_session():
    """Five concurrent tasks on ONE singleton repo each open a distinct Session.

    This is the isolation lever behind concurrent_updates: without the per-task
    ContextVar, ``repo.db`` would hand every task the same cached ``self._db``.
    """
    repo = _IsolationRepo()

    with (
        patch("src.repositories.base_repository.get_db", _fresh_session_get_db()),
        patch("src.repositories.base_repository.db_circuit_breaker") as cb,
    ):
        cb.allow_request.return_value = True

        async def open_session_in_task():
            # Each gathered coroutine runs as its own Task with a copied context.
            # detach → forget any inherited session; the sleep forces interleave.
            repo.detach_session()
            await asyncio.sleep(0)
            return repo.db  # return the object (held) so id() can't be reused

        sessions = await asyncio.gather(*(open_session_in_task() for _ in range(5)))

    distinct = {id(s) for s in sessions}
    assert len(distinct) == 5, (
        f"per-task ContextVar isolation: expected 5 distinct sessions across "
        f"concurrent tasks on one singleton repo, got {len(distinct)}"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_same_context_reuses_cached_session():
    """Control: within ONE context (no detach), ``.db`` reuses its cached Session.

    Proves the distinctness above comes from detach + the per-task context copy,
    not merely from calling ``.db`` twice.
    """
    repo = _IsolationRepo()

    with (
        patch("src.repositories.base_repository.get_db", _fresh_session_get_db()),
        patch("src.repositories.base_repository.db_circuit_breaker") as cb,
    ):
        cb.allow_request.return_value = True
        first = repo.db
        second = repo.db

    assert first is second, "one context must reuse its cached Session"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_detach_opens_a_fresh_session_in_same_context():
    """``detach_session`` forgets the current session so the next ``.db`` is fresh.

    This is the primitive the autopost background task and the media_sync offload
    use to avoid sharing the parent's inherited session.
    """
    repo = _IsolationRepo()

    with (
        patch("src.repositories.base_repository.get_db", _fresh_session_get_db()),
        patch("src.repositories.base_repository.db_circuit_breaker") as cb,
    ):
        cb.allow_request.return_value = True
        before = repo.db
        repo.detach_session()
        after = repo.db

    assert before is not after, (
        "detach_session must drop the cached session so the next .db opens fresh"
    )
