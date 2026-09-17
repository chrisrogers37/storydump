"""The worker-implementation gate contract (#942) — a side-effect-free leaf.

Which composition root `python -m src.main` (the Procfile `worker` line) runs
— historically: arming was the M.3 step-4 decision, an operator setting
WORKER_IMPL=target on the service (2026-08-24). Since the legacy tier's
deletion (#1216) there is one root and `src.main` runs it under either label;
this contract survives only to refuse an unknown value, until phase 02 of the
tear-out retires the variable and this module.

This lives OUTSIDE `src.main` because the contract has two consumers with
opposite import budgets: `src.main` enforces it (and imports the world
anyway), while `scripts/target_reachability.py` only LABELS with it — and a
label must not have to import the ~780-module legacy closure, with its
import-time settings floor, to read four strings. Stdlib-only on purpose;
anything heavier added here re-couples the instrument to what it measures.

Values are matched EXACTLY: any other spelling refuses at boot rather than
guessing, because an operator who typo'd the variable must get a crash loop
they notice, never a worker booting under a value nobody meant.
"""

import sys

WORKER_IMPL_VAR = "WORKER_IMPL"
WORKER_IMPL_LEGACY = "legacy"
WORKER_IMPL_TARGET = "target"
WORKER_IMPLS = (WORKER_IMPL_LEGACY, WORKER_IMPL_TARGET)


def resolve_worker_impl(env) -> str:
    """Decide which root serves, from a mapping of env vars.

    Unset resolves to the legacy LABEL. Since the tear-out (phase 01; #1216)
    `src.main` runs the target root under either label — the legacy loops are
    gone — and this resolver's one remaining job is to refuse an unknown value
    (phase 02 retires the variable and this module). Present-but-empty is NOT
    unset: it is a half-typed arm, and it refuses like any other unknown value.
    """
    raw = env.get(WORKER_IMPL_VAR)
    if raw is None:
        return WORKER_IMPL_LEGACY
    if raw in WORKER_IMPLS:
        return raw
    print(
        f"FATAL: {WORKER_IMPL_VAR}={raw!r} is not a worker implementation. "
        f"Valid values, matched exactly: {WORKER_IMPL_LEGACY!r} (the default "
        f"when unset) or {WORKER_IMPL_TARGET!r}. Refusing to boot under a "
        f"value nobody meant.",
        file=sys.stderr,
    )
    raise SystemExit(2)
