"""#942: can any DEPLOYED process reach the target tier?

The cutover blocker's measurement, as a script rather than as prose. It was
prose, it got re-run by hand two days later, and the hand-run silently
measured a different repository -- see `assert_root` for why that is now
impossible.

## What it measures

1. **The deployed axis.** Each `Procfile` entrypoint is imported and its module
   closure inspected for `src.services.target.*` / `src.models.target.*`. This
   is the load-bearing claim: a target module in a deployed closure means a
   deployed process *could* execute target code.
2. **The root axis.** `src.worker` is measured the same way even though no
   Procfile line runs it, because "the root exists but is not deployed" and
   "the root does not exist" are different states with different remaining
   work, and the deployed axis reports both as zero.
3. **The FC-7.3 parity bar.** Public callables DEFINED in the target tier
   (filtered on `__module__`, so an imported name does not count) enumerated,
   and the 14-item vocabulary tested against them.

## Import-reachable is not execution-reachable

A module in a closure proves it *can be imported*, never that anything calls
it. This script does not and cannot establish a call path -- it establishes the
weaker claim, and the weaker claim is sufficient for the issue because it comes
back EMPTY: nothing can be called if nothing is even imported. If a target
module ever appears in a deployed closure, that is the moment this instrument
stops being sufficient and someone has to go find the call site by hand.

## The parity matcher is a FORM matcher and is labelled as one

Item detection is substring-over-identifier. That matches a name shape, not a
capability -- `IntentNotApproved` matches "approve" and is not an approve
command. Raw hits are printed WITH their evidence so the reader adjudicates;
the script prints no parity headline count, deliberately, because the headline
is the part that would get quoted without the evidence.

## Running it

    python -m scripts.target_reachability

## Exit codes

    0  measurement ran
    2  the root assertion failed -- a DIFFERENT src was on sys.path
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import pathlib
import pkgutil
import sys

OK, ERROR = 0, 2

TARGET_PREFIXES = ("src.services.target", "src.models.target")

# The 14 FC-7.3 items (04-execution-sequence.md L322) and the identifier
# substrings that would EVIDENCE each. Deliberately generous: a false hit is
# visible in the printed evidence, a false miss is not.
PARITY_ITEMS = [
    ("approve", ["approve"]),
    ("skip", ["skip"]),
    ("reject", ["reject"]),
    ("mark_posted", ["mark_posted", "markposted"]),
    ("cancel", ["cancel"]),
    ("sync_now", ["sync_now", "syncnow"]),
    ("settings_change", ["settings_change", "settings"]),
    ("pause/resume", ["pause", "resume"]),
    ("prompts", ["prompt"]),
    ("notifications", ["notification", "notify"]),
    ("media sync", ["media_sync", "sync_media"]),
    ("scheduling", ["schedul", "plan_slot", "next_slot"]),
    ("manual mode", ["manual"]),
    ("API publish path", ["publish"]),
]


def assert_root(root: pathlib.Path) -> pathlib.Path:
    """Refuse unless `src` resolves under the root we were told to measure.

    An editable install of a SECOND storydump checkout was on sys.path here.
    A run launched from outside the repo imported that one instead, and
    returned byte-identical numbers for two different commits -- a wrong
    answer that reads as 'no change', which is the direction nobody queries.
    """
    sys.path.insert(0, str(root))
    import src

    resolved = pathlib.Path(src.__file__).resolve()
    if root not in resolved.parents:
        raise RuntimeError(
            f"refusing: `src` resolved to {resolved.parent}, which is not under "
            f"{root}. Another checkout is shadowing this one on sys.path."
        )
    return resolved


def procfile_entrypoints(root: pathlib.Path) -> list[tuple[str, str]]:
    """(process name, importable module) for each Procfile line.

    Parsed rather than hardcoded: a third process added to the Procfile is
    exactly the event that would make a hardcoded pair silently incomplete.
    """
    out = []
    for line in (root / "Procfile").read_text().splitlines():
        if ":" not in line:
            continue
        name, cmd = line.split(":", 1)
        for tok in cmd.split():
            if tok.startswith("src.") or tok.startswith("src/"):
                out.append((name.strip(), tok.split(":")[0].replace("/", ".")))
                break
    return out


def target_modules_on_disk(root: pathlib.Path) -> set[str]:
    found = set()
    for sub in ("services/target", "models/target"):
        d = root / "src" / sub
        if not d.is_dir():
            continue
        for f in d.rglob("*.py"):
            name = ".".join(f.relative_to(root).with_suffix("").parts)
            found.add(name[: -len(".__init__")] if name.endswith(".__init__") else name)
    return found


#: The positive and negative controls from the manual #942 run, ported rather
#: than reinvented. They pin OPPOSITE failure directions and that is the point:
#: `POSITIVE` catches a walker that has stopped matching, `NEGATIVE` catches one
#: that has started matching everything. The second is not hypothetical — before
#: the `(after - before)` fix below, a target module imported by an EARLIER
#: entrypoint was attributed to every later one, so `src.main` measured after
#: `src.worker` reported 14 target hits while importing none of them.
CONTROL_POSITIVE = "src.config.settings"
CONTROL_NEGATIVE = "src.services.target.__NONEXISTENT__"


def closure_for(entry: str) -> tuple[int, set[str], bool]:
    """Modules `entry` adds to this process, and which of them are target tier.

    METHOD — SEQUENTIAL, ONE PROCESS, and it decides the answer, so it is
    stated here rather than left to the reader. Every entrypoint is imported
    into the SAME interpreter in Procfile order, so what is measured is what
    each one adds GIVEN WHAT CAME BEFORE IT. A fresh process per entrypoint
    answers a different question and returns different numbers — measured on
    `92a92e6`, `src.api.app` is +381 sequentially and +1155 fresh, a 3x swing
    on one commit. #942 §1 was retracted over exactly this distinction.

    HITS ARE DIFFERENTIAL, and must stay that way. They are computed from
    `after - before`, never from `after`: the latter attributes an earlier
    call's target modules to every later one, which fails toward "the deployed
    entrypoint reaches target code" — the direction nobody re-checks, because
    it is the answer everyone wants.
    """
    before = set(sys.modules)
    importlib.import_module(entry)
    after = set(sys.modules)
    new = after - before
    hits = {n for n in new if n.startswith(TARGET_PREFIXES)}
    # The positive control is CUMULATIVE on purpose, and the asymmetry with
    # `hits` is the whole subtlety. "settings is in this closure" is a question
    # about `after`; against `new` it would go false for every entrypoint after
    # the first, since settings is imported once per process — a control that
    # fails on correct behaviour trains its reader to ignore it.
    return len(new), hits, CONTROL_POSITIVE in after


def target_callables(root: pathlib.Path) -> list[str]:
    import src.services.target as tgt

    flat = []
    for _, name, _ in pkgutil.walk_packages(
        tgt.__path__, prefix="src.services.target."
    ):
        try:
            m = importlib.import_module(name)
        except Exception:  # noqa: BLE001 — a module that will not import defines nothing reachable
            continue
        for attr, obj in vars(m).items():
            if attr.startswith("_"):
                continue
            if not (inspect.isfunction(obj) or inspect.isclass(obj)):
                continue
            if getattr(obj, "__module__", None) != name:  # defined here, not imported
                continue
            flat.append(f"{name}.{attr}")
    return sorted(flat)


def parity(callables: list[str]) -> dict:
    low = [c.lower() for c in callables]
    return {
        label: sorted(
            {callables[i] for i, c in enumerate(low) if any(n in c for n in needles)}
        )
        for label, needles in PARITY_ITEMS
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--root", default=None, help="repo root (default: this file's repo)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    args = p.parse_args(argv)

    root = pathlib.Path(
        args.root or pathlib.Path(__file__).resolve().parent.parent
    ).resolve()

    try:
        resolved = assert_root(root)
    except RuntimeError as exc:
        print(f"target_reachability ERROR: {exc}", file=sys.stderr)
        return ERROR

    on_disk = target_modules_on_disk(root)
    controls_positive: list[tuple[str, bool]] = []
    deployed = {}
    for proc, mod in procfile_entrypoints(root):
        size, hits, pos_ok = closure_for(mod)
        controls_positive.append((proc, pos_ok))
        deployed[proc] = {
            "module": mod,
            "closure_new": size,
            "target_hits": sorted(hits),
        }

    # A missing root is the FINDING ("no composition root exists"), not an
    # error -- this instrument has to run on commits that predate W1, or it
    # cannot produce the before/after it exists to produce.
    try:
        root_size, root_hits, pos_ok = closure_for("src.worker")
        controls_positive.append(("src.worker", pos_ok))
        root_present = True
    except ModuleNotFoundError:
        root_size, root_hits, root_present = 0, set(), False
    callables = target_callables(root)
    ev = parity(callables)

    # CONTROLS, run rather than asserted in prose. They are reported in both
    # output modes and set the exit code, because a control nobody can see the
    # result of is decoration.
    all_hits = set(root_hits)
    for d in deployed.values():
        all_hits |= set(d["target_hits"])
    controls = {
        "positive": {
            "probe": CONTROL_POSITIVE,
            "per_closure": dict(controls_positive),
            "ok": all(ok for _, ok in controls_positive) and bool(controls_positive),
        },
        "negative": {
            "probe": CONTROL_NEGATIVE,
            "ok": CONTROL_NEGATIVE not in all_hits
            and not any(h == CONTROL_NEGATIVE for h in all_hits),
        },
    }
    controls_ok = controls["positive"]["ok"] and controls["negative"]["ok"]

    if args.json:
        print(
            json.dumps(
                {
                    "root": str(root),
                    "src_from": str(resolved.parent),
                    "target_on_disk": sorted(on_disk),
                    "deployed": deployed,
                    "src_worker": {
                        "present": root_present,
                        "closure_new": root_size,
                        "target_hits": sorted(root_hits),
                    },
                    "target_callables": len(callables),
                    "parity_evidence": ev,
                    "controls": controls,
                    "closure_new_is_comparable": False,
                },
                indent=2,
            )
        )
        return OK if controls_ok else ERROR

    print("=" * 78)
    print("#942 TARGET REACHABILITY")
    print("=" * 78)
    print(f"  root        {root}")
    print(f"  src from    {resolved.parent}   (asserted under root)")
    print(f"  target modules on disk: {len(on_disk)}")

    print("\nDEPLOYED ENTRYPOINTS (from Procfile)\n" + "-" * 78)
    for proc, d in deployed.items():
        print(
            f"  {proc:8s} {d['module']:16s} closure +{d['closure_new']:5d}   "
            f"target modules reached: {len(d['target_hits'])}"
        )

    print("\nTHE TARGET ROOT (src.worker — NOT a Procfile entrypoint)\n" + "-" * 78)
    if not root_present:
        print("  src.worker does not exist at this commit — no composition root.")
    else:
        print(
            f"  closure +{root_size}   target modules reached: "
            f"{len(root_hits)} of {len(on_disk)}"
        )
        for m in sorted(root_hits):
            print(f"      + {m}")
        for m in sorted(on_disk - root_hits):
            print(f"      - {m}")

    print(
        f"\nFC-7.3 PARITY EVIDENCE — {len(callables)} public callables defined "
        f"in the tier\n" + "-" * 78
    )
    print("  Substring-over-identifier. A hit is EVIDENCE, not a verdict —")
    print("  `IntentNotApproved` matches 'approve' and is not an approve command.\n")
    for label, hits in ev.items():
        short = [h.replace("src.services.target.", "") for h in hits[:3]]
        print(f"  {label:18s} {'(none)' if not hits else ', '.join(short)[:52]}")

    print("\nCONTROLS\n" + "-" * 78)
    pos = controls["positive"]
    print(
        f"  positive  {pos['probe']:38s} "
        f"{'PASS' if pos['ok'] else 'FAIL'}  (present in every closure)"
    )
    for name, ok in pos["per_closure"].items():
        print(f"              {name:36s} {'yes' if ok else 'NO'}")
    neg = controls["negative"]
    print(
        f"  negative  {neg['probe']:38s} "
        f"{'PASS' if neg['ok'] else 'FAIL'}  (never matches)"
    )
    if not controls_ok:
        print("\n  *** A CONTROL FAILED — the numbers above are not evidence. ***")

    print("\n" + "=" * 78)
    print("Import-reachable is not execution-reachable. A module in a closure")
    print("can be imported; nothing here proves it is ever called.")
    print()
    print("`closure +N` is NOT COMPARABLE ACROSS RUNS and must not be quoted")
    print("against a figure from another environment. It counts every module")
    print("including stdlib and third-party, so it moves with the interpreter")
    print("and the installed set; and it is measured sequentially, so it also")
    print("moves with Procfile order. Only the `target modules reached` counts")
    print("and the module NAMES transfer between runs.")
    print("=" * 78)
    return OK if controls_ok else ERROR


if __name__ == "__main__":
    raise SystemExit(main())
