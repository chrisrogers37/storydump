---
title: "Decompose the publish ladder without changing a verdict — one readiness router, one wait class, one finalize"
type: plan
status: draft
owner: chris
created: 2026-09-20
tags: [plan, tech-debt, priority:medium, pipeline, worker]
links: []
---

# 06 — The publish pipeline's shape

| | |
|---|---|
| **PR title** | refactor(pipeline): one readiness router, one wait-class object, one finalize — `_ladder`, `_retry_or_poison` and `_run_job` stop repeating themselves |
| **Risk** | Medium — this is the module that posts. Bounded by two gates that pin every branch being moved (32 test classes across 4,583 lines) and by an ordering that keeps the suite green after each step |
| **Effort** | L (≈1.5 days) |
| **Files modified** | `src/services/target/publish_pipeline.py`, `src/services/target/work_loop.py`, `CHANGELOG.md` |
| **Findings addressed** | TD-A7, TD-A13, TD-A14 |
| **Depends on** | 03 (extracts the service helpers this module calls), 05 (re-homes the session factories `_run_job` uses) |
| **Blocks** | nothing |

## Summary

`publish_pipeline.py` is 2,112 lines and its `_ladder` is 520 of them (110 statements,
cyclomatic complexity 31). Two of its rungs — `container_created` and `publish_called` — route the
readiness verdict through **byte-identical** code, including a 12-line inline `ContainerDead`
error dict. `_retry_or_poison` carries ten keyword parameters because three wait classes are
threaded by hand through thirteen call sites. In `work_loop.py`, `_run_job` is 155 lines with the
same `finalize_job` call written into both of its branches. None of this changes what the pipeline
decides: the point of the PR is that the decision is written once. **What must not change:** which
verdict leads to which outcome, the order of the rungs, when each transaction opens and commits,
the `wait` class recorded on a float, and the audited counters. A transaction never spans a
provider call — no extraction may move code across a commit boundary.

## Findings addressed

| ID | Where | One line |
|---|---|---|
| TD-A7 | `publish_pipeline.py:1316-1357` vs `:1359-1400` | The readiness-verdict routing (three branches + the `ContainerDead` dict) is identical in both rungs |
| TD-A13 | `publish_pipeline.py:714-730`, 13 call sites | `_retry_or_poison` takes 10 keyword parameters threading three wait classes by hand |
| TD-A14 | `work_loop.py:869-1024`, blocks `:894-905` and `:919-927` | `_run_job` is 155 lines; the `finalize_job` call is written into both branches |
| — | `publish_pipeline.py:503-556` `_admit` | Included: its own docstring already names three responsibilities, which are the seams |

## Dependencies

- **Depends on 03** — it extracts `restate_everywhere` adoption and the `customer_notified`
  evidence-merge helper out of this same file. Landing 06 first would force 03 to re-target moved
  code.
- **Depends on 05** — `_run_job` calls `self._session_for`, built by `make_session_for`, which 05
  moves to `unit_of_work`. Landing 05 first means this PR edits `_run_job` once.
- Blocks nothing. It is the terminal node of the `01 → 02 → 05 → 06` path.

## Implementation Plan

### Steps

Each step leaves the suite green on its own. Do not batch them.

1. **Record the baseline and the call-site inventory** in the PR:

   ```bash
   grep -n "_retry_or_poison(" src/services/target/publish_pipeline.py        # 1 def + 13 calls
   grep -rn "_ladder\|_retry_or_poison\|_await_ready\|_admit" src tests --include='*.py' | grep -v "^src/services/target/publish_pipeline.py"
   grep -rn "_retry_or_poison\|_ladder\|_run_job\|finalize_job" tests/mutations/*.sh
   ```
   The last grep must be empty. If it is not, those shell batteries embed the source text and
   every rename in this PR must be applied to them in the same commit (a sibling doc found three
   such sites for a different symbol — `tests/mutations/cli_v2_01.sh:118,120`,
   `cli_v2_02.sh:75`).

2. **Extract the readiness router** — the identical part of the two rungs. Verified: the three
   verdict branches at `:1317-1348` and `:1371-1399` are the same code, including the
   `ContainerDead` dict. What differs is only what surrounds them — rung A advances the DB step
   afterwards, rung B has a permit-resolution guard before.

   New module-level helper, placed directly above `_ladder`:

   ```python
   async def _route_readiness(
       uow, ctx: _Ctx, meta, sleep, backoff_seconds, now_fn
   ) -> Optional[str]:
       """One bounded readiness segment, routed. Returns the outcome to stop
       with, or None when the container is ready and the caller continues.

       Both rungs that poll a container — `container_created` and
       `publish_called` — routed the verdict through identical code, the
       `ContainerDead` error dict included (the tech-debt audit, 2026-09-20).
       A verdict that means one thing on one rung and another on the next is
       exactly the drift a second copy produces, so the routing lives once.
       """
       verdict = await _await_ready(ctx, meta, sleep)
       if verdict == "dead":
           # The container is definitively gone; a NEW one needs a fresh
           # create (and a fresh generation) from the still-valid transit
           # asset — step back, retry on the ladder.
           return await _retry_or_poison(
               uow,
               ctx,
               backoff_seconds,
               now_fn,
               step_back_to="transit_uploaded",
               error={
                   "v": 1,
                   "error": {
                       "type": "ContainerDead",
                       "code": None,
                       "message": "Meta reported the container ERROR or EXPIRED —"
                       " the media at the delivery URL could not be processed",
                   },
               },
           )
       if verdict == "unauthorized":
           return await _retry_or_poison(
               uow,
               ctx,
               backoff_seconds,
               now_fn,
               error=_error_of(ctx.poll_error),
               poison_now=True,
           )
       if verdict == "pending":
           return await _retry_or_poison(uow, ctx, backoff_seconds, now_fn)
       return None
   ```

   Rung A becomes (`:1316-1357` → ):
   ```python
       if step == "container_created":
           stop = await _route_readiness(uow, ctx, meta, sleep, backoff_seconds, now_fn)
           if stop is not None:
               return stop
           async with _leased_tx(uow, ctx.job) as session:
               await session.execute(
                   text(
                       "UPDATE post_intents SET publish_step = 'container_ready'"
                       " WHERE id = :intent AND state = 'publishing'"
                   ),
                   {"intent": ctx.intent_id},
               )
           step = "container_ready"
   ```
   Rung B (`:1359-1400` → ) keeps its permit guard verbatim, then:
   ```python
           stop = await _route_readiness(uow, ctx, meta, sleep, backoff_seconds, now_fn)
           if stop is not None:
               return stop
           step = "container_ready"
   ```

   **Note for the builder:** `_await_ready`'s docstring at `:1583` says it returns
   `'ready' | 'dead' | 'pending'`, but both rungs also branch on `"unauthorized"`. Do not change
   the code; fix the docstring to name all four in this step, since you are moving its only two
   callers.

   Tests: `tests/scripts/test_l5_pipeline_gate.py::TestTheReadinessPoll` (`:817`),
   `::TestADeadContainerIdDoesNotOutliveItsContainer` (`:1458`), `::TestKillResume` (`:1070`),
   `::TestColdEntryAtEveryCheckpoint` (`:1031`). Run these before moving on.

3. **Collapse `_retry_or_poison`'s wait threading into one object** — `:714-730`. The ten
   keyword parameters fall into two groups: the *wait* description (`wait`, `attempt`, `spent`,
   `counters`, `restore_attempt`) and the *outcome* description (`resolve_op_id`,
   `resolve_response`, `step_back_to`, `error`, `poison_now`). Introduce a frozen dataclass for
   the first group only — it is the one threaded by hand — leaving the outcome keywords alone:

   ```python
   @dataclass(frozen=True)
   class _Wait:
       """How a float is audited: its class, its rung index and the story's
       counters (plan 03 D3). Five parameters travelled together through
       thirteen call sites of `_retry_or_poison`; one object is the same data
       with one name (the tech-debt audit, 2026-09-20)."""

       kind: str = "retry"
       attempt: Optional[int] = None
       spent: Optional[bool] = None
       counters: Optional[dict] = None
       restore_attempt: bool = False
   ```

   `_retry_or_poison` takes `wait: _Wait = _Wait()` in place of the five, and its body reads
   `wait.kind`, `wait.attempt`, … . **Rename carefully**: the existing parameter is `wait: str`,
   so `wait.kind` is where today's `wait` value goes. Update all 13 call sites — `:1047`,
   `:1120`, `:1216`, `:1254`, `:1275`, `:1322`, `:1339`, `:1348`, `:1373`, `:1390`, `:1399`,
   `:1462`, `:1488`. The three that pass no wait keywords (`:1348`, `:1399`, and whichever of the
   others uses the bare four-argument form) need no edit at all.

   This step must not change a single audited value: the same `wait` string, the same attempt
   index, the same counters dict reach `audit_events`.
   Tests: `::TestTheFloatsSafetyNets` (`:2638`), `::TestError9` (`:1278`),
   `::TestDefinitiveFailureClassification` (`:1343`).

4. **Split `_admit` along the seams its own docstring names** — `:503-556`. The docstring already
   says "cancel honor, the §8 advisory pre-check, then the §4 flip", which is three helpers:
   `_honour_cancel` already exists and is called at `:509`; extract `_advisory_precheck(...)` and
   `_flip_to_publishing(...)` from the remaining body, leaving `_admit` as the three calls plus
   its early returns. Keep the comment at `:512-515` (the re-entering-from-a-wait rule) attached
   to the pre-check helper — it explains that branch, not the gate.
   Tests: `::TestThePrecheck` (`:1567`), `::TestTheFlipIntegration` (`:476`),
   `::TestRoutingAndCancel` (`:1517`), `::TestPauseAndDryRun` (`:883`).

5. **One finalize in `_run_job`** — `work_loop.py:894-905` and `:919-927`. Verified: the
   `finalize_job` call and its `terminal_state` ternary are identical; the branches differ only
   in whether they open a session (`:895` does, `:919` reuses the caller's). So the helper takes
   the session:

   ```python
   async def _finalize(session, job, *, undeliverable: bool) -> None:
       """The job's terminal state from its outcome — one spelling.

       Written into both arms of `_run_job` (the executor that owns its own
       transaction and the one that does not), which is how the two could
       come to disagree about what `UNDELIVERABLE` means (the tech-debt
       audit, 2026-09-20)."""
       await jobs.finalize_job(
           session,
           job["id"],
           job["lease_token"],
           terminal_state="review_required" if undeliverable else "succeeded",
       )
   ```
   Branch A becomes:
   ```python
                   if outcome is not jobs.SELF_FINALIZED:
                       async with self._session_for(job) as session:
                           await _finalize(session, job, undeliverable=undeliverable)
   ```
   Branch B:
   ```python
                       if outcome is not jobs.SELF_FINALIZED:
                           await _finalize(session, job, undeliverable=undeliverable)
   ```
   Tests: `tests/src/services/target/test_work_loop.py::TestAnExecutorThatFinalizesItself`
   (`:1318`), `::TestAnExecutorThatOwnsItsTransactions` (`:1495`),
   `::TestAJobThatReachedNobodyIsNotASuccess` (`:282`), `::TestADeadPublishJobParksItsStory`
   (`:1617`).

6. **Extract the parked-kind arm of `_run_job`** — `:872-893`, the `entry is None` / `Parked`
   handling, into `_run_parked(self, job, entry)`. It is a self-contained 20-line concern
   (log, reschedule, return) that has nothing to do with running an executor, and removing it
   takes `_run_job` under 120 lines. Tests: `::TestSeamAbsenceParksTheDependentKind` (`:122`),
   `::TestRegistryCoversTheSchema` (`:59`).

7. **Do not decompose `_ladder` further in this PR.** After steps 2–4 it loses roughly 90 lines
   and one complexity cluster. Splitting the rung loop itself is a second PR with its own
   evidence — say so in the PR body so a reviewer does not ask for it.

8. **CHANGELOG** — under `## [Unreleased]` → `### Changed`:

   > **The publish ladder decides the same things, written once (#1216).** The two rungs that
   > poll a container — `container_created` and `publish_called` — routed the readiness verdict
   > through identical code, the twelve-line `ContainerDead` error dict included; that routing is
   > now one `_route_readiness` and a verdict cannot come to mean two things. `_retry_or_poison`
   > took ten keyword arguments because five of them described one thing — how a float is
   > audited — and travelled together through thirteen call sites; they are one `_Wait` object,
   > with the same class, attempt index and counters reaching `audit_events` as before. `_admit`
   > splits along the three responsibilities its own docstring already named, and in the work
   > loop `_run_job`'s `finalize_job` call — written into both of its arms — is one `_finalize`.
   > No verdict, ordering, transaction boundary or audited value changed; the L5 pipeline gate
   > and the work-loop suite pin every branch that moved.

## Test Plan

- **Pre-change baseline:** `REQUIRE_TEST_DATABASE=1 pytest --no-cov` on the parent commit —
  green, pass count recorded. The count must be identical after: this PR moves code and adds no
  assertions.
- **The characterization tests** are `tests/scripts/test_l5_pipeline_gate.py` (2,939 lines, 15
  test classes) and `tests/src/services/target/test_work_loop.py` (1,644 lines, 17 classes). Each
  step above names the classes that pin the branch it moves. **Run those named classes after that
  step, before starting the next** — that is what makes the PR bisectable.
- **Targeted, after every step:**
  ```bash
  REQUIRE_TEST_DATABASE=1 pytest tests/scripts/test_l5_pipeline_gate.py \
      tests/src/services/target/test_work_loop.py --no-cov
  ```
- **Whole-worker sweep before the PR opens:**
  ```bash
  REQUIRE_TEST_DATABASE=1 pytest tests/scripts/test_w1_worker_gate.py \
      tests/scripts/test_outbox_sender_gate.py tests/scripts/test_jobs_lease_gate.py \
      tests/scripts/test_scheduler_clock_gate.py tests/src/test_worker.py --no-cov
  ```
- **New pins:** none. If a step tempts you to add one, it is a sign the step changed behaviour.
- **Post-change:** the full suite, pass count compared to the baseline number.

## Verification Checklist

- [ ] 03 and 05 have landed
- [ ] baseline `REQUIRE_TEST_DATABASE=1 pytest --no-cov` green on the parent commit, **pass count recorded**
- [ ] step 1's `tests/mutations/*.sh` grep run and empty (or the batteries edited in the same commit)
- [ ] each step's named test classes green **before the next step starts**
- [ ] call-site audit: all 13 `_retry_or_poison` sites updated; `grep -n "_retry_or_poison(" src/services/target/publish_pipeline.py` shows 1 def + 13 calls
- [ ] full suite green after, **pass count identical to the baseline**
- [ ] `ruff check . && ruff format --check .`; report the new `C901` numbers for `_ladder` and `_run_job`
- [ ] manual smoke: none — this system posts to Instagram and the pipeline is not exercised by hand. The gates are the smoke test; say so in the PR rather than claiming a manual check
- [ ] `CHANGELOG.md` entry under `[Unreleased]`
- [ ] nothing in "What NOT To Do" happened

## What NOT To Do

- **Do not run the pipeline against production, or run `python -m src.main`, to "see it work."**
  This module posts to Instagram. The gates are the evidence.
- **Do not fix the 7-vs-30-day repost TTL** (`repost_ttl_days_default=7` at `:263` vs
  `DEFAULT_REPOST_TTL_DAYS = 30`) while you are in this file. It is audit finding A1, flagged for
  an owner ruling, and it changes how long a story's media stays locked.
- **Do not move code across a commit boundary.** `_leased_tx` blocks and the checkpoint-then-call
  rule are the pipeline's correctness; an extraction that pulls a provider call inside an open
  transaction is a defect, not a refactor.
- **Do not "simplify" the `publish_called` permit guard** (`:1365-1371`, the `ValueError` on a
  succeeded permit). It is an assertion about an invariant the terminal transaction maintains;
  its unreachability is the point.
- **Do not collapse the outcome keywords** (`resolve_op_id`, `resolve_response`, `step_back_to`,
  `error`, `poison_now`) into `_Wait`. They describe what happened, not how the float is
  audited, and each call site sets a different one.
- **Do not change `_await_ready`'s return values** while fixing its docstring (step 2). The code
  is right and the docstring is incomplete.
- **Do not decompose the rung loop itself** (step 7).
- **Do not batch the steps.** A decomposition PR that is green only at the end cannot be
  bisected, and this is the module that posts.

## Related

- `00_TECH_DEBT.md` — findings TD-A7, TD-A13, TD-A14.
- `03_rule-of-three-services.md`, `05_imports-and-homes.md` — land first.
- `07_executors-and-tap.md` — the same axis in `command_executors.py` and `telegram_dispatch.py`.
- `.claude/rules/scheduler.md` — the worker's clock, jobs and publish pipeline.

## Origin

`/artemis-skills:audit tech debt`, 2026-09-20, on `main` at `0966771`. Research:
`tech-debt-2026-09-20/research/pipeline-worker.md`, findings TD-A7, TD-A13, TD-A14. Both verdict
blocks, both finalize blocks, all 13 `_retry_or_poison` call sites, `_admit`'s docstring seams and
the 32 characterization test classes were re-opened and verified while writing this plan; the
stale `_await_ready` docstring was found in that pass.
