---
title: "Run log — the tech-debt audit sprint (16 phases, one PR each)"
type: run-log
status: in-progress
owner: chris
created: 2026-09-20
tags: [run-log, sprint, tech-debt]
links: [00_TECH_DEBT.md]
---

# Sprint run log — tech-debt audit of the one tier

Driver: `/artemis-skills:build-all` over `documentation/planning/2026-09-20-tech-debt-audit/`.
Epic: [`00_TECH_DEBT.md`](00_TECH_DEBT.md) — 75 findings, 16 cleanup PRs, 12 defects flagged out.

**Goal condition.** All 16 phase PRs merged with the frozen baseline unregressed; the 12 flagged
defects still *outside* the cleanup PRs and queued to the owner; this directory archived to
`documentation/archive/` with a banner and an index row (the epic's own Live-status instruction).
"Not met, because X" is an acceptable answer to this — silence is not.

---

## 1. Kickoff gates

| # | Gate | Disposition |
|---|---|---|
| G1 | The 17 phase docs are committed and reachable from `main` | **PASSED** — owner ruled: land them as their own docs PR from `main` |
| G2 | The sprint branches from a clean, intended base | **PASSED** — owner ruled: push the unrelated branch as its own PR; branch the sprint from `origin/main` |
| G3 | The behaviour-preservation baseline is runnable | **PASSED** (runner-resolved, see below) |
| G4 | Merge method known, so the topology can be planned | **PASSED** — squash |
| G5 | CHANGELOG gate understood | **PASSED** — warn-only; docs-only PRs exempt |
| G6 | Posting-safety constraint acknowledged | **PASSED** — standing, see below |
| G7 | Merge authority | **WAIVED BY THE OWNER IN WRITING** — see below |

### G1 — the phase docs (PASSED)

They were untracked: `git log --all -- documentation/planning/2026-09-20-tech-debt-audit/` was
empty, so all 17 docs (≈590 KB) existed only in the working tree. Owner ruled they land as their
own docs PR cut from `main`, carrying the 17 docs and this ledger and nothing else.

**One thing they deliberately do not carry:** the `documentation/README.md` index rows for this
epic. Commit `0cf13f7` — the unrelated device-native branch, now PR #1334 — already adds both the
tree entry and the full bullet row for `2026-09-20-tech-debt-audit/`. Editing that file here would
conflict with #1334 for no gain, so the docs PR stays disjoint from it and the index rows ride on
#1334. If #1334 is ever abandoned, this epic's index row has to be re-added by hand — recorded in
§7 so that is not discovered later.

### G2 — the base (PASSED)

The tree was on `docs/device-native-inbound-spec`, one unpushed commit (`0cf13f7`) of unrelated
device-native docs. Pushed and opened as **PR #1334**, left for the owner — it is not a phase of
this sprint and this sprint does not depend on it merging. The sprint's own branches cut from
`origin/main`.

### G3 — the baseline is runnable (PASSED, runner-resolved)

Three things were wrong and all three are fixed; recorded because every later run needs them:

1. No PostgreSQL was running. Started `postgresql@17` via `pg_ctl` (homebrew; three versions
   installed, all stopped).
2. **The Bash sandbox denies TCP to `localhost:5432`** ("Operation not permitted"), which the
   harness reports as "no PostgreSQL answered". Every suite run in this sprint must be issued
   with the sandbox disabled, or it produces a false "no database" verdict.
3. The role `storydump_user` did not exist on the fresh instance. Created (superuser, local
   throwaway instance only).

Baseline command, and the one every phase re-runs:

```bash
REQUIRE_TEST_DATABASE=1 .venv/bin/pytest --no-cov -q     # sandbox OFF
```

Note `.venv/`, not `./venv/` — the `Makefile` targets that say `./venv/bin/pytest` are finding
TD-O6 in doc 12, not a usable command.

**Frozen baseline** — `main` at `5b90388`, PostgreSQL 15.18, 2026-09-20:

```
===== 2 failed, 3671 passed, 2 skipped, 5 deselected in 166.55s (0:02:46) ======
```

The two failures are the same pair every run, and they are the platform, not the tree:

```
TestARedirectHopIsPinnedToItsOwnValidation.test_a_hop_resolving_to_a_forbidden_address_is_refused
TestARedirectHopIsPinnedToItsOwnValidation.test_a_hop_resolving_ELSEWHERE_is_reached_at_its_own_address
E   OSError: could not bind on any address out of [('127.0.0.2', 0)]
```

macOS's loopback carries only `127.0.0.1`; Linux carries all of `127/8`, so both pass in CI.
Unblocking action if they are ever needed locally: `sudo ifconfig lo0 alias 127.0.0.2 up`.

Both skips are explained and expected:

```
SKIPPED [1] tests/scripts/test_schema_drift_live.py:109: SCHEMA_DRIFT_DSN not set — live drift is audited on a schedule only
SKIPPED [1] tests/src/services/target/test_egress_floor.py:968: no port free on both loopback addresses
```

The suite's own ceiling is `MAX_EXPECTED_SKIPS = 1`, so a 2-skip run prints a loud complaint
without failing. That complaint is part of the frozen baseline — **it is not a new problem when a
phase sees it**, and a phase that silences it has changed the gate rather than passed it.

**One CI-parity fact worth keeping:** the local role must be a *superuser*, because CI's
`POSTGRES_USER: test_user` is the postgres image's bootstrap superuser. A non-superuser fails
`test_ops_views_gate.py::test_the_predicates_confine_rows_even_without_row_level_security` (only
an owner/superuser bypasses RLS). On PostgreSQL **17** a superuser additionally auto-grants itself
role memberships, which fails the RLS harness — which is why this baseline is pinned to 15.

### G4 — merge method (PASSED)

Squash. Every recent PR is one commit on `main` (`5b90388` #1332, `0966771` #1325, `a85f6db`
#1324). This drives §3.

### G5 — CHANGELOG (PASSED)

`.github/workflows/ci.yml:232` `changelog-check` warns rather than fails, and exempts docs-only
PRs. The epic asks every phase for a CHANGELOG entry anyway; that is the standard held here, not
CI's.

### G6 — posting safety (PASSED, standing)

Per `CLAUDE.md`: this system posts to Instagram. No phase in this sprint runs `python -m src.main`,
`storydump approve|cancel|resolve`, `storydump tokens revoke`, or `storydump webhook
register|deregister`, and nothing runs against production. Docs 06, 07 and 02 edit the publish
path; they edit it, they never exercise it against a live tenant.

### G7 — merge authority (WAIVED BY THE OWNER IN WRITING)

Asked, with the consequence stated plainly: merging a phase PR to `main` auto-deploys it to
production via Railway, and this is the service that posts to Instagram. The owner chose
**"Claude merges everything, all 4 waves"** over an option that stopped after Wave 1 for review,
having been shown that this means *~16 production deploys of the posting service, no human review
of any diff*.

**Residual risk, in plain words:** every defect this sprint introduces reaches production without
a second pair of human eyes. The controls that remain are the ones in §2 — the frozen baseline,
the behaviour-preservation invariant, the four guards — plus `/simplify`'s independent read per
phase. They are real but they are machine checks; none of them is a human reading a diff.

This waiver covers merging and deploying. It does **not** touch G6: no phase runs the posting
scheduler, `storydump approve|cancel|resolve`, `storydump tokens revoke`, or
`storydump webhook register|deregister`, and nothing is run against production.

---

## 2. Invariant registry

Declared once; **every one re-checked after every phase merges**, output pasted into that phase's
entry. A broken invariant stops the sprint.

| ID | Invariant | Check | On breakage |
|---|---|---|---|
| I1 | The frozen test baseline does not regress | `REQUIRE_TEST_DATABASE=1 .venv/bin/pytest --no-cov -q` (sandbox off); compare pass/fail/skip to §1 G3 | Stop. A cleanup sprint that loses a test has changed behaviour. |
| I2 | **No phase changes behaviour.** The 12 flagged defects (epic §Questions) stay out of every cleanup PR | Per-phase: the doc's own call-site audit + I1. Any step that would change a rendered value, a wire contract or a stored key stops the phase | Queue to §7. Never fold a defect fix into a cleanup PR. |
| I3 | The docs guards stay green — the never-run mirror between `CLAUDE.md` and `AGENTS.md`, the legacy-name pins, the satellite copies | `.venv/bin/pytest --no-cov -q tests/test_agent_docs.py` | Docs 12 and 13 rename and delete; a drifted mirror is a real defect, not a test to relax. |
| I4 | The FC-2 Telegram ratchet stays green | `python scripts/telegram_ratchet.py` | `src/exceptions/telegram.py` **is** in the baseline's `telegram_modules` (4 entries). Doc 02 deleting it must `--write-baseline` deliberately, in the same PR. |
| I5 | No phase adds a skipped test | Falls out of I1; `MAX_EXPECTED_SKIPS = 1` (`tests/conftest.py:80`) | Docs 02, 11, 13 delete test-only surfaces — deleting a test is fine, skipping one is not. |
| I6 | Lint stays clean at the repo's own policy | `ruff check .` and `ruff format . --check` | Whole-repo scope including `tests/`. |
| I7 | `landing/` stays green — docs 14, 15 and 16 all edit it | `npx vitest run` · `npx tsc --noEmit` · `npx eslint`, from `landing/` | Baseline 2026-09-20: **378 passed / 36 files**, `tsc` clean, `eslint` clean. Node 20.19.5. |

Why a registry at all: this epic's *whole* premise is that hand-maintained copies drift. Two of
its findings (C1, C5) are drifts that already shipped. An invariant checked by memory across 16
PRs would be a seventeenth.

---

## 3. Merge topology

One repo, **squash merge**. Squash rewrites the parent's commits into one, so a child branched on
top of an unmerged parent re-includes the parent's diff and conflicts structurally. The epic's
dependency matrix has six such chains, so:

**Rule for this sprint: do not stack. A phase branches from `origin/main` only after every phase
it depends on has squash-merged.** This costs wall-clock and buys back every rebase.

Waves, derived from the epic's Depends-on/Blocks matrix:

| Wave | Phases | Branches from |
|---|---|---|
| W1 | 01, 10, 11, 13, 14 | `origin/main` (no dependencies) |
| W2 | 02, 03, 04 (after 01) · 15 (after 14) | fresh `origin/main` |
| W3 | 05, 12 (after 02) · 07, 08 (after 03) · 09 (after 04) · 16 (after 15) | fresh `origin/main` |
| W4 | 06 (after 03 **and** 05) | fresh `origin/main` |

Critical path: 01 → 02 → 05 → 06. Phase 06 is last by construction, not by effort.

No cross-repo pairs — `landing/` lives in this repo (docs 14–16), so there is no second-repo
merge order to hold.

---

**`main` is not branch-protected.** `gh api repos/.../branches/main/protection` returns 404, and
the runner holds ADMIN. Nothing mechanical stops a merge over red or still-running CI, so the
merge gate in this sprint is entirely the runner's own discipline:

> **No phase merges until its PR's CI is green — every job, observed, not assumed.** `gh pr checks
> <n> --watch` before `gh pr merge <n> --squash`. A merge deploys to production (§1 G7); a merge
> over unfinished CI deploys something nothing has checked.


**Operating note — the working tree is shared.** At 20:22 during this sprint's setup, another
session wrote `documentation/planning/2026-09-20-device-native-inbound/` (a `/forge` plan, 7 docs)
into the same checkout. Nothing of this sprint's is in it and it is not staged here. The
consequence for the runner: **every phase works in its own git worktree**, and the main checkout
is left alone rather than being branch-switched under a concurrent session. Untracked output
survives branch switches, so the other session is unharmed, but a phase that ran in the main
checkout would not be.


## 4. Phase results

| # | Doc | Status | PR | CI | Deploy |
|---|---|---|---|---|---|
| 01 | `01_one-spelling.md` | not started | — | — | — |
| 02 | `02_dead-lane-and-surfaces.md` | not started | — | — | — |
| 03 | `03_rule-of-three-services.md` | not started | — | — | — |
| 04 | `04_rule-of-three-api-cli.md` | not started | — | — | — |
| 05 | `05_imports-and-homes.md` | not started | — | — | — |
| 06 | `06_publish-pipeline-shape.md` | not started | — | — | — |
| 07 | `07_executors-and-tap.md` | not started | — | — | — |
| 08 | `08_integrations-shape.md` | not started | — | — | — |
| 09 | `09_composition-roots.md` | not started | — | — | — |
| 10 | `10_dependencies-and-ci.md` | not started | — | — | — |
| 11 | `11_test-scaffolding.md` | not started | — | — | — |
| 12 | `12_stale-words-and-names.md` | not started | — | — | — |
| 13 | `13_legacy-instruments.md` | not started | — | — | — |
| 14 | `14_landing-one-contract.md` | not started | — | — | — |
| 15 | `15_landing-shared-shapes.md` | not started | — | — | — |
| 16 | `16_landing-integrations-tab.md` | not started | — | — | — |

---

## 5. Per-phase entries

_Written as each phase completes: verification output pasted, scope exclusions stated, premise
findings recorded rather than papered over, invariant sweep output._

### Pre-flight finding — doc 10, measured before dispatch

Doc 10 step 7 turns `pip-audit` from `|| true` + `continue-on-error` into a gating job. That is
only safe if the tree passes it today, so it was measured rather than assumed:

```
$ .venv/bin/pip-audit -r requirements.txt
Found 4 known vulnerabilities in 2 packages
Name      Version ID              Fix Versions
--------- ------- --------------- ------------
httpx2    2.2.0   PYSEC-2026-3849 2.11.0
httpx2    2.2.0   PYSEC-2026-3848 2.11.0
httpx2    2.2.0   PYSEC-2026-3846 2.12.0
httpcore2 2.2.0   PYSEC-2026-3844 2.10.0
```

**The placeholder suppression was hiding four live advisories**, not nothing — and all four are in
`httpx2`, which is one of the eight packages nothing imports, plus its transitive `httpcore2`
(which `requirements.txt` does not pin itself). So the same PR that makes the job able to fail is
the PR that makes it pass. Verified against a pruned copy before any code was written:

```
$ grep -vE "^(alembic|httpx2|python-dateutil|tenacity|google-api-python-client|google-auth|google-auth-oauthlib|anthropic)==" requirements.txt > req_pruned.txt
$ .venv/bin/pip-audit -r req_pruned.txt
No known vulnerabilities found
```

Consequence for the phase: steps 2 and 7 must land in the **same** PR. Splitting them either
leaves the gate blind or turns `main` red.


---

## 6. Review rounds

_Per round: which findings, the class sweep for each with its command and counts, what closed._

---

## 7. Owner-decision queue

| # | Item | Why it is here |
|---|---|---|
| Q1 | **PR #1334** — the device-native inbound docs | Not a phase of this sprint. Opened and left for the owner. It also carries this epic's `documentation/README.md` index rows (see §1 G1) — if it is abandoned, those rows need re-adding by hand. |
| Q2 | The 12 flagged defects (epic §Questions 1–12) | Each changes behaviour; each wants its own ruling or a bug-fix PR, never a cleanup PR. Carried here for the sprint's duration so none is silently folded in. |
| Q3 | `.claude/settings.json` still names the deleted legacy CLI verbs and denies none of the `storydump` never-run verbs | Owner edit, noted in `CLAUDE.md`. Out of every phase's scope; recorded so it is not lost. |
| Q4 | Two egress-floor tests cannot run on macOS | `OSError: could not bind on any address out of [('127.0.0.2', 0)]`. Smallest unblocking action: `sudo ifconfig lo0 alias 127.0.0.2`. They run in CI (Linux), so the sprint is not blind to them — but the local baseline excludes them. |

