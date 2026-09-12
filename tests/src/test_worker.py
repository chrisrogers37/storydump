"""W1 — the composition root's object graph (#942): what `python -m src.worker`
assembles, tested without connecting anything.

The compose() seam exists exactly so this is checkable: the W1 deployment's
live kinds, the clock's recurring set staying inside them (a clock that mints
work the registry parks would manufacture parked jobs on its own cadence),
and the heartbeat/lease numbers agreeing.
"""

import pytest

from src.services.target.work_loop import _UNBUILT_REASON, Parked, WorkerConfig
from src.worker import compose


def test_w1_composition_parks_only_for_a_named_reason():
    """A kind is dead in a bare composition ONLY because something is missing
    and SAYS SO — never silently.

    This asserted a hardcoded set of five live kinds until #1083. A set literal
    pins the CURRENT state as correct: it fails the moment anyone adds a job
    kind, passes again the moment they paste the name in, and never once tests
    the property the comment above it describes. It had already been updated
    that way before, and the same literal exists twice more in
    `test_work_loop.py` — which is how one PR can patch one copy and be red on
    another.

    So the expectation is DERIVED from the registry's own park reasons instead:

    - `_UNBUILT_REASON` marks a kind with no executor anywhere.
    - any OTHER reason marks a seam this deployment did not wire.

    The invariant is that those two sets account for EVERY difference between a
    bare composition and a fully-configured one. A new seam-free kind (#1083's
    `revoke_workspace_credentials` is one) changes both sides equally and this
    test does not move. A kind that starts parking for no stated reason does.

    What it deliberately no longer pins: WHICH kinds are live. That was the
    snapshot, and a snapshot is what made this a maintenance tax rather than a
    gate.
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
        from src.services.target.unit_of_work import engine_url_from_env

        url = engine_url_from_env({"TARGET_DATABASE_URL": "postgresql://u:p@h/db"})
        assert url == "postgresql+asyncpg://u:p@h/db"

    def test_libpq_ssl_params_are_rewritten_for_asyncpg(self):
        from src.services.target.unit_of_work import engine_url_from_env

        url = engine_url_from_env(
            {
                "TARGET_DATABASE_URL": "postgresql://u:p@h/db?sslmode=require&channel_binding=require"
            }
        )
        assert "sslmode" not in url and "channel_binding" not in url
        assert url.endswith("?ssl=require")

    def test_absent_env_returns_none_so_settings_decide(self):
        from src.services.target.unit_of_work import engine_url_from_env

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

    async def test_a_token_for_the_wrong_bot_parks_the_channel_loudly(self, caplog):
        """The 2026-09-10 crosswire: the worker sent cards as @storydumpapp_bot
        while the API's webhook listened on @storydump_app_bot — every tap went
        where nothing listened. With the configured bot known, a mismatched
        token parks the sender and says why."""
        from src.services.target.work_loop import Parked
        from src.worker import apply_transport_probe

        class _Other:
            def for_chat(self, ref):  # pragma: no cover - never bound
                raise AssertionError

            async def probe(self):
                return "storydumpapp_bot"

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
        assert "WRONG BOT" in entry.reason and "storydumpapp_bot" in entry.reason
        assert "storydump_app_bot" in entry.reason
        assert any("configured bot" in r.message for r in caplog.records)
        assert app.bot_username == "storydumpapp_bot"

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


class TestPromptSweeperConsumesTheSweep:
    """The sweep's count vocabulary is the sweeper's contract. #1033 retired
    the `failed_no_surface` leg (the web queue is a surface every workspace
    has, so "no reachable surface" cannot occur); a sweeper still reaching
    for that key would raise INSIDE its own `except Exception` — the sweep
    itself committed, the counters before the bad read still moved, and the
    only symptom is a "prompt sweep failed" line on every cadence. So the
    assertion that carries this test is the log staying silent."""

    async def test_one_sweep_moves_exactly_the_counts_the_sweep_returned(
        self, monkeypatch, caplog
    ):
        import asyncio
        import logging

        from src import worker

        class _Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def begin(self):
                return self

        monkeypatch.setattr(
            worker, "async_sessionmaker", lambda engine, **kw: lambda: _Session()
        )

        async def fake_gucs(session, **kw):
            pass

        monkeypatch.setattr(worker.unit_of_work, "apply_gucs", fake_gucs)

        stop = asyncio.Event()

        async def fake_sweep(session, *, limit):
            stop.set()  # one iteration, then the loop sees the stop
            return {"prompted": 2, "advanced": 1}  # the whole vocabulary since #1033

        monkeypatch.setattr(worker.prompts_mod, "sweep_due_prompts", fake_sweep)

        class _Config:
            prompt_sweep_seconds = 0.01

        class _App:
            engine = object()
            config = _Config()

        sweeper = worker.PromptSweeper(_App())
        with caplog.at_level(logging.ERROR, logger="target.worker"):
            await sweeper.run(stop)

        assert (sweeper.sweeps, sweeper.prompted, sweeper.advanced) == (1, 2, 1)
        assert "prompt sweep failed" not in caplog.text


class TestThePublishLegIsWired:
    """#1220 step 3: with a Drive adapter and the Cloudinary trio the
    publish_pipeline kind is LIVE; without Drive it parks naming media_fetch;
    the Graph adapter is always built and the fetch reads the intent row."""

    def test_drive_and_cloudinary_bring_publish_pipeline_live(self):
        from src.services.target.work_loop import Parked

        env = {
            "CLOUDINARY_CLOUD_NAME": "c",
            "CLOUDINARY_API_KEY": "k",
            "CLOUDINARY_API_SECRET": "s",
        }
        app = compose(engine=object(), config=WorkerConfig(), env=env, drive=object())
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
            async def fetch_bytes(self, **kw):
                seen.update(kw)
                return b"jpeg-bytes", "f.jpg", "image/jpeg"

        fetch = _publish_media_fetch(_Drive())
        intent = {
            "source_id": "src-1",
            "workspace_id": "ws-1",
            "provider_file_ref": "ref-1",
            "media_kind": "video",
        }
        assert await fetch(intent) == b"jpeg-bytes"
        assert seen == {
            "source_id": "src-1",
            "workspace_id": "ws-1",
            "file_ref": "ref-1",
            "max_bytes": PUBLISH_MAX_BYTES["video"],
        }


class TestTheReconcilerPollIsWired:
    """#1220 step 3: production no longer runs `poll=None` — the ambiguous
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
        assert await poll(intent_id="i-1") == "PUBLISHED"
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
        assert await none(intent_id="i-1") is None
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
        assert await dead(intent_id="i-1") is None


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
    """Phase 3a step 6: the status line carries the queue's depth and age
    per lane, the outbox backlog and the pacing state — `01:88`'s
    'visible backpressure', from one read the reporter takes itself."""

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
    """Phase 3b: K loops per lane on pooled checkouts, from the config, with
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
    """`02` §8 / `05` §8: the advisory Meta usage pre-check ships behind a
    default-off flag the S.5 canary flips. Before 2026-09-12 nothing in
    production composed it — the pipeline's `precheck` seam always received
    None — so the flag had nothing to flip. `TARGET_USAGE_PRECHECK_ENABLED`
    now composes one `UsagePrecheck` (the 5-minute cache, shared across the
    process) into the pipeline's deps; absent or off, the seam stays None and
    no usage read is ever made (the l5 gate pins that end)."""

    ENV = {
        "CLOUDINARY_CLOUD_NAME": "c",
        "CLOUDINARY_API_KEY": "k",
        "CLOUDINARY_API_SECRET": "s",
    }

    def test_off_by_default_the_seam_is_none(self):
        app = compose(
            engine=object(), config=WorkerConfig(), env=dict(self.ENV), drive=object()
        )
        assert app.deps.precheck is None

    @pytest.mark.parametrize("value", ["0", "false", "no", "", "off"])
    def test_a_falsy_flag_keeps_it_off(self, value):
        env = {**self.ENV, "TARGET_USAGE_PRECHECK_ENABLED": value}
        app = compose(engine=object(), config=WorkerConfig(), env=env, drive=object())
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
                engine=object(), config=WorkerConfig(), env=env, drive=object()
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


def test_the_approval_ttl_default_is_the_documented_number():
    """`05`'s row is normative and the worker's default is pinned TO IT — not
    to a literal copied from it (the first slice shipped 72 h against a doc
    that said 24, and three-day-old cards were live on 2026-09-12)."""
    import re
    from pathlib import Path

    doc = (
        Path(__file__).resolve().parents[2]
        / "documentation/planning/2026-08-02-consolidated-design-plan/05-operational-numbers.md"
    ).read_text()
    row = next(line for line in doc.splitlines() if "Approval TTL default" in line)
    minutes = int(re.search(r"\|\s*([\d,]+) min", row).group(1).replace(",", ""))
    assert WorkerConfig().approval_ttl_seconds == minutes * 60
