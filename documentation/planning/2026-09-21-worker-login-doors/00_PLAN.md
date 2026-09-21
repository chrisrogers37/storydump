---
title: "The worker's login (#751, part 2): doors for the tenant-less sweeps, then the switch"
type: plan
status: in-progress
owner: chris
created: 2026-09-21
tags: [rls, worker, doors, migration, f4, "#751"]
links: ["documentation/operations/runtime-database-roles.md", "documentation/planning/2026-08-02-consolidated-design-plan/07-security-model.md"]
---

# The worker's login (#751, part 2)

## Summary

Part 1 (PR #1333, migration 081) gave the API's estate-wide reads doors so the API can run as
`svc_ingress`. The worker cannot follow yet: measured statement by statement on 2026-09-21, it
issues no direct DELETE and touches none of the four tables `svc_worker` has no access to (the
oauth-state purge, `reap_expired_states`, is unwired dead code), but it runs **six tenant-less
paths** of exactly the class that blinded the health surfaces — `apply_gucs(session,
tenant_id="", actor_kind="system")` followed by direct SQL on policy-covered tables — and one
scope leak (the first measurement found four; the review of the build, PR #1349, found the
other two and the leak). Under `svc_worker`, two of them go silent in the worst way: the outbox
sender sweep mints no delivery jobs (no card is ever sent) and the prompt sweep finds no due story
(nothing ever asks for approval), while its per-row writes would be refused by `WITH CHECK`. This
plan gives those reads doors, keeps the writes under each workspace's own tenant, hands the
caller's scope back, and then repeats the switch for the worker with the same before/after check
the API's switch now has.

## Evidence

The clock, the claims and leases, the reaper, the reconciler's sweep, retention, the auth-plane
sweep and offboarding's finalize already go through SECURITY DEFINER doors (059 onward) and are
driven as `svc_worker` by `tests/scripts/test_rls_runtime_harness.py`. The six that do not, and
the leak:

1. **The outbox sender sweep** — `src/services/target/work_loop.py::ensure_sender_jobs` (called
   from `src/worker.py`'s sender sweep under `tenant_id=""`): one `INSERT INTO jobs … SELECT
   'deliver_outbox', b.workspace_id … FROM channel_bindings b WHERE … EXISTS (SELECT 1 FROM
   channel_outbox o …) AND NOT EXISTS (SELECT 1 FROM jobs j …) LIMIT 200`. Every table is
   policy-covered; with no tenant set the SELECT yields nothing and the sweep mints nothing.
2. **The prompt sweep** — `src/services/target/prompts.py::sweep_due_prompts` (from `src/worker.py`'s
   prompt sweep under `tenant_id=""`): `_CARD_SELECT … WHERE i.state = 'scheduled' AND
   i.schedule_slot_at <= now() AND w.state = 'active' AND NOT w.is_paused` across every workspace,
   then per row `push_bindings` and `prompt_intent` (an INSERT into `channel_outbox`) and the
   `prompt_pending → awaiting_approval` transition. The read is hidden; the writes carry a
   `workspace_id` that is not the (empty) tenant and fail `p_tenant`'s `WITH CHECK`.
3. **The stranded-source alert** — `src/services/target/media_sync.py::alert_stranded_sources`
   (the `alert_stranded_sources` job, a system kind): `UPDATE media_sources SET alerted_at = now()
   WHERE id IN (SELECT id FROM media_sources WHERE state = 'error' …) RETURNING id, workspace_id`,
   then per row `push_bindings` and a card. Alert-only; blind, not harmful.
4. **The reconciler's container poll** — `src/worker.py::_poll_from`: a fresh session with no GUCs
   reads `post_intents JOIN ig_accounts WHERE i.id = :id` for an ambiguous intent the reconciler's
   door listed. Hidden, it returns `None`, which the reconciler classifies as inconclusive under
   every mode: a lost publish answer can never be resolved by polling.
5. **The settled-card sweep** — `src/services/target/prompts.py::sweep_settled_cards` (from the
   `reap_expired` singleton, under `tenant_id=""`): a direct SELECT over `channel_outbox`,
   `channel_bindings`, `post_intents` and `workspaces` for live approval cards whose story has
   ended, then per row the settlement read and `outbox.supersede_all`. Hidden: no ended story's
   card ever loses its buttons. *(Found in review.)*
6. **The reconciler's ladder count** — `src/services/target/reconciler.py::sweep_due` read
   `last_error->'evidence'->>'checks'` for every due row in one statement, before any per-row
   claim. Hidden, it answered 0 for every row: every step counted as the first, the ladder never
   exhausted, and a lost publish answer was never parked for review. *(Found in review.)*
7. **The scope leak** — `plan_slot` (`work_loop.py`) runs `sweep_due_prompts(limit=1)` inside a
   TENANT job's transaction and then `finalize_job`. A sweep that claims workspaces per row and
   never restores the caller's leaves the session under the last workspace prompted — another
   workspace's, whenever the oldest due story is elsewhere — and the finalization's `UPDATE jobs
   … WHERE id = :id AND lease_token = :token` matches no row under the policies: `JobFenced` on
   every planned slot after the switch. *(Found in review.)*

Grants (production, 2026-09-20): `svc_worker` holds SELECT, INSERT and UPDATE on all 26
policy-covered tables and no DELETE; EXECUTE on every worker-side door. `svc_maintenance` holds
`USING (true)` on `jobs`, `channel_outbox`, `post_intents`, `daily_post_counts`, `rate_counters`,
`workspaces`, `post_locks`, `provider_operations`, `audit_events`, `command_dedup`,
`onboarding_sessions`, `workspace_invitations` and, since 081, SELECT on `ig_accounts`. It lacks
`channel_bindings`, `media_items` and `media_sources`, which the sweeps above read.

The existing executor gates (`test_w3_prompt_gate.py`, `test_w1_worker_gate.py`,
`test_w5de_credential_lifecycle.py`, `test_publish_cap_gate.py`) run as the owner actor, which
bypasses the policies; none of the six paths had ever run as `svc_worker` in a test.

## Decision Forks

- **F1 — one door per sweep, or a read door with the writes under the tenant.** *Options:* (a) each
  sweep becomes one SECURITY DEFINER function that reads and writes across tenants; (b) a door
  lists the cross-tenant rows with their workspace ids, and the worker claims each workspace's
  tenant (and the `system` actor) before the writes, under the policies — the pattern part 1 used
  for the deauthorize revoke. *Lean:* (a) for the sender sweep, whose statement is already one
  atomic, idempotent `INSERT … SELECT` whose correctness rests on the `NOT EXISTS` live-job check
  being evaluated in the same statement; (b) for the prompt sweep and the stranded alert, whose
  writes are per row, fire ledger triggers that read the actor GUCs, and are the same writes a
  member's own action makes. *Ratifier:* the owner. *Status:* built as leaned (the owner merged
  the plan, 2026-09-21; migration 082).
- **F2 — the reconciler's poll.** *Options:* (a) `fn_reconciler_sweep` already returns each
  ambiguous intent's workspace: the poll claims that tenant in its own session before the read;
  (b) a door returns the container id and account reference for an intent id. *Lean:* (a) — no new
  door, the read stays under the policy it was written for. *Ratifier:* the owner. *Status:* built
  as leaned (the poll takes the workspace its sweep row carries).

## Implementation Plan

### Dependencies

Migration 081 applied (it is: ledger head 81 on production since 2026-09-21 00:4x UTC). The API's
switch is independent of this plan and may go first.

### Blocks

The worker's switch (`f4_switch.sh worker`) and the close of #751.

### Steps

1. **Red first — the gates as `svc_worker`** in `tests/scripts/test_worker_login_gate.py`, on a
   replayed world of three workspaces (a bound Telegram group, a pending outbox row, a due
   `scheduled` intent, an `error` source, an `ambiguous` intent with a container id and one
   recorded check, an ended intent whose card is still live, a leased `plan_slot` job; a second
   workspace with an older due story; a third with a `prompt_pending` story): run
   `ensure_sender_jobs`, `sweep_due_prompts` (the full sweep, and bounded to one story inside a
   tenant claim followed by `finalize_job`), `sweep_settled_cards`, `alert_stranded_sources`, the
   poll closure and `reconcile_ambiguous` as `svc_worker` and expect what the owner login gets —
   asserting the effect under each workspace's own tenant, never the count a sweep reports —
   beside the vacuity guard (the plain reads as `svc_worker` see nothing) and the owner actor as
   the control. Four gates were red on the unfixed tree; the settled-card, scope and ladder gates
   were red on the first build.
2. **Migration 082** (`07` §25, the manifest, the ratified list, the advertised count):
   `fn_sender_sweep(p_prefix text, p_attempts int, p_deadline numeric, p_age numeric, p_limit int)
   RETURNS int` — the `INSERT … SELECT` verbatim with its literals as parameters, owned by
   `svc_maintenance`, EXECUTE `svc_worker`; `fn_prompts_due(p_limit int) RETURNS TABLE (…the
   `_CARD_SELECT` columns…)` and `fn_prompts_pending(p_limit int) RETURNS TABLE (o_id uuid,
   o_workspace_id uuid)` — the two cross-tenant reads of the prompt sweep; `fn_settled_cards(p_terminal
   text[], p_limit int)` — the settled-card sweep's selection, the terminal states as its argument
   so `intent_ledger.TERMINAL_STATES` stays the one spelling; `fn_stranded_sources(…)
   RETURNS TABLE (o_id uuid, o_workspace_id uuid)` — the read half of the alert's selection. The
   policies `svc_maintenance` lacks on `channel_bindings`, `media_items` and `media_sources`
   (SELECT, `USING (true)`), with their grants. The probes name the doors; the EXECUTE rows are a
   floor. The CREATE bracket.
3. **The code.** `ensure_sender_jobs` calls its door. `sweep_due_prompts` reads through
   `fn_prompts_due`, groups rows by workspace, and for each workspace claims the tenant and the
   `system` actor before `push_bindings`, `prompt_intent` and the transitions; the
   `prompt_pending` phase the same through `fn_prompts_pending`. `sweep_settled_cards` reads
   through `fn_settled_cards` and claims each row's workspace before its savepoint.
   `alert_stranded_sources` reads through its door and stamps `alerted_at` per workspace under the
   tenant (the single `UPDATE … RETURNING` becomes a read door plus a per-workspace `UPDATE … WHERE
   id = ANY(:ids)` — the same idempotence, since the stamp is what the read excludes). The claims
   are `unit_of_work.WorkspaceClaims`: one home, which records the caller's scope on the first
   claim and hands it back on `release()`. The poll closure takes the workspace id the
   reconciler's row carries and claims it before its read; the ladder's count moves out of
   `sweep_due` into `reconciler.checks_so_far`, read per row after the claim.
4. **The harness.** The new doors in `DOORS` (permitted `svc_worker`), the new policies in
   `POLICY_CENSUS`, the counts.
5. **The switch's check.** `f4_switch.sh worker` prints, before and after, the worker's boot line
   and one sweep cycle's counts from the logs (prompts minted, sender jobs minted); the runbook's
   worker section names the four paths and the door precondition (082).
6. **The battery** `tests/mutations/worker_login_doors.sh`: each read emptied; each write losing
   its tenant claim; the scope kept; the ladder counted before the claim; the file's own
   postconditions.
7. **Docs:** CHANGELOG, the plan README's F.4 row, `.claude/rules/database.md`'s bullet.

## Test Plan

The gates of step 1 (DB recipe), the RLS runtime harness, the lineage lane and the advertised
ratchet, the window and snapshot gates (they derive the last deployable version since #1333), the
existing prompt/sender/sync gates as the owner, the unit tests of `work_loop`, `prompts` and
`media_sync` (their fakes may match SQL by table name — list every test that imports each module
before changing its SQL, never through a truncated listing). Two review lenses on a detached
snapshot, a fold, a fresh re-verify; the battery on the committed tree, alone on the test
database; CI; the owner's admin squash.

## Verification Checklist

- [x] The four first gates failed on the unfixed tree for the reason named (0 minted / nothing
      prompted / `WITH CHECK` refused / `None`); the settled-card, scope and ladder gates failed
      on the first build; all pass with 082 and the code.
- [ ] `tests/scripts/test_rls_runtime_harness.py` registers every new door and policy.
- [ ] The battery kills every mutation, each verdict a real `N failed`.
- [ ] After the owner's `f4_switch.sh worker`: the boot line reads `svc_worker` / `False`; within
      one sweep cycle the logs show prompts and sender jobs minted at the same rate as before; the
      two fleet verdicts unchanged; a card delivered to a bound group.

## What NOT To Do

- Do not grant `svc_worker` `BYPASSRLS` or `USING (true)` policies on the tenant tables to make
  the sweeps pass — that is the owner login by another name.
- Do not make the writes cross-tenant inside a door where a per-workspace tenant claim suffices:
  the ledger's triggers attribute every write to the actor GUCs, and a definer write door hides
  the workspace from them.
- Do not switch the worker before 082 is applied and the gates are green: the first API switch
  showed what a blind sweep looks like in production, and the worker's sweeps are the product's
  delivery and approval loops.

## Context

Area: the worker's sweeps, the migrations, the RLS harness · Effort: M (one migration with five
doors and three policies; five modules; one gate file; the battery) · Risk: medium — the
prompting and delivery loops change shape; the gates as `svc_worker` and the owner-actor control are
what make it safe · Priority: the close of #751 and F.4.
