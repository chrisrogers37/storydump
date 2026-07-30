# Repository-Specific Architecture Evaluation

**Evaluation prompt:** [`fable-evaluation-prompt.md`](fable-evaluation-prompt.md)  
**Repository baseline:** `main` at `683f7cf`  
**Evaluated:** 2026-07-30  
**Status:** COMPLETED SELF-EVALUATION — independent approval still pending  
**Scope:** Design review only; no production or infrastructure mutation

## 1. Executive verdict

**Approve with required changes.**

The evolutionary PostgreSQL-authoritative/Redis-coordinated direction fits the
repository and preserves its best posting-safety work. The original proposal
needed stronger contracts for broker-loss recovery, Telegram callback
acknowledgement, admission ordering, cancellation, fair dispatch, and
transaction-local RLS context. Those are now explicit in
[`epic.md`](epic.md).

Implementation is not yet authorized or safe. Exact migrations, transition
constraints, runtime database roles, connection budgets, Redis operating policy,
and provider adapter contracts remain phase gates.

## 2. Scorecard

Scores evaluate the amended target design, not the current implementation.

| Dimension | Score | Reason |
|---|---:|---|
| Correctness | 4/5 | Clear authority, uniqueness, lease, and ambiguity boundaries; exact DDL/transition table remains |
| Durability | 4/5 | PostgreSQL owns accepted work and a ready-job scanner closes broker-loss gaps |
| Tenant isolation | 4/5 | Mandatory context plus RLS is strong; backfill inventory and role design remain |
| Fairness | 3/5 | Weighted tenant quantum and persisted ordering are specified but not algorithmically/provisionally proven |
| Capacity control | 4/5 | Hierarchical budgets and reserved lanes are sound; deployment-wide numbers are not yet derived |
| Security | 4/5 | Auth, replay, egress, credentials, audit, and fail policy are covered; Railway source-IP trust needs deployment proof |
| Operability | 4/5 | Required signals and terminal/review states are broad; alert thresholds/runbooks remain |
| Migration safety | 4/5 | Shadowing, cohorts, phase gates, and non-destructive rollback are explicit |
| Testability | 4/5 | Failure injection and migrated-DB tests are concrete; provider fakes and load environment must be built |

Current-system readiness for horizontal scaling is approximately 1/5: the safe
pieces are local, tenant enforcement is incomplete, the worker cannot be
replicated as-is, and durable jobs/outbox/shared limits do not exist.

## 3. Fundamentals and assumptions

| Claim or assumption | Classification | Assessment |
|---|---|---|
| An accepted command must survive process/Redis loss | Fundamental | Requires PostgreSQL commit before success |
| Third-party success can be ambiguous | Fundamental | Already handled conservatively for Meta and Telegram |
| At-least-once delivery is the realistic transport contract | Fundamental | Correct; domain effects need unique business keys |
| PostgreSQL is the authoritative work/effect store | Architectural assumption, well justified | Fits existing transactional safety and Neon |
| Redis is non-authoritative coordination | Architectural assumption, well justified | Safe only with ready-job recovery independent of outbox publication |
| Webhooks enable replicated Telegram ingress | Externally verified | Telegram supports concurrent webhook delivery; polling/webhook are mutually exclusive |
| Redis Streams provide weighted tenant fairness | Unsafe if stated alone | Consumer groups distribute work; relay/claim logic must implement fairness |
| `SET LOCAL` tenant context works with transaction pooling | Externally verified, integration test required | Correct transaction lifetime; must be issued inside every transaction |
| One active Meta flow per account prevents publish overlap | Plausible and required | Must be a PostgreSQL constraint/conditional claim, not only Redis lock |
| Zero duplicate publishes can be tested | Plausible with precise wording | Achieved by holding ambiguity and refusing unsafe replay, not provider exactly-once |
| Every provisioned tenant may be active simultaneously | Not an initial requirement | Envelope defines 1,000 provisioned, 250 active |
| Scaling worker replicas increases safe provider throughput | Unsafe if automatic | Global provider and DB budgets must remain fixed unless separately changed |

## 4. Repository evidence audit

| Design evidence claim | Verdict | Repository evidence | Consequence |
|---|---|---|---|
| Operation coordination is process-local | Verified | `src/services/core/telegram_operation_state.py`; `TelegramService.operation_state` | Another process cannot see lock, cancel, or in-flight state |
| A committed `processing` queue row is re-claimable | Verified | `QueueRepository.claim_for_processing()` admits `processing` | `SKIP LOCKED` protects only overlapping transactions, not a durable lease |
| Concurrency test proves one winner | Partially verified | `tests/src/repositories/test_queue_claim_concurrency.py` deliberately holds the winner lock | It proves concurrent lock contention only; its own comments acknowledge a later claimant can re-win |
| API rate limits are process-local | Verified | `src/api/rate_limit.py` uses `memory://` | Limits reset and multiply with replicas |
| API trusts any proxy host | Verified | `src/api/app.py` uses `ProxyHeadersMiddleware(trusted_hosts=["*"])` | Client identity is not a safe limiter key until ingress trust is corrected |
| Telegram concurrency is bounded at eight | Verified | `TelegramService._build_application()` and `src/config/settings.py` | Good local backpressure, not deployment-wide coordination |
| PTB pacing covers all senders | Contradicted as a system claim | Worker uses one rate-limited `ExtBot`; code comments explicitly exclude API/CLI one-shot bots | Shared token budget is still unenforced across processes |
| Scheduler visits tenants sequentially | Verified | `src/services/core/loops/scheduler_loop.py` iterates `active_chats` and awaits each `process_slot` | One slow tenant delays later tenants |
| Media sync visits tenants sequentially | Verified | `src/services/core/loops/media_sync_loop.py` awaits one `to_thread` sync per chat | No system-wide transfer pool or tenant fairness |
| Session isolation is absent | Stale if stated broadly | `BaseRepository` uses task-local `ContextVar` state and task boundaries detach sessions | Session sharing improved, but DB calls remain synchronous and transactions remain implicit/long-lived |
| Tenant filters are optional | Verified | `BaseRepository._apply_tenant_filter(..., None)` is a no-op | A missing argument still becomes an unscoped query |
| Tenant-owned columns remain nullable | Verified | `PostingQueue`, `PostingHistory`, `MediaItem`, locks, categories, and tokens include nullable tenant ownership | RLS/non-null migration needs an inventory and backfill gates |
| All July system-review tenant findings remain open | Stale | Changelog records fixes for API membership, account removal/switching, media writes/selection, and notifications | Use current baseline evidence, not the July 2 tracker status alone |
| Worker process owns unrelated loops | Verified | `src/main.py` starts polling, scheduler, cleanup, sync, health, and transaction hygiene | Replicating the process is unsafe and poorly isolated |
| Polling rollback preserves pending updates | Contradicted | `TelegramService.start_polling()` currently passes `drop_pending_updates=True` | This must be removed and tested before webhook cutover |
| There is no general job/outbox model | Verified | `PostingQueue` docstring calls it ephemeral; no job, attempt, provider-operation, or outbox models exist | Durable admission and worker extraction are foundational changes |
| Meta container is persisted before publish | Verified | `QueueRepository.mark_publishing()` and scheduler/autopost callbacks | Strong invariant to preserve in `provider_operations` |
| Posting history idempotency has a DB uniqueness backstop | Contradicted | `HistoryRepository.create_idempotent()` is application-level; model has no unique constraint on `queue_item_id` | Existing duplicate groups must be remediated before adding the index |
| End-to-end capacity telemetry exists | Contradicted | Health, service runs, logs, and pool logging exist; no OpenTelemetry/Prometheus queue-age/fairness model | Phase 1 measurement work is required before capacity claims |
| Baseline test total is independently reproduced here | Verified | Safe local run on 2026-07-30 completed with 2,194 passed / 56 PostgreSQL-dependent skips in 40.04 seconds | Matches the supplied baseline count |

## 5. Failure-window analysis

### Outbox marked published, Redis message lost

1. PostgreSQL command/job/outbox commit succeeds.
2. Relay publishes to Redis and stores the broker ID.
3. Redis loses or trims the stream entry before a worker leases the job.
4. A relay that only scans `published_at IS NULL` never republishes.

**Required invariant:** periodically scan authoritative ready/unleased jobs,
atomically increment a monotonic wake-up generation, and insert an event unique
to that generation independent of the old outbox delivery flag. A cooldown
bounds duplicate generations.

### Delayed wake-up is lost

1. A rate-limited job enters `waiting` until `available_at`.
2. Its delayed wake-up is published and then lost.
3. A scanner that looks only for `ready` jobs never sees it.

**Required invariant:** atomically promote due `waiting` work to `ready` and emit
the same generation-based outbox wake-up.

### Callback webhook succeeds without callback answer

1. Telegram POSTs a callback update.
2. Ingress returns HTTP 2xx after creating a command.
3. No `answerCallbackQuery` request occurs.
4. The user's button keeps spinning despite successful admission.

**Required invariant:** callback acknowledgement has a measured, reserved path.
Webhook HTTP acknowledgement and Bot API callback acknowledgement are distinct.

### Worker pauses beyond its lease

1. Worker A leases a publish job and pauses during provider I/O.
2. Its lease expires; worker B obtains a new lease.
3. Worker A resumes and finalizes or performs another effect.

**Required invariant:** every lease has an ownership token/fencing value.
Heartbeat/finalization updates match that token. Persist an irreversible one-shot
effect permit bound to the lease, then recheck immediately before send.
Successors only reconcile. A stale holder paused in the unavoidable
post-check/pre-send gap may still issue the sole call after lease loss, but cannot
issue twice or finalize; post-effect expiry moves to reconciliation/review.

### Cancellation races an external effect

1. A command enters `running`.
2. The worker starts a provider request.
3. The user cancels before a response is received.
4. Declaring immediate `cancelled` could hide a successful effect.

**Required invariant:** persist `cancel_requested`; if an effect may have started,
transition through `cancelling`/reconciliation. Only a proven no-effect outcome
may become `cancelled`.

### Redis limiter fails between hierarchical buckets

1. Global capacity token is consumed.
2. Tenant bucket check fails or Redis connection drops.
3. Capacity leaks or only part of the hierarchy is enforced.

**Required invariant:** one atomic script checks and consumes all applicable
buckets, or consumes none. Cost/config version is part of the script input.

### RLS context is missing on a pooled connection

1. A task starts a transaction but omits tenant context.
2. It queries a tenant-owned table.
3. A permissive policy or owner runtime role exposes rows.

**Required invariant:** runtime role is non-owner/no-`BYPASSRLS`; enabled tables
default deny; unit-of-work startup sets transaction-local tenant context; tests
connect as that exact role.

### One tenant floods FIFO stream

1. Tenant A enqueues thousands of jobs before tenant B.
2. Consumer-group FIFO keeps serving A.
3. A's per-tenant concurrency may limit active jobs, but a large prefetch/pending
   list can still delay B.

**Required invariant:** fairness occurs before/at dispatch with bounded prefetch,
weighted tenant quantum, persisted tenant ordering, and age promotion. Stream
FIFO is not the fairness proof.

### Meta publish response is lost

1. Container ID is persisted.
2. `media_publish` succeeds at Meta.
3. Connection drops before Storydump stores the story ID.
4. Retrying publish can create a duplicate.

**Required invariant:** provider operation becomes ambiguous and is reconciled or
held for operator review. No blind retry is allowed.

### Shadow jobs become live after a global flag flip

1. Legacy execution performs the real effect and writes a shadow job.
2. The system later enables durable dispatch globally.
3. If the old row is merely “disabled,” a worker can lease it and repeat the
   already-completed effect.

**Required invariant:** shadow rows carry immutable `execution_mode='shadow_only'`
and lease SQL permanently excludes them. Cutover creates distinct `live` work for
new requests and never activates historical shadow rows.

## 6. Tenant and security analysis

The target security model is materially stronger than the baseline:

- authorization binds principal, active membership, role, and server-resolved
  tenant before admission;
- repository interfaces reject missing tenant context;
- non-null ownership, composite constraints, and RLS provide independent layers;
- privileged maintenance is separated instead of smuggling global behavior
  through `tenant_id=None`;
- credentials and replay-bearing data are excluded from durable work payloads;
- egress validates host and resolved address around redirects;
- audit rows carry actor, tenant, operation, external effect, and outcome.

Remaining proof obligations:

1. Inventory every nullable tenant-owned table and every global read/write path.
2. Decide whether `chat_settings_id` remains the physical tenant key or is
   migrated; avoid dual ambiguous ownership columns.
3. Define runtime, migration, read-only, and maintenance roles and verify
   ownership/`BYPASSRLS` attributes.
4. Verify Railway's header overwrite/trust behavior in the real deployment.
   Merely switching from `X-Forwarded-For` to another client-supplied header is
   not sufficient.
5. Define encrypted command/provider payload references so credentials never
   enter Redis, traces, or attempt errors.
6. Treat foreign-key and unique-constraint error shapes as potential
   cross-tenant existence oracles; PostgreSQL integrity checks bypass RLS.

## 7. Capacity and fairness analysis

The envelope is credible for an evolutionary design, but implementation must
derive these numbers before scaling:

```text
total DB client pool budget
  >= sum(service replicas × per-replica pool size)
  >= peak DB-active tasks, not total provider-active tasks
  < configured Neon/runtime budget with maintenance headroom

peak temporary storage
  <= sync replicas × transfer concurrency × max streamed file size

peak thread work
  <= explicit thread limiter
  <= work that cannot use native async adapters

provider concurrency
  = fixed deployment budget partitioned among replicas
  != per-replica default × replica count
```

Required per-queue settings include process concurrency, global concurrency,
per-tenant concurrency, provider-key concurrency, prefetch, lease duration,
heartbeat interval, attempt/deadline budget, and reserved priority capacity.

The design correctly uses queue age and saturation for autoscaling. CPU can stay
low while provider slots, Redis budgets, DB connections, or ambiguity backlogs
are exhausted.

The weighted relay design is implementable at the initial 250-active-tenant
envelope, but the exact selection query/algorithm needs an ADR and deterministic
tests. “Weighted fair” must produce an observable maximum peer lag, not only a
distribution chart.

## 8. Migration analysis

The order is safe:

1. Measure before behavior change.
2. Establish migration replay and tenant/idempotency foundations.
3. Shadow-write durable work without external dispatch.
4. Add Redis admission and wake-up recovery.
5. Extract low-risk commands and sync before publishing.
6. Canary the publish state machine only after reconciliation is operational.
7. Scale and enforce RLS only after the runtime-role tests pass.
8. Remove legacy loops/state last.

Hazards to prevent:

- dependency upgrades, async-session migration, schema changes, and provider
  cutover must not land as one unreviewable release;
- shadow mode must be immutably non-executable, including Telegram status
  messages; a later live-dispatch flag cannot activate old rows;
- webhook/polling cutover must account for their mutual exclusion and pending
  updates and must remove the baseline's current `drop_pending_updates=True`;
- migration replay must model non-transactional phases and postconditions:
  migration 023 stamps version 23 before its concurrent index is created;
- rollback from publish workers must drain/reconcile leased or ambiguous
  operations before legacy routing resumes;
- RLS rollback must not be “use the owner role”; fix the policy/context issue or
  stop the affected route;
- existing duplicate posting-history groups must be resolved under explicit
  production review before adding uniqueness.

## 9. Required design changes

The original design changes below are incorporated into `epic.md`; they remain
implementation gates.

### P0 — correctness blockers before any new external-effect cutover

- Define and migrate constrained command/job/provider-operation/outbox state
  machines, including lease fencing and one active Meta operation per account.
- Build migration replay/schema-parity CI with non-transactional postconditions
  before relying on new constraints.
- Add due-waiting promotion and generation-based PostgreSQL wake-up recovery so
  Redis/outbox delivery flags cannot strand accepted work.
- Make shadow execution mode immutable and permanently non-leaseable.
- Preserve the container-before-publish anchor and add an explicit ambiguous
  provider-operation state with no blind retry.
- Make tenant context mandatory at service/repository boundaries and prove
  cross-tenant denial with the runtime role.
- Specify Telegram callback acknowledgement separately from webhook HTTP success.

### P1 — required before multi-replica production admission

- Implement atomic shared admission, trusted client identity, and explicit
  Redis-degraded behavior.
- Implement fair dispatch, bounded prefetch, global/per-tenant/provider
  concurrency, and deployment-wide DB budgets.
- Replace synchronous long-lived sessions with explicit task-local async units
  of work.
- Make poison handling atomically quiesce both job and command in review so
  recovery cannot regenerate or lease the same poison work.
- Instrument callback/admission latency, queue age, lease recovery, fairness,
  pool wait, provider budget, and ambiguity backlog.
- Build worker-kill, Redis-loss, rolling-deploy, and one-tenant-flood tests.

### P2 — hardening and scale follow-ups

- Adaptive Cloudinary/Drive concurrency and broader provider budget forecasting.
- Online credential-key rotation and complete SSRF-safe shared egress client.
- Dependency upgrades behind adapter contract tests.
- Additional operation-status delivery mechanisms beyond API polling.

## 10. Acceptance-test matrix

| Scenario | Injection | Expected state/effect | Proof signal |
|---|---|---|---|
| duplicate API command | same tenant/namespace/key/fingerprint concurrently | one command/job, same operation response | unique conflict/idempotency-hit metric |
| mismatched idempotency reuse | same key, different command/payload fingerprint | conflict, no new work | conflict metric + row census |
| webhook replay | same Telegram update ID | one command, callback safely acknowledged | inbox unique row + ack latency |
| post-commit Redis outage | stop Redis before relay | job remains ready, no loss | oldest-ready age + later recovered wake-up |
| published message loss | remove/trim wake-up after relay marks publish | generation scan emits replacement | recovery counter + one eventual lease |
| delayed wake-up loss | lose wake-up while job is waiting | due job promotes to ready and emits a generation | state/wake generation |
| shadow dispatch flip | enable live dispatch with historical shadow rows | shadow rows remain non-leaseable | zero shadow leases/effects |
| concurrent claims | separate processes/connections | one live lease token | conditional-update row count |
| worker crash pre-call | kill after lease, before provider | lease recovery, one later attempt | attempts and lease-expiry trace |
| worker crash post-Meta call | drop response/kill after request | ambiguous/review; no second publish call | provider fake call count equals one |
| stale worker resumes | pause past lease and resume | stale owner cannot finalize | fencing mismatch metric |
| stale one-shot permit holder | pause after permit/check, expire lease, run successor, resume holder | successor reconciles; at most one call; stale holder cannot finalize | provider call count + permit token |
| cancel during provider call | issue cancel while response blocked | cancelling/reconcile, not false cancelled | command state and provider call count |
| RLS missing context | omit `SET LOCAL` as runtime role | no tenant rows / denied mutation | runtime-role integration assertion |
| RLS hostile context | tenant A requests tenant B ID | no rows/effects | denial audit + unchanged B rows |
| flood fairness | A contributes 90%, B–N enqueue peers | peers start within SLO | tenant lag quantiles |
| provider 429/420 storm | fake reset/retry headers | bounded retry, lower concurrency, no herd | active/retry timeline |
| DB pool saturation | hold DB-active tasks | fast reject/defer, no 30s request hang | pool wait and admission status |
| rolling deploy | replace ingress/workers during load | no lost command/duplicate publish | terminal-or-review operation census |
| poison job | deterministic non-retryable payload | bounded attempts and quiescent `review_required` job/command | dead-letter/review dashboard |

All external adapters must be fake, sandboxed, or dry-run. A production post is
not an acceptable test oracle.

## 11. Open decisions

1. **Migration mechanism:** adopt Alembic or formalize/replay the numbered SQL
   runner. Either choice must build test databases from migrations.
2. **Physical tenant key:** retain `chat_settings_id` as the tenant key or perform
   a separately reviewed rename; do not maintain two optional ownership sources.
3. **Fair relay algorithm:** choose the persisted virtual-finish/weighted-quantum
   implementation and define its starvation bound.
4. **Redis operating policy:** persistence, memory/no-eviction policy, stream
   trimming, TLS/private networking, backup expectations, and outage SLO.
5. **Callback acknowledgement adapter:** Bot API call in the webhook response
   versus a separately measured reserved sender path.
6. **Database roles and budget:** exact runtime/maintenance/migration roles,
   compute connection ceiling, and per-service pool allocations.
7. **Operation result API:** polling is sufficient for first cutover or a
   server-push mechanism is required.

These decisions affect schema or correctness and require explicit review. Package
layout and naming can remain implementation details.

## 12. Final recommendation

Approve the architecture direction and documentation set for implementation
planning. Do not authorize schema, webhook, Redis, Railway, or publish cutover
from this approval alone. Begin only with measurement/migration-test foundations,
then require each phase's documented gate before external effects move to the new
path.
