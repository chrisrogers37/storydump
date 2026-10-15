"""The load harness is opt-in (`RUN_LOAD_HARNESS=1`). Without it the scenarios
are DESELECTED at collection, not skipped: the suite's skip ceiling
(`tests/conftest.py`, `MAX_EXPECTED_SKIPS`) exists to catch a swallowed
database, and five silently skipped scenarios on every CI run would read as
exactly that. Deselection keeps the ceiling honest and the harness one
environment variable away."""

from __future__ import annotations

import os


def pytest_collection_modifyitems(config, items):
    if os.environ.get("RUN_LOAD_HARNESS") == "1":
        return
    load, keep = [], []
    for item in items:
        (load if item.get_closest_marker("load") else keep).append(item)
    if load:
        items[:] = keep
        config.hook.pytest_deselected(items=load)
