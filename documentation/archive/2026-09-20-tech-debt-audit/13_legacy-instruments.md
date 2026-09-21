---
title: "Retire the five legacy-tier measurement instruments whose questions closed and whose tables 079 dropped"
type: plan
status: draft
owner: chris
created: 2026-09-20
tags: [plan, tech-debt, priority:medium, tests]
links: []
---

# 13 — The legacy-tier instruments

| | |
|---|---|
| **PR title** | chore: retire the legacy-tier instruments — the questions closed, and 079 took the tables they read (#1216, #943, #790, #942) |
| **Risk** | Low — nothing deployed imports them; CI runs none of them. The one live dependency is a runbook citation, handled in step 5 |
| **Effort** | S (≈2h) |
| **Files modified** | 5 scripts + 4 test files deleted; `src/main.py`, `.github/workflows/ci.yml`, `documentation/operations/legacy-window-close.md`, `CHANGELOG.md` |
| **Findings addressed** | TD-O7 |
| **Depends on** | nothing — the 079 window ran on 2026-09-19, which is what un-blocked this |
| **Blocks** | nothing |

## Summary

Five scripts under `scripts/` were built to answer questions about the legacy tier: can
`posting_history` rows be attributed to an account by time alone (#943 fork A), what does dropping
the legacy `recent_post` locks cost (#943 fork E), is the FC-8 zero-row bar met (#790), what
commands are in production use (#790), and can any deployed process reach the target tier (#942).
Every one of those questions is closed — by ruling, by the tear-out, or by the window — and every
one of the scripts queries tables that migration 079 dropped from production on **2026-09-19**, so
they can no longer run against the system they were written to measure. Together with their tests
that is **3,542 lines**. #1324 set the precedent when it deleted `m1_ladder.py` and
`m1_preflight.py` (1,419 lines) on the same reasoning. What must not change: the instruments that
audit the *live* tree stay, and the one live runbook that cites a constant out of one of these
files keeps that value.

## Findings addressed

| ID | Where | One line |
|---|---|---|
| TD-O7 | `scripts/fork_a_attribution.py` (411) + test (306) | #943 fork A: attribution of `posting_history` by time — the table is gone |
| TD-O7 | `scripts/fork_e_lock_cost.py` (241) + test (63) | #943 fork E: the cost of dropping legacy `recent_post` locks — dropped |
| TD-O7 | `scripts/fc8_gate.py` (205) + test (378) | #790 FC-8 zero-row window-prep halt — the window ran |
| TD-O7 | `scripts/observed_use.py` (525), no test | #790 observed use over `user_interactions` — the table is gone |
| TD-O7 | `scripts/target_reachability.py` (670) + test (743) | #942: can a deployed process reach the target tier — every process *is* the target tier |

## Dependencies

None. This was the one item in the session gated on an owner action — the 079 window — and it ran
on 2026-09-19 (`documentation/operations/legacy-window-close.md:3`; `AGENTS.md` records production's
ledger at 080). Independent of docs 01–12; it touches no module they edit.

## Implementation Plan

### Steps

1. **Record why each question is closed** in the PR body — this is the justification a reviewer
   checks, and it is the whole argument for the deletion:

   | Script | Question | Closed by |
   |---|---|---|
   | `fork_a_attribution.py` | Can `posting_history` rows be attributed to an IG account by time alone? | Owner ruling 2026-09-02: legacy data is not migrated (M.1 abandoned, #1046). 079 dropped `posting_history`. |
   | `fork_e_lock_cost.py` | What does dropping the legacy `recent_post` locks cost? | The locks went with the schema (079). |
   | `fc8_gate.py` | Is the FC-8 zero-row bar met before window prep? | The window ran, 2026-09-19. |
   | `observed_use.py` | Which commands are in production use (`user_interactions`)? | M.3 parity bar deferred (#854); the table is gone (079). |
   | `target_reachability.py` | Can any deployed process reach the target tier? | The tear-out (#1216): there is one tier and every process runs it. |

2. **Confirm nothing imports them** — paste the output:

   ```bash
   for s in fork_a_attribution fork_e_lock_cost fc8_gate observed_use target_reachability; do
     echo "== $s"
     grep -rn "$s" src storydump_cli scripts Makefile railway.toml Procfile .github --include='*.py' --include='*.yml' --include='*.toml' 2>/dev/null | grep -v "^scripts/$s.py"
   done
   ```

   Expected: exactly two hits — `src/main.py:14` (a comment) and `.github/workflows/ci.yml:55`
   (a comment naming `fc8_gate.py` as a *precedent*, not running it). Both are reworded in steps
   3 and 4. Anything else stops the PR.

3. **Reword the `src/main.py` comment** — `src/main.py:12-16`. The eager import stays; only the
   sentence naming a deleted measurement changes.

   Before:
   ```python
   # #942: the target composition root rides the deployed worker artifact.
   # EAGER on purpose, and load-bearing: the import closure is how reachability
   # is measured (scripts/target_reachability.py), and a lazy import is invisible
   # to it (#979). The root's own config (TARGET_DATABASE_URL and friends) is
   # read at RUN time inside src.worker.main, never at import (pinned in
   # tests/src/test_worker_entrypoint.py).
   ```
   After:
   ```python
   # #942: the target composition root rides the deployed worker artifact.
   # EAGER on purpose: a lazy import would make the worker's closure invisible to
   # any import-graph measurement, and it is the closure that makes the artifact
   # the artifact (#979; the reachability instrument that measured it was retired
   # with the legacy tier's questions, #1216). The root's own config
   # (TARGET_DATABASE_URL and friends) is read at RUN time inside src.worker.main,
   # never at import (pinned in tests/src/test_worker_entrypoint.py).
   ```

4. **Reword the CI comment** — `.github/workflows/ci.yml:54-58`: replace "the `fc8_gate.py`
   precedent" with the rule itself, since the exemplar is going.

   Before:
   ```yaml
      # No install step, deliberately: the ratchet is stdlib-only (the
      # fc8_gate.py precedent), so it runs before and independently of the
      # application's dependency tree. A gate that needs the app installed to
   ```
   After:
   ```yaml
      # No install step, deliberately: the ratchet is stdlib-only, so it runs
      # before and independently of the application's dependency tree. A gate
      # that needs the app installed to
   ```
   (The standalone-module rule is still stated by `scripts/migration_runner.py`'s and
   `scripts/telegram_ratchet.py`'s own docstrings — the precedent survives its exemplar.)

5. **Keep the runbook's guard value** — `documentation/operations/legacy-window-close.md:50`
   cites `scripts/observed_use.py`, `EXPECTED_HOST` as "production's endpoint id as the
   repository records it". `documentation/operations/` is a LIVE directory and
   `tests/test_agent_docs.py` fails when a live page names a deleted module path.

   Copy the literal value from `scripts/observed_use.py:78` (the variable `EXPECTED_HOST` — a
   Neon endpoint hostname; **do not retype it, copy it, and do not paste it into the PR
   description**) into the runbook sentence in place of the module citation, and say where it
   came from and when it was read. Suggested wording, with `<VALUE>` replaced by the copied
   string:

   > The guard's first arm is production's endpoint id as the repository recorded it on
   > 2026-09-18 — `<VALUE>` — if the Neon console shows another compute endpoint on the
   > `production` branch, fix the arm before running.

   The window has already run (line 3 of that file records it), so the page is a record; the
   value is preserved for a future PITR rehearsal rather than left pointing at a deleted file.

6. **Delete the five scripts and their four tests** (`observed_use.py` has no test):

   ```bash
   git rm scripts/fork_a_attribution.py tests/scripts/test_fork_a_attribution.py \
          scripts/fork_e_lock_cost.py   tests/scripts/test_fork_e_lock_cost.py \
          scripts/fc8_gate.py           tests/scripts/test_fc8_gate.py \
          scripts/observed_use.py \
          scripts/target_reachability.py tests/scripts/test_target_reachability.py
   ```

7. **Confirm `test_fork_e_lock_cost.py` carries nothing that outlives it.** Its whole content is
   the guard that the script's printed `CODE_DEFAULT_REPOST_TTL_DAYS` matches
   `src/config/defaults.py` — a self-contained loop (the script prints the constant, the test
   checks the script's copy). With the script gone there is no copy to check.
   `tests/src/config/test_defaults.py:49` already pins that `DEFAULT_REPOST_TTL_DAYS` exists.
   **Verify before deleting**: `grep -n "DEFAULT_REPOST_TTL_DAYS" tests/src/config/test_defaults.py`
   must show the constant is named there. If it only checks existence and you want the value
   pinned, that is a one-line addition to `test_defaults.py` in this PR — but note the value
   itself is contested (audit finding A1, flagged), so pin *existence*, not the number.

8. **Keep these — do not touch them:**
   - `scripts/unreachable_capabilities.py` (686 lines) — audits the **live** tree (port commands
     vs the landing envelope, SQL functions, API response fields) and has its own test. It is not
     wired into CI; wiring it is a separate decision, not this PR.
   - `scripts/migration_runner.py`, `schema_parity.py`, `tenancy_gate.py`, `advertised_ddl.py`
     (6 test files), `telegram_ratchet.py` (CI), `scheduling_monitor.py`, `posting_monitor.py`
     (imported by the CLI's `health` verb — `storydump_cli/commands/env.py:24`).

9. **Run the doc guards** — they are the tests most likely to catch a missed reference:
   ```bash
   REQUIRE_TEST_DATABASE=1 pytest tests/test_agent_docs.py tests/test_legacy_cli_gone.py \
       tests/test_deploy_guardrails.py --no-cov
   ```

10. **CHANGELOG** — under `## [Unreleased]` → `### Removed`:

    > **The legacy tier's measurement instruments, whose questions closed with it (#1216, #943,
    > #790, #942).** Five scripts and their four tests — 3,542 lines — are gone:
    > `fork_a_attribution.py` and `fork_e_lock_cost.py` (the #943 forks, answered by the owner's
    > ruling that legacy data is not migrated), `fc8_gate.py` (the FC-8 window-prep halt; the
    > window ran on 2026-09-19), `observed_use.py` (the observed-use measurement over
    > `user_interactions`) and `target_reachability.py` (#942's "can any deployed process reach
    > the target tier" — the tear-out answered it: every process is the target tier). Each queried
    > tables that migration 079 dropped, so none could run against production any more. The
    > runbook `documentation/operations/legacy-window-close.md` now carries the endpoint-guard
    > value itself rather than citing the deleted module for it, and the two comments naming these
    > files (`src/main.py`, the ratchet's CI job) name the rule instead of the retired exemplar.
    > `scripts/unreachable_capabilities.py` stays — it audits the live tree, not the retired one.

## Test Plan

- **Pre-change baseline:** `REQUIRE_TEST_DATABASE=1 pytest --no-cov` on the parent commit — green,
  pass count recorded. After the PR the count drops by exactly the number of tests in the four
  deleted files; record both numbers so the delta is accounted for rather than assumed.
- **The characterization here is the reference sweep, not a test**: step 2's grep is what proves
  nothing imports these modules, and step 9's doc guards are what prove no live page names them.
- **Targeted:**
  `REQUIRE_TEST_DATABASE=1 pytest tests/test_agent_docs.py tests/test_legacy_cli_gone.py tests/test_deploy_guardrails.py tests/src/config/test_defaults.py tests/src/test_worker_entrypoint.py --no-cov`
- **New pins:** none, unless step 7's optional existence assertion is added.
- **Post-change:** the full suite, plus a final `grep -rn "fork_a_attribution\|fork_e_lock_cost\|fc8_gate\|observed_use\|target_reachability" src storydump_cli scripts tests documentation/guides documentation/operations AGENTS.md CLAUDE.md README.md .claude .github` returning nothing. (`documentation/planning/` and `documentation/archive/` keep their references — those pages are history and `documentation/README.md` says so.)

## Verification Checklist

- [ ] baseline `REQUIRE_TEST_DATABASE=1 pytest --no-cov` green on the parent commit, pass count recorded
- [ ] step 2's grep output in the PR: only the two comment hits
- [ ] step 1's closure table in the PR body
- [ ] the runbook keeps the endpoint value (step 5) and no longer names the deleted module
- [ ] `pytest tests/test_agent_docs.py tests/test_legacy_cli_gone.py tests/test_deploy_guardrails.py --no-cov` green
- [ ] full suite green after; pass-count delta equals the deleted files' test count
- [ ] final reference sweep returns nothing outside `documentation/planning/` and `documentation/archive/`
- [ ] `ruff check . && ruff format --check .`
- [ ] manual smoke: `python -m scripts.migration_runner status` (read-only) still runs — proves the surviving standalone scripts are untouched
- [ ] `CHANGELOG.md` entry under `[Unreleased]`
- [ ] nothing in "What NOT To Do" happened

## What NOT To Do

- **Do not delete `scripts/unreachable_capabilities.py`.** It reads the live tree, not the legacy
  one. It looks like a sibling of these five and is not.
- **Do not delete `observed_use.py` before step 5 lands the runbook value.** The endpoint id is
  the rehearsal's fail-closed guard; losing it costs a future PITR rehearsal its first arm.
- **Do not paste the `EXPECTED_HOST` value into the PR description, a commit message or a
  comment thread.** It is production infrastructure identity; it belongs in the tracked runbook
  and nowhere else. Copy it file-to-file.
- **Do not edit `documentation/planning/` or `documentation/archive/` pages** to remove these
  names. Those pages are history and are supposed to name what existed then
  (`documentation/README.md`).
- **Do not pin the repost-TTL *value* in `test_defaults.py`** (step 7). Which number is correct
  is an open question — audit finding A1, flagged for an owner ruling. Pin existence only.
- **Do not wire `unreachable_capabilities.py` into CI here.** Worth doing, separate decision,
  separate PR.
- **Do not run any of these scripts against production to "check one last time."** They read
  tables 079 dropped; there is nothing to read, and `AGENTS.md`'s production rule stands.

## Related

- `00_TECH_DEBT.md` — finding TD-O7.
- `10_dependencies-and-ci.md` — the other "delete what nothing uses" PR, on the packaging side.
- `documentation/planning/2026-09-16-legacy-tear-out/00_EPIC.md` — the epic these instruments
  served; its phase 04 is the window that closed their questions.
- `documentation/operations/legacy-window-close.md` — the runbook step 5 edits.

## Origin

`/artemis-skills:audit tech debt`, 2026-09-20, on `main` at `0966771`. Research:
`tech-debt-2026-09-20/research/orchestrator.md`, finding TD-O7. The 079 window's completion
(2026-09-19) was confirmed against `AGENTS.md` and the runbook during planning — it is what moved
this doc from blocked to executable.
