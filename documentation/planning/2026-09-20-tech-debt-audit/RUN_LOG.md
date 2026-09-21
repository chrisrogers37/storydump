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

**Frozen baseline** — `main` at `2edf0cc`, PostgreSQL 15.18, 2026-09-20:

```
===== 2 failed, 3690 passed, 2 skipped, 5 deselected in 185.22s (0:03:05) ======
```

It was measured twice, and the first figure is recorded rather than quietly replaced. The first
run gave **3671 passed** on `5b90388`. While the sprint was setting up, #1333 merged (the
fleet-health doors, migration 081) and moved `main` to `f03af19`, adding 19 tests. Every Wave 1
branch is cut from `2edf0cc`, so `2edf0cc` is the number that governs, and the four agents already
dispatched against 3671 were sent the correction rather than left to discover it.

The re-measurement also caught a defect in the runner's own tooling: the copied `testenv.sh` had
lost the trailing `=` of the Fernet `ENCRYPTION_KEY` to a `cut -d= -f2`, which produced 33
credential-test failures that looked like a regression in the tree and were nothing of the kind.
A baseline is only worth what its environment is worth.

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

**Second waiver, asked separately once the rail was discovered.** `main` carries an active
repository *ruleset* (`Protect main branch`, id 11551284) — not classic branch protection, which
is why a first check via the protection API returned 404 and read as "unprotected". The ruleset
requires `required_approving_review_count: 1` with `require_code_owner_review: true`, and lists
RepositoryRole 5 (admin) as a bypass actor with `bypass_mode: always`.

So the merge the owner authorised was not available: `gh pr merge` refused #1335 with "the base
branch policy prohibits the merge" despite all nine checks passing. Bypassing it is a *different*
act from merging — it overrides a review rail the repository owner deliberately installed — so it
was put back to the owner rather than assumed. Shown the consequence in those words, the owner
chose **`--admin` on every phase**.

**Residual risk:** the code-owner review requirement is bypassed on all 16 merges. The rail stays
configured and will apply to everyone else; it simply does not apply to this sprint. Combined with
G7's first waiver, no human reads any diff in this epic before it reaches production.

The runner's own compensating discipline, unchanged by either waiver: **every phase's CI must be
observed green before its merge** (`gh pr checks <n> --watch`), and every invariant in §2 re-run
after it.

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
checkout would not be. (Resolved on its own: that session has since committed its forge plan to
`docs/device-native-inbound-spec` — PR #1334, now two commits — and moved into a worktree of its
own. Nothing was lost and this sprint did not touch it.)

---

## 3a. Deploy reachability — answered once, for the sprint's phase classes

The skill's question is *"what makes this change live, and is that automated?"* For this repo the
answer is uniform per class, so it is settled here rather than re-derived sixteen times.

**Railway auto-deploys `main`, both services, with no human step.** `railway.toml` and the
`Procfile` say what that means concretely:

```
preDeployCommand = "python -m scripts.migration_runner apply"
healthcheckPath  = "/health"      healthcheckTimeout = 30
drainingSeconds  = 60             restartPolicyType = "ON_FAILURE"

worker: python -m src.main
web:    uvicorn src.api.app:app --host 0.0.0.0 --port ${PORT:-8000}
```

So for this sprint, **merged really is live** — there is no "merged, NOT live" caveat to carry and
no manual operator command owed. Three consequences the runner holds rather than discovers:

1. **Every merge restarts the production posting worker.** `python -m src.main` is the scheduler
   this repo's safety rules forbid an agent to run; Railway runs it as the service's own
   entrypoint, which is a different thing from the runner invoking it, and is exactly what the
   owner authorised in §1 G7. `drainingSeconds = 60` lets an in-flight publish or delivery commit
   before SIGKILL, and a lease it still holds lapses within 90s for the clock's reaper.
2. **Every deploy applies pending migrations** before the new version serves. That is the armed,
   automatic `runner apply`, not the gated `apply --manual` the safety rules name. A failing
   migration aborts the deploy with the old version still serving.
3. **The deployed SHA is worth checking after a merge, not assumed.** This repo has already been
   bitten once: after the owner's window the worker came back on an *old* deployment because a
   `railway redeploy` re-ran a superseded commit. Auto-deploy is automatic, not infallible.

| Phase class | Phases | What makes it live |
|---|---|---|
| Runtime Python (`src/`, `storydump_cli/`) | 01–09, 12 | Railway, both services, automatic |
| Dependency manifest (`requirements.txt`, `setup.py`) | 10 | Railway **rebuild** — the riskiest deploy in Wave 1, because it changes the image rather than the code in it |
| `landing/` | 14, 15, 16 | Vercel, automatic (it already reports as a PR check) |
| Tests, CI config, deleted dev scripts | 11, 13 | **Nothing — inert at runtime.** `scripts/migration_runner.py` is the exception that *is* reachable (it is the `preDeployCommand`), and no phase in this sprint touches it |


## 4. Phase results

| # | Doc | Status | PR | CI | Deploy |
|---|---|---|---|---|---|
| 01 | `01_one-spelling.md` | PR open, CI running | #1336 | 8/9 | auto |
| 02 | `02_dead-lane-and-surfaces.md` | not started | — | — | — |
| 03 | `03_rule-of-three-services.md` | not started | — | — | — |
| 04 | `04_rule-of-three-api-cli.md` | not started | — | — | — |
| 05 | `05_imports-and-homes.md` | not started | — | — | — |
| 06 | `06_publish-pipeline-shape.md` | not started | — | — | — |
| 07 | `07_executors-and-tap.md` | not started | — | — | — |
| 08 | `08_integrations-shape.md` | not started | — | — | — |
| 09 | `09_composition-roots.md` | not started | — | — | — |
| 10 | `10_dependencies-and-ci.md` | **CI red — in rework** (`httpx2` is load-bearing) | #1337 | Test ✗ | auto |
| 11 | `11_test-scaffolding.md` | PR open, CI running | #1339 | — | inert |
| 12 | `12_stale-words-and-names.md` | not started | — | — | — |
| 13 | `13_legacy-instruments.md` | suite re-run (one suspected flake) | — | — | inert |
| 14 | `14_landing-one-contract.md` | PR open, CI running | #1338 | 8/9 | Vercel |
| 15 | `15_landing-shared-shapes.md` | not started | — | — | — |
| 16 | `16_landing-integrations-tab.md` | not started | — | — | — |

---

## 5. Per-phase entries

_Written as each phase completes: verification output pasted, scope exclusions stated, premise
findings recorded rather than papered over, invariant sweep output._

### Wave 1 — dispatched, interrupted, resumed

All five W1 phases (01, 10, 11, 13, 14) were dispatched into isolated worktrees cut from
`2edf0cc`. Roughly forty minutes in, **all five were terminated mid-flight by an account session
rate limit** — an infrastructure interruption, not a failure of any phase.

Recorded because the recovery is the interesting part, and because a sprint that hides an
interruption is not auditable:

| Phase | Where it stopped | State of its worktree |
|---|---|---|
| 01 | re-running the suite after judging one collection error pre-existing | 9 files modified, uncommitted |
| 10 | verifying the new dependency pin catches drift both ways | `requirements.txt`, `setup.py` modified; new test file untracked |
| 11 | baseline confirmed, edits not yet begun | clean |
| 13 | final orphan sweep; 3,542 lines confirmed | 5 deletions staged, 7 files modified |
| 14 | executing the D3 route deletion | 2 deletions, 7 files modified |

**Nothing was lost.** Git worktrees are ordinary directories, so uncommitted work survives an
agent's death; the agents were resumed from their own transcripts rather than restarted, which
preserved their reasoning as well as their edits. Two test databases leaked from the killed runs
(≈15 MB) and were deliberately left for the repo's own reaper rather than dropped by hand — with
fresh suites running concurrently, a stray and a live database are not distinguishable from
outside.

Two phase-specific flags were added on resume, both of them invariant defence rather than new work:

- **13** had modified *both* `scripts/telegram_ratchet.py` and its baseline JSON. The FC-2 ratchet
  is invariant I4 and `src/exceptions/telegram.py` is one of its 4 `telegram_modules` entries, so
  the phase was asked to state exactly what changed in each and why. A re-baseline must be
  deliberate and explained, never incidental — that is the whole point of a ratchet.
- **14** had modified `calendar/page.tsx`, which is where flagged defect **D2** lives (the
  posting-window arithmetic that is wrong for wrap-midnight and 24-hour windows). The phase was
  asked to confirm its change there is import/type only, and to revert it otherwise. D2 is an
  owner ruling, not a cleanup.


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

### The two things Wave 1 taught the sprint

Both cost real time, both are mechanical, and both are written here so Waves 2–4 do not re-learn
them.

#### 1. The DB gates serialise on one cluster-wide advisory lock — do not run phases in parallel

`tests/scripts/conftest.py` holds `SUITE_CLUSTER_LOCK_KEY = 7_532_026` and `admin_conn` polls
`pg_try_advisory_lock` every 5s, raising after `SUITE_LOCK_WAIT_SECONDS = 1200`. **Only one
checkout on the host can run `tests/scripts/` at a time.** Five phases were dispatched in
parallel; three of them independently diagnosed this, one by pinning the hang with
`faulthandler.dump_traceback_later` and naming the holder out of `pg_locks`.

The concrete damage: one phase's orphaned pytest held the lock **idle for 68 minutes** after its
agent died, so two later phases' full-suite runs died at the 20-minute wait with ~950 collection
errors each — all of them the same `RuntimeError`, none of them a real failure. At peak, 17 pytest
processes were queued behind one dead one.

**The rule for the rest of this sprint: full-suite runs are serialised, one phase at a time.** The
runner does them, not the phases. Everything outside `tests/scripts/` is contention-free and
finishes in ~25s, so a phase can self-check with `--ignore=tests/scripts` and leave the gate tree
to the runner.

Housekeeping that followed: 14 stray `storydump_test_*` databases from killed sessions were
dropped once `pg_stat_activity` showed zero `storydump_user` backends and no pytest process
remained. The repo's own reaper ships disarmed on purpose — dropping on a shared host has gone
wrong here before — so this was done by hand, after proving the cluster was quiet, rather than by
arming it.

#### 2. A green local suite is not evidence when the change is to `requirements.txt`

Doc 10 removed eight packages "nothing imports". Its phase ran the full suite locally and got
**3705 passed** — green, and wrong. CI went red at collection:

```
starlette/testclient.py:36  import httpx2 as httpx
E  starlette.exceptions.StarletteDeprecationWarning: Using `httpx` with `starlette.testclient`
   is deprecated; install `httpx2` instead.
!!!!!!!!!!!!!!!!!!! Interrupted: 7 errors during collection !!!!!!!!!!!!!!!!!!!!
```

**`httpx2` is not unimported — `starlette.testclient` imports it**, falling back to `httpx` with a
custom `StarletteDeprecationWarning`, which `pytest.ini`'s `filterwarnings = error` escalates
(its three ignores do not cover a custom warning class).

Two separate reasons the phase could not see it, both worth generalising:

- **Removing a line from `requirements.txt` does not uninstall anything.** The local run used a
  venv that still had the package. Only a fresh install proves a removal.
- **The doc's own clean-venv probe was runtime-only** — it imported `src.api.app`, `src.worker`,
  `src.main`, `storydump_cli.main` and the scripts. `starlette.testclient` is imported by the
  *tests*, so nothing the probe touched could reach it.

The audit's grep was over *this repository's* source. It cannot see a third-party package
importing a name, and "no importer in `src/`" is not "no importer".

**Resolution:** `httpx2` stays, bumped to `2.13.0`. The advisories are fixed in 2.11.0 and 2.12.0
(and httpcore2's in 2.10.0), so all four clear and `pip-audit` can still gate. This is a
deliberate deviation from the doc's "removes names, does not bump versions" — a rule that existed
only because the package was assumed removable. With the premise falsified, both alternatives are
worse: keeping 2.2.0 either leaves four known CVEs live or forces the gate back off.

**The merge gate did its job.** This is precisely the "green CI plus honest evidence was not
sufficient" shape, caught because no phase merges before its CI is observed green.


#### 3. A finding the audit did not have: `greenlet` is absent on Apple Silicon

Surfaced while doc 10 was re-verifying its removal in a *fresh* venv, which is the only kind of
run that can see it. The first fresh-venv attempt came back `555 failed, 3141 passed`, with
**1,252 × `ValueError: the greenlet library is required to use this function`**.

SQLAlchemy declares `greenlet` conditionally:

```
greenlet>=1; platform_machine == "aarch64" or ... "x86_64" or ... "amd64" or ... "win32" ...
```

`arm64` — what an Apple Silicon Mac reports — **is not in that list**. CI's `ubuntu-latest` is
`x86_64`, which is. So a fresh install on this machine silently lacks SQLAlchemy's async support
while CI has it.

Proven pre-existing rather than caused by the pruning, by building a venv from **`origin/main`'s
own** `requirements.txt` and finding `greenlet` equally absent. Correctly **not** fixed inside doc
10 — adding a dependency would be an undeclared behaviour change smuggled into a cleanup PR.

Two consequences:

- **Operationally:** any fresh-venv run on Apple Silicon needs `pip install greenlet` first to
  stand in for CI. Without it, 1,252 failures look like catastrophe and mean nothing.
- **As a finding:** this is real debt the audit could not see, because the audit read the source
  tree and this lives in a dependency's environment markers. Queued in §7 rather than folded in.

The same phase also demonstrated the right shape for this kind of proof. Its new probe asserts
`starlette.testclient.httpx.__name__ == 'httpx2'` under `pytest.ini`'s own filters, **and carries
a negative control** — remove `httpx2`, watch the probe fail, restore it, watch collection
recover. A probe that cannot fail proves nothing, which is the entire lesson of the `GHSA-1234`
placeholder this phase deleted.

One detail worth keeping: `StarletteDeprecationWarning` subclasses **`UserWarning`**, not
`DeprecationWarning`. Warning filters match subclasses, so had it subclassed `DeprecationWarning`,
`pytest.ini`'s `ignore::DeprecationWarning` would have caught it and there would have been no
error at all. The escalation was not a misconfiguration; it was the filter working as written.


### Deploy reachability, verified rather than assumed (Wave 1)

§3a says merged is live here. That was checked against Railway rather than taken on trust, because
this repo has been bitten once by a redeploy re-running a superseded commit.

Both services exist and **both deploy on every merge**:

```
worker      cadbb06c  SUCCESS  2026-09-20 23:14:36
            d6135da2  SUCCESS  2026-09-20 23:01:19
storydump   68aa9e3a  WAITING  2026-09-20 23:14:36
            9ccc4bf7  SUCCESS  2026-09-20 23:01:19
```

One deployment pair per merge, both reaching SUCCESS, with the newest still settling. So the
sprint carries no "merged, NOT live" caveat and owes no manual operator command — and, as §3a
warns, each of those pairs restarted the production posting worker under its 60-second drain.

`railway status --json` names the services (`worker`, `storydump`) without linking one, which is
the read to prefer: `railway link` mutates local state another session depends on.


## 6. Review rounds

_Per round: which findings, the class sweep for each with its command and counts, what closed._

---

## 7. Owner-decision queue

| # | Item | Why it is here |
|---|---|---|
| Q1 | **PR #1334** — the device-native inbound docs | Not a phase of this sprint. Opened and left for the owner. It also carries this epic's `documentation/README.md` index rows (see §1 G1) — if it is abandoned, those rows need re-adding by hand. |
| Q2 | The 12 flagged defects (epic §Questions 1–12) | Each changes behaviour; each wants its own ruling or a bug-fix PR, never a cleanup PR. Carried here for the sprint's duration so none is silently folded in. |
| Q3 | `.claude/settings.json` still names the deleted legacy CLI verbs and denies none of the `storydump` never-run verbs | Owner edit, noted in `CLAUDE.md`. Out of every phase's scope; recorded so it is not lost. |
| Q4 | **`greenlet` is absent from a fresh install on Apple Silicon** — SQLAlchemy's environment marker lists `aarch64`/`x86_64`/`amd64`/`win32` but not `arm64`. CI (`x86_64`) is unaffected; a local fresh venv silently loses async SQLAlchemy and produces ~1,252 spurious failures. Pre-existing on `main`, deliberately not fixed inside a cleanup PR. A real finding the audit could not see, because it lives in a dependency's markers rather than in this tree | Discovered by doc 10's fresh-venv verification |
| Q5 | **`documentation/guides/TEST_COVERAGE.md` is knowingly stale mid-sprint and needs one authoritative recount at the close.** Doc 10 recounted it correctly (158 files / 3,709 tests) — then doc 01 added 10 tests and doc 13 removed 4 files and 88, and the rebased tree collects **3,636**. Nothing enforces the table, so it is documentation rather than a gate; re-deriving it inside every one of sixteen PRs would be churn that is wrong again an hour later. **The sprint's final act recounts it once against the finished tree.** | A snapshot doc in a 16-PR sprint |
| Q6 | Two egress-floor tests cannot run on macOS | `OSError: could not bind on any address out of [('127.0.0.2', 0)]`. Smallest unblocking action: `sudo ifconfig lo0 alias 127.0.0.2`. They run in CI (Linux), so the sprint is not blind to them — but the local baseline excludes them. |

