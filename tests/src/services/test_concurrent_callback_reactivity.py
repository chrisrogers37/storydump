"""RED integration harness for the #557 concurrency fix (two levers, one gate).

Making concurrent Telegram button-taps *both* reactive *and* safe requires two
independent production changes that this harness measures against the REAL
``telegram.ext.Application`` dispatch path:

  Lever 1 — ``concurrent_updates`` on the Application.
      Concurrency is applied inside ``Application.__update_fetcher`` (started by
      ``await app.start()``): it pulls each update off ``app.update_queue`` and,
      *iff* ``app._update_processor.max_concurrent_updates > 1``, dispatches it
      as a semaphore-gated ``create_task`` (CONCURRENT). When ``== 1`` it
      ``await``s each update wrapper in turn (SERIAL). So a single slow handler
      blocks every later tap until it finishes.

  Lever 2 — per-callback DB-session isolation.
      Production repositories are long-lived singletons and ``BaseRepository``
      caches ONE session on ``self._db`` (opened lazily, reused forever). Under
      Lever 1 the update handlers now run as concurrent tasks — but they all
      reach through the same singleton repo and receive the *same* ``Session``
      object. A SQLAlchemy ``Session`` is not safe for concurrent use.

Why not the existing tests: ``TestAutopostMediaOffload`` calls the handler
method directly and probes the loop from inside the same coroutine — it proves
the loop is not parked, but it never exercises PTB's real cross-update dispatch,
so it cannot see Lever 1 or Lever 2. This harness instead builds a real
``Application`` with a real ``CallbackQueryHandler``, feeds updates through
``app.update_queue``, and lets ``__update_fetcher`` dispatch them.

The harness sets Lever 1 itself (it builds the app with ``concurrent_updates(N)``)
so it can *demonstrate the discrimination* between serial (N=1) and concurrent
(N=8) dispatch. Lever 2, however, is owned by production code: because
``BaseRepository`` still shares one session, the N=8 case observes a single
session id across every concurrently-handled tap. That is the intended RED — it
stays red until per-callback session isolation lands.

Offline notes: ``app.initialize()`` would call ``bot.get_me()`` (network), so
``Bot.get_me`` and ``Bot.answer_callback_query`` are patched; the ack timestamp
is recorded per ``callback_query_id`` inside the ``answer_callback_query`` patch.
"""

import asyncio
import os
import time
import unittest.mock as mock
from unittest.mock import AsyncMock

import pytest
import telegram
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
from telegram import Update, User
from telegram.ext import ApplicationBuilder, CallbackQueryHandler

from src.repositories.base_repository import BaseRepository

# The slow tap holds in-flight work via an OFFLOADED blocking op — models the
# post-#572 media transfer that runs off the loop (loop stays free during it).
SLOW_SECONDS = 0.5
# A reactive fast-tap ack must land well inside Telegram's callback validity
# window; 50ms is comfortably reactive and ~100x below the serial floor.
REACTIVE_CEILING_S = 0.05
FAKE_TOKEN = "123456:AAHfake-fake-fake"  # never used for a real request

SLOW_USER = 1000
FAST_USERS = range(2001, 2006)  # five other users tapping concurrently


class _ProbeRepo(BaseRepository):
    """Concrete repo modelling production's singleton repos.

    ``BaseRepository`` stores a single instance-level session on ``self._db`` and
    hands it back on every ``.db`` access — so one shared instance = one shared
    session across all callers, exactly the production shape under test.
    """


def _callback_update(update_id: int, user_id: int, data: str) -> dict:
    """Minimal wire dict for a CallbackQuery update (bind via Update.de_json)."""
    return {
        "update_id": update_id,
        "callback_query": {
            "id": str(update_id),
            "from": {"id": user_id, "is_bot": False, "first_name": "U"},
            "chat_instance": "ci",
            "data": data,
            "message": {
                "message_id": update_id,
                "date": 0,
                "chat": {"id": 999, "type": "group"},
                "from": {"id": 1, "is_bot": True, "first_name": "b"},
            },
        },
    }


async def _drive_real_application(n_concurrent: int) -> dict:
    """Drive a real Application at ``concurrent_updates(n_concurrent)``.

    Fires one slow tap followed by five fast taps back-to-back onto the update
    queue and lets ``__update_fetcher`` dispatch them. Returns measured latency,
    session-id distinctness, and any handler exceptions.
    """
    repo = _ProbeRepo()
    session_ids: dict[str, int] = {}  # callback_query id -> id(session) it saw
    ack_times: dict[str, float] = {}  # callback_query id -> loop time of ack
    errors: list[tuple[str, str]] = []
    done = asyncio.Event()
    completed = 0
    n_total = 1 + len(FAST_USERS)

    async def on_callback(update: Update, _context) -> None:
        nonlocal completed
        cq = update.callback_query
        try:
            # Goes through production get_db()/SessionLocal -> the .env.test DB.
            sess = repo.db
            sess.execute(text("SELECT 1"))
            repo.end_read_transaction()
            session_ids[cq.id] = id(sess)
            if cq.data == "slow":
                # Offloaded blocking op — the loop is free to service fast taps.
                await asyncio.to_thread(time.sleep, SLOW_SECONDS)
        except Exception as exc:  # noqa: BLE001 — collect, do not propagate
            errors.append((cq.id, repr(exc)))
        finally:
            # Always ack so latency is recorded even if the DB work raised.
            await cq.answer()
            completed += 1
            if completed >= n_total:
                done.set()

    fake_me = User(id=42, is_bot=True, first_name="probe", username="probe_bot")

    def record_ack(*_args, **kwargs) -> bool:
        ack_times[kwargs.get("callback_query_id")] = asyncio.get_running_loop().time()
        return True

    with (
        mock.patch.object(
            telegram.Bot, "get_me", new_callable=AsyncMock, return_value=fake_me
        ),
        mock.patch.object(
            telegram.Bot,
            "answer_callback_query",
            new_callable=AsyncMock,
            side_effect=record_ack,
        ),
    ):
        app = (
            ApplicationBuilder()
            .token(FAKE_TOKEN)
            .updater(None)  # no real Updater; start() still runs the fetcher
            .concurrent_updates(n_concurrent)
            .build()
        )
        app.add_handler(CallbackQueryHandler(on_callback))
        # The mocked get_me returns a User but does not cache it the way the real
        # method does; seed the bot-user so start() can read bot.id offline.
        app.bot._bot_user = fake_me

        await app.initialize()
        await app.start()
        try:
            loop = asyncio.get_running_loop()
            updates = [_callback_update(1, SLOW_USER, "slow")]
            for idx, uid in enumerate(FAST_USERS, start=2):
                updates.append(_callback_update(idx, uid, f"fast:{uid}"))

            t0 = loop.time()
            for payload in updates:
                await app.update_queue.put(Update.de_json(payload, app.bot))

            await asyncio.wait_for(done.wait(), timeout=10)
        finally:
            await app.stop()
            await app.shutdown()
            repo.close()

    fast_latencies = {cid: ack_times[cid] - t0 for cid in ack_times if cid != "1"}
    return {
        "n_concurrent": n_concurrent,
        "fast_latencies": fast_latencies,
        "max_fast_latency": max(fast_latencies.values()),
        "n_handlers": len(session_ids),
        "distinct_session_ids": len(set(session_ids.values())),
        "errors": errors,
    }


@pytest.fixture(autouse=True)
def _behavioral_db_gate(setup_test_database, monkeypatch):
    """Gate: this is a real-Postgres behavioral timing proof, not a CI unit test.

    Skips in CI — storydump CI is mock-based with no real database, and the
    ack-latency assertions below need controlled timing that a shared CI runner
    can't guarantee. The proof runs locally and in review (rajan reproduces it
    under real conditions). The mechanism it proves (per-task ContextVar session
    isolation) is covered hermetically, without a DB, in
    tests/src/repositories/test_base_repository_isolation.py — which DOES run in
    CI.

    When it does run, it routes the production session factory at the conftest
    test DB (current schema, isolated from the real ``storyline_ai``) so
    ``_ProbeRepo().db`` exercises real per-task sessions.
    """
    if os.environ.get("CI"):
        pytest.skip(
            "real-PTB behavioral timing proof — verified locally / in review, "
            "not in CI (mock-based, no real DB); mechanism covered hermetically "
            "in test_base_repository_isolation.py"
        )
    if setup_test_database is None:
        pytest.skip("real Postgres not available — behavioral proof requires it")

    import src.config.database as db_module

    monkeypatch.setattr(
        db_module,
        "SessionLocal",
        sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=setup_test_database,
            expire_on_commit=False,
        ),
    )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("n_concurrent", [1, 8])
async def test_concurrent_callback_reactivity_and_isolation(n_concurrent: int):
    """One slow tap + five fast taps through the REAL Application dispatch path.

    N=1 (serial baseline) proves the harness actually measures cross-update
    reactivity: the fast taps queue behind the slow one, so their ack latency is
    ~SLOW_SECONDS. N=8 proves the fast taps become reactive (<50ms) AND asserts
    per-callback session isolation — which FAILS on current code because the
    singleton repo hands every concurrent handler the same session.
    """
    res = await _drive_real_application(n_concurrent)

    latencies = sorted(round(v, 4) for v in res["fast_latencies"].values())
    print(
        f"\n[concurrent_updates={n_concurrent}] "
        f"fast ack latencies={latencies}s "
        f"max={res['max_fast_latency']:.4f}s | "
        f"handlers={res['n_handlers']} "
        f"distinct_session_ids={res['distinct_session_ids']} | "
        f"errors={res['errors']}"
    )

    if n_concurrent == 1:
        # SERIAL: the slow tap blocks the fetcher, so every fast ack lands only
        # after SLOW_SECONDS. This is the control that proves the measurement.
        assert res["max_fast_latency"] >= SLOW_SECONDS * 0.8, (
            f"expected serial dispatch (~{SLOW_SECONDS}s) at concurrent_updates=1, "
            f"got max fast ack {res['max_fast_latency']:.4f}s"
        )
        return

    # --- concurrent_updates=8 ---

    # Lever 1 (reactivity): with concurrency the fast taps are not blocked by the
    # slow one. Passes even on current code — the harness supplies this lever.
    assert res["max_fast_latency"] < REACTIVE_CEILING_S, (
        f"fast taps not reactive under concurrent dispatch: max ack "
        f"{res['max_fast_latency']:.4f}s >= {REACTIVE_CEILING_S}s"
    )

    # A shared session used across concurrent tasks can raise; surface it.
    assert not res["errors"], (
        f"handlers raised under concurrent shared-session use: {res['errors']}"
    )

    # Lever 2 (isolation) — THE RED. Each concurrently-handled tap must see its
    # own session. On current code BaseRepository caches one self._db, so every
    # handler observes the same session id -> distinct count collapses to 1.
    assert res["distinct_session_ids"] == res["n_handlers"], (
        f"per-callback session isolation FAILS: {res['n_handlers']} concurrent "
        f"handlers observed {res['distinct_session_ids']} distinct session id "
        f"(expected {res['n_handlers']}, one per task) — the singleton repo "
        f"shares a single SQLAlchemy Session across concurrent callbacks"
    )
