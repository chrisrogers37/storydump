# Full-System Review — Detailed Findings (per subsystem)

**Created:** 2026-07-02
**Companion to:** [`triage-tracker.md`](triage-tracker.md) (consolidated, de-duplicated backlog)

This document preserves the **raw, per-subsystem analysis** from the system
review, before consolidation into the `TD-NNN` tracker. It is intentionally more
verbose and grouped by file so that when someone picks up a specific module they
can see every observation about it in one place. Line numbers reference `main`
at commit `7c99a34`. Severities: High / Med / Low. Categories: BUG · TECH DEBT ·
OVER-COMPLICATION · ARCHITECTURE · SECURITY · ENHANCEMENT · MISSING TESTS.

> Where a finding maps to a tracker item, the `TD-NNN` id is noted. Some raw
> findings were merged into a single tracker item (e.g. all "double `query.answer()`"
> instances → TD-033).

---

## 1. Scheduler, posting & background loops

### `src/services/core/scheduler.py`
- **878–879, 312, 792** — Multi-tenant media/category selection not scoped (High, BUG/MT → TD-031). `_select_media_from_pool()` / `_pick_category_for_slot()` never pass `chat_settings_id`.
- **217–234** — `get_queue_preview()` ignores `telegram_chat_id` (High → TD-031).
- **271–277** — `check_availability()` not tenant-scoped (Med → TD-031).
- **145–146** — Global queue hygiene (`delete_stale_pending`/`requeue_stale_processing`) runs unfiltered in a per-tenant tick (Med).
- **312–363** — TOCTOU between select and queue insert; no DB uniqueness on `media_item_id` (Med → TD-036).
- **370–374 vs 459–461** — Failed sends block media for up to 24h (Med → TD-042).
- **307–324** — `no_eligible_media` still opens a service_run (log/DB noise) (Med).
- **575–578** — Instagram auto-approve failure advances `last_post_sent_at` but records no post → slot lost (Med → TD-043).
- **65, 98** — No guard for `posts_per_day == 0` → `ZeroDivisionError` (Med → TD-037).
- **396–397** — Class-level `_consecutive_send_failures` shared across instances (Med → TD-041).
- **690–695** — Video detection uses local `file_path` only → cloud video mis-posted as IMAGE (Med → TD-044).
- **156–170, 202–215** — Daily-cap check non-atomic (Med → TD-037).
- **422–424** — No null guard on injected `telegram_service` (Med).
- **302–305** — Core scheduler imports `session_state` from `loops.lifecycle` (ARCH → TD-054).
- **611–613** — `MediaLockService()` constructed inline instead of injected (ARCH → TD-054).
- **31** — Unused `SCHEDULE_JITTER_MINUTES` (Low → TD-088).
- **800–834** — Dead allocation helpers `_allocate_slots_to_categories` / `_summarize_allocation` (Low → TD-088).
- **154, 200, 537** — Repeated lazy imports of `can_post_today` (TECH DEBT).
- Missing tests: no assertions that `chat_settings_id` flows through selection/preview; no `posts_per_day=0` test.

### `src/services/core/posting.py`
- **55–89 vs 24** — Builds a raw `telegram.Bot` for alerts despite having `TelegramService` (ARCH → TD-055).
- **92–94** — Alert dedup (`gdrive_alerted_at`) set only after successful send → spam during Telegram outages (Med → TD-055).

### `src/services/core/daily_cap.py`
- **21–26** — Cap counts only `posted` history; failed/in-flight not counted (Med → TD-037).
- **21** — `posts_per_day == 0` always blocks with no validation error (Med → TD-037).
- **16–28** — TOCTOU between count and post (Med → TD-037).

### `loops/scheduler_loop.py`
- **114–272** — Raw `application.bot.send_message` for pool/token/auto-approve/refresh alerts (ARCH → TD-055/TD-053).
- **315–317, 57–61** — Standalone `QueueRepository` not included in per-tick `cleanup_transactions()` (Med → TD-077).
- **355–368** — Onboarding `ConversationService.cleanup_expired()` embedded in scheduler loop, keyed off `retention_tick_counter` (TECH DEBT → TD-057).
- **108–124** — Auto-approve notification errors swallowed (`except Exception: pass`) (Med).
- **80–83** — Throttle state on a function object (`_scheduler_tick._no_active_ticks`) (Low → TD-041).
- Missing tests for `_pool_health_tick`, `_token_health_tick`, `_token_refresh_tick`.

### `loops/guarded.py`
- **65–80** — Exhausted restart budget stops the loop silently (Med → TD-084).
- **121–124** — Crash alert calls `bot.send_message` with possibly-`None` `ADMIN_TELEGRAM_CHAT_ID` (Low → TD-055).

### `loops/heartbeat.py`
- **41–46** — Never-started loops reported healthy forever (Med → TD-083).
- **17 vs transaction_cleanup_loop:22** — Interval mismatch (Low).
- **21–26** — Global mutable `loop_heartbeats` dict, no locking (Low).

### `loops/lifecycle.py`
- **20–21** — Process-global `session_state` singleton read/written by scheduler, media sync, health (ARCH → TD-054).

### `loops/queue_cleanup_loop.py`
- **24–32** — No `finally` transaction cleanup/rollback on error (Med → TD-077).
- **25–26** — Sleep-before-first-run: stale rows persist up to 1h after startup (Low → TD-085).
- Missing tests (High → TD-104).

### `loops/lock_cleanup_loop.py`
- **17–18** — Sleep-before-first-run (Low → TD-085). Missing tests (→ TD-104).

### `loops/cloud_cleanup_loop.py`
- **15–28** — Instantiates `MediaRepository` in the loop, calls DB cleanup directly (ARCH → TD-053).
- **37–51** — Asymmetric cleanup (cloud service vs repo) (Med). Missing tests (→ TD-104).

### `loops/transaction_cleanup_loop.py`
- **25–32** — Only injected `BaseService` instances covered; standalone repos accumulate idle-in-transaction sessions (Med → TD-077). Missing tests (→ TD-104).

### `loops/media_sync_loop.py`
- **45–87** — Sync gate opens `initial_sync_complete=True` even when every per-chat sync failed (High → TD-049).
- **62–66 vs 85–87** — Per-chat errors don't affect `consecutive_failures` / alerts (Med → TD-049).
- **67–69** — Legacy global `sync_service.sync()` fallback with no tenant context (Med).
- **132–136** — Error notifications gated on one channel's verbose flag (Low).

### `media_lifecycle.py`
- **47–58** — Cloud delete returning `False` still removes the DB row → Cloudinary orphan (Med).
- **36–58** — No tenant scoping on delete (Low).

### `media_lock.py`
- **83–99** — `create_lock()` never passes `chat_settings_id`; audit gets null tenant (High → TD-050).
- **38–42** — Uses `ChatSettingsRepository` directly instead of `SettingsService` (ARCH → TD-058).
- **79–81, 136–138** — `is_locked()` not tenant-aware (Med → TD-050).

### `media_ingestion.py`
- **155–186** — Duplicate-hash check and `create()` omit `chat_settings_id` (Med → TD-051).
- **232–258** — Category-mix CRUD mixed into ingestion service (TECH DEBT → TD-059).
- **68–86** — Unbounded directory glob materialized in memory (Low).
- **146–149** — Re-scan skips existing path silently (no update on content/category change) (Low).

---

## 2. Telegram subsystem

### `telegram_service.py`
- **66–115** — Constructs 7 repositories, exposes them to every handler (ARCH/OVER → TD-032).
- **154–163** — `is_paused`/`set_paused` always use `TELEGRAM_CHANNEL_ID` (High → TD-061).
- **265–434** — Split callback routing across dispatch table + special-cases + nested `if`; eager `query.answer()` drops downstream toasts (Med → TD-033).
- **372–434** — Commands/messages lack `cleanup_transactions()` in `finally` (Med → TD-077).
- **523–524** — `start_polling` blocks forever on an Event that's never set (Low).

### `telegram_commands.py`
- **113–118, 486–491** — Direct repo reads in `/status`, `/approveall` (ARCH → TD-032).
- **57–97, 707–755** (+ `start_command_router.py:266–302`) — Triplicated DM instance-list UI (OVER → TD-087).
- **251–257** — `_get_sync_status_line` uses global `MediaSyncService().get_last_sync_info()` (Med → TD-031).
- **311** — `/next` skips group membership linking (no `telegram_chat_id`) (Med).
- **464–467** — `/cleanup` blocks the handler 5s via `asyncio.sleep(5)` (Med → TD-086).
- **625–631** — `/link` injects `membership_repo` into `ConversationService` (ARCH → TD-032).

### `telegram_callbacks*.py`
- `telegram_callbacks.py:39–65` — Facade exposes private core methods purely for tests (Low).
- `telegram_callbacks_core.py:89–129` — `_shared_session` monkey-patches `session.commit` → `flush` (High → TD-012).
- `telegram_callbacks_core.py:150–198` — Queue-completion business logic (cap guard, history, lock, delete, stats) lives in a callback utility (ARCH → TD-012).
- `telegram_callbacks_queue.py:213–405` — Repeated keyboard/caption rebuild (OVER → TD-087).
- `telegram_callbacks_queue.py:88–100` — Cap restore may not reset status on all failure paths (Med → TD-013).
- `telegram_callbacks_admin.py:204, 224, 240` — `resume:*` calls `set_paused(False)` → wrong tenant (High → TD-061).
- `telegram_callbacks_admin.py:117–132` — Batch approve ignores daily cap (Med → TD-013).
- `telegram_callbacks_admin.py:194–250` — Legacy resume/reschedule logic, mostly dead under JIT scheduler (Low).

### `telegram_autopost.py`
- **244–346, 591–635** — Failed/cancelled/dry-run/error paths leave queue row in `processing` (High → TD-011).
- **475–505** — Non-atomic success recording via separate commits (High → TD-010).
- **454–458 vs 378–382** — Inconsistent video detection (dry-run mime vs live extension) (Med → TD-044).
- **84, 119–158, 196–198** — In-memory operation locks + background task; not multi-worker safe (High → TD-014).
- **192–195** — Swallowed background exceptions can leave a spinner-only caption (Med).

### `telegram_accounts.py`
- **338–346, 411–414** — `get_by_id_prefix` / `get_account_by_id_prefix` cross-tenant (no filter) (High → TD-060).
- **537–607** — Unbounded Telegram fan-out in `_batch_update_pending_captions`, no backoff (Med → TD-092).
- **49, 125, 160, 191** — Redundant `query.answer()` after global answer (Low → TD-033).

### `telegram_settings.py`
- **210–235** — `instance_manage` lacks authorization (no membership check) (High → TD-004 family; API/authz).
- **210–235, 163–208** — DM instance-manage toggles wrong chat (`query.message.chat_id` vs managed chat) (High).
- **216–219** — Direct `ChatSettingsRepository` access (ARCH → TD-032).
- **491–514** — Triple `query.answer()` on clear queue → no confirmation toast (Med → TD-033).
- **248–255, 385–411** — Settings edit state in `context.user_data` (Med → TD-062).

### `telegram_membership.py`
- **76–85** — Fixed 2s sleep for onboarding race (Med → TD-089).
- **94–100** — Repository injected into `ConversationService` (ARCH → TD-032).
- **138–184** — Dual onboarding state (`user_data` + DB) can desync (Med → TD-062).
- **43–48** — Anonymous-admin bot-add silently ignored (Low).

### `telegram_notification.py`
- **88–98, 133–162** — Always uses `channel_id` for settings/account/send/track → wrong tenant (High → TD-052).
- **139–145** — Always sends as photo; video misbehaves (Med → TD-052).
- **293** — Unescaped filename in enhanced caption (Low).
- **308–327** — Dead `_get_header_emoji` (Low → TD-088).

### `telegram_lifecycle.py`
- **47–48** — Startup overview uses `admin_chat_id` as a user id (Med → TD-090).
- **35–36** — Bare `except Exception` on settings read (Low).

### `telegram_operation_state.py`
- **14–33** — Process-local locks/cancel flags; useless across workers (High → TD-014).
- **14–16** — Unbounded dict growth if `cleanup()` skipped (Med → TD-015).

### `telegram_user_manager.py`
- **23–62** — Process-local membership cache can go stale (Med → TD-016).
- **31–47** — User CRUD via service-held repos (Low → TD-032).

### `telegram_utils.py`
- **60–68** — Fragile string-prefix terminal-caption detection (Med → TD-088).
- **321–339** — Dead `ADD_ACCOUNT_KEYS` / `clear_add_account_state` (Low → TD-088).
- **102–141** — Good `validate_queue_item` helpers underused by admin/batch paths (ENHANCEMENT).

### `start_command_router.py`
- **61–62** — Direct `MembershipRepository()` in router (ARCH → TD-032).
- **266–302** — Duplicated instance list (OVER → TD-087).
- **81–109** — `startgroup` deep-link has no bot-membership check (Med → TD-091).
- **52–53** — `login` payload silently no-ops (Low).

### `conversation_service.py`
- **70–109** — `link_group_to_instance` is not one transaction (Med).
- **10** — Returns ORM `OnboardingSession` to handlers (Low).

---

## 3. Data layer (repositories, models, config)

### Schema management (summary)
- Production: manual numbered SQL in `scripts/migrations/` (001–041) applied via `psql`, tracked in `schema_version`. No automated runner in repo. (→ TD-070)
- Alembic listed in `setup.py` but **no** `alembic.ini` / `migrations/versions/`. (→ TD-070)
- `config/database.py:65–87` `init_db()` uses `Base.metadata.create_all()`; tests do the same (`conftest.py:99–100`). (→ TD-071)
- `create_all()` does not apply partial indexes / backfills / constraints that live only in SQL (021, 015, 014, 040). (→ TD-071, TD-073)

### `repositories/media_repository.py`
- **637–648** — `locked_hashes_subquery` has no tenant filter → cross-tenant exclusion (High → TD-020).
- **769–778** — `get_duplicate_hash_groups()` outer query unfiltered (High → TD-021).
- **183–189, 214–227, 370–398, 424–432, 470–488** — Write paths resolve by bare `media_id` (High → TD-030).
- **800–809** — `deactivate_by_ids()` global (Med → TD-030).
- **435–465** — `clear_stale_cloud_info()` global sweeper touches all tenants (Med).
- **600–811** — Monolithic (~811 lines): CRUD + eligibility engine + analytics + bulk (OVER → TD-059).
- **600–730** — Eligibility EXISTS/subquery engine belongs in a selector service / SQL view (OVER → TD-059).

### `repositories/token_repository.py`
- **194–202** — UPSERT ignores `auth_method` → overwrites wrong credential (High → TD-025).
- **424–446** — `get_expiring_tokens()` no `end_read_transaction()`; naive `utcnow()` (Med/Low → TD-077, TD-038).

### `repositories/queue_repository.py`
- **97–116** — `get_pending()` uses `FOR UPDATE SKIP LOCKED` but never ends the transaction (Med → TD-077).
- **254–295** — `delete_stale()` loads all rows then deletes (Med → TD-039).
- **60–82** — `get_by_id_prefix()` unscoped (Low → TD-060).

### `repositories/history_repository.py`
- **236–248** — `get_by_queue_item_id()` no tenant filter (Low → TD-060).
- **255–696** — Large analytics/reporting SQL block, duplicated aggregation patterns (OVER → TD-059).

### `repositories/chat_settings_repository.py`
- **42–82** — `get_or_create` has no `IntegrityError` retry → race on unique constraint (Med → TD-034).

### `repositories/lock_repository.py`
- **69–93** — Repeated `create()` can stack multiple active TTL locks; `get_active_lock` returns arbitrary `.first()` (Med → TD-035).

### `repositories/membership_repository.py`
- **13–26, 88–131** — Instantiates a second `AuditRepository`/session inside the data layer; membership + audit not atomic (Med → TD-058).

### `repositories/base_repository.py`
- **181–192** — `use_session()` swaps `_db` without closing/restoring the prior generator → leak (Med → TD-075).
- **74–85 vs `self.db.commit()`** — Only `commit()` heals the circuit breaker; direct commits bypass it (Med → TD-076).
- **56–72, 96–129** — Session recovery on every `.db` access + commit-on-read; complex, root cause is pool/Neon-idle (OVER → TD-074).
- **194–200** — `_apply_tenant_filter()` is a no-op when `chat_settings_id` is `None` (ARCH/MT → EPIC-A).
- **162–170** — `__del__` cleanup reliance (Low).

### Models
- `models/api_token.py:106–112` — `NULL auth_method` breaks `UNIQUE(..., auth_method)` (Med → TD-026).
- `models/media_item.py:41–44` — Nullable `chat_settings_id` on tenant-owned rows (Med → TD-031b).
- `models/media_item.py:70–72` — Global `instagram_media_id` unique (Low).
- `models/posting_queue.py:36–39` — No uniqueness on queued `media_item_id` (Med → TD-036).
- `models/category_mix.py:32–44` — No "one current ratio per category/tenant" guarantee (Med → TD-065).
- `models/media_lock.py:53–57`, `api_token.py:98–112`, `media_item.py:95–99`, `posting_history.py:51–54`, `user_chat_membership.py:31–37` — Constraints/indexes exist only in SQL migrations, not ORM `__table_args__` (Med → TD-073).
- Enum-as-string throughout (Low → TD-063b).

### Config
- `config/database.py:72–85` — `init_db()` omits `onboarding_session` / `user_chat_membership` model imports (Med → TD-072).
- `config/settings.py:25–26` + `database.py:11–18` — `pool_size=10`/`max_overflow=20` vs per-repo lazy sessions → exhaustion risk (Med → TD-079).
- `config/database.py:33–37` — `expire_on_commit=False` risks stale ORM state (Med → TD-079).
- `utils/resilience.py:128–133` — Global process-wide `db_circuit_breaker` (Low → TD-064).
- Mixed naive/aware datetimes across repos (Med → TD-038).

### Missing tests (data layer)
- No migration/schema-parity tests; CI never applies SQL migrations (High → TD-100).
- Repo tests are mock-only; no eligibility/concurrency/`FOR UPDATE`/dual-`auth_method` coverage (High → TD-101).
- No dedicated tests for `AuditRepository`, `OnboardingRepository`, `MembershipRepository` (Med → TD-101).

---

## 4. Integrations & media sources

### `integrations/token_refresh.py` (717 lines → OVER, TD-059)
- **147–149, 217–302** — Dual-token refresh targets wrong row (omits `auth_method`); `refresh_all_instagram_tokens()` refreshes same account twice (High → TD-025).
- **247–342** — Failed refresh leaves `FOR UPDATE` lock open (no rollback) (High → TD-078).
- **344–413** — `SKIP LOCKED` skip is silently counted as failure by callers (Med → TD-078).
- **271–299** — Full Meta error payloads logged/stored (Med → TD-018).
- **55–56, 436–450** — In-memory consecutive-failure tracking (Med → TD-041).
- **687–715** — Hardcoded revoke URLs; IG-login revoke hits `graph.facebook.com` (Med/Low → TD-018).

### `integrations/instagram_api.py`
- **331–404** — HTTP 429 without Meta codes 4/17 not mapped to `RateLimitError` (Med → TD-048).
- **123–158** — Rate-limit check non-atomic (Med → TD-037).
- **130–158** — No token refresh-and-retry on mid-flow expiry (Med → TD-048).
- **218–329** — One-shot `httpx` calls, no retry/backoff on transient errors; three clients per post (Med/Low → TD-048).
- **228–315** — Access tokens in query strings (Med → TD-018).
- **65–71** — Unused `TokenRefreshService` dependency (Med → TD-088).

### `integrations/instagram_credentials.py`
- **80–100** — Expired token returns `None`, no refresh attempt (Med → TD-048).
- **29–30, 206–208** — Unbounded class-level `_account_info_cache` (Low → TD-016).
- **260–272** — SSRF in `validate_media_url()` (HEAD any URL, redirects on) (Med → TD-019).

### `integrations/instagram_backfill.py`
- **232–234** — `_get_credentials()` omits `auth_method="instagram_login"` (High → TD-025).
- **226–249** — Reaches into `instagram_service.token_repo`/`.encryption`/`.account_service` (ARCH → TD-032).

### `integrations/backfill_downloader.py`
- **108, 114–128** — SHA256 hashing vs MD5 everywhere else → dedup breaks; also direct `media_repo.db.commit()` + ORM mutation (High → TD-046, TD-040).
- **205–247** — Unbounded download into memory; SSRF via `follow_redirects=True`, no allowlist (Med → TD-045, TD-019).
- **275–287** — Aware `since` raises `TypeError` (Med).
- **137–203** — Backfill API calls have no rate-limit/retry (Med → TD-048).

### `integrations/google_drive*.py` + `media_sources/`
- `media_sources/factory.py:86–99` — Any OAuth error falls back to global service account → cross-tenant (High → TD-056).
- `media_sync.py:403–404` — Uses `TELEGRAM_CHANNEL_ID` as tenant fallback for Drive OAuth (Med → TD-056).
- `google_drive_provider.py:290–294` — Hash fallback switches MD5→SHA256 mid-flight (Med → TD-044/TD-046).
- `google_drive.py:203–209` — Constructs `GoogleDriveOAuthService` ad hoc (Low).
- `media_sync.py:72` — `SyncContext.provider: object` loses the provider contract (Low).

### OAuth flows
- `instagram_login_oauth.py:109–132`, `google_drive_oauth.py:121–145` — `validate_state_token()` ignores `provider`; reaches into `encryption._cipher` (Med/Low → TD-003, TD-022).
- `oauth_service.py:107–137` — State time-limited but not single-use (Med → TD-003).

### Utils
- `utils/encryption.py:41–48` — Singleton `MultiFernet` blocks runtime key rotation (Med → TD-022).
- `utils/image_processing.py:33, 122–151` — GIF listed as supported but rejected by IG Stories; optimize flattens to JPEG (Med → TD-047).
- `utils/resilience.py:18–133` — `CircuitBreaker` transitions untested (Med → TD-104).

---

## 5. API, OAuth & Mini App (FastAPI)

### Security
- `webapp_auth.py:56–58` — Future `auth_date` bypasses TTL (High → TD-001).
- `app.py:74` + `rate_limit.py:7–9` — `trusted_hosts=["*"]` + `memory://` store → spoofable/ineffective rate limiting (High → TD-002).
- `onboarding/helpers.py:70–83` — Bound-token path skips `is_active_member`; revoked members retain access until TTL (High/Med → TD-004).
- `onboarding/settings.py:155–167` + `instagram_account_service.py:501–531` — `remove-account` deployment-wide (High → TD-005).
- `dashboard.py:87–118, 172–188, 256–265` — `/accounts`, `/system-status`, `/analytics/service-health` leak cross-tenant/global data (Med → TD-006).
- `oauth.py:18–32, 192–206` — OAuth `/start` accepts arbitrary `chat_id`, no caller auth → notification spam (Med → TD-007).
- `settings.py:138–167, 255–343` — No RBAC (`instance_role`); shared-account credential overwrite (Med → TD-008).
- All onboarding GET endpoints — `init_data` in query strings (Med → TD-009).
- `webapp_auth.py` — No replay protection for initData/URL tokens (Med → TD-017).
- `oauth_service.py:107–137, 122–125` — State replayable; reaches into `_cipher` (Med → TD-003).

### Bugs
- `dashboard.py:330–336` — Upload size check trusts spoofable `Content-Length` (Med → TD-023).
- `dashboard.py:352–357` — Magic-byte check skipped when detection returns `None` (Low).
- `setup.py:197–217` — Schedule hours not validated for consistency (Low).
- `oauth.py:343–369` — OAuth error responses return HTTP 200 (Low).
- `oauth_service.py:292–296` — Long-lived token exchange may return `None` (Low).

### Architecture
- `dashboard.py:11–13, 367–457, 482–534`, `settings.py:18–19, 361–424` — Routes call `AuditRepository`/`ChatSettingsRepository`/`MediaRepository`/`CategoryMixRepository` directly (Med → TD-053).
- `dashboard.py:87–118, 323–466` — Business logic (account shaping, upload validation, dedup, media creation) in route handlers (Med → TD-053).
- `settings.py:266–302` — Instagram credential validation via `httpx` in the route (Low → TD-053).

### Tech debt / over-complication
- `dashboard.py:290–293, 443–444` — Uploads to ephemeral `/tmp/media/uploads` (Med → TD-024).
- `onboarding/helpers.py:46–102` — `Request` never passed to `_validate_request` → `_client_ip()` always "unknown" (Low).
- `settings.py:60–61, 90–91` — Dashboard mutations omit acting user in audit (`changed_by_user_id=None`) (Low).
- `dashboard_service.py:19–43` — Facade still eagerly constructs six repos (Low → TD-032).

### Missing tests
- No tests for `validate_url_token`/`generate_url_token`; future-`auth_date`; bound-token-without-membership; stale access after revocation (Med → TD-102).
- Rate limits disabled in all API tests (`tests/src/api/conftest.py:15–20`); no 429 assertions (Med → TD-102).
- No tests for cross-instance `remove-account`, factory OAuth→service-account fallback, RBAC (Med → TD-102).
- No `/upload-media` or `/audit-log` API tests (Med → TD-102).

---

## 6. Repo hygiene / build / CI

- Two HTTP servers: raw-socket health server in `src/main.py:29–87` alongside FastAPI `/health` (Low → TD-080).
- Empty `src/services/domain/` package (Low → TD-081).
- No `pyproject.toml`; `setup.py`-only packaging; ruff runs on defaults (Low → TD-082).
- `CHANGELOG.md` is very large (~2k lines) — consider periodic archival of released sections (nice-to-have).
- CI `security` job runs `pip-audit`/`bandit` with `continue-on-error: true` and `|| true` — findings never fail the build (enhancement; consider gating on High severity).
