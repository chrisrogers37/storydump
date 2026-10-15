"""The worker's deployed entrypoint: `python -m src.main` (the Procfile's
`worker` line) runs the target composition root, `src.worker`.

There is one root and nothing to select (#1216), so this module is the
dispatch and nothing else. It stays because the Procfile names it.
"""

# #942: the target composition root rides the deployed worker artifact.
# EAGER on purpose: a lazy import would make the worker's closure invisible to
# any import-graph measurement, and it is the closure that makes the artifact
# the artifact (#979, #1216). The root's own config
# (TARGET_DATABASE_URL and friends) is read at RUN time inside src.worker.main,
# never at import (pinned in tests/src/test_worker_entrypoint.py).
import src.worker as target_worker


def main():
    target_worker.main()


if __name__ == "__main__":
    main()
