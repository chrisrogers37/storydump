> **⛔ SUPERSEDED** — part of the 2026-07-29 data-model package; the authoritative plan is [`../../planning/2026-08-02-consolidated-design-plan/`](../../planning/2026-08-02-consolidated-design-plan/README.md). See this directory's README for what survived. Archived 2026-09-02.

# Migration and Consumer Plan

**Baseline:** `main` at `683f7cf`.
**Target:** [03-recommended-target-model.md](03-recommended-target-model.md).
**Sequencing/gates:** [04-epic.md](04-epic.md).
This document is the playbook: how each migration stage works mechanically, and what
changes in every consumer of the data model. Rollback is defined for every stage.

---

## A. The stage machine

Every capability (tenancy, credentials, media, publishing, config) moves through the
same six stages. A capability never skips a stage; stages never run out of order for
a given capability; different capabilities may be at different stages.

### A1. Expand

New tables/columns land via Alembic, nullable/empty, invisible to all readers.
DDL is backward-compatible with currently deployed code by construction: additive
only, no renames, no type changes, no constraint tightening.
**Gate:** CI migration replay + ORM parity green; deploy applies DDL with zero
behavior change (error rates and tick latency flat).
**Rollback:** none needed — the structures are inert. (A down-migration exists for
hygiene but is not used on production; inert structures are left in place.)

### A2. Backfill

A deterministic, idempotent, batched script (same repo, run via a Railway one-off
job against the production database, rehearsed first on a Neon branch) copies legacy
rows into the new representation. Batches respect the connection-pool budget
(#713 context). Every backfill publishes a **reconciliation report** to
`documentation/updates/` (dated, per repo convention): row counts per table and per
workspace, orphan/NULL-owner enumerations with resolutions, and checksums over
natural keys. Production backfills require human ratification — the migration `044`
precedent.
**Gate:** report shows bijection (or enumerated, ratified exceptions).
**Rollback:** truncate-and-rerun is safe (idempotent, keyed writes); legacy data is
never modified in this stage.

### A3. Dual-write

Writes go to both representations **in the same Unit-of-Work transaction** at the
service seam — never DB triggers (invisible to code review, hard to flag off), never
best-effort second commits (a second commit that can fail independently is precisely
how silent drift between representations is created). A nightly comparator job
re-runs the reconciliation and publishes diffs as metrics.
**Gate:** 0 unexplained diffs for 14 consecutive days, including ≥1 deploy and ≥1
worker restart during the window.
**Rollback:** disable the dual-write flag; new-model tables go stale but nothing
reads them; re-entry re-runs the backfill delta.

### A4. Shadow-read

Read paths compute results from both representations for flagged workspaces; the
legacy result is served, the comparison is logged/counted. Applies to decisions, not
just rows: tenant resolution (AC1.3), media eligibility (AC3.3), ledger state
(AC4.2).
**Gate:** agreement threshold per capability (documented in the epic ACs) with every
disagreement dispositioned in writing.
**Rollback:** remove the shadow flag; zero user impact by design.

### A5. Cutover

The read path (and behavior that depends on it) flips to the new representation —
per capability, and for publishing per workspace (dry-run workspaces first, then the
operator's own workspace, then the rest). Flags live in `workspace_settings` (DB,
not env) so worker and web flip atomically and per-tenant.
**Gate:** error budget and the capability's business metrics (posts delivered,
approvals responded) flat across the flip; comparator still 0-diff since dual-write
continues.
**Rollback:** flip the flag back to legacy reads. Dual-write has kept legacy current,
so rollback is lossless and immediate. This is the primary recovery mechanism for
the entire program.

### A6. Contract

Only after burn-in with legacy reads off: constraints tighten (`NOT NULL`, FKs,
uniques), legacy writes stop (dual-write flag removed from code), legacy tables are
archived (logical dump retained) and dropped in a later release than the code that
stopped writing them.
**Gate:** AC6.1–AC6.3 in the epic.
**Rollback:** before the drop, re-enabling legacy writes is a revert of the
dual-write-removal commit plus a delta backfill *from* the new model (reverse
scripts are written and rehearsed as part of this stage). After the drop, recovery
is restore-from-archive — which is why the drop waits a full release cycle.

### Validation without posting (applies to every stage)

All gates are measured by read-only SQL, comparator jobs, and CI. Publishing-path
rehearsals use workspaces with `dry_run_mode` (which already short-circuits before
Instagram publish) and Neon branch databases. No stage's verification sends a
Telegram message or creates Instagram content; this repeats and honors the safety
rules in `CLAUDE.md`.

---

## B. Consumer-by-consumer plan

### C1. Python models and repositories (`src/models/`, `src/repositories/`)

- New SQLAlchemy models per target entity; existing models untouched until contract.
- `BaseRepository` (`src/repositories/base_repository.py`) gains a UoW-session mode:
  repositories accept an injected session and **never commit**; the ContextVar
  self-managed mode remains only for legacy paths and is deleted at contract
  (#608, #630). `atomic_session.py` is retired when its last caller moves.
- `_apply_tenant_filter`'s silent no-op is replaced in new repositories by a
  **required** `workspace_id` parameter (type-level, not runtime-optional); legacy
  repositories keep the old behavior until their consumers cut over.
- Enum SSOT (`src/models/enums.py`) extends to all new status vocabularies; the
  parity test pattern (`tests/src/models/test_enum_ssot_parity.py`) covers them.

### C2. Services and worker loops (`src/services/`, `src/main.py`)

- Services become the dual-write seam: `SettingsService`, `MembershipService`,
  `InstagramAccountService`, `MediaSyncService`, `SchedulerService`, and the
  Telegram callback/autopost services each gain a UoW-wrapped write path that
  touches both representations (Phase-matched, per epic).
- The scheduler's slot decision reads `workspace_settings` post-cutover; its cursor
  (`last_post_sent_at`) moves to a ledger-derived projection in Phase 5.
- The outbox dispatcher joins `src/main.py`'s loop set with a heartbeat like the
  existing loops; the four cleanup loops are absorbed by the reconciler (#571) with
  the loop-liveness conventions preserved (`get_loop_liveness` heartbeats, guarded
  restarts).
- Process-local operation state (`telegram_operation_state.py`) is deleted at Phase
  4 cutover; cancellation becomes an intent-status transition.

### C3. FastAPI (`src/api/`)

- `_validate_request`/membership checks resolve tenant through the single
  resolution seam (AC1.3): `chat_id` in, workspace out; request contracts do not
  change during dual-write.
- Post-cutover, endpoints accept/return `workspace_id` alongside `chat_id`
  (additive response fields first; `chat_id` request params retired only at
  contract, coordinated with the BFF — see C7).
- The three repository-bypassing endpoints (`upload-media`, `audit-log`,
  category-mix; #314) are routed through services in their capability's cutover PR.
- Rate limiting, initData/URL-token validation are unchanged; token payloads gain
  workspace claims only when C7 flips.

### C4. Telegram surface (`src/services/core/telegram_*`)

- Command/callback handlers resolve tenant via the same seam; `chat_id` remains the
  UX-level identifier forever (it is what Telegram gives us) — it just stops being
  the storage key.
- Approval cards: during Phase 4 dual-write, the card send stamps both
  `posting_queue` and `approval_requests`; the callback handlers finalize through
  the UoW so history/ledger/queue stay atomic. Card payload formats (`autopost:`,
  `posted:` etc.) are unchanged — in-flight cards across a deploy must keep working;
  callback data never embeds a representation-specific id (queue-item UUIDs persist
  as intent ids via a mapping column during coexistence).
- Onboarding wizard writes workspace onboarding state after Phase 1 cutover; deep
  links (`startgroup=setup_{session_id}`) keep their format.

### C5. CLI (`cli/`)

- Read-only commands (`list-queue`, `pool-health`, `check-health`, `sync-status`,
  `backfill-status`) grow dual views during shadow-read (legacy + ledger) so
  operators can see both representations while they coexist.
- Mutating commands adopt the UoW and the tenant seam; `revoke-tokens` and
  `rotate-keys` stop bypassing the service layer as part of Phase 2.
- New operator commands (all read-only or ratify-gated): `reconcile-report`,
  `backfill-workspaces --dry-run/--apply`, `release-stuck-attempt` (adopts #565).
  The safety split in `CLAUDE.md` is updated in the same PRs.

### C6. OAuth (`src/api/routes/oauth.py`, `src/services/integrations/`)

- State tokens carry `workspace_id` (Phase 2); callbacks write
  `integration_connections`/`credentials` + legacy `api_tokens` under dual-write.
- **Ciphertext is copied verbatim** — no re-encryption, no re-issuance, no user
  re-consent; `ENCRYPTION_KEY(S)` semantics unchanged, `rotate-keys` operates on
  whichever tables exist at its stage.
- Token refresh (`token_refresh.py`) reads the new tables at Phase 2 cutover;
  refresh writes are dual-written like everything else. Revocation semantics
  (`revoked_at`) carry over unchanged.
- `/auth/*/start` authorization hardening (#513) ships with this phase since the
  routes are already open for surgery.

### C7. Next.js / BFF / JWT types (`landing/`)

- JWT payload (`landing/src/lib/session.ts`): `activeChatId` is joined by
  `activeWorkspaceId` (additive claim). The BFF (`landing/src/app/api/`) forwards
  whichever the FastAPI contract expects for its stage; middleware
  (`landing/src/middleware.ts`) checks for either claim during coexistence and for
  the workspace claim after contract. Existing sessions (24h TTL) age out
  naturally — no forced logout: the BFF back-fills the workspace claim on first use
  via the resolution endpoint.
- TypeScript types for instances/dashboards gain `workspaceId`; the instance picker
  (`/api/instances`) returns both ids during coexistence.
- `generateUrlToken` (`landing/src/lib/auth.ts`) and its Python mirror
  (`src/utils/webapp_auth.py`) change **together in one deploy window** when the
  token payload gains the workspace claim — this is the one cross-repo-surface
  lockstep in the program, and it gets its own rehearsal.
- Drizzle/`waitlist_signups` untouched (non-goal N9).

### C8. Analytics

- Dashboard analytics (`DashboardService`) keep reading `posting_history` — which
  *is* the outcome ledger post-evolution — so most analytics migrate by column
  addition, not table swap.
- Counters consumed by dashboards (`times_posted`, `total_posts`) switch to ledger
  projections at Phase 5 with a documented tolerance and a rebuild command (AC5.2).
- Plausible/landing analytics are unaffected.

### C9. CI (`.github/workflows/ci.yml`, `tests/`)

- Phase 0 adds: a migration-replay job (clean Postgres → all Alembic revisions →
  metadata diff), and switches `tests/conftest.py` from `create_all()` to
  Alembic-produced schema so tests exercise real constraints and partial indexes
  (#654, #639; enables #655).
- Comparator jobs run in CI against fixtures as unit tests, and in production as
  scheduled jobs; both share the same comparison code.
- The changelog gate and ruff jobs are unchanged; test-suite flakiness work (#672)
  is a named risk for the new DB-heavy jobs.

### C10. Railway

- Pre-deploy migration step (`railway.toml` `releaseCommand` or equivalent) added in
  Phase 0 (#712), with the rule: **DDL deploys before code that needs it**, and both
  services deploy from the same commit.
- Backfills run as one-off jobs, not release commands (they need ratification and
  pacing).
- Env vars: any new setting is added to **both** worker and web services before the
  code that reads it deploys (documented trap in `CLAUDE.md`); the plan minimizes
  env usage by keeping flags in `workspace_settings`.

### C11. Neon

- Every production DDL/backfill is rehearsed on a Neon branch of current production
  data first — the existing convention recorded in `CHANGELOG.md` and in
  `scripts/migrations/048_backfill_queue_delivery_states.sql`; kept as a hard gate.
- Branch rehearsals double as restore tests for the contract-stage archives.
- Connection budget during backfills: batch sizes are sized against the pool
  ceiling (#713, #690); backfills run in the low-activity window observable from
  `posting_history` timestamps.

### C12. Operations (`documentation/operations/`, `documentation/guides/`)

- `deployment.md`/`cloud-deployment.md` migration sections rewrite to "Alembic at
  deploy"; the stale `psql -f` loop instructions (still referencing migrations
  `001`–`021`) are corrected in Phase 0 (#531 dedup can ride along).
- `worker-recovery.md` gains: outbox dispatcher recovery, reconciler runbook,
  cutover/rollback flag procedures per capability.
- Monitoring: comparator diffs, outbox lag, reconciler action counts, and
  constraint-violation alerts (a violation of a new unique is a caught incident, and
  pages) join the health checks.
- `CLAUDE.md` safe/unsafe command lists updated with each new CLI command.

### C13. Rollback (consolidated)

| Stage | Rollback action | Data loss |
|---|---|---|
| Expand | none needed (inert) | none |
| Backfill | rerun (idempotent) or truncate new tables | none (legacy untouched) |
| Dual-write | flag off; delta-backfill on re-entry | none |
| Shadow-read | flag off | none |
| Cutover | flag back to legacy reads (dual-write still on) | none — this is the designed recovery path |
| Contract (pre-drop) | revert commit + reverse delta-backfill (rehearsed) | none if executed before drop |
| Contract (post-drop) | restore archived dump | bounded by archive point; why drops trail by a release |

Destructive down-migrations are never the recovery mechanism; they exist only for
development hygiene. Any rollback that would affect live posting behavior is
executed with the affected workspaces' scheduling paused via the existing
`is_paused` mechanism — an operator action that stops posting, never starts it.
