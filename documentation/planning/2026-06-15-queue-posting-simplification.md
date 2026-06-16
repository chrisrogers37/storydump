---
title: "Queue/Posting Simplification — Eliminate the Concurrency Race Class"
type: plan
status: draft
owner: astrid
tags: [storydump, queue, posting, concurrency, race-condition, data-model, scheduler, daily-cap, lease, idempotency, migration, ironclad-cycle-2]
created: 2026-06-15
updated: 2026-06-15
---

# Queue/Posting Simplification — Eliminate the Concurrency Race Class

> **Plan PR for `/ironclad`. Revised after Cycles 1–2 (2026-06-15).** Production data-model + code change; no code ships from this PR. **Cycle 2: NOT CONVERGED — 1 major (M1) remained; all cycle-1 items verified resolved; 2 of 3 lenses approve (navi/adversarial, alex/feasibility), 1 request-changes (branden/first-principles).** This revision folds **M1 + its 9-item minor cluster** ahead of fork-lock — see the **Cycle-2 Review Response** map. Cycle 3 is a focused M1 re-verify, then Chris locks the 8 forks. Rationale/evidence: companion critique `crog-eng-team/shared/planning/active/storydump-queue-concurrency-critique.md`.

## Goal

Eliminate the recurring queue/posting **race class** (`Queue item not found`, duplicate send, orphaned `processing`/`pending`, cap-hit stranding, spinning buttons) by replacing the reconciliation machinery that *manufactures* it with atomic primitives: an **atomic daily-cap admission gate**, an **atomic claim with a lease**, a **durable pre-send marker**, and **idempotent completion in one transaction**. A parallel track removes the single-event-loop / sync-DB substrate.

**Mission alignment (`PROJECT_MISSION.md`):** north star *"zero-friction content automation… posts are never lost."* The `:79` "Queue item not found" miss **is a silently lost post**; the duplicate-send class is an over-post. Schema changes "require approval" — that approval is the `/ironclad` + human ratification (Chris) this PR exists to obtain. **Non-goals:** no change to the multi-account/Instance tenancy model; no new integrations; no UX redesign.

## Cycle-1 Review Response (delta map)

| Item | Resolution | Where |
|------|-----------|-------|
| **B1** full unique index breaks failed-post re-approval | F6 → **PARTIAL** unique index `WHERE status='posted'` | F6, Phase 3 |
| **B2** dedup backfill mutates append-only history | **No-backfill route** (partial index `WHERE created_at >= '<migration_date>'`), else explicit criteria + Chris sign-off; split index+backfill into a guarded fast-follow | **F8**, Phase 3 |
| **Critical** send-then-crash double-post window | **Durable pre-send marker** + pinned 4-step ordering; reclaim-after-confirmed-send **out of scope** (manual repair); new Phase-2 validation case | Architecture, Phase 2 |
| **R1** cutover cap race | F4 lean now **requires** a reject-safe cutover feature-flag | **F7**, F4, Phase 1 |
| **R2** dep table wrong | Phase 4 split; **4b `concurrent_updates` depends on Phase 3** | Dependencies, Complexity |
| **R3** Phase 0 DDL-only | Extend to a **data-migration test harness** + byte-faithful index/constraint diff | Phase 0 |
| **R4** status-vocab contradiction | Pin: claim keeps **`processing`** + lease cols; reclaim → **`pending`**; `ready/claimed` conceptual only | Architecture, Phase 2, F1 |
| **R5** tz-naive vs TIMESTAMPTZ | **Code-only** — see Cycle-2 (R5 refined: new cols tz-aware from birth, no `ALTER COLUMN`) | Phase 2 |
| **R6** concurrency untestable | **Multi-connection test harness** as an explicit deliverable | Phase 0/1 |
| **R7** counter rebuildability gap | Counter declared **authoritative (not rebuilt) until Phase 3** | F2, Phase 1 |
| **R8** deleting sweepers early | Ship Phase 2 with sweepers **no-op behind `WHERE lease_expires_at IS NULL`**; delete only after TTL validated | Phase 2 |
| **R9** PR #483 sixth sweeper | Added to **Superseded** beside #509; coordinate with author **Chris** | Current State, Companion |
| **G1–G10, Q1–Q5** | tz-aware counter, release floor/identity, discard backstop, cleanup scope, `CONCURRENTLY`, rollback ordering, 6 paths canonical, drop `updated_at`, #190, model registration, dedup→F8, ordering pinned, retrofit verification, #95 validation | F2/Phase 2/Phase 3/Phase 0 |

## Cycle-2 Review Response (delta map for Cycle 3)

Cycle 2: **2 lenses approve** (navi/adversarial, alex/feasibility); all cycle-1 items verified resolved (12 blocker/major items → 1). **1 major (M1) + a 9-item minor cluster** fold here before fork-lock. Cycle 3 = focused M1 re-verify (first-principles only).

| Item | Resolution | Where |
|------|-----------|-------|
| **M1** marker invariant only pinned for `reclaim()`; the **alive-worker** send-failure path reopens the double-post (an ambiguous-but-delivered send is indistinguishable from a clean failure) | Generalize the invariant to **ALL** re-send paths (reclaim, in-worker retry, sync-failure handler, B1 re-approval): **no path re-issues a marker-present row.** Classify every send **{success, definitive-pre-delivery-reject, ambiguous}**; only a definitive pre-delivery reject clears the marker + releases the reservation (safe retry); **ambiguous = treat as sent** (leave marker, no release, manual repair, never auto-retry) | Architecture, Phase 2, Phase 3 |
| reclaim predicate | `WHERE status='processing' AND lease_expires_at < now()` | Phase 2 |
| synchronous send-failure | releases the reservation + terminal `failed` (distinct from the crash/ambiguous branch — two marker-present sub-cases, two handlers) | Phase 2 |
| 24h discard exemption | exempt `dispatch_marker_at IS NOT NULL` (never hard-delete a repair / possibly-delivered row) | Phase 2 |
| in-worker retry loop | restructure `_send_to_telegram` so a failure **after** a successful `send_photo` does not re-issue | Phase 2 |
| `migration_date` + `ON CONFLICT` | pin convention (migration-file write date, ≤ go-live, `created_at` non-null server-defaulted); `ON CONFLICT` inference **mirrors the partial-index predicate** | Phase 3 |
| manual-repair protocol | Phase-0 runbook deliverable (+ ghost-reservation tolerance) | Phase 0 |
| repair surface auto-clear | clears on a subsequent idempotent completion | Phase 3 |
| **B1 marker-aware** | a marker-present `failed` row is a **repair item, not a re-approval/retry candidate** | Phase 3 |
| R5 **code-only** | new lease cols tz-aware from birth; reclaim compares only those; **no `ALTER COLUMN … TYPE`** on existing cols | Phase 2 |
| F3 rationale | the marker makes the TTL low-stakes — **1–2 min fine, 10-vs-15 moot** | F3 |

## Current State

Verified against `main @ e36e4c7` (file:symbol anchors are durable; line numbers drift). Full evidence in the critique.

- **No `posted` state on the queue.** `posting_queue.status ∈ {pending, processing, failed}` (named `CheckConstraint("…","check_status")`). A successful post **deletes** the row and writes `posting_history`. "This post happened" is spread across **6 representations in 4 stores**, read by 7 decision points.
- **Cap = live COUNT** over `posting_history` (status='posted' ∧ success ∧ tz-aware `posted_at >= day_start(tz)`); no composite `(chat_settings_id, posted_at, status)` index; the cap check is **outside** the completion write.
- **Claim = bare status flip, no lease** (`claim_for_processing`, `FOR UPDATE SKIP LOCKED` → `processing`); per-id mutual exclusion is an `asyncio.Lock` one layer up (**batch-approve skips it**).
- **Wall-clock GC sweepers** (`delete_stale_pending`, `requeue_stale_processing`, predicate `status ∧ telegram_message_id IS NULL ∧ created_at ≤ now−10min`, **10 min hardcoded**, **no tenant filter**, **once per active chat per tick**), plus `discard_abandoned_processing` (24h) and an hourly prune loop. **The `:79` race is open at HEAD.**
- **Completion non-atomic on 3 of 5 paths** — only manual Posted/Reject use `_shared_session()`; `_auto_approve`, autopost, and the failure path commit independently.
- **Six posting paths are canonical** (undercounting to 5 by omitting batch-approve is how prior fixes regressed): `process_slot`, `force_send_next` (`/next`), `_auto_approve`, manual Posted (`_do_complete_queue_action`), autopost (`_do_autopost`), batch-approve (`handle_batch_approve`).
- **In-flight band-aids this plan supersedes:** **PR #509** (`fix/autopost-cap-orphans-queue-row`, restore-only) **and PR #483** (`feat/posting-queue-cleanup-loop`, the *sixth sweeper* — an hourly `posting_queue` auto-prune loop; author **Chris**). Both OPEN; one root, one should yield.
- **Substrate:** PTB poller + 60s scheduler + media-sync on one asyncio loop; sync psycopg2 inline, no offload; `concurrent_updates` off → the hang regression.

### Operational ground truth (constrains every phase)

| Fact | Consequence |
|------|-------------|
| Migrations = hand-written numbered SQL (`scripts/migrations/NNN_*.sql`), idempotent DDL, `schema_version` insert | "Additive migration" = a new `NNN_*.sql`; not Alembic |
| **No downgrade path** | Rollback = compensating forward `NNN_revert_*.sql` |
| **Not auto-applied on deploy** (`psql` against Neon is a manual gate) | Per-phase apply-before-deploy runbook; additive-first mandatory; symmetric rollback ordering |
| **Not exercised by tests/CI** (test DB from `Base.metadata.create_all()`, not the SQL) | Phase 0 migration/data harness; `#190` `schema_version` startup check is a building block |
| `check_status`/`check_history_status` are **named** constraints | Avoid new statuses → no drop/re-add surgery |
| Repos commit internally; atomic multi-write only via `_shared_session()` | Phase 3 retrofits the 3 non-atomic paths |
| Test fixture = one session in a savepoint | Cannot express claim/cap contention → **multi-connection harness** |
| `posting_history` is append-only / never mutated in prod | No-backfill idempotency route preferred |
| CI `changelog-check` exempts docs-only PRs | This plan PR is green; implementation PRs must update `CHANGELOG.md` |

## Architecture

**One claim/complete contract, six paths, no parallel paths, no shims** (`consolidate-dont-fork`). Primitives live in the **Repository** layer; services orchestrate (`CLAUDE.md` strict layering). **Status vocabulary is pinned (R4): no new status value.** `ready`/`claimed` are *conceptual* labels; on disk a claim keeps **`status='processing'`** and sets lease columns, and reclaim sets **`status='pending'`**.

**Pinned completion protocol — the 4-step ordering:**

```
1. cap-reserve        atomic admission (Phase 1, F2) — reserve a slot WHERE count < cap
2. pre-send marker    durable, committed in its OWN txn IMMEDIATELY BEFORE the irreversible send (Phase 2)
3. send               Telegram/Instagram (irreversible; no server-side dedup)
4. completion         ONE txn (Phase 3): history + counters + queue-resolution + idempotency key

reclaim() (Phase 2, per tick, tenant-aware, WHERE status='processing' AND lease_expires_at < now()):
   • marker ABSENT  → never sent → safe to requeue (status='pending') + release the reservation
   • marker PRESENT → attempted  → never re-sent, never released; route to manual repair
```

**The marker invariant is generalized to ALL re-send paths (M1 — load-bearing for "no double-post"): no path re-issues a marker-present row** — binding `reclaim()`, the in-worker `_send_to_telegram` retry loop, the synchronous send-failure handler, and B1 re-approval. **Every send resolves to one of three outcomes:**

- **success** → proceed to completion (step 4).
- **definitive pre-delivery reject** (provably never delivered — e.g. a 4xx validation error before the send leaves the worker) → **clear the marker, release the cap reservation, safe to retry.**
- **ambiguous** (response timeout / 5xx / dropped connection — delivered-vs-not is indistinguishable) → **treat as sent: leave the marker, do NOT release, surface for manual repair, never auto-retry.**

This closes the **alive-worker** re-send, not just the crash: a Telegram/IG send that delivers but returns ambiguously looks identical to a clean failure to the alive worker, so classifying it as `ambiguous` (non-retryable) is what stops the re-send + cap under-count M1 identified. The **irreducible window** is the sub-second marker-commit→send-issue gap: a crash there leaves a marker with no post — surfaced for manual repair, never silently lost or double-posted; the repair surface **auto-clears on a subsequent idempotent completion** (a slow send that exceeded TTL but did complete resolves itself). **Auto-recovery of a sent-but-uncompleted post is explicitly out of scope** — the #95 "reset only causes re-sends" lesson honored across *every* path, not relocated into one.

Net effect is **subtraction**: Phases 1–3 retire `delete_stale_pending`, `requeue_stale_processing`, the per-chat cadence, the hourly prune (#483), the 10-min hardcode, claim-then-abort/restore (#509), and the history-lookup recovery path.

## Phases

Additive-migration-first; net-deletes code; `S/M/L` sizing only. Within a phase, paths cut **lowest-risk first**: `scheduler → /next → auto-approve → batch-approve → Posted → autopost`.

### Phase 0: Migration, data & concurrency safety rails — *Prerequisite* (M)

> Not in ari's original 1–4. **Ratifier must confirm scope.** Phases 1–3 ship prod schema **and data** on a process untested in CI, manually applied, with no rollback tooling — the dominant risk.

- **Deliverables:** (1) CI job applying `scripts/migrations/*.sql` in order to a throwaway Postgres, asserting the result matches `Base.metadata.create_all()` **and a byte-faithful comparison of partial-index predicates + named constraints** (R3); (2) a **data-migration test harness** (R3): *seed duplicate `posted` rows → run the dedup/no-backfill path → assert; seed history → run counter-init → assert*; (3) a **multi-connection concurrency harness** (R6) — N real psycopg2 connections against the CI `postgres:15` service — reused by Phases 1–2; (4) a deploy **runbook** in `documentation/operations/` with **symmetric ordering: forward = migrate-then-deploy; rollback = deploy-revert-then-compensating-drop** (G5), `schema_version` verification (assess **#190** startup check as the building block, G8), a Neon-branch dry-run step; (5) a **manual-repair protocol** runbook — how an operator resolves a marker-present repair row (verify whether the post actually delivered, then complete or re-queue), including **ghost-reservation tolerance** (a cap slot held by a never-delivered ambiguous send) via periodic reconciliation.
- **Additive migration:** none.
- **Validation:** harness fails on a deliberately-broken fixture migration and on a seeded-dup backfill that loses a row; passes on `main`.
- **Rollback:** remove CI jobs + runbook (zero prod impact).

### Phase 1: Atomic daily-cap admission (M)

- **Goal:** the cap is an atomic admission gate that **cannot be exceeded under concurrency** and needs **no claim-then-abort/restore**. Supersedes #509.
- **Additive migration (F2-a):** `daily_post_counts(chat_settings_id, post_day, count, …)` with **`post_day` computed in the tenant timezone** to match `day_start(tz)` (G1), **registered in the `init_db()` import graph** so `create_all` builds it in the test DB (G9). Composite `(chat_settings_id, posted_at, status)` index on `posting_history` for the rebuild path.
- **Path-by-path rollout:** one Repository-layer **admission helper** atomically reserves a slot (`UPDATE … SET count=count+1 WHERE count < cap RETURNING`, **single-writer**), releases on send-failure with a **floor and reservation identity** (`… SET count=count-1 WHERE count>0 AND <reservation matches>`, G10). Cut all **six** paths' `can_post_today` call-sites to it in order. **Cutover safety (R1/F7):** a feature flag makes un-migrated call-sites **reject-safe** (fail closed) until cut. **Counter authority (R7):** authoritative (not rebuilt-from-history) until Phase 3 — in the 1→3 window non-atomic paths can write counter-without-history, so the COUNT-rebuild is invalid until Phase 3; consistency is held by reservation-identity release, not by rebuild. **Verify (Q4):** the `_auto_approve` retrofit composes with its non-raising dict-return error handling without committing partial state.
- **Validation (test DB only — never trigger real posting, `CLAUDE.md`):** the **multi-connection harness** drives N concurrent reservations at `cap−1` → **posts ≤ cap**, **zero stranded `processing` rows**, autopost cap-hit no longer orphans; extend `tests/src/services/test_daily_cap.py`.
- **Rollback:** compensating migration drops the table/index; revert the helper to `count_posts_today`.

### Phase 2: Claim-lease + durable pre-send marker; sweepers neutered (M/L) — *closes `:79`*

- **Goal:** deterministic lease-expiry reclamation (no wall-clock GC deleting live rows) **and** a pre-send marker + send-outcome classification that close the send-then-crash **and** alive-worker double-post windows.
- **Additive migration:** `ALTER TABLE posting_queue ADD COLUMN IF NOT EXISTS claimed_by TEXT, ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ, ADD COLUMN IF NOT EXISTS dispatch_marker_at TIMESTAMPTZ` — the marker. **No `updated_at`** (audit garnish). **No status change.** All nullable → safe on a live table. **tz (R5, code-only):** the **new** lease/marker columns are tz-aware **from birth** (`DateTime(timezone=True)`, written with `datetime.now(timezone.utc)`), and `reclaim()` compares **only** those new columns. **No `ALTER COLUMN … TYPE TIMESTAMPTZ`** on existing tz-naive columns (that is a non-additive rewrite); existing columns stay untouched.
- **Path-by-path rollout:** the shared claim sets `status='processing'`, `claimed_by`, `lease_expires_at = now(tz) + TTL` (F3). A durable **pre-send marker** (`dispatch_marker_at`) is committed in its own txn immediately before the send. **`reclaim()`** runs **once per tick** (not per chat), tenant-aware, predicate **`WHERE status='processing' AND lease_expires_at < now()`** → marker-absent rows requeue (`status='pending'`) + release the reservation; marker-present rows are surfaced for repair, **not** re-sent/released. **Send-outcome handling (M1) — two marker-present sub-cases, two handlers:** a **synchronous, definitive pre-delivery reject** clears the marker, releases the reservation, and the row is safe to retry (or goes terminal `failed`), **distinct** from the **ambiguous/crash** branch which leaves the marker, does not release, and routes to manual repair. The in-worker `_send_to_telegram` retry loop is **restructured so a failure after a successful `send_photo` does not re-issue.** The 24h `discard_abandoned_processing` is amended to **exempt `dispatch_marker_at IS NOT NULL` rows** (never hard-delete a repair / possibly-delivered row → no silent lost-post). **Sweepers NOT deleted yet (R8):** neutered to no-ops behind `WHERE lease_expires_at IS NULL`, removed only after the TTL is validated in prod over several cycles; `discard_abandoned_processing` kept as the no-reclaim backstop until then (G2/Q3). **Cleanup scope (G3):** the #111-scale cleanup surface (sweeper tests, log lines, docs) is named as an explicit deliverable.
- **Validation:** reproduce the `:79` scenario (tz-aware) → **never deleted while lease held**; a stuck row reclaimed **at lease expiry**; **#95 invariant (Q5):** a `telegram_message_id IS NULL` row under reclaim yields a fresh send, **never a duplicate**; **critical-gap case:** *send succeeds → worker dies pre-completion → row is NOT re-sent and cap is NOT released*; **M1 case:** *send returns ambiguous (timeout/5xx) → row is NOT re-sent by reclaim, the retry loop, or B1 re-approval; reservation NOT released.* Update `test_scheduler_queue_reliability.py`, `test_queue_repository.py`.
- **Rollback:** compensating migration drops the columns; revert the PR (sweepers restored from git history).

### Phase 3: Idempotent completion in one transaction (M/L)

- **Goal:** every path completes atomically; a duplicate completion is a no-op. Removes the history-lookup recovery path.
- **Additive migration (F6, B1, B2):** a **PARTIAL unique index** on `posting_history(queue_item_id) WHERE status='posted'` — one terminal *success* per item; failed/skipped/rejected rows unconstrained, so **re-approval of a failed post still records its success** (B1). **No backfill (F8 lean):** scope it additionally `WHERE created_at >= '<migration_date>'` — only future writes constrained, **no ledger mutation** (B2). **`migration_date` convention (pinned):** the migration-file write date, which must be **≤ protocol go-live**, with `created_at` verified **non-null and server-defaulted**; the completion-path **`ON CONFLICT` inference mirrors the partial-index predicate exactly**. Built with **`CREATE UNIQUE INDEX CONCURRENTLY`** outside a transaction, after prerequisite commits, lock window documented (G4).
- **Path-by-path rollout:** retrofit the **3 non-atomic paths** (`_auto_approve`, autopost, scheduler auto-reapproval) onto `_shared_session()` so history + counter + queue-resolution + cap-confirm are one txn, guarded by the idempotency key (`ON CONFLICT DO NOTHING`). Completion confirms/clears the Phase-2 pre-send marker, and the **repair surface auto-clears** when a delayed-but-real completion lands. **B1 is marker-aware (M1):** a **marker-present `failed` row is a repair item, not a re-approval/retry candidate** — re-approval must skip it, so an ambiguous-but-delivered send recorded as `failed` cannot feed the failed-post retry pathway into a double-post. **Counter rebuild-from-history becomes valid here** (closes R7). **Delete** `get_by_queue_item_id`-as-recovery.
- **Validation:** fire completion twice → **exactly one** `posted` history row, **no** double counter increment, **no** double cap consumption; crash mid-completion → no partial state; `_auto_approve` covered explicitly.
- **Rollback:** drop the unique index (data-safe). If F8-b (backfill) was chosen, the dedup step has its own compensating record.

### Phase 4: DB-off-loop foundation — *parallel track, scope per F5*

- **4a — DB offload (L, parallel, no deps):** move synchronous DB off the event loop on the hot paths (`asyncio.to_thread`/executor or async engine). Independent of 1–3 for correctness; also the standing hang fix.
- **4b — enable `concurrent_updates` (S, depends on Phase 3):** enabling PTB concurrency lets two callbacks complete the same item, safe **only after** Phase 3's idempotency guard. **Sequenced after Phase 3, not parallel.**
- **Additive migration:** none. **Validation:** `py-spy` shows no loop-blocking DB; button-hang repro gone; concurrency test (multi-connection harness) shows no double-complete. **Rollback:** revert; disable `concurrent_updates`. **Owner/trigger (F5):** named so the parallel track doesn't stall.

## Decision Forks

Cycle 2: all 8 forks "sound, ready to lock **after M1 folds**." **Ratifier: Chris, after the focused Cycle-3 M1 re-verify.**

### F1: terminal `posted` status vs write-history-in-same-txn-as-delete — *lean (b)* (5/5)
- **(a)** terminal `posted` status — named-constraint surgery + ORM lockstep + table growth. **(b)** keep delete-on-success, write history+delete in **one** `_shared_session()` txn; idempotency from F6.
- **Lean (b)** — no constraint surgery; atomicity from `_shared_session`, idempotency from F6. Folds R4 (claim keeps `processing`, reclaim → `pending`, no new status). Realized in Phase 3.
- **Ratifier:** Chris. **Status:** leaning(b). **Evidence:** Cycle-1/2 (5/5).

### F2: cap counter-row reservation vs conditional insert — *lean (a)* (5/5)
- **(a)** `daily_post_counts` reserved via atomic `UPDATE … WHERE count < cap`. **(b)** conditional insert — can only *detect* over-cap **after** the irreversible send. **Lean (a)** with: **tz-aware `post_day`, single-writer, release floor + reservation identity, counter authoritative-until-Phase-3, admission-control framing.**
- **Ratifier:** Chris (new table). **Status:** leaning(a). **Evidence:** Cycle-1/2 (5/5).

### F3: lease TTL + reclamation cadence — *lean (b); TTL low-stakes* (4/4)
- **(b)** config TTL, reclaim once per tick. **The pre-send marker makes the TTL value low-stakes** — TTL governs only the sub-second `claim → marker-write` window, not the send — so **1–2 min is fine and the 10-vs-15-min debate is moot.** The reclaim-release **semantics** (the generalized marker invariant + send-outcome classification, M1), not the TTL number, bear the correctness.
- **Ratifier:** Chris / eng-lead. **Status:** leaning(b); TTL low-stakes. **Evidence:** Cycle-1/2 (O1).

### F4: rollout style — staged path-by-path **+ required cutover flag** (4/4)
- **(a)** big-bang per phase. **(b)** staged path-by-path with a hard delete-old-path gate. **Lean (b) — but plain ordering is insufficient for the R1 cap race; a reject-safe cutover feature-flag is REQUIRED** (or accept (a)). See **F7**.
- **Ratifier:** Chris. **Status:** leaning(b)+flag. **Evidence:** Cycle-1 (R1).

### F5: scope — ship 1–3 now, Phase 4 parallel — *lean (a)* (5/5)
- **Lean (a):** don't couple the still-open `:79` lost-post fix to L-sized Phase 4; minimum high-value slice = **Phases 0+1+2**. **Name an owner/trigger for the Phase-4 track.** 4b sequences after Phase 3 (R2).
- **Ratifier:** Chris. **Status:** leaning(a). **Evidence:** Cycle-1/2 (5/5).

### F6: idempotency key — *lean (a) PARTIAL index* (5/5)
- **(a)** PARTIAL unique index on `posting_history(queue_item_id) WHERE status='posted'` (B1). **(b)** composite key. **Lean (a)**, scoped no-backfill via **F8**, built `CONCURRENTLY` (G4), and **marker-aware re-approval** so failed→repair rows don't re-enter the retry pathway (M1).
- **Ratifier:** Chris. **Status:** leaning(a-partial). **Evidence:** Cycle-1/2 (5/5, B1).

### F7: F4 cutover mitigation — *lean (a) reject-safe flag* (sound, lock after M1)
- **Context:** during staged cutover the new counter and the old `history.COUNT` read `count<cap` from **different stores** → over-cap-by-1 (R1). **(a)** a feature flag making un-migrated `can_post_today()` call-sites **reject-safe / fail-closed** until cut (defer-not-drop; deleted at phase close). **(b)** F4-a big-bang per phase — no dual-store window, no flag, larger blast radius.
- **Lean (a)** — keeps incremental rollout while making the cap race impossible; no permanent shim.
- **Ratifier:** **Chris.** **Status:** open (lock after M1). **Evidence:** Cycle-1 R1; Cycle-2.

### F8: Phase-3 dedup / backfill approach — *lean (a) no-backfill* (sound, lock after M1)
- **Context:** the index can't be added while duplicate `posted` rows exist; deleting them mutates the **append-only** ledger (B2); Q1 asks which duplicate is authoritative. **(a) No-backfill** — partial index `WHERE created_at >= '<migration_date>'`; enforce only future writes; no ledger mutation (mason: "the real descope lever"). **(b) Backfill** — explicit dedup criteria (keep earliest `posted` per `queue_item_id`) + **Chris's sign-off**, then unconditional partial index.
- **Lean (a) No-backfill** — preserves the append-only invariant; ships as a guarded fast-follow; pre-migration dups are a closed historical set.
- **Ratifier:** **Chris.** **Status:** open (lock after M1). **Evidence:** Cycle-1 B2/Q1; Cycle-2.

## Companion Plans

- **Critique (rationale/evidence):** `crog-eng-team/shared/planning/active/storydump-queue-concurrency-critique.md`.
- **Knowledge:** `shared/knowledge/storydump/hang-perf-regression-2026-06-12.md`; `…/queue-item-not-found-regression-2026-06-12.md`.
- **Superseded in-flight PRs:** **#509** (restore-only cap fix) and **#483** (hourly auto-prune, the sixth sweeper; author **Chris**) — coordinate; one should yield.
- **Historical anchors:** #190 (`schema_version` startup check), #111/#112/#113 (cleanup-scale precedent), #95 + the `:79` incident (lost-post / reclaim-release lineage).
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
| Migrations + data migrations untested/manual | High | Phase 0 DDL + data harness + byte-faithful diff; additive-first; Neon-branch dry-run |
| Send-then-crash **and alive-worker ambiguous** double-post | High | Pre-send marker + send-outcome classification; **no path** re-issues a marker-present row; ambiguous = treat-as-sent; manual repair (M1) |
| Cutover cap race (dual-store window) | Med-High | F7 reject-safe flag, or F4-a big-bang |
| Counter wrong in the 1→3 window | Med | Counter authoritative-until-Phase-3 (R7); reservation-identity release |
| tz naive/aware comparison reclaims a live row | Med | R5 code-only: new cols tz-aware from birth, reclaim compares only those, no `ALTER COLUMN` |
| Deleting sweepers before TTL proven | Med | Neuter behind `lease_expires_at IS NULL`; keep 24h backstop (marker-exempt); delete after prod validation (R8) |
| Idempotency index over pre-existing dupes | Med | No-backfill route (F8-a); else guarded dedup + Chris sign-off |
| Ghost reservation (ambiguous send never delivered) | Med | Manual-repair runbook + periodic reconciliation tolerance (Phase 0) |
| No downgrade tooling | Med | Compensating `NNN_revert_*.sql`; symmetric apply/rollback ordering |

## Validation Strategy

- **Per phase:** reproduce the *specific* race, prove it closed, assert the retired machinery is gone. All tests run against the Postgres test DB / CI `postgres:15`; **never** trigger real posting (`CLAUDE.md`) — no `process-queue`, no `python -m src.main`, no mutating SQL on `posting_history`.
- **New explicit harness deliverables (Phase 0):** a **migration + data-migration** harness (the only thing that makes "the migration is correct" testable), and a **multi-connection** harness (the savepoint fixture is one session and cannot express claim/cap contention).
- **Acceptance (objective):** (1) concurrent claims post **≤ cap**, zero stranded rows; (2) `:79` repro → zero "Queue item not found" with a live lease; (3) **send-succeeds-then-crash → not re-sent, cap not released**; (4) **ambiguous send → not re-sent by reclaim / retry-loop / re-approval, reservation not released** (M1); (5) double-completion → exactly one `posted` row; (6) `py-spy` shows no loop-blocking DB (Phase 4); (7) `#95` invariant holds under reclaim.
- **Prod signal:** the historical log signatures (`Queue item not found`, `Requeuing stale processing item`, pool-timeout waits) drop to zero post-rollout.

## Complexity and Sequencing

| Phase | Size | Depends on | Parallel with |
|-------|------|-----------|---------------|
| 0 — Safety rails (prereq) | M | — | everything |
| 1 — Atomic cap admission | M | 0 | 4a |
| 2 — Claim-lease + pre-send marker (`:79`) | M/L | 0, 1 | 4a |
| 3 — Idempotent completion | M/L | 1, 2 | 4a |
| 4a — DB offload | L | — | 1, 2, 3 |
| 4b — `concurrent_updates` | S | **3** | — |

Critical path: **0 → 1 → 2 → 3 → 4b**. **4a** runs in parallel. Profile: **M×2, M/L×2, L×1, S×1**. Each phase is net-negative LOC.

## Adversarial Review Findings

1. **Scope (must ratify):** Phase 0 (M, with data + concurrency harnesses) is not in ari's 1–4. Ratifier must accept it, fold it into Phase 1, or accept the manual-gate risk.
2. **Path-naming:** doc lives in the repo's established `documentation/planning/` (dispatch said `documentation/plans/`) per codebase convention — trivial to move.
3. **The two genuine ratifier calls** are F7 (cutover mitigation) and F8 (dedup/backfill) — routed to Chris with leans.
4. **The irreducible window is acknowledged, not hidden:** the marker→send gap (and an ambiguous-send ghost reservation) can produce a manual-repair item; stated out-of-scope for auto-recovery rather than implying the class is *fully* closed. Operationally bounded by the Phase-0 repair runbook + auto-clear-on-completion.
5. **F3 numeric pick (10 vs 15 min) is moot** post-M1 — the marker, not the TTL value, bears correctness; 1–2 min is fine.

---
*Forged `/first-principles` → `/forge`; revised after `/ironclad` Cycles 1–2. M1 + minor cluster folded; all 8 forks (F1–F8) ready to lock by Chris after the focused Cycle-3 M1 re-verify.*
