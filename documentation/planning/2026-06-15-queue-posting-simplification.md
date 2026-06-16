---
title: "Queue/Posting Simplification — Eliminate the Concurrency Race Class"
type: plan
status: draft
owner: astrid
tags: [storydump, queue, posting, concurrency, race-condition, data-model, scheduler, daily-cap, lease, idempotency, migration, ironclad-cycle-1]
created: 2026-06-15
updated: 2026-06-15
---

# Queue/Posting Simplification — Eliminate the Concurrency Race Class

> **Plan PR for `/ironclad`. Revised after Cycle 1 (2026-06-15).** Production data-model + code change; no code ships from this PR. Cycle-1 verdict was **NOT CONVERGED** (2 blockers + 1 critical design gap + 9 majors), with the **architecture and root-cause diagnosis endorsed by all 5 lenses** and gating items called "protocol/coverage details, not redesigns." This revision resolves all three blockers, the 9 majors, the 10 gaps, and the 5 questions, and sharpens two genuine ratifier calls into forks **F7** and **F8** for Chris. See the **Cycle-1 Review Response** map below; rationale/evidence in the companion critique `crog-eng-team/shared/planning/active/storydump-queue-concurrency-critique.md`.

## Goal

Eliminate the recurring queue/posting **race class** (`Queue item not found`, duplicate send, orphaned `processing`/`pending`, cap-hit stranding, spinning buttons) by replacing the reconciliation machinery that *manufactures* it with atomic primitives: an **atomic daily-cap admission gate**, an **atomic claim with a lease**, a **durable pre-send marker**, and **idempotent completion in one transaction**. A parallel track removes the single-event-loop / sync-DB substrate.

**Mission alignment (`PROJECT_MISSION.md`):** north star *"zero-friction content automation… posts are never lost."* The `:79` "Queue item not found" miss **is a silently lost post**; the duplicate-send class is an over-post. Schema changes "require approval" — that approval is the `/ironclad` + human ratification (Chris) this PR exists to obtain. **Non-goals:** no change to the multi-account/Instance tenancy model; no new integrations; no UX redesign.

## Cycle-1 Review Response (delta map for Cycle 2)

| Item | Resolution | Where |
|------|-----------|-------|
| **B1** full unique index breaks failed-post re-approval | F6 → **PARTIAL** unique index `WHERE status='posted'` | F6, Phase 3 |
| **B2** dedup backfill mutates append-only history | **No-backfill route** (partial index `WHERE created_at >= '<migration_date>'`), else explicit criteria + Chris sign-off; split index+backfill into a guarded fast-follow | **F8 (new)**, Phase 3 |
| **Critical** send-then-crash double-post window | **Durable pre-send marker** + pinned 4-step ordering; reclaim-after-confirmed-send **out of scope** (manual repair); new Phase-2 validation case | Architecture, Phase 2 |
| **R1** cutover cap race | F4 lean now **requires** a reject-safe cutover feature-flag | **F7 (new)**, F4, Phase 1 |
| **R2** dep table wrong | Phase 4 split; **4b `concurrent_updates` depends on Phase 3** | Dependencies, Complexity |
| **R3** Phase 0 DDL-only | Extend to a **data-migration test harness** + byte-faithful index/constraint diff | Phase 0 |
| **R4** status-vocab contradiction | Pin: claim keeps **`processing`** + lease cols; reclaim → **`pending`**; `ready/claimed` conceptual only | Architecture, Phase 2, F1 |
| **R5** tz-naive vs TIMESTAMPTZ | Standardize `datetime.now(timezone.utc)`, ORM `DateTime(timezone=True)`, tz-aware repro | Phase 2 |
| **R6** concurrency untestable | **Multi-connection test harness** as an explicit deliverable | Phase 0/1 |
| **R7** counter rebuildability gap | Counter declared **authoritative (not rebuilt) until Phase 3**; reservation-identity release keeps it consistent | F2, Phase 1 |
| **R8** deleting sweepers early | Ship Phase 2 with sweepers **no-op behind `WHERE lease_expires_at IS NULL`**; delete only after TTL validated in prod | Phase 2 |
| **R9** PR #483 sixth sweeper | Added to **Superseded** beside #509; coordinate with author **Chris** | Current State, Companion |
| **G1/G10** counter tz + release floor/identity | tz-aware `post_day`; release `WHERE count>0` tied to reservation identity | F2 |
| **G2/Q3** `discard_abandoned_processing` (24h) | **Kept as backstop** until lease TTL validated, then retired | Phase 2 |
| **G3** vestigial cleanup scope (#111-scale) | Named test/log/doc cleanup surface as a Phase-2 deliverable | Phase 2 |
| **G4** index build mechanics | `CREATE UNIQUE INDEX CONCURRENTLY` outside a txn, after dedup commits | Phase 3 |
| **G5** rollback ordering | Symmetric rule in the Phase-0 runbook | Phase 0 |
| **G6** path count | **6 paths canonical** (batch-approve included) | Current State |
| **G7** `updated_at` garnish | Dropped from the Phase-2 migration | Phase 2 |
| **G8** `schema_version` startup check | Phase 0 references **#190** as a building block | Phase 0 |
| **G9** model registration | Register `daily_post_counts` in the `init_db()` import graph | Phase 1 |
| **Q1** dedup authoritative-row | Ratifier call → **F8** | F8 |
| **Q2** completion ordering | Pinned: cap-reserve → pre-send marker → send → completion | Phase 2 |
| **Q4** `_auto_approve` retrofit composes? | Explicit verification item (non-raising dict-return path) | Phase 1 |
| **Q5** #95 invariant under reclaim | Explicit Phase-2 validation case | Phase 2 |

## Current State

Verified against `main @ e36e4c7` (file:symbol anchors are durable; line numbers drift). Full evidence in the critique.

- **No `posted` state on the queue.** `posting_queue.status ∈ {pending, processing, failed}` (named `CheckConstraint("…","check_status")`). A successful post **deletes** the row and writes `posting_history`. "This post happened" is spread across **6 representations in 4 stores**, read by 7 decision points.
- **Cap = live COUNT** over `posting_history` (status='posted' ∧ success ∧ tz-aware `posted_at >= day_start(tz)`); no composite `(chat_settings_id, posted_at, status)` index; the cap check is **outside** the completion write.
- **Claim = bare status flip, no lease** (`claim_for_processing`, `FOR UPDATE SKIP LOCKED` → `processing`); per-id mutual exclusion is an `asyncio.Lock` one layer up (**batch-approve skips it**).
- **Wall-clock GC sweepers** (`delete_stale_pending`, `requeue_stale_processing`, predicate `status ∧ telegram_message_id IS NULL ∧ created_at ≤ now−10min`, **10 min hardcoded**, **no tenant filter**, **once per active chat per tick**), plus `discard_abandoned_processing` (24h) and an hourly prune loop. **The `:79` race is open at HEAD.**
- **Completion non-atomic on 3 of 5 paths** — only manual Posted/Reject use `_shared_session()`; `_auto_approve`, autopost, and the failure path commit independently.
- **Six posting paths are canonical** (G6 — undercounting to 5 by omitting batch-approve is how prior fixes regressed): `process_slot`, `force_send_next` (`/next`), `_auto_approve`, manual Posted (`_do_complete_queue_action`), autopost (`_do_autopost`), batch-approve (`handle_batch_approve`).
- **In-flight band-aids this plan supersedes:** **PR #509** (`fix/autopost-cap-orphans-queue-row`, restore-only) **and PR #483** (`feat/posting-queue-cleanup-loop`, the *sixth sweeper* — an hourly `posting_queue` auto-prune loop; author **Chris**). Both are OPEN; one root, one should yield (R9).
- **Substrate:** PTB poller + 60s scheduler + media-sync on one asyncio loop; sync psycopg2 inline, no offload; `concurrent_updates` off → the hang regression.

### Operational ground truth (constrains every phase)

| Fact | Consequence |
|------|-------------|
| Migrations = hand-written numbered SQL (`scripts/migrations/NNN_*.sql`), idempotent DDL, `schema_version` insert | "Additive migration" = a new `NNN_*.sql`; not Alembic |
| **No downgrade path** | Rollback = compensating forward `NNN_revert_*.sql` |
| **Not auto-applied on deploy** (`psql` against Neon is a manual gate) | Per-phase apply-before-deploy runbook; additive-first mandatory; symmetric rollback ordering (G5) |
| **Not exercised by tests/CI** (test DB from `Base.metadata.create_all()`, not the SQL) | Phase 0 migration/data harness; `#190` `schema_version` startup check is a building block (G8) |
| `check_status`/`check_history_status` are **named** constraints | Avoid new statuses (R4) → no drop/re-add surgery |
| Repos commit internally; atomic multi-write only via `_shared_session()` | Phase 3 retrofits the 3 non-atomic paths |
| Test fixture = one session in a savepoint | Cannot express claim/cap contention → **multi-connection harness** (R6) |
| `posting_history` is append-only / never mutated in prod | No-backfill idempotency route preferred (B2/F8) |
| CI `changelog-check` exempts docs-only PRs | This plan PR is green; implementation PRs must update `CHANGELOG.md` |

## Architecture

**One claim/complete contract, six paths, no parallel paths, no shims** (`consolidate-dont-fork`). Primitives live in the **Repository** layer; services orchestrate (`CLAUDE.md` strict layering). **Status vocabulary is pinned (R4): no new status value.** `ready`/`claimed` are *conceptual* labels; on disk a claim keeps **`status='processing'`** and sets lease columns, and reclaim sets **`status='pending'`**.

**Pinned completion protocol (Q2 / critical gap) — the 4-step ordering:**

```
1. cap-reserve        atomic admission (Phase 1, F2) — reserve a slot WHERE count < cap
2. pre-send marker    durable, committed in its OWN txn IMMEDIATELY BEFORE the irreversible send (Phase 2)
3. send               Telegram/Instagram (irreversible; no server-side dedup)
4. completion         ONE txn (Phase 3): history + counters + queue-resolution + idempotency key

reclaim() (Phase 2, per tick, tenant-aware, WHERE lease_expires_at < now()):
   • marker ABSENT → never sent → safe to requeue + release the cap reservation
   • marker PRESENT → attempted → do NOT re-send, do NOT release; route to manual repair
```

Marker-before-send means its presence ⇒ "send was attempted," so reclaim never re-issues a sent post (no double-post). The **irreducible window** is the marker-commit→send-issue gap (microseconds): a crash there leaves a marker with no post — **surfaced for manual repair, not silently lost, and never double-posted.** **Reclaim-after-confirmed-send (auto-recovering a sent-but-uncompleted post) is explicitly out of scope.** This is the #95 lesson ("reset only causes re-sends") honored, not relocated into `reclaim()`.

Net effect is **subtraction**: Phases 1–3 retire `delete_stale_pending`, `requeue_stale_processing`, the per-chat cadence, the hourly prune (#483), the 10-min hardcode, claim-then-abort/restore (#509), and the history-lookup recovery path.

## Phases

Additive-migration-first; net-deletes code; `S/M/L` sizing only. Within a phase, paths cut **lowest-risk first**: `scheduler → /next → auto-approve → batch-approve → Posted → autopost`.

### Phase 0: Migration, data & concurrency safety rails — *Prerequisite* (M)

> Not in ari's original 1–4. **Ratifier must confirm scope.** It exists because Phases 1–3 ship prod schema **and data** on a process untested in CI, manually applied, with no rollback tooling — the dominant risk.

- **Deliverables:** (1) CI job applying `scripts/migrations/*.sql` in order to a throwaway Postgres, asserting the result matches `Base.metadata.create_all()` (DDL drift check) **and a byte-faithful comparison of partial-index predicates + named constraints** (R3); (2) a **data-migration test harness** (R3): *seed duplicate `posted` rows → run the dedup/no-backfill path → assert; seed history → run counter-init → assert*; (3) a **multi-connection concurrency harness** (R6) — N real psycopg2 connections against the CI `postgres:15` service — reused by Phases 1–2; (4) a deploy **runbook** in `documentation/operations/` with **symmetric ordering (G5): forward = migrate-then-deploy; rollback = deploy-revert-then-compensating-drop**, `schema_version` verification (assess **#190** startup check as the building block, G8), and a Neon-branch dry-run step.
- **Additive migration:** none.
- **Validation:** harness fails on a deliberately-broken fixture migration and on a seeded-dup backfill that loses a row; passes on `main`.
- **Rollback:** remove CI jobs + runbook (zero prod impact).

### Phase 1: Atomic daily-cap admission (M)

- **Goal:** the cap is an atomic admission gate that **cannot be exceeded under concurrency** and needs **no claim-then-abort/restore**. Supersedes #509.
- **Additive migration (per F2-a):** `daily_post_counts(chat_settings_id, post_day, count, …)` with **`post_day` computed in the tenant timezone** to match `day_start(tz)` (G1), **registered in the `init_db()` import graph** so `create_all` builds it in the test DB (G9). Composite `(chat_settings_id, posted_at, status)` index on `posting_history` for the rebuild path.
- **Path-by-path rollout:** one Repository-layer **admission helper** atomically reserves a slot (`UPDATE … SET count=count+1 WHERE count < cap RETURNING`, **single-writer**), releases on send-failure with a **floor and reservation identity** (`… SET count=count-1 WHERE count>0 AND <reservation matches>`, so a double-reclaim cannot under-count, G10). Cut all **six** paths' `can_post_today` call-sites to it in order. **Cutover safety (R1/F7):** a feature flag makes un-migrated call-sites **reject-safe** (fail closed, never over-post) until the path is cut. **Counter authority (R7):** the counter is **authoritative (not rebuilt-from-history) until Phase 3** — in the 1→3 window the non-atomic paths can write counter-without-history, so the COUNT-rebuild is declared invalid until Phase 3 makes completion atomic; consistency is held by reservation-identity release, not by rebuild. **Verify (Q4):** the `_auto_approve` retrofit composes with its non-raising dict-return error handling without committing partial state.
- **Validation (test DB only — never trigger real posting, `CLAUDE.md`):** the **multi-connection harness** drives N concurrent reservations at `cap−1` and asserts **posts ≤ cap**, **zero stranded `processing` rows**, and the autopost cap-hit no longer orphans (#509 scenario); extend `tests/src/services/test_daily_cap.py`.
- **Rollback:** compensating migration drops the table/index; revert the helper to `count_posts_today`.

### Phase 2: Claim-lease + durable pre-send marker; sweepers neutered (M/L) — *closes `:79`*

- **Goal:** deterministic lease-expiry reclamation (no wall-clock GC deleting live rows) **and** a pre-send marker that closes the send-then-crash double-post window.
- **Additive migration:** `ALTER TABLE posting_queue ADD COLUMN IF NOT EXISTS claimed_by TEXT, ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ, ADD COLUMN IF NOT EXISTS dispatch_marker_at TIMESTAMPTZ` — the marker. **No `updated_at`** (G7, audit garnish). **No status change** (R4). All nullable → safe on a live table. **tz (R5):** declare ORM cols `DateTime(timezone=True)`; standardize writers on `datetime.now(timezone.utc)`; the existing tz-naive `created_at`/`now()` sites that interact with the lease are migrated to tz-aware to avoid naive/aware comparison errors that would reclaim a live row.
- **Path-by-path rollout:** the shared claim sets `status='processing'`, `claimed_by`, `lease_expires_at = now(tz) + TTL` (F3). A durable **pre-send marker** is committed in its own txn immediately before the send (per the pinned ordering). One **`reclaim()`** repo method runs **once per tick** (not per chat), tenant-aware: `WHERE lease_expires_at < now()` → marker-absent rows requeue (`status='pending'`) + release the cap reservation; marker-present rows are surfaced for repair, **not** re-sent/released. **Sweepers are NOT deleted yet (R8):** `delete_stale_pending`/`requeue_stale_processing` are neutered to no-ops behind `WHERE lease_expires_at IS NULL` and removed only after the TTL is validated in prod over several cycles; **`discard_abandoned_processing` (24h) is kept as the no-reclaim backstop** until then (G2/Q3). **Cleanup scope (G3):** this phase names its #111-scale cleanup surface (tests referencing the sweepers, log lines, docs) as an explicit deliverable to avoid the #112/#113 vestigial-debris follow-ups.
- **Validation:** reproduce the `:79` scenario (tz-aware) → **never deleted while lease held**; a stuck row reclaimed **at lease expiry**; **#95 invariant (Q5):** a `telegram_message_id IS NULL` row under reclaim yields a fresh send, **never a duplicate**; **critical-gap case:** *send succeeds → worker dies pre-completion → row is NOT re-sent and cap is NOT released.* Update `test_scheduler_queue_reliability.py`, `test_queue_repository.py`.
- **Rollback:** compensating migration drops the columns; revert the PR (sweepers restored from git history, not resurrected as a parallel path).

### Phase 3: Idempotent completion in one transaction (M/L)

- **Goal:** every path completes atomically; a duplicate completion is a no-op. Removes the history-lookup recovery path.
- **Additive migration (F6, B1, B2):** a **PARTIAL unique index** on `posting_history(queue_item_id) WHERE status='posted'` — one terminal *success* per item; failed/skipped/rejected rows are unconstrained, so **re-approval of a failed post still records its success** (B1). **No backfill (F8 lean):** scope it additionally `WHERE created_at >= '<migration_date>'` so only future writes are constrained — **no mutation of the append-only ledger** (B2). Built with **`CREATE UNIQUE INDEX CONCURRENTLY`** outside a transaction, after any prerequisite commits, with the lock window documented (G4).
- **Path-by-path rollout:** retrofit the **3 non-atomic paths** (`_auto_approve`, autopost, scheduler auto-reapproval) onto `_shared_session()` so history + counter + queue-resolution + cap-confirm are one txn, guarded by the idempotency key (`ON CONFLICT DO NOTHING`). Completion confirms/clears the Phase-2 pre-send marker. This is where **F1** (delete-in-same-txn) lands. **Counter rebuild-from-history becomes valid here** (closes R7). **Delete** `get_by_queue_item_id`-as-recovery.
- **Validation:** fire completion twice → **exactly one** `posted` history row, **no** double counter increment, **no** double cap consumption; crash mid-completion → no partial state; `_auto_approve` covered explicitly.
- **Rollback:** drop the unique index (data-safe). If F8-b (backfill) was chosen, the dedup step has its own compensating record.

### Phase 4: DB-off-loop foundation — *parallel track, scope per F5*

- **4a — DB offload (L, parallel, no deps):** move synchronous DB off the event loop on the hot paths (`asyncio.to_thread`/executor or async engine). Independent of 1–3 for correctness; also the standing hang fix.
- **4b — enable `concurrent_updates` (S, depends on Phase 3) (R2):** enabling PTB concurrency lets two callbacks complete the same item, which is only safe **after** Phase 3's idempotency guard exists. **Sequenced after Phase 3, not parallel.**
- **Additive migration:** none. **Validation:** `py-spy` shows no loop-blocking DB; button-hang repro gone; concurrency test (multi-connection harness) shows no double-complete. **Rollback:** revert; disable `concurrent_updates`. **Owner/trigger (F5):** named so the parallel track doesn't stall.

## Decision Forks

Cycle-1 left forks **strongly leaning but NOT locked** — B1–B3 reshape F6 + the completion protocol and R4 touches F1. **Ratifier: Chris, to lock after Cycle 2.**

### F1: terminal `posted` status vs write-history-in-same-txn-as-delete — *lean (b)* (5/5)
- **(a)** terminal `posted` status — requires named-constraint surgery + ORM lockstep + table growth. **(b)** keep delete-on-success, write history+delete in **one** `_shared_session()` txn; idempotency from F6.
- **Lean (b)** — no constraint surgery; atomicity from `_shared_session`, idempotency from F6. **Folds R4:** claim keeps `processing`, reclaim → `pending`, no new status. Realized in Phase 3.
- **Ratifier:** Chris. **Status:** leaning(b). **Evidence:** critique §Q1; recon §2/§3; Cycle-1 (5/5).

### F2: cap counter-row reservation vs conditional insert — *lean (a)* (5/5)
- **(a)** `daily_post_counts` reserved via atomic `UPDATE … WHERE count < cap`. **(b)** conditional insert — can only *detect* over-cap **after** the irreversible send. **Lean (a)** with stated conditions: **tz-aware `post_day` (G1), single-writer, release floor + reservation identity (G10), counter authoritative-until-Phase-3 (R7), framed as admission-control state (not an audit fork).**
- **Ratifier:** Chris (new table). **Status:** leaning(a). **Evidence:** critique §Q2; Cycle-1 (5/5) — "(b) only detects post-send."

### F3: lease TTL + reclamation cadence — *lean (b), default 15 min* (4/4)
- **(b)** config TTL above worst-case send (IG ~180s), reclaim once per tick. **Default lean 15 min** (navi); **the 10-vs-15 numeric pick is open** (rajan notes 10 min is the historical `:79` culprit). Framing: **the TTL value is not the risk — the reclaim-release semantics are** (the pre-send marker, not the number, closes the double-post window).
- **Ratifier:** Chris / eng-lead. **Status:** leaning(b); numeric pick open. **Evidence:** Cycle-1.

### F4: rollout style — staged path-by-path **+ required cutover flag** (4/4)
- **(a)** big-bang per phase. **(b)** staged path-by-path with a hard delete-old-path gate. **Lean (b) — but plain ordering is insufficient for the R1 cap race; a reject-safe cutover feature-flag is REQUIRED** (or accept (a)). The flag is part of the lean, not optional. See **F7**.
- **Ratifier:** Chris. **Status:** leaning(b)+flag. **Evidence:** Cycle-1 (R1) — "F4-b + ordering alone does not mitigate."

### F5: scope — ship 1–3 now, Phase 4 parallel — *lean (a)* (5/5)
- **Lean (a):** don't couple the still-open `:79` lost-post fix to L-sized Phase 4; minimum high-value slice = **Phases 0+1+2**. **Name an owner/trigger for the Phase-4 track** (Cycle-1 ask). Note 4b sequences after Phase 3 (R2).
- **Ratifier:** Chris. **Status:** leaning(a). **Evidence:** Cycle-1 (5/5).

### F6: idempotency key — *lean (a) PARTIAL index* (5/5)
- **(a)** PARTIAL unique index on `posting_history(queue_item_id) **WHERE status='posted'**` (B1 — full index silently breaks failed-post re-approval). **(b)** composite key. **Lean (a)**, scoped no-backfill via **F8**, built `CONCURRENTLY` (G4).
- **Ratifier:** Chris. **Status:** leaning(a-partial). **Evidence:** Cycle-1 (5/5, B1).

### F7 (new — sharpened ratifier call): F4 cutover mitigation
- **Context:** during staged path-by-path cutover, the new counter and the old `history.COUNT` read `count<cap` from **different stores** → an over-cap-by-1 race (R1). Ordering narrows but does not close it.
- **Options:** **(a)** a **feature flag** that makes un-migrated `can_post_today()` call-sites **reject-safe** (fail closed) until cut — preserves staged safety, adds a time-boxed flag deleted at phase close. **(b)** **F4-a big-bang per phase** — cut all six call-sites in one PR, no dual-store window, no flag, larger blast radius.
- **Lean:** **(a)** the reject-safe flag — keeps incremental rollout while making the cap race impossible; the flag is deleted when the phase's old path is, so no permanent shim.
- **Ratifier:** **Chris** (prod-rollout risk posture). **Status:** open. **Evidence:** Cycle-1 R1; principles `consolidate-dont-fork`.

### F8 (new — sharpened ratifier call): Phase-3 dedup / backfill approach
- **Context:** the idempotency index can't be added while duplicate `posted` rows exist (near-certain after 4 months of bugs); deleting them mutates the **append-only** `posting_history` ledger (B2). Q1 asks which of two duplicate `posted` rows is authoritative.
- **Options:** **(a) No-backfill** — scope the partial index `WHERE created_at >= '<migration_date>'`; enforce idempotency only on future writes; **no ledger mutation**, append-compatible (mason: "the real descope lever"). **(b) Backfill** — define explicit dedup criteria (e.g. keep earliest `posted` per `queue_item_id`), get **Chris's sign-off** for the ledger mutation, then add the unconditional partial index.
- **Lean:** **(a) No-backfill** — preserves the append-only invariant, ships as a guarded fast-follow, and the pre-migration duplicates are a closed historical set the new primitives prevent recurring.
- **Ratifier:** **Chris** (audit-ledger mutation authority). **Status:** open. **Evidence:** Cycle-1 B2/Q1; `CLAUDE.md` (history never mutated in prod).

## Companion Plans

- **Critique (rationale/evidence):** `crog-eng-team/shared/planning/active/storydump-queue-concurrency-critique.md`.
- **Knowledge:** `shared/knowledge/storydump/hang-perf-regression-2026-06-12.md`; `…/queue-item-not-found-regression-2026-06-12.md`.
- **Superseded in-flight PRs:** **#509** (restore-only cap fix) and **#483** (hourly auto-prune, the sixth sweeper; author **Chris**) — coordinate now; one should yield (R9).
- **Historical anchors cited:** #190 (`schema_version` startup check), #111/#112/#113 (cleanup-scale precedent), #95 + the `:79` incident (lost-post / reclaim-release lineage).
- No conflicting active plan in `documentation/planning/` or `shared/planning/active/`.

## Dependencies

| Dependency | Blocks | Risk |
|------------|--------|------|
| Phase 0 (safety rails) | 1, 2, 3 | High if skipped |
| Phase 1 (cap reservation) | 2 (lease couples to reservation) | Med |
| Phases 1 + 2 | 3 (completion confirms reservation + lease + marker) | Med |
| **Phase 3** | **4b (`concurrent_updates`)** — concurrency unsafe without the idempotency guard (R2) | **High** |
| Phase 4a (offload) | none (parallel) | Low for correctness; High for the hang symptom |
| #509 + #483 disposition | Phase 1 / Phase 2 start | Low — coordinate with Chris |

## Risks

| Risk | Impact | Mitigation |
|------|--------|-----------|
| Migrations + **data migrations** untested/manual | High | Phase 0 DDL + **data** harness + byte-faithful diff (R3); additive-first; Neon-branch dry-run |
| Send-then-crash double-post | High | Durable pre-send marker; reclaim never re-sends a marked row; out-of-scope auto-recovery surfaced for repair |
| Cutover cap race (dual-store window) | Med-High | F7 reject-safe flag, or F4-a big-bang |
| Counter wrong in the 1→3 window | Med | Counter authoritative-until-Phase-3 (R7); reservation-identity release |
| tz naive/aware comparison reclaims a live row | Med | Standardize tz-aware lease + writers (R5); tz-aware repro test |
| Deleting sweepers before TTL proven | Med | Neuter behind `lease_expires_at IS NULL`; keep 24h backstop; delete after prod validation (R8) |
| Idempotency index over pre-existing dupes | Med | No-backfill route (F8-a); else guarded dedup + Chris sign-off |
| No downgrade tooling | Med | Compensating `NNN_revert_*.sql` written with each forward migration; symmetric apply/rollback ordering (G5) |
| #111-scale vestigial debris | Low-Med | Phase-2 cleanup surface named up front (G3) |

## Validation Strategy

- **Per phase:** reproduce the *specific* race, prove it closed, assert the retired machinery is gone. All tests run against the Postgres test DB / CI `postgres:15`; **never** trigger real posting (`CLAUDE.md`) — no `process-queue`, no `python -m src.main`, no mutating SQL on `posting_history`.
- **New explicit harness deliverables (Phase 0):** a **migration + data-migration** harness (the only thing that makes "the migration is correct" testable — the suite otherwise builds schema from the ORM, not the SQL), and a **multi-connection** harness (the savepoint fixture is one session and cannot express claim/cap contention) (R3, R6).
- **Acceptance (objective):** (1) concurrent claims post **≤ cap**, zero stranded rows; (2) the `:79` repro yields zero "Queue item not found" with a live lease; (3) **send-succeeds-then-crash → not re-sent, cap not released**; (4) double-completion → exactly one `posted` row; (5) `py-spy` shows no loop-blocking DB (Phase 4); (6) `#95` invariant holds under reclaim.
- **Prod signal:** the historical log signatures (`Queue item not found: <id>`, `Requeuing stale processing item <id>`, pool-timeout waits) drop to zero post-rollout.

## Complexity and Sequencing

| Phase | Size | Depends on | Parallel with |
|-------|------|-----------|---------------|
| 0 — Safety rails (prereq) | M | — | everything |
| 1 — Atomic cap admission | M | 0 | 4a |
| 2 — Claim-lease + pre-send marker (`:79`) | M/L | 0, 1 | 4a |
| 3 — Idempotent completion | M/L | 1, 2 | 4a |
| 4a — DB offload | L | — | 1, 2, 3 |
| 4b — `concurrent_updates` | S | **3** | — |

Critical path: **0 → 1 → 2 → 3 → 4b**. **4a** runs in parallel. Profile: **M×1, M×1, M/L×2, L×1, S×1**. Each phase is net-negative LOC.

## Adversarial Review Findings

1. **Scope (must ratify):** Phase 0 (now M, with data + concurrency harnesses) is not in ari's 1–4. Ratifier must accept it, fold it into Phase 1, or accept the manual-gate risk.
2. **Path-naming:** doc lives in the repo's established `documentation/planning/` (dispatch said `documentation/plans/`) per codebase convention — trivial to move.
3. **The two genuine ratifier calls** are now **F7** (F4 cutover mitigation) and **F8** (dedup/backfill approach) — both routed to Chris with leans.
4. **Irreducible window is acknowledged, not hidden:** the marker→send gap can lose a post to manual repair; this is stated as out-of-scope rather than implying the class is *fully* closed. `/ironclad` should pressure-test whether "surfaced for manual repair" is operationally acceptable.
5. **F3 numeric pick (10 vs 15 min)** is left open for Chris; the plan asserts the value is not the risk-bearing decision.

---
*Forged `/first-principles` → `/forge`; revised after `/ironclad` Cycle 1. Forks F1–F6 leaning, F7–F8 open; all to Chris. Ready for Cycle-2 delta review.*
