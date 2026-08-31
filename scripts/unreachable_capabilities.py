"""Capabilities that are BUILT and that nothing reaches (#1167).

**Every test asks whether what runs is correct. Nothing asks whether anything
does not run.** A test suite is coverage of executed paths, and unreachable code
is by definition not executed — so a green suite is not weak evidence about this
class, it is *no evidence at all*. Nine instances were found on this repo in one
evening by people asking the inverted question by hand.

**The reason a checker beats a human sweep here is measured, not assumed.** Of
the four manual probes in #1167, two returned wrong answers and one came back
UNMEASURED because the probe could not see its own axis. That is the bar.

## Why this class is mechanisable when #1120's was not

#1120 concluded the empty-population rule could not be mechanised: its instances
shared no syntactic shape. **This one is the opposite. Both sides of every
comparison are enumerable lists, so it is a set difference rather than a
judgement** — and a set difference has no false-positive-*rate* problem. It has
a known-exceptions problem, and exceptions are few, meaningful, and worth
writing down. That is what `ALLOWLIST` is, and why each entry is required to
carry a reason.

## Two rules this script obeys, both learned from things that went wrong

**1. Never carry a copy of what you are checking.** The Python side is
IMPORTED, not parsed: a checker holding its own copy of the command registry
re-creates the drift it exists to detect. Where a copy is unavoidable (the
TypeScript side cannot be imported from Python) the extraction is anchored and
**fails loudly when the anchor moves**, rather than returning a plausible empty
set.

**2. Fail loud when you cannot look.** Every enumeration asserts it found
something and raises :class:`CannotLook` otherwise. This matters more here than
almost anywhere, because of the DIRECTION of the failures: if the producing side
comes back empty the difference is empty and the script reports **all clear**
while having examined nothing. A checker for invisible defects that fails
silently is the joke telling itself.

## What it does NOT cover, stated because a gap nobody names reads as coverage

- **Cause B — a capability whose render precondition never holds.** #1167's
  instance 4: the Drive OAuth button is reached, its condition is not, at zero
  sources. No set difference can find that; it needs an empty-state audit.
- **Config.** The scheduling alerter was deployed, enrolled, and missing two
  environment lines. That is a deploy-time difference against a list this
  repository does not contain.
- **Probe 3 in general** — see :func:`probe_api_fields`, which reports the
  subset it can decide and names the reason for the rest rather than implying
  the axis is clean.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


class CannotLook(Exception):
    """An enumeration could not be performed.

    Distinct from "found nothing": this is the script being unable to see, and
    it always exits nonzero. The two must never render the same, because they
    have opposite meanings and the silent one reads as good news.
    """


@dataclass
class Finding:
    probe: str
    name: str
    detail: str


@dataclass
class ProbeResult:
    probe: str
    findings: list[Finding] = field(default_factory=list)
    allowed: list[tuple[str, str]] = field(default_factory=list)
    #: Things the probe could not decide. NEVER silently dropped — an
    #: undecidable case reported as absent is this script committing the class
    #: it detects.
    undecided: list[str] = field(default_factory=list)
    scope: str = ""


# --- enumeration: the source of truth, never a copy of it --------------------


def port_commands() -> dict[str, bool]:
    """Every command in the port's vocabulary → whether it has a real executor.

    **Imported, not parsed.** `commands.VOCABULARY` and `commands.REGISTRY` are
    the normative surface, and a regex over the same file would be a second copy
    that can disagree with the first — which is the exact defect class this
    script exists to find, committed by the finder.

    The executor flag matters: a command mapped to `None` is *unbuilt*, which is
    a different finding with a different fix, and is already pinned by
    `commands.UNBUILT`. Only a command with a real executor can be *unreachable*.
    """
    sys.path.insert(0, str(REPO))
    try:
        from src.services.target import commands  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 — cannot look, must be loud
        raise CannotLook(f"could not import the command port: {exc}") from exc
    vocab = tuple(getattr(commands, "VOCABULARY", ()))
    registry = dict(getattr(commands, "REGISTRY", {}))
    if not vocab:
        raise CannotLook("commands.VOCABULARY is empty — the port moved")
    if not registry:
        raise CannotLook("commands.REGISTRY is empty — the port moved")
    missing = [c for c in vocab if c not in registry]
    if missing:
        raise CannotLook(f"registry is not total over VOCABULARY: {missing}")
    return {c: registry[c] is not None for c in vocab}


#: The object literal the BFF's generic command envelope validates against. If
#: this identifier moves, the extraction below raises rather than returning an
#: empty set — an empty right-hand side would report every command as
#: unreachable, and an empty LEFT-hand side would report all clear.
_ENVELOPE_ANCHOR = "export const COMMAND_SPECS"
_ENVELOPE_FILE = "landing/src/lib/commands.ts"


def _strip_ts_noise(text: str) -> str:
    """Blank out comments and string literals, preserving offsets and newlines.

    Written because the first version scanned the raw text and harvested
    `here`, `is`, `py` and `reason` — ordinary English from a comment inside the
    object literal, each followed by a colon. **The direction of that error is
    what makes it worth a function:** spurious keys on the CONSUMING side make
    the set difference SMALLER, so the script under-reports and reads as good
    news. A checker whose parse bug hides findings is worse than no checker.

    Offsets are preserved (characters are replaced, never removed) so the brace
    scan below can still index into the original text.
    """
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        two = text[i : i + 2]
        if two == "//":
            while i < n and text[i] != "\n":
                out[i] = " "
                i += 1
        elif two == "/*":
            while i < n and text[i : i + 2] != "*/":
                if text[i] != "\n":
                    out[i] = " "
                i += 1
            for j in range(i, min(i + 2, n)):
                out[j] = " "
            i += 2
        elif text[i] in "\"'`":
            quote = text[i]
            out[i] = " "
            i += 1
            while i < n and text[i] != quote:
                if text[i] == "\\":
                    out[i] = " "
                    i += 1
                    if i < n:
                        out[i] = " "
                elif text[i] != "\n":
                    out[i] = " "
                i += 1
            if i < n:
                out[i] = " "
            i += 1
        else:
            i += 1
    return "".join(out)


def envelope_commands() -> set[str]:
    """Command names the front end's generic envelope will send.

    Brace-depth extraction of the object literal's TOP-LEVEL keys, not a regex
    for `name:`. The difference is load-bearing: several specs contain nested
    object literals with their own keys, and a flat pattern would harvest those
    too and silently shrink the difference — under-reporting, which is the
    direction that reads as good news.
    """
    path = REPO / _ENVELOPE_FILE
    try:
        text = path.read_text()
    except OSError as exc:
        raise CannotLook(f"could not read {_ENVELOPE_FILE}: {exc}") from exc
    start = text.find(_ENVELOPE_ANCHOR)
    if start < 0:
        raise CannotLook(
            f"{_ENVELOPE_ANCHOR!r} not found in {_ENVELOPE_FILE} — the anchor"
            " moved. Refusing rather than reporting an empty envelope, which"
            " would flag every command as unreachable."
        )
    brace = text.find("{", start)
    if brace < 0:
        raise CannotLook(f"no object literal after {_ENVELOPE_ANCHOR!r}")
    scan = _strip_ts_noise(text)
    depth, i, keys, n = 0, brace, set(), len(scan)
    while i < n:
        ch = scan[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
        elif depth == 1 and (ch.isalpha() or ch == "_"):
            m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*:", scan[i:])
            if m:
                keys.add(m.group(1))
                i += m.end() - 1
        i += 1
    if depth != 0:
        raise CannotLook(f"unbalanced braces in {_ENVELOPE_FILE}")
    if not keys:
        raise CannotLook(f"no keys extracted from {_ENVELOPE_ANCHOR}")
    return keys


def assert_envelope_is_a_subset(envelope: set[str], vocabulary: set[str]) -> None:
    """The envelope validates its path segment against the port's names, so
    every key it holds MUST be a command the port knows.

    A key outside the vocabulary therefore means the EXTRACTION is wrong, not
    that the product is. This is the self-check that catches a parser bug on the
    consuming side — the side whose errors hide findings — and it is the check
    that caught the comment-harvesting bug in this script's first version.
    """
    stray = sorted(envelope - vocabulary)
    if stray:
        raise CannotLook(
            f"extracted {stray} from {_ENVELOPE_ANCHOR}, which the port does not"
            " know. The envelope is a subset of VOCABULARY by construction, so"
            " this is a parse defect in this script, not a finding."
        )


# --- known exceptions -------------------------------------------------------

#: Capabilities that are deliberately not reachable by the route a probe checks.
#:
#: **A REASON IS REQUIRED and is enforced, not conventional** (see
#: :func:`_validate_allowlist`). A bare list is how a checker dies: the first
#: noisy run gets suppressed wholesale, nobody can later tell a considered
#: exception from a silenced finding, and the tool is deleted a month later as
#: "always red". A reason is also the only thing that can go STALE visibly — a
#: door that is removed leaves a reason that no longer reads true.
ALLOWLIST: dict[str, dict[str, str]] = {
    "commands": {
        "create_workspace": (
            "Has its own URL. `v1.py`'s generic command route refuses this name"
            " explicitly — a workspace id cannot be in the path of the call that"
            " creates the workspace, which is the one fact the port cannot know."
        ),
        "connect_account": (
            "Reached by POST /api/workspaces/[id]/sources/[sourceId]/connect,"
            " which returns an authorization URL rather than a command result."
        ),
        "reconnect_account": (
            "Same door as connect_account: `google_drive_oauth.connect_purpose`"
            " answers 'connect' or 'reconnect' for the same route, so one BFF"
            " path serves both commands. (#1167 listed this as unresolved.)"
        ),
    },
    "sql": {},
    "api_fields": {},
}


def _validate_allowlist() -> None:
    """An entry with an empty or absent reason is a hard error, not a warning.

    Enforced here rather than trusted, because the failure mode is silent: a
    reasonless entry looks identical to a considered one and suppresses a real
    finding forever.
    """
    for probe, entries in ALLOWLIST.items():
        for name, reason in entries.items():
            if not isinstance(reason, str) or len(reason.strip()) < 20:
                raise CannotLook(
                    f"allowlist entry {probe}.{name} has no usable reason."
                    " Every exception must say why, or it is a silenced finding."
                )


# --- probe 1: commands built but reachable from no door ---------------------


def probe_commands() -> ProbeResult:
    """Commands with a real executor that the generic envelope does not send.

    This is #1167's instance 1 (`rename_workspace`) and instance 7
    (`pause_workspace` / `resume_workspace`).
    """
    port = port_commands()
    envelope = envelope_commands()
    assert_envelope_is_a_subset(envelope, set(port))
    built = {name for name, has_executor in port.items() if has_executor}
    allow = ALLOWLIST["commands"]
    res = ProbeResult(
        probe="commands",
        scope=(
            f"{len(port)} in VOCABULARY, {len(built)} with a real executor,"
            f" {len(envelope)} reachable through the generic envelope,"
            f" {len(allow)} allowlisted with a stated second door"
        ),
    )
    for name in sorted(built - envelope):
        if name in allow:
            res.allowed.append((name, allow[name]))
        else:
            res.findings.append(
                Finding(
                    "commands",
                    name,
                    "real executor; no envelope entry, no declared door",
                )
            )
    return res


# --- probe 2: SQL functions with no caller ----------------------------------


def sql_functions() -> dict[str, str]:
    """Every `fn_*` the migration stream defines → the file that defines it."""
    migrations = REPO / "scripts" / "migrations"
    files = sorted(migrations.glob("*.sql"))
    if not files:
        raise CannotLook(f"no migrations under {migrations}")
    found: dict[str, str] = {}
    pattern = re.compile(
        r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+(fn_[a-z0-9_]+)", re.I
    )
    for f in files:
        for name in pattern.findall(f.read_text()):
            found.setdefault(name, f.name)
    if not found:
        raise CannotLook("no fn_* definitions found — the pattern or layout moved")
    return found


def _names_in(paths: list[Path], label: str) -> str:
    if not paths:
        raise CannotLook(f"no {label} files found")
    return "\n".join(p.read_text() for p in paths)


def _sql_calls_only(sql: str) -> str:
    """SQL with privilege and metadata statements blanked out.

    Two kinds of non-call name a function and must be removed, and BOTH were
    found by running this against a known instance rather than by reasoning:

    * **Privilege and metadata statements.** `GRANT EXECUTE ON FUNCTION fn_x(…)`
      names it and even carries parentheses.
    * **Comments.** `fn_auth_plane_sweep` is mentioned in four `--` comments in
      the migration stream — prose describing the schedule and the door
      inventory. That is what kept it "undecided" after the privilege fix.

    Both downgrade a genuine finding to undecided, which is the direction that
    makes the probe useless: `fn_auth_plane_sweep` is #1167's instance 6 and a
    CONFIRMED real one, so a probe that cannot name it is not working.
    """
    keep = []
    for line in sql.splitlines():
        line = re.sub(r"--.*$", "", line)
        if re.match(r"\s*(GRANT|REVOKE|COMMENT|ALTER|DROP)\b", line, re.I):
            line = ""
        keep.append(line)
    return "\n".join(keep)


def probe_sql_functions() -> ProbeResult:
    """SQL functions that no Python names, separated from those SQL calls.

    #1167's instance 6: `fn_auth_plane_sweep` existed in production with zero
    Python callers and its job kind parked, so 96 of 97 expired rows were
    retained. A function called only by other SQL is a legitimate exception and
    is reported as its own population rather than silently subtracted — the two
    have different remedies and collapsing them hides the second.
    """
    defined = sql_functions()
    py = _names_in(sorted((REPO / "src").rglob("*.py")), "python")
    sql = _names_in(sorted((REPO / "scripts" / "migrations").glob("*.sql")), "sql")
    res = ProbeResult(
        probe="sql", scope=f"{len(defined)} fn_* defined in the migration stream"
    )
    allow = ALLOWLIST["sql"]
    for name, where in sorted(defined.items()):
        if re.search(rf"\b{name}\b", py):
            continue
        if name in allow:
            res.allowed.append((name, allow[name]))
            continue
        # Called by other SQL? Two things must not count as calls: the CREATE
        # itself, and the PRIVILEGE statements beside it. `GRANT EXECUTE ON
        # FUNCTION fn_x(...)` names the function and even carries parentheses,
        # so a naive occurrence count reads a grant as a caller and downgrades a
        # genuine finding to "undecided" — measured: it did exactly that to
        # `fn_auth_plane_sweep`, which is #1167's instance 6 and a CONFIRMED
        # real one. Under-reporting a known instance is the direction that
        # would have made this probe useless.
        if len(re.findall(rf"\b{name}\b", _sql_calls_only(sql))) > 1:
            res.undecided.append(
                f"{name} ({where}): named only in SQL — may be called by another"
                " function or by a scheduled statement this script cannot see"
            )
            continue
        res.findings.append(
            Finding(
                "sql", name, f"defined in {where}; named by no Python and no other SQL"
            )
        )
    return res


# --- probe 3: API fields no client reads ------------------------------------

#: Where the reader functions that assemble API payloads live. Their SELECT
#: lists are the closest thing this codebase has to a declared response shape.
_READER_MODULE = "src/services/target/workspaces.py"


def _select_output_names(sql: str) -> set[str]:
    """The names a SELECT list puts in its result rows.

    `x AS alias` yields `alias`; `t.col` and bare `col` yield `col`. Expressions
    without an alias are NOT named and are therefore skipped — they cannot be
    matched against a client field, and guessing one would manufacture a finding.
    """
    head = re.split(r"\bFROM\b", sql, maxsplit=1, flags=re.I)[0]
    head = re.sub(r"^\s*SELECT\b", "", head, flags=re.I)
    names: set[str] = set()
    depth = 0
    current: list[str] = []
    for ch in head:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            names |= _one_select_item("".join(current))
            current = []
        else:
            current.append(ch)
    names |= _one_select_item("".join(current))
    return {n for n in names if n and n != "*"}


def _one_select_item(item: str) -> set[str]:
    item = item.strip()
    if not item:
        return set()
    alias = re.search(r"\bAS\s+([A-Za-z_][A-Za-z0-9_]*)\s*$", item, re.I)
    if alias:
        return {alias.group(1)}
    plain = re.fullmatch(r"(?:[A-Za-z_][A-Za-z0-9_]*\.)?([A-Za-z_][A-Za-z0-9_]*)", item)
    return {plain.group(1)} if plain else set()


def probe_api_fields() -> ProbeResult:
    """Fields the API produces that no front-end source names.

    **This axis is NOT statically decidable in general on this codebase, and
    that is a finding rather than a limitation to hide.** `src/api/routes/v1.py`
    declares **zero** response models — every endpoint returns a bare dict
    assembled at runtime — so there is no producing-side field set to diff
    against. #1167 specified this probe as "response model fields minus the
    front end's mirrored interfaces"; the first half of that subtraction does
    not exist.

    What IS decidable is the subset where the producer states its columns as a
    literal: the reader functions in `workspaces.py`, whose SELECT lists are the
    closest thing here to a declared shape. That is enough to cover the case
    that actually bit — `credential_status` (#1080) shipped in the sources
    payload with a docstring saying *"CLOSED at the API and not yet consumed
    here"*, and nothing surfaced it again.

    **Consumption is tested by naming, not by pairing.** Matching a reader to
    "its" TypeScript interface would require a hardcoded map — a copy, and the
    thing this script refuses to carry. A field named NOWHERE in `landing/src`
    is a candidate regardless of which interface should have held it.

    **What would make the general case decidable: declared response models.**
    Until then this probe reports its own coverage and the rest is `undecided`,
    never absent.
    """
    path = REPO / _READER_MODULE
    try:
        source = path.read_text()
    except OSError as exc:
        raise CannotLook(f"could not read {_READER_MODULE}: {exc}") from exc
    selects = re.findall(r'"\s*SELECT\b.*?"\s*(?:\)|,)', source, re.S)
    if not selects:
        raise CannotLook(
            f"no SELECT literals found in {_READER_MODULE} — the reader layout"
            " moved. Refusing rather than reporting an empty producing side,"
            " which would render as 'every field is consumed'."
        )
    produced: set[str] = set()
    for block in selects:
        produced |= _select_output_names(re.sub(r'"\s*"?', " ", block))

    ts_files = sorted((REPO / "landing" / "src").rglob("*.ts")) + sorted(
        (REPO / "landing" / "src").rglob("*.tsx")
    )
    if not ts_files:
        raise CannotLook("no front-end sources found")
    # Comments stripped so prose mentioning a field does not read as a consumer.
    # Strings are KEPT: `row["credential_status"]` is a real read.
    consumed_text = "\n".join(
        re.sub(r"//.*$", "", p.read_text(), flags=re.M) for p in ts_files
    )

    res = ProbeResult(
        probe="api_fields",
        scope=(
            f"{len(selects)} SELECT literals parsed in {_READER_MODULE};"
            f" {len(produced)} distinct output names; consumption tested by name"
            f" across {len(ts_files)} front-end files"
        ),
    )
    res.undecided.append(
        "the general axis: v1.py declares NO response models, so every endpoint"
        " that does not read through workspaces.py is outside this probe"
    )
    allow = ALLOWLIST["api_fields"]
    for name in sorted(produced):
        if re.search(rf"\b{re.escape(name)}\b", consumed_text):
            continue
        if name in allow:
            res.allowed.append((name, allow[name]))
            continue
        res.findings.append(
            Finding(
                "api_fields", name, "produced by a reader; named nowhere in landing/src"
            )
        )
    return res


# --- CLI --------------------------------------------------------------------

PROBES = {
    "commands": probe_commands,
    "sql": probe_sql_functions,
    "api_fields": probe_api_fields,
}

#: Exit codes. **3 is the one that matters.** "I found nothing" and "I could not
#: look" must never share a code: they have opposite meanings, and the silent
#: one reads as good news. A caller that treats every nonzero the same still
#: cannot be misled, because 3 is nonzero too — the split only ever adds
#: information.
EXIT_CLEAN, EXIT_FINDINGS, EXIT_USAGE, EXIT_CANNOT_LOOK = 0, 1, 2, 3


def run(names: list[str]) -> list[ProbeResult]:
    _validate_allowlist()
    return [PROBES[n]() for n in names]


def render(results: list[ProbeResult]) -> str:
    out: list[str] = []
    total = 0
    for r in results:
        out.append(f"\n## {r.probe}")
        out.append(f"   scope: {r.scope}")
        for f in r.findings:
            total += 1
            out.append(f"   FINDING   {f.name} — {f.detail}")
        for name, reason in r.allowed:
            out.append(f"   allowed   {name} — {reason[:96]}")
        for u in r.undecided:
            out.append(f"   UNDECIDED {u}")
        if not r.findings:
            out.append("   (no findings)")
    out.append(f"\n{total} finding(s) across {len(results)} probe(s).")
    out.append(
        "A finding is a capability that is BUILT and that nothing reaches."
        " UNDECIDED is not a pass — it is a question this script cannot answer."
    )
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--probe", action="append", choices=sorted(PROBES), default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    names = args.probe or sorted(PROBES)
    try:
        results = run(names)
    except CannotLook as exc:
        print(f"CANNOT LOOK: {exc}", file=sys.stderr)
        print(
            "This is not a clean result. An enumeration failed, so the set"
            " difference was never computed.",
            file=sys.stderr,
        )
        return EXIT_CANNOT_LOOK
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "probe": r.probe,
                        "scope": r.scope,
                        "findings": [vars(f) for f in r.findings],
                        "allowed": [{"name": n, "reason": z} for n, z in r.allowed],
                        "undecided": r.undecided,
                    }
                    for r in results
                ],
                indent=2,
            )
        )
    else:
        print(render(results))
    return EXIT_FINDINGS if any(r.findings for r in results) else EXIT_CLEAN


if __name__ == "__main__":
    raise SystemExit(main())
