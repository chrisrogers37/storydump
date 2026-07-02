# Tech Debt & System Review Triage

**Created:** 2026-07-02
**Reviewer:** Full-system architecture & code review
**Scope:** `src/` (~27k LOC), `cli/`, `tests/`, config, CI, deployment
**Status:** OPEN — triage backlog. Each item is written to be convertible into a standalone GitHub issue.

> This is a living tracker. It captures bugs, tech debt, architectural smells,
> over-complication, security gaps, and enhancement opportunities found during a
> structured review of every subsystem. It does **not** change any behavior.
> Items are prioritized so the highest-risk (data-integrity / security /
> multi-tenant) work is addressed first.

---

## How to use this tracker

- **ID** — stable reference (`TD-NNN`). Cite it in commits/PRs (e.g. `fix: scope media eligibility by tenant (TD-004)`).
- **Sev** — High / Med / Low. High = data loss, security, cross-tenant leakage, or production outage risk.
- **Type** — `bug` · `security` · `arch` · `debt` · `overcomplex` · `enhancement` · `test`.
- Check the box and add a PR link when resolved.

### Severity summary

| Severity | Count |
|----------|-------|
| High     | 26    |
| Medium   | 58    |
| Low      | 7     |
| **Total**| **91**|

### Type summary

| Type | Notable theme |
|------|---------------|
| security | Mini App authz, OAuth state replay, spoofable rate-limit key, initData future-timestamp bypass |
| bug | Non-atomic multi-step DB writes, queue rows stuck in `processing`, double `query.answer()` |
| arch | Multi-tenant scoping gaps, per-repo session model, API/loops reaching into repositories |
| overcomplex | God-object `TelegramService`, 800-line repos/services, split callback routing |
| debt | No migration tooling (Alembic declared but unused), schema drift, in-memory state |
| test | No migration/schema-parity tests, mock-only repo tests, missing authz tests |

---

## Cross-cutting themes (epics)

These tie many individual findings together. Fixing the epic root cause resolves
a cluster of downstream items.

### EPIC-A — Multi-tenant isolation is inconsistent and mostly opt-in
The system moved to a multi-tenant model (`chat_settings_id`), but tenant scoping
is a *convention*, not enforced. `BaseRepository._apply_tenant_filter()` is a **no-op
when `chat_settings_id` is `None`**, so any caller that forgets the parameter
silently queries/mutates across all tenants. This produces a large family of
High/Med findings in the data layer, scheduler, media locks, ingestion, Telegram
notifications, and prefix lookups. Recent security fixes (#511/#512/#519) patched
the API surface, but the same class of bug persists deeper in the stack.
→ TD-004, TD-005, TD-006, TD-007, TD-020, TD-021, TD-030, TD-031, TD-050, TD-051, TD-052, TD-060, TD-061.

### EPIC-B — Non-atomic, multi-commit "posting" workflow
Recording a successful post touches 5 things (history, media counter, lock, queue
delete, user stats). The manual callback path uses a shared-session hack; the
autopost path and scheduler do **separate commits**, so a partial failure can
duplicate history, delete a queue row without a lock, or leave a row stuck in
`processing`. There is no single transactional "publish" service reused by all
5 posting paths.
→ TD-010, TD-011, TD-012, TD-013, TD-040.

### EPIC-C — No migration tooling; schema drift between prod and tests
Alembic is declared in `setup.py` but there is **no** `alembic.ini` / `migrations/`.
Production schema is 41 hand-written `scripts/migrations/*.sql` applied manually by
`psql`; tests and `init_db()` build the schema from ORM `create_all()`. Partial
indexes, check constraints, and backfills that exist only in SQL are therefore
**never validated in CI**, and `create_all()` omits some models entirely.
→ TD-070, TD-071, TD-072, TD-073.

### EPIC-D — Process-local state in a multi-worker deployment
Operation locks, cancel flags, membership caches, consecutive-failure counters,
pause state, and settings-edit state live in in-memory dicts / class attributes /
`context.user_data`. Railway runs worker + API (and can scale replicas); this state
is not shared and is lost on restart, enabling double-posting and lost coordination.
→ TD-014, TD-015, TD-016, TD-041, TD-062, TD-090.

### EPIC-E — `TelegramService` is a God-facade and the layering is leaky
Despite a "thin orchestrator" refactor, `TelegramService` still constructs 7
repositories and hands them to every handler; handlers call repos directly, pass
repos into services, and business logic lives in callback utilities. API routes and
background loops also reach into repositories directly, violating the documented
`API/CLI → Services → Repositories → Models` boundary.
→ TD-032, TD-033, TD-053, TD-063, TD-080, TD-081.

---

## Security

- [ ] **TD-001 — initData TTL bypass via future `auth_date`** · High · security
  `src/utils/webapp_auth.py:56-58`. Expiry is `time.time() - auth_date > INIT_DATA_TTL`;
  a token with a *future* `auth_date` yields a negative age and passes indefinitely.
  Add a clock-skew ceiling (reject `auth_date` more than a small delta in the future).

- [ ] **TD-002 — Rate-limit key is spoofable** · High · security
  `src/api/app.py:74` + `src/api/rate_limit.py:7-9`. `ProxyHeadersMiddleware(trusted_hosts=["*"])`
  trusts any `X-Forwarded-For`, and the SlowAPI store is `memory://` (per-process,
  resets on deploy, multiplied by replica count). Per-IP limits are trivially bypassed.
  Restrict trusted proxies to the known ingress and move the limiter store to a shared backend (e.g. Redis).

- [ ] **TD-003 — OAuth state tokens are replayable / not single-use** · Med · security
  `src/services/core/oauth_service.py:107-137`, `instagram_login_oauth.py:109-132`,
  `google_drive_oauth.py:121-145`. State is time-limited (Fernet TTL) but never consumed,
  and `validate_state_token()` ignores the `provider` field, so a state minted for one
  flow is accepted by another. Bind `provider` and consume state on first use.

- [ ] **TD-004 — Bound-token path skips membership check (authz gap)** · High · security · MT
  `src/api/routes/onboarding/helpers.py:70-83`. When initData carries a bound `chat_id`,
  authorization stops at the crypto binding and never calls `MembershipService.is_active_member()`.
  A group member who never interacted with the bot (no `UserChatMembership` row) can still
  open that instance's dashboard, and revoked members retain access until TTL expiry.
  Require an active-membership check on **all** authenticated paths.

- [ ] **TD-005 — `remove-account` is deployment-wide, not chat-scoped** · High · security · MT
  `src/api/routes/onboarding/settings.py:155-167` + `instagram_account_service.py:501-531`.
  Route validates membership for `request.chat_id`, but `deactivate_account(account_id)`
  soft-deletes the account for the entire deployment. Any member of one chat can disable an
  account used by other instances. Scope deactivation to the chat, or gate behind ownership.

- [ ] **TD-006 — Cross-tenant metadata exposure on `/accounts`, `/system-status`, `/analytics/service-health`** · Med · security · MT
  `dashboard.py:87-118, 172-188, 256-265`. These return every deployment account, global
  health (queue depth, IG rate-limit headroom, loop liveness), and cross-tenant `service_runs`
  aggregates to any authenticated caller. Scope responses to the requesting instance.

- [ ] **TD-007 — OAuth `/start` accepts arbitrary `chat_id` with no caller auth** · Med · security
  `src/api/routes/oauth.py:18-32, 192-206`. Anyone can start an OAuth flow for any chat and,
  on cancel/error, trigger Telegram notifications to that chat (harassment/spam vector).
  Require authentication or rate-limit + bind to an authenticated session.

- [ ] **TD-008 — Missing RBAC on account management + settings mutations** · Med · security
  `settings.py:138-167, 255-343`. `_validate_request` checks membership but never
  `instance_role`; any member can switch the active account, deactivate accounts, or overwrite
  credentials for a shared `instagram_account_id`. Add owner/admin role checks (the follow-up
  already noted in the #512 changelog entry).

- [ ] **TD-009 — Auth material (`init_data`) passed in query strings** · Med · security
  All onboarding GET endpoints (`dashboard.py:31-32`, `setup.py:31-32`). HMAC-signed
  credentials land in access logs, browser history, and `Referer`. Move to a header (e.g.
  `Authorization` / `X-Telegram-Init-Data`).

- [ ] **TD-017 — No replay protection for initData / URL tokens** · Med · security
  `src/utils/webapp_auth.py`. Valid tokens are accepted repeatedly within the 1h TTL; there
  is no nonce/`query_id` single-use store, so a stolen token is replayable until expiry.

- [ ] **TD-018 — Access tokens in Meta API query strings; error payloads logged** · Med · security
  `instagram_api.py:228-315`, `token_refresh.py:271-299`, `oauth_service.py:317-318`.
  Tokens are passed as URL params (exposed to proxy/LB logs) and full Meta error bodies are
  logged/stored in `service_runs.result_summary`. Prefer headers; redact error payloads.

- [ ] **TD-019 — SSRF on media download / URL validation** · Med · security
  `backfill_downloader.py:205-247`, `instagram_credentials.py:260-272`. Downloads/HEADs
  arbitrary URLs with `follow_redirects=True` and no host allowlist or max-size cap; a
  redirect could reach internal addresses (and OOM the worker — see TD-045). Add an allowlist
  + size cap + disable redirects to non-CDN hosts.

- [ ] **TD-022 — Encryption singleton blocks key rotation without restart** · Med · security
  `src/utils/encryption.py:41-48`. `MultiFernet` is cached process-wide on first use; rotating
  `ENCRYPTION_KEYS` in env has no effect until restart (only a test-only `reset()` exists).
  OAuth state validation also reaches into the private `_cipher` API (`oauth_service.py:122-125`).

- [ ] **TD-023 — Upload endpoint trusts spoofable `Content-Length`, no dedicated rate limit** · Med · security · bug
  `dashboard.py:330-336, 405-466`. Pre-read size check uses the client header; a wrong/missing
  header still reads the full body into memory. `/upload-media` also has no `@limiter.limit()`.
  Enforce a hard streamed size cap and add a stricter per-endpoint limit.

- [ ] **TD-024 — Uploads written to ephemeral `/tmp`** · Med · debt · bug
  `dashboard.py:290-293, 443-444`. `/tmp/media/uploads` is lost on reboot, leaving stale DB
  paths. Store uploads in durable storage (Cloudinary/object store) like the rest of the media pipeline.

## Data integrity & multi-tenant scoping (see EPIC-A / EPIC-B)

- [ ] **TD-020 — Cross-tenant hash-lock exclusion in media eligibility** · High · bug · MT
  `src/repositories/media_repository.py:637-648`. `_apply_eligibility_filters()` tenant-scopes
  queue/lock subqueries but `locked_hashes_subquery` has **no** tenant filter, so Tenant A's
  locked hashes wrongly exclude eligible media in Tenant B.

- [ ] **TD-021 — Cross-tenant duplicate-hash groups** · High · bug · MT
  `media_repository.py:769-778`. `get_duplicate_hash_groups()` filters the inner subquery but the
  outer join has no `chat_settings_id`, returning duplicate groups across all tenants (drives
  `dedup-media` across tenant boundaries).

- [ ] **TD-025 — Token UPSERT ignores `auth_method`, overwrites wrong credential** · High · bug
  `token_repository.py:194-202` + `token_refresh.py:147-149, 217-302`. Lookup keys on
  `(service, type, account_id)` only; after migration 040 an account can hold both
  `instagram_login` and `fb_login` tokens, so refresh/reconnect can clobber the wrong row.
  Also `refresh_all_instagram_tokens()` refreshes the same account twice, and backfill
  (`instagram_backfill.py:232-234`) omits the `auth_method` filter when selecting credentials.

- [ ] **TD-026 — `NULL auth_method` defeats the unique constraint** · Med · bug
  `models/api_token.py:106-112` + migration 040. Postgres treats NULLs as distinct in
  `UNIQUE(..., auth_method)`, so multiple active rows per account/flow are possible while
  `auth_method` is NULL. Backfill + `NOT NULL` or a `COALESCE`-based partial index.

- [ ] **TD-030 — Media write paths skip tenant filter** · High · bug · MT
  `media_repository.py:183-189, 214-227, 370-388, 392-398, 424-432, 470-476, 484-488, 800-809`.
  Mutators (`reactivate`, `update_metadata`, `increment_times_posted`, `deactivate_by_ids`)
  resolve rows by bare `media_id`; knowing a UUID allows cross-tenant reads/writes.

- [ ] **TD-031 — Scheduler selection/preview/availability not tenant-scoped** · High · bug · MT
  `scheduler.py:217-234, 271-277, 312, 792, 878-879`. `_select_media_from_pool()`,
  `_pick_category_for_slot()`, `get_queue_preview(telegram_chat_id)` (ignores the arg), and
  `check_availability()` omit `chat_settings_id`, so media/category/preview/lock checks can
  bleed across tenants.

- [ ] **TD-050 — Media locks created without `chat_settings_id`** · High · bug · MT
  `media_lock.py:83-99`. `create_lock()` takes `telegram_chat_id` for TTL lookup but never
  passes `chat_settings_id` to `lock_repo.create()`, so locks are globally scoped and the audit
  row gets a null tenant. Breaks eligibility isolation.

- [ ] **TD-051 — Ingestion not tenant-scoped** · Med · bug · MT
  `media_ingestion.py:155-159, 176-186`. Duplicate-hash check and `create()` omit
  `chat_settings_id`; local/CLI ingestion can't associate media with a tenant.

- [ ] **TD-052 — Telegram notifications always target the env channel** · High · bug · MT
  `telegram_notification.py:88-98, 133-149`. `send_notification` always uses
  `self.service.channel_id` (`TELEGRAM_CHANNEL_ID`) for settings, account lookup, and send —
  so in a multi-instance deployment every post goes to the single env channel with the wrong
  tenant's settings. Use the queue item's tenant chat.

- [ ] **TD-060 — Prefix lookups are global (queue item + account)** · High · bug · MT
  `telegram_accounts.py:338-346, 411-414`. `get_by_id_prefix()` / `get_account_by_id_prefix()`
  run with no tenant filter; an 8-char UUID prefix can resolve to another instance's queue item
  or account during inline posting. Also `queue_repository.py:60-82`, `history_repository.py:236-248`.

- [ ] **TD-061 — Global pause uses `TELEGRAM_CHANNEL_ID`, not the acting chat** · High · bug · MT
  `telegram_service.py:155-163`. `is_paused`/`set_paused` always read/write
  `settings_service.get_settings(self.channel_id)`. Resume callbacks
  (`telegram_callbacks_admin.py:204, 224, 240`) therefore unpause the env channel, not the chat
  that pressed the button.

- [ ] **TD-034 — `chat_settings.get_or_create` race** · Med · bug
  `chat_settings_repository.py:42-82`. No `IntegrityError` retry (unlike
  `MembershipRepository.create_membership`); concurrent first access for the same
  `telegram_chat_id` raises on the unique constraint.

- [ ] **TD-035 — Duplicate active TTL locks possible** · Med · bug
  `lock_repository.py:69-93`. Migration 021 only partial-indexes *permanent* locks; repeated
  `create()` can stack multiple active TTL locks per media item, and `get_active_lock()` returns
  an arbitrary `.first()`.

- [ ] **TD-036 — No uniqueness on queued media (double-queue race)** · Med · bug
  `models/posting_queue.py:36-39`. Eligibility uses `EXISTS`, but there's no DB constraint on
  `media_item_id` in `pending`/`processing`; concurrent ticks/workers can double-select the same
  item (TOCTOU with `scheduler.py:312-363`).

- [ ] **TD-037 — Daily-cap check is non-atomic (TOCTOU) and only counts `posted`** · Med · bug
  `daily_cap.py:16-28`, `scheduler.py:156-170`, `instagram_api.py:123-158`. `can_post_today()` is
  checked before writing history with no reservation; concurrent `/next` + scheduler can exceed
  `posts_per_day`. Also `posts_per_day == 0` silently blocks all posting and raises
  `ZeroDivisionError` in interval math (`scheduler.py:65,98`). Validate config; reserve atomically.

## Posting workflow atomicity (EPIC-B)

- [ ] **TD-010 — Autopost success recording is non-atomic** · High · bug
  `telegram_autopost.py:475-505`. Separate commits for history, media increment, lock, queue
  delete, and user stats; partial failure can duplicate history or delete the queue row without
  a lock. Wrap in one transaction (a shared publish service).

- [ ] **TD-011 — Autopost failure/cancel leaves queue row in `processing`** · High · bug
  `telegram_autopost.py:244-346, 591-635`. Only the daily-cap path restores `pending`;
  safety-check failure, user cancel, dry-run exit, and generic errors leave the item claimed
  until `requeue_stale_processing` (~10 min). Restore on every exit path.

- [ ] **TD-012 — Callback queue-completion logic lives in a utility + monkey-patches `commit`** · High · overcomplex · arch
  `telegram_callbacks_core.py:89-198`. `_shared_session` replaces `session.commit` with `flush`
  to fake atomicity across five repos; daily-cap guard, history/lock creation, queue delete, and
  user stats live in `TelegramCallbackCore` rather than a reusable service. Replace with a real
  Unit-of-Work / transactional publish service (ties into EPIC-B and TD-074).

- [ ] **TD-013 — Batch approve ignores daily cap; cap-restore incomplete on other failures** · Med · bug
  `telegram_callbacks_admin.py:117-132`, `telegram_callbacks_queue.py:88-100`. Batch approve calls
  `_execute_complete_db_ops(...,"posted",True)` with no pre-check and no cap-specific message; the
  single-item path can also leave an item stuck in `processing` if `_execute_complete_db_ops`
  fails after claim for a non-cap reason.

- [ ] **TD-040 — Backfill downloader mutates ORM + commits directly** · High · arch · bug
  `backfill_downloader.py:114-128`. After `media_repo.create()` it sets fields on the ORM object
  and calls `media_repo.db.commit()` directly, bypassing repository methods and transaction
  boundaries.

## Multi-worker / process-local state (EPIC-D)

- [ ] **TD-014 — Autopost operation locks & cancel flags are process-local** · High · arch
  `telegram_operation_state.py:14-33`, `telegram_autopost.py:84, 119-158`. In-memory dicts are
  useless across replicas and lost on restart, so a second worker or a restart mid-autopost can
  double-post. Move coordination to the DB (e.g. `claim_for_processing` + a lease/heartbeat).

- [ ] **TD-015 — Operation-state dicts grow unbounded** · Med · debt
  `telegram_operation_state.py:14-16`. If `cleanup()` is skipped on an abnormal path, entries
  accumulate per `queue_id` with no TTL/LRU.

- [ ] **TD-016 — Membership cache can go stale** · Med · arch
  `telegram_user_manager.py:23-62`. `_known_memberships` avoids DB re-checks but stays stale if
  membership is created/deactivated elsewhere until process restart or explicit eviction.

- [ ] **TD-041 — In-memory consecutive-failure / throttle counters** · Med · debt
  `scheduler.py:396-397` (class-level `_consecutive_send_failures`), `token_refresh.py:55-56`,
  `scheduler_loop.py:80-83` (state on a function object). Per-process, not shared, and cause
  cross-tenant coupling of the systemic-failure alert.

- [ ] **TD-062 — Settings-edit & onboarding state in `context.user_data`** · Med · debt
  `telegram_settings.py:248-255, 385-411`, `telegram_membership.py:138-184`. Numeric edit flow and
  `onboarding_session_id` live in per-worker memory and desync from the DB on restart.

## Scheduler / posting logic

- [ ] **TD-042 — Failed sends block media for up to 24h** · Med · bug
  `scheduler.py:370-374 vs 459-461`. On Telegram failure the queue row stays `failed`; eligibility
  excludes any queued media (all statuses) and `last_post_sent_at` is unchanged, so the slot stays
  "due" but the item can't be re-selected until the hourly `delete_stale(hours=24)`.

- [ ] **TD-043 — Instagram auto-approve failure consumes the slot** · Med · bug
  `scheduler.py:575-578`. On IG API failure `last_post_sent_at` is advanced but no
  post/history/lock is recorded; the tenant silently loses that slot.

- [ ] **TD-044 — Inconsistent video detection across paths** · Med · bug
  `scheduler.py:690-695`, `telegram_autopost.py:454-458 vs 378-382`, `google_drive_provider.py:290-294`.
  Some paths detect video by local `file_path` extension (absent for cloud items), others by
  `mime_type`; cloud-only videos can be mis-posted as IMAGE. Centralize media-type detection.

- [ ] **TD-045 — Unbounded download into memory (OOM)** · Med · bug
  `backfill_downloader.py:205-247`. `response.content` is read fully with no max-bytes cap (see also SSRF, TD-019).

- [ ] **TD-046 — Backfill uses SHA256 while the rest uses MD5** · High · bug
  `backfill_downloader.py:108-128` vs `file_hash.py` / `LocalMediaProvider` / Drive `md5Checksum`.
  Cross-source dedup/rename detection silently fails for backfilled items — and a test currently
  *codifies* the SHA256 expectation (`test_backfill_downloader.py:164`).

- [ ] **TD-047 — GIF listed as supported but rejected by IG Stories** · Med · bug
  `image_processing.py:33, 122-151`. `SUPPORTED_FORMATS` includes GIF and
  `optimize_for_instagram()` flattens it to JPEG (destroying animation) while IG still rejects it
  (error 9004). Remove GIF or convert to MP4.

- [ ] **TD-048 — No retry/backoff on transient Instagram API errors; 429 not mapped** · Med · bug
  `instagram_api.py:218-404`. Container create/poll/publish are one-shot `httpx` calls (no tenacity,
  unlike the Drive provider), and a bare HTTP 429 without Meta codes 4/17 becomes a generic error,
  bypassing rate-limit handling. Also no refresh-and-retry when a token expires mid-post
  (`instagram_api.py:130-158`, `instagram_credentials.py:80-100`).

- [ ] **TD-049 — Media-sync gate opens even when every tenant sync failed** · High · bug
  `media_sync_loop.py:45-87`. Per-chat `except` logs and continues; the outer block still sets
  `initial_sync_complete = True`, letting the scheduler post against empty/stale media when all
  tenant syncs failed. Per-chat errors also don't affect `consecutive_failures` or alerts.

## Architecture / layering (EPIC-E)

- [ ] **TD-032 — `TelegramService` is a repository hub / God-facade** · Med · overcomplex · arch
  `telegram_service.py:66-115`. Constructs 7 repositories and exposes them to every handler;
  handlers read repos directly and pass repos into services (`telegram_commands.py:113-118, 486-491,
  625-631`, `telegram_membership.py:94-100`, `start_command_router.py:61-62`). Introduce narrow
  domain services (Queue/Dashboard/User/Membership) and stop leaking repos into handlers.

- [ ] **TD-033 — Split, fragile callback routing + eager `query.answer()`** · Med · overcomplex · bug
  `telegram_service.py:265-434`. Dispatch is spread across `_build_callback_dispatch_table()`,
  `_handle_callback_special_cases()`, and nested `if data == ...` branches (new callbacks are easy
  to miss → dead buttons). `_handle_callback` always answers immediately, but many handlers call
  `query.answer(text=...)` again — Telegram allows one answer, so user-facing toasts are silently
  dropped (`telegram_accounts.py:49,125,160,191`, `telegram_settings.py:491-514`).

- [ ] **TD-053 — API routes & loops reach into repositories directly** · Med · arch
  `dashboard.py:11-13, 367-457`, `settings.py:18-19, 361-424` (AuditRepository / ChatSettingsRepository
  / MediaRepository / CategoryMixRepository in route handlers); `cloud_cleanup_loop.py:15-28` and
  `scheduler_loop.py:114-272` instantiate repos / call `application.bot.send_message` directly.
  Route/loop code should go through services and a notification service.

- [ ] **TD-054 — Scheduler couples to worker loop internals** · Med · arch
  `scheduler.py:302-305, 611-613`. Core scheduler imports `session_state` from `loops.lifecycle` and
  constructs `MediaLockService()` inline instead of injecting. `loops/lifecycle.py:20-21` exposes a
  process-global `session_state` singleton read/written by scheduler, media sync, and health.

- [ ] **TD-055 — `posting.py` and loops build raw `telegram.Bot` for alerts** · Med · arch
  `posting.py:55-94`, `scheduler_loop.py:114-272`, `guarded.py:121-124`. Duplicates send logic and
  bypasses `TelegramService`; alert dedup (`gdrive_alerted_at`) only sets after a *successful* send,
  so persistent Telegram outages spam alerts each tick. Also crash alerts don't guard a missing
  `ADMIN_TELEGRAM_CHAT_ID`.

- [ ] **TD-056 — `media_sources/factory.py` falls back to service account on ANY OAuth error** · High · arch · security
  `factory.py:86-99`. A broad `except Exception` around per-tenant OAuth silently falls back to the
  global service account, potentially crossing tenant boundaries and masking auth errors. Narrow the
  fallback and never cross tenants. Related: `media_sync.py:403-404` uses `TELEGRAM_CHANNEL_ID` as
  the tenant fallback for Drive OAuth lookups.

- [ ] **TD-057 — Unrelated cleanup jobs embedded in the scheduler loop** · Med · debt
  `scheduler_loop.py:355-368`. Onboarding `ConversationService.cleanup_expired()` runs inside the
  scheduler tick keyed off `retention_tick_counter`, coupling unrelated jobs and complicating failure
  isolation. Give it its own loop (like lock/queue cleanup).

- [ ] **TD-058 — `MembershipRepository` composes a second repo/session; audit isn't atomic** · Med · arch
  `membership_repository.py:13-26, 88-131`. Instantiates `AuditRepository` (a second session) inside
  the data layer; membership commit and audit log are not atomic. `media_lock.py:38-42` similarly
  reaches into `ChatSettingsRepository` instead of `SettingsService`.

## Data layer / session model

- [ ] **TD-074 — Per-repo lazy session instead of Unit-of-Work** · Med · debt · arch
  `base_repository.py:29-72`. Every repository opens/holds its own session; multi-repo transactions
  need the fragile `use_session()` + `commit`-monkey-patch hack (TD-012). Introduce a session factory /
  Unit-of-Work so a request/handler shares one transactional session.

- [ ] **TD-075 — `use_session()` leaks the pre-swap session** · Med · bug
  `base_repository.py:181-192`. Swapping `_db` doesn't close/restore the original `_db_generator`;
  a lazily-opened session before the swap leaks a pool connection until GC.

- [ ] **TD-076 — Direct `self.db.commit()` bypasses the circuit-breaker heal** · Med · bug
  `base_repository.py:74-85` vs repos calling `self.db.commit()`. Only `BaseRepository.commit()` calls
  `db_circuit_breaker.record_success()`, so successful writes done via `self.db.commit()` never heal
  the circuit after failures.

- [ ] **TD-077 — Open read transactions left dangling** · Med · bug
  `queue_repository.py:97-116` (`get_pending` uses `FOR UPDATE SKIP LOCKED`, no `end_read_transaction`),
  `token_repository.py:424-446`. Holds row locks / idle-in-transaction connections until the repo closes.
  Also `queue_cleanup_loop.py:24-32` has no rollback/cleanup on error (unlike the other loops).

- [ ] **TD-078 — Failed token refresh leaves a `FOR UPDATE` lock open** · High · bug
  `token_refresh.py:247-342`. After `get_token_for_update()`, API failure / `TokenRevokedError` /
  network errors return or raise without `rollback()`, blocking other workers via `SKIP LOCKED`.
  A silent skip also gets counted as a failure by callers (`token_refresh.py:344-413`).

- [ ] **TD-079 — Pool sizing vs per-repo sessions & `expire_on_commit=False`** · Med · arch
  `config/settings.py:25-26` (`pool_size=10`, `max_overflow=20`), `config/database.py:33-37`.
  Worker + API + many lazily-held repo sessions per request can exhaust 30 connections; and
  `expire_on_commit=False` risks stale ORM state across commits. Revisit with the UoW work (TD-074).

- [ ] **TD-039 — `delete_stale()` loads all rows then deletes** · Med · debt
  `queue_repository.py:254-295`. Fetches all stale rows into Python before deleting (the 954-row
  incident noted in comments). Use a single `DELETE ... RETURNING`.

- [ ] **TD-038 — Mixed naive/aware datetimes** · Med · bug
  Widespread `datetime.utcnow()` (`queue_repository.py:105`, `lock_repository.py:34`,
  `token_repository.py:436`) vs `timezone.utc` elsewhere, feeding naive timestamps into TZ-aware
  columns (`ApiToken.revoked_at`, `ChatSettings.last_post_sent_at`). Standardize on
  `datetime_utils.ensure_utc()`.

- [ ] **TD-059 — Monolithic repositories/services with embedded query logic** · Med · overcomplex
  `media_repository.py` (~810 lines: CRUD + eligibility engine + analytics + bulk),
  `history_repository.py` (analytics SQL), `token_refresh.py` (717 lines), `scheduler.py` (880),
  `telegram_commands.py` (816). Extract the eligibility engine (a SQL view or selector service) and
  split analytics/reporting reads.

- [ ] **TD-064 — Global process-wide DB circuit breaker** · Low · debt
  `resilience.py:128-133` + `base_repository.py:46-51`. One tenant's outage opens the circuit for all
  repos in the process.

## Schema / models

- [ ] **TD-070 — Adopt a real migration tool (Alembic) — declared but unused** · High · debt · EPIC-C
  `setup.py:12` lists Alembic but there is no `alembic.ini` / `migrations/`. 41 hand-written
  `scripts/migrations/*.sql` are applied manually via `psql` with no in-repo runner. Introduce Alembic
  (or a scripted runner + CI apply) so schema evolution is reproducible and tested.

- [ ] **TD-071 — Prod/test schema divergence (`create_all()` vs SQL migrations)** · High · debt · test · EPIC-C
  `config/database.py:65-87`, `tests/conftest.py:99-100`. Tests build schema from ORM only, so
  partial indexes / check constraints / backfills that exist only in SQL migrations (021, 015, 040, 014)
  are never validated. Add a schema-parity test and run migrations in CI.

- [ ] **TD-072 — `init_db()` omits models** · Med · bug
  `config/database.py:72-85` doesn't import `onboarding_session` / `user_chat_membership` (and anything
  added later), so a fresh `create_all()` install is missing those tables.

- [ ] **TD-073 — Constraints/indexes exist only in SQL, not in ORM `__table_args__`** · Med · bug · EPIC-C
  Permanent-lock partial unique (`media_lock.py:53-57` vs migration 021), Google-Drive-token partial
  unique (`api_token.py:98-112` vs 015), legacy file_path unique (`media_item.py:95-99` vs 014),
  `posting_method` check (`posting_history.py:51-54` vs 004), UCM FK indexes (`user_chat_membership.py:31-37`
  vs 023). Mirror these in the models so ORM-built schemas match production.

- [ ] **TD-031b — Nullable `chat_settings_id` on tenant-owned rows** · Med · debt · MT
  `models/media_item.py:41-44`. FK is nullable "legacy"; NULL rows bypass every tenant filter (caused a
  production backfill, migration 018). Plan a `NOT NULL` migration once legacy rows are cleaned.

- [ ] **TD-063b — Enum-as-string throughout** · Low · debt
  Status/role/lock_reason/interaction_type are `String` + optional `CheckConstraint` rather than
  Postgres ENUMs; typos fail at runtime/DB, not import. Consider native enums or Python `Enum` + validation.

- [ ] **TD-065 — `category_mix` lacks a single-current-row guarantee** · Med · bug
  `models/category_mix.py:32-44`. Only a ratio range check exists; concurrent `set_mix()` could leave
  multiple `is_current=True` rows per category/tenant. Add a partial unique index.

## Enhancements & smaller items

- [ ] **TD-080 — Two HTTP servers (raw-socket health server + FastAPI)** · Low · overcomplex
  `src/main.py:29-87` hand-builds HTTP responses with `asyncio.start_server` while a full FastAPI app
  with `/health` already exists (`api/app.py`). Consolidate on one.

- [ ] **TD-081 — Empty `src/services/domain/` package** · Low · debt
  Only `__init__.py`; documented in the architecture as a layer but unused. Remove or populate.

- [ ] **TD-082 — No `pyproject.toml`; ruff runs on defaults** · Low · debt
  Packaging is `setup.py`-only and there is no ruff/tool config. Adopt `pyproject.toml` (PEP 621) and
  pin ruff rules so lint is reproducible and versioned.

- [ ] **TD-083 — `heartbeat` reports never-started loops as healthy** · Med · bug
  `loops/heartbeat.py:41-46`. A loop that failed to launch shows `alive: True, "Starting up"` forever, so
  `/health` never flags it stale. Track an explicit "started" transition.

- [ ] **TD-084 — `guarded()` silently stops a loop after the restart budget** · Med · bug
  `loops/guarded.py:65-80`. After max restarts it returns; the worker keeps running without that
  background task (only a Telegram alert signals it). Consider crashing the process so Railway restarts it.

- [ ] **TD-085 — Sleep-before-first-run in cleanup loops** · Low · enhancement
  `queue_cleanup_loop.py:25-26`, `lock_cleanup_loop.py:17-18`. Stale rows from a prior crash persist up
  to an hour after start. Run once on startup, then sleep.

- [ ] **TD-086 — `/cleanup` blocks the update pipeline for 5s** · Med · debt
  `telegram_commands.py:464-467`. `await asyncio.sleep(5)` inside the handler stalls the bot for every
  cleanup. Move to a background task.

- [ ] **TD-087 — Duplicated instance-list UI across 3+ sites** · Med · overcomplex
  `telegram_commands.py:57-97, 707-755`, `start_command_router.py:266-302`. Near-identical
  rendering/keyboards drift (MarkdownV2 escaping, fields, buttons). Extract one renderer. Related:
  keyboard/caption rebuild duplicated in `telegram_callbacks_queue.py:213-405` and
  `telegram_accounts.rebuild_posting_workflow`.

- [ ] **TD-088 — Fragile terminal-caption detection & dead code** · Low · debt
  `telegram_utils.py:60-68` (`_TERMINAL_CAPTION_PREFIXES` string-prefix list must track copy changes),
  plus dead code: `telegram_notification.py:308-327` (`_get_header_emoji`), `telegram_utils.py:321-339`
  (`ADD_ACCOUNT_KEYS`), `scheduler.py:800-834` (`_allocate_slots_to_categories`), `scheduler.py:31`
  (unused `SCHEDULE_JITTER_MINUTES`), `instagram_api.py:65-71` (unused `TokenRefreshService`).

- [ ] **TD-089 — Onboarding auto-link uses a fixed 2s sleep** · Med · debt
  `telegram_membership.py:76-85`. `asyncio.sleep(2)` to race the bot-add commit is slow and still fails
  if the commit takes >2s. Use a DB "pending link" signal instead.

- [ ] **TD-090 — Startup overview mis-uses admin chat id as a user id** · Med · bug
  `telegram_lifecycle.py:47-48`. `get_user_instances(self.service.admin_chat_id)` expects a Telegram
  *user* id; if `ADMIN_TELEGRAM_CHAT_ID` is a group/channel, it always reports "No instances configured."

- [ ] **TD-091 — `startgroup` deep-link setup skips bot-membership check** · Med · bug
  `start_command_router.py:81-109`. Unlike `/link`, deep-link group setup calls
  `link_group_to_instance` without verifying the bot is actually a member.

- [ ] **TD-092 — Unbounded Telegram fan-out without rate-limit wrapper** · Med · debt
  `telegram_accounts.py:537-607`. `_batch_update_pending_captions` sequentially edits every pending
  message with no backoff (unlike `telegram_edit_with_retry`). Batch with the resilience wrapper.

## Testing gaps

- [ ] **TD-100 — No migration / schema-parity tests** · High · test · EPIC-C
  CI never applies `scripts/migrations/*.sql`; no test asserts migrated schema == ORM schema, nor that
  the 021/015/040 constraints behave as intended (`tests/conftest.py:99-100`).

- [ ] **TD-101 — Repo tests are mock-only; no eligibility/concurrency coverage** · High · test
  Nearly all repo tests patch `Session`. Missing: `_apply_eligibility_filters` / cross-tenant exclusion
  against real Postgres, `FOR UPDATE SKIP LOCKED` claim behavior, dual-`auth_method` token UPSERT
  (TD-025), and no dedicated tests for `AuditRepository`, `OnboardingRepository`, `MembershipRepository`.

- [ ] **TD-102 — Missing security/authz tests** · Med · test
  No tests for: bound-token access without membership (TD-004), stale access after revocation, future
  `auth_date` bypass (TD-001), `validate_url_token`, cross-instance `remove-account`, factory
  OAuth→service-account fallback (TD-056), RBAC on account management. Rate limits are disabled in all
  API tests (`tests/src/api/conftest.py:15-20`) so 429 behavior is never asserted.

- [ ] **TD-103 — Missing tests for tenant scoping across services** · Med · test
  No tests assert `chat_settings_id` propagation in scheduler selection/preview, media-lock creation
  (TD-050), notification target chat (TD-052), or that all-tenant sync failure keeps the gate closed (TD-049).

- [ ] **TD-104 — Untized cross-cutting utilities & loops** · Med · test
  `CircuitBreaker`/`db_circuit_breaker` state transitions (`resilience.py`), `cloud_storage.get_story_optimized_url()`
  regex/transform, `telegram_operation_state`, and the cleanup loops (queue/lock/cloud/transaction) have
  no dedicated tests.

---

## Appendix — review method

Findings were produced by a structured pass over each subsystem (scheduler/posting,
Telegram, data layer, integrations, API/OAuth, CLI/config/utils), cross-referenced
against the documented architecture (`CLAUDE.md`: `CLI/API → Services → Repositories → Models`)
and the recent security work (#511/#512/#519). Line numbers reference the state of
`main` at commit `7c99a34`. No runtime behavior was changed by this document.
