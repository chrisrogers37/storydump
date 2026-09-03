> **⛔ SUPERSEDED — DO NOT IMPLEMENT FROM THIS DOCUMENT.** The authoritative plan is [`../../planning/2026-08-02-consolidated-design-plan/`](../../planning/2026-08-02-consolidated-design-plan/README.md); every increment, gate, and shape this file proposed is restated or struck there. Retained only as historical input and evidence for the reviews that cite it.

# Test-Driven Implementation Plan

**Epic:** [`epic.md`](epic.md)  
**Triage:** [`tiered-issue-triage.md`](tiered-issue-triage.md)  
**Baseline:** `main` at `683f7cf`  
**Status:** Proposed; implementation and production mutation are not authorized

This plan gives file-level seams and test order. Each increment is independently
reviewable, deployable behind a disabled flag, and reversible by stopping new
routing. It must not run production migrations, set Telegram webhooks, provision
Redis/Railway services, or exercise live provider effects without separate
explicit approval.

## Execution rules

1. Write a failing unit/contract/integration test before behavior.
2. Keep API/worker entrypoints thin:
   `API/worker/CLI → Services → Repositories → Models`.
3. Repositories do not call providers and do not commit transactions they do not
   own. Services define the unit of work.
4. PostgreSQL owns accepted intent, jobs, leases, effect anchors, and outcomes.
5. Redis adapters may pace or wake work but never create authoritative intent.
6. Every external-effect change ships with a fake adapter and kill-at-boundary
   tests before a live cohort can be enabled.
7. Upgrade dependencies separately from behavioral cutovers.
8. Use the package manager to select the latest compatible dependency at
   implementation time; do not copy versions from this design.
9. Update `CHANGELOG.md` and relevant operations/design documentation per PR.
10. Run the repository pre-commit suite before every implementation commit.

## Proposed target file map

Names may be refined in the first contract PR, but layer ownership must not
change.

```text
src/
├── api/
│   ├── dependencies.py
│   ├── middleware/
│   │   ├── correlation.py
│   │   └── request_limits.py
│   └── routes/
│       ├── commands.py
│       ├── operations.py
│       └── telegram_webhook.py
├── config/
│   ├── database.py
│   └── settings.py
├── models/
│   ├── inbox_event.py
│   ├── command.py
│   ├── job.py
│   ├── job_attempt.py
│   ├── provider_operation.py
│   ├── outbox_event.py
│   ├── tenant_dispatch_state.py
│   └── rate_limit_observation.py
├── observability/
│   ├── metrics.py
│   ├── tracing.py
│   └── event_loop.py
├── repositories/
│   ├── unit_of_work.py
│   ├── inbox_repository.py
│   ├── command_repository.py
│   ├── job_repository.py
│   ├── provider_operation_repository.py
│   ├── outbox_repository.py
│   ├── tenant_dispatch_repository.py
│   └── rate_limit_observation_repository.py
├── services/
│   ├── core/
│   │   ├── admission_service.py
│   │   ├── operation_service.py
│   │   ├── job_service.py
│   │   ├── outbox_service.py
│   │   ├── fair_dispatch_service.py
│   │   ├── provider_operation_service.py
│   │   └── schedule_dispatch_service.py
│   └── integrations/
│       ├── redis_client.py
│       ├── distributed_limiter.py
│       └── redis_streams.py
└── workers/
    ├── runtime.py
    ├── command_worker.py
    ├── publish_worker.py
    ├── sync_worker.py
    ├── maintenance_worker.py
    └── reconciler_worker.py
```

Corresponding tests stay under `tests/src/<layer>/`; real PostgreSQL/Redis and
cross-process scenarios go under `tests/integration/`; deterministic load
scenarios go under `tests/load/`.

## Increment 0 — Freeze contracts and measurement points

**Purpose:** Remove ambiguity before schema or dependency work.

### Tests first

- Add a documentation/contract test that enumerates persistence states from model
  enums and legal transition policy from the owning services.
- Add tests for operation/trace ID propagation through current FastAPI and
  Telegram entry seams.
- Add an event-loop lag sampler test with deterministic fake time.
- Add a metric-cardinality test that rejects tenant/job/operation IDs as labels.

### Files

- Add `src/observability/metrics.py`, `tracing.py`, `event_loop.py`.
- Add `src/api/middleware/correlation.py`.
- Modify `src/api/app.py`, `src/main.py`, and Telegram dispatch entrypoints to
  create/propagate IDs without changing decisions.
- Modify `src/config/settings.py` for export and sampling kill switches.
- Add `tests/load/scenarios.py` and an adapter-only baseline harness.

### Gate

- Existing posting decisions and call counts are unchanged.
- Callback/API latency, event-loop lag, pool wait, and current scheduler fan-out
  have named measurement points.
- Load harness cannot resolve production provider credentials.

## Increment 1 — Establish migration replay

**Purpose:** Make schema changes testable before adding tables.

### Decision

Choose one:

- configure Alembic, already present as a dependency, and represent the existing
  49 migrations in a verified baseline; or
- formalize the numbered SQL files with transactional/non-transactional phases,
  checksums, dependency/version validation, postconditions, and CI replay.

Do not maintain two independent migration histories.

### Tests first

- Empty-database replay reaches the expected schema version.
- Re-running replay is a no-op.
- A failed transactional phase leaves its version unchanged; a failed
  non-transactional phase remains incomplete until its postcondition passes.
- Migration 023's version-before-`CREATE INDEX CONCURRENTLY` shape is detected
  and repaired/baselined by postcondition before the runner calls version 23
  complete.
- Introspection proves representative partial indexes/check constraints from
  existing migrations.
- ORM metadata drift is reported, not silently used to “fix” production.

### Files

- Add the chosen migration configuration/runner under the standard migration
  location.
- Modify `tests/conftest.py` so migration integration databases are built by
  replay rather than `Base.metadata.create_all()`.
- Narrow or remove `src/config/database.py:init_db()` from production guidance.
- Add `tests/integration/test_migration_replay.py` and
  `tests/integration/test_schema_contract.py`.
- Update deployment/testing documentation with pooled versus direct endpoint use.

### Gate

A clean PostgreSQL instance replays in CI and current integration tests pass
against the migrated schema.

## Increment 2 — Add an async unit-of-work substrate

**Purpose:** Give new request/job paths explicit task-local transactions without
requiring a big-bang legacy rewrite.

### Dependency PR

In a standalone change, use the package manager to add the latest compatible
psycopg 3 async driver and pool support. Keep psycopg2 only while legacy sync
paths require it. Verify Neon pooled and direct endpoints before removing it.

### Tests first

- One `AsyncSession` per concurrent task.
- Commit on successful unit-of-work exit; rollback on exception/cancellation.
- Provider waits occur after transaction/session release.
- Pool acquisition, statement, lock, and transaction timeouts are classified.
- PgBouncer transaction-mode contract: transaction-local settings do not leak to
  the next transaction.

### Files

- Modify `src/config/database.py` to expose explicit sync-legacy, async-runtime,
  and direct-migration engine factories without creating hidden sessions.
- Add `src/repositories/unit_of_work.py`.
- Add `src/api/dependencies.py` to provide services/unit-of-work, never raw
  repositories to routes.
- Add `tests/src/repositories/test_unit_of_work.py`.
- Add `tests/integration/test_async_database.py` and
  `tests/integration/test_pgbouncer_transaction_contract.py`.

### Gate

New durable-path repositories use an injected `AsyncSession` and never call
`commit()` internally. Legacy `BaseRepository` remains isolated until migrated.

## Increment 3 — Make tenant context explicit

**Purpose:** Close optional scoping before durable multi-tenant tables become
active.

### Tests first

Create a parameterized repository contract:

- missing tenant raises before SQL;
- tenant A reads/writes A;
- tenant A cannot read/write B by known UUID;
- global maintenance requires an explicit privileged method/type;
- insert/update cannot change a row's tenant;
- legacy NULL behavior is measured table by table.

### Files

- Keep tenant resolution/context service-owned and pass an explicit tenant UUID
  into repositories; repositories must not import the service layer.
- Modify `src/repositories/base_repository.py` so tenant-owned query helpers
  require context; keep a temporary explicitly named legacy/global helper only
  for unmigrated callers.
- Modify service/repository signatures table by table, not by a repository-wide
  flag.
- Add owner inventory/backfill migrations for `media_items`, `posting_queue`,
  `posting_history`, media locks, category mix, API tokens, memberships, audit,
  and any newly classified table.
- Add `tests/integration/test_tenant_repository_contract.py`.
- Update existing tenant-related tests under `tests/src/repositories/` and
  `tests/src/services/`.

### Gate

No changed production path can convert a missing tenant into an unscoped query.
Backfill migrations include preflight counts and stop on ambiguous ownership.

## Increment 4 — Add runtime roles and RLS in test/audit mode

**Purpose:** Build defense in depth before enforcement.

### Tests first

- Runtime role is not owner, superuser, or `BYPASSRLS`.
- Missing transaction-local tenant context defaults to no tenant rows.
- Correct context permits expected rows and `WITH CHECK` writes.
- Wrong context denies reads/writes.
- Context does not survive transaction reuse through the pool.
- Maintenance role reaches only documented global operations.

### Files

- Add migration-managed role grants/policies, or documented bootstrap SQL if
  provider role creation must be separate.
- Add tenant-context setup to `src/repositories/unit_of_work.py` using
  transaction-local `set_config`.
- Add `tests/integration/test_rls_runtime_role.py`.
- Add a policy inventory test that compares tenant-owned models with enabled
  policies.
- Add deployment configuration names for runtime, migration, and maintenance
  URLs/roles.

### Gate

Policies are testable and can run in an audit/shadow cohort. Production
enforcement waits until every caller supplies context.

## Increment 5 — Add durable operation schema

**Purpose:** Create authoritative records without dispatching them.

### Tests first

- Model/DDL state vocabularies match.
- Unique `(provider, provider_account_id, provider_event_id)` and
  `(tenant_id, command_namespace, idempotency_key, execution_mode)` collapse
  concurrent inserts.
- A matching key with a different request fingerprint returns conflict.
- Deterministic schedule-intent uniqueness collapses multi-replica discovery.
- Every tenant-owned foreign key/unique constraint carries tenant identity where
  needed.
- Sanitization rejects secrets and oversized payloads.

### Files

- Add models:
  `inbox_event.py`, `command.py`, `job.py`, `job_attempt.py`,
  `provider_operation.py`, `outbox_event.py`, `tenant_dispatch_state.py`, and
  `rate_limit_observation.py`.
- Extend `src/models/enums.py` with persistence state enums only. Legal transition
  policy belongs to the owning services, not models.
- Import new models in `src/models/__init__.py`.
- Add one or more additive migrations; do not reuse `posting_queue` as `jobs`.
- Add matching repository files with injected `AsyncSession`.
- Add model/repository tests and real concurrent uniqueness tests.

### Gate

Tables are additive, empty, non-dispatching, and replayable. No legacy behavior
reads them.

## Increment 6 — Implement command and job state services

**Purpose:** Put all transitions behind service-owned units of work.

### Tests first

Use table-driven and property/model-based tests:

- every legal transition succeeds once;
- every illegal/stale transition affects zero rows;
- duplicate command returns the original operation/result;
- idempotency reuse with a different command/payload fingerprint returns conflict;
- `shadow_only` commands/jobs can never transition into live execution;
- cancellation before lease becomes cancelled;
- cancellation after possible effect becomes cancelling/review, never falsely
  cancelled;
- terminal states cannot return to ready;
- deadlines and retry budgets produce explicit terminal reasons.

### Files

- Add `src/services/core/admission_service.py`,
  `operation_service.py`, and `job_service.py`.
- Add typed request/result DTOs in service modules or a dedicated core schema
  module; do not return ORM models to API routes.
- Add `tests/src/services/test_admission_service.py`,
  `tests/src/services/test_operation_service.py`, and
  `tests/src/services/test_job_service.py`.
- Add `tests/integration/test_command_idempotency.py`.

### Gate

One service call can atomically insert inbox, command, initial job, and outbox
records, but dispatch remains disabled.

## Increment 7 — Implement leases and attempts

**Purpose:** Replace re-claimable status with explicit ownership.

### Tests first

- N separate connections/processes claim one job: one winner.
- A committed live lease is not claimable.
- Owner/token-matched heartbeat extends the lease.
- Wrong or stale token cannot renew/finalize/release.
- Expiry creates one new attempt opportunity.
- Graceful shutdown releases only safe pre-effect work.
- Pause worker A past expiry, let B claim, resume A: A cannot finalize.
- Expired pre-effect work may return to ready; work whose provider operation is
  `effect_may_have_started` enters reconciliation/review and never generic retry.

### Files

- Extend `job_repository.py` with single-statement conditional claim, renew,
  finalize, wait, and reap methods.
- Extend `job_service.py` with lease lifecycle and cancellation checks.
- Add worker identity/fencing types to `src/workers/runtime.py`.
- Add `tests/integration/test_job_claim_concurrency.py`,
  `tests/integration/test_job_lease_fencing.py`, and
  `tests/integration/test_job_recovery.py`.

### Gate

No worker begins job processing or finalizes without a live PostgreSQL lease
token. The unavoidable post-check/pre-send external-call gap is handled by the
one-shot effect permit in Increment 8, not by pretending the database can
interrupt a network call.

## Increment 8 — Generalize external-effect safety

**Purpose:** Move the existing Meta anchor into a durable provider-operation
state machine and make finalization race-safe.

### Tests first

- Concurrent creation of one provider business key yields one operation.
- One active Meta publish operation per account.
- Container ID commits before fake `media_publish` starts.
- An irreversible one-shot `effect_may_have_started` permit, bound to the current
  lease token, commits before each externally visible call. The holder rechecks
  its lease immediately before send; successors may only reconcile/review and
  never issue that effect.
- Pause the permit holder after permit/check but before send, expire its lease,
  run a successor, then resume it: the stale holder may issue the sole unavoidable
  call but cannot issue twice or finalize; total provider call count stays at
  most one.
- Kill/drop response at create, poll, publish, and finalize boundaries.
- Pre-effect transient failure may retry within budget.
- Provider-confirmed failed container may retry according to adapter contract.
- Ambiguous publish never invokes `media_publish` again.
- Stale lease owner cannot finalize provider success.
- Final history/counters/lock/job/command/outbox commit once.

### Files

- Add `provider_operation_repository.py` and
  `provider_operation_service.py`.
- Refactor the state-machine core out of
  `src/services/core/scheduler.py` and
  `src/services/core/telegram_autopost.py` behind a service interface while
  legacy routing remains unchanged.
- Modify `src/services/integrations/instagram_api.py` to implement a typed
  adapter contract with sanitized response categories.
- Add a deterministic fake provider under `tests/fakes/`.
- Add the reviewed posting-history uniqueness migration only after a separately
  approved duplicate remediation.
- Add `tests/integration/test_meta_publish_recovery.py` and
  `tests/integration/test_atomic_publish_finalization.py`.

### Gate

The new provider state machine remains dispatch-disabled but passes every crash
boundary with at most one fake external publish.

## Increment 9 — Implement outbox, relay, and independent ready-job recovery

**Purpose:** Guarantee eventual wake-up without making Redis authoritative.

### Tests first

- Domain transaction rollback leaves no outbox row.
- Committed domain change always has its outbox row.
- Concurrent relays claim disjoint bounded batches.
- Publish failure schedules jittered retry.
- Poison event atomically moves its job and command to quiescent
  `review_required`; relay, recovery scans, and lease queries exclude both.
- Audited operator resolution creates a replacement generation/job rather than
  reviving a poison row.
- Redis loss before publication recovers.
- Redis loss/trim after recorded publication recovers via a generation-based
  ready-job scan.
- A lost delayed wake-up atomically promotes due `waiting` work to `ready`,
  increments `wakeup_generation`, and emits a new outbox event.
- Concurrent recovery scans cannot use the same generation; a cooldown bounds
  duplicate generations.
- Duplicate wake-ups produce one lease/effect.

### Files

- Add `outbox_repository.py`, `outbox_service.py`, and
  `src/workers/reconciler_worker.py`.
- Add a broker port/interface in a core module and a no-effect in-memory fake.
- Add `tests/src/services/test_outbox_service.py`.
- Add `tests/integration/test_outbox_relay.py` and
  `tests/integration/test_ready_job_recovery.py`.

### Gate

With no Redis implementation installed, the fake broker proves the
PostgreSQL/outbox/recovery semantics.

## Increment 10 — Shadow-write legacy decisions

**Purpose:** Compare new records with current behavior without new effects.

### Tests first

- Each selected legacy command/schedule action creates exactly one correlated
  shadow operation.
- Shadow command/job records carry immutable `execution_mode='shadow_only'`;
  relay and lease SQL exclude them even after live dispatch is enabled.
- Cutover creates new `live` jobs for new requests and never flips historical
  shadow rows.
- Every shadow provider adapter call raises a test failure.
- Divergent legacy/shadow decision records enough sanitized context to diagnose.

### Files

- Modify current service seams, not handlers/repositories directly, to invoke the
  admission/shadow service.
- Add settings:
  `SHADOW_OPERATIONS_ENABLED` and `DURABLE_DISPATCH_ENABLED` (default false).
- Add a shadow comparison service and bounded metrics.
- Add scenario tests for callback, API command, schedule intent, sync, and manual
  posting finalization.

### Gate

Production-like test traffic shows one-to-one shadow cardinality and zero calls
through new provider adapters. Turning on live dispatch in the test still leaves
all historical shadow jobs non-leaseable.

## Increment 11 — Add Redis coordination in an isolated dependency PR

**Purpose:** Implement shared limits and stream wake-ups without changing route
ownership.

### Tests first

- Atomic multi-bucket check/consume or no consume.
- Redis server time controls refill.
- Scope keys match bot/chat, Meta account, Drive project/user, Cloudinary
  environment/tenant, principal/tenant/global admission.
- Reserved callback/cancellation/reconciliation capacity.
- Script cache loss reloads safely.
- Timeout/unavailable policy is explicit per operation class.
- Stream publish/read/ack/pending reclaim and bounded prefetch.

### Files

- Use the package manager to add the latest compatible async Redis client.
- Add `src/services/integrations/redis_client.py`,
  `distributed_limiter.py`, and `redis_streams.py`.
- Add Redis settings with private/TLS credential handling and key prefixes.
- Add `tests/src/services/integrations/test_distributed_limiter.py`.
- Add `tests/integration/test_redis_streams.py`.
- Add an integration Redis service to CI; do not provision production.

### Gate

Redis restart and script-cache loss pass. PostgreSQL tests still prove no accepted
work depends solely on Redis.

## Increment 12 — Implement fair dispatch

**Purpose:** Prevent stream FIFO or a heavy tenant from defining execution order.

### Decision/test first

Write an ADR selecting the weighted-quantum/virtual-finish algorithm and define:

- tenant weight and maximum quantum;
- queue priority and age promotion;
- per-tenant/provider active limits;
- persisted cursor/virtual finish;
- bounded relay batch and stream prefetch;
- starvation bound and restart behavior.

Then test deterministic sequences, concurrent relay replicas, a 90% heavy tenant,
priority reservation, and low-priority progress.

### Files

- Add `tenant_dispatch_repository.py` and
  `fair_dispatch_service.py`.
- Extend outbox relay to publish selected queue/priority wake-ups.
- Add `tests/src/services/test_fair_dispatch_service.py`.
- Add `tests/integration/test_fair_relay_concurrency.py`.
- Add `tests/load/test_tenant_fairness.py`.

### Gate

Peer latency and share assertions pass under the acceptance envelope; worker
prefetch cannot monopolize one tenant's backlog.

## Increment 13 — Add ingress routes without cutover

**Purpose:** Exercise durable admission and operation lookup behind disabled
routes/cohorts.

### Tests first

- Provider secret, payload type/size, update ID, and callback ID validation.
- API auth/membership/role and required idempotency key.
- Exact auth → bounded fingerprint-checked idempotency lookup → Redis write
  budget → transaction → response ordering.
- Commit failure never returns accepted.
- Idempotency hit returns original operation.
- Idempotency mismatch returns conflict and creates no work.
- An identical retry can recover its operation through a separately bounded read
  path during Redis degradation.
- Redis failure rejects writes with documented status.
- `answerCallbackQuery` is invoked/returned and timed separately from command
  completion.
- Telegram non-2xx behavior on PostgreSQL failure.
- Redis-degraded tests cover callback acknowledgement, authorized cancellation of
  an admitted operation, duplicate lookup, rejection of new work, and rejection
  of unauthorized cancellation.

### Files

- Add `src/api/routes/telegram_webhook.py`, `commands.py`, and `operations.py`.
- Add request/response schemas without exposing ORM rows.
- Modify `src/api/app.py` to register routes only when enabled.
- Replace wildcard proxy trust with a deployed-topology-tested configuration.
- Add `TELEGRAM_WEBHOOK_INGRESS_ENABLED=false` and
  `DURABLE_API_COMMANDS_ENABLED=false`.
- Add API tests under `tests/src/api/`.
- Add `tests/integration/test_redis_degraded_admission.py`.

### Gate

Routes pass replay/burst tests with fake providers. No `setWebhook` call occurs;
polling remains the only live ingress.

## Increment 14 — Extract command workers

**Purpose:** Cut over low-risk, non-publish operations first.

### Tests first

- Multiple workers process disjoint leases.
- Slow command does not delay callback acknowledgement or another tenant.
- Telegram send timeout enters an ambiguous provider-operation state where
  applicable.
- Distributed bot/chat budget applies across workers.
- Rolling restart yields one effect and one terminal or quiescent
  `review_required` operation.

### Files

- Add `src/workers/runtime.py` and `command_worker.py`.
- Move selected handler business logic into narrow core services; handlers/API
  only admit commands and render responses.
- Add Railway process command documentation, health/readiness, and graceful
  shutdown.
- Add command-kind routing allowlist and tenant cohort settings.

### Gate

Quick-command cohort meets SLOs and can roll back by setting routing percentage
to zero while already accepted jobs drain.

## Increment 15 — Extract sync workers and streaming media

**Purpose:** Isolate the largest bulk workload.

### Tests first

- Paged Drive listing and incremental cursor recovery.
- Stream byte cap, temporary-file cleanup, and redirect/DNS validation.
- Global/per-worker/per-tenant transfer caps.
- Drive weighted quota and Cloudinary 420 backoff/adaptive reduction.
- Worker kill during listing/download/reconcile.
- Slow tenant does not delay peers.

### Files

- Add `src/workers/sync_worker.py`.
- Refactor `src/services/core/media_sync.py` into paged, resumable service
  operations.
- Modify Drive and Cloudinary adapters to use lifecycle-managed clients/streaming.
- Remove only the routed cohort from `media_sync_loop`; keep rollback behavior.
- Add provider fake and load tests.

### Gate

Memory, temporary storage, provider concurrency, and fairness remain bounded under
250-tenant due/sync scenarios.

## Increment 16 — Cut over Telegram webhooks

**Purpose:** Make ingress horizontally scalable after command workers are proven.

### Pre-cutover proof

- Railway trusted-header behavior tested in a staging deployment.
- Webhook secret rotation and health/readiness runbook complete.
- Remove and regression-test the baseline
  `TelegramService.start_polling(drop_pending_updates=True)` behavior before any
  webhook cutover. The pending-update plan preserves updates; rollback polling
  passes updates through the same durable inbox/dedup seam.
- One polling worker can be restored by a tested rollback procedure.
- Shared Telegram budget covers every worker/API sender.

### Files

- Add staging/production operations guide under `documentation/operations/`.
- Modify worker startup so polling is mutually exclusive with webhook mode.
- Add webhook status metrics and alerting.

### Gate

The actual `setWebhook` mutation requires explicit operator approval. Cutover is
complete only after replay, callback SLO, multi-replica, and rollback drills.

## Increment 17 — Canary publish workers

**Purpose:** Move the highest-risk effect last.

### Tests first

Re-run the Increment 8 boundary suite through the real worker, stream, limiter,
lease, and outbox stack. Add:

- Meta quota exhaustion and reset scheduling;
- account serialization across replicas;
- cancellation at every phase;
- worker rolling restart;
- outbox/Redis loss around provider transitions;
- manual fallback and operator-review behavior.

### Files

- Add `src/workers/publish_worker.py` and `maintenance_worker.py` reconciliation
  handlers.
- Route `scheduler.py` and `telegram_autopost.py` through durable admission for
  allowlisted tenants only.
- Add reconciliation dashboard/API through services.
- Add `PUBLISH_WORKER_TENANT_ALLOWLIST` and global kill switch, default empty/off.

### Gate

No cohort expansion until every operation is terminal/review, ambiguous backlog
alerts work, and fake/sandbox crash drills issue zero duplicate publishes.
Production publish enablement requires explicit user/operator approval.

## Increment 18 — Enforce RLS, then retire legacy state

**Purpose:** Enforce defense in depth before broad multi-replica rollout, then
remove legacy state only after every rollback cohort is gone.

### Sequence

1. Validate owner backfills and `NOT NULL` constraints table by table.
2. Run canary application traffic with the non-owner role and RLS policies for
   controlled cohorts.
3. Require RLS/runtime-role gates before broad multi-replica rollout.
4. Remove privileged/global calls from ordinary services.
5. Migrate remaining synchronous repositories to the async unit of work.
6. Extract maintenance loops.
7. After rollback cohorts are gone, remove polling, legacy dispatch, and
   in-memory operation-state correctness
   responsibilities.
8. Remove psycopg2 and old session cleanup only after no caller remains.

### Tests

- Full runtime-role isolation suite.
- Full migration replay and schema contract.
- No imports/calls of retired global helpers.
- Multi-replica load/resilience suite.
- Manual posting fallback still works.

### Gate

Removal is a separate PR from cutover and occurs only after telemetry shows no
legacy cohort or queued operation depends on the old path.

## State transition contract to pin in tests

### Commands

| From | Allowed to |
|---|---|
| `accepted` | `queued`, `cancelled`, `failed`, `review_required` |
| `queued` | `running`, `cancelled`, `failed`, `review_required` |
| `running` | `queued` (classified retry), `cancelling`, `succeeded`, `failed`, `review_required` |
| `cancelling` | `cancelled`, `succeeded`, `failed`, `review_required` |
| `review_required` | no automatic transition; audited operator resolution may queue reconciliation or choose a terminal state |
| terminal (`succeeded`, `failed`, `cancelled`) | none |

### Jobs

| From | Allowed to |
|---|---|
| `ready` | `leased`, `cancelled`, `failed` |
| `leased` | `running`, `ready` (safe release/expiry), `cancelled`, `failed` |
| `running` | `waiting`, `succeeded`, `failed`, `review_required`; `cancelled` only when no external effect may have started |
| `waiting` | `ready`, `cancelled`, `failed`, `review_required` |
| `review_required` | no automatic transition; operator action creates/audits reconciliation |
| terminal (`succeeded`, `failed`, `cancelled`) | none |

`leased` and `running` transitions always match lease owner/token. Expiry is an
explicit reaper transition, not an implicit claim of `running`. Due `waiting`
work is promoted to `ready` with a generation-based wake-up. Expired work with
`effect_may_have_started` enters reconciliation/review instead of `ready`.

### Meta provider operation

```text
created
  -> container_requested
  -> container_ready
  -> publish_requested
     -> confirmed_succeeded
     -> confirmed_failed
     -> ambiguous
ambiguous -> reconciling -> confirmed_succeeded | confirmed_failed | review_required
review_required --audited operator evidence--> reconciling
```

Only `confirmed_failed` classifications explicitly marked safe by the adapter may
create a new publish attempt. `review_required` is quiescent and never
auto-retries; an audited operator action may attach evidence and re-enter
reconciliation.

## Feature flags and rollback semantics

| Flag | Default until gate | Rollback effect |
|---|---|---|
| `SHADOW_OPERATIONS_ENABLED` | false | stop new immutable shadow-only writes |
| `DURABLE_DISPATCH_ENABLED` | false | stop new live leases; shadow-only rows remain permanently excluded |
| `REDIS_ADMISSION_ENABLED` | false until tested | reject new durable admissions; allow only bounded duplicate lookup, callback acknowledgement, authorized cancellation of admitted work, and separately configured shadow/legacy rollback paths |
| `DURABLE_API_COMMANDS_ENABLED` | false | stop new API durable route |
| `TELEGRAM_WEBHOOK_INGRESS_ENABLED` | false | requires coordinated return to one poller |
| `COMMAND_WORKER_ROUTING_PERCENT` | 0 | stop new cohort routing; drain accepted jobs |
| `SYNC_WORKER_ROUTING_PERCENT` | 0 | stop new sync routing; preserve cursors/jobs |
| `PUBLISH_WORKER_TENANT_ALLOWLIST` | empty | stop new publish admissions; reconcile in-flight |

No rollback deletes command, job, attempt, outbox, provider-operation, or audit
rows. A publish rollback never sends an ambiguous operation back to legacy
autopost.

## Verification matrix

| Stage | Required verification |
|---|---|
| Every PR | Ruff check, Ruff format check, full pytest, changelog/docs |
| Schema PR | Empty replay, upgrade replay, constraint introspection, ORM drift |
| Redis PR | Unit scripts + real Redis restart/cache-loss tests |
| Lease PR | Separate connections/processes, stale-owner fencing |
| RLS PR | Exact runtime role through pooled endpoint |
| Provider PR | Fake adapter kill/drop-response matrix |
| Worker PR | Rolling restart, graceful shutdown, bounded concurrency |
| Cutover PR | 200-click burst, 250 due tenants, 90% tenant flood, provider storms, DB saturation |

PostgreSQL/Redis tests that cannot run locally must fail visibly as environment
skips and be mandatory in CI. A skipped concurrency/RLS test is not evidence that
the corresponding gate passed.

## Definition of done

Implementation is complete only when all epic acceptance criteria pass, the
operation census has no silent non-terminal rows, every production route uses
mandatory tenant context, provider ambiguity is actionable, and the legacy
single-process correctness mechanisms can be removed without changing external
behavior.
