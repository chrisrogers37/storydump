---
title: "One publish-job mint for approve and resolve_review, one refusal sentence, and a tap that reads in one sitting"
type: plan
status: draft
owner: chris
created: 2026-09-20
tags: [plan, tech-debt, priority:medium, services, worker]
links: []
---

# 07 — Command executors and the tap

| | |
|---|---|
| **PR title** | refactor(commands): one publish-job mint, one `illegal_transition` sentence, and `_tap` split along its own steps |
| **Risk** | Medium — `_tap` is the Telegram write path; bounded by two gates (11 + 9 test classes) and by leaving the nested-transaction seam untouched |
| **Effort** | M (≈5h) |
| **Files modified** | `src/services/target/command_executors.py`, `src/services/target/telegram_dispatch.py`, `CHANGELOG.md` |
| **Findings addressed** | TD-A17 (narrowed), TD-A18 (narrowed) |
| **Depends on** | 03 (extracts the service helpers these call) |
| **Blocks** | nothing |

## Summary

Two of the command port's executors — `approve` and `resolve_review` — mint the same
`publish_pipeline` job with the same serialization key, the same `NO_DEADLINE` and the same
payload, written out twice; one of the two copies carries the comments explaining why, so a
reader of the other cannot see the reasoning. The same refusal sentence is spelled three times in
one file. And `_tap` is 156 lines doing admission, execution, debit and answer in one body.
**This doc is materially narrower than the scan suggested**: re-reading showed the four "helper
pairs" are a deliberate parallel family (supersede vs restate — two different card semantics,
each documented), and the two refusal *gates* share a condition but not their sentences, which
reach the user. Only what is genuinely one thing written twice is planned here.

## Findings addressed

| ID | Where | One line |
|---|---|---|
| TD-A17 (part) | `command_executors.py:430-445` vs `:727-741` | The `publish_pipeline` mint is duplicated; only the first copy carries the comments explaining `NO_DEADLINE` and the travelling dry-run flag |
| TD-A17 (part) | `command_executors.py:683-684`, `:727-728`, `:762-763` | `CommandRefused("illegal_transition", "the review was resolved by someone else first")` spelled 3× |
| TD-A17 (**withdrawn**) | `:177`/`:363`, `:295`/`:323`, `:348`/`:376` | **Not duplicates.** `_supersede_*` strips buttons and writes the outcome (the approve path); `_restate_*` restates by ref and drops the review keyboard (the resolution path). `_restate_everywhere`'s own docstring calls itself "the resolution's door to a card the approve tap already superseded". Merging them would flatten a real distinction |
| TD-A17 (**withdrawn**) | the two refusal gates `:411-427` vs `:694-704` | The conditions match; the sentences differ per verb ("use mark_posted after posting by hand" vs "give up here and post by hand") and are user-facing. A shared gate would either flatten them or take both messages as parameters, which is the duplication again with extra steps |
| TD-A18 (part) | `telegram_dispatch.py:530-534` | `_observe_all`'s two branches both assign `result = seen`, and the first condition is a strict subset of the second — provably dead |
| TD-A18 (part) | `telegram_dispatch.py:324-479` | `_tap` is 156 lines, complexity 14 |
| TD-A18 (**deferred**) | `telegram_dispatch.py:449-463` | The duck-typed `begin_nested` fallback: reachability is **unproven** either way. Step 5 says how to settle it; it is not removed in this PR |

## Dependencies

- **Depends on 03** — it extracts shared service helpers (`restate_everywhere` adoption, the
  evidence-merge SQL) that these executors call. Landing 07 first means re-targeting.
- Blocks nothing.

## Implementation Plan

### Steps

Each step leaves the suite green on its own.

1. **Record the inventory** in the PR:
   ```bash
   grep -n "jobs.enqueue(" src/services/target/command_executors.py
   grep -n "the review was resolved by someone else first" src/services/target/command_executors.py   # 3
   grep -rn "_tap\|_observe_all\|_mint_publish" src tests --include='*.py' | grep -v "^src/services/target/telegram_dispatch.py"
   grep -rn "_tap\|_observe_all\|jobs.enqueue" tests/mutations/*.sh    # must be empty
   ```

2. **One publish-job mint.** The two blocks differ only in how the intent id is spelled
   (`str(intent["id"])` at `:443` vs the local `intent_id` at `:738` — the same value) and in
   that the first carries the comments. Extract, keeping every comment:

   ```python
   async def _mint_publish_job(session, intent: dict[str, Any], command: Command) -> None:
       """The `publish_pipeline` job for an intent entering the ladder — the one
       mint `approve` and `resolve_review` both use.

       It was written out in both (the tech-debt audit, 2026-09-20), and only
       `approve`'s copy carried the two reasons below, so a reader of the
       resolution path could not see why the deadline is absent or why the
       dry-run flag travels.
       """
       await jobs.enqueue(
           session,
           kind="publish_pipeline",
           workspace_id=command.workspace_id,
           serialization_key=f"ig:{intent['provider_account_ref']}",
           # The pipeline's ceiling is its own (`05:38`: deadline = slot end; the
           # slot may be a day away) — the loop's deadline would end a deferred
           # publish on its first escaped error. Attempts still bound it.
           deadline_seconds=jobs.NO_DEADLINE,
           # The dry-run decision travels WITH the job: what the tapper was told
           # is what the run does, whatever the flag says by the time it runs.
           payload={
               "v": 1,
               "intent_id": str(intent["id"]),
               "dry_run": bool(intent.get("dry_run_mode")),
           },
       )
   ```
   `approve` (`:430-445`) and `resolve_review` (`:727-741`) each become
   `await _mint_publish_job(session, intent, command)`.

   **Before editing `resolve_review`, confirm the two ids are the same value**:
   `sed -n '694,741p' src/services/target/command_executors.py` and check that its `intent_id`
   derives from the same `intent` row. If it does not, the finding is withdrawn and the two mints
   stay.

   Tests: `tests/src/services/target/test_command_executors.py`,
   `tests/scripts/test_command_executors_gate.py`.

3. **One refusal sentence.** Module-level constant beside the other literals in the file:
   ```python
   _RESOLVED_BY_SOMEONE_ELSE = "the review was resolved by someone else first"
   ```
   and the three raise sites (`:683`, `:727`, `:762`) use it. The reason code
   (`"illegal_transition"`) is already shared vocabulary; only the sentence is copied. Doc 01
   moves cross-module constants — this one is local to the file, so it belongs here; check doc
   01's Steps first to be sure it is not already claimed.

4. **Delete `_observe_all`'s dead branch** — `telegram_dispatch.py:530-534`:

   Before:
   ```python
               if seen.handled and not result.handled:
                   result = seen
               elif not result.handled:
                   result = seen
   ```
   After:
   ```python
               if not result.handled:
                   result = seen
   ```
   This is provable, not a judgement: both arms assign the same thing, and
   `seen.handled and not result.handled` implies `not result.handled`, so the `elif` already
   covers every case the `if` did. Tests:
   `tests/src/services/target/test_telegram_dispatch.py::TestGroupMessagesReachTheMembershipStep`
   (`:128`), `::TestABareStartInAGroupIsSpeechNotAGreeting` (`:185`).

5. **Settle the `begin_nested` fallback — do not remove it on assumption.** `:449-463` chooses
   between a savepoint and a bare call via `getattr(conn, "begin_nested", None)`. A grep shows
   the only test fakes that define `begin_nested` are in `test_publish_cap.py:49` and
   `test_work_loop.py:367` — neither is a `_tap` caller — which tells us nothing about whether
   any `_tap` caller lacks it. To settle it: add a temporary `assert callable(begin_nested)` at
   `:449`, run
   `REQUIRE_TEST_DATABASE=1 pytest tests/src/services/target/test_telegram_dispatch.py tests/scripts/test_w4_tap_gate.py tests/src/api/test_webhook_ingress_route.py --no-cov`,
   and record what happens. **If every test passes**, note in the PR that the fallback appears
   unreachable under test — and still leave it, because the production caller path
   (`src/api/routes/webhooks.py` → `TelegramDispatcher`) is what matters and the tests are not
   proof of it. Removing it is a separate, behaviour-adjacent PR with its own evidence. Remove
   the temporary assert before committing.

6. **Split `_tap` along the steps its body already has** — `:324-479`. Reading it, the sequence
   is: resolve the tenant → build the `Command` → admission (the window/limit guard) → execute
   with the debit → map the outcome to an answer. Extract the middle two as
   `_execute_with_debit(self, conn, command, tenant, window, limit)` — the block at `:445-463`
   **including the `begin_nested` choice verbatim** — and `_answer_for(...)` for the
   outcome-to-sentence mapping if it is a contiguous block. Leave the `except` arms
   (`TenantResolutionError`, `CommandRefused`, `_AdmissionExhausted`) in `_tap`: they are the
   function's error contract and moving them changes which frame catches what.

   **Do not change any tap outcome string.** `TAP_OUTCOMES` is pinned and `storydump`'s renderer
   reads these; audit finding C1 already concerns a mismatch between what the API reports and
   what the CLI renders, and is flagged.

   Tests: `::TestTheTap` (`:343`), `::TestTapAdmission` (`:523`), `::TestEveryReasonHasAnAnswer`
   (`:493`), `::TestATapIsCheap` (`:602`), `::TestTheReviewTaps` (`:640`), and in the gate
   `::TestATapFlipsOnceAndEditsEveryCard` (`:252`), `::TestTwoTapsRacingOnOneCard` (`:566`),
   `::TestTapAdmissionOnTheLedger` (`:748`).

7. **CHANGELOG** — under `## [Unreleased]` → `### Changed`:

   > **One publish-job mint, one refusal sentence, one observation rule (#1216).** `approve` and
   > `resolve_review` each wrote out the same `publish_pipeline` job — same serialization key,
   > same absent deadline, same payload — and only `approve`'s copy carried the comments saying
   > why, so the resolution path's reader could not see the reasoning; both now call one
   > `_mint_publish_job`. The sentence a caller gets when someone else resolved the review first
   > was spelled three times and is now a constant. In the Telegram dispatcher, `_observe_all`
   > had two branches assigning the same value under conditions where one implied the other, and
   > `_tap` — 156 lines of admission, execution, debit and answer — is split along those steps
   > with its error contract and its nested-transaction choice untouched. What the scan read as
   > four duplicated helper pairs is not: `_supersede_*` and `_restate_*` are two different card
   > operations, and they stay two.

## Test Plan

- **Pre-change baseline:** `REQUIRE_TEST_DATABASE=1 pytest --no-cov` on the parent commit —
  green, pass count recorded; identical after.
- **Characterization**, named per step above:
  `tests/src/services/target/test_telegram_dispatch.py` (11 classes),
  `tests/scripts/test_w4_tap_gate.py` (9 classes, 1,089 lines),
  `tests/src/services/target/test_command_executors.py`,
  `tests/scripts/test_command_executors_gate.py`.
- **Targeted, after each step:**
  ```bash
  REQUIRE_TEST_DATABASE=1 pytest tests/src/services/target/test_command_executors.py \
      tests/scripts/test_command_executors_gate.py \
      tests/src/services/target/test_telegram_dispatch.py \
      tests/scripts/test_w4_tap_gate.py --no-cov
  ```
- **Also run the webhook route**, since `_tap` is reached through it:
  `REQUIRE_TEST_DATABASE=1 pytest tests/src/api/test_webhook_ingress_route.py --no-cov`.
- **New pins:** none.
- **Post-change:** the full suite; pass count compared.

## Verification Checklist

- [ ] 03 has landed
- [ ] baseline green on the parent commit, **pass count recorded**
- [ ] step 2's id-equivalence check done and recorded (or the finding withdrawn)
- [ ] step 5's reachability probe run, its result recorded, **and the temporary assert removed**
- [ ] call-site audit incl. `tests/mutations/*.sh` (empty, or edited in the same commit)
- [ ] each step's named test classes green before the next step starts
- [ ] full suite green, **pass count identical**
- [ ] `ruff check . && ruff format --check .`; report `_tap`'s new `C901`
- [ ] manual smoke: none — this is the Telegram write path and `AGENTS.md` forbids driving the bot by hand. The gates are the evidence; say so in the PR
- [ ] `CHANGELOG.md` entry under `[Unreleased]`
- [ ] nothing in "What NOT To Do" happened

## What NOT To Do

- **Do not merge `_supersede_*` with `_restate_*`.** They are two card operations — buttons
  stripped and outcome written, versus restated by ref with the review keyboard dropped. The
  scan read the parallel naming as duplication; the docstrings say otherwise.
- **Do not extract the two refusal gates into one.** Their sentences differ per verb and reach
  the person tapping. Flattening them changes what a user is told; parameterising them re-creates
  the duplication.
- **Do not remove the `begin_nested` fallback** on the strength of a green test run (step 5).
- **Do not touch `TAP_OUTCOMES` or any outcome string.** Audit finding C1 concerns exactly this
  surface and is flagged for a ruling; a rename here collides with it.
- **Do not move the `except` arms out of `_tap`.** They are its error contract.
- **Do not type `remove_member`'s `LookupError`/`ValueError`** while you are in this file — doc
  01 owns that decision and it is conditional on the caller's behaviour being identical.
- **Do not tap a live card, or run the bot, to check anything.** `AGENTS.md`: Telegram Web is
  view-and-screenshot only, and all bot interaction goes through the database or the user's own
  device.

## Related

- `00_TECH_DEBT.md` — findings TD-A17, TD-A18, and the Corrections table this doc adds four rows to.
- `01_one-spelling.md` — owns cross-module constants; check it before adding step 3's local one.
- `03_rule-of-three-services.md` — lands first.
- `06_publish-pipeline-shape.md` — the same axis in the pipeline.
- `.claude/rules/telegram.md` — the adapter, the webhook, the outbox, the approval card.

## Origin

`/artemis-skills:audit tech debt`, 2026-09-20, on `main` at `0966771`. Research:
`tech-debt-2026-09-20/research/pipeline-worker.md`, findings TD-A17 and TD-A18. Both executors'
mints and gates, the three refusal sites, the four claimed helper pairs, `_observe_all`'s branches
and the `begin_nested` seam were re-opened while writing this plan — four sub-findings were
withdrawn and one deferred as a result.
