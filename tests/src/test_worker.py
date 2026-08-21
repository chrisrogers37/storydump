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
