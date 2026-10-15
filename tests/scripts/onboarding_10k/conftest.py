"""The 10,000-post onboarding fixture is opt-in (`RUN_ONBOARDING_10K=1`).

It takes about eleven hundred job turns, each landing atomically and
re-reading the whole index, so minutes rather than seconds. Without the variable
its tests are DESELECTED at collection, not skipped: the suite's skip ceiling
(`tests/conftest.py`, `MAX_EXPECTED_SKIPS`) exists to catch a swallowed
database, and two silently skipped tests on every CI run would read as exactly
that. The same pattern as the load harness (`tests/scripts/load/conftest.py`).
"""

from __future__ import annotations

import os
from pathlib import Path

HERE = Path(__file__).parent


def pytest_collection_modifyitems(config, items):
    if os.environ.get("RUN_ONBOARDING_10K") == "1":
        return
    big, keep = [], []
    for item in items:
        (big if HERE in Path(str(item.path)).parents else keep).append(item)
    if big:
        items[:] = keep
        config.hook.pytest_deselected(items=big)
