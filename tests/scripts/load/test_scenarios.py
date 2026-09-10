"""S.1 — the versioned load harness (`02_api-under-load.md` step 5) and the
F1/F5 evidence (step 6).

Skipped unless `RUN_LOAD_HARNESS=1`: it stands a scratch database, seeds
50 × 20 cards (plus the smaller worlds the other scenarios need), starts the
real API (the Procfile's `uvicorn src.api.app:app`) and the target worker as
subprocesses against a fake Telegram on this machine, offers the four
scenarios the plan names, and writes `reports/<date>.md`. Each scenario
states its workspace spread so the admission cap (F12: 120/min/workspace) is
visible in the numbers rather than hidden by them.

    RUN_LOAD_HARNESS=1 PATH=/opt/homebrew/opt/postgresql@15/bin:$PATH \\
      DB_HOST=localhost DB_PORT=65433 DB_USER=test_user DB_PASSWORD=test_password \\
      DB_NAME=storyline_ai TEST_DB_NAME=storyline_test REQUIRE_TEST_DATABASE=1 \\
      python -m pytest tests/scripts/load -q -m load -p no:cacheprovider --no-cov

`LOAD_HARNESS_WORKERS=2` runs the API with `--workers 2` (F5 (a)).
`LOAD_HARNESS_RTT` names the run's round-trip conditions in the report.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import time
from pathlib import Path

import psycopg2
import pytest

from tests.scripts.conftest import (
    _scratch,
    replay_advertised_stream,
    set_test_passwords,
)

from . import harness as h
from .fake_telegram import BOT_USERNAME, FakeServer, FakeTelegram
from .latency_proxy import LatencyProxy, through_proxy
from .processes import SECRET, Api, Worker, process_env
from .seed import seed_world

pytestmark = [pytest.mark.load, pytest.mark.integration]

ENABLED = os.environ.get("RUN_LOAD_HARNESS") == "1"
REPORTS = Path(__file__).resolve().parent / "reports"
SLOW_CHAT_DELAY_S = 5.0
SETTLE_S = 12.0  # how long edits are awaited after the last delivery


def _sql(dsn, sql, params=None, fetch=False):
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'system'")
            cur.execute(sql, params)
            return cur.fetchall() if fetch else cur.rowcount
    finally:
        conn.close()


@pytest.fixture(scope="module")
def stage(admin_conn, owner_actor):
    """The whole stage: database, worlds, fake Telegram, API, worker."""
    if not ENABLED:
        pytest.skip("RUN_LOAD_HARNESS=1 runs the load harness")
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    fake = server = api = worker = proxy = None
    try:
        dsn = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        name = dsn.rsplit("/", 1)[-1]
        admin_conn.autocommit = True
        with admin_conn.cursor() as cur:
            cur.execute(f'ALTER DATABASE "{name}" SET synchronous_commit = off')
        worlds = {
            "many": seed_world(dsn, workspaces=50, cards_per_workspace=20, tag="many"),
            "one": seed_world(
                dsn,
                workspaces=1,
                cards_per_workspace=1,
                tag="one",
                first_chat=-200_000_000,
                first_tapper=2_000_000,
            ),
            "cards": seed_world(
                dsn,
                workspaces=10,
                cards_per_workspace=20,
                tag="cards",
                first_chat=-300_000_000,
                first_tapper=3_000_000,
            ),
            "burst": seed_world(
                dsn,
                workspaces=50,
                cards_per_workspace=20,
                tag="burst",
                first_chat=-500_000_000,
                first_tapper=5_000_000,
            ),
            "slow": seed_world(
                dsn,
                workspaces=10,
                cards_per_workspace=20,
                tag="slow",
                first_chat=-400_000_000,
                first_tapper=4_000_000,
            ),
        }
        slow_chat = worlds["slow"].workspaces[0].bindings[0].chat
        fake = FakeTelegram(chat_delays={slow_chat: SLOW_CHAT_DELAY_S})
        server = FakeServer(fake).start()
        workers = int(os.environ.get("LOAD_HARNESS_WORKERS") or 1)
        # A production-like round trip: the processes reach Postgres through
        # a proxy that adds LOAD_HARNESS_DB_DELAY_MS per direction.
        delay_ms = float(os.environ.get("LOAD_HARNESS_DB_DELAY_MS") or 0)
        process_dsn = dsn
        if delay_ms:
            from urllib.parse import urlsplit

            parts = urlsplit(dsn)
            proxy = LatencyProxy(
                parts.hostname or "127.0.0.1",
                parts.port or 5432,
                delay_s=delay_ms / 1000,
            ).start()
            process_dsn = through_proxy(dsn, proxy)
        env = process_env(
            database_url=process_dsn,
            fake_base=server.base,
            bot_username=BOT_USERNAME,
            workers=workers,
        )
        api = Api(env, workers=workers)
        health = api.wait_ready()
        worker = Worker(env)
        time.sleep(3.0)  # the worker's probe and first beats
        container = {
            "version": _sql(dsn, "SHOW server_version", fetch=True)[0][0],
            "synchronous_commit": _sql(dsn, "SHOW synchronous_commit", fetch=True)[0][
                0
            ],
            "max_connections": _sql(dsn, "SHOW max_connections", fetch=True)[0][0],
        }
        yield {
            "dsn": dsn,
            "worlds": worlds,
            "slow_chat": slow_chat,
            "fake": fake,
            "api": api,
            "worker": worker,
            "health_at_start": health,
            "container": container,
            "scenarios": [],
        }
    finally:
        for proc in (worker, api):
            if proc is not None:
                proc.stop()
        if server is not None:
            server.stop()
        if proxy is not None:
            proxy.stop()
        gen.close()


@pytest.fixture(scope="module", autouse=True)
def report(stage):
    yield
    scenarios = stage["scenarios"]
    if not scenarios:
        return
    REPORTS.mkdir(exist_ok=True)
    run_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    suffix = (
        f"-rtt{os.environ['LOAD_HARNESS_DB_DELAY_MS']}ms"
        if os.environ.get("LOAD_HARNESS_DB_DELAY_MS")
        else ""
    )
    workers = os.environ.get("LOAD_HARNESS_WORKERS")
    if workers and workers != "1":
        suffix += f"-workers{workers}"
    path = REPORTS / f"{dt.datetime.now(dt.timezone.utc).date().isoformat()}{suffix}.md"
    path.write_text(
        h.render_report(
            run_at=run_at,
            rtt_note=os.environ.get("LOAD_HARNESS_RTT")
            or (
                f"loopback + {os.environ.get('LOAD_HARNESS_DB_DELAY_MS')} ms injected"
                " per direction on the database socket (≈ twice that per round trip)"
                if os.environ.get("LOAD_HARNESS_DB_DELAY_MS")
                else "loopback (API, worker, fake Telegram and Postgres on one"
                " machine — Docker fsync off)"
            ),
            container=stage["container"],
            api_health=stage["api"].health(),
            scenarios=scenarios,
        )
    )
    print(f"\nload harness report: {path}")


def _db_counters(dsn) -> tuple[int, int]:
    (commits, written) = _sql(
        dsn,
        "SELECT xact_commit, tup_inserted + tup_updated + tup_deleted"
        " FROM pg_stat_database WHERE datname = current_database()",
        fetch=True,
    )[0]
    return int(commits), int(written)


def _run_scenario(
    stage, *, name, spread, taps, offer_within_s=0.0, notes=(), max_connections=None
):
    client = h.Client(
        stage["api"].base,
        SECRET,
        **({"max_connections": max_connections} if max_connections else {}),
    )
    fake: FakeTelegram = stage["fake"]
    before = len(fake.snapshot())
    db_before = _db_counters(stage["dsn"])
    deliveries = asyncio.run(client.deliver(taps, offer_within_s=offer_within_s))
    # The database delta is the DELIVERIES' — sampled before the settle, so
    # the sender's backlog (its own commits) stays out of "per tap".
    time.sleep(1.0)  # pg_stat_database flushes stats within ~500 ms
    db_after = _db_counters(stage["dsn"])
    fake.pending_update_count = client.queued_peak
    # Let the sender land the outcome lines (paced: 18/min/chat, 2 s cadence).
    time.sleep(SETTLE_S)
    intents = tuple({d.tap.intent_id for d in deliveries})
    (flips,) = _sql(
        stage["dsn"],
        "SELECT count(*) FROM post_intents WHERE id = ANY(%s::uuid[])"
        " AND state <> 'awaiting_approval'",
        (list(intents),),
        fetch=True,
    )[0]
    (audit,) = _sql(
        stage["dsn"],
        "SELECT count(*) FROM audit_events WHERE entity_kind = 'post_intent'"
        " AND entity_id = ANY(%s::uuid[]) AND from_state IS DISTINCT FROM to_state",
        (list(intents),),
        fetch=True,
    )[0]
    scenario = h.Scenario(
        name=name,
        spread=spread,
        deliveries=deliveries,
        fake=fake,
        settled_until=time.monotonic(),
        health_after=stage["api"].health(),
        flips=int(flips),
        audit_rows=int(audit),
        pending_peak=client.queued_peak,
        xact_commit=db_after[0] - db_before[0],
        rows_written=db_after[1] - db_before[1],
        notes=list(notes)
        + [
            f"fake calls during the scenario: {len(fake.snapshot()) - before}",
            f"delivered at max_connections = {client.max_connections}",
        ],
    )
    stage["scenarios"].append(scenario)
    return scenario, scenario.numbers()


def test_taps_1000_across_50_workspaces(stage):
    world = stage["worlds"]["many"]
    taps = [h.tap_for(ws, card) for ws, card in world.cards()]
    assert len(taps) == 1000
    scenario, n = _run_scenario(
        stage,
        name="taps_1000_across_50_workspaces",
        spread="50 workspaces × 20 cards, one tap each; 1,000 taps offered within 1 s;"
        " delivered at max_connections",
        taps=taps,
        offer_within_s=1.0,
    )
    assert n["five_xx"] == 0, n
    assert n["answer_p95_s"] is not None and n["answer_p95_s"] < 2.0, n
    assert n["flips"] == 1000, n


def test_double_tap_one_card(stage):
    ws = stage["worlds"]["one"].workspaces[0]
    card = ws.cards[0]
    taps = [h.tap_for(ws, card) for _ in range(50)]
    scenario, n = _run_scenario(
        stage,
        name="double_tap_one_card",
        spread="1 workspace, 1 card; 50 taps offered within 100 ms",
        taps=taps,
        offer_within_s=0.1,
        notes=["exactly one flip; the other 49 answered with the card's state"],
    )
    assert n["five_xx"] == 0, n
    assert n["flips"] == 1, n
    assert n["by_outcome"].get("executed") == 1, n
    assert n["by_outcome"].get("answered") == 49, n
    assert n["answer_p95_s"] is not None and n["answer_p95_s"] < 2.0, n


def test_taps_across_many_cards(stage):
    world = stage["worlds"]["cards"]
    taps = [h.tap_for(ws, card) for ws, card in world.cards()]
    scenario, n = _run_scenario(
        stage,
        name="taps_across_many_cards",
        spread="10 workspaces × 20 cards, one tap each, all at once (20/workspace, inside F12's 120/min)",
        taps=taps,
    )
    assert n["five_xx"] == 0, n
    assert n["flips"] == 200, n
    stage["many_cards_answer_p95"] = n["answer_p95_s"]
    stage["many_cards_outcome_p95"] = n["outcome_p95_s"]


def test_one_slow_chat(stage):
    world = stage["worlds"]["slow"]
    slow = stage["slow_chat"]
    taps = [h.tap_for(ws, card) for ws, card in world.cards()]
    scenario, n = _run_scenario(
        stage,
        name="one_slow_chat",
        spread=f"10 workspaces × 20 cards; the fake answers every call for chat {slow}"
        f" after {SLOW_CHAT_DELAY_S:g} s, the others at once",
        taps=taps,
        notes=[
            "edit-landed criterion for the OTHER chats rides phase 3a's sender hold"
            " (`03_worker-throughput.md`); reported here, judged there",
        ],
    )
    # The other chats: answered within budget and within 200 ms of the same
    # run's `taps_across_many_cards` p95.
    others = [d for d in scenario.deliveries if d.tap.chat != slow]
    answers = stage["fake"].answers()
    lat = sorted(
        answers[d.tap.callback_query_id] - d.first_attempt_at
        for d in others
        if d.tap.callback_query_id in answers
    )
    p95 = h.pct(lat, 95)
    scenario.notes.append(f"other chats: answer p95 = {p95:.3f} s over {len(lat)} taps")
    assert n["five_xx"] == 0, n
    assert p95 is not None and p95 < 2.0
    base = stage.get("many_cards_answer_p95")
    if base is not None:
        assert p95 <= base + 0.2, (p95, base)


def test_taps_1000_at_twenty_connections_reach_the_boundary(stage):
    """The busy boundary, EXERCISED: Telegram's registered `max_connections`
    (10) equals the pool's size, so at 10 the pool can never saturate and
    `busy` is 0 by construction. Delivering at 20 — what F5 (a) would
    register with `--workers 2` — lets checkouts exceed the pool and shows
    the boundary answering rather than failing: busy answers, never a 5xx on
    a tap. A busy tap is consumed with 200 and NOT re-offered here (Telegram
    would not either — the person taps again), so flips + busy == taps."""
    world = stage["worlds"]["burst"]
    taps = [h.tap_for(ws, card) for ws, card in world.cards()]
    scenario, n = _run_scenario(
        stage,
        name="taps_1000_at_twenty_connections",
        spread="50 workspaces × 20 cards, one tap each; 1,000 taps offered within 1 s;"
        " delivered at TWENTY connections against a pool of ten",
        taps=taps,
        offer_within_s=1.0,
        max_connections=20,
        notes=["the boundary's own scenario: the only run where the pool can saturate"],
    )
    assert n["five_xx"] == 0, n
    assert n["flips"] + n["busy"] == 1000, n
    stage["boundary_busy"] = n["busy"]
