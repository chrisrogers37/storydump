# Epic: Workspace-Rooted Data Model via Strangler Migration

**Status:** Proposed (documentation only; no implementation on this branch).
**Baseline:** `main` at `683f7cf`.
**Design:** [03-recommended-target-model.md](03-recommended-target-model.md).
**Playbook each phase must follow:**
[06-migration-and-consumer-plan.md](06-migration-and-consumer-plan.md).
**Issue mapping:** [05-tiered-issue-triage.md](05-tiered-issue-triage.md).

---

## Epic statement

Migrate Storydump from a chat-settings-rooted, nullable-tenant schema maintained by
hand-applied SQL to a workspace-rooted domain model with structural tenant isolation,
DB-enforced idempotency, durable external side effects, and a single CI-exercised
migration system — without a big-bang rewrite, without downtime, and without ever
triggering a live Instagram or Telegram post from any migration, test, or validation
step.

## Outcomes

| # | Outcome | Verifiable end state |
|---|---|---|
| O1 | One migration system | Alembic produces prod upgrades, fresh installs, and CI/test schemas; `create_all()` and `scripts/setup_database.sql` retired; Railway applies migrations at deploy (#638, #712, #411) |
| O2 | Structural tenancy | Every workspace-scoped table has `workspace_id NOT NULL` + FK (+ composite FKs on cross-referencing children); zero rows with NULL ownership (#412, #669, #670, #576) |
| O3 | Idempotent publishing ledger | Intent/attempt/outcome tables with the §3 uniques; duplicate-outcome and double-claim classes are constraint violations, not incidents (#695, #551, #604, #711, #560) |
| O4 | Durable side effects | Transactional outbox + DB-guarded claims replace process-local locks/markers; a worker restart mid-operation loses nothing (#578, #611, #612) |
| O5 | Single-owner transactions | Unit of Work at service seams; repositories never commit; `atomic_session` monkey-patch deleted (#608, #630) |
| O6 | Legacy retirement | Legacy columns/tables removed after burn-in; consumers (API, bot, CLI, BFF/JWT, analytics) read only the target model |

## Phases, sequencing, and acceptance criteria

Phases are strictly ordered where stated; within a phase, work items may parallelize.
Every phase ends with its gate **green in production for the stated burn-in** before
the next phase's cutover begins (expand work may proceed earlier).

### Phase 0 — Rails (prerequisite for everything)

Adopt Alembic with the current schema as the baseline revision; CI job that replays
all migrations onto a clean Postgres and diffs against ORM metadata (constraints and
partial indexes included); Railway pre-deploy migration step with ordering rules for
worker vs web; reconciliation/drift report tooling; UoW scaffolding introduced behind
existing call sites.

- **AC0.1** CI fails on any ORM↔migration divergence (closes the #639/#641/#654
  class); demonstrated by a deliberately divergent test commit.
- **AC0.2** A fresh database created only by Alembic passes the full test suite.
- **AC0.3** A no-op deploy applies zero migrations; a deploy with a pending migration
  applies it before new code serves traffic (evidence: Railway logs).
- **AC0.4** `schema_version` history is archived and mapped to the Alembic baseline.
- **Sequencing:** blocks all other phases. Nothing else merges DDL until AC0.1–0.3
  hold.

### Phase 1 — Tenancy and identity (expand → backfill → dual-write → cutover)

`workspaces`, `workspace_chat_bindings`, `workspace_settings`,
`external_identities`, `workspace_memberships` (re-rooted), onboarding-state merge.

- **AC1.1** Backfill creates exactly one workspace per existing `chat_settings` row;
  reconciliation report shows 0 unmatched chats, 0 unmatched memberships, and
  enumerates every legacy row with NULL `chat_settings_id` with its resolution
  (adopted by a workspace, or quarantined with reason).
- **AC1.2** Dual-write keeps `chat_settings`/memberships and the new tables
  consistent; a nightly comparator reports 0 diffs for 14 consecutive days.
- **AC1.3** Tenant resolution (`chat_id` → workspace) is one shared function used by
  API, bot, and CLI; shadow-read comparison of legacy vs new resolution logs 0
  mismatches over the burn-in window.
- **AC1.4** No phantom workspace is created by DM interactions (the #524 class);
  test asserts the creation paths.
- **Depends on:** Phase 0.

### Phase 2 — Connections, social accounts, credentials

`integration_connections`, `social_accounts` (workspace-owned), `credentials`
(ciphertext copied verbatim — **no re-encryption**), `auth_method NOT NULL`.

- **AC2.1** Every active `api_token` row maps to exactly one credential row;
  reconciliation lists and resolves rows with NULL `auth_method` (#596) before the
  unique constraint lands.
- **AC2.2** Token refresh, revocation, and key rotation operate on the new tables
  with unchanged observable behavior (existing tests pass against both
  representations during dual-write).
- **AC2.3** A per-workspace Drive error can no longer select the global service
  account: the fallback path is structurally absent (#627) — negative test required.
- **AC2.4** OAuth callbacks bind to a workspace (not a raw chat id) via the state
  token; `/auth/*/start` gains the authorization treatment tracked in #513.
- **Depends on:** Phase 1 (workspace ids must exist).

### Phase 3 — Media

`media_sources`, `provider_objects`, `assets`, `content_items`, `content_holds`.

- **AC3.1** Backfill decomposes every `media_items` row; checksum report proves
  bijection (per-workspace counts by hash, category, active state all match).
- **AC3.2** Asset dedup unique `(workspace_id, content_hash)` lands only after the
  reconciliation shows 0 in-workspace duplicates (dedup performed by the CLI's
  existing dry-run-first pattern, never automatically).
- **AC3.3** Eligibility selection reads the new model behind a flag; because the
  slot decision includes a weighted-random category pick, the shadow comparison
  targets the **deterministic inputs** — the eligible candidate set and the category
  weights — which must agree ≥99.9% over the burn-in, with every disagreement
  explained in the report before cutover.
- **AC3.4** Sync loop writes both representations under one UoW transaction; sync
  latency regression < 10% at p95.
- **Depends on:** Phase 1. Independent of Phase 2 (may run in parallel after
  Phase 1).

### Phase 4 — Publishing ledger and outbox

`publish_intents`, `approval_requests`, `publish_attempts`, `publish_outcomes`
(evolving `posting_history` in place), `outbox_messages`, outbox dispatcher,
consolidated reconciler.

- **AC4.1** `posting_history` gains `intent_id` + unique index only after the
  duplicate groups found by #695 are resolved by a ratified data fix.
- **AC4.2** Every queue transition dual-writes an intent/attempt/approval record;
  ledger-vs-queue comparator reports 0 unexplained diffs for 14 consecutive days
  including at least one worker restart mid-flight.
- **AC4.3** Outbox dispatcher performs all Telegram sends and IG publishes for
  flagged workspaces; kill-the-worker-mid-publish chaos test (dry-run tenant)
  produces: no duplicate story, no lost outcome, one reconciled attempt row.
- **AC4.4** Process-local operation locks, in-flight markers, and cancel flags are
  deleted; #711's double-claim test passes as a DB constraint violation.
- **AC4.5** The four cleanup loops collapse into the reconciler with per-class
  metrics (#571, #565, #366 classes observable as gauges).
- **Depends on:** Phases 1–3 (intents reference content items and social accounts).
  This is the highest-risk phase; it cuts over **per workspace**, dry-run workspaces
  first.

### Phase 5 — Config, audit, read models

`workspace_settings` becomes the only config read path (env = defaults for new
workspaces only); `audit_events` widened; counters replaced by ledger-derived read
models with rebuild commands.

- **AC5.1** Grep-gate in CI: no production code path reads posting-behavior config
  from env (allowlist for bootstrap defaults) (#532, #461, #322).
- **AC5.2** `times_posted`/`total_posts`/`last_post_sent_at` consumers read derived
  projections; a rebuild command reproduces current values within documented
  tolerance, and drift alarms exist.
- **Depends on:** Phase 1 (settings), Phase 4 (ledger for projections).

### Phase 6 — Contract and retirement

`NOT NULL`/FK/unique tightening everywhere; legacy reads retired, then writes, then
tables; `chat_settings`, `posting_queue`, `media_items` (legacy shape),
`api_tokens`, `instagram_accounts`, `user_chat_memberships`, `onboarding_sessions`,
`media_posting_locks`, `audit_log` dropped after burn-in.

- **AC6.1** Each constraint lands only after a 0-violation report over 14 days.
- **AC6.2** Each legacy table is dropped only after (a) a full logical backup of the
  table is archived, (b) code search shows zero references, and (c) one full release
  cycle has run with reads disabled.
- **AC6.3** Post-contract, the CI parity gate, migration replay, and full test suite
  are the only schema mechanisms left standing.
- **Depends on:** all prior phases.

## Cross-phase constraints

1. **Safety:** no migration, backfill, comparator, test, or validation step may send
   a Telegram message or publish to Instagram. Chaos and cutover rehearsals use
   workspaces with `dry_run_mode` semantics and Neon branch databases (the
   Neon-branch dry-run convention already recorded in `CHANGELOG.md` and in
   `scripts/migrations/048_backfill_queue_delivery_states.sql`).
2. **Deployment ordering:** DDL is always backward-compatible with the currently
   deployed code (expand style); worker and web deploy from the same commit; env
   vars are set on **both** Railway services before flags flip (a known operational
   trap — `CLAUDE.md`).
3. **Reversibility:** every cutover is a flag flip back; dual-write remains on until
   the phase's contract step. Down-migrations exist for DDL hygiene but are not the
   recovery mechanism.
4. **CHANGELOG discipline:** every increment updates `CHANGELOG.md` (CI enforces it).

## Dependencies

- **In-repo:** Phase 0 rails; the enum-SSOT/parity-gate pattern
  (`src/models/enums.py`) reused for all new status columns; the `035`–`041`
  credential-refactor precedent as the dual-write template.
- **External:** Railway pre-deploy command support; Neon branch databases for
  rehearsal; no Meta/Google API changes required (token ciphertext is copied, not
  re-issued).
- **People:** a human ratifier for every production data backfill (the migration
  `044` precedent) and for each per-workspace cutover of Phase 4.

## Risks

| Risk | Phase | Mitigation |
|---|---|---|
| Dual-write drift goes unnoticed | 1–5 | Comparators are per-phase ACs with day-count gates, not best-effort logs |
| Legacy NULL-tenant rows have no owner | 1 | Quarantine + explicit resolution report (AC1.1); constraint lands last |
| Duplicate history rows block the unique index | 4 | #695 data fix ratified first (AC4.1) |
| Outbox dispatcher changes send timing/rate | 4 | Per-workspace cutover; Telegram rate-limit metrics watched (#716 context); rollback = flag |
| Long coexistence window stalls (strangler fatigue) | all | Each phase delivers standalone value (see triage tiers); exit conditions below cap the window |
| Worker/web deploy skew during flag flips | all | Flags read at request/tick time from DB (`workspace_settings`), not env, wherever feasible |
| Neon connection ceiling during backfills | 1,3,4 | Backfills batched with pool budget; run windows documented (#713 context) |

## Exit conditions

The epic is **done** when O1–O6 hold and:

1. The CI migration-replay + parity job has been green for a full release cycle with
   the legacy tables gone.
2. The reconciler has run for 30 days with zero manual interventions for the failure
   classes F1–F5 and F7–F9 from
   [02-self-evaluation.md](02-self-evaluation.md) §4.3.
3. The issue set mapped in [05-tiered-issue-triage.md](05-tiered-issue-triage.md)
   tiers P0–P2 is closed or explicitly re-triaged.

The epic is **abandoned/paused safely** if stopped after any phase gate: every phase
leaves the system strictly better (more constraints, fewer write paths) with
dual-write disabled by flag and no half-cutover capability.
