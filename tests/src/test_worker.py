"""The composition root's object graph: what `python -m src.worker`
assembles, tested without connecting anything.

The compose() seam exists exactly so this is checkable: the deployment's
live kinds, the clock's recurring set staying inside them (a clock that mints
work the registry parks would manufacture parked jobs on its own cadence),
and the heartbeat/lease numbers agreeing.
"""

import logging

import pytest

from src.services.target.work_loop import _UNBUILT_REASON, Parked, WorkerConfig
from src.worker import (
    ACTIVATION_NUDGE_ENV,
    ACTIVATION_NUDGE_LIMIT_ENV,
    _env_flag,
    compose,
    logger as worker_logger,
    worker_config_from_env,
)


def test_composition_parks_only_for_a_named_reason():
    """A kind is dead in a bare composition ONLY because something is missing
    and SAYS SO — never silently.

    A hardcoded set of live kinds pins the CURRENT state as correct: it
    fails the moment anyone adds a job kind, passes again the moment they paste
    the name in, and never tests the property it is meant to pin. Such a
    literal exists twice in `test_work_loop.py`, which is how one change can
    patch one copy and be red on another.

    So the expectation is DERIVED from the registry's own park reasons instead:

    - `_UNBUILT_REASON` marks a kind with no executor anywhere.
    - any OTHER reason marks a seam this deployment did not wire.

    The invariant is that those two sets account for EVERY difference between a
    bare composition and a fully-configured one. A new seam-free kind such as
    `revoke_workspace_credentials` changes both sides equally and this
    test does not move. A kind that starts parking for no stated reason does.

    What it deliberately does not pin: WHICH kinds are live. That would be a
    snapshot, and a snapshot makes this a maintenance tax rather than a gate.
    """
    bare = compose(engine=object(), config=WorkerConfig(), env={}).registry
    full = compose(
        engine=object(),
        config=WorkerConfig(),
        env={
            "CLOUDINARY_CLOUD_NAME": "c",
            "CLOUDINARY_API_KEY": "k",
            "CLOUDINARY_API_SECRET": "s",
        },
    ).registry

    def live_of(reg):
        return {k for k, e in reg.items() if not isinstance(e, Parked)}

    live_bare, live_full = live_of(bare), live_of(full)

    # A composition that parks everything is a dead worker, and every assertion
    # below is vacuously true of one. This is the control.
    assert live_bare, "a bare composition ran nothing at all"

    # Nothing may be live bare and dead when MORE is configured.
    assert live_bare <= live_full

    # The whole difference is seams, each one named. Not silence.
    seam_gated = {
        k
        for k, e in bare.items()
        if isinstance(e, Parked) and e.reason != _UNBUILT_REASON
    }
    assert live_full - live_bare == seam_gated & live_full
    for kind in seam_gated:
        assert bare[kind].reason.strip(), f"{kind} parked with an empty reason"


def test_cloudinary_config_brings_the_transit_reaper_live():
    env = {
        "CLOUDINARY_CLOUD_NAME": "c",
        "CLOUDINARY_API_KEY": "k",
        "CLOUDINARY_API_SECRET": "s",
    }
    app = compose(engine=object(), config=WorkerConfig(), env=env)
    live = {k for k, e in app.registry.items() if not isinstance(e, Parked)}
    assert "reap_transit_assets" in live


def test_the_activation_nudge_is_off_and_off_the_clock_by_default(caplog):
    """Built off: nothing mints the sweep until it is switched on."""
    with caplog.at_level(logging.INFO, logger=worker_logger.name):
        app = compose(engine=object(), config=WorkerConfig(), env={})
    assert isinstance(app.registry["activation_nudge_sweep"], Parked)
    assert "activation_nudge_sweep" not in app.recurring
    assert "activation nudge armed" not in caplog.text


def test_an_armed_activation_nudge_is_on_the_clock_daily_and_says_so(caplog):
    """The arming line is the deploy log's only mark that real people may be
    emailed, and it carries the cap the sweep runs with."""
    env = {"RESEND_API_KEY": "re_test", "EMAIL_FROM": "hello@example.com"}
    config = WorkerConfig(
        activation_nudge_enabled=True,
        web_app_origin="https://app.example",
        activation_nudge_limit=7,
    )
    with caplog.at_level(logging.INFO, logger=worker_logger.name):
        app = compose(engine=object(), config=config, env=env)
    assert not isinstance(app.registry["activation_nudge_sweep"], Parked)
    assert app.recurring["activation_nudge_sweep"] == 24 * 3600.0
    assert "activation nudge armed (limit=7/day)" in caplog.text


def test_a_live_runs_bounds_are_a_small_first_run():
    """What one armed day may do. Three emails, because the latch is set
    as each is queued, so a provider mistake uses them up; only people who
    signed up in the last 30 days; each idle for 72 hours."""
    config = WorkerConfig()
    assert config.activation_nudge_limit == 3
    assert config.activation_nudge_since_days == 30
    assert config.activation_nudge_stall_seconds == 72 * 3600


def test_the_config_main_runs_with_carries_the_nudges_switch_and_cap():
    """The switch reaches the config `main` builds, not only the flag
    helper, and so does the cap."""
    origin = "https://app.example"
    default = worker_config_from_env({}, web_app_origin=origin)
    assert default.activation_nudge_enabled is False
    assert default.activation_nudge_limit == WorkerConfig().activation_nudge_limit
    assert default.web_app_origin == origin
    armed = worker_config_from_env(
        {ACTIVATION_NUDGE_ENV: "on", ACTIVATION_NUDGE_LIMIT_ENV: " 12 "},
        web_app_origin=origin,
    )
    assert armed.activation_nudge_enabled is True
    assert armed.activation_nudge_limit == 12


@pytest.mark.parametrize("raw", ["0", "-1", "ten", "2.5"])
def test_a_nudge_cap_that_is_not_a_count_is_refused_by_name(raw):
    with pytest.raises(ValueError, match=ACTIVATION_NUDGE_LIMIT_ENV):
        worker_config_from_env({ACTIVATION_NUDGE_LIMIT_ENV: raw}, web_app_origin=None)


@pytest.mark.parametrize(
    "raw, armed",
    [
        ("true", True),
        ("1", True),
        (" On ", True),
        ("yes", True),
        ("", False),
        ("false", False),
        ("0", False),
        ("enabled", False),
        (None, False),
    ],
)
def test_the_nudge_switch_reads_the_truthy_spellings_only(raw, armed):
    env = {} if raw is None else {ACTIVATION_NUDGE_ENV: raw}
    assert _env_flag(env, ACTIVATION_NUDGE_ENV) is armed


def test_clock_recurring_kinds_are_a_subset_of_the_live_registry():
    app = compose(engine=object(), config=WorkerConfig(), env={})
    live = {k for k, e in app.registry.items() if not isinstance(e, Parked)}
    recurring = set(app.recurring) - {"v"}
    assert recurring, "the clock must mint at least one recurring singleton"
    assert recurring <= live, (
        f"the clock would mint kinds this deployment parks: {sorted(recurring - live)}"
    )


def test_the_monitors_stale_threshold_spans_three_beats_of_the_fastest_kind():
    """`DEFAULT_WORKER_STALE_S` (`storydump_cli/health_classify/scheduling.py`)
    rests on the fastest recurring kind of the bare composition — the set every
    deployment mints. It must span three beats — two plus one of slack — or one
    late beat pages a healthy worker. Slowing the fastest kinds (`reap_expired`
    and `reconcile_ambiguous`, both 60 s) breaks this: raise the threshold with
    the cadence, never loosen this assertion.
    """
    from storydump_cli.health_classify.scheduling import DEFAULT_WORKER_STALE_S

    app = compose(engine=object(), config=WorkerConfig(), env={})
    cadences = {kind: secs for kind, secs in app.recurring.items() if kind != "v"}
    beat = min(cadences.values())
    assert 3 * beat <= DEFAULT_WORKER_STALE_S, (
        f"the fastest recurring kind beats every {beat:.0f}s, so three beats — two "
        f"plus one of slack — ({3 * beat:.0f}s) no longer fit inside the monitor's "
        f"{DEFAULT_WORKER_STALE_S}s worker-down threshold — raise "
        f"DEFAULT_WORKER_STALE_S with the cadence: {cadences}"
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


def test_the_late_window_is_one_hour():
    """A planned story is still served up to 60 minutes after its time,
    and missed out loud past it. A change to the number is a product decision
    this test makes visible."""
    cfg = WorkerConfig()
    assert cfg.planned_late_seconds == 60 * 60
    assert cfg.planned_late_seconds > cfg.prompt_sweep_seconds, (
        "a window shorter than one sweep beat would miss a story the worker"
        " was up to serve"
    )


def test_a_card_is_late_only_past_what_the_worker_s_own_beats_explain():
    """`prompts.SERVED_LATE_AFTER` is justified by the prompt sweep's beat and
    the sender's: a card served on the next beat and sent on the sender's must
    not read "served late", or every planned card would."""
    from src.services.target import prompts

    cfg = WorkerConfig()
    assert prompts.SERVED_LATE_AFTER.total_seconds() > (
        cfg.prompt_sweep_seconds + cfg.sender_sweep_seconds
    )


def test_a_stopped_loop_is_restarted_before_the_monitor_pages_for_it():
    """The watchdog ends a worker whose event loop stopped turning
    (`loop_watchdog`), and the platform restarts it. The page
    (`DEFAULT_WORKER_STALE_S`) is for a worker nothing repaired, so the whole
    repair has to fit inside it, term by term: the last success can be one
    beat of the fastest recurring kind old when the loop stops; the watchdog
    sees it after its bound and one check; the report takes its grace at most;
    and the restarted worker's first success is allowed one more beat. Raise
    the bound, or lower the threshold, and this fails until the other moves.
    """
    from src.services.target import loop_watchdog
    from storydump_cli.health_classify.scheduling import DEFAULT_WORKER_STALE_S

    cfg = WorkerConfig()
    app = compose(engine=object(), config=cfg, env={})
    beat = min(secs for kind, secs in app.recurring.items() if kind != "v")
    repair = (
        beat
        + cfg.loop_stall_seconds
        + cfg.loop_beat_seconds
        + loop_watchdog.REPORT_GRACE_SECONDS
        + beat
    )
    assert repair <= DEFAULT_WORKER_STALE_S, (
        f"a stopped loop is repaired in up to {repair:.0f}s (two {beat:.0f}s beats"
        f" around a {cfg.loop_stall_seconds:.0f}s bound), past the monitor's"
        f" {DEFAULT_WORKER_STALE_S}s worker-down threshold: the page would arrive"
        " for a stall the worker was already repairing"
    )


class TestEngineUrlFromEnv:
    """TARGET_DATABASE_URL is the branch-soak/deploy door: a plain postgres URL
    in, an asyncpg-dialect URL out, with the libpq-only params asyncpg refuses
    rewritten (Neon hands out `sslmode=require&channel_binding=require`)."""

    def test_plain_postgres_url_gains_the_asyncpg_driver(self):
        from src.services.target.unit_of_work import engine_url_from_env

        url = engine_url_from_env(
            {"TARGET_DATABASE_URL": "postgresql://u:not-a-real-password@h/db"}
        )
        assert url == "postgresql+asyncpg://u:not-a-real-password@h/db"

    def test_libpq_ssl_params_are_rewritten_for_asyncpg(self):
        from src.services.target.unit_of_work import engine_url_from_env

        url = engine_url_from_env(
            {
                "TARGET_DATABASE_URL": "postgresql://u:not-a-real-password@h/db?sslmode=require&channel_binding=require"
            }
        )
        assert "sslmode" not in url and "channel_binding" not in url
        assert url.endswith("?ssl=require")

    def test_absent_env_returns_none_for_the_caller_to_refuse(self):
        from src.services.target.unit_of_work import engine_url_from_env

        assert engine_url_from_env({}) is None

    def test_a_blank_value_is_absent(self):
        from src.services.target.unit_of_work import engine_url_from_env

        assert engine_url_from_env({"TARGET_DATABASE_URL": ""}) is None
        assert engine_url_from_env({"TARGET_DATABASE_URL": "   "}) is None


class TestAsyncpgUrl:
    """`unit_of_work.asyncpg_url` renders SQLAlchemy's one reading of a URL
    for asyncpg, so what the engine reads back is what was written, but for
    the libpq-only parameters it drops and a repeated parameter, which takes
    its last value."""

    @staticmethod
    def _round_trip(url):
        from sqlalchemy.engine import make_url

        from src.services.target.unit_of_work import asyncpg_url

        return make_url(url), make_url(asyncpg_url(url))

    def test_the_production_shape_keeps_who_connects_where_and_takes_ssl(self):
        given, read = self._round_trip(
            "postgresql://owner:not-a-real-password@ep-x.region.aws.neon.tech/db"
            "?sslmode=require&channel_binding=require"
        )
        assert read.drivername == "postgresql+asyncpg"
        assert dict(read.query) == {"ssl": "require"}
        for part in ("username", "password", "host", "port", "database"):
            assert getattr(read, part) == getattr(given, part), part

    def test_an_ssl_parameter_already_there_is_kept(self):
        _, read = self._round_trip(
            "postgresql://owner:not-a-real-password@db.invalid/db"
            "?ssl=verify-full&sslmode=require"
        )
        assert dict(read.query) == {"ssl": "verify-full"}

    def test_a_repeated_host_takes_its_last_value_as_libpq_does(self):
        _, read = self._round_trip(
            "postgresql://owner@/db?host=a.invalid:5432&host=b.invalid:5432"
        )
        assert read.query["host"] == "b.invalid:5432"

    def test_any_other_repeated_parameter_takes_its_last_value(self):
        _, read = self._round_trip(
            "postgresql://owner:not-a-real-password@db.invalid/db"
            "?sslmode=disable&sslmode=require&application_name=a&application_name=b"
        )
        assert dict(read.query) == {"ssl": "require", "application_name": "b"}

    @pytest.mark.parametrize(
        "form", ["raw-slash", "raw-question-mark", "raw-hash", "raw-bracket"]
    )
    def test_a_raw_delimiter_in_the_password_reads_back_as_written(self, form):
        from tests.unreadable_urls import PASSWORDS, URLS

        given, read = self._round_trip(URLS[form])
        assert read.password == PASSWORDS[form]
        for part in ("username", "password", "host", "port", "database"):
            assert getattr(read, part) == getattr(given, part), part

    def test_an_encoded_option_reads_back_as_written(self):
        _, read = self._round_trip(
            "postgresql://owner:not-a-real-password@db.invalid/db"
            "?options=-c%20search_path%3Dx"
        )
        assert read.query["options"] == "-c search_path=x"

    def test_a_percent_encoded_password_reads_back_as_written(self):
        _, read = self._round_trip(
            "postgresql://owner:not-a-real%40%2F%3F%23%25password@db.invalid/db"
        )
        assert read.password == "not-a-real@/?#%password"

    def test_an_asyncpg_url_or_another_scheme_is_left_as_it_is(self):
        _, read = self._round_trip("postgresql+asyncpg://owner@db.invalid/db")
        assert read.drivername == "postgresql+asyncpg"
        _, read = self._round_trip("postgres://owner@db.invalid/db")
        assert read.drivername == "postgres"


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
    """The channel goes live only through a LIVE credential, and a dead one is
    a named, recurring, observable state."""

    def test_a_supplied_transport_brings_deliver_outbox_live(self):
        from src.services.target.work_loop import Parked

        class _T:
            def for_chat(self, ref):
                async def send(row):
                    return "1"

                return send

        app = compose(engine=object(), config=WorkerConfig(), env={}, transport=_T())
        assert not isinstance(app.registry["deliver_outbox"], Parked)

    def test_no_transport_parks_naming_the_missing_transport(self):
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

    async def test_a_token_for_the_wrong_bot_parks_the_channel_loudly(self, caplog):
        """A token for another bot: the worker would send cards as @example_bot
        while the API's webhook listens on @storydump_app_bot — every tap would
        go where nothing listens. With the configured bot known, a mismatched
        token parks the sender and says why."""
        from src.services.target.work_loop import Parked
        from src.worker import apply_transport_probe

        class _Other:
            def for_chat(self, ref):  # pragma: no cover - never bound
                raise AssertionError

            async def probe(self):
                return "example_bot"

        app = compose(
            engine=object(),
            config=WorkerConfig(),
            env={"TARGET_TELEGRAM_BOT_USERNAME": "@storydump_app_bot"},
            transport=_Other(),
        )
        with caplog.at_level("ERROR"):
            await apply_transport_probe(app)
        entry = app.registry["deliver_outbox"]
        assert isinstance(entry, Parked)
        assert "WRONG BOT" in entry.reason and "example_bot" in entry.reason
        assert "storydump_app_bot" in entry.reason
        assert any("configured bot" in r.message for r in caplog.records)
        assert app.bot_username == "example_bot"

    async def test_the_configured_bot_keeps_the_channel(self):
        from src.services.target.work_loop import Parked
        from src.worker import apply_transport_probe

        class _Right:
            def for_chat(self, ref):
                async def send(row):
                    return "1"

                return send

            async def probe(self):
                return "Storydump_App_Bot"

        app = compose(
            engine=object(),
            config=WorkerConfig(),
            env={"TARGET_TELEGRAM_BOT_USERNAME": "storydump_app_bot"},
            transport=_Right(),
        )
        await apply_transport_probe(app)
        assert not isinstance(app.registry["deliver_outbox"], Parked)
        assert app.bot_username == "Storydump_App_Bot"

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
    """The worker supervises every task it owns: a death is logged
    loudly, stops the worker, and surfaces as a raise — silence is
    structurally impossible."""

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
        """An independent crash racing an external stop signal must surface as a
        death — the blanket stop.is_set() exclusion masked it as a clean stop."""
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
        """The race: stop fires, a task's own loop sees it and returns, and
        both land in the same FIRST_COMPLETED batch. An exit AFTER stop is a
        clean stop, never a death."""
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

    def test_status_line_carries_the_watchdog_and_says_when_it_is_gone(self):
        from src.worker import status_line

        class _L:
            lane, processed, parked, failures, fenced = "bulk", 0, 0, 0, 0

        class _H:
            beats, short_beats, consecutive_failures = 0, 0, 0

        class _W:
            armed = True

        line = status_line(loops=[_L], clock=None, heartbeat=_H, watchdog=_W)
        assert "watchdog[armed=True]" in line
        _W.armed = False
        line = status_line(loops=[_L], clock=None, heartbeat=_H, watchdog=_W)
        assert "watchdog[armed=False]" in line
        assert "watchdog" not in status_line(loops=[_L], clock=None, heartbeat=_H)


class TestPromptSweeperConsumesTheSweep:
    """The sweep's count vocabulary is the sweeper's contract. It has no
    `failed_no_surface` leg: the web queue is a surface every workspace
    has, so "no reachable surface" cannot occur. A sweeper reaching
    for that key would raise INSIDE its own `except Exception` — the sweep
    itself committed, the counters before the bad read still moved, and the
    only symptom is a "prompt sweep failed" line on every cadence. So the
    assertion that carries this test is the log staying silent."""

    @pytest.fixture
    def sweeper(self, monkeypatch):
        """A PromptSweeper over no database: each leg's transaction is the one
        `make_session_for` opens, stood in for by a no-op, and every one it
        opens is recorded."""
        from contextlib import asynccontextmanager

        from src import worker

        opened = []

        def make_session_for(engine):
            @asynccontextmanager
            async def session_for(job):
                opened.append(job)
                from types import SimpleNamespace

                yield SimpleNamespace(info={})

            return session_for

        monkeypatch.setattr(worker.unit_of_work, "make_session_for", make_session_for)

        class _Config:
            prompt_sweep_seconds = 0.01
            # The door reads its batch from the config too;
            # a stub missing it spins the loop's `except Exception` forever.
            prompt_sweep_limit = 50
            planned_late_seconds = 3600

        class _App:
            engine = object()
            config = _Config()

        return worker, worker.PromptSweeper(_App()), opened

    async def test_one_sweep_moves_exactly_the_counts_the_sweep_returned(
        self, sweeper, monkeypatch, caplog
    ):
        import asyncio
        import logging

        worker, prompt_sweeper, opened = sweeper
        stop = asyncio.Event()
        asked = []

        async def fake_sweep(session, *, limit, late_seconds):
            asked.append(("serve", limit, late_seconds))
            stop.set()  # one iteration: both legs run, then the loop sees the stop
            return {"prompted": 2, "advanced": 1}  # the whole vocabulary

        async def fake_misses(session, *, limit, late_seconds):
            asked.append(("miss", limit, late_seconds))
            return {
                "missed": 3,
                "unheard": 1,
            }  # the miss leg's vocabulary (`fn_planned_serve` / `fn_planned_misses`)

        monkeypatch.setattr(worker.prompts_mod, "sweep_due_prompts", fake_sweep)
        monkeypatch.setattr(worker.prompts_mod, "sweep_planned_misses", fake_misses)
        with caplog.at_level(logging.ERROR, logger="target.worker"):
            await prompt_sweeper.run(stop)

        p = prompt_sweeper
        assert (p.sweeps, p.prompted, p.advanced) == (1, 2, 1)
        assert (p.missed, p.unheard) == (3, 1)
        assert asked == [("serve", 50, 3600), ("miss", 50, 3600)], (
            "both legs, serve first, at the config's batch and late window"
        )
        assert opened == [{}, {}], "a transaction per leg, neither with a tenant"
        assert "sweep failed" not in caplog.text

    @pytest.mark.parametrize(
        "broken, logged, counts",
        [
            ("serve", "prompt sweep failed", (0, 1)),
            ("miss", "planned-miss sweep failed", (1, 0)),
        ],
    )
    async def test_a_failing_leg_never_holds_the_other_back(
        self, sweeper, monkeypatch, caplog, broken, logged, counts
    ):
        """The serve leg and the miss leg run in two transactions: a fault in
        either is logged and the other still runs, in both directions."""
        import asyncio
        import logging

        worker, prompt_sweeper, _ = sweeper
        stop = asyncio.Event()
        ran = []

        async def fake_sweep(session, *, limit, late_seconds):
            ran.append("serve")
            stop.set()
            if broken == "serve":
                raise RuntimeError("serve leg down")
            return {"prompted": 1, "advanced": 1}

        async def fake_misses(session, *, limit, late_seconds):
            ran.append("miss")
            if broken == "miss":
                raise RuntimeError("miss leg down")
            return {"missed": 1, "unheard": 0}

        monkeypatch.setattr(worker.prompts_mod, "sweep_due_prompts", fake_sweep)
        monkeypatch.setattr(worker.prompts_mod, "sweep_planned_misses", fake_misses)
        with caplog.at_level(logging.ERROR, logger="target.worker"):
            await prompt_sweeper.run(stop)

        assert ran == ["serve", "miss"], ran
        assert logged in caplog.text
        assert (prompt_sweeper.prompted, prompt_sweeper.missed) == counts

    def test_the_status_line_carries_the_miss_counters(self):
        from src.worker import status_line

        class _L:
            lane = "bulk"
            processed = parked = failures = fenced = exhausted = 0

        class _H:
            beats, short_beats, consecutive_failures = 0, 0, 0

        class _P:
            sweeps, prompted, advanced, approved, missed, unheard = 9, 4, 4, 3, 2, 1

        line = status_line(loops=[_L], clock=None, heartbeat=_H, prompt_sweeper=_P)
        assert (
            "prompts[sweeps=9 prompted=4 advanced=4 approved=3 missed=2 unheard=1]"
            in line
        )


class TestThePublishLegIsWired:
    """With a Drive adapter and the Cloudinary trio the
    publish_pipeline kind is LIVE; without Drive it parks naming media_fetch;
    the Graph adapter is always built and the fetch reads the intent row."""

    def test_drive_and_cloudinary_bring_publish_pipeline_live(self):
        from src.services.target.work_loop import Parked

        env = {
            "CLOUDINARY_CLOUD_NAME": "c",
            "CLOUDINARY_API_KEY": "k",
            "CLOUDINARY_API_SECRET": "s",
        }
        app = compose(
            engine=object(),
            config=WorkerConfig(),
            env=env,
            adapters={"gdrive": object()},
        )
        assert not isinstance(app.registry["publish_pipeline"], Parked)
        assert app.deps.meta is not None and app.deps.media_fetch is not None

    def test_without_drive_it_parks_naming_the_fetch(self):
        from src.services.target.work_loop import Parked

        env = {
            "CLOUDINARY_CLOUD_NAME": "c",
            "CLOUDINARY_API_KEY": "k",
            "CLOUDINARY_API_SECRET": "s",
        }
        app = compose(engine=object(), config=WorkerConfig(), env=env)
        entry = app.registry["publish_pipeline"]
        assert isinstance(entry, Parked) and entry.reason.startswith(
            "media_fetch is not wired"
        )

    async def test_the_publish_fetch_reads_the_intent_row_under_the_story_cap(self):
        from src.worker import PUBLISH_MAX_BYTES, _publish_media_fetch

        seen = {}

        class _Drive:
            async def fetch_bytes(self, config, file_ref, **kw):
                seen.update(kw, config=config, file_ref=file_ref)
                return b"jpeg-bytes", "f.jpg", "image/jpeg"

        fetch = _publish_media_fetch({"gdrive": _Drive()})
        intent = {
            "source_id": "src-1",
            "workspace_id": "ws-1",
            "provider_file_ref": "ref-1",
            "media_kind": "video",
            "provider": "gdrive",
            "source_config": {"v": 1, "folder_ref": "folder-1"},
            "source_state": "active",
        }
        assert await fetch(intent) == b"jpeg-bytes"
        assert seen == {
            "source_id": "src-1",
            "workspace_id": "ws-1",
            "file_ref": "ref-1",
            "config": {"v": 1, "folder_ref": "folder-1"},
            "max_bytes": PUBLISH_MAX_BYTES["video"],
        }


class TestThePublishAdapterTakesTheSharedGraphVersion:
    """The application has one Graph version, `instagram_graph.graph_version`:
    `META_GRAPH_VERSION`, else the default. The publish adapter is built with
    it here; the past-posts composition takes the same function's answer
    (`test_past_posts_composition.py`)."""

    @pytest.mark.parametrize("env", [{}, {"META_GRAPH_VERSION": ""}])
    def test_unset_or_blank_is_the_default(self, env):
        from src.services.target.instagram_graph import DEFAULT_GRAPH_VERSION

        app = compose(engine=object(), config=WorkerConfig(), env=env)
        assert app.deps.meta._version == DEFAULT_GRAPH_VERSION

    def test_the_variable_overrides_it(self):
        env = {"META_GRAPH_VERSION": "v23.0"}
        app = compose(engine=object(), config=WorkerConfig(), env=env)
        assert app.deps.meta._version == "v23.0"


def _a_conversion_record():
    from src.services.target.mix_conversion import MixConversionEvidence

    return MixConversionEvidence("run-1", "d" * 64)


class TestTheConversionRunIsReadOnce:
    """`main` reads the release's conversion run once and hands it to
    `compose`, which reads it itself only when it is handed none. Every process
    has one: there is no record to be missing."""

    @pytest.mark.parametrize("release", ["b" * 40, None], ids=["deployed", "local"])
    def test_the_worker_s_run_is_its_release_s(self, release):
        import src.worker as worker
        from src.services.target import mix_conversion

        env = {} if release is None else {"RAILWAY_GIT_COMMIT_SHA": release}
        run = worker._conversion_evidence(env)
        assert run == mix_conversion.release_evidence(release)
        assert isinstance(run, mix_conversion.MixConversionEvidence)

    def test_compose_reads_the_record_when_it_is_handed_none(self, monkeypatch):
        import src.worker as worker

        record, reads = _a_conversion_record(), []
        monkeypatch.setattr(
            worker, "_conversion_evidence", lambda env: reads.append(env) or record
        )
        env = {}
        app = compose(engine=object(), config=WorkerConfig(), env=env)
        assert reads == [env]
        assert app.mix_conversion_evidence is record

    def test_compose_takes_the_record_it_is_handed_and_reads_none(self, monkeypatch):
        import src.worker as worker

        def read_again(env):
            pytest.fail("compose read the conversion record it was handed")

        monkeypatch.setattr(worker, "_conversion_evidence", read_again)
        record = _a_conversion_record()
        app = compose(
            engine=object(), config=WorkerConfig(), env={}, conversion_evidence=record
        )
        assert app.mix_conversion_evidence is record
        assert app.deps.mix_conversion_evidence is record

    def test_main_reads_the_run_once_and_hands_it_to_compose(
        self, ring_env, monkeypatch
    ):
        import src.worker as worker
        from cryptography.fernet import Fernet

        record, reads, handed = _a_conversion_record(), [], {}

        def build_worker(**kwargs):
            handed["past_posts"] = kwargs

        def fake_compose(**kwargs):
            handed["compose"] = kwargs
            return "app"

        ring_env(key=Fernet.generate_key().decode())
        monkeypatch.setenv("TARGET_DATABASE_URL", "postgresql://nobody@localhost:1/x")
        monkeypatch.delenv("TARGET_TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.setattr(
            worker, "_conversion_evidence", lambda env: reads.append(env) or record
        )
        monkeypatch.setattr(worker.unit_of_work, "create_engine", lambda url: "engine")
        monkeypatch.setattr(worker.past_posts_composition, "build_worker", build_worker)
        monkeypatch.setattr(worker, "compose", fake_compose)
        monkeypatch.setattr(worker, "run", lambda app: app)
        monkeypatch.setattr(worker.asyncio, "run", lambda coroutine: None)
        worker.main()
        assert len(reads) == 1
        assert "conversion" not in handed["past_posts"]
        assert handed["compose"]["conversion_evidence"] is record


class TestThePastPostsSourceTakesTheConfiguredNumbers:
    def test_main_hands_it_the_retry_and_the_download_budgets(
        self, ring_env, monkeypatch
    ):
        """A selected post's fetch is tried again, and downloads, under
        `WorkerConfig`'s numbers."""
        import src.worker as worker
        from cryptography.fernet import Fernet

        from src.services.target.past_posts_retry import Retry

        handed = {}

        def build_worker(**kwargs):
            handed.update(kwargs)

        ring_env(key=Fernet.generate_key().decode())
        monkeypatch.setenv("TARGET_DATABASE_URL", "postgresql://nobody@localhost:1/x")
        monkeypatch.delenv("TARGET_TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.setattr(
            worker, "_conversion_evidence", lambda env: _a_conversion_record()
        )
        monkeypatch.setattr(worker.unit_of_work, "create_engine", lambda url: "engine")
        monkeypatch.setattr(worker.past_posts_composition, "build_worker", build_worker)
        monkeypatch.setattr(worker, "compose", lambda **kwargs: "app")
        monkeypatch.setattr(worker, "run", lambda app: app)
        monkeypatch.setattr(worker.asyncio, "run", lambda coroutine: None)
        worker.main()
        config = WorkerConfig()
        assert handed["retry"] == Retry.from_config(config)
        assert handed["download_seconds"] == config.past_posts_download_seconds
        assert config.past_posts_download_seconds == {"image": 120.0, "video": 240.0}


class TestTheReconcilerPollIsWired:
    """Production does not run `poll=None` — the ambiguous
    ladder asks Meta for the container's status through the Graph adapter."""

    def test_compose_supplies_the_poll_seam(self):
        app = compose(engine=object(), config=WorkerConfig(), env={})
        assert callable(app.deps.poll)

    async def test_the_poll_returns_the_containers_status_for_the_intent(self):
        from src.worker import _poll_from

        class _Meta:
            def __init__(self):
                self.calls = []

            async def container_status(
                self, container_id, *, provider_account_ref=None, workspace_id=None
            ):
                self.calls.append((container_id, provider_account_ref, workspace_id))
                return "PUBLISHED"

        meta = _Meta()
        poll = _poll_from(
            object(),
            meta,
            session_factory=_scripted_session_factory(
                {
                    "ig_container_id": "ctr-7",
                    "provider_account_ref": "1784",
                    "workspace_id": "ws-1",
                }
            ),
        )
        assert await poll(intent_id="i-1", workspace_id="ws-1") == "PUBLISHED"
        assert meta.calls == [("ctr-7", "1784", "ws-1")]

    async def test_no_container_or_a_typed_error_is_inconclusive_not_a_crash(self):
        from src.services.target.meta_adapter import MetaRetryableError
        from src.worker import _poll_from

        class _Dead:
            async def container_status(
                self, container_id, *, provider_account_ref=None, workspace_id=None
            ):
                raise MetaRetryableError(code=190, message="dead token")

        none = _poll_from(
            object(),
            _Dead(),
            session_factory=_scripted_session_factory(
                {
                    "ig_container_id": None,
                    "provider_account_ref": "1784",
                    "workspace_id": "ws-1",
                }
            ),
        )
        assert await none(intent_id="i-1", workspace_id="ws-1") is None
        dead = _poll_from(
            object(),
            _Dead(),
            session_factory=_scripted_session_factory(
                {
                    "ig_container_id": "ctr-7",
                    "provider_account_ref": "1784",
                    "workspace_id": "ws-1",
                }
            ),
        )
        assert await dead(intent_id="i-1", workspace_id="ws-1") is None


def _scripted_session_factory(row):
    class _Result:
        def mappings(self):
            return self

        def first(self):
            return row

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, statement, params=None):
            return _Result()

    return _Session


def test_the_story_video_cap_is_cloudinarys_synchronous_limit():
    from src.worker import PUBLISH_MAX_BYTES

    assert PUBLISH_MAX_BYTES["video"] == 40 * 1000 * 1000
    assert PUBLISH_MAX_BYTES["image"] == 8 * 1024 * 1024


class TestBackpressureOnTheStatusLine:
    """The status line carries the queue's depth and age per lane, the outbox
    backlog and the pacing state — 'visible backpressure', from one read the
    reporter takes itself."""

    def test_status_line_renders_the_snapshot_and_the_exhausted_counter(self):
        from src.worker import status_line

        class _L:
            lane, processed, parked, failures, fenced, exhausted = "bulk", 0, 0, 2, 0, 1

        class _H:
            beats, short_beats, consecutive_failures = 0, 0, 0

        snap = {
            "lanes": {
                "interactive": {"ready": 1, "oldest_age_s": 0.5},
                "bulk": {"ready": 7, "oldest_age_s": 42.0},
            },
            "outbox_pending": 3,
            "tg_global": {"paced_windows_last_minute": 2, "hold_active": False},
            "ws_oldest_wait": {"workspace_id": "abcdef12-0000", "wait_s": 9.0},
        }
        line = status_line(loops=[_L], clock=None, heartbeat=_H, backpressure=snap)
        for token in (
            "exhausted=1",
            "bulk[ready=7 oldest_age=42.0s]",
            "outbox_pending=3",
            "tg_global_paced=2",
            "ws_oldest_wait=abcdef12 9.0s",
        ):
            assert token in line, line

    def test_without_a_snapshot_the_line_is_unchanged(self):
        from src.worker import status_line

        class _L:
            lane, processed, parked, failures, fenced = "bulk", 0, 0, 0, 0

        class _H:
            beats, short_beats, consecutive_failures = 0, 0, 0

        assert "queue" not in status_line(loops=[_L], clock=None, heartbeat=_H)


class TestKTasksPerLane:
    """K loops per lane on pooled checkouts, from the config, with
    the ceiling asserted at composition; the env names override the K."""

    def _compose(self, config):
        from types import SimpleNamespace

        from src.worker import compose

        engine = SimpleNamespace(connect=lambda: None)
        return compose(engine=engine, config=config, env={})

    def test_compose_builds_k_loops_per_lane_sharing_the_registry(self):
        from src.services.target.work_loop import WorkerConfig

        app = self._compose(WorkerConfig())
        by_lane = {}
        for wl in app.loops:
            by_lane.setdefault(wl.lane, []).append(wl)
        assert {lane: len(loops) for lane, loops in by_lane.items()} == {
            "interactive": 3,
            "bulk": 2,
        }
        assert len({wl._worker_name for wl in app.loops}) == 5, "distinct names"
        assert all(wl._registry is app.registry for wl in app.loops)
        assert all(wl._connect is app.engine.connect for wl in app.loops)

    def test_an_oversubscribed_config_refuses_at_composition(self):
        from src.services.target.work_loop import WorkerConfig

        with pytest.raises(ValueError):
            self._compose(WorkerConfig(lane_concurrency={"interactive": 4, "bulk": 2}))

    def test_lane_concurrency_from_env(self):
        from src.worker import lane_concurrency_from_env

        assert lane_concurrency_from_env({}) == {"interactive": 3, "bulk": 2}
        assert lane_concurrency_from_env(
            {
                "TARGET_WORKER_INTERACTIVE_CONCURRENCY": "3",
                "TARGET_WORKER_BULK_CONCURRENCY": " 1 ",
            }
        ) == {"interactive": 3, "bulk": 1}
        with pytest.raises(ValueError):
            lane_concurrency_from_env({"TARGET_WORKER_BULK_CONCURRENCY": "two"})
        with pytest.raises(ValueError):
            lane_concurrency_from_env({"TARGET_WORKER_BULK_CONCURRENCY": "0"})

    def test_the_status_line_sums_k_loops_into_one_lane(self):
        from src.worker import status_line

        class _L:
            def __init__(self, lane, processed):
                self.lane = lane
                self.processed, self.parked, self.failures = processed, 0, 1
                self.fenced, self.exhausted = 0, 0

        class _H:
            beats, short_beats, consecutive_failures = 0, 0, 0

        line = status_line(
            loops=[_L("interactive", 3), _L("interactive", 4), _L("bulk", 1)],
            clock=None,
            heartbeat=_H,
        )
        assert "interactive[tasks=2 processed=7 parked=0 failures=2" in line
        assert "bulk[tasks=1 processed=1" in line
        assert "waits=0" in line


class TestTheUsagePrecheckIsWiredBehindItsFlag:
    """The advisory Meta usage pre-check ships behind a default-off flag.
    `TARGET_USAGE_PRECHECK_ENABLED` composes one `UsagePrecheck` (the 5-minute
    cache, shared across the process) into the pipeline's deps; absent or off,
    the seam stays None and no usage read is ever made
    (`test_l5_pipeline_gate.py` pins that end)."""

    ENV = {
        "CLOUDINARY_CLOUD_NAME": "c",
        "CLOUDINARY_API_KEY": "k",
        "CLOUDINARY_API_SECRET": "s",
    }

    def test_off_by_default_the_seam_is_none(self):
        app = compose(
            engine=object(),
            config=WorkerConfig(),
            env=dict(self.ENV),
            adapters={"gdrive": object()},
        )
        assert app.deps.precheck is None

    @pytest.mark.parametrize("value", ["0", "false", "no", "", "off"])
    def test_a_falsy_flag_keeps_it_off(self, value):
        env = {**self.ENV, "TARGET_USAGE_PRECHECK_ENABLED": value}
        app = compose(
            engine=object(),
            config=WorkerConfig(),
            env=env,
            adapters={"gdrive": object()},
        )
        assert app.deps.precheck is None

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_the_flag_composes_the_shared_cache_and_says_so(self, value, caplog):
        """A canary that is armed must be visible in the deploy log — the
        `worker up` line is identical either way."""
        import logging

        from src.services.target.usage_precheck import UsagePrecheck

        env = {**self.ENV, "TARGET_USAGE_PRECHECK_ENABLED": value}
        with caplog.at_level(logging.INFO, logger="target.worker"):
            app = compose(
                engine=object(),
                config=WorkerConfig(),
                env=env,
                adapters={"gdrive": object()},
            )
        assert isinstance(app.deps.precheck, UsagePrecheck)
        assert any("usage pre-check armed" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_the_pipeline_receives_the_composed_precheck(self, monkeypatch):
        """The registry's `publish_pipeline` executor passes `deps.precheck`
        through — the one seam the flag reaches."""
        from src.services.target import publish_pipeline, work_loop

        seen = {}

        async def run_publish_pipeline(job, **kw):
            seen.update(kw)
            return "posted"

        monkeypatch.setattr(
            publish_pipeline, "run_publish_pipeline", run_publish_pipeline
        )
        sentinel = object()
        deps = work_loop.WorkerDeps(
            meta=object(),
            transit=object(),
            media_fetch=lambda row: None,
            precheck=sentinel,
            engine=object(),
        )
        registry = work_loop.build_registry(deps)
        assert not isinstance(registry["publish_pipeline"], Parked)
        await registry["publish_pipeline"](
            None, {"id": "j", "kind": "publish_pipeline"}
        )
        assert seen["precheck"] is sentinel


def _documented_numbers(label: str, pattern: str) -> tuple:
    """The numbers *pattern* captures in the row of
    `tests/fixtures/design_rows/05-operational-numbers.md` that names *label*,
    commas stripped — failing with the row itself when its wording no longer
    matches."""
    import re
    from pathlib import Path

    doc = (
        Path(__file__).resolve().parents[2]
        / "tests/fixtures/design_rows/05-operational-numbers.md"
    ).read_text()
    row = next(line for line in doc.splitlines() if label in line)
    found = re.search(pattern, row)
    assert found, f"the {label!r} row no longer reads as {pattern!r}: {row}"
    return tuple(int(group.replace(",", "")) for group in found.groups())


def test_the_approval_ttl_default_is_the_documented_number():
    """The fixture's row is normative and the worker's default is pinned TO IT
    — not to a literal copied from it."""
    (minutes,) = _documented_numbers("Approval TTL default", r"\|\s*([\d,]+) min")
    assert WorkerConfig().approval_ttl_seconds == minutes * 60


def test_the_reaper_cadence_and_budget_are_the_documented_numbers():
    """Both halves of the fixture's reaper row are pinned TO IT: every
    expired lease waits for this beat — its serialization key stays held until
    the reaper re-readies it."""
    seconds, budget = _documented_numbers(
        "Reaper cadence + budget", r"\|\s*every (\d+) s, ([\d,]+) per sweep"
    )
    config = WorkerConfig()
    app = compose(engine=object(), config=config, env={})
    assert app.recurring["reap_expired"] == seconds
    assert config.reap_limit == budget


def test_the_rate_counter_retention_is_the_documented_numbers():
    """The fixture's `rate_counters` row (7 d) and its sweep's batch (5,000) and
    time budget (5 s) are pinned TO it, like the reaper's: the rows hold client
    addresses, so the keep window is a promise about how long one is kept."""
    (days,) = _documented_numbers("| `rate_counters` |", r"\|\s*(\d+) d\s*\|")
    (batch,) = _documented_numbers(
        "Retention sweep cadence", r"batches of ([\d,]+) per class"
    )
    (budget,) = _documented_numbers("Retention sweep cadence", r"or (\d+) s is spent")
    config = WorkerConfig()
    assert config.rate_counters_keep_seconds == days * 24 * 3600
    assert config.retention_batch == batch
    assert config.retention_budget_seconds == budget


def test_the_clock_mints_the_retention_sweep_every_hour():
    app = compose(engine=object(), config=WorkerConfig(), env={})
    assert app.recurring["retention_sweep"] == 3600.0


async def test_selected_prepare_runs_after_discovery_closes_and_isolates_failure():
    from src import worker
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    closed = []
    subjects = [
        {"workspace_id": "workspace-a", "intent_id": "pending-card"},
        {"workspace_id": "workspace-b", "intent_id": "scheduled"},
    ]

    class Result:
        def mappings(self):
            return self

        def all(self):
            return subjects

    class Session:
        async def execute(self, query, binds):
            assert "fn_selected_preparation_due" in str(query) and binds == {"lim": 2}
            return Result()

    @asynccontextmanager
    async def sessions(job):
        assert job == {}
        yield Session()
        closed.append(True)

    seen = []

    async def prepare(**kwargs):
        assert closed == [True]
        assert kwargs["engine"] is engine
        seen.append((kwargs["workspace_id"], kwargs["intent_id"]))
        if len(seen) == 1:
            raise RuntimeError("synthetic refusal")

    engine = object()
    app = SimpleNamespace(
        engine=engine,
        config=SimpleNamespace(prompt_sweep_limit=2),
        deps=SimpleNamespace(selected_prepare=prepare),
    )
    await worker.PromptSweeper(app).prepare_selected(sessions)
    assert seen == [("workspace-a", "pending-card"), ("workspace-b", "scheduled")]
