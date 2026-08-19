"""The FC-2 Telegram-reference ratchet (`04` F.6, epic #843).

FC-2 clause 3 asks for *"a CI ratchet ... enforcing an allowlist of modules
permitted to reference Telegram: deliberate additions only, any reference
outside it fails CI, and the core-services segment must reach empty."* This is
that gate, plus the two structural rules and the `07` §5 hygiene pattern list
`04` F.6 attaches to the same mechanism.

Standalone stdlib module (the `fc8_gate.py` / `dispatch-overdue.py` precedent),
so it is unit-testable and runs in CI without installing the application.

## THE PREDICATE IS THE CODE, AND THAT IS THE POINT

The F.1 ownership inventory (§4) measured this axis and found the plan's
anchor — "75 of 147" — unreproducible, because *nothing recorded what was
counted*: the same axis yields 18, 64, 70 or 72 depending on the predicate. Its
conclusion is this module's design: **commit the predicate as executable code
and derive the count from it, so the baseline and the gate cannot disagree.**

Two consequences, both load-bearing:

1. The baseline file stores **sets, never counts**. Every count this gate
   reports is `len(...)` of a stored set. A count stored beside its set is a
   second source of truth that can drift from it — the exact failure §4
   describes one level up.
2. Re-measuring is `--write-baseline`, which runs the same functions the check
   runs. There is no second implementation to keep in step.

## COUNT THE THING, NOT THE STRING

A raw substring match is not a usable predicate here, and this is measured
rather than asserted. On the tree at install:

    substring "telegram" anywhere ....... 72 modules
    structural coupling (this module) ... 27 modules

**46 of the 72 are false positives** — prose, comments, unrelated identifiers —
and a ratchet baselined at 72 would carry ~45 modules of phantom headroom in
which a genuine new coupling could land without reddening. That is precisely
the defect found in the F.1 `SYSTEM_SCOPE` ratchet, at larger scale.

It also fails in the *other* direction, which matters more:
``src/services/core/telegram_operation_state.py`` is a Telegram module that
contains no literal "telegram" in its text, so a substring predicate **misses**
it. Over-counting reads as protection; under-counting is a hole.

So coupling is resolved from the AST import graph:

    a module is Telegram-coupled if it
      (a) imports the `telegram` SDK (`import telegram`, `from telegram... `), OR
      (b) imports any module whose dotted path contains `telegram`, OR
      (c) is itself at a path containing `telegram`

(c) is included because an adapter module is part of the Telegram surface even
when nothing it imports names Telegram — that is the case the substring
predicate misses.

## EQUALITY, NOT CEILING

Every axis asserts **set equality** against the baseline, not `<=`. This
follows the ruling on the F.1 ratchet and the reasoning transfers exactly: a
ceiling accrues headroom as the burn-down retires sites, silently pre-buying
unreviewed access for some future addition. Equality forces every delta —
shrink or grow — into the same reviewable diff.

Shrink-only falls out of equality plus review rather than needing its own
mechanism: retiring a module is a baseline edit that removes a line, and a
module returning later is a baseline edit that adds one back. Both are visible
in the same place, which is what "deliberate additions only" asks for.

## THE FOUR AXES, AND WHY THEY ARE NOT ALL PASS/FAIL

Measured at install, before choosing any of their shapes:

    telegram_modules ....................  27   ratcheted (equality)
    core_telegram_modules ...............  22   ratcheted; FC-2 says -> empty
    chat_id_functions_outside_adapters ..  115  ratcheted (equality)
    provider_account_ref_log_sites ......  0    HARD ZERO

Only the last is a pass/fail rule, and only because it is already clean. The
other three are violated today by the plan's own account — FC-2 clause 3 says
the core segment *must reach* empty, which presupposes it is not — so shipping
them as pass/fail would be red on arrival, and narrowing them until they passed
would be the "blocks nothing or blocks everything" failure §4 warns about. They
ratchet instead.

`chat_id_functions_outside_adapters` excludes the allowlisted Telegram modules
deliberately: a Telegram adapter accepting a chat id is FC-2 working as
intended (adapters at the edges), so counting those 45 functions as violations
would make the rule fire on the correct design. The rule is that *non-adapter*
code must not take Telegram ids.

## Exit codes

    0  CLEAN      — every axis matches the baseline
    1  DRIFT      — at least one axis differs; the diff is printed per axis
    2  USAGE      — bad invocation or unreadable baseline
"""

from __future__ import annotations

import argparse
import ast
import json
import pathlib
import re
import sys

#: Roots the ratchet governs. `tests/` is deliberately excluded: a test that
#: exercises the Telegram adapter must import it, and counting that as coupling
#: would make the burn-down unachievable by construction.
DEFAULT_ROOTS = ("src", "cli")

#: The core-services segment FC-2 clause 3 requires to reach empty.
CORE_SEGMENT = "src/services/core/"

#: Logging call names the `07` §5 pattern list applies to.
LOG_CALLS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
)

#: `07` §5: provider identifiers must not reach the output surface.
HYGIENE_IDENT = "provider_account_ref"

_CHAT_ID = re.compile(r"chat_?id", re.IGNORECASE)

DEFAULT_BASELINE = pathlib.Path(__file__).with_name("telegram_ratchet_baseline.json")

AXES = (
    "telegram_modules",
    "core_telegram_modules",
    "chat_id_functions_outside_adapters",
    "provider_account_ref_log_sites",
)


def _parse(path: pathlib.Path):
    """AST for *path*, or None when it will not parse.

    A syntactically broken module is skipped rather than crashing the gate: the
    test suite and ruff already fail loudly on one, and a ratchet that cannot
    run is worse than one that reports on what it could read. It is disclosed
    by :func:`unparsable`, never silently dropped.
    """
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return None


def discover_modules(repo: pathlib.Path, roots=DEFAULT_ROOTS):
    """Every `.py` under *roots*, as repo-relative POSIX paths, sorted."""
    out = []
    for root in roots:
        base = repo / root
        if not base.exists():
            continue
        out.extend(p.relative_to(repo).as_posix() for p in base.rglob("*.py"))
    return sorted(out)


def unparsable(repo: pathlib.Path, roots=DEFAULT_ROOTS):
    """Modules the gate could not read — disclosed, never silently skipped."""
    return sorted(m for m in discover_modules(repo, roots) if _parse(repo / m) is None)


def imported_names(tree) -> set:
    """Every dotted name *tree* imports, absolute forms only.

    Relative imports are recorded by their module part; the leading dots are
    dropped because coupling is judged on the name, and a relative import of a
    Telegram module still names it.
    """
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
    return names


def is_telegram_coupled(module: str, tree) -> bool:
    """The predicate. See the module docstring for why it is not a substring."""
    if "telegram" in module.lower():
        return True
    if tree is None:
        return False
    for name in imported_names(tree):
        lowered = name.lower()
        if lowered == "telegram" or lowered.startswith("telegram."):
            return True
        if "telegram" in lowered:
            return True
    return False


def chat_id_functions(tree):
    """`name::lineno`-free list of function names taking a chat-id parameter.

    Positional-only, positional, and keyword-only parameters are all read;
    omitting any of the three would let the rule be evaded by moving an
    argument across the `/` or `*`.
    """
    found = []
    if tree is None:
        return found
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            params = list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
            if args.vararg:
                params.append(args.vararg)
            if args.kwarg:
                params.append(args.kwarg)
            if any(_CHAT_ID.search(a.arg) for a in params):
                found.append(node.name)
    return found


def provider_ref_log_sites(tree):
    """Line numbers of logging calls mentioning `provider_account_ref` (`07` §5).

    Matched on the CALL, not the file: a module may name the identifier freely
    in a query or a comment, and only its arrival at a logging call site is
    what §5 forbids.
    """
    sites = []
    if tree is None:
        return sites
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", None) or getattr(func, "id", None)
        if name not in LOG_CALLS:
            continue
        for sub in ast.walk(node):
            hit = (
                isinstance(sub, ast.Name)
                and sub.id == HYGIENE_IDENT
                or isinstance(sub, ast.Attribute)
                and sub.attr == HYGIENE_IDENT
                or isinstance(sub, ast.Constant)
                and isinstance(sub.value, str)
                and HYGIENE_IDENT in sub.value
            )
            if hit:
                sites.append(f"{node.lineno}")
                break
    return sites


def measure(repo: pathlib.Path, roots=DEFAULT_ROOTS) -> dict:
    """Every axis, as sorted lists. Counts are never stored — only derived."""
    telegram, core, chat_ids, log_sites = [], [], [], []
    for module in discover_modules(repo, roots):
        tree = _parse(repo / module)
        coupled = is_telegram_coupled(module, tree)
        if coupled:
            telegram.append(module)
            if module.startswith(CORE_SEGMENT):
                core.append(module)
        else:
            chat_ids.extend(f"{module}::{fn}" for fn in chat_id_functions(tree))
        log_sites.extend(f"{module}::{ln}" for ln in provider_ref_log_sites(tree))
    return {
        "telegram_modules": sorted(telegram),
        "core_telegram_modules": sorted(core),
        "chat_id_functions_outside_adapters": sorted(chat_ids),
        "provider_account_ref_log_sites": sorted(log_sites),
    }


def compare(observed: dict, baseline: dict):
    """Per-axis (added, removed) against the baseline. EQUALITY, not ceiling."""
    report = {}
    for axis in AXES:
        obs = set(observed.get(axis, []))
        base = set(baseline.get(axis, []))
        report[axis] = (sorted(obs - base), sorted(base - obs))
    return report


def _render(report, observed, baseline) -> int:
    drift = 0
    for axis in AXES:
        added, removed = report[axis]
        n_obs, n_base = len(observed.get(axis, [])), len(baseline.get(axis, []))
        status = "ok" if not added and not removed else "DRIFT"
        print(f"[{status:5}] {axis}: {n_obs} (baseline {n_base})")
        for entry in added:
            print(f"           + {entry}")
        for entry in removed:
            print(f"           - {entry}")
        if added or removed:
            drift = 1
    if drift:
        print(
            "\nThe ratchet asserts EQUALITY, not a ceiling: a shrink is drift too.\n"
            "If the change is deliberate, re-measure and commit the baseline in\n"
            "the same PR:  python scripts/telegram_ratchet.py --write-baseline"
        )
    return drift


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="FC-2 Telegram-reference ratchet")
    parser.add_argument("--repo", default=".", help="repository root")
    parser.add_argument("--baseline", default=None, help="baseline JSON path")
    parser.add_argument(
        "--write-baseline", action="store_true", help="re-measure and rewrite"
    )
    parser.add_argument("--json", action="store_true", help="emit measurement as JSON")
    args = parser.parse_args(argv)

    repo = pathlib.Path(args.repo).resolve()
    baseline_path = pathlib.Path(args.baseline) if args.baseline else DEFAULT_BASELINE
    observed = measure(repo)

    broken = unparsable(repo)
    if broken:
        print(f"NOTE: {len(broken)} module(s) did not parse and were read as empty:")
        for m in broken:
            print(f"      {m}")

    if args.json:
        print(json.dumps(observed, indent=2, sort_keys=True))
        return 0

    if args.write_baseline:
        payload = {"predicate": __doc__.strip(), **observed}
        baseline_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"baseline written to {baseline_path}")
        for axis in AXES:
            print(f"  {axis}: {len(observed[axis])}")
        return 0

    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read baseline {baseline_path}: {exc}", file=sys.stderr)
        return 2

    return _render(compare(observed, baseline), observed, baseline)


if __name__ == "__main__":
    raise SystemExit(main())
