---
title: "Delete the dead sync writer lane and the unreachable surfaces, and re-aim its RLS gate at the live writers"
type: plan
status: draft
owner: chris
created: 2026-09-20
tags: [plan, tech-debt, priority:high, services, tests]
links: []
---

# 02 — Delete the dead sync writer lane and the unreachable surfaces, and re-aim its RLS gate at the live writers

| | |
|---|---|
| **PR title** | Delete the dead sync writer lane and the unreachable surfaces, and re-aim its RLS gate at the live writers |
| **Risk** | Medium — the deletions are provably unreachable from `src/`, but the gate re-homing rewrites a security-adjacent test file and may surface real divergences in the live path |
| **Effort** | L (≈ 10–14 hours; the deletions are ~3, the gate port is the rest) |
| **Files deleted** | `src/services/target/{identity_provisioning,workspace_provisioning,web_sessions,sync_tx}.py`, `src/exceptions/identity.py`, `src/exceptions/telegram.py`, `tests/src/services/target/test_drive_adapter.py` |
| **Files modified** | `src/services/target/{work_loop,publish_pipeline,rate_counters,publish_cap,scheduler,telegram_dispatch,drive_adapter,google_drive_adapter,drive_credentials,google_drive_oauth,outbox,invitation_cards,reconciler,intent_ledger}.py`, `src/exceptions/{__init__,tenancy}.py`, `src/api/app.py`, `src/utils/{datetime_utils,logger,encryption}.py`, `scripts/telegram_ratchet_baseline.json`, `tests/scripts/conftest.py`, `tests/scripts/test_identity_writers.py` (rewritten), plus the pins named per step |
| **Findings addressed** | TD-B1/TD-C3, TD-C4, TD-A16, TD-B5, TD-C16, TD-A3, TD-A6 |
| **Depends on** | 01 (`01_one-spelling.md`) |
| **Blocks** | 05 (`05_imports-and-homes.md`), 12 (`12_stale-words-and-names.md`) |

## Summary

678 lines of the authentication plane — session minting, identity upsert, tenant
mint — are a psycopg2 twin of the live async writers that **no `src/` path
calls**. They are kept alive by one file: `tests/scripts/test_identity_writers.py`,
the gate that drives writers as `svc_ingress` under the replayed RLS. So the
repo's one RLS gate on sign-in is pointed at code production does not run, while
the code it does run has no gate at all — and the two have already drifted
(`sessions.resolve` refuses a disabled user; `web_sessions.authenticate_session`
does not). Beside that lane sit a second dead Telegram exception module, a dead
Drive stub, and about a dozen functions with no caller.

This PR deletes what nothing reaches and **re-aims the gate**, which is the
load-bearing half: the assertions that transfer verbatim, the ones that were
only ever true of the dead twin, and the one new fixture the live writers need
(they run in the user plane, before a workspace exists, where `in_tenant`'s
mandatory `tenant_id` does not apply).

**Nothing in `src/` changes behaviour.** Every deletion is preceded by a grep
whose expected result is zero `src/` callers, printed in its step. Where the
port reveals that the live path behaves differently from the dead one, that is
**recorded as a finding, not fixed here** — see "What NOT To Do".

## Findings addressed

| ID | Where | One line |
|---|---|---|
| TD-B1 / TD-C3 | `identity_provisioning.py`, `workspace_provisioning.py`, `web_sessions.py`, `sync_tx.py`, `src/exceptions/identity.py`, `tenancy.py:81-101` | The sync writer lane has zero `src/` callers and is what the RLS gate proves; the live async twin has no gate and the two have drifted. |
| TD-C4 | `src/exceptions/telegram.py:7-98` | `AmbiguousDeliveryError`/`ChatMigratedError`: zero references anywhere, kept alive by a set-equality entry in `scripts/telegram_ratchet_baseline.json`. |
| TD-A16 | `rate_counters.py:43`, `publish_cap.py:317`, `scheduler.py:95`, `telegram_dispatch.py:69`, `:106`, `:129`, `work_loop.py:83`; test-only: `outbox.py:992`, `:1468`, `invitation_cards.py:110`, `reconciler.py:51`, `:377`, `intent_ledger.py:66` | Dead and test-only surfaces, ruled per item below. |
| TD-B5 | `drive_adapter.py:107`, `:125`, `:178-203`, `:236-293`; `google_drive_adapter.py:778-836`, `:950-953` | Half of `drive_adapter.py` is a dead seam; both adapters' headers describe a world before P3/P5 landed. |
| TD-C16 | `app.py:544-547`; `datetime_utils.py:29`, `logger.py:74`, `encryption.py` `rotate` | Tear-out leftovers with zero production callers. |
| TD-A3 | `work_loop.py:525-536` vs `:649-658` | The first Drive-park assignment is overwritten unconditionally; its eight-line rationale never reaches the registry. |
| TD-A6 | `publish_pipeline.py:465-468`, inlines at `:526-527`, `:580-581`, `:1414-1415` | `_next_slot` has zero callers while its expression is inlined three times — **adopt**, do not delete. |
| ~~`workspaces.list_bindings` / `list_invitations`~~ | `workspaces.py:347`, `:356` | **Withdrawn.** The brief called these zero-reference. They are not: `src/api/routes/v1.py:469` and `:478` pass them as the reader to the list door. Verified by `grep -rn "list_bindings\|list_invitations" src storydump_cli scripts tests`. Do not delete. |
| ~~`drive_credentials.provider_from_engine`~~ | `drive_credentials.py:300` | **Withdrawn.** Two live callers: `src/worker.py:845` and `src/api/routes/v1.py:883`. Verified by the same grep. Do not delete. |
| `WorkerConfig.retry_backoff_seconds` | `work_loop.py:83` | Zero `src/` readers, but **one test reader** — `tests/scripts/test_w1_worker_gate.py:202`. Deletion requires rewriting that assertion; step 3f says how. |

## Dependencies

- **Depends on 01.** Doc 01 folds hand-copied constants back to their owners and
  explicitly *names* `work_loop.py:83 retry_backoff_seconds` without deleting it,
  so that doc's diff stays provably value-preserving. This doc deletes it.
  Landing 02 first would leave 01 folding constants in files this doc is
  rewriting.
- **Blocks 05** (`05_imports-and-homes.md`, TD-A12/B8): the in-function-import
  census and the session-factory re-homing both count imports in modules this doc
  deletes.
- **Blocks 12** (`12_stale-words-and-names.md`): the stale-prose sweep must run
  after the modules whose prose is stale are gone.

## Implementation Plan

### Steps

Steps 1–8 are ordered smallest-blast-radius first, each an independent commit.
Step 9 (the gate re-homing) is the one that needs a running test database and a
real review. Step 10 is the doc audit, step 11 the CHANGELOG.

---

1. **The dead second Drive-park block (TD-A3)** — `src/services/target/work_loop.py:525-536`.

   Verified: `registry["sync_media_source"]` and `registry["first_ingest_chunk"]`
   are assigned here when `deps.drive is None`, then assigned **unconditionally**
   at `:653-658` from `_NO_DRIVE`. The first assignment cannot survive; the
   operator reading a park reason in a log can never see the sentence at `:533-535`.

   Before (`:525-536`):
   ```python
       # W6's two kinds are seam-blocked rather than unbuilt, and the difference is
       # visible to whoever reads the park reason: an executor that does not exist
       # needs building, an absent seam needs WIRING. Naming the seam is the
       # contract W6 parks behind (#982) — a silent park would be indistinguishable
       # from a kind nobody has started.
       if deps.drive is None:
           for _kind in ("sync_media_source", "first_ingest_chunk"):
               registry[_kind] = Parked(
                   "no Drive read seam configured (WorkerDeps.drive is None;"
                   " build-path #982) — the executor is blocked on the seam, not"
                   " unwritten"
               )
   ```
   After: **deleted.** Move the rationale comment's substance to `_NO_DRIVE`
   (`:649-652`), which is the reason that actually ships:
   ```python
       # Seam-blocked, not unbuilt, and the difference is visible to whoever reads
       # the park reason: an executor that does not exist needs building, an absent
       # seam needs WIRING (#982). A silent park would be indistinguishable from a
       # kind nobody has started.
       _NO_DRIVE = Parked(
           "no drive door configured (build-path #982); wiring a test fake into"
           " production is not composition"
       )
   ```
   **Do not change the park reason's text.** `_NO_DRIVE`'s sentence is what ships
   today; changing it would change a log line and an assertion.

   **Call-site audit.** `grep -rn "no Drive read seam configured" src storydump_cli scripts tests`
   — before: one row (`work_loop.py:533`). After: **zero**. If the grep finds a
   test asserting that sentence, that test was asserting an unreachable branch —
   delete the assertion and say so in the PR body.
   `grep -rn "_NO_DRIVE" src tests` — unchanged.

   **Tests.** `tests/src/services/target/test_work_loop.py`,
   `tests/scripts/test_w1_worker_gate.py`, `tests/src/test_worker.py` (park reasons).

---

2. **`_next_slot` is adopted at its three inline sites (TD-A6)** — `src/services/target/publish_pipeline.py:465-468`, `:526-527`, `:580-581`, `:1414-1415`.

   **Prefer adoption over deletion**, per the brief. First, the verification the
   brief asks for: the three inlined bodies are **identical**, verified by reading
   all three —

   `:526-527`, `:580-581` and `:1414-1415` each read exactly:
   ```python
               slot = _slot_at(ctx, now_fn)
               run_at = slot or now_fn() + timedelta(seconds=backoff_seconds[0])
   ```
   (indentation differs; the expression does not), and `_next_slot`'s body at
   `:468` is `return _slot_at(ctx, now_fn) or now_fn() + timedelta(seconds=backoff_seconds[0])`
   — the same expression, returning only the `run_at` half. Each site keeps `slot`
   separately because the card line downstream is conditional on it, which is why
   the helper as written could not be called. **Record this verification in the PR
   body**: three identical bodies, one helper missing one of the two values.

   2a. Change the helper to return both:

   Before:
   ```python
   def _next_slot(ctx: _Ctx, now_fn, backoff_seconds) -> datetime:
       """The deferral target: the account's next product slot, or one backoff
       rung when the clock has not stamped one yet."""
       return _slot_at(ctx, now_fn) or now_fn() + timedelta(seconds=backoff_seconds[0])
   ```
   After:
   ```python
   def _next_slot(
       ctx: _Ctx, now_fn, backoff_seconds
   ) -> tuple[Optional[datetime], datetime]:
       """The deferral target, as `(slot, run_at)`.

       `slot` is the account's next product slot when the clock has stamped one
       and None otherwise — the three callers each branch their card line on it,
       which is why this returns the pair rather than the target alone. `run_at`
       is that slot, or one backoff rung. Written once because it was inlined at
       three sites before anyone called the helper (#1325 audit, TD-A6).
       """
       slot = _slot_at(ctx, now_fn)
       return slot, slot or now_fn() + timedelta(seconds=backoff_seconds[0])
   ```
   Confirm `Optional` and `datetime` are imported in this module
   (`grep -n "^from typing\|^from datetime" src/services/target/publish_pipeline.py`).

   2b. Each of the three sites becomes one line:
   ```python
               slot, run_at = _next_slot(ctx, now_fn, backoff_seconds)
   ```
   at `:526-527` (`_admit`, the pre-check defer), `:580-581` (`_admit`, the cap
   defer) and `:1414-1415` (`_ladder`, error 9). **Keep the surrounding comments
   as they are** — each names the branch it belongs to, not the expression.

   **Call-site audit.** `grep -rn "_next_slot" src storydump_cli scripts tests`
   — before: **one** row, the definition at `:465`. After: the definition plus
   three callers, all in `publish_pipeline.py`.
   `grep -rn "or now_fn() + timedelta(seconds=backoff_seconds\[0\])" src` —
   before: three rows plus the helper. After: **only** the helper.

   **Tests.** `tests/scripts/test_l5_pipeline_gate.py` — the `DEFERRED_CAP` and
   `DEFERRED_META_CAP` paths execute two of the three sites; error 9 is the third.
   Add a unit pin in `tests/src/services/target/` (the module that already
   exercises `_slot_at`; find it with
   `grep -rln "_slot_at" tests/src/services/target/`): `_next_slot` returns
   `(None, now + backoff_seconds[0])` when the clock stamped no slot, and
   `(slot, slot)` when it did.

---

3. **Fully dead surfaces (TD-A16, part 1)** — delete. Each has been grepped across `src storydump_cli scripts tests`; the result is stated.

   3a. **`rate_counters.py:43-57 count`** — `grep -rn "rate_counters.count\|from src.services.target.rate_counters import" src storydump_cli scripts tests` plus
   `grep -rn "\bcount(" src/services/target/ | grep -v "def count"` finds **no
   caller and no test**. Delete the function. Its docstring claims it is "a READ
   for a check that must not spend (an admission check before the flip)" — no such
   check calls it; `increment`'s `WHERE rc.count < :limit` is the live rule, which
   is the database being the authority. Delete with no replacement.

   3b. **`publish_cap.py:317-333 current_day_debit`** — `grep -rn "current_day_debit" src storydump_cli scripts tests`: **one row**, the definition. Its docstring claims "the usage pre-check" reads it; `usage_precheck.py` does not (`grep -n "publish_cap" src/services/target/usage_precheck.py`). Delete.

   3c. **`scheduler.py:95-101 ClockNotElected`** — `grep -rn "ClockNotElected" src storydump_cli scripts tests`: **one row**, the definition. Never raised; `Clock.tick_once` returns None instead (`scheduler.py:611-615`). Delete the class. Check `scheduler.py`'s `__all__` if it has one.

   3d. **`telegram_dispatch.py:69 PUBLISH_LEG_LIVE`** and the branch at `:183-185`.
   `grep -rn "PUBLISH_LEG_LIVE" src storydump_cli scripts tests` — before: the
   definition, the branch at `:183`, and `tests/src/services/target/test_telegram_dispatch.py:404`.

   Before (`:180-185`):
   ```python
               return "✅ Approved — dry run, nothing will be published"
           if data.get("paused"):
               return "✅ Approved — posting is paused; it posts when you resume (within 3 days)"
           if PUBLISH_LEG_LIVE:
               return "✅ Approved — posting shortly"
           return "✅ Approved — publishing isn't live yet; it will post when it is"
   ```
   After:
   ```python
               return "✅ Approved — dry run, nothing will be published"
           if data.get("paused"):
               return "✅ Approved — posting is paused; it posts when you resume (within 3 days)"
           return "✅ Approved — posting shortly"
   ```
   and delete the constant at `:69`. The flag is already `True`, so the
   `"isn't live yet"` string is unreachable — deleting it changes no answer text.
   `tests/src/services/target/test_telegram_dispatch.py:400-407` becomes:
   ```python
           assert "posting shortly" in r.answer_text.lower()
   ```
   (drop the `if telegram_dispatch.PUBLISH_LEG_LIVE:` / `else:` fork). **After:**
   `grep -rn "PUBLISH_LEG_LIVE\|isn't live yet" src storydump_cli scripts tests`
   returns **zero**.

   Note the brief names this `intent_ledger.py:66 PUBLISH_LEG_LIVE`. That is a
   conflation of two A16 items: `intent_ledger.py:66` is `legal_transitions`
   (step 4c) and `PUBLISH_LEG_LIVE` is `telegram_dispatch.py:69`. Both are handled.

   3e. **The `rate_limited` tap outcome** — `telegram_dispatch.py:106` (in
   `TAP_OUTCOMES`) and `:129` (in `ANSWERS`).

   **The brief asks whether a test counts `TAP_OUTCOMES` before removing the
   member. It does not count it — it parametrizes over it**:
   `tests/src/services/target/test_telegram_dispatch.py:513-516`
   ```python
       @pytest.mark.parametrize("outcome", telegram_dispatch.TAP_OUTCOMES)
       def test_every_tap_outcome_has_an_entry(self, outcome):
           text, _ = telegram_dispatch.answer_for(outcome)
           assert text
   ```
   So removing the member removes one parametrized case; no assertion on the
   tuple's length or contents exists (`grep -n "TAP_OUTCOMES" tests/` returns that
   one row). The test stays green untouched.

   `_tap` answers `"too_many"`, never `"rate_limited"` — verify before deleting:
   `grep -n "rate_limited\|too_many" src/services/target/telegram_dispatch.py`.
   Expected: `"rate_limited"` only at `:106` and `:129`; `"too_many"` at `:469`.
   If the grep shows a raiser of `"rate_limited"`, **stop and leave it** — the
   finding would be wrong. Otherwise delete both lines. `answer_for` falls back to
   `FALLBACK_ANSWER` for an unknown outcome (`:111-112`), so even a future raiser
   still gets a sentence.

   3f. **`WorkerConfig.retry_backoff_seconds`** — `work_loop.py:83`.
   `grep -rn "retry_backoff_seconds" src storydump_cli scripts tests` — before:
   the field at `work_loop.py:83` and **one test reader**,
   `tests/scripts/test_w1_worker_gate.py:202`:
   ```python
           assert 30 < eta <= WorkerConfig().retry_backoff_seconds + 30
   ```
   The field is not the backoff that runs — R8's backoff is `jobs.backoff_seconds`
   (the lane ladder plus ±20 % jitter), which is what produces `eta`. The
   assertion happens to hold because `60.0` sits in the right range, not because
   the number is the one under test. Rewrite it against the real authority:
   ```python
           first_rung = jobs.BACKOFF_SECONDS["bulk"][0]
           # ±20 % jitter (`jobs.backoff_seconds`), plus the clock between the
           # reschedule and this read.
           assert 0.8 * first_rung - 5 < eta <= 1.2 * first_rung + 30
   ```
   — then check the lane this test's job actually runs in
   (`grep -n "lane" tests/scripts/test_w1_worker_gate.py | head`) and use that
   lane's ladder, not `"bulk"`, if it differs. Import `jobs` in the test module.
   Delete the field. **After:**
   `grep -rn "retry_backoff_seconds" src storydump_cli scripts tests` returns **zero**.

   **Tests for step 3.** `tests/src/services/target/test_telegram_dispatch.py`,
   `tests/scripts/test_w1_worker_gate.py`, plus the full suite — a deleted symbol
   with a forgotten importer fails at collection, which is the point of running
   the whole suite for this step.

---

4. **Test-only surfaces (TD-A16, part 2)** — a ruling per item. The rule applied: **delete it with its test when the test only re-proves what a live path already proves; keep it when the test is the seam's contract and the seam is injected.**

   4a. **`outbox.py:992-1052 deliver` — KEEP, and say why in its docstring.**
   Callers: `tests/scripts/test_outbox_sender_gate.py:922`,
   `tests/src/services/target/test_outbox_paced.py:177`,
   `test_outbox_destination_gone.py`. Production runs `OutboxPoller.tick`.
   `deliver` is the *one-transaction composition* of claim → send → settle, and
   the three tests use it to prove the settle taxonomy without standing up a
   poller. That is a seam contract, not a duplicate: deleting it would push a
   poller into three unit tests that are deliberately not about the poller. Add
   one sentence to its docstring:
   ```python
       Production runs :meth:`OutboxPoller.tick`; this is the same sequence as ONE
       transaction, kept as the seam the settle-taxonomy tests drive (#1325 audit,
       TD-A16 — not a second production path).
   ```

   4b. **`outbox.py:1468-1486 OutboxPoller.start/stop` — DELETE.**
   Caller: `tests/scripts/test_outbox_sender_gate.py:1258` only. Unlike `deliver`,
   these are a lifecycle wrapper around `tick`, and the worker drives `tick`
   directly through the lane (`grep -rn "OutboxPoller" src` to confirm production
   never calls `start`/`stop`). **Run that grep first**; if `src/worker.py` or
   `work_loop.py` calls either, this item is withdrawn and they stay. If the grep
   shows zero `src/` callers, delete both methods and rewrite
   `test_outbox_sender_gate.py:1258`'s test to drive `tick` in a loop, or delete
   the test if its only assertion is that `start` starts — say which in the PR body.

   4c. **`intent_ledger.py:66-75 legal_transitions` — KEEP, and correct its docstring.**
   Caller: `tests/scripts/test_intent_ledger_gate.py:791-816` only. The test is
   *the design assertion*: it proves `legal_transitions` reads the
   `post_intent_transitions` table rather than holding a Python copy of the edge
   set — exactly the "the database is the authority" rule. Deleting the function
   deletes that proof. But its docstring promises a caller that does not exist
   ("For display only"). Change the second paragraph to:
   ```python
       For display only, and NOT yet displayed anywhere: the one caller today is
       `tests/scripts/test_intent_ledger_gate.py`, which uses it to prove the edge
       set is READ from `post_intent_transitions` rather than kept in Python
       (#1325 audit, TD-A16). Never call this to pre-validate a write — see the
       module docstring on why a second authority is the thing this module avoids.
   ```

   4d. **`invitation_cards.py:110-157 announce` — KEEP, with the ticket in its docstring.**
   Callers: `tests/scripts/test_invitation_cards.py:131`, `:146`, `:164`, `:200`.
   Its module docstring (`:33-39`) already says nothing calls it — the producer
   was merged in #1188 and the consumer never landed. That is a *wiring* gap, and
   deleting the door makes the gap invisible. Leave the code; update the docstring
   to name the date the observation was re-made and what would wire it:
   append `Still true 2026-09-20 (#1325 audit, TD-A16): the caller would be the
   invitation write in `invitations.create`, beside the outbox row it already
   mints.` **Do not wire it** — wiring an announcement changes what a chat receives.

   4e. **`reconciler.py:51 evidence_capture` and `:377 stories_check` — KEEP.**
   `grep -rn "evidence_capture\|stories_check" src storydump_cli scripts tests`
   shows `evidence_capture` is a **mode of a two-mode classifier**
   (`EVIDENCE_MODES`), asserted for set-equality at
   `tests/scripts/test_l3_reconciler.py:36` and exercised at
   `test_l3_permit_rail.py:659,:704`; `stories_check` is an **injected seam**
   (`reconciler.py:416-418`) driven at `test_l3_permit_rail.py:579`. Both are the
   "seams are injected" rule working as designed (`.claude/rules/development-patterns.md`
   › Service modules). Neither is dead code; the finding's own text says "unless
   the seam is still wanted (then the tests are its only reason — say so)". **Say
   so**: add one line to `reconciler.py:44-55`'s comment naming the driver —
   `The second mode and the `stories_check` seam are driven by
   `tests/scripts/test_l3_permit_rail.py`; production passes neither (#1325, TD-A16).`

   4f. **`work_loop.py:809-812 bind_claim_conn` — KEEP.** The finding itself calls
   it a documented test seam with three gate-test drivers. No change.

   **Call-site audit for step 4.** For each item, the grep is named inline. The
   net expected diff under `src/`: one deletion (4b, conditional on its grep) and
   four docstring edits. **A docstring edit is not a behaviour change** — confirm
   by `git diff --stat src/` showing no executable line touched outside 4b.

---

5. **`src/exceptions/telegram.py` and its ratchet entry (TD-C4)** — delete the file; re-measure the baseline in the same PR.

   5a. `grep -rn "exceptions.telegram\|exceptions/telegram\|AmbiguousDeliveryError\|ChatMigratedError" src storydump_cli scripts tests documentation .claude AGENTS.md CLAUDE.md README.md`
   — expected **zero** rows outside the file itself and
   `scripts/telegram_ratchet_baseline.json`. (Verified for the documentation roots:
   no live page names it.) If the grep finds a reference, stop.

   5b. `git rm src/services/../src/exceptions/telegram.py` (exact path:
   `src/exceptions/telegram.py`). `src/exceptions/__init__.py` mentions it in
   prose at `:5-6` ("`identity` and `telegram` are imported by their own path") —
   rewrite that sentence in step 9c, where `identity` goes too.

   5c. **The baseline is re-measured, not hand-edited.**
   `scripts/telegram_ratchet.py:26` — "Re-measuring is `--write-baseline`, which
   runs the same functions the check runs. There is no second implementation to
   keep in step." And `:91` — "retiring a module is a baseline edit that removes a
   line". So:
   ```bash
   python scripts/telegram_ratchet.py --write-baseline
   git diff scripts/telegram_ratchet_baseline.json
   ```
   The **only** expected change is one line removed from `"telegram_modules"`,
   leaving three entries:
   ```json
     "telegram_modules": [
       "src/channels/telegram_transport.py",
       "src/channels/telegram_webhook_registration.py",
       "src/services/target/telegram_dispatch.py"
     ]
   ```
   If `--write-baseline` changes any other axis
   (`chat_id_functions_outside_adapters`, `core_telegram_modules`,
   `provider_account_ref_log_sites`) or rewrites the `"predicate"` string,
   **stop and review** — the gate asserts set equality per axis and an unexpected
   delta means this PR moved something else. Then re-run the check:
   ```bash
   python scripts/telegram_ratchet.py   # expect exit 0, CLEAN
   ```

   **Tests.** `tests/src/test_legacy_tier_gone.py:370-383` asserts the list is
   non-empty and inside the target homes — three entries satisfy both, so **no
   test change**. Run `tests/scripts/test_telegram_ratchet.py` and
   `tests/src/test_legacy_tier_gone.py`.

---

6. **The Drive adapter's dead seam and its stale headers (TD-B5)** — `src/services/target/drive_adapter.py`, `google_drive_adapter.py`.

   6a. **Delete, verified zero `src/` references outside their own module:**
   - `drive_adapter.py:237-293 StubDriveAdapter` — referenced only by
     `tests/src/services/target/test_drive_adapter.py` and two prose mentions
     (`drive_adapter.py:7`, `work_loop.py:204`, `google_drive_adapter.py:14`).
   - `drive_adapter.py:178-186 DriveFile` and `:189-203 DrivePage` — used only by
     `StubDriveAdapter` and that test.
   - `drive_adapter.py:125-126 DriveAuthError` — **never raised or caught anywhere**
     (`grep -rn "DriveAuthError" src storydump_cli scripts tests` returns the
     definition and one docstring mention at `:93`). The live credential refusal is
     `media_sync.DriveCredentialDead`.
   - `drive_adapter.py:110 DEFAULT_PAGE_SIZE` — a second constant of the same name
     as `google_drive_adapter.DEFAULT_PAGE_SIZE = 200` (`:170`), used only by the
     stub.
   - `drive_adapter.py:16-41` — the 40-line paging essay. It documents the
     `next_page_token` protocol of `list_files`, which no implementation provides
     (`google_drive_adapter.py:8-10` says so outright). It goes with the protocol.

   **Keep**: `DriveError`, `DriveRetryableError`, `DriveTerminalError`,
   `DriveMediaTooLarge` (the live taxonomy), `validate_source_config`,
   `checkpoint_incomplete`, `PROVIDER`.

   6b. **`tests/src/services/target/test_drive_adapter.py` is deleted with the
   stub.** `grep -n "import\|^class \|def test_" tests/src/services/target/test_drive_adapter.py`
   first: if **every** test in it drives `StubDriveAdapter`/`DrivePage`/`DriveFile`,
   delete the file. If any test drives `validate_source_config` or
   `checkpoint_incomplete` (the surviving seam), **keep those tests** in the file
   and delete only the stub ones. Say which in the PR body.

   6c. **`probe` / `ProbeResult` / `_get` — DELETE.** `google_drive_adapter.py:778-836 probe`,
   `drive_adapter.py:206-233 ProbeResult`, and `google_drive_adapter.py:950-953 _get`
   (which exists only for `probe` — confirm with
   `grep -n "self._get(" src/services/target/google_drive_adapter.py`; expected:
   only inside `probe`). Their only driver is
   `tests/src/services/target/test_google_drive_adapter.py:308-433`, deleted with
   them. The alternative the finding offers — wiring `probe` to the connect form —
   is **new behaviour** and out of scope. Note the collision: `scripts/unreachable_capabilities.py:81`
   defines its own unrelated `ProbeResult`; leave it alone.
   Remove `ProbeResult` from `google_drive_adapter.py:92`'s import list.

   6d. **The three false-fact sentences.**
   - `google_drive_adapter.py:41-47` — "Drive has no refresh door yet …
     `credential_lifecycle` ships `ig_refresh` and nothing for Google". Verify with
     `grep -n "def _refresh\|refresh" src/services/target/drive_credentials.py | head`;
     `drive_credentials._refresh` is that door. Rewrite the heading to
     `## The token arrives injected` and the first sentence to name
     `drive_credentials.provider_from_engine` as the live provider. **Keep** the
     rest (the `token_provider` seam rationale) — it is still true.
   - `google_drive_adapter.py:812-814` — "**Today every gdrive source probes
     `DriveCredentialDead`**, because nothing writes a gdrive credential yet".
     `google_drive_oauth.store_credential` does. This paragraph is inside `probe`,
     so it goes with 6c.
   - `drive_credentials.py:24-25` and `google_drive_oauth.py:45-54`, `:151-152` —
     "Until P5 mints from the refresh token". P5 is #1247 and shipped. Read each
     and rewrite the sentence in the present tense; **do not change any code
     around them**.
   - `drive_adapter.py:145-150` — `checkpoint_incomplete`'s docstring says the only
     complete checkpoint is `{"v": 1}` while the walk writes `{"v": 2, "walk": …}`
     (`google_drive_adapter.py:321-325`). Change `` `{"v": 1}` `` to
     `the version key alone (`v`), with no `page_token`, `current` or `queue``.
     **The function's behaviour is version-agnostic already** (it tests three keys
     and ignores `v`), so this is a docstring correction only — confirm by reading
     `:151-153`.
   - `drive_adapter.py:1-12` header — rewrite to "the seam contract and its typed
     errors", striking the "ships the SEAM and a scripted stub … the gate injects
     `StubDriveAdapter`" sentence, and `work_loop.py:204`'s
     "`StubDriveAdapter` until M.3 (#862)" comment with it.

   **Call-site audit.** After 6a–6d:
   `grep -rn "StubDriveAdapter\|DrivePage\|DriveFile\|DriveAuthError\|ProbeResult" src storydump_cli scripts tests`
   — expected: **only** `scripts/unreachable_capabilities.py`'s own `ProbeResult`.
   `grep -rn "list_files" src` — expected: **zero** (the prose mentions go with the
   header rewrites).
   `grep -rn "DEFAULT_PAGE_SIZE" src` — expected: **one** definition
   (`google_drive_adapter.py:170`) and its readers.

   **Tests.** `tests/scripts/test_w6_sync_gate.py` (the live sync path, must stay
   green), `tests/src/services/target/test_google_drive_adapter.py` (minus the
   probe block).

---

7. **API and utils leftovers (TD-C16)** — `src/api/app.py:544-547`, `src/utils/`.

   7a. **`app.py:544-547 _telegram_reply` — DELETE.**
   `grep -rn "_telegram_reply" src storydump_cli scripts tests` — before: **one**
   row, the definition. Superseded by `bot.send_text` at `:640`.
   ```python
   def _telegram_reply(env: Mapping[str, str]):
       """The `/start` door's acknowledgement sender, or None without the token."""
       transport = _telegram_transport(env)
       return None if transport is None else transport.send_text
   ```
   → deleted. Leave `_telegram_transport` (`:529-541`) — it has a live caller and
   moves in doc 09.

   7b. **`datetime_utils.naive_utc` — DELETE, with its mirror test.**
   `grep -rn "naive_utc" src storydump_cli scripts tests` — before: the definition
   (`:29-59`), its docstring, and `tests/src/utils/test_datetime_utils_mirrors.py`
   (the whole file is about the pair). Every target column is `TIMESTAMPTZ`
   (`columns.py:24`), so the naive boundary the function serves does not exist in
   this tier. Delete the function; delete
   `tests/src/utils/test_datetime_utils_mirrors.py` **only if every test in it
   names `naive_utc`** — run `grep -n "def test_" -A 3 tests/src/utils/test_datetime_utils_mirrors.py`
   and keep any test that is purely about `ensure_utc`, moving it to a file named
   for `ensure_utc` if the mirror file would otherwise be empty.
   Also rewrite `datetime_utils.py:10-11`, whose docstring names
   `api_tokens.expires_at` and `chat_settings.last_post_sent_at` — both legacy
   tables — and `ensure_utc`'s docstring, for the one live caller
   (`transit.py:68` imports it; find the use with
   `grep -n "ensure_utc" src/services/target/transit.py`).

   7c. **`logger.get_logger` — DELETE, with its tests.**
   `grep -rn "get_logger" src storydump_cli scripts tests` — before: the definition
   (`logger.py:74-87`) and `tests/src/utils/test_logger.py:8`, `:38-50`. Delete the
   function, remove it from the test module's import at `:8`, and delete the two
   tests at `:37-50`. `setup_logger` and the shared `logger` stay (the API routes
   import the latter).

   7d. **`encryption.TokenEncryption.rotate` — KEEP, say why; `%`-style the two log calls.**
   Its caller is the `reencrypt_credentials` executor, which sits in
   `work_loop.UNBUILT_KINDS` — so the method is waiting for a kind that is
   declared-but-unbuilt, not orphaned. Add to its docstring:
   `Waits on the `reencrypt_credentials` executor (`work_loop.UNBUILT_KINDS`);
   driven today only by `tests/src/utils/test_encryption.py` (#1325, TD-C16).`
   Separately, `encryption.py:126` and `:132` are two of the three f-string log
   calls measured repo-wide (`.claude/rules/development-patterns.md` › Logging:
   "A module logger and `%`-style arguments, not f-strings"). Convert both:
   read them with `sed -n '120,136p' src/utils/encryption.py` and replace
   `logger.info(f"… {x} …")` with `logger.info("… %s …", x)`. **Never log a token
   or a key** — if either line interpolates a secret, delete the interpolation
   rather than converting it, and say so.

   **Call-site audit.** After 7a–7d:
   `grep -rn "_telegram_reply\|naive_utc\|get_logger" src storydump_cli scripts tests`
   — expected **zero**.
   `grep -rn 'logger\.\(info\|warning\|error\|debug\)(f"' src storydump_cli`
   — expected: **one** row (the third measured f-string, outside this PR's scope)
   or zero. Record which.

---

8. **Delete the sync writer lane (TD-B1 / TD-C3, the deletion half)** — do this **after** step 9's rewrite is green, or on a branch where step 9 lands first. Listed here for the file inventory; execute it as the commit that follows step 9.

   Files deleted:
   - `src/services/target/identity_provisioning.py` (182 lines)
   - `src/services/target/workspace_provisioning.py` (136)
   - `src/services/target/web_sessions.py` (232)
   - `src/services/target/sync_tx.py` (128)
   - `src/exceptions/identity.py` (28)

   Symbol removed: `src/exceptions/tenancy.py:81-101 TenantProvisioningError`
   (raised only by the deleted lane).

   8a. **The grep that authorizes each deletion** — run all of them and record the output:
   ```bash
   grep -rn "identity_provisioning\|workspace_provisioning\|web_sessions\|sync_tx" src storydump_cli scripts
   grep -rn "IdentityProvisioningError\|TenantProvisioningError\|TransactionRequired" src storydump_cli scripts
   ```
   **Expected before: zero rows under `src/`, `storydump_cli/` and `scripts/`** —
   the only references are in `tests/scripts/test_identity_writers.py` (rewritten
   in step 9) and in the deleted modules' own docstrings. If any row appears under
   `src/`, **stop**: the finding is wrong and the deletion is not behaviour-preserving.

   8b. `src/exceptions/__init__.py` — the docstring at `:3-6` says "`identity` and
   `telegram` are imported by their own path". Both are gone (5b deletes
   `telegram`). Rewrite:
   ```python
   """Storydump exception classes.

   The legacy tier's `backfill`, `google_drive` and `instagram` modules went with
   it (the tear-out, phase 01; #1216); the sync writer lane's `identity` and the
   unused `telegram` module went with the tech-debt fold (#1325). This package
   exports the base classes and the tenancy refusal the target tier raises.
   """
   ```
   `__all__` is unchanged (`StorydumpError`, `TenantResolutionError`).

   8c. **`_dbapi.constraint_violated`'s psycopg2 arm (`_dbapi.py:31-35`, the
   `diag.constraint_name` read) — KEEP.** The research proposes removing it with
   the sync lane. Do not: the function reads both spellings through a two-`getattr`
   chain that costs nothing, `tests/scripts/` still drives psycopg2 connections
   throughout its fixtures, and removing a defensive read is not the same class of
   change as deleting an unreachable module. Instead, correct the docstring at
   `:31-35`, which says "on the sync lane, arrive unwrapped" — change "the sync
   lane" to "a psycopg2 caller (the gates' fixtures)".

   8d. `documentation/` audit — step 10.

---

9. **Re-home the RLS gate onto the live async writers (TD-B1 / TD-C3, the load-bearing half)** — rewrite `tests/scripts/test_identity_writers.py` (557 lines) against `identity.py`, `sessions.py` and `workspaces.py`; add one fixture to `tests/scripts/conftest.py`.

   **Why this is the point of the PR.** The file's own header claims it proves
   "the three writers … AS `svc_ingress` under the printed RLS". It proves that
   about code production does not run. After this step the same claim is true of
   the code production *does* run, and the divergences the two lanes accumulated
   become visible instead of latent.

   9a. **The new fixture.** `tests/scripts/conftest.py:1221-1237` has `in_tenant`,
   which opens a `unit_of_work` with a **mandatory** `tenant_id` and asserts
   `current_user == "svc_ingress"`. Sign-in and identity upsert happen **before a
   workspace exists**, in the user plane — the shape `workspaces.list_for_user`
   uses (`workspaces.py:195-197`: `set_config('app.actor_user_id', …)` alone).
   Add a sibling beside `in_tenant`, in the same style and with the same role
   assertion:
   ```python
   async def in_user_plane(dsn, fn, *, actor_user_id=None):
       """Run *fn(session)* in one committed transaction as `svc_ingress`, with
       the USER-plane GUC only — no tenant.

       The identity and session writers run before any workspace exists, so
       `in_tenant`'s mandatory `tenant_id` does not apply to them. The role is
       ASSERTED for the same reason it is there: a driver that quietly connected
       as the owner would bypass RLS and every isolation claim built on it would
       be vacuous while still reading green.
       """
       async with ingress_engine(dsn) as engine:
           async with engine.begin() as conn:
               who = (await conn.execute(text("SELECT current_user"))).scalar()
               assert who == "svc_ingress", who
               if actor_user_id is not None:
                   await conn.execute(
                       text("SELECT set_config('app.actor_user_id', :u, true)"),
                       {"u": str(actor_user_id)},
                   )
               return await fn(conn)
   ```
   **Before writing it, read `in_tenant`'s body and `unit_of_work`'s signature**
   (`grep -n "def unit_of_work" -A 30 src/services/target/unit_of_work.py`): if
   `unit_of_work` accepts `tenant_id=""` (the worker's system path passes an empty
   string — see `src/worker.py:533-535`, `apply_gucs(session, tenant_id="", actor_kind="system")`),
   prefer reusing `unit_of_work` with `tenant_id=""` over a raw
   `engine.begin()`, so the gate runs through the same door production does. **Try
   that first**; fall back to the raw form only if an empty tenant is refused.
   Export the new name from the conftest the same way `in_tenant` is exported.

   9b. **The assertions that carry over verbatim** (re-pointed at the live writer, same claim, same shape):

   | Current test | Live target | Note |
   |---|---|---|
   | `:115 first_sign_in_creates_both_rows_keyed_on_the_subject` | `identity.upsert_google_identity` | Both `users` and `user_identities` rows, keyed on `(provider, external_id)`. |
   | `:138 returning_subject_reuses_the_user_and_refreshes` | same | The second call returns the same `user_id`. |
   | `:160 an_absent_display_name_does_not_erase_the_stored_one` | same | Verify against `identity.py:60-90`, which updates `display_name` in the returning branch. |
   | `:170 primary_email_fills_when_empty_and_never_overwrites` | `identity._fill_primary_email` (`:127-140`) | The live twin has this exactly. |
   | `:317 mint_stores_only_the_hash_and_authenticates` | `sessions.issue` + `sessions.resolve` | Only the hash reaches `session_tokens` (`sessions.py:94-109`). |
   | `:362 an_expired_token_reports_expired_and_cannot_be_renewed` | `sessions.resolve` | Now a **raise** (`TenantResolutionError("expired_session")`), not a returned status — see 9c. |
   | `:402 a_revoked_token_cannot_be_slid` | `sessions.resolve` | Same; `revoked_session`. |
   | `:413 create_workspace_commits_with_its_owner_and_audit_trail` | `workspaces.create_workspace` | Carries. |
   | `:464 rls_refuses_a_workspace_that_is_not_the_claimed_tenant` | `workspaces.*` under `in_tenant` | **The whole reason the file exists.** Carries unchanged in substance. |
   | `:499 an_invalid_timezone_is_refused_by_the_schema` | `workspaces.create_workspace(tz=…)` | The schema refuses it on both lanes. |
   | `:510 a_named_timezone_is_stored` | same | Carries. |
   | `:530 sign_up_then_sign_in_reaches_the_new_workspace` | all three async writers | Carries; re-point the three calls. |

   9c. **The assertions that change shape** (same property, different surface — rewrite, do not drop):

   - **`:336 an_unknown_token_is_invalid_not_empty`.** `web_sessions.authenticate_session`
     returned a status object; `sessions.resolve` **raises**
     `TenantResolutionError("invalid_session")` (`sessions.py:127-128`). Becomes
     `with pytest.raises(TenantResolutionError) as exc: … ; assert exc.value.reason == "invalid_session"`.
   - **`:341 revoke_is_idempotent_and_keeps_the_first_instant`.** `sessions.revoke`
     returns a **bool** (`True` then `False`, `sessions.py:143-152`). The
     "keeps the first instant" half is still assertable — read `revoked_at` back as
     the owner over the two calls and assert it did not move (the live statement's
     `WHERE … revoked_at IS NULL` is what guarantees it).
   - **`:186 a_colliding_email_refuses_and_never_merges`.** The exception type
     changes: `IdentityProvisioningError("email_belongs_to_another")` →
     `identity.IdentityCollision` (`identity.py:29`, raised at `:122`).
     **`IdentityCollision` is a plain `StorydumpError` with no `reason` attribute**
     — so `assert exc.value.reason == "email_belongs_to_another"` does **not**
     carry. Assert the type and the "never merged" half (the second user's row is
     untouched), which is the substantive claim.
   - **`:383 touch_slides_the_window_and_stamps_last_seen`.** **There is no
     `touch_session` on the live lane.** `sessions.resolve` slides the expiry
     inline, throttled by `RENEW_THROTTLE_SECONDS = 60` (`sessions.py:41`,
     `:111-142`). Rewrite as two assertions: a `resolve` past the throttle slides
     `expires_at`; a second `resolve` inside the throttle window does **not**.
     Read the `_RESOLVE` statement (`grep -n "_RESOLVE" -A 25 src/services/target/sessions.py`)
     to see whether it also stamps a last-seen column; assert that only if the
     column exists.

   9d. **The assertions that were only ever true of the dead twin** (drop, with the reason in the PR body):

   - **`:482 an_autocommit_connection_is_refused_by_name`.** It asserts
     `sync_tx.TransactionRequired`, a type deleted in step 8. The async
     `unit_of_work` opens a transaction unconditionally — there is no autocommit
     shape to refuse, so the property does not exist on the live lane. **Drop the
     test.** `tenancy.py:94-99`'s docstring paragraph ("Deliberately NOT here: an
     autocommit connection … it has its own type (`sync_tx.TransactionRequired`)")
     goes with `TenantProvisioningError` in step 8.
   - **`:302 an_empty_subject_is_refused_before_any_write`.** The dead twin
     refused a blank `sub` in Python. **Check whether `identity.upsert_google_identity`
     does** (`sed -n '48,70p' src/services/target/identity.py`). If it does, the
     test carries. If it does not — the row would be refused by
     `uq_user_identities_provider_external` or a NOT NULL, or not refused at all —
     **do not add the Python pre-check**; the database is the authority. Port the
     test to assert *what the live lane actually does* and note the difference.
   - **`:491 a_blank_name_and_a_missing_owner_are_refused`.** Same treatment.
     `workspaces.create_workspace` (`:142-182`) does not obviously refuse a blank
     name in Python; the refusal, if any, is the schema's. Read the migration's
     `workspaces` CHECK (`grep -rn "ck_workspace\|name" scripts/migrations/*workspace* | head`)
     and port the test to the real authority. **If nothing refuses a blank
     workspace name on the live path, that is a finding** — record it, do not add
     a check.
   - **`:225 two_concurrent_first_sign_ins_converge_on_one_user`.** This is the
     one to run before you write it. The dead twin caught `uq_users_primary_email`
     (the database as authority, `identity_provisioning.py:169-171`); the live twin
     **pre-checks in Python** (`identity.py:92-93` →
     `_refuse_if_held_elsewhere`, `:113-125`, a plain `SELECT` with no lock).
     Under a real race the live lane can therefore surface the driver's unique
     violation rather than a typed `IdentityCollision`. Port the race using
     `asyncio.gather` over two `in_user_plane` calls, **run it, and record the
     observed outcome**. Whatever it is:
     - both callers converging on one user → assert that, done;
     - one caller getting a raw `IntegrityError` → **that is a finding, not a fix**.
       Assert the observed behaviour so the gate is honest, and file the divergence
       for its own PR (it is the same class as TD-B1's "the DB is the authority"
       note). **Do not add a savepoint, an advisory lock or a caught
       `uq_users_primary_email` in this PR** — every one of those changes the live
       sign-in path.

   9e. **The new assertions the live lane makes possible** — add them; they are why
   the port is worth the hours:

   - **`sessions.resolve` refuses a `disabled` user** with `disabled_user`
     (`sessions.py:138-139`), which `web_sessions.authenticate_session` never did.
     `02` §1 calls this "the ONE ingress gate", and it had no gate coverage at all.
   - **`sessions.new_token` never returns a value starting with
     `vocabulary.TOKEN_PREFIX`** (`sessions.py:79-92`, the re-draw). A session value
     wearing `sdt_` would be routed to the token resolver and fail every call. A
     deterministic test: monkeypatch `secrets.token_urlsafe` to return a prefixed
     value once and a clean one after, and assert the clean one comes back.
   - **`workspaces.create_workspace(tz=None)` stores `'UTC'`** — the live writer
     spells `COALESCE(:tz, 'UTC')` in Python (`workspaces.py:175`) where the dead
     twin deliberately left it to the column default. Assert the stored value.
     **Note in the PR body** that this is a second home for a default the schema
     owns; it is an observation for a later finding, **not** something to fix here
     (removing the COALESCE changes what a NULL `tz` writes).

   9f. **The file's header must be rewritten.** Its current header claims a proof
   about the sync writers. Replace it with what the file now proves: the three live
   writers, as `svc_ingress`, on the replayed schema, with the user-plane door for
   sign-in and `in_tenant` for the workspace half — and carry over the
   `test_provisioning_gate.py:24-40` caveat verbatim in substance: **`svc_ingress`
   is the posture F.4 intends, not the role production connects as**, so the
   isolation assertions are about the intended posture and must not be read as
   evidence about the deployed system.

   9g. **Markers.** The current file is marked `integration`, `slow`
   (`grep -n "pytestmark\|@pytest.mark" tests/scripts/test_identity_writers.py | head`).
   Keep whatever markers the sibling gates in `tests/scripts/` carry — check
   `tests/scripts/test_provisioning_gate.py`'s and match it, since the directory
   serializes on one cluster-wide advisory lock and refuses xdist
   (`tests/scripts/conftest.py:62`).

   **Call-site audit for step 9.**
   `grep -rn "test_identity_writers" tests scripts .github` — before and after:
   whatever CI selection names it, unchanged (the file keeps its name).
   `grep -rn "in_user_plane" tests` — after: the definition plus every call in the
   rewritten file.
   `grep -rn "in_tenant" tests/scripts` — after: the three existing gates plus the
   rewritten file's workspace half.

---

10. **The documentation audit for every deleted module.** `tests/test_agent_docs.py` fails when a LIVE page names a deleted module path (the legacy-name pin added in #1322), so this is a gate, not hygiene.

    Run, for each deleted module and for the deleted symbols:
    ```bash
    grep -rn "identity_provisioning\|workspace_provisioning\|web_sessions\|sync_tx" \
      documentation/guides documentation/operations AGENTS.md CLAUDE.md README.md .claude/
    grep -rn "exceptions/identity\|exceptions\.identity\|exceptions/telegram\|exceptions\.telegram" \
      documentation/guides documentation/operations AGENTS.md CLAUDE.md README.md .claude/
    grep -rn "StubDriveAdapter\|DriveAuthError\|ProbeResult\|naive_utc\|get_logger\|_telegram_reply\|retry_backoff_seconds\|PUBLISH_LEG_LIVE\|ClockNotElected\|current_day_debit" \
      documentation/guides documentation/operations AGENTS.md CLAUDE.md README.md .claude/
    ```
    **Measured on `main` at `0966771`: the first two greps return ZERO rows** — no
    live page names the sync lane or either exceptions module. Re-run them anyway
    (the tree moves), and run the third, which has not been measured. Any hit is
    an edit to that page in this PR.

    Then check the deleted-path list the pin reads: `tests/test_agent_docs.py`
    holds phase 01's deleted-module lists and asserts each named path is actually
    gone. Read how it is built —
    `grep -n "deleted\|DELETED\|module\|path" tests/test_agent_docs.py | head -30`
    — and if the newly deleted modules belong in that list, **add them**; if the
    list is phase 01's historical record, leave it and say so.

    Also check the two context satellites and the rules, which `.claude/` covers
    above but which are worth naming: `.claude/PROJECT_CONTEXT.md`,
    `.claude/QUICK_REFERENCE.md`, `.claude/rules/development-patterns.md`,
    `.claude/rules/database.md`, `.claude/rules/testing.md`.

---

11. **CHANGELOG** — add under `## [Unreleased]` → `### Removed`, creating the section if it does not exist (the format's order is Added, Changed, Deprecated, Removed, Fixed, Security):

    ```markdown
    - **The dead sync writer lane goes, and its RLS gate is re-aimed at the writers production runs (the tech-debt audit, doc 02; #1325).** `identity_provisioning.py`, `workspace_provisioning.py`, `web_sessions.py` and `sync_tx.py` — 678 lines of psycopg2 session minting, identity upsert and tenant mint — had zero callers under `src/`, `storydump_cli/` or `scripts/`, and were kept alive by one file: `tests/scripts/test_identity_writers.py`, the gate that drives writers as `svc_ingress` under the replayed RLS. So the repository's one RLS gate on sign-in proved code production does not run while the code it does run had none, and the two twins had already drifted — `sessions.resolve` refuses a disabled user where `web_sessions.authenticate_session` did not; `workspaces.create_workspace` spells `COALESCE(:tz,'UTC')` where its twin deliberately left the default to the schema; the live token draw re-rolls a value wearing the API-token prefix and its twin never did. The gate is rewritten against `identity.upsert_google_identity`, `sessions.issue/resolve/revoke` and `workspaces.create_workspace`, with a new user-plane fixture (`in_user_plane`) because sign-in happens before any workspace exists and `in_tenant`'s tenant is mandatory; the disabled-user refusal, the token-prefix re-draw and the `COALESCE` now have assertions for the first time, and the one test that was only ever true of the dead twin — an autocommit connection refused by name, a `sync_tx` type — is dropped with its type. `src/exceptions/identity.py`, `TenantProvisioningError` and the unused `src/exceptions/telegram.py` (`AmbiguousDeliveryError`, `ChatMigratedError`, referenced nowhere and held alive only by a set-equality line in the FC-2 Telegram ratchet baseline, re-measured here) go with them. Also removed, each with the grep that showed no caller: `rate_counters.count`, `publish_cap.current_day_debit`, `scheduler.ClockNotElected`, the already-flipped `PUBLISH_LEG_LIVE` flag and the unreachable "publishing isn't live yet" answer behind it, the `rate_limited` tap outcome `_tap` never produces, `WorkerConfig.retry_backoff_seconds` (a backoff nothing reads — R8's is `jobs.backoff_seconds`, which is what the worker gate now asserts against), `app._telegram_reply`, `datetime_utils.naive_utc` and `logger.get_logger`; and the Drive seam's dead half — `StubDriveAdapter`, `DriveFile`, `DrivePage`, `DriveAuthError`, the duplicate `DEFAULT_PAGE_SIZE`, the paging essay for a `list_files` no implementation provides, and `GoogleDriveAdapter.probe` with `ProbeResult` and the `_get` that existed for it. The `build_registry` block that parked the two Drive kinds with a reason the registry overwrote one line later is gone, its rationale folded into the park reason that actually ships; `publish_pipeline._next_slot` now returns `(slot, run_at)` and is called at the three sites that had inlined its expression verbatim. Surfaces whose only caller is a test but whose test IS the seam's contract are kept and say so in their docstrings: `outbox.deliver` (the one-transaction composition the settle-taxonomy tests drive), `intent_ledger.legal_transitions` (which proves the edge set is read from `post_intent_transitions` rather than held in Python), `invitation_cards.announce` (a merged producer still waiting on its consumer), the reconciler's second evidence mode and its `stories_check` seam, and `TokenEncryption.rotate` (waiting on the `reencrypt_credentials` executor). No production path changed.
    ```

    CI's `changelog-check` (`.github/workflows/ci.yml`) gates on the entry.

## Test Plan

- **Pre-change baseline, on the parent commit** (this doc's parent is doc 01's
  merge commit, not `0966771`), with the Docker test PostgreSQL up:
  ```bash
  PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" \
  DB_HOST=localhost DB_PORT=65433 DB_USER=test_user DB_PASSWORD=test_password \
  DB_NAME=storyline_ai TEST_DB_NAME=storyline_test REQUIRE_TEST_DATABASE=1 \
    pytest --no-cov
  ```
  Record pass/fail/skip. **Also record the baseline of the file being rewritten
  on its own**, so the port's coverage can be compared:
  ```bash
  REQUIRE_TEST_DATABASE=1 pytest --no-cov tests/scripts/test_identity_writers.py -v
  ```
  Keep that `-v` output; it is the list of properties the rewrite must still claim.
- **Which existing gate characterizes this change.** For the deletions, the whole
  suite is the gate: a deleted symbol with a forgotten importer fails at
  collection. For the Drive half, `tests/scripts/test_w6_sync_gate.py` is the live
  sync path and must stay green with the stub gone. For step 9, the rewritten file
  **is** the gate, and its `-v` list against the recorded baseline is the review
  artefact.
- **Required by the brief, run explicitly:**
  ```bash
  REQUIRE_TEST_DATABASE=1 pytest --no-cov tests/test_agent_docs.py tests/test_legacy_cli_gone.py
  ```
  `test_agent_docs.py` is the pin that fails when a live page names a deleted
  module path; `test_legacy_cli_gone.py` proves nothing resurrected the retired
  CLI surface alongside these deletions.
- **Targeted:**
  ```bash
  REQUIRE_TEST_DATABASE=1 pytest --no-cov \
    tests/scripts/test_identity_writers.py \
    tests/scripts/test_w1_worker_gate.py \
    tests/scripts/test_w6_sync_gate.py \
    tests/scripts/test_l5_pipeline_gate.py \
    tests/scripts/test_outbox_sender_gate.py \
    tests/scripts/test_intent_ledger_gate.py \
    tests/scripts/test_invitation_cards.py \
    tests/scripts/test_l3_reconciler.py \
    tests/scripts/test_l3_permit_rail.py \
    tests/scripts/test_telegram_ratchet.py \
    tests/scripts/test_tenant_resolution.py
  REQUIRE_TEST_DATABASE=1 pytest --no-cov \
    tests/src/test_legacy_tier_gone.py \
    tests/src/test_worker.py \
    tests/src/services/target/test_work_loop.py \
    tests/src/services/target/test_telegram_dispatch.py \
    tests/src/services/target/test_outbox_paced.py \
    tests/src/services/target/test_outbox_destination_gone.py \
    tests/src/services/target/test_google_drive_adapter.py \
    tests/src/api/test_app_factory.py \
    tests/src/api/test_auth_routes.py \
    tests/src/utils/test_logger.py \
    tests/src/utils/test_encryption.py
  ```
- **The ratchet, run as its own check** (step 5c):
  ```bash
  python scripts/telegram_ratchet.py     # expect exit 0 after --write-baseline
  ```
- **New/updated pins**:
  - `tests/scripts/conftest.py` — **new** `in_user_plane` (step 9a): a committed `svc_ingress` transaction with the user-plane GUC and no tenant, asserting the role.
  - `tests/scripts/test_identity_writers.py` — **rewritten** (step 9): the twelve carried assertions, the four reshaped ones, the three new ones (disabled-user refusal, token-prefix re-draw, the `COALESCE` default), minus the autocommit test.
  - `tests/scripts/test_w1_worker_gate.py:202` — **updated** to assert against `jobs.BACKOFF_SECONDS` instead of the deleted `WorkerConfig.retry_backoff_seconds` (step 3f).
  - `tests/src/services/target/test_telegram_dispatch.py:400-407` — **updated**: the `PUBLISH_LEG_LIVE` fork collapses to one assertion (step 3d).
  - `tests/src/services/target/test_drive_adapter.py` — **deleted** with the stub (step 6b), unless it holds tests for the surviving seam.
  - `tests/src/services/target/test_google_drive_adapter.py:308-433` — **deleted** with `probe` (step 6c).
  - `tests/src/utils/test_datetime_utils_mirrors.py`, `tests/src/utils/test_logger.py:37-50` — **deleted** with their functions (steps 7b, 7c).
  - A unit pin for `_next_slot`'s pair return (step 2).
- **Post-change**: the same full-suite command. **The counts will not match the
  baseline** — this PR deletes tests. Record the delta and account for every
  removed test by name in the PR body; an unaccounted-for drop is a regression
  hiding in a deletion.
- Then `ruff check . && ruff format --check .`.

## Verification Checklist

- [ ] baseline green on the parent commit (doc 01's merge), counts recorded, plus the `-v` property list of `test_identity_writers.py`
- [ ] call-site audit: every grep in steps 1–10 run, results as stated — in particular the step 8a greps returning **zero** `src/`/`storydump_cli/`/`scripts/` rows before any deletion
- [ ] targeted tests + full suite green; every removed test accounted for by name
- [ ] `tests/test_agent_docs.py` and `tests/test_legacy_cli_gone.py` green
- [ ] `python scripts/telegram_ratchet.py` exits 0 CLEAN, and the baseline diff is **one line removed from `telegram_modules` and nothing else**
- [ ] `ruff check . && ruff format --check .`
- [ ] manual smoke: sign in on the web (the live `identity.upsert_google_identity` + `sessions.issue` path this PR re-gated but did not touch), then `storydump whoami` against the same deployment — both surfaces answer as before
- [ ] `CHANGELOG.md` entry under `## [Unreleased]` → `### Removed`
- [ ] nothing in "What NOT To Do" happened

## What NOT To Do

**No flagged defect rides in this PR** (`00_TECH_DEBT.md` › Questions):

- **TD-A1 repost TTL**, **TD-C1 health tap keys**, **TD-B4 `bindings.repoint`**,
  **TD-B3 enqueue routing**, **TD-C5 `/health.version`**, **TD-B2 the
  `_IN_TRANSACTION` tripwire**, **TD-B17 the tenant-less credential read**,
  **TD-B16 the `category_mix` shim** — none of these is touched. This PR edits
  `publish_pipeline.py` (step 2), `app.py` (step 7a) and `work_loop.py` (steps 1,
  3f); in each file, edit only the lines the step names.

**Specific to this PR:**

- **Do not "fix" a divergence the port reveals.** Step 9d's race test, the blank
  workspace name, the empty subject: if the live lane behaves differently from the
  dead one, the gate asserts **what the live lane does** and the difference is
  filed. Adding a Python pre-check, a savepoint or an advisory lock to
  `identity.py` or `workspaces.py` is a behaviour change to the sign-in path and
  belongs in its own reviewed PR.
- **Do not add a Python pre-check duplicating the database.** `identity.py:92-93`
  already pre-checks the email collision in Python; this PR neither extends that
  pattern nor removes it.
- **Do not delete `workspaces.list_bindings` / `list_invitations`** — both have
  live callers at `src/api/routes/v1.py:469` and `:478`. The brief's
  zero-reference claim is wrong; the finding is withdrawn.
- **Do not delete `drive_credentials.provider_from_engine`** — two live callers,
  `src/worker.py:845` and `src/api/routes/v1.py:883`. Withdrawn.
- **Do not hand-edit `scripts/telegram_ratchet_baseline.json`.** It is regenerated
  by `--write-baseline`, which runs the same functions the check runs; a hand edit
  creates the second source of truth the module's docstring exists to prevent.
- **Do not wire `invitation_cards.announce`, `GoogleDriveAdapter.probe`, or the
  `reencrypt_credentials` executor.** Each is a *new* behaviour: a chat receives a
  message it did not before, a connect form gains a verdict, credentials get
  re-encrypted. Keeping or deleting is in scope; wiring is not.
- **Do not remove `_dbapi.constraint_violated`'s psycopg2 arm.** The gates'
  fixtures still drive psycopg2; step 8c corrects its docstring instead.
- **Do not extract anything.** This doc deletes and re-points. The one structural
  move is step 2's adoption of an existing helper at three existing sites — a
  rule-of-three fix on a helper that already exists, not a new abstraction.
- **Do not change `_NO_DRIVE`'s park sentence** (step 1) or any other operator-
  visible string. Deleting an unreachable string is fine; editing a reachable one
  is a behaviour change.
- **Do not run `python -m src.main`, any `storydump` write verb, or
  `python -m scripts.migration_runner apply`.** The manual smoke is a sign-in and
  a `storydump whoami` — both read-only.

## Related

- `00_TECH_DEBT.md` — the inventory and the remediation order (row 02).
- `01_one-spelling.md` — the parent; names `retry_backoff_seconds` without deleting it, which this doc does.
- `05_imports-and-homes.md`, `12_stale-words-and-names.md` — both blocked on this.
- `tests/scripts/test_provisioning_gate.py:1-50` — the header this doc's step 9f copies in substance (what `svc_ingress` proves and what it does not).
- `tests/scripts/conftest.py:1210-1237` — `ingress_engine` and `in_tenant`, the shape `in_user_plane` follows.
- `scripts/telegram_ratchet.py:22-26,84-95` — why the baseline stores sets and how a retirement is recorded.
- `.claude/rules/testing.md` › Two kinds of test, and the `tests/scripts/` serialization note.

## Origin

- `/artemis-skills:audit tech debt`, 2026-09-20, on `main` at `0966771`.
- Research: `tech-debt-2026-09-20/research/integrations-identity.md` (TD-B1, B5), `research/api-channels-cli.md` (TD-C3, C4, C16), `research/pipeline-worker.md` (TD-A3, A6, A16).
