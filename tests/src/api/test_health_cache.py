"""`/health/scheduling` and `/health/posting` reuse their last answer.

Both are unauthenticated and each answer takes a connection from the API's
shared pool, so a caller polling them could drain the pool the webhook needs.
Each answer is kept for `HEALTH_CACHE_SECONDS`; these tests drive the window
with an injected clock and count the connections the routes open.
"""

from __future__ import annotations

import asyncio
import traceback
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.routes.health import HEALTH_CACHE_SECONDS, AnswerCache
from src.services.target import posting_health, scheduling_health

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
def seams(monkeypatch):
    """Stub every seam of both routes; return how often each route read."""
    reads = {"scheduling": 0, "posting": 0}

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

    monkeypatch.setattr(scheduling_health, "scheduling_lag", lag)
    monkeypatch.setattr(scheduling_health, "worker_freshness", worker)
    monkeypatch.setattr(posting_health, "posting_freshness", freshness)
    monkeypatch.setattr(posting_health, "publish_attempts", attempts)
    monkeypatch.setattr(posting_health, "destinations", destinations)
    return reads


def test_the_window_is_thirty_seconds():
    assert HEALTH_CACHE_SECONDS == 30


@pytest.mark.parametrize(
    "path,key,field",
    [
        ("/health/scheduling", "scheduling", "accounts_active"),
        ("/health/posting", "posting", "posted_ever"),
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
