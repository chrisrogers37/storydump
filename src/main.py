"""The worker's deployed entrypoint: `python -m src.main` (the Procfile's
`worker` line) dispatches to the target composition root, `src.worker`.

The legacy loops that ran here went with the legacy tier (the tear-out,
phase 01; #1216) — nothing deployed had run them since `WORKER_IMPL=target`
was set on 2026-08-24. The module stays (fork F2): the Procfile and the
never-run lists name it, and a deletion PR makes no deploy-time change.

`WORKER_IMPL` is still READ, for one reason: an unknown value refuses loudly
(exit 2) rather than boot anything — an operator who typo'd the arm must not
get a worker they believe is something else. The variable, the contract
module `src.worker_impl` and this read retire together in phase 02.
"""

import os

# #942: the target composition root rides the deployed worker artifact.
# EAGER on purpose, and load-bearing: the import closure is how reachability
# is measured (scripts/target_reachability.py), and a lazy import is invisible
# to it (#979). The root's own config (TARGET_DATABASE_URL and friends) is
# read at RUN time inside src.worker.main, never at import (pinned in
# tests/src/test_worker_impl_gate.py).
import src.worker as target_worker
from src.utils.logger import logger
from src.worker_impl import WORKER_IMPL_TARGET, WORKER_IMPL_VAR, resolve_worker_impl


def main():
    """Refuse a garbage `WORKER_IMPL`; run the target root."""
    impl = resolve_worker_impl(os.environ)
    if impl != WORKER_IMPL_TARGET:
        logger.info(
            f"{WORKER_IMPL_VAR}={impl!r}: the legacy tier is gone (#1216);"
            " the target composition root (src.worker) runs"
        )
    target_worker.main()


if __name__ == "__main__":
    main()
