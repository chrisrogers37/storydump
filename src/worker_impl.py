"""The worker-implementation gate contract (#942) — a side-effect-free leaf.

Which composition root `python -m src.main` (the Procfile `worker` line) runs.
The Procfile never changes; arming is the M.3 step-4 decision, made by an
operator setting WORKER_IMPL=target on the service — a config flip with
instant rollback, the same dormant-until-armed shape as the migration
runner's preDeployCommand (railway.toml).

This lives OUTSIDE `src.main` because the contract has two consumers with
opposite import budgets: `src.main` enforces it (and imports the world
anyway), while `scripts/target_reachability.py` only LABELS with it — and a
label must not have to import the ~780-module legacy closure, with its
import-time settings floor, to read four strings. Stdlib-only on purpose;
anything heavier added here re-couples the instrument to what it measures.

Values are matched EXACTLY: any other spelling refuses at boot rather than
guessing, because an operator who typo'd the arm must get a crash loop they
notice, never a legacy worker they believe is the target.
"""

import sys

WORKER_IMPL_VAR = "WORKER_IMPL"
WORKER_IMPL_LEGACY = "legacy"
WORKER_IMPL_TARGET = "target"
WORKER_IMPLS = (WORKER_IMPL_LEGACY, WORKER_IMPL_TARGET)


def resolve_worker_impl(env) -> str:
    """Decide which root serves, from a mapping of env vars.

    Unset selects legacy — the default must keep every existing deploy
    byte-identical in behavior. Present-but-empty is NOT unset: it is a
    half-typed arm, and it refuses like any other unknown value.
    """
    raw = env.get(WORKER_IMPL_VAR)
    if raw is None:
        return WORKER_IMPL_LEGACY
    if raw in WORKER_IMPLS:
        return raw
    print(
        f"FATAL: {WORKER_IMPL_VAR}={raw!r} is not a worker implementation. "
        f"Valid values, matched exactly: {WORKER_IMPL_LEGACY!r} (the default "
        f"when unset) or {WORKER_IMPL_TARGET!r}. Refusing to guess which "
        f"worker should serve.",
        file=sys.stderr,
    )
    raise SystemExit(2)
