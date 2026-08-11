# Self-Evaluation: Storydump's System and Data Model

**Baseline:** `main` at `683f7cf` (2026-07-29).
**Method:** This document answers
[01-fable-system-evaluation-prompt.md](01-fable-system-evaluation-prompt.md) using only
repository evidence. Claims are tagged **Observed** / **Inferred** / **Recommended**.
File-by-file citations are consolidated in [07-evidence-map.md](07-evidence-map.md).

---

## Executive summary

Storydump is a working, revenue-adjacent product: a Telegram-operated Instagram Story
scheduler with a JIT scheduler, human approval cards, optional Graph API auto-posting,
Google Drive media sync, a FastAPI Mini App, and a Next.js dashboard — all sharing one
Neon PostgreSQL database (Observed, Part 1). The data model that supports it grew
column-by-column from a single-deployment Phase 1 design into a de facto multi-tenant
system: the Telegram chat settings row *is* the tenant, tenant ownership columns are
nullable on every core table, repositories own their own sessions and commits, and the
schema is maintained by 49 hand-applied SQL files that CI never executes (Observed,
Part 2). The system's known failure classes — duplicate posts, orphaned queue rows,
cross-tenant reads, prod/test schema drift — map directly onto those structural facts
(Part 4), and most are already individually tracked in GitHub issues.

After comparing an in-place repair, an evolutionary redesign, and a rewrite (Part 5),
this session recommends the evolutionary redesign: a workspace-rooted domain model
reached by a strangler migration, with the in-place repair's highest-value items
executed first as its enabling tier (Part 6). The recommendation is developed in
[03-recommended-target-model.md](03-recommended-target-model.md) and its delivery plan
in [04-epic.md](04-epic.md)–[06-migration-and-consumer-plan.md](06-migration-and-consumer-plan.md).

---

## Part 1 — The system as built

### 1.1 Product purpose (Observed)

Storydump automates Instagram Story posting for small teams. Media flows from a source
(local folder or Google Drive) into an indexed pool (`media_items`); a just-in-time
scheduler decides when a slot is due per chat, picks a category by weighted mix and a
media item by eligibility rules, creates a `posting_queue` row, and sends an approval
card to a Telegram group. A human taps **Posted** (manual flow), **Auto Post**
(Instagram Graph API flow), **Skip**, or **Reject**; the terminal outcome is recorded
in `posting_history` and the queue row is deleted. The phased philosophy (manual first,
API optional, web UI later) is documented in `CLAUDE.md` and
`documentation/ROADMAP.md`.

### 1.2 Actors (Observed)

| Actor | Surface | Authentication |
|---|---|---|
| Team member | Telegram group chat (bot commands, approval buttons) | Telegram identity; membership rows in `user_chat_memberships`; callback authorization in `src/services/core/telegram_service.py` |
| Team member | Telegram Mini App / browser dashboard (`src/api/static/onboarding/`) | Telegram WebApp `initData` HMAC or signed URL token (`src/utils/webapp_auth.py`), then an active-membership check per request (`src/api/routes/onboarding/helpers.py`) |
| Team member | Next.js dashboard (`landing/`) | Telegram Login Widget → HS256 JWT cookie `storydump_session` (`landing/src/lib/session.ts`); BFF proxies to FastAPI with a regenerated URL token (`landing/src/lib/auth.ts`) |
| Operator | CLI (`cli/main.py`) | Environment access only; commands are mostly tenant-unscoped |
| Background worker | `python -m src.main` | Direct DB access; no per-tenant identity |
| External services | Meta Graph API (two OAuth variants + manual tokens), Google Drive (OAuth + service account), Cloudinary, Telegram Bot API, Plausible | OAuth tokens encrypted with Fernet into `api_tokens` (`src/utils/encryption.py`) |

### 1.3 Tenant boundary (Observed, with Inferred assessment)

The tenant is a row in `chat_settings`, keyed by a unique `telegram_chat_id`
(`src/models/chat_settings.py`). The model docstring still says "For Phase 1, there
will be one record per deployment. Phase 3 introduces true multi-tenancy" — the code
has since moved to one record per Telegram group, so the Telegram chat *is* the tenant.

Enforcement is opt-in, not structural:

- `chat_settings_id` is **nullable** on `media_items`, `posting_queue`,
  `posting_history`, `media_posting_locks`, `category_post_case_mix`, `api_tokens`,
  and `audit_log` (migration `scripts/migrations/014_multi_tenant_chat_settings_fk.sql`
  and successors; issue #412).
- The shared tenant filter is a silent no-op when no tenant is passed:
  `BaseRepository._apply_tenant_filter` in `src/repositories/base_repository.py`
  returns the unfiltered query when `chat_settings_id` is `None`.
- `users` and `instagram_accounts` are global (no tenant column);
  `user_interactions` carries only a raw `telegram_chat_id`; `service_runs` has no
  tenant at all.
- Epic #576 ("Multi-tenant isolation is opt-in, not enforced") and the recurring
  family of scoping bugs (#593, #594, #598, #599, #600, #601, #677, #575) document
  the consequences; `CHANGELOG.md` records several already-fixed members of the same
  class.

**Inferred:** the tenant boundary is real in intent but is enforced per-call-site; any
new query starts unsafe by default.

### 1.4 Workflows (Observed)

- **Scheduling:** JIT — no pre-materialized schedule. `chat_settings` holds
  `posts_per_day`, posting hours, timezone, and the scheduler cursor
  `last_post_sent_at`; `SchedulerService.process_slot`
  (`src/services/core/scheduler.py`) decides "is a slot due now?" each 60s tick.
- **Approval:** queue row status machine `pending → processing → {delivered,
  sent_unconfirmed, failed, publishing}` with terminal outcomes recorded in
  `posting_history` and the queue row deleted (`src/models/enums.py`,
  `src/models/posting_queue.py`). `delivered` requires a stamped
  `telegram_message_id` (CHECK `check_delivered_stamped`, migration `049`).
- **Auto-posting:** claim → in-process operation lock → Cloudinary upload →
  `publishing` + `instagram_container_id` persisted **before** the publish call
  (claim-before-publish, migration `043`) → publish → atomic finalize
  (`src/services/core/telegram_autopost.py`).
- **Media ingestion:** `MediaSyncService` (`src/services/core/media_sync.py`) syncs
  per-chat sources every ~300s; identity by content hash; deactivation of missing
  files; Cloudinary used only as temporary egress for Instagram.
- **Onboarding:** two coexisting state machines — `onboarding_sessions` (DM wizard:
  `naming → awaiting_group → complete`) and `chat_settings.onboarding_step` +
  `onboarding_completed` (per-chat setup progress);
  `src/models/onboarding_session.py` documents the split.
- **Account connection:** three Instagram credential flows (Facebook Login OAuth,
  Instagram Login OAuth, manual token) plus Google Drive OAuth and service-account
  connection, all landing in `api_tokens` (`src/api/routes/oauth.py`,
  `src/services/integrations/`).

### 1.5 Runtime topology (Observed)

Two Railway services from one repo (`Procfile`): a **worker** (`python -m src.main`:
scheduler tick 60s, Telegram polling, lock/queue/cloud cleanup hourly, media sync
~300s, transaction cleanup 30s, raw-socket health server) and a **web** service
(`uvicorn src.api.app:app`: OAuth, Mini App, ~40 `/api/onboarding/*` endpoints). The
Next.js app deploys separately to Vercel and reaches the same Neon database directly
for exactly one table (`waitlist_signups`, `landing/src/lib/schema.ts`) and reaches
everything else through the FastAPI API. There is no queue broker, no cache tier; all
coordination happens in PostgreSQL or in process memory.

### 1.6 Operational constraints (Observed)

- Schema changes are applied to production **manually** with `psql` per
  `documentation/guides/deployment.md`; `railway.toml` has no release/migration phase
  (issue #712). Applied versions are tracked in a SQL-only `schema_version` table;
  two migrations (`010`, `034`) never insert their version row.
- CI (`.github/workflows/ci.yml`) runs ruff, pytest against a Postgres 15 service
  container whose schema comes from `Base.metadata.create_all()`
  (`tests/conftest.py`) — migration files are never executed in CI (issues #639,
  #654). `scripts/setup_database.sql` is a stale Phase-1 snapshot (issue #411).
- Rollback of application code is a Railway redeploy; rollback of schema has no
  defined mechanism.

---

## Part 2 — As-built data model inventory

### 2.1 Tables (Observed)

Fifteen SQLAlchemy models (`src/models/`), plus `schema_version` (SQL-only) and the
Drizzle-owned `waitlist_signups`:

| Table | Purpose | Tenant column |
|---|---|---|
| `chat_settings` | Tenant root + Telegram binding + ~25 config columns + onboarding state + scheduler cursor + alert state | is the tenant |
| `users` | Global Telegram identity + global `role` + denormalized `total_posts` | none |
| `user_chat_memberships` | Person↔tenant link with `instance_role` | NOT NULL (only table where it is) |
| `instagram_accounts` | Instagram profile identity, globally unique `instagram_account_id` | none |
| `api_tokens` | Encrypted OAuth/manual credentials for Instagram + Google Drive | nullable |
| `media_items` | Provider object + durable asset + editorial content + Cloudinary temp state + backfill provenance + counters | nullable |
| `media_posting_locks` | TTL/permanent posting locks | nullable |
| `posting_queue` | Ephemeral active work items (deleted on terminal outcome) | nullable |
| `posting_history` | Terminal outcomes; soft `queue_item_id` link (no FK, no unique) | nullable |
| `category_post_case_mix` | Type-2 SCD category ratios | nullable |
| `onboarding_sessions` | DM onboarding wizard state | via `pending_chat_settings_id` |
| `audit_log` | Field-level change audit for settings/membership/locks | nullable |
| `user_interactions` | Command/callback telemetry | raw `telegram_chat_id` only |
| `service_runs` | Execution audit + durable periodic-task markers | none |
| `schema_version` | Applied-migration tracking (no ORM model) | — |
| `waitlist_signups` | Landing waitlist (Drizzle, same database) | — |

### 2.2 Source of truth per concept (Observed → Inferred)

| Concept | Source of truth | Competing/duplicated representations |
|---|---|---|
| Tenant | `chat_settings.id` | Natural key `telegram_chat_id` used directly by `posting_queue.telegram_chat_id`, `user_interactions.telegram_chat_id` |
| Person | `users` | Global `users.role` vs per-tenant `user_chat_memberships.instance_role`; `posting_history.posted_by_telegram_username` snapshot |
| Social account | `instagram_accounts` | `api_tokens.meta_account_id` duplicates the Meta ID (migrations `035`–`041`); per-tenant selection lives on `chat_settings.active_instagram_account_id` |
| Credential | `api_tokens` (Fernet-encrypted) | Ownership split: Instagram tokens keyed by account FK (global), Drive tokens by `chat_settings_id`; nullable `auth_method` weakens the unique constraint (#596) |
| Media | `media_items` | One row conflates provider object (`file_path`, `source_identifier`), durable asset (`file_hash`), editorial content (`caption`, `category`, `tags`), Cloudinary temp state, backfill provenance, and counters |
| Posting outcome | `posting_history` | Denormalized `media_items.times_posted`/`last_posted_at`, `users.total_posts`, `chat_settings.last_post_sent_at` |
| Configuration | `chat_settings` columns | `NULL` means "fall back to env" for timezone, TTLs, caption style, media source, lifecycle notifications (`src/config/defaults.py`; issues #532, #322, #461) |
| Audit | `audit_log` | Overlaps `user_interactions` and `service_runs` as three write-mostly observability tables (#415) |
| Schema | 49 SQL migrations | vs `Base.metadata.create_all()` vs stale `scripts/setup_database.sql` (#411, #639) |

### 2.3 Where the schema enforces correctness (Observed)

Enforced structurally: PKs/UUIDs everywhere; unique `telegram_chat_id`,
`telegram_user_id`, `instagram_account_id`; enum-derived CHECKs with a CI parity gate
(`src/models/enums.py`, `tests/src/models/test_enum_ssot_parity.py`); INV-1
`check_delivered_stamped`; partial uniques for legacy-NULL media paths, permanent
locks, and Drive tokens — but the partial uniques exist **only in SQL migrations**,
not in ORM `__table_args__` (#641), so test databases don't have them.

Left to application discipline: tenant scoping (nullable FKs + optional filter);
history idempotency (`HistoryRepository.create_idempotent` is read-then-insert with no
unique index — #695, #551); one-active-queue-row-per-media (no unique — #604);
single-current category mix row (#643); counter consistency (#416); FK `ON DELETE`
behavior for `users` references (#417).

### 2.4 Schema production mechanism (Observed)

Three parallel mechanisms produce schemas: hand-applied SQL migrations (production),
`create_all()` (tests, `scripts/init_db.py` fresh installs), and the stale
`setup_database.sql`. No single mechanism produces all three; the drift is tracked as
epic #577 and issues #638–#641, #654. Alembic is declared in `requirements.txt` but
unused (#638).

---

## Part 3 — Path traces

### 3.1 Scheduled slot → Telegram card → terminal outcome (Observed)

1. Tick (`src/main.py` → `SchedulerService.process_slot`,
   `src/services/core/scheduler.py`): stale-row cleanup, slot-due check against
   `last_post_sent_at`, weighted category pick, eligibility query
   (`MediaRepository.get_next_eligible_for_posting`).
2. `QueueRepository.create` — **commit 1** (row `pending`).
3. Claim: status → `processing` — **commit 2** — *before* the Telegram send
   (`claim_for_processing` uses `FOR UPDATE SKIP LOCKED` for button paths).
4. Telegram send (`src/services/core/telegram_notification.py`); on success the
   message id is stamped and status → `delivered` — **commit 3**; on ambiguous
   timeout → `sent_unconfirmed` (no resend); then `last_post_sent_at` advances —
   **commit 4**.
5. Human button press → `claim_for_processing` → atomic finalize via
   `atomic_session` (`src/repositories/atomic_session.py`): history insert
   (`create_idempotent`), media counters, lock creation, queue delete — one commit.

Every inter-commit gap is a crash window that leaves partial state; the 10-minute and
24-hour reapers (`src/services/core/loops/queue_cleanup_loop.py`, scheduler sweeps)
are the compensation mechanism. Issues #680, #691, #366, #363 document the residual
gaps.

### 3.2 Instagram auto-post (Observed)

Button `autopost:{qid}` → process-local asyncio operation lock for claim + in-flight
marker + task spawn (`src/services/core/telegram_operation_state.py`) → Cloudinary
upload → `mark_publishing` + `instagram_container_id` — **commit before publish** —
→ Graph API publish → atomic finalize. A crash after publish but before finalize
leaves a `publishing` row that blocks re-serving (no duplicate story) but may lack a
history record (#549 residual, #565). The operation lock and cancel flags are
process-local, so a restart forgets them (#578, #611, #612); a DB-level claim guard is
proposed in #711.

### 3.3 Media ingestion (Observed)

Local or Drive provider (`src/services/media_sources/`) → `MediaSyncService` hash-based
sync → `media_items` upsert with category from folder structure. Dedup is by
`file_hash` in application queries only; there is no hash unique constraint, and the
locked-hash eligibility subquery has known cross-tenant scoping gaps (#593, #594).

### 3.4 OAuth connection (Observed)

`GET /auth/{provider}/start?chat_id=N` — unauthenticated; tenant binding rides in an
encrypted state token (#513) → provider consent → callback validates state → token
exchange → Fernet encryption (`ENCRYPTION_KEY`/`ENCRYPTION_KEYS`,
`src/utils/encryption.py`) → `api_tokens` upsert; Instagram flows also upsert
`instagram_accounts` and may flip `chat_settings.active_instagram_account_id`.
Refresh: `src/services/integrations/token_refresh.py` (24h cadence). Revocation:
`revoked_at` plus CLI `revoke-tokens` (which queries `ApiToken` directly, bypassing
the service layer). Known ownership defects: token upsert keyed without
`auth_method` can clobber the wrong credential (#595); a per-tenant Drive OAuth error
can fall back to the global service account (#627).

### 3.5 Read paths per surface (Observed)

- **Telegram bot:** tenant = message/callback chat id; reads via services.
- **FastAPI:** tenant = client-supplied `chat_id` validated by membership; most
  routes go through services, but `upload-media`, `audit-log`, and the category-mix
  endpoints construct repositories directly (#314); `GET /accounts` lists globally
  and marks the active one (#584).
- **Next.js dashboard:** JWT `activeChatId` → BFF (`landing/src/app/api/dashboard/`)
  → FastAPI with a regenerated URL token; FastAPI re-checks membership.
- **CLI:** direct service/repo access, largely tenant-unscoped; `revoke-tokens`,
  `rotate-keys`, `pool-health` bypass services.

---

## Part 4 — Evaluation

### 4.1 Strengths to preserve (Observed → Inferred)

1. **Layered architecture is real.** CLI/API → services → repositories → models is
   followed in the overwhelming majority of paths; the bypasses are enumerable (#314).
2. **The queue state machine is converging on correctness.** Enum SSOT with a CI
   parity gate, `sent_unconfirmed`/`delivered` states, INV-1 CHECK, and
   claim-before-publish are recent, well-reasoned hardening (issue #692's plan,
   migrations `043`–`049`). The redesign should extend this trajectory, not restart it.
3. **Credential hygiene.** Fernet encryption, key rotation, revocation timestamps,
   and the `035`–`041` additive→dual-write→cutover credential migration prove the
   team can execute a staged migration safely.
4. **Real-Postgres tests** (116 test files, per-test transaction rollback) — the gap
   is schema provenance, not engine fidelity.
5. **JIT scheduling** avoids pre-materialized schedule tables and their consistency
   problems.
6. **Operational self-awareness:** the 2026-07 system review
   (`documentation/planning/2026-07-system-review/`) already triaged 91 findings into
   epics; this evaluation confirms rather than contradicts it.

### 4.2 Structural liabilities (Observed, each with evidence)

1. **The tenant is a config row for a chat.** `chat_settings` conflates tenant
   identity, Telegram binding, ~25 configuration values with env fallbacks,
   onboarding state, scheduler cursor, and alert state. Phantom DM rows had to be
   deleted by migration `024` and still get created (#524).
2. **Tenant ownership is nullable and filtering optional** (#412, #576, #669, #670;
   `_apply_tenant_filter` no-op).
3. **Identity is Telegram-only.** `users` is a Telegram identity table; there is no
   person/identity separation, and the landing JWT (`userId` = internal UUID,
   `activeChatId` = Telegram chat id) welds web sessions to Telegram numbering
   (`landing/src/lib/session.ts`).
4. **`instagram_accounts` is global**, not workspace-owned; tenancy hangs off a
   selection pointer (`active_instagram_account_id`) and token rows.
5. **`media_items` is five concepts in one table** (provider object, asset,
   editorial content, CDN temp state, counters) — every lifecycle change (rename,
   re-sync, backfill, Cloudinary expiry) mutates the same row (#418, #420).
6. **Publishing state is split across an ephemeral row and an append log** with a
   soft, non-unique link (`posting_history.queue_item_id`; #695 found 6 duplicate
   groups in production; #551).
7. **External side effects are not durably attempted.** Telegram sends and IG
   publishes happen between commits with process-local coordination (#578, #611,
   #606, #607); reconciliation exists but is a patchwork of reapers.
8. **Repositories own sessions and commits**; multi-write atomicity requires the
   `atomic_session` monkey-patch that swaps `session.commit` for `session.flush`
   (`src/repositories/atomic_session.py`; epic-level issues #608, #630).
9. **Three schema mechanisms, none authoritative** (#577, #638–#641, #654, #411,
   #712); migration `018` contains a hard-coded production UUID; `044` requires
   manual ratification; `010`/`034` skip `schema_version`.
10. **Config truth is split between DB and env** (#532, #322, #461).
11. **Denormalized counters drift** (`times_posted`, `total_posts`,
    `last_post_sent_at`; #416).

### 4.3 Failure modes (Observed where issue-tracked; otherwise Inferred)

| # | Sequence | Consequence | Evidence |
|---|---|---|---|
| F1 | Crash between IG publish and finalize | Story live, no history; row wedged in `publishing` | #549, #565, `posting_queue.py` comments |
| F2 | Concurrent double-claim (re-entrant `claim_for_processing` + unguarded `mark_publishing`) | Double Instagram post | #711 |
| F3 | Replayed finalize races `create_idempotent` | Duplicate history rows; inflated caps/analytics | #695, #551 |
| F4 | Worker restart mid-autopost | Op locks/cancel flags lost; DB claim persists | #578, #611, #612, #363, #366 |
| F5 | Query written without tenant filter or row without tenant stamp | Cross-tenant read/write | #576, #593, #594, #598–#601, #677, #575 |
| F6 | Deploy merged but migration not applied | `UndefinedColumn` crash loop | `documentation/operations/worker-recovery.md`, #712 |
| F7 | Constraint exists only in SQL (or only in ORM) | Tests pass, production diverges (or vice versa) | #639, #641, #654 |
| F8 | Token upsert without `auth_method` in key; NULL `auth_method` in unique | Wrong credential clobbered; duplicate credentials | #595, #596 |
| F9 | Read-modify-write on counters/mix without row locks | Drift, multiple "current" mix rows | #416, #643 |
| F10 | Ambiguous Telegram delivery | Card exists without stamp until heal | `sent_unconfirmed` design, #680 |

### 4.4 Scaling constraints (Observed → Inferred)

First to break as tenants/volume grow: the single `getUpdates` consumer and shared
30/s Telegram budget (#715, #716); the 30-connection pool ceiling shared by all
tenants (#713, #690); global (not per-account) Instagram rate-limit computation
(#545); per-tick eligibility subqueries and 5-scan dashboard analytics (#414, #413);
unbounded `posting_history` (#423); and — decisively — the process-local coordination
state that forbids running a second worker at all (#578).

---

## Part 5 — Comparison of target approaches

### Criteria

C1 integrity & tenant isolation enforced structurally; C2 idempotency of external
side effects; C3 operability of schema change (one mechanism, CI-exercised,
deploy-applied); C4 evolvability toward the roadmap (multi-account, insights #666,
billing #661–#665, multi-platform #186); C5 migration risk to the running system;
C6 implementation cost. Scores 1 (poor) – 5 (strong).

### Approach A — Incremental repair in place

Keep the current shape. Execute: Alembic adoption + schema-parity tests + deploy-time
migrations (#638, #654, #712, #639–#641); stamp and backfill tenant columns, then
`NOT NULL` (#669, #670, #412); DB-level idempotency guards (#695, #604, #711);
Unit-of-Work replacing per-repo commits (#608, #630); durable claim state replacing
process-local locks (#611, #578).

- **Fixes:** F2, F3, F5 (partially), F6, F7, F8, F9; most of C1–C3.
- **Deliberately does not fix:** chat-as-tenant conflation (L1, L3), global social
  accounts (L4), the media grab-bag (L5), ephemeral-queue/append-log split (L6),
  config duality (L10). Each future feature (billing per tenant, insights per
  account, web-first identity) keeps paying the conflation tax.
- **Risk:** low per step; the end state is the current model, hardened.
- Scores: C1 4, C2 4, C3 5, C4 2, C5 5, C6 4.

### Approach B — Evolutionary redesign (workspace-rooted model, strangler migration)

Introduce a clean domain model *alongside* the current one — a first-class workspace
as tenant root with the Telegram chat as one external binding; person / external
identity / membership separation; workspace-owned integration connections, social
accounts, and credentials; media split into source / provider object / asset /
editorial content; publishing split into intent / approval delivery / platform
attempt / terminal outcome / audit events; a transactional outbox for external side
effects; Unit-of-Work transaction boundaries; one migration system. Migrate by
expand → backfill → dual-write → shadow-read → cutover → contract, per capability.
Approach A's tooling/constraint items are the enabling first tier, not an
alternative.

- **Fixes:** all of A, plus L1, L3–L6, L10 over time.
- **Deliberately does not fix:** Telegram single-bot topology (#715), horizontal
  worker scaling beyond what durable claims give (#578 is mitigated, not fully
  solved), UI architecture duality (Mini App vs Next.js).
- **Risk:** medium — long coexistence window, dual-write discipline, sustained
  focus; mitigated by per-capability cutover and the team's proven `035`–`041`
  pattern.
- Scores: C1 5, C2 5, C3 5, C4 5, C5 3, C6 2.

### Approach C — Greenfield rewrite and cutover

Build the target schema and a new service; run the old system until a one-shot (or
short-window) data migration flips traffic.

- **Fixes:** everything, in theory, with no legacy compromises in the target.
- **Rejected because:** the product is live and Telegram-operated with production
  data defects (NULL tenants, duplicate history) that must be reconciled *anyway*;
  a rewrite freezes the 265-issue backlog; a one-shot cutover concentrates all
  migration risk (OAuth credential re-encryption, live queue state, in-flight
  Telegram cards) into one irreversible event; team capacity evidence (solo/small
  maintainer cadence in `CHANGELOG.md`) does not support parallel systems.
- Scores: C1 5, C2 5, C3 5, C4 5, C5 1, C6 1.

A fourth variant — event-sourcing the posting pipeline — was considered and folded
into B's design space; a full event-sourced store is over-engineered for this team
size, but B adopts its useful core (append-only attempts/outcomes + outbox) without
making events the source of truth for everything.

---

## Part 6 — Recommendation

**Recommended: Approach B**, with Approach A's mechanisms executed first as B's
enabling tier (they are prerequisites, and they de-risk B's every subsequent step).

This session was asked to treat the workspace-rooted strangler design as a hypothesis.
Verdict against the evidence:

- **Supported.** Chat-as-tenant is already leaking (phantom DM rows: migration `024`,
  #524; web dashboard needs instance selection independent of any chat:
  `landing/src/app/(dashboard)/`, `planning/multi-account-dashboard.md`; onboarding
  needs a tenant before a chat exists: `onboarding_sessions.pending_chat_settings_id`).
  Credential/account ownership is already half-migrated toward explicit ownership
  (migrations `035`–`041`). The queue work (#692) is already inventing the
  intent/attempt/outcome distinctions ad hoc. Billing epics (#661–#665) need a tenant
  that is not a Telegram chat id.
- **Modified by evidence.** (1) The publishing redesign must *extend* the shipped
  delivery-state machine rather than replace it — cutover happens behind the existing
  statuses. (2) `users` global identity is worth keeping as the person spine; the
  redesign adds external-identity and membership structure around it instead of
  replacing it. (3) A single Telegram bot token means workspace↔chat binding stays
  effectively 1:1 for now; the binding table must not pretend otherwise in v1.
- **One-way doors:** the workspace identifier scheme (UUIDs, exposed in JWTs and
  URLs), the decision that `posting_history` remains append-only and becomes the
  outcome ledger, and the choice of Alembic as the single migration system. Everything
  else is staged and reversible.

Non-goals are enumerated in
[03-recommended-target-model.md](03-recommended-target-model.md) §6.

---

## Part 7 — Migration and validation strategy (summary)

The full consumer-by-consumer plan is
[06-migration-and-consumer-plan.md](06-migration-and-consumer-plan.md). In brief:

0. **Prerequisites:** Alembic owns the schema; CI replays migrations and asserts
   parity against ORM metadata; Railway applies migrations at deploy; reconciliation
   and drift dashboards exist before any new table.
1. **Expand:** new tables created empty alongside legacy; no reads.
2. **Backfill:** deterministic scripts, idempotent, with published reconciliation
   reports (row counts, orphan lists, checksum comparisons).
3. **Dual-write:** at service seams inside the Unit-of-Work transaction — never as
   triggers callers can miss silently, never as best-effort second commits.
4. **Shadow-read:** new-model reads computed alongside legacy reads, compared, and
   logged; discrepancies drive fixes, not cutover delays hidden as prose.
5. **Cutover:** per bounded capability (identity, credentials, media, publishing,
   config) and, where sensible, per workspace, behind reversible flags.
6. **Contract:** stronger constraints (`NOT NULL`, FKs, uniques) only after data and
   callers comply; legacy reads retired, then writes, then tables after burn-in.

**Rollback** at every stage means flipping reads/behavior back to legacy while
dual-write keeps both representations current. Destructive down-migrations are not
the recovery mechanism. **Validation never posts:** all gates are measured with
read-only queries, dry-run modes, and test tenants with `dry_run_mode` enabled —
consistent with the repository's live-posting safety rules in `CLAUDE.md`.
