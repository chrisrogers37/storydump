"""The worker's deployed entrypoint: `python -m src.main` (the Procfile's
`worker` line) runs the target composition root, `src.worker`.

The legacy loops that ran here went with the legacy tier (the tear-out,
phase 01; #1216). The environment switch that chose between the two roots,
its contract module and the refusal of an unknown value went in phase 02:
there is one root and nothing to select, so this module is the dispatch and
nothing else. It stays (fork F2) because the Procfile and the never-run lists
name it, and a deletion PR would make no deploy-time change.
"""

# #942: the target composition root rides the deployed worker artifact.
# EAGER on purpose: a lazy import would make the worker's closure invisible to
# any import-graph measurement, and it is the closure that makes the artifact
# the artifact (#979; the reachability instrument that measured it was retired
# with the legacy tier's questions, #1216). The root's own config
# (TARGET_DATABASE_URL and friends) is read at RUN time inside src.worker.main,
# never at import (pinned in tests/src/test_worker_entrypoint.py).
import src.worker as target_worker


def main():
    target_worker.main()


if __name__ == "__main__":
    main()
