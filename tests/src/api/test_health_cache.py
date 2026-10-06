"""`/health/scheduling`, `/health/posting` and `/health/delivery` reuse their
last answer.

All three are unauthenticated and each answer takes a connection from the API's
shared pool, so a caller polling them could drain the pool the webhook needs.
Each answer is kept for `HEALTH_CACHE_SECONDS`; these tests drive the window
with an injected clock and count the connections the routes open.
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from contextlib import asynccontextmanager

import pytest
from asyncpg.exceptions import LockNotAvailableError, QueryCanceledError
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError

from src.api.app import create_app
from src.api.routes.health import HEALTH_CACHE_SECONDS, AnswerCache
from src.services.target import (
    delivery_health,
    health_reads,
    posting_health,
    scheduling_health,
)

from .conftest import FakeEngine


class CountingEngine(FakeEngine):
    """A `FakeEngine` that counts `connect()` — the pool checkout."""

    def __init__(self):
        super().__init__()
        self.connects = 0

    @asynccontextmanager
    async def connect(self):
        self.connects += 1
        yield self.session


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def engine():
    return CountingEngine()


@pytest.fixture
def clock(app):
    clock = Clock()
    app.state.health_cache = AnswerCache(clock=clock)
    return clock


@pytest.fixture
def seams(monkeypatch, stubbed_bound):
    """Stub every seam of the three routes, the statement cap's included;
    return how often each route read."""
    reads = {"scheduling": 0, "posting": 0, "delivery": 0}

    async def lag(executor):
        reads["scheduling"] += 1
        return {"stalled": 0, "accounts_active": reads["scheduling"]}

    async def worker(executor):
        return {"succeeded_ever": 0, "last_success_age_seconds": None}

    async def freshness(executor):
        reads["posting"] += 1
        return {"posted_ever": reads["posting"]}

    async def attempts(executor):
        return {"debited_total": 0}

    async def destinations(executor):
        return {"accounts_active": 0}

    async def failures(executor):
        reads["delivery"] += 1
        return {"sent_in_window": reads["delivery"]}

    monkeypatch.setattr(scheduling_health, "scheduling_lag", lag)
    monkeypatch.setattr(scheduling_health, "worker_freshness", worker)
    monkeypatch.setattr(posting_health, "posting_freshness", freshness)
    monkeypatch.setattr(posting_health, "publish_attempts", attempts)
    monkeypatch.setattr(posting_health, "destinations", destinations)
    monkeypatch.setattr(delivery_health, "outbox_failures", failures)
    return reads


def test_the_window_is_thirty_seconds():
    assert HEALTH_CACHE_SECONDS == 30


@pytest.mark.parametrize(
    "path,key,field",
    [
        ("/health/scheduling", "scheduling", "accounts_active"),
        ("/health/posting", "posting", "posted_ever"),
        ("/health/delivery", "delivery", "sent_in_window"),
    ],
)
def test_a_second_hit_in_the_window_reuses_the_answer_and_opens_nothing(
    client, engine, clock, seams, path, key, field
):
    first = client.get(path)
    assert first.status_code == 200, first.text
    assert (engine.connects, seams[key]) == (1, 1)

    clock.now += HEALTH_CACHE_SECONDS - 0.001
    second = client.get(path)
    assert second.status_code == 200
    assert second.json() == first.json()
    assert (engine.connects, seams[key]) == (1, 1)

    clock.now += 0.001
    third = client.get(path)
    assert third.status_code == 200
    assert third.json()[field] == 2
    assert (engine.connects, seams[key]) == (2, 2)


def test_the_two_surfaces_are_cached_apart(client, engine, clock, seams):
    client.get("/health/scheduling")
    posting = client.get("/health/posting")
    assert posting.json()["posted_ever"] == 1
    assert engine.connects == 2


def test_a_failure_is_reused_for_the_window_too(app, engine, clock, seams, monkeypatch):
    """A database that raised is not asked again until the window passes, so an
    outage does not turn every poll into a fresh connection attempt."""
    calls = 0

    async def broken(executor):
        nonlocal calls
        calls += 1
        raise RuntimeError("database unreachable")

    monkeypatch.setattr(posting_health, "posting_freshness", broken)
    client = TestClient(app, raise_server_exceptions=False)

    assert client.get("/health/posting").status_code == 500
    assert client.get("/health/posting").status_code == 500
    assert (calls, engine.connects) == (1, 1)

    clock.now += HEALTH_CACHE_SECONDS
    assert client.get("/health/posting").status_code == 500
    assert (calls, engine.connects) == (2, 2)


def test_a_reused_failure_does_not_grow_its_traceback():
    """Re-raising the one cached exception would append each request's frames
    to its traceback for the whole window, and every 500 logs it. Each reuse
    starts again from where it failed, so the origin is kept."""
    cache = AnswerCache(clock=lambda: 0.0)

    async def broken():
        raise RuntimeError("database unreachable")

    def frames():
        try:
            asyncio.run(cache.answer("posting", broken))
        except RuntimeError as exc:
            return [f.name for f in traceback.extract_tb(exc.__traceback__)]

    first = frames()
    assert "broken" in first, "the failure keeps where it happened"
    assert first == frames() == frames()


def test_each_app_starts_with_an_empty_cache(seams):
    """The cache lives on `app.state`, so one app's answer never leaks into an
    app another test builds."""
    for _ in range(2):
        engine = CountingEngine()
        client = TestClient(create_app(engine=engine))
        assert client.get("/health/posting").status_code == 200
        assert engine.connects == 1


def test_an_absent_engine_is_still_a_503(seams):
    """The 503 opens no connection, so it is answered before the cache."""
    client = TestClient(create_app(env={}))
    assert client.get("/health/scheduling").status_code == 503
    assert client.get("/health/posting").status_code == 503


async def test_concurrent_misses_share_one_read():
    """The calls that arrive while one read is in flight wait for it rather
    than each starting their own: ten at once read once."""
    cache = AnswerCache(clock=lambda: 0.0)
    gate = asyncio.Event()
    reads = 0

    async def read():
        nonlocal reads
        reads += 1
        await gate.wait()
        return {"stalled": 0}

    calls = [asyncio.create_task(cache.answer("k", read)) for _ in range(10)]
    # One turn of the loop: every call is at the cache before the read may finish.
    await asyncio.sleep(0)
    gate.set()
    answers = await asyncio.gather(*calls)
    assert reads == 1
    assert answers == [{"stalled": 0}] * 10


def test_a_statement_past_its_timeout_answers_503(
    app, engine, clock, seams, monkeypatch, caplog
):
    """A read the statement cap cancelled (SQLSTATE 57014) is load, as a pool
    wait is: a 503 to retry, kept for the window like any failure, and logged
    once, by the read, not again for each answer the window reuses."""

    async def cancelled(executor):
        raise DBAPIError(
            "SELECT 1",
            None,
            QueryCanceledError("canceling statement due to statement timeout"),
        )

    monkeypatch.setattr(posting_health, "posting_freshness", cancelled)
    client = TestClient(app, raise_server_exceptions=False)
    with caplog.at_level(logging.WARNING, logger=health_reads.__name__):
        first = client.get("/health/posting")
        second = client.get("/health/posting")
    assert first.status_code == 503
    assert first.json() == {"detail": "busy — try again", "reason": "statement_timeout"}
    assert first.headers["retry-after"] == "1"
    assert second.status_code == 503
    assert engine.connects == 1
    assert len([r for r in caplog.records if r.name == health_reads.__name__]) == 1


def test_any_other_database_error_stays_a_500(app, engine, clock, seams, monkeypatch):
    """Only the cancel is load: any other database error is a fault, and stays
    the server's 500, a server error of another class included."""

    async def broken(executor):
        raise DBAPIError("SELECT 1", None, LockNotAvailableError("lock not available"))

    monkeypatch.setattr(posting_health, "posting_freshness", broken)
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/health/posting").status_code == 500


def test_each_read_runs_under_the_statement_cap(
    client, engine, clock, seams, monkeypatch
):
    """Each surface sets the cap on the connection it reads with, before
    anything else runs on it."""
    capped = []

    async def bound(executor):
        capped.append(executor)

    monkeypatch.setattr(health_reads, "bound", bound)
    assert client.get("/health/scheduling").status_code == 200
    assert client.get("/health/posting").status_code == 200
    assert client.get("/health/delivery").status_code == 200
    assert capped == [engine.session] * 3
