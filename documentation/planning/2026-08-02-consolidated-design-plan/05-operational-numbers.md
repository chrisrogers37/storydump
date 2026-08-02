# Operational numbers

Both prior packages shipped **zero** operational values (SE:246-248 enumerates nine required per-queue settings and assigns none; SE:34 concedes deployment numbers "not yet derived"). This file closes that gap and is the **only home** for these figures — other files cite, never restate. Every value is initial, revised only under the rule at the end. Implementers wire each number through a config seam (env or settings row per C7), never a literal, so revision is config-only.

## Envelope arithmetic (the inputs everything derives from)

Declared inputs (each a config seam or a 0.4-verified platform fact):

| Input | Value | Status |
|---|---|---|
| Provisioned workspaces (FC-0) | 5,000 | ruled envelope |
| **Bounding account count** | **5,000 posting accounts at full cap** | working figure: one cap-saturating account per provisioned workspace; FC-1 multi-account workspaces raise the true account count, but not the *bounding* arithmetic until measured — revise via S.1 |
| Meta publish cap | 25 / rolling 24 h / real account | platform fact, 0.4-verified |
| Meta call rate | 200 / user / hr | platform fact, 0.4-verified |
| Worker replicas | 3 | initial |
| Ingress replicas | 2 | initial |
| Effective mean publish-pipeline duration | ~30 s | modeling assumption between p50 ~20 s and p95 ~90 s (poll-dominated tail); S.1 measures the real mean |
| Max accepted media file size | 100 MB | initial config seam |

Derived:

- **Publish ceiling:** 5,000 × 25 / 86,400 ≈ **1.45 publishes/s fleet average** (absolute upper bound with every bounding account at cap; realistic steady state is a fraction).
- **Interactive rate:** ~0.2–0.6/s fleet average; peak ×20 ≈ **4–12/s** (posting-window edges). Peak *job* rates therefore sit in the low tens per second — still two orders of magnitude inside Postgres territory, which is C3's arithmetic basis.
- **Concurrent publish pipelines:** 1.45/s × 30 s ≈ **44 steady**; the bulk global cap of 150 below gives ≈ ×3.4 headroom.

## The nine per-queue settings (SE:246-248, now with values)

| # | Setting | Interactive lane | Bulk lane | Derivation |
|---|---|---|---|---|
| 1 | Per-process concurrency (pool slots/replica) | 10 | 50 | interactive: peak 12/s × 2 s ≈ 24 concurrent ÷ 3 replicas = 8, +~25% margin → 10. bulk: 44 steady × ~3.4 headroom ≈ 150 ÷ 3 replicas = 50 |
| 2 | Global concurrency (deployment-wide) | 30 | 150 | replicas × per-process (3 × 10, 3 × 50); interactive 30 covers peak 24 with margin |
| 3 | Per-workspace concurrency | 5 | 3 | bulk: 1 publish + 1 sync + 1 misc — a workspace can never monopolize a lane. interactive: single-workspace burst allowance, capped at half one replica's interactive pool so two bursting workspaces cannot saturate a replica |
| 4 | Per-provider-key concurrency | 1 per Telegram binding (send ordering) | 1 per `provider_account_ref` (publish — also enforced by schema key 4) | H1/G1; ordering correctness, not throughput |
| 5 | Prefetch / claim batch | 1 | 1 | SKIP LOCKED claim-one; prefetching buys nothing at these rates and widens crash blast radius |
| 6 | Lease duration | 60 s | 120 s | ≥ longest single checkpointed step (container poll segments ≤ 60 s) + margin; steps checkpoint, so a lease never covers a whole pipeline |
| 7 | Heartbeat interval | 20 s | 30 s | 3–4 beats per lease; a worker missing 2 beats is presumed dead well inside the lease |
| 8 | Attempt / deadline budget | 3 attempts, deadline +10 min | 5 attempts, backoff 1/5/15/60 min, deadline = slot end (or +6 h for non-slot jobs) | R8: retryable classes only; ambiguous never re-attempts (reconciler owns it) |
| 9 | Reserved interactive capacity | 10 of each replica's 60 slots are interactive-only (10:50 split, ≈17% of pool slots) | — | H2: bulk can never occupy interactive capacity |

## Supporting cadences and budgets

| Concern | Initial value | Derivation / note |
|---|---|---|
| Scheduler tick | 15 s; ≤ 500 job inserts/tick | O(due) scan; slot key 1 makes double-insert impossible |
| Outbox sender poll | 2 s | Replaces Redis wake-up (C3); at ≪1 msg/s average this is invisible in pg |
| Telegram pacing | 20 msgs/min/group, 30/s global | Telegram published budgets; carried from the AIORateLimiter work into durable pacing |
| Jobs-ready poll (workers) | 1 s interactive / 2 s bulk | The pg-polling cost the annex trigger watches |
| Admission (pg fixed-window, S.2) | 30 commands/min/workspace; 50/s global | Replaces SlowAPI `memory://`; fail-closed |
| DB connections | 3 workers × 10 + 2 ingress × 10 = **50** (clock runs inside an elected worker — no separate pool); pgbouncer in front | Inequality (SE:228): Σ(replica × pool) ≥ peak DB-active tasks and < the Neon plan's ceiling — re-verify both sides against the actual plan at S.3 |
| Media transfers per worker | 4 | EP:78's initial cap, kept |
| Temp storage | 3 workers × 4 transfers × 100 MB = **1.2 GB** headroom per environment | The SE:234-235 inequality with declared inputs |
| Sync baseline | every 6 h jittered; pre-slot sync at T−15 min if source stale > 30 min; first-ingest chunks of 200 files | H4 demand-driven shape (the slow jittered baseline it allows, not short-cadence polling) |
| Cloudinary (FC-3) | signed-URL TTL 15 min; transit hard TTL 24 h; reap sweep every 15 min | FC-3.2/3.6; blast radius stays minutes-to-hours of transit media |
| Meta usage pre-check cache | 5 min per account | Advisory only (`02` §8); error 9 remains the authority |
| Parked-intent alarm | `publishing_ambiguous` > 15 min pages | Observability floor (`01`) |
| Reconciler cadence | every 60 s, LIMITed sweep over the ambiguous set | Bounded per H5/RF-R1 |
| Backfill batch / comparator window | 5,000 rows / 14 days | Six-stage machine inputs (`04` §Ground rules) |

## Redis annex (gated, C3/RF-R4)

#722's Increments 11–12 (Streams wake-ups, Redis admission) are **not scheduled**. They activate only on measured breach, reviewed as an ADR citing harness data:

- interactive-lane oldest-runnable-age p95 > 5 s sustained over 7 days, or
- DB load attributable to queue/outbox polling > 30% of the Neon budget, or
- fleet publish demand within ×2 of the pg-core's measured ceiling.

Until a trigger fires, adding Redis is scope creep by definition.

## Revision rule

Two instruments, cleanly split:

1. **Platform inputs** (Meta caps, scope names, endpoint shapes) revise via 0.4's primary-doc verification — corrections land here as input-row edits with the doc citation.
2. **Everything derived** revises only via S.1's load harness (200-click / 250-due-tenant scenarios, versioned): a revision PR cites the measurement, changes the config seam (never code), and re-runs the harness to show the intended effect.

A number copied anywhere without citing this file, or hardcoded past its config seam, is a review-blocking defect — this sentence is the single normative statement of that rule.
