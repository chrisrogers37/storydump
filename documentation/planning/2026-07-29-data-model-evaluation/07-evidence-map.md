# Evidence Map

**Baseline:** `main` at `683f7cf` (2026-07-29). Every path below exists at that
commit; every issue number existed in the tracker at the snapshot date (open unless
noted). This map ties the package's material conclusions to their evidence; the
narrative versions live in [02-self-evaluation.md](02-self-evaluation.md).

Conventions: `M-NNN` = `scripts/migrations/NNN_*.sql`; `#NNN` = GitHub issue.

## 1. Tenancy

| Conclusion | Code | Migrations | Tests | Issues |
|---|---|---|---|---|
| The tenant is the Telegram chat's settings row; docstring still says Phase-1 single-deployment | `src/models/chat_settings.py` | M-006, M-014 | — | #576 |
| Tenant ownership columns are nullable on all core tables | `src/models/media_item.py`, `src/models/posting_queue.py`, `src/models/posting_history.py`, `src/models/media_lock.py`, `src/models/category_mix.py`, `src/models/api_token.py`, `src/models/audit_log.py` | M-014, M-015, M-018 (hard-coded prod UUID), M-044 (manual ratification) | — | #412, #669, #670 |
| Tenant filtering is a silent no-op when unset | `src/repositories/base_repository.py` (`_apply_tenant_filter`) | — | — | #576 |
| Cross-tenant read/scoping gaps recur per call site | `src/repositories/media_repository.py`, `src/services/core/scheduler.py` | — | — | #593, #594, #598, #599, #600, #601, #677, #575, #584, #667 |
| Chat-as-tenant leaks: phantom DM rows; tenant needed before a chat exists | `src/services/core/conversation_service.py`, `src/models/onboarding_session.py` | M-024 | — | #524 |
| No tenant on interactions/service telemetry; raw chat ids duplicated | `src/models/user_interaction.py`, `src/models/service_run.py`, `src/models/posting_queue.py` (`telegram_chat_id`) | M-003 | — | #415 |

## 2. Identity and membership

| Conclusion | Code | Migrations | Tests | Issues |
|---|---|---|---|---|
| Person identity is Telegram-only; global role vs per-chat role split | `src/models/user.py`, `src/models/user_chat_membership.py` | M-023 | — | #530, #585 |
| Web sessions weld to Telegram numbering (`activeChatId` in JWT) | `landing/src/lib/session.ts`, `landing/src/middleware.ts`, `landing/src/lib/auth.ts` | — | — | — (see `planning/multi-account-dashboard.md`) |
| Two onboarding state machines coexist | `src/models/onboarding_session.py`, `src/models/chat_settings.py` | M-016, M-023, M-027 | — | #650 |

## 3. Accounts and credentials

| Conclusion | Code | Migrations | Tests | Issues |
|---|---|---|---|---|
| Instagram accounts are global, selected per chat | `src/models/instagram_account.py`, `src/models/chat_settings.py` (`active_instagram_account_id`) | M-007, M-009 | — | #584, #546 |
| Credential identity/ownership split across account FK, chat FK, and duplicated Meta id | `src/models/api_token.py` | M-008, M-015, M-035–M-041 | — | #380, #595 |
| Nullable `auth_method` weakens the credential unique constraint | `src/models/api_token.py` | M-040 | — | #596 |
| Per-tenant Drive OAuth errors can fall back to the global service account | `src/services/integrations/` (Drive factory/OAuth) | — | — | #627 |
| OAuth start endpoints are unauthenticated; tenant binding rides the state token | `src/api/routes/oauth.py` | — | — | #513, #587 |
| Encryption/rotation machinery is sound and reusable | `src/utils/encryption.py`, `cli/commands/tokens.py` | M-032 | — | #590 |

## 4. Media

| Conclusion | Code | Migrations | Tests | Issues |
|---|---|---|---|---|
| One row conflates provider object, asset, editorial content, CDN temp state, provenance, counters | `src/models/media_item.py` | M-001, M-004, M-011, M-013, M-026, M-028 | — | #418, #420 |
| Hash dedup is app-level only; algorithm mismatch between sync and backfill | `src/services/core/media_sync.py`, `src/services/integrations/` (backfill) | — | — | #619, #695 (analogue), #604 |
| Locks: partial unique exists only in SQL; stale model comment | `src/models/media_lock.py` | M-021 | — | #641, #424, #419, #603 |

## 5. Publishing pipeline

| Conclusion | Code | Migrations | Tests | Issues |
|---|---|---|---|---|
| Queue is ephemeral (delete-on-terminal); history linked by soft, non-unique `queue_item_id` | `src/models/posting_queue.py`, `src/models/posting_history.py` | M-020 | — | #695, #551 |
| History idempotency is read-then-insert, not a constraint | `src/repositories/history_repository.py` (`create_idempotent`) | — | — | #551, #695 |
| Delivery-state machine + INV-1 CHECK are recent, sound hardening to extend | `src/models/enums.py`, `src/models/posting_queue.py` | M-042, M-043, M-045–M-049 | `tests/src/models/test_enum_ssot_parity.py` | #692, #680 |
| Claim-before-publish anchors IG publishes; crash still strands `publishing` rows | `src/services/core/telegram_autopost.py`, `src/services/core/scheduler.py` | M-043 | — | #549, #565, #711, #366 |
| Coordination state is process-local (op locks, in-flight markers, cancel flags) | `src/services/core/telegram_operation_state.py` | — | — | #578, #611, #612, #363 |
| One logical operation spans multiple commits; reapers compensate | `src/services/core/scheduler.py`, `src/services/core/telegram_notification.py`, `src/services/core/loops/queue_cleanup_loop.py` | — | — | #560, #680, #691, #571, #636 |
| Multi-repo atomicity relies on a commit→flush monkey-patch | `src/repositories/atomic_session.py` | — | — | #608, #630 |

## 6. Sessions and transactions

| Conclusion | Code | Migrations | Tests | Issues |
|---|---|---|---|---|
| Repositories open their own ContextVar sessions and commit themselves | `src/repositories/base_repository.py`, `src/config/database.py` | — | `tests/src/config/test_database.py` | #608, #630, #629, #631–#635 |
| A 30s loop commits idle transactions as hygiene | `src/services/core/loops/transaction_cleanup_loop.py` | — | — | #571, #633 |

## 7. Schema mechanism and operations

| Conclusion | Code | Migrations | Tests | Issues |
|---|---|---|---|---|
| 49 hand-numbered SQL files, manually applied; Alembic declared but unused | `scripts/migrations/`, `requirements.txt` | M-001–M-049 | — | #577, #638, #712 |
| Version tracking is best-effort (`schema_version`; two files skip it) | `scripts/migrations/010_add_verbose_notifications.sql`, `scripts/migrations/034_send_failure_tracking.sql` | M-001 | — | #638 |
| Tests/fresh installs use `create_all()`; SQL-only constraints missing there | `tests/conftest.py`, `scripts/init_db.py`, `src/config/database.py` | M-014, M-015, M-021 | `.github/workflows/ci.yml` | #639, #640, #641, #654, #655 |
| `setup_database.sql` is a stale Phase-1 snapshot | `scripts/setup_database.sql` | — | — | #411 |
| No deploy-time migration step; docs prescribe manual psql (and lag at `021`) | `railway.toml`, `Procfile`, `documentation/guides/deployment.md` | — | — | #712, #531 |
| Missed migrations crash the worker (`UndefinedColumn`) | `documentation/operations/worker-recovery.md` | — | — | #712 |

## 8. Configuration and read models

| Conclusion | Code | Migrations | Tests | Issues |
|---|---|---|---|---|
| NULL settings mean "fall back to env" — dual truth | `src/models/chat_settings.py`, `src/config/defaults.py`, `src/config/settings.py` | M-029, M-030, M-033 | — | #532, #322, #461 |
| Denormalized counters drift (media, user, scheduler cursor) | `src/models/media_item.py`, `src/models/user.py`, `src/models/chat_settings.py` (`last_post_sent_at`) | M-019 | — | #416 |
| Category mix lacks a single-current guarantee | `src/models/category_mix.py` | M-002 | — | #643 |

## 9. Surfaces and bypasses

| Conclusion | Code | Migrations | Tests | Issues |
|---|---|---|---|---|
| API mostly respects layering; three endpoints construct repositories directly | `src/api/routes/onboarding/dashboard.py`, `src/api/routes/onboarding/settings.py` | — | — | #314, #492 |
| CLI token/pool commands bypass the service layer | `cli/commands/tokens.py`, `cli/commands/media.py` | — | — | #314 (class), #610 (analogue) |
| Mini App and Next.js dashboard both consume the same API with different auth stacks | `src/api/static/onboarding/app.js`, `src/utils/webapp_auth.py`, `landing/src/app/api/`, `landing/src/lib/auth.ts` | — | — | #461 |
| Landing owns exactly one table on the shared database | `landing/src/lib/schema.ts`, `landing/drizzle.config.ts`, `scripts/migrations/NOTE_waitlist_table.md` | — | — | — |

## 10. Scaling constraints

| Conclusion | Code | Migrations | Tests | Issues |
|---|---|---|---|---|
| Single polling consumer; shared Telegram send budget | `src/main.py`, `src/services/core/telegram_service.py` | — | — | #715, #716, #409 |
| Shared 30-connection pool ceiling | `src/config/database.py`, `src/config/settings.py` | — | — | #713, #690 |
| Global (not per-account) IG rate-limit computation | `src/services/integrations/instagram_api.py` | — | — | #545 |
| Per-tick eligibility subqueries; multi-scan analytics; unbounded history | `src/repositories/media_repository.py`, `src/services/core/dashboard_*` (query classes), `src/models/posting_history.py` | — | — | #414, #413, #423, #422 |

## 11. Prior art this package builds on

| Artifact | Location | Relationship |
|---|---|---|
| 2026-07 full-system review (91 findings, 5 epics) | `documentation/planning/2026-07-system-review/triage-tracker.md`, `documentation/planning/2026-07-system-review/detailed-findings.md` | Confirmed and extended; epics #576–#579, #560 adopted as umbrellas |
| Data-model redesign plan (enum SSOT + delivery states) | issue #692; shipped as M-045–M-049 | Phase 4 extends it (self-evaluation §4.1.2) |
| Credential refactor (staged additive→dual-write→cutover) | `documentation/planning/2026-05-18-instagram-credential-refactor.md`; M-035–M-041 | The in-repo template for every stage in [06-migration-and-consumer-plan.md](06-migration-and-consumer-plan.md) |
| Session-isolation planning vs shipped ContextVar design | `documentation/planning/per-request-session-isolation.md`, `src/repositories/base_repository.py` | Background for the UoW work (#608, #630) |
| Web app migration plan / instance picker | `documentation/planning/web-app-migration-plan.md`, `planning/multi-account-dashboard.md` | Motivates workspace-first identity (C7) |

## Verification notes

- File existence for every path above was checked against the baseline tree; issue
  numbers were checked against the tracker snapshot (see
  [README.md](README.md) "Baseline Facts").
- Line-number citations were deliberately avoided in this package (they rot);
  where a specific construct is named (a function, a constraint, a docstring), it
  was read directly during this session.
