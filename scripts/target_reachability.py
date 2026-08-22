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

## A non-zero DEPLOYED figure is labelled by the script, not by a reader

The deployed axis moving off zero reads as this blocker clearing, and it moves
in the flattering direction, so it is the reading nobody re-checks. When any
entrypoint reports a non-zero count the script prints the bound in the same
block as the figure -- importable-not-serving, which entrypoints are still
zero, and what would actually clear the blocker. A caveat kept in prose
elsewhere degrades the moment someone re-derives the number; a label the print
path emits cannot be separated from it.

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
import re
import pkgutil
import subprocess
import sysconfig
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


def assert_provenance(root: pathlib.Path, modules: dict | None = None) -> None:
    """Refuse unless every `src` module ACTUALLY LOADED came from `root`.

    `assert_root` checks the package ANCHOR before any measuring happens; this
    checks the MEASUREMENT after it. They are different objects, and the escape
    lives at the second one: an editable-install finder satisfies the anchor
    from the measured tree's own `src/__init__.py`, then backfills SUBMODULES
    from the checkout that owns the `.pth`. Measured with `-S -E` removed --
    a silent wrong answer (483 modules, 19 target hits, read from the real
    repo) with the anchor assertion passing. A backstop that cannot see the one
    demonstrated escape is not a backstop.

    Containment is `pathlib`, never a string prefix. `/x/root-two/src/w.py`
    starts with `/x/root`, so a prefix test is satisfied by a SIBLING checkout
    -- which is the precise thing that produced the original wrong measurement.
    Same predicate as `assert_root`, not a second copy of it.

    BOUND, stated rather than left to be found: this sees FILE-BACKED modules.
    A namespace package carries no `__file__` and is invisible to it. `src` is
    a regular package -- `assert_root` reads `src.__file__` -- so the escape
    under guard is in scope, but a future `src` without an `__init__.py` would
    leave this quiet.
    """
    mods = sys.modules if modules is None else modules
    root = pathlib.Path(root).resolve()
    bad = []
    # sorted() snapshots: iterating sys.modules live raises if an import
    # mutates it, and a deterministic order keeps the message diffable.
    for name in sorted(mods):
        if name != "src" and not name.startswith("src."):
            continue
        origin = getattr(mods[name], "__file__", None)
        if origin is None:
            continue
        resolved = pathlib.Path(origin).resolve()
        if root not in resolved.parents:
            bad.append(f"{name} <- {resolved.parent}")
    if bad:
        raise RuntimeError(
            f"refusing: {len(bad)} loaded module(s) resolved outside {root}: "
            f"{bad[:5]} -- the measurement read another checkout."
        )


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


#: Two controls pinning OPPOSITE failure directions: `POSITIVE` catches a walker
#: that has stopped matching, `NEGATIVE` catches one that has started matching
#: everything.
#:
#: THE NEGATIVE PROBE IS A DELIBERATE DIVERGENCE FROM THE MANUAL #942 RUN, and
#: is documented here so nobody later "restores" the original as a tidy-up. That
#: run used a FABRICATED module (`src.services.target.__NONEXISTENT__`). A probe
#: for something that cannot exist can never enter `sys.modules`, so it can never
#: enter `hits` under ANY defect that derives hits from `sys.modules` — including
#: the one this script was fixed for, and including the very failure the control
#: claims to catch. Measured on the running instrument: mutate the walker to
#: match everything and it reports 780 garbage hits, exit 0, negative control
#: `ok: true`. **A control whose probe cannot exist is unfalsifiable — it is a
#: sentence, not a control.**
#:
#: So the negative is a SELECTIVITY probe instead: a module that genuinely IS
#: present in every closure but is NOT target tier. Under match-everything it
#: lands in hits and the control goes red. It is the same module as the positive
#: probe on purpose — one module, two opposite questions: "does the walker see
#: real modules at all" (it must be in the closure) and "does the walker
#: discriminate" (it must NOT be in target hits).
CONTROL_POSITIVE = "src.config.settings"
CONTROL_NEGATIVE = "src.config.settings"


def closure_for(entry: str) -> tuple[int, set[str], bool]:
    """Modules `entry` adds to this process, and which of them are target tier.

    IN-PROCESS PRIMITIVE. Callers do not use this directly — `measure()` runs
    it in a FRESH INTERPRETER per entrypoint (#986). Kept as its own function
    because it is what executes inside that subprocess, and because the #972
    pin is written against it.

    METHOD — one entrypoint per interpreter, so what is measured is that
    entrypoint's OWN closure and nothing else. It used to run every entrypoint
    into the same process in Procfile order, which made every number a function
    of measurement order: the same module measured twice in one process gives
    19 target hits and then 0 (measured, #986). That under-reports ANY
    entrypoint whose target modules something earlier imported — the root was
    merely where it surfaced, being measured last and reaching the most.

    Isolation removes the SHARING, not the subtraction. Both survive: see
    below.

    `closure_new` is still not comparable across runs — it counts stdlib and
    third-party, so it moves with the interpreter and the installed set. Only
    the target-reach counts and the module NAMES transfer.

    HITS ARE DIFFERENTIAL, and must stay that way even under isolation. They
    are computed from `after - before`, never from `after`: the latter
    attributes anything already imported to the entrypoint under measurement,
    which fails toward "the deployed entrypoint reaches target code" — the
    direction nobody re-checks, because it is the answer everyone wants (#972:
    `src.main` reported 14 target hits while importing none). In a fresh
    interpreter this is a cheap safety net rather than the load-bearing
    mechanism, and it stays for that reason.
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


#: The PARENT's site-packages, resolved while site.py is still in effect.
#: The child cannot work this out for itself under `-S` (see `_PROBE`).
_PURELIB = sysconfig.get_paths()["purelib"]

#: Generous on purpose: a slow import must not be reported as a broken one.
PROBE_TIMEOUT_SECONDS = 120

_MISSING_RE = re.compile(r"No module named '([^']+)'")


def _missing_module(stderr: str) -> str | None:
    """The module Python said was missing, or None.

    The LAST occurrence, because an import chain reports the innermost failure
    last and that is the one that actually did not resolve.
    """
    found = _MISSING_RE.findall(stderr)
    return found[-1] if found else None


class MeasurementFailed(RuntimeError):
    """A subprocess measurement did not produce an answer.

    RAISED, NEVER DEFAULTED. The whole point of isolation is that this
    instrument can no longer manufacture a zero, so a failed probe must not
    return "0 target modules" — that is indistinguishable from the finding
    #942 rests on. No answer is a third state and it is loud.
    """


#: The child runs with `-S -E` and then re-adds site-packages BY HAND. Both
#: halves are load-bearing and the reason is a mechanism rather than a
#: preference (#989, astrid).
#:
#: An editable install puts a finder on the child's path through a `.pth`
#: (PEP 660 `__editable__*.pth`) or an `.egg-link` — and BOTH are processed by
#: `site.py`. When the measured tree LACKS a submodule, `PathFinder` misses,
#: falls through to that finder, and the REAL repo backfills it. The instrument
#: then answers about a different tree than the one it was pointed at, silently.
#:
#: The blast radius is the historical case exactly: a tree missing submodules is
#: what "measure a commit that predates W1" means, so every backward-looking
#: comparison this instrument exists to support is the case it broke on, while
#: the forward-looking runs we happen to have taken are the ones it got right.
#:
#: `-S` skips `site.py`, so no `.pth` and no `.egg-link` is ever processed and
#: neither finder is installed. `-E` drops `PYTHONPATH`, which is the same
#: escape by another door. Site-packages is then appended explicitly, which
#: restores third-party DEPENDENCIES (without it the child cannot import `src`
#: at all — measured) while restoring none of the path injection, because that
#: lived in the files `site.py` reads.
#:
#: THIS IS ENVIRONMENT-DEPENDENT AND THAT IS THE FINDING. It reproduces in a
#: PEP 660 venv and does not in a setuptools-develop one; correctness of the
#: instrument was resting on which pip mode happened to be in use.
_PROBE = """
import json, sys
# Passed IN by the parent, deliberately not computed here: `-S` skips the
# site.py that makes a venv a venv, so the child's own `sysconfig` resolves to
# the BASE interpreter and its site-packages -- measured, the child could not
# import pydantic. The parent runs with site enabled and knows its real one.
sys.path.append({purelib!r})
sys.path.insert(0, {root!r})
from scripts.target_reachability import assert_provenance, assert_root, closure_for
import pathlib
# The parent already asserts the tree it was pointed at. The CHILD is the one
# that actually imports, and it was asserting nothing -- the same guard missing
# one layer down. Same predicate, not a second copy.
assert_root(pathlib.Path({root!r}))
size, hits, positive_ok = closure_for({entry!r})
# The anchor check above ran BEFORE any submodule was imported. This one runs
# after, over what was actually loaded -- the object the escape moves. Anchor
# checks the package; this checks the measurement.
assert_provenance(pathlib.Path({root!r}))
print("__RESULT__" + json.dumps(
    {{"size": size, "hits": sorted(hits), "positive_ok": positive_ok}}
))
"""


def measure(entry: str, root: pathlib.Path) -> tuple[int, set[str], bool]:
    """`entry`'s own closure, measured in a FRESH interpreter (#986).

    One entrypoint per process, so the answer does not depend on what was
    measured before it. Order becomes IRRELEVANT rather than merely correct —
    which survives a Procfile reorder, a third process, and the next entrypoint
    that reaches further. Order-correct survives none of those, and its failure
    is silent.

    A `ModuleNotFoundError` for `entry` propagates as such, because "this
    commit predates the composition root" is a FINDING the caller handles, not
    an error. Anything else is :class:`MeasurementFailed`.
    """
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-S",
                "-E",
                "-c",
                _PROBE.format(root=str(root), entry=entry, purelib=_PURELIB),
            ],
            capture_output=True,
            text=True,
            cwd=str(root),
            # A hung import is otherwise a SILENT FOURTH STATE alongside
            # reaches / does-not-reach / raises — an instrument that can
            # neither answer nor fail is not loud. Today's entrypoints import
            # cleanly; an import-time DB connect or egress call added to any of
            # them turns the cutover instrument into a hang. Generous, because
            # a slow import must not read as a broken one.
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise MeasurementFailed(
            f"probe for {entry} did not finish within {PROBE_TIMEOUT_SECONDS}s"
            " — an import that hangs is no answer, not a zero"
        ) from exc
    if proc.returncode != 0:
        # EXACT match, never substring. `src.worker` is a substring of
        # `src.worker.does_not_exist`, so a root that merely FAILS TO IMPORT
        # would route to the predates-W1 finding and print "no composition
        # root" — this instrument manufacturing #942's literal headline out of
        # a broken root. That is the same recursion this file closed for the
        # target axis, one axis over: no-answer-is-loud has to hold on EVERY
        # axis, not the one where the bug was first noticed.
        missing = _missing_module(proc.stderr)
        if missing == entry:
            raise ModuleNotFoundError(entry)
        raise MeasurementFailed(
            f"probe for {entry} exited {proc.returncode}: {proc.stderr[-600:]}"
        )
    for line in proc.stdout.splitlines():
        if line.startswith("__RESULT__"):
            d = json.loads(line[len("__RESULT__") :])
            return d["size"], set(d["hits"]), d["positive_ok"]
    raise MeasurementFailed(
        f"probe for {entry} exited 0 but printed no result: {proc.stdout[-400:]}"
    )


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


#: The Procfile entrypoint whose movement would actually clear #942. Named once,
#: as a value, because `_label_deployed` both TESTS this and PRINTS it: a copy in
#: prose inside the print is free to drift from the copy in the predicate, and
#: the drift would only ever show up in the one state the label exists for.
CLEARING_ENTRYPOINT = "worker"


def _label_deployed(deployed: dict) -> None:
    """Print the bound ON THE SAME BLOCK as any non-zero deployed figure.

    This exists because prose can always be separated from the number it
    describes. The #942 body carries an amendment saying a non-zero `web`
    reading is importable-not-serving; that amendment does not travel with a
    figure someone re-derives by running this script and pastes into a new
    comment. A label emitted by the act of printing the number cannot be
    separated from it -- including by the author of this script months later,
    which is the case that actually matters.

    **The message is keyed on the same fact it names, which is not decoration.**
    An earlier version fired on "any entrypoint non-zero" and printed one fixed
    conclusion: that this is not the blocker clearing, because the blocker
    clears when `CLEARING_ENTRYPOINT` reaches the tier. But that entrypoint is
    itself a key in `deployed`, so on the day it DID move, the banner would have
    printed it in the non-zero list underneath a headline denying the blocker
    was clearing -- a caveat outliving its premise, at the exact moment it
    mattered, with more authority than the prose it replaced because a script
    printed it. That is the failure this label exists to prevent, one level up.

    Silent when every entrypoint reads zero: there is nothing to misread yet,
    and a banner that always fires is one nobody reads.
    """
    nonzero = [p for p, d in deployed.items() if d["target_hits"]]
    if not nonzero:
        return
    zero = [p for p, d in deployed.items() if not d["target_hits"]]
    rule = "  " + "=" * 74
    print()
    print(rule)
    if CLEARING_ENTRYPOINT in nonzero:
        print(
            f"  THE CLEARING ENTRYPOINT HAS MOVED: {CLEARING_ENTRYPOINT!r} now reaches"
        )
        print(
            "  the target tier. This instrument CANNOT confirm the blocker is cleared."
        )
        print(
            "  It establishes import-reachability only -- that the process could import"
        )
        print(
            "  target code, never that any call path runs it. Someone has to go find the"
        )
        print("  call site by hand before this is reported as resolved.")
    else:
        print("  IMPORTABLE, NOT SERVING. This is NOT the #942 blocker clearing.")
        print(
            "  A target module in a deployed closure can be IMPORTED by that process."
        )
        print("  Nothing here shows traffic reaching it -- a route mounted but never")
        print("  registered with its provider imports its handler and serves nothing.")
        print(
            f"  The blocker clears when {CLEARING_ENTRYPOINT!r} reaches the tier. That is"
        )
        print("  a separate decision; do not read movement elsewhere as resolution.")
    print(
        f"  Non-zero: {', '.join(nonzero)}.   Still zero: {', '.join(zero) or 'none'}."
    )
    print(rule)


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
        size, hits, pos_ok = measure(mod, root)
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
        root_size, root_hits, pos_ok = measure("src.worker", root)
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
            "ok": CONTROL_NEGATIVE not in all_hits,
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
        for m in d["target_hits"]:
            print(f"      + {m}")
    _label_deployed(deployed)

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
        f"{'PASS' if neg['ok'] else 'FAIL'}  (present, but never a target hit)"
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
    print("and the installed set. It is no longer order-dependent — each")
    print("entrypoint is measured in its own interpreter — but that buys")
    print("nothing about portability. Only the `target modules reached`")
    print("counts and the module NAMES transfer between runs.")
    print("=" * 78)
    return OK if controls_ok else ERROR


if __name__ == "__main__":
    raise SystemExit(main())
