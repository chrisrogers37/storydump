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
