# Operational numbers

Both prior packages shipped **zero** operational values (SE:246-248 enumerates nine required per-queue settings and assigns none; SE:34 concedes deployment numbers "not yet derived"). This file closes that gap and is the **only home** for these figures — other files cite, never restate. Every value is initial, revised only under the rule at the end. Implementers wire each number through a config seam (env or settings row per C7), never a literal, so revision is config-only.

## Envelope arithmetic (the inputs everything derives from)

Declared inputs (each a config seam or a 0.4-verified platform fact):

| Input | Value | Status |
|---|---|---|
| Provisioned workspaces (FC-0) | 5,000 | ruled envelope |
| **Bounding posting accounts** | **7,500 at full cap** | declared input, decoupled from workspace count (FC-1: workspaces hold multiple accounts — pass-2 fix of the one-account-per-workspace conflation, review A §3.34); 1.5×workspaces is an initial planning ratio, revise via S.1 |
| Meta publish cap | 25 / rolling 24 h / real account | platform fact, 0.4-verified |
| Meta call rate | 200 / user / hr | platform fact, 0.4-verified |
| Worker replicas | 3 | initial |
| Ingress replicas | 2 | initial |
| Effective mean publish-pipeline duration | ~30 s | modeling assumption between p50 ~20 s and p95 ~90 s (poll-dominated tail); S.1 measures the real mean |
| Max accepted media file size | 100 MB | initial config seam |

Derived:

- **Publish ceiling:** 7,500 × 25 / 86,400 ≈ **2.2 publishes/s fleet average** (~130/min; absolute upper bound with every bounding account at cap; realistic steady state is a fraction).
- **Interactive rate:** ~0.2–0.6/s fleet average; peak ×20 ≈ **4–12/s** (posting-window edges). Peak *job* rates sit in the low tens per second — two orders of magnitude inside Postgres territory, which is C3's arithmetic basis.
- **Concurrent publish pipelines:** 2.2/s × 30 s ≈ **65 steady**; the bulk global pool of 150 below is **headroom sizing (~×2.3), not a requirement derivation** — the requirement is that the pool is bounded and configurable (H5); the size is an initial seam value S.1 revises (pass-2 reframing, review A §4.2).

## The nine per-queue settings (SE:246-248, now with values)

| # | Setting | Interactive lane | Bulk lane | Derivation |
|---|---|---|---|---|
| 1 | Per-process concurrency (pool slots/replica) | 10 | 50 | interactive: peak 12/s × 2 s ≈ 24 concurrent ÷ 3 replicas = 8, +~25% margin → 10. bulk: 65 steady × ~2.3 headroom ≈ 150 ÷ 3 replicas = 50 |
| 2 | Global concurrency (deployment-wide) | 30 | 150 | replicas × per-process; headroom framing above |
| 3 | Per-workspace concurrency | 5 | 3 | bulk: 1 publish + 1 sync + 1 misc — a workspace can never monopolize a lane. interactive: capped at half one replica's interactive pool |
| 4 | Per-provider-key concurrency | 1 per Telegram binding (send ordering) | 1 per `provider_account_ref` (publish — also schema key 4) | H1/G1; ordering correctness, not throughput |
| 5 | Prefetch / claim batch | 1 | 1 | claim-one; prefetching buys nothing at these rates and widens crash blast radius |
| 6 | Lease duration | 60 s | 120 s | ≥ longest single checkpointed step (container poll segments ≤ 60 s) + margin; steps checkpoint, so a lease never covers a whole pipeline |
| 7 | Heartbeat interval | 20 s | 30 s | 3–4 beats per lease; the beat task is independent of pipeline awaits (`02` §5), so provider waits never starve it; missing 2 beats ⇒ presumed dead well inside the lease |
| 8 | Attempt / deadline budget | 3 attempts, deadline +10 min | 5 attempts, backoff 1/5/15/60 min, deadline = slot end (or +6 h for non-slot jobs) | R8: retryable classes only; ambiguous never re-attempts (reconciler owns it) |
| 9 | Reserved interactive capacity | 10 of each replica's 60 slots interactive-only (10:50, ≈17%) | — | H2: bulk can never occupy interactive capacity |

**Tasks vs connections (the SE:228 inequality, made explicit — review A §3.35):** a pool slot is an asyncio task, not a connection; connections are held only inside transaction blocks, and the L.0 discipline (transaction-per-checkpoint, never across a provider call — `02` §5) keeps DB-active time ≲5% of pipeline wall time. Expected concurrent DB-active tasks ≈ 180 slots × 5% ≈ 9 fleet-wide; per-replica connection pools of 10 bound the spike case. The invariant to re-verify at S.3: Σ(replica × pool) = 3×10 + 2×10 = **50** ≥ peak DB-active tasks, and < the Neon plan ceiling (pgbouncer in front; clock runs inside an elected worker — no separate pool).

## Supporting cadences and budgets

| Concern | Initial value | Derivation / note |
|---|---|---|
| Scheduler tick | 15 s; ≤ 500 inserts/tick | O(due) scan over `ix_ig_accounts_due`; slot key 1 makes double-insert impossible |
| Outbox sender poll | 2 s | replaces Redis wake-up (C3); invisible in pg at ≪1 msg/s |
| Telegram pacing | 20 msgs/min/group, 30/s global | Telegram published budgets, carried into durable pacing |
| Jobs-ready poll (workers) | 1 s interactive / 2 s bulk | the pg-polling cost the annex trigger watches |
| Admission (pg fixed-window, S.2) | 30 commands/min/workspace; **no global ceiling** | per-workspace abuse guard, fail-closed. The pass-1 50/s global cap is struck (review A §4.1): no app-wide platform budget exists to protect; global protection = pool bounds + backpressure visibility |
| DB connections | 50 total (3×10 workers + 2×10 ingress) | inequality above; re-verify both sides at S.3 |
| Media transfers per worker | 4 | EP:78's initial cap, kept |
| Temp storage | 3 × 4 × 100 MB = 1.2 GB headroom per env | SE:234-235 inequality with declared inputs |
| Sync baseline | every 6 h jittered; pre-slot sync at T−15 min if source stale > 30 min; first-ingest chunks of 200 files | H4 demand-driven shape |
| Cloudinary (FC-3) | signed-URL TTL 15 min; transit hard TTL 24 h; reap sweep every 15 min | FC-3.2/3.6 |
| Meta usage pre-check | inline-only at publish admission; in-process cache TTL 5 min keyed on `provider_account_ref` | `02` §8 — **no background refresh exists**; worst case ≤ 1 query per publish attempt (≤ ~130/min at ceiling), vs the struck eager reading's 1,500/min at 7,500 accounts (review B§6) |
| Parked-intent alarm | `publishing_ambiguous` or `review_required` > 15 min pages; customer notification per `06` §5 after 24 h | observability floor (`01`) |
| Reconciler cadence + budget | sweep every 60 s, LIMIT 50; per-intent evidence budget = poll to container expiry (~24 h), then one stories check, then `review_required` | bounded per H5/RF-R1; contract in `02` §6 |
| Quarantine backoff ladder | 1 m / 5 m / 30 m / 2 h / 24 h (cap); strike decay 24 h; re-alert dedup 1 h | `02` §2 semantics |
| Backfill batch / comparator window | 5,000 rows / 14 days | six-stage machine inputs (`04` §Ground rules) |
| Approval TTL default (`approval_ttl_minutes` NULL) | 1,440 min (24 h) | workspace seam; reaper clock (`02` §4) |
| Offboarding | grace window 30 days; publish-drain timeout 15 min; revocation retry 3 × 1 h backoff | `06` §1 workflow |
| Invitations / OTP / sessions / OAuth state | invite expiry 7 d · OTP 10 min, 5 attempts, issue rate 3/h/email · session 30 d sliding · state token 15 min | `07` §§1–2 |
| Re-auth campaign cadence | 1 prompt / account / week; "no media available" notice dedup 24 h | `06` §5, G.1 |
| Card TTL (W.6 drop condition) | 30 days | `04` W.6 mechanical drop rule |

## Retention (swept by `retention_sweep`, S.4; per-class, terminal/age-qualified)

| Data | Keep | Then |
|---|---|---|
| `audit_events` | 400 d | COPY-export to archive, delete (export-or-abort, `07` §4) |
| `jobs` terminal (succeeded/cancelled) | 30 d | delete |
| `jobs` terminal (failed/review_required) | 90 d | delete |
| `provider_operations` resolved | 90 d | delete |
| `channel_outbox` sent/superseded | 30 d | delete |
| `channel_outbox` failed/ambiguous | 90 d | delete |
| `daily_post_counts` | 400 d | delete |
| `post_intents` terminal | **kept forever** | they ARE the posting history (product data, not bookkeeping) |
| Contract-stage table dumps | 90 d | archive expiry (`04` ground rules) |

## Backup / DR (review A §5.15)

| Concern | Value |
|---|---|
| PITR floor | Neon PITR window ≥ 7 days — verified at 0.2's gate and re-checked when the plan changes |
| RPO | Neon continuous WAL (~minutes) — no additional mechanism |
| RTO target | 1 h (restore branch + repoint DATABASE_URL + smoke suite) |
| Restore drill | quarterly, runbook'd: PITR branch → runner parity check → smoke suite; first drill is a W-phase gate |
| Tenant-level recovery | PITR branch + selective per-workspace copy (runbook; exercised once in the first drill) — RLS keys make per-tenant extraction a WHERE clause, not archaeology |

## Redis annex (gated, C3/RF-R4)

#722's Increments 11–12 (Streams wake-ups, Redis admission) are **not scheduled**. They activate only on measured breach, reviewed as an ADR citing harness data:

- interactive-lane oldest-runnable-age p95 > 5 s sustained over 7 days, or
- DB load attributable to queue/outbox polling > 30% of the Neon budget, or
- fleet publish demand within ×2 of the pg-core's measured ceiling.

Until a trigger fires, adding Redis is scope creep by definition.

## Revision rule

Two instruments, cleanly split:

1. **Platform inputs** (Meta caps, scope names, endpoint shapes, container status vocabulary) revise via 0.4's primary-doc verification — corrections land here as input-row edits with the doc citation.
2. **Everything derived** revises only via S.1's load harness (200-click / 250-due-account scenarios, versioned): a revision PR cites the measurement, changes the config seam (never code), and re-runs the harness to show the intended effect.

A number copied anywhere without citing this file, or hardcoded past its config seam, is a review-blocking defect — this sentence is the single normative statement of that rule.
