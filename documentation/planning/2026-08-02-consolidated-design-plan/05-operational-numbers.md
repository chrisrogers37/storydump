# Operational numbers

Both prior packages shipped **zero** operational values (SE:248-250 enumerates nine required per-queue settings and assigns none; SE:36 concedes deployment numbers "not yet derived" — package anchors re-pinned at the pass-4 anchor: the supersession banners shifted every pre-banner line by +2). This file closes that gap and is the **only home** for these figures — other files cite, never restate. Every value is initial, revised only under the rule at the end. Implementers wire each number through a config seam (env or settings row per C7), never a literal, so revision is config-only.

## Envelope arithmetic (the inputs everything derives from)

Declared inputs (each a config seam or a 0.4-verified platform fact):

| Input | Value | Status |
|---|---|---|
| Provisioned workspaces (FC-0) | 5,000 | ruled envelope |
| **Bounding posting accounts** | **7,500 at full cap** | declared input, decoupled from workspace count (FC-1: workspaces hold multiple accounts — pass-2 fix of the one-account-per-workspace conflation, review A §3.34); 1.5×workspaces is an initial planning ratio, revise via S.1 |
| Meta publish cap | **100** / rolling 24 h / real account | platform fact — **corrected at the pass-4 anchor** from the stale 25 and pinned against Meta's content-publishing doc 2026-08-03 ("Instagram accounts are limited to 100 API-published posts within a 24-hour moving period"). Meta has raised this over time (25→50→100), which is why `main` fetches the authoritative per-account value live (`GET /{ig-user}/content_publishing_limit`; `settings.py` fallback 100) — the live value, not this constant, is the runtime arbiter. **0.4 re-verified 2026-08-13** against the same page, wording unchanged, and added two things: **carousels count as a single post**, and Meta's own endpoint reference currently describes `quota_total` as "currently 50" while the guide says 100 — the vendor contradicts itself today, which is the argument for the live read rather than a case to adjudicate. Neither page may be cited as *the* number. The in-tree guide drift (#734) was a different defect than filed and is fixed here; see `0.4-meta-primary-doc-verification.md` |
| Meta call rate | **200 × daily-active users / hr, app-wide pool** | **0.4-corrected 2026-08-13** against Meta's rate-limiting doc: "Calls within one hour = 200 * Number of Users", where "the Number of Users is based on the number of unique daily active users an app has". The carried "200/user/hr" had the figure right and the **shape** wrong — this is an app-level budget sized by DAU, not a per-user ceiling. Both directions matter: one user may exceed 200 while the app stays in budget, and a quiet user base yields a *smaller* pool than "200 × registered accounts" implies, so reading it per-user over-estimates headroom exactly when activity is low |
| Worker replicas | 3 | initial |
| Ingress replicas | 2 | initial |
| Effective mean publish-pipeline duration | ~30 s | modeling assumption between p50 ~20 s and p95 ~90 s (poll-dominated tail); S.1 measures the real mean |
| Max accepted media file size | 100 MB | initial config seam |
| Invitation email volume | launch: single digits/day; model = workspace-onboarding rate × invites/workspace (+ bounce notices). Provider free tier: 100/day / 3,000/month, **paused at quota** | pass-5 input (R4's Resend finding); the burst-risk rationale lives in `07` §1; the `email_global` budget below defers under our own ceiling |

Derived:

- **Publish ceiling:** 7,500 × 100 / 86,400 ≈ **8.7 publishes/s fleet average at absolute cap** (~520/min; the upper bound with every bounding account publishing at the full corrected cap — a bound, not a forecast; realistic steady state is a small fraction, and per-account **product cadence** (`posts_per_day` ≤ 50) binds long before Meta's 100 does). The pass-3 figure (2.2/s, ~130/min) was computed at the stale cap of 25.
- **Interactive rate:** ~0.2–0.6/s fleet average; peak ×20 ≈ **4–12/s** (posting-window edges). Peak *job* rates even at the corrected absolute publish ceiling sit in the low tens per second — still well over an order of magnitude inside Postgres territory, which is C3's arithmetic basis (the margin narrowed with the cap correction; the conclusion did not move).
- **Concurrent publish pipelines:** at the cadence-realistic steady state the pass-3 figure stands (≈ 2.2/s × 30 s ≈ **65 steady**, now read as a product-cadence working figure rather than the Meta bound); the absolute-cap bound is 8.7/s × 30 s ≈ **260**. The bulk global pool of 150 below is a **bounded seam, not a requirement derivation** — sized ~×2.3 over the steady working figure; at loads approaching the absolute bound the pool saturates first and defers (slip-a-slot, H5 — the designed behavior, not a failure); S.1 revises the size on measurement (pass-2 reframing, review A §4.2; pass-4 cap correction applied).

## The nine per-queue settings (SE:248-250, now with values)

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

**Tasks vs connections (the SE:230-234 inequality, made explicit — review A §3.35):** a pool slot is an asyncio task, not a connection; connections are held only inside transaction blocks, and the L.0 discipline (transaction-per-checkpoint, never across a provider call — `02` §5) keeps DB-active time ≲5% of pipeline wall time. Expected concurrent DB-active tasks ≈ 180 slots × 5% ≈ 9 fleet-wide; per-replica connection pools of 10 bound the spike case. **The inequality counts `pool_size + max_overflow`, and `max_overflow` is pinned to 0 at L.0** (pass 5 — R4's finding: the anchored repo ships `DB_MAX_OVERFLOW=20`, which silently makes the true ceiling (10+20)×5 = 150, not 50; the pool-slots-are-tasks model sizes `pool_size` for the spike case, so overflow un-bounds the invariant for nothing). The invariant: Σ(replica × (pool + overflow)) = 3×(10+0) + 2×(10+0) = **50** ≥ peak DB-active tasks, and < the actual Neon plan ceiling — **first verified against the real plan at M.3's smoke, re-verified at S.3 scale** (pgbouncer in front; clock runs inside an elected worker — no separate pool).

## Supporting cadences and budgets

| Concern | Initial value | Derivation / note |
|---|---|---|
| Scheduler tick | 15 s; ≤ 500 inserts/tick | O(due) scan over `ix_ig_accounts_due`; slot key 1 makes double-insert impossible. **The bound is `fn_clock_tick`'s `p_max` — the TOTAL insert budget across all four job classes, enforced by construction per the `02` §7 door bound rule (priority order and starvation semantics: the door's own comment is the one home — pass 6, R5)** |
| Outbox sender poll | 2 s | replaces Redis wake-up (C3); invisible in pg at ≪1 msg/s |
| Telegram pacing | 20 msgs/min/group, 30/s global | Telegram published budgets, carried into durable `rate_counters` rows (`02` §6 — scopes `tg_chat`/`tg_global`) |
| Jobs-ready poll (workers) | 1 s interactive / 2 s bulk | the pg-polling cost the annex trigger watches |
| Admission (pg fixed-window, S.2) | 30 commands/min/workspace; **no global ceiling** | per-workspace abuse guard, fail-closed; durable home: `rate_counters` scope `ws_admission` (`02` §6). The pass-1 50/s global cap is struck (review A §4.1): no app-wide platform budget exists to protect; global protection = pool bounds + backpressure visibility |
| DB connections | 50 total (3×10 workers + 2×10 ingress) | inequality above; re-verify both sides at S.3 |
| Media transfers per worker | 4 | EP:80's initial cap, kept |
| Temp storage | 3 × 4 × 100 MB = 1.2 GB headroom per env | SE:236-237 inequality with declared inputs |
| Sync baseline | every 6 h jittered; pre-slot sync at T−15 min if source stale > 30 min; first-ingest chunks of 200 files | H4 demand-driven shape |
| Credential refresh cadence (`next_refresh_at`) | every 7 d from issue, jittered — decoupled from expiry proximity | the scheduled refresh doubles as the credential **liveness probe** (`02` §2 D31), so this number bounds dead-token detection latency between publish attempts. The legacy semantics — refresh only within 7 d of expiry (`REFRESH_BUFFER_HOURS = 168`) — left a token dead at day 0 of a 60-day window unprobed for ~53 days (the 2026-05 incident class); decoupling from expiry proximity is the point. **0.4 verified the refresh-eligibility constraints 2026-08-13 and this cadence clears them.** Meta extends a long-lived token by 60 days only while it "is at least 24 hours old", is still valid, and the user still grants `instagram_business_basic`; "tokens that have not been refreshed in 60 days will expire and can no longer be refreshed". The **min-age floor is 24 h**, so a 7-day jittered cadence sits well outside it — decoupling from expiry proximity introduces no eligibility conflict, which was the open question here. Note the third condition: a scope revocation presents as a *refresh failure*, which is consistent with D31 treating a definitive auth rejection as a liveness signal rather than a transient error |
| Cloudinary (FC-3) | **transit TTL = the asset's lifetime**: reap-on-success (minutes, FC-3.5) + hard TTL 24 h + reap sweep every 15 min (FC-3.6). Delivery URLs are **signed, non-expiring** (FC-3.2 as amended) | per D38 (the analysis's home). **Revisit trigger: sustained monthly Cloudinary credit consumption > 225 credits** (D38's condition; this row is the number's home) |
| Meta usage pre-check | inline-only at publish admission, **behind a default-off flag (the S.5 canary decides)**; in-process cache TTL 5 min keyed on `provider_account_ref` | `02` §8 — **no background refresh exists**; worst case ≤ 1 query per publish attempt (≤ ~520/min at the corrected absolute ceiling; far less at cadence-realistic load), vs the struck eager reading's 1,500/min at 7,500 accounts (review B§6) |
| Parked-intent alarm | `publishing_ambiguous` or `review_required` > 15 min pages; customer notification per `06` §5 after 24 h | observability floor (`01`) |
| Reconciler cadence + budget | sweep every 60 s, LIMIT 50 — **the sweep's TOTAL across both reasons, enforced by construction per the `02` §7 door bound rule (priority and fill: the door's own comment is the one home — pass 6, R5)**; **per-intent poll ladder 60 s → 5 m → 30 m → 2 h (exponential, capped at container expiry ~24 h) — ≈ 15–20 status calls per ambiguous intent worst-case, vs ~1,440 at the pass-2 flat 60 s poll**; ladder exhausted ⇒ the `02` §6 exhaustion tail (final stories check, park `review_required`) | bounded per H5/RF-R1; mode-parameterized contract in `02` §6 |
| Quarantine backoff ladder | 1 m / 5 m / 30 m / 2 h / 24 h (cap); strike decay 24 h; re-alert dedup 1 h | `02` §2 semantics |
| Transform batch (M.1) | 5,000 rows | offline-transform batching (`04` §Ground rules; the 14-day comparator window died with shadow-read — FC-7) |
| Approval TTL default (`approval_ttl_minutes` NULL) | 1,440 min (24 h) | workspace seam; reaper clock (`02` §4) |
| Reaper cadence + budget (`fn_reaper_sweep p_lim`) | sweep every 60 s, 500 — the sweep's TOTAL, enforced by construction per the `02` §7 door bound rule (pass 7 — R6: this row did not exist and the legs drew p_lim each); priority and fill: the door's own comment is the one home | same bound family as the tick's per-tick 500 |
| Offboarding | grace window 30 days; publish-drain timeout 15 min; revocation retry 3 × 1 h backoff | `06` §1 workflow |
| Invitations / sessions / OAuth state | invite expiry 7 d · session 30 d sliding · state token 15 min (every purpose — connect/reconnect/signin/link) | `07` §§1–2 |
| Pre-auth admission (unauthenticated surfaces) | 30/min per IP (the Google sign-in endpoints included) | `07` §1 via `rate_counters` scope `preauth_ip` (`02` §6, incl. the client-IP source rule) — deliberately distinct from the per-workspace S.2 admission, which requires tenant context |
| Email delivery (`send_email`, `07` §1) | 3 attempts, backoff 1/5/15 min; **provider-wide budget 90/day** (`rate_counters` scope `email_global`, key `''` — headroom under the free tier's 100/day hard pause; over budget ⇒ the job defers on its retry schedule) | provider + bounce semantics: `07` §1 (ack status: `03` items); the volume model is the envelope input above; X.3's gate delivers a real invitation email end-to-end |
| Retention sweep cadence | daily, batches of 5,000 per class, walking the `ix_*_retire` indexes | `02` §5 pattern; H5-bounded |
| Reauth-prompt cadence | 1 prompt / account / week; "no media available" notice dedup 24 h | `06` §5 (runtime credential death — `reauth_prompt` jobs; the G-phase campaign died with FC-7) |
| `legacy_queue_item_id` drop condition | 30 days after M.3 + zero non-terminal carriers | `02` §3 mechanical drop rule |

## Retention (swept by `retention_sweep`, S.4; per-class, terminal/age-qualified)

| Data | Keep | Then |
|---|---|---|
| `audit_events` | 400 d | COPY-export to archive, delete (export-or-abort, `07` §4) |
| `jobs` terminal (succeeded/cancelled) | 30 d | delete |
| `jobs` terminal (failed/review_required) | 90 d | delete |
| `provider_operations` succeeded/failed | 90 d | delete (ambiguous rows are excluded — the reconciler terminalizes every one, `02` §6, so the class drains) |
| `channel_outbox` sent/superseded | 30 d | delete |
| `channel_outbox` failed/ambiguous | 90 d | delete |
| `daily_post_counts` | 400 d | delete |
| `session_tokens` expired/revoked | 30 d | delete (via `fn_auth_plane_sweep`, on this sweep's schedule) |
| `command_dedup` | 7 d | delete (via `fn_auth_plane_sweep`; Telegram's replay window is hours) |
| `rate_counters` | 7 d | delete (windows are minutes–hours; the class keeps days) |
| Expiry-class rows (`post_locks`, `workspace_invitations`, `oauth_states`) | on expiry | swept by `reap_expired` (`02` §5 remit), not this sweep |
| `post_intents` terminal | **kept forever** | they ARE the posting history (product data, not bookkeeping) |
| M.3 snapshot tables (`archive` schema) | 90 d | DROP TABLE — the `archive_snapshots` retention class (names carry the `04` date suffix, so one name-test mechanism ages both archive families) |
| Audit export batch tables (`archive` schema) | 400 d after export | DROP TABLE — the `archive_audit` retention class |

## Backup / DR (review A §5.15)

| Concern | Value |
|---|---|
| Archive location | **the in-database `archive` schema (same Neon Postgres)** — audit export batches + contract-stage snapshots as tables; access rules `07` §4, rationale `03` D30. Revisit only if archive size threatens the Neon plan |
| PITR floor | Neon PITR window ≥ 7 days — verified at 0.2's gate and re-checked when the plan changes |
| RPO | Neon continuous WAL (~minutes) — no additional mechanism |
| RTO target | 1 h (restore branch + repoint DATABASE_URL + smoke suite) |
| Restore drill | quarterly, runbook'd: PITR branch → runner parity check → smoke suite; **the M.2 rehearsal IS the first drill** (pass 5 — it runs exactly this sequence on a production PITR branch). The `archive` schema is covered by construction (it IS the database); S.4's gate additionally proves one audit export and one snapshot restore |
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
