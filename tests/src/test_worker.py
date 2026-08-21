"""W1 — the composition root's object graph (#942): what `python -m src.worker`
assembles, tested without connecting anything.

The compose() seam exists exactly so this is checkable: the W1 deployment's
live kinds, the clock's recurring set staying inside them (a clock that mints
work the registry parks would manufacture parked jobs on its own cadence),
and the heartbeat/lease numbers agreeing.
"""

from src.services.target.work_loop import Parked, WorkerConfig
from src.worker import compose


def test_w1_composition_live_kinds_are_plan_slot_and_reap_expired_without_cloudinary():
    app = compose(engine=object(), config=WorkerConfig(), env={})
    live = {k for k, e in app.registry.items() if not isinstance(e, Parked)}
    assert live == {"plan_slot", "reap_expired"}


def test_cloudinary_config_brings_the_transit_reaper_live():
    env = {
        "CLOUDINARY_CLOUD_NAME": "c",
        "CLOUDINARY_API_KEY": "k",
        "CLOUDINARY_API_SECRET": "s",
    }
    app = compose(engine=object(), config=WorkerConfig(), env=env)
    live = {k for k, e in app.registry.items() if not isinstance(e, Parked)}
    assert "reap_transit_assets" in live


def test_clock_recurring_kinds_are_a_subset_of_the_live_registry():
    app = compose(engine=object(), config=WorkerConfig(), env={})
    live = {k for k, e in app.registry.items() if not isinstance(e, Parked)}
    recurring = set(app.recurring) - {"v"}
    assert recurring, "the clock must mint at least one recurring singleton"
    assert recurring <= live, (
        f"the clock would mint kinds this deployment parks: {sorted(recurring - live)}"
    )


def test_both_lanes_are_served():
    app = compose(engine=object(), config=WorkerConfig(), env={})
    assert {loop.lane for loop in app.loops} == {"interactive", "bulk"}


def test_heartbeat_and_lease_numbers_agree():
    cfg = WorkerConfig()
    app = compose(engine=object(), config=cfg, env={})
    assert app.heartbeat_lease_seconds == cfg.lease_seconds
    assert cfg.sender_hold_seconds < cfg.lease_seconds
    assert app.heartbeat_interval_seconds < cfg.lease_seconds / 2


class TestEngineUrlFromEnv:
    """TARGET_DATABASE_URL is the branch-soak/deploy door: a plain postgres URL
    in, an asyncpg-dialect URL out, with the libpq-only params asyncpg refuses
    rewritten (Neon hands out `sslmode=require&channel_binding=require`)."""

    def test_plain_postgres_url_gains_the_asyncpg_driver(self):
        from src.worker import engine_url_from_env

        url = engine_url_from_env({"TARGET_DATABASE_URL": "postgresql://u:p@h/db"})
        assert url == "postgresql+asyncpg://u:p@h/db"

    def test_libpq_ssl_params_are_rewritten_for_asyncpg(self):
        from src.worker import engine_url_from_env

        url = engine_url_from_env(
            {
                "TARGET_DATABASE_URL": "postgresql://u:p@h/db?sslmode=require&channel_binding=require"
            }
        )
        assert "sslmode" not in url and "channel_binding" not in url
        assert url.endswith("?ssl=require")

    def test_absent_env_returns_none_so_settings_decide(self):
        from src.worker import engine_url_from_env

        assert engine_url_from_env({}) is None


class TestStatusLine:
    """The soak's visibility: one line a human can read from the log, built
    from the observables the loops/clock/heartbeat already keep."""

    def test_status_line_carries_every_lane_and_the_clock_and_heartbeat(self):
        from src.worker import status_line

        class _L:
            def __init__(self, lane):
                self.lane = lane
                self.processed, self.parked, self.failures, self.fenced = 3, 1, 0, 0

        class _C:
            ticks, inserts, elected, consecutive_failures = 40, 2, True, 0

        class _H:
            beats, short_beats, consecutive_failures = 12, 0, 0

        line = status_line(
            loops=[_L("interactive"), _L("bulk")], clock=_C, heartbeat=_H
        )
        for token in (
            "interactive",
            "bulk",
            "processed=3",
            "parked=1",
            "ticks=40",
            "elected=True",
            "beats=12",
        ):
            assert token in line


class TestTransportComposition:
    """W2: the channel goes live only through a LIVE credential, and a dead
    one is a named, recurring, observable state — the shitpost-alpha lesson
    as compose/run behavior."""

    def test_a_supplied_transport_brings_deliver_outbox_live(self):
        from src.services.target.work_loop import Parked

        class _T:
            def for_chat(self, ref):
                async def send(row):
                    return "1"

                return send

        app = compose(engine=object(), config=WorkerConfig(), env={}, transport=_T())
        assert not isinstance(app.registry["deliver_outbox"], Parked)

    def test_no_transport_parks_with_the_w2_reason(self):
        from src.services.target.work_loop import Parked

        app = compose(engine=object(), config=WorkerConfig(), env={})
        entry = app.registry["deliver_outbox"]
        assert isinstance(entry, Parked) and "transport" in entry.reason

    async def test_a_dead_probe_parks_the_channel_loudly_and_names_the_dead_token(
        self, caplog
    ):
        from src.channels.telegram_transport import TelegramAuthDead
        from src.services.target.work_loop import Parked
        from src.worker import apply_transport_probe

        class _Dead:
            def for_chat(self, ref):  # pragma: no cover - never bound
                raise AssertionError

            async def probe(self):
                raise TelegramAuthDead("getMe: 401 Unauthorized")

        app = compose(engine=object(), config=WorkerConfig(), env={}, transport=_Dead())
        with caplog.at_level("ERROR"):
            await apply_transport_probe(app)

        entry = app.registry["deliver_outbox"]
        assert isinstance(entry, Parked)
        assert "DEAD" in entry.reason and "credential" in entry.reason.lower()
        assert any("DEAD" in r.message for r in caplog.records)
        # the loops share the same dict object, so the park reaches them
        assert app.loops[0]._registry is app.registry

    async def test_a_live_probe_logs_the_bot_identity_and_keeps_the_channel(self):
        from src.services.target.work_loop import Parked
        from src.worker import apply_transport_probe

        class _Live:
            def for_chat(self, ref):
                async def send(row):
                    return "1"

                return send

            async def probe(self):
                return "soak_bot"

        app = compose(engine=object(), config=WorkerConfig(), env={}, transport=_Live())
        await apply_transport_probe(app)
        assert not isinstance(app.registry["deliver_outbox"], Parked)


class TestTaskSupervision:
    """navi's class finding on #958: two background tasks could die silently
    (the sweeper's missing config field; a lane's unguarded claim error) while
    the worker kept printing healthy status lines. The worker now supervises
    every task it owns: a death is logged loudly, stops the worker, and
    surfaces as a raise — silence is structurally impossible."""

    def test_the_sweep_cadence_field_exists(self):
        assert WorkerConfig().sender_sweep_seconds > 0

    async def test_supervise_returns_the_task_that_died(self):
        import asyncio

        from src.worker import supervise

        async def dies():
            raise RuntimeError("boom")

        async def healthy():
            await asyncio.Event().wait()

        stop = asyncio.Event()
        t_dead = asyncio.create_task(dies(), name="doomed")
        t_ok = asyncio.create_task(healthy(), name="fine")
        died = await supervise(stop, [t_dead, t_ok])
        assert died is t_dead
        assert isinstance(died.exception(), RuntimeError)
        t_ok.cancel()

    async def test_a_real_death_coincident_with_stop_is_still_reported(self):
        """navi's cycle-2 adversarial case, taken as shipped: an independent
        crash racing an external stop signal must surface as a death — the
        blanket stop.is_set() exclusion masked it as a clean stop."""
        import asyncio

        from src.worker import supervise

        stop = asyncio.Event()

        async def crashes():
            raise RuntimeError("real unrelated crash")

        t = asyncio.create_task(crashes(), name="victim")
        stop.set()
        await asyncio.sleep(0)
        died = await supervise(stop, [t])
        assert died is t
        assert isinstance(died.exception(), RuntimeError)

    async def test_a_clean_exit_while_stop_is_unset_is_still_a_death(self):
        """The case the per-exception condition alone would drop: every
        supervised body is a while-not-stop loop, so a CLEAN return with stop
        unset is only reachable through a bug — it must be a death, not a
        quiet exit-0 shutdown."""
        import asyncio

        from src.worker import supervise

        async def wanders_off():
            return  # no exception, no stop — a loop that just... ended

        stop = asyncio.Event()
        t = asyncio.create_task(wanders_off(), name="wanderer")
        died = await supervise(stop, [t])
        assert died is t and died.exception() is None

    async def test_a_task_exiting_because_stop_fired_is_not_a_death(self):
        """The race the soak caught live: stop fires, a task's own loop sees
        it and returns, and both land in the same FIRST_COMPLETED batch. An
        exit AFTER stop is a clean stop, never a death."""
        import asyncio

        from src.worker import supervise

        stop = asyncio.Event()

        async def stops_with_us():
            await stop.wait()

        t = asyncio.create_task(stops_with_us(), name="obedient")
        stop.set()
        await asyncio.sleep(0)  # let the task finish alongside the waiter
        died = await supervise(stop, [t])
        assert died is None

    async def test_supervise_returns_none_when_stop_fires_first(self):
        import asyncio

        from src.worker import supervise

        async def healthy():
            await asyncio.Event().wait()

        stop = asyncio.Event()
        t_ok = asyncio.create_task(healthy(), name="fine")
        stop.set()
        died = await supervise(stop, [t_ok])
        assert died is None and not t_ok.done()
        t_ok.cancel()


class TestSweeperObservables:
    def test_status_line_carries_the_sweeper_counters(self):
        from src.worker import status_line

        class _L:
            lane, processed, parked, failures, fenced = "bulk", 0, 0, 0, 0

        class _H:
            beats, short_beats, consecutive_failures = 0, 0, 0

        class _S:
            sweeps, mints = 7, 2

        line = status_line(loops=[_L], clock=None, heartbeat=_H, sweeper=_S)
        assert "sweeps=7" in line and "mints=2" in line
