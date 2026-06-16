---
title: "Queue/Posting Simplification — Eliminate the Concurrency Race Class"
type: plan
status: draft
owner: astrid
tags: [storydump, queue, posting, concurrency, race-condition, data-model, scheduler, daily-cap, lease, idempotency, migration]
created: 2026-06-15
updated: 2026-06-15
---

# Queue/Posting Simplification — Eliminate the Concurrency Race Class

> **Plan PR for `/ironclad`.** This is a session-forged plan for a **production data-model + code change**. No code ships from this PR — it is the contract the team ratifies before implementation. Rationale and evidence live in the companion critique: `crog-eng-team/shared/planning/active/storydump-queue-concurrency-critique.md` (file:line anchors at `e36e4c7`).

## Goal

Eliminate the recurring queue/posting **race class** (`Queue item not found`, duplicate send, orphaned `processing`/`pending`, cap-hit row stranding, spinning buttons) by replacing the reconciliation machinery that *manufactures* it with three atomic primitives: an **atomic daily-cap admission gate**, an **atomic claim with a lease**, and **idempotent completion in one transaction**. A fourth, parallel track removes the single-event-loop / sync-DB substrate that distorts these paths.

**Mission alignment (`PROJECT_MISSION.md`):** the north star is *"zero-friction content automation… with as few manual steps as possible"* and the product guarantee is *"posts are never lost"* (`CLAUDE.md`, feature-flags). The `:79` "Queue item not found" miss **is a silently lost post**, and the duplicate-send class is an over-post. This plan directly defends both. Schema changes "require approval" per mission — that approval is the `/ironclad` + human ratification this PR exists to obtain.

**Non-goals:** no change to the multi-account/Instance tenancy model; no new platform integrations; no UX redesign. Tenant scoping (`chat_settings_id`) is preserved throughout.

## Current State

Verified against `main @ e36e4c7` (file:symbol anchors are durable; line numbers drift). Three independent code traces + a 4-month git history converged; full evidence in the critique.

- **No `posted` state on the queue.** `posting_queue.status ∈ {pending, processing, failed}` (named `CheckConstraint("…","check_status")`, `src/models/posting_queue.py`). A *successful* post **deletes** the row and writes a `posting_history` row. "This post happened" is therefore spread across **6 authoritative representations in 4 stores** (history row; queue-row deletion; queue `telegram_message_id`+`processing`; `media_items.times_posted`; `media_posting_locks`; `chat_settings.last_post_sent_at`), read by 7 decision points.
- **Cap = live COUNT.** `daily_cap.can_post_today` → `history_repository.count_posts_today` COUNTs `posting_history WHERE status='posted' AND success AND posted_at >= day_start(tz)`. No composite `(chat_settings_id, posted_at, status)` index exists. The cap check is a **separate read transaction, outside** the completion write.
- **Claim = bare status flip, no lease.** `queue_repository.claim_for_processing` does `… WHERE id=? AND status IN ('pending','processing') FOR UPDATE SKIP LOCKED` → `status='processing'`. Per-`queue_id` mutual exclusion is an `asyncio.Lock` one layer up (`telegram_service.get_operation_lock`); **batch-approve skips that lock**.
- **Sweepers = wall-clock GC.** `delete_stale_pending` (deletes `pending ∧ telegram_message_id IS NULL ∧ created_at ≤ now−10min`) and `requeue_stale_processing` (same predicate, `processing` → `pending`), `src/repositories/queue_repository.py`. The **10 min is a hardcoded default param**, predicates carry **no tenant filter**, and both run **once per active chat per tick** (`scheduler.py` top of `process_slot`, invoked in the per-chat loop in `scheduler_loop.py`). A third, `discard_abandoned_processing` (24h), plus an hourly auto-prune loop, also exist. **The `:79` race is open at HEAD:** `delete_stale_pending` can hard-delete a row the in-flight send path re-reads via `get_by_id` (`telegram_notification.py`).
- **Completion is non-atomic on 3 of 5 paths.** Only the manual **Posted**/**Reject** paths wrap writes in `TelegramCallbacksCore._shared_session()` (forces all repos onto one session, monkey-patches repo `commit`→`flush`, single real commit at exit). **`_auto_approve` (`scheduler.py`), autopost (`telegram_autopost.py`), and the failure path commit independently** — the orphan-prone outliers.
- **Cap-check position diverges across paths.** Scheduler / `/next` / auto-approve **gate** (cap before claim). The **Posted** and **autopost** buttons + **batch-approve** are **claim-then-abort**; only Posted restores; autopost and batch-approve **strand** the row.
- **In-flight band-aid:** **PR #509** (`fix/autopost-cap-orphans-queue-row`, **OPEN**) adds restore-only on the autopost cap-hit and explicitly *drops its pre-claim gate*. It is the next instance-patch in the lineage — **Phase 1 supersedes it**.
- **Substrate:** PTB poller + 60s scheduler + media-sync on **one** asyncio loop (`main.py`); **sync** psycopg2 (`config/database.py`) called inline with no offload; `concurrent_updates` off (`telegram_service.py`). Every DB round-trip blocks the loop → the hang regression.

### Operational ground truth (constrains every phase)

| Fact | Source | Consequence for this plan |
|------|--------|---------------------------|
| Migrations are hand-written numbered SQL (`scripts/migrations/NNN_*.sql`, idempotent DDL, `schema_version` insert) | recon | "Additive migration" = a new `NNN_*.sql`; **not** Alembic |
| **No downgrade path** | recon | Every "rollback" = a hand-written compensating forward migration `NNN_revert_*.sql` |
| **Migrations are NOT auto-applied on deploy** (Railway runs code only; `psql` against Neon is a manual human gate) | `railway.toml`, `Procfile` | Each schema phase needs an explicit **apply-before-deploy runbook step**; additive-first ordering is mandatory |
| **Migrations are NOT exercised by tests/CI** (test DB built from `Base.metadata.create_all()`, not the SQL) | `tests/conftest.py`, `ci.yml` | A broken/forgotten migration **ships green** → Phase 0 |
| `check_status` / `check_history_status` are **named** constraints | models + `034_*.sql` | A new status = drop-by-name + re-add **+ ORM inline constraint in lockstep** |
| Repos commit internally; atomic multi-write only via `_shared_session()` | recon | Phase 3 retrofits the 3 non-atomic paths onto that one contract |
| CI `changelog-check` fails PRs that touch **non-doc** files without a `CHANGELOG.md` edit | `ci.yml` | Implementation PRs must update `CHANGELOG.md`; this docs-only plan PR does not |
| `posting_history` is **append-only / never mutated in prod** | `CLAUDE.md` | Idempotency/cap must not require rewriting history rows |

## Architecture

**Target: one claim/complete contract, used by all six posting paths — no parallel paths, no shims** (`consolidate-dont-fork`, `no-backwards-compat`; the current 6-paths-each-re-deriving-the-claim *is* the disease). Primitives live in the **Repository** layer; services orchestrate (respects `CLAUDE.md` strict layering).

```
                 ┌─────────────────────── one shared contract ───────────────────────┐
 6 paths  ─────► │  admit_and_claim(queue_id, chat)            complete(queue_id, …)  │
 (scheduler,     │   ▸ atomic cap reservation  (Phase 1, F2)    ▸ ONE txn via shared  │
  /next,         │   ▸ atomic claim + lease    (Phase 2, F3)      session (Phase 3)   │
  auto-approve,  │     status: ready → claimed                  ▸ idempotency key      │
  Posted,        │                                                (Phase 3, F6) → no-op│
  autopost,      │  reclaim(): WHERE lease_expires_at < now()   on duplicate          │
  batch-approve) │   ▸ once per tick, tenant-aware, releases cap reservation (Phase 2) │
                 └────────────────────────────────────────────────────────────────────┘
   substrate (Phase 4, parallel): DB off the event loop + concurrent_updates
```

- **Admission (Phase 1):** the daily cap becomes an **atomic reservation taken before the irreversible send** (you cannot un-send), released on failure/reclaim — not a COUNT-then-decide. Mechanism is **F2** (open).
- **Claim + lease (Phase 2):** the claim atomically sets `status='claimed'`, `claimed_by`, `lease_expires_at`. Reclamation is **deterministic lease expiry**, replacing all wall-clock sweepers. The lease and the cap reservation are **coupled** — one mechanism reserves, completes, or releases.
- **Completion (Phase 3):** every path completes in **one transaction** via the existing `_shared_session()` template, guarded by an **idempotency key (F6)** so a double-tap / redeploy-double-instance is a no-op. Replaces `get_by_queue_item_id`-as-recovery and the duplicate-callback patch lineage.
- **Net effect is subtraction:** Phases 1–3 **delete** `delete_stale_pending`, `requeue_stale_processing`, the per-chat sweep cadence, the hourly prune, the 10-min hardcode, claim-then-abort/restore (incl. #509), and the history-lookup recovery path.

## Phases

Each phase is independently shippable, **additive-migration-first**, net-deletes code, and removes one symptom family. `S/M/L` sizing only (no time estimates). Within a phase, paths are cut **lowest-risk first**: `scheduler → /next → auto-approve → batch-approve → Posted → autopost`.

### Phase 0: Migration & rollout safety rails — *Prerequisite* (S)

> Added from recon, not in ari's original 1–4 enumeration. **Ratifier must confirm scope** (see Adversarial Findings). It exists because Phases 1–3 ship prod schema on a process that is untested in CI and manually applied — the single largest risk here.

- **Deliverables:** (1) a CI job that applies `scripts/migrations/*.sql` in order to a throwaway Postgres and asserts the result matches `Base.metadata.create_all()` (model↔SQL **drift check**); (2) a documented **deploy runbook** in `documentation/operations/` (apply order, `schema_version` verification, apply-migration-**before** code deploy, post-apply smoke check); (3) a `scripts/migrations/README` convention note for compensating `NNN_revert_*.sql`.
- **Additive migration:** none (tooling/CI only).
- **Validation:** CI drift-check fails on a deliberately-broken fixture migration and passes on `main`; runbook dry-run against a Neon **branch** (copy-on-write, cents) applies cleanly.
- **Rollback:** remove the CI job and runbook (zero prod impact).

### Phase 1: Atomic daily-cap admission (M)

- **Goal:** the daily cap is an atomic admission gate that **cannot be exceeded under concurrency** and requires **no claim-then-abort/restore**. Supersedes #509.
- **Additive migration (per F2):** either a new `daily_post_counts(chat_settings_id, post_day, count, …)` table (F2-a) **or** a composite `idx_posting_history_chat_posted_status (chat_settings_id, posted_at, status)` index (F2-b). Both are additive; `ADD … IF NOT EXISTS` + `schema_version` insert; new table is also auto-created by `create_all` once registered in `init_db()` (still needs the prod SQL).
- **Path-by-path rollout:** implement one **admission helper** (Repository layer) that atomically *reserves* a slot (`… WHERE count < cap`) and returns success/failure; reserve **before** the irreversible send, **release on send-failure**. Cut all six paths' `can_post_today` call-sites to it in order. **Close/supersede #509** (restore-only is unnecessary once the cap gates pre-claim). Per F4, the transient old-path coexistence is deleted before the phase closes.
- **Validation (test DB only — never trigger real posting, `CLAUDE.md`):** new test in `tests/src/services/` driving **N concurrent claims at cap−1** asserts **posts ≤ cap** and **zero rows stranded in `processing`**; extend `tests/src/services/test_daily_cap.py`; assert the autopost cap-hit no longer orphans (the #509 scenario) and no second blocking COUNT is added to the callback path.
- **Rollback:** compensating `NNN_revert_*.sql` drops the table/index; revert the admission helper to `count_posts_today`. (If F2-a: the counter is rebuildable from `posting_history`.)
- **Deletes:** claim-then-abort + restore on the button paths; the need for #509.

### Phase 2: Claim-lease replaces the wall-clock sweepers (M) — *closes `:79`*

- **Goal:** reclamation becomes deterministic (lease expiry), so the GC can never delete a live row. Closes the `:79` "Queue item not found" race.
- **Additive migration:** `ALTER TABLE posting_queue ADD COLUMN IF NOT EXISTS claimed_by TEXT, ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ, ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ` (`updated_at` mirrors the existing `chat_settings` template). No status change in this phase. New columns nullable → safe on a live table.
- **Path-by-path rollout:** extend the shared claim to set `claimed_by` + `lease_expires_at = now() + TTL` (F3). Add a single **`reclaim()`** repo method (`WHERE lease_expires_at < now()` → `status='ready/pending'`, release the cap reservation), called **once per tick** (not per chat), tenant-aware. **Delete** `delete_stale_pending`, `requeue_stale_processing`, the per-chat sweep invocation, and the hourly prune loop. TTL is **config, not hardcoded**.
- **Validation:** reproduce the critique's `:79` scenario (a slow in-flight row) and assert it is **never deleted while the lease is held**; assert a genuinely stuck row is reclaimed **exactly at lease expiry**; update `tests/src/services/test_scheduler_queue_reliability.py` and `tests/src/repositories/test_queue_repository.py`; assert no cross-tenant deletion.
- **Rollback:** compensating migration drops the columns; revert the PR to restore the sweeper calls (kept in git history, not resurrected as a parallel path).
- **Deletes:** 3 sweepers' wall-clock logic, the 10-min hardcode, the per-chat O(N) sweep cadence, the hourly prune.

### Phase 3: Idempotent completion in one transaction (M/L)

- **Goal:** every path completes atomically; a duplicate completion is a no-op. Removes the history-lookup recovery path and the duplicate-callback lineage's reason to exist.
- **Additive migration:** an **idempotency key (F6)** — e.g. a partial **unique** index on `posting_history(queue_item_id)` (one terminal outcome per queue item). The forward migration **must dedup any pre-existing duplicates first** (cannot add a unique index over dupes) — a guarded, append-only-respecting backfill, reviewed separately. Plus the composite cap index if not added in Phase 1.
- **Path-by-path rollout:** retrofit the **3 non-atomic paths** (`_auto_approve`, autopost, scheduler auto-reapproval) onto `_shared_session()` so history-write + counter increments + queue-resolution + cap-confirm are one txn; make completion **idempotent** via the key (`ON CONFLICT DO NOTHING`). This is where **F1** (terminal `posted` status vs delete-in-same-txn) is realized. **Delete** `get_by_queue_item_id`-as-recovery and the duplicate-callback defensive layers it backstops.
- **Validation:** fire the same completion twice → **exactly one** `posting_history` row, **no** double counter increment, **no** double cap consumption; simulate a crash mid-completion → **no partial state**; cover `_auto_approve` explicitly (the outlier); update `test_telegram_autopost.py`, `test_telegram_callbacks_*.py`, scheduler tests.
- **Rollback:** compensating migration drops the unique index (data-safe — dropping an index never loses rows); revert the session retrofit per path.
- **Deletes:** the history-reconciliation recovery path; the duplicate-callback patch lineage's load-bearing role.

### Phase 4: DB-off-loop foundation (L) — *parallel track, scope per F5*

- **Goal:** remove the single-event-loop / sync-DB substrate that blocks the loop (the button-hang regression) and that originally distorted the cap design toward claim-then-abort.
- **Additive migration:** none.
- **Rollout:** offload synchronous DB off the event loop on the hot paths (`asyncio.to_thread`/executor, or migrate hot paths to an async engine); **then** enable PTB `concurrent_updates` (only safe after offload). Independent of Phases 1–3 for correctness; it removes the *latency* objection and is the standing hang fix.
- **Validation:** `py-spy dump` on the worker during load shows **no** main-thread parked in psycopg2/socket reads; the button-hang repro no longer reproduces; a concurrency test asserts handlers run concurrently.
- **Rollback:** revert (no schema); disable `concurrent_updates`.

## Decision Forks

### Fork F1: Terminal `posted` status vs write-history-in-same-txn-as-delete
- **Context:** today success **deletes** the queue row; "posted" lives only in `posting_history`. How should the single-source lifecycle be represented?
- **Options:**
  - **(a)** Add a terminal `posted` status to `posting_queue` and keep the row — durable single-row lifecycle, but requires drop/re-add of the **named** `check_status` + ORM inline constraint in lockstep, and the queue table now grows (needs archival/pruning).
  - **(b)** Keep delete-on-success, but write history **and** delete in the **same** `_shared_session()` transaction across all paths — no constraint surgery, preserves the "queue = active work only" intent; idempotency comes from F6, not row retention.
- **Lean:** **(b)** — smaller blast radius, no constraint/ORM-lockstep risk, no table-growth/archival burden, and it fixes the actual defect (non-atomic completion). Realized in Phase 3.
- **Ratifier:** human (Chris) — data-model shape (mission: schema changes require approval).
- **Status:** open
- **Evidence:** critique §Q1; recon §2 (named constraints), §3 (`_shared_session`).

### Fork F2: Cap as counter-row `UPDATE … WHERE n < cap` vs conditional insert
- **Context:** the cap must **reserve atomically before the irreversible send**, then confirm on success / release on failure. (A post-hoc conditional insert into `posting_history` only *detects* over-cap after the send — too late — so it can't be the sole gate.)
- **Options:**
  - **(a)** New `daily_post_counts` counter row, reserved via one atomic `UPDATE … SET count=count+1 WHERE count < cap RETURNING` (insert-on-conflict for day rollover), released via `count=count−1`. O(1); natural reservation primitive; **but** a second representation of "posts today" alongside `posting_history` (rebuildable from history; treat history as the immutable audit ledger and the counter as the live admission gate — admission-control vs audit are different questions).
  - **(b)** Conditional insert of a **reservation row** (e.g. a `reserved` history status) gated on `… < cap` then transitioned — keeps one table, but adds status surgery and writes to the append-only history table for in-flight state.
- **Lean:** **(a)** counter-row reservation, **coupled to the lease (F3)**: the claim reserves, completion confirms, lease-expiry releases — one mechanism. Flag the `consolidate-dont-fork` tension explicitly: the counter is admission-control state, not a fork of the audit ledger, and is deterministically rebuildable.
- **Ratifier:** eng-lead / ari (mechanism), with human sign-off on the new table (schema).
- **Status:** open
- **Evidence:** critique §Q2/§simpler-design; recon §2 (no cap index), §3 (cap outside txn).

### Fork F3: Lease TTL + reclamation cadence
- **Context:** too-short TTL re-creates the current "reclaim a slow-but-live row" bug; too-long delays recovery of genuinely stuck rows.
- **Options:**
  - **(a)** Short TTL (~2–3 min), fast recovery — risks reclaiming legitimately slow sends (IG post path is bounded ~180s).
  - **(b)** TTL comfortably above worst-case send (~10 min to start), **config-driven**, reclamation **once per scheduler tick** (global, single query) — safe margin, tunable.
- **Lean:** **(b)** — TTL as configuration set above the bounded worst-case send, reclaim once per tick. Never hardcode (the current 10-min hardcode is the smell being removed).
- **Ratifier:** eng-lead / ari (operational tuning).
- **Status:** open
- **Evidence:** critique §Q2; recon §6 (IG path bound); knowledge `hang-perf-regression-2026-06-12`.

### Fork F4: Rollout style — big-bang-per-phase vs staged path-by-path
- **Context:** `consolidate-dont-fork`/`no-shims` forbids **permanent** parallel paths; safe prod rollout wants **incremental** cutover. Reconcile.
- **Options:**
  - **(a)** Big-bang per phase — cut all six paths to the new primitive in one PR per phase. Cleanest, honors no-shims, but larger blast radius per PR.
  - **(b)** Staged path-by-path within a phase — cut one path, validate, next; **hard rule:** the transient dual-path is **deleted before the phase is marked done** (a tracked debt, not a permanent fork). Order: internal paths → user buttons.
- **Lean:** **(b)** with the deletion gate — incremental safety for a prod posting system, without leaving a permanent parallel path. The shared helper means "all six paths" is one helper + six call-site swaps regardless.
- **Ratifier:** human (Chris) — prod-rollout risk posture.
- **Status:** open
- **Evidence:** principles `consolidate-dont-fork`, `no-backwards-compat`; recon §6 (six call-sites).

### Fork F5: Scope — ship Phases 1–3 now with 4 as a parallel track, or include 4
- **Context:** Phases 1–3 are correctness (eliminate the race class); Phase 4 is the substrate/perf fix (the hang), L-sized and independent.
- **Options:**
  - **(a)** Ship 1–3 now; run 4 as an independent parallel foundation track (also the standing hang fix). 1–3 don't require 4 for correctness — Phase 1's reservation is one guarded `UPDATE`, *cheaper* than today's COUNT+`get_settings`, so it doesn't worsen the loop.
  - **(b)** Include 4 in the same sequence (1→2→3→4) — single coordinated effort; couples correctness delivery to a larger perf rewrite.
- **Lean:** **(a)** — decouple correctness from the L-sized substrate work; escalate 4's priority only if prod hangs are acute.
- **Ratifier:** human (Chris) — scope/sequencing.
- **Status:** open
- **Evidence:** critique §verdict/§sequencing; knowledge `hang-perf-regression-2026-06-12`.

### Fork F6: Idempotency key location/shape (Phase 3)
- **Context:** recon found **no natural unique key** for idempotent completion; one must be added.
- **Options:**
  - **(a)** Partial unique index on `posting_history(queue_item_id)` — one terminal outcome per queue item; `ON CONFLICT DO NOTHING` makes double-completion a no-op. Requires a dedup backfill first.
  - **(b)** A composite idempotency key (e.g. `(media_item_id, post_day)` or an explicit `idempotency_key` column set at claim) — survives even if `queue_item_id` is reused; more design surface.
- **Lean:** **(a)** — `queue_item_id` is already carried on `posting_history`, the semantics ("one outcome per queue item") are exactly right, and dropping the index is data-safe rollback. The dedup backfill is reviewed as its own guarded step (history is append-only in prod).
- **Ratifier:** eng-lead / ari (mechanism), human sign-off on the backfill.
- **Status:** open
- **Evidence:** recon §2 (no unique key); critique §simpler-design (idempotent completion).

## Companion Plans

- **Critique (rationale/evidence):** `crog-eng-team/shared/planning/active/storydump-queue-concurrency-critique.md`.
- **Knowledge:** `shared/knowledge/storydump/hang-perf-regression-2026-06-12.md` (Phase 4 substrate), `…/queue-item-not-found-regression-2026-06-12.md` (the `:79` race, Phase 2).
- **In-flight PR superseded:** #509 `fix/autopost-cap-orphans-queue-row` (closed/superseded by Phase 1).
- No conflicting active plan found in `documentation/planning/` or `shared/planning/active/`.

## Dependencies

| Dependency | Blocks | Risk |
|------------|--------|------|
| Phase 0 (migration safety rails) | 1, 2, 3 (all schema phases) | High if skipped — untested migrations ship green |
| Phase 1 (cap reservation) | 2 (lease couples to the reservation) | Med |
| Phases 1 + 2 | 3 (completion confirms reservation + lease) | Med |
| Phase 4 | none (parallel) | Low for correctness; High for the hang symptom |
| #509 disposition | Phase 1 start | Low — coordinate close/supersede with author (alex) |

## Risks

| Risk | Impact | Mitigation |
|------|--------|-----------|
| Migrations not auto-applied/tested → bad migration ships green | High | **Phase 0** (CI drift-check + deploy runbook); additive-first; test on a Neon branch |
| No downgrade tooling | Med | Every rollback = compensating `NNN_revert_*.sql`, written **with** the forward migration, not after |
| Named `check_status` surgery (if F1-a) | Med | Lean F1-b avoids it; if F1-a, drop-by-name + ORM inline constraint in **one** change |
| `_auto_approve` non-atomic outlier | Med | Phase 3 explicitly retrofits it onto `_shared_session()` |
| Over-cap during cutover window | Med | Reservation atomicity + additive-first + internal-paths-first order (F4) |
| Idempotency unique index over pre-existing dupes | Med | Forward migration includes a guarded dedup backfill; history append-only respected |
| #509 collision | Low–Med | Close/supersede in Phase 1; coordinate with alex |
| `expire_on_commit=False` stale reads | Low | Keep cap-confirm + completion in one txn; re-read within the txn |
| Cross-tenant sweep already global today | Low (existing) | Phase 2 reclaim is tenant-aware by design |

## Validation Strategy

- **Per phase:** reproduce the *specific* race from the critique/knowledge repros, prove it closed, then assert the deleted machinery is gone. All tests run against the **Postgres test DB** (`tests/conftest.py` / CI `postgres:15`); **never** trigger real posting (`CLAUDE.md` safety rules) — no `process-queue`, no `python -m src.main`, no mutating SQL on `posting_history`.
- **Cross-cutting (Phase 0):** the migration drift-check is the *only* thing that makes "the migration is correct" a testable claim — without it, validation criteria that say "tests confirm the migration" are false (tests build schema from the ORM, not the SQL).
- **Acceptance (objective):** (1) a concurrency test posts **≤ cap** with zero stranded rows; (2) the `:79` repro yields **zero** "Queue item not found" with a live lease; (3) double-completion → **exactly one** history row; (4) `py-spy` shows no loop-blocking DB (Phase 4); (5) CI green incl. `changelog-check` on each implementation PR.
- **Prod signal:** the historical log signatures (`Queue item not found: <id>`, `Requeuing stale processing item <id>`, pool-timeout waits) drop to zero post-rollout.

## Complexity and Sequencing

| Phase | Size | Depends on | Parallel with |
|-------|------|-----------|---------------|
| 0 — Migration safety rails (prereq) | S | — | everything |
| 1 — Atomic cap admission | M | 0 | 4 |
| 2 — Claim-lease replaces sweepers (`:79`) | M | 0 (and 1 for reservation coupling) | 4 |
| 3 — Idempotent completion | M/L | 1, 2 | 4 |
| 4 — DB-off-loop foundation | L | — | 1, 2, 3 |

Critical path: **0 → 1 → 2 → 3**. **4** runs in parallel (F5-a). Complexity profile: **S×1, M×2, M/L×1, L×1**. Each phase is net-negative LOC.

## Adversarial Review Findings

Pre-handoff stress test — blind spots surfaced before review:

1. **Scope deviation (must ratify):** ari enumerated Phases 1–4; I added **Phase 0** because recon proved the migration process is untested + manually applied + has no rollback tooling. Shipping a prod *data-model* change on that is the dominant risk. **Ratifier must explicitly accept Phase 0, fold it into Phase 1, or consciously accept the manual-gate risk.** I did not silently expand scope — flagging it here.
2. **Path-naming deviation:** dispatch said `documentation/plans/`; the repo's established dir is `documentation/planning/` (5+ existing plans). I used the established dir per codebase-consistency. Trivial to move if the ratifier prefers.
3. **F2 hides a real semantic shift:** "atomic cap precondition" actually means **reserve-before-irreversible-send + release-on-failure**, not just a guarded write. If F2-a is chosen, a second "posts today" representation exists — defensible (admission vs audit) but it *is* in tension with this plan's own thesis. Called out in F2; `/ironclad` should pressure-test it hardest.
4. **Phase ordering assumption:** Phase 2's lease couples to Phase 1's reservation. If the ratifier reorders (2 before 1), the reservation-release-on-reclaim must be designed without the counter — re-open F2/F3 together.
5. **Idempotency backfill is the sharp edge:** adding a unique index (F6-a) over a table that may already contain duplicate `queue_item_id` rows will fail; the dedup backfill touches the append-only `posting_history` and needs its own review + human sign-off.
6. **"Delete the sweepers" is load-bearing:** if Phase 2 ships but the lease TTL (F3) is mis-tuned, removing the sweepers removes the safety net simultaneously. Mitigation: Phase 2 validation must prove deterministic reclaim **before** the sweeper deletion lands in the same PR (revert = the rollback).

---
*Forged by `/first-principles` → `/forge`. Ready for `/ironclad` multi-lens review. Forks F1–F6 open; Phase 0 scope pending ratifier confirmation.*
