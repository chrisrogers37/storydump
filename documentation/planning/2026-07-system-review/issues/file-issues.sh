#!/usr/bin/env bash
#
# file-issues.sh — create the system-review backlog as GitHub issues.
#
# Generated from documentation/planning/2026-07-system-review/triage-tracker.md
# Files 36 issues: 6 x P0 (individual), 20 x P1 (individual), 10 x clusters
# (P2/P3/P4/nice-to-have), covering all 91 review findings.
#
# The cloud-agent gh CLI is read-only, so run this from an authenticated
# environment with issue-write scope.
#
# Usage:
#   ./file-issues.sh                      # dry-run: print titles/labels only
#   ./file-issues.sh --confirm            # create the issues
#   ./file-issues.sh --confirm --repo owner/name
#
set -euo pipefail

DRY_RUN=true
REPO=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --confirm) DRY_RUN=false; shift ;;
    --repo) REPO="$2"; shift 2 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

if ! command -v gh >/dev/null 2>&1; then
  echo "ERROR: gh (GitHub CLI) not found on PATH." >&2
  exit 1
fi

REPO_ARGS=()
if [[ -n "$REPO" ]]; then REPO_ARGS=(--repo "$REPO"); fi

if ! $DRY_RUN; then
  if ! gh auth status >/dev/null 2>&1; then
    echo "ERROR: 'gh auth status' failed — authenticate with issue-write scope first." >&2
    exit 1
  fi
fi

COUNT=0
create_issue() {
  local title="$1"; local labels="$2"; local body="$3"
  COUNT=$((COUNT + 1))
  if $DRY_RUN; then
    printf '[%02d] (dry-run) %s\n        labels: %s\n' "$COUNT" "$title" "$labels"
    return 0
  fi
  echo "[$COUNT] creating: $title"
  gh issue create "${REPO_ARGS[@]}" --title "$title" --label "$labels" --body "$body"
}

FOOTER=$'\n\n---\nFiled from the 2026-07 full-system review. See `documentation/planning/2026-07-system-review/triage-tracker.md` and `detailed-findings.md`.'

# =====================================================================
# P0 — Critical (individual)
# =====================================================================

create_issue "P0: initData TTL bypass via a future auth_date (TD-001)" \
  "priority:critical,security,auto-audit" \
  "$(cat <<'EOF'
**Priority:** P0 · **Severity:** High · **Type:** security · **Ref:** TD-001

### Problem
`src/utils/webapp_auth.py:56-58` validates initData freshness with
`time.time() - auth_date > INIT_DATA_TTL`. A token carrying a **future**
`auth_date` yields a negative age, so the expiry check passes indefinitely until
real time catches up. There is no clock-skew ceiling.

### Impact
A crafted/replayed initData with a forward-dated `auth_date` never expires,
extending the window for stolen-token reuse against the Mini App API.

### Fix
- Reject `auth_date` more than a small delta (e.g. 60s) in the future.
- Add a maximum absolute age check regardless of sign.

### Acceptance criteria
- Future `auth_date` beyond the skew ceiling is rejected.
- Unit tests cover past-expired, future-dated, and within-window cases.
EOF
)$FOOTER"

create_issue "P0: Spoofable / ineffective Mini App rate limiting (TD-002)" \
  "priority:critical,security,auto-audit" \
  "$(cat <<'EOF'
**Priority:** P0 · **Severity:** High · **Type:** security · **Ref:** TD-002

### Problem
`src/api/app.py:74` installs `ProxyHeadersMiddleware(trusted_hosts=["*"])`, so any
client-supplied `X-Forwarded-For` is trusted, and `src/api/rate_limit.py:7-9`
uses an in-memory (`memory://`) SlowAPI store that resets on deploy and is
per-replica.

### Impact
Per-IP limits (including the global 30/min) are trivially bypassed by rotating a
spoofed forwarded IP, and effective limits are multiplied by replica count.

### Fix
- Restrict trusted proxies to the known ingress host(s) instead of `*`.
- Move the limiter store to a shared backend (e.g. Redis) so limits hold across replicas/deploys.

### Acceptance criteria
- Forwarded-IP spoofing no longer resets the rate-limit bucket.
- Limits are enforced consistently across replicas.
EOF
)$FOOTER"

create_issue "P0: Mini App bound-token path skips membership authorization (TD-004)" \
  "priority:critical,security,auto-audit" \
  "$(cat <<'EOF'
**Priority:** P0 · **Severity:** High · **Type:** security (authz) · **Ref:** TD-004

### Problem
`src/api/routes/onboarding/helpers.py:70-83`: when initData carries a bound
`chat_id`, authorization stops at the cryptographic binding and never calls
`MembershipService.is_active_member()`. Membership rows are only auto-created on
bot interaction, not on WebApp open.

### Impact
A Telegram group member who never interacted with the bot can open that
instance's dashboard, and **revoked** members retain access until token TTL
expiry (up to 1h) because the bound path performs no membership re-check.

### Fix
- Require an active-membership check on **all** authenticated paths (bound and unbound).
- Re-check membership on each request rather than trusting the token binding alone.

### Acceptance criteria
- Non-member with valid bound initData is rejected.
- Deactivated member is rejected immediately, not after TTL.
- Tests cover both.
EOF
)$FOOTER"

create_issue "P0: remove-account is deployment-wide, not chat-scoped (TD-005)" \
  "priority:critical,security,auto-audit" \
  "$(cat <<'EOF'
**Priority:** P0 · **Severity:** High · **Type:** security (cross-tenant) · **Ref:** TD-005

### Problem
`src/api/routes/onboarding/settings.py:155-167` validates membership for
`request.chat_id`, but `InstagramAccountService.deactivate_account(account_id)`
(`instagram_account_service.py:501-531`) soft-deletes the account for the **entire
deployment**.

### Impact
Any authorized member of one chat can disable an Instagram account used by other
chats/instances — a cross-tenant destructive action.

### Fix
- Scope deactivation to the requesting chat (per-chat account link), or
- Gate the global deactivation behind account ownership + admin role.

### Acceptance criteria
- A member of chat A cannot affect account usage in chat B.
- Regression test asserts cross-instance isolation.
EOF
)$FOOTER"

create_issue "P0: Media repository write paths bypass the tenant filter (TD-030)" \
  "priority:critical,security,bug,auto-audit" \
  "$(cat <<'EOF'
**Priority:** P0 · **Severity:** High · **Type:** bug / security (IDOR) · **Ref:** TD-030

### Problem
`src/repositories/media_repository.py` mutators resolve rows by bare `media_id`
without `chat_settings_id`: `reactivate`, `update_metadata`,
`increment_times_posted`, etc. (lines 183-189, 214-227, 370-388, 392-398,
424-432, 470-476, 484-488) and `deactivate_by_ids` (800-809).

### Impact
Knowing (or guessing) a media UUID allows cross-tenant reads/writes, bypassing
the multi-tenant boundary that the API layer tries to enforce.

### Fix
- Thread `chat_settings_id` through all media mutators and filter on it.
- Consider enforcing tenant scoping at the repository base so `None` cannot silently mean "all tenants" (see EPIC-A / TD-030).

### Acceptance criteria
- Every media mutator requires/honors a tenant scope.
- Tests verify a foreign `chat_settings_id` cannot mutate another tenant's media.
EOF
)$FOOTER"

create_issue "P0: Drive factory falls back to the global service account on any OAuth error (TD-056)" \
  "priority:critical,security,auto-audit" \
  "$(cat <<'EOF'
**Priority:** P0 · **Severity:** High · **Type:** arch / security (cross-tenant) · **Ref:** TD-056

### Problem
`src/services/media_sources/factory.py:86-99` wraps per-tenant Google Drive OAuth
in a broad `except Exception` and, on **any** failure, falls back to the global
service-account credentials. Related: `media_sync.py:403-404` uses
`TELEGRAM_CHANNEL_ID` as the tenant fallback for Drive OAuth lookups.

### Impact
A tenant's transient/auth error can silently switch media sourcing to a global
account, potentially crossing tenant boundaries and masking the real auth error.

### Fix
- Narrow the fallback (only when no per-tenant OAuth is configured, never on auth failure).
- Never cross tenant boundaries on error; surface the real error.
- Fix the `TELEGRAM_CHANNEL_ID` tenant-id namespace mismatch.

### Acceptance criteria
- OAuth failure for a tenant does not fall back to the service account.
- Test asserts no cross-tenant provider is returned on error.
EOF
)$FOOTER"

# =====================================================================
# P1 — High (individual)
# =====================================================================

create_issue "P1: Autopost success recording is non-atomic (TD-010)" \
  "priority:high,bug,auto-audit" \
  "$(cat <<'EOF'
**Priority:** P1 · **Severity:** High · **Type:** bug (data integrity) · **Ref:** TD-010

### Problem
`src/services/core/telegram_autopost.py:475-505` records a successful post via
separate commits (history, media counter, lock, queue delete, user stats). A
partial failure can duplicate history or delete the queue row without creating a
lock. The manual callback path fakes atomicity with a shared-session hack; autopost does not.

### Fix
Wrap the whole "publish success" write in a single transaction — ideally a shared
transactional publish service reused by all posting paths (see EPIC-B / TD-012).

### Acceptance criteria
- All success-side writes commit or roll back together.
- Failure-injection test proves no partial state remains.
EOF
)$FOOTER"

create_issue "P1: Autopost failure/cancel leaves the queue row stuck in processing (TD-011)" \
  "priority:high,bug,auto-audit" \
  "$(cat <<'EOF'
**Priority:** P1 · **Severity:** High · **Type:** bug · **Ref:** TD-011

### Problem
`src/services/core/telegram_autopost.py:244-346, 591-635`: only the daily-cap
path restores the row to `pending`. Safety-check failure, user cancel, dry-run
exit, upload cancel, and generic errors leave the item claimed in `processing`
until `requeue_stale_processing` (~10 min).

### Fix
Restore the row to `pending` on **every** non-success exit path (a `finally`/
context-manager around the claim).

### Acceptance criteria
- Each failure/cancel path releases the claim immediately.
- Tests cover cancel, dry-run, safety-check fail, and generic error.
EOF
)$FOOTER"

create_issue "P1: Callback queue-completion monkey-patches session.commit (TD-012)" \
  "priority:high,bug,architecture,auto-audit" \
  "$(cat <<'EOF'
**Priority:** P1 · **Severity:** High · **Type:** bug / arch · **Ref:** TD-012

### Problem
`src/services/core/telegram_callbacks_core.py:89-198`: `_shared_session` replaces
`session.commit` with `flush` to fake atomicity across five repos, and the
queue-completion business logic (cap guard, history/lock creation, queue delete,
user stats) lives in a callback utility rather than a reusable service.

### Fix
Replace the monkey-patch with a real Unit-of-Work / transactional publish service
(shared with autopost — EPIC-B). Move business logic out of the callback layer.

### Acceptance criteria
- No runtime patching of `session.commit`.
- One publish service is used by manual, autopost, and scheduler paths.
EOF
)$FOOTER"

create_issue "P1: Autopost operation locks are process-local (double-post risk) (TD-014)" \
  "priority:high,architecture,auto-audit" \
  "$(cat <<'EOF'
**Priority:** P1 · **Severity:** High · **Type:** arch · **Ref:** TD-014

### Problem
`src/services/core/telegram_operation_state.py:14-33` and
`telegram_autopost.py:84,119-158` keep operation locks and cancel flags in
in-memory dicts. These are useless across replicas and lost on restart.

### Impact
A second worker, or a restart mid-autopost, can double-post.

### Fix
Move coordination to the DB (e.g. `claim_for_processing` + a lease/heartbeat) so
locking is shared and restart-safe.

### Acceptance criteria
- Concurrent workers cannot both process the same queue item.
- Restart mid-operation does not orphan or duplicate.
EOF
)$FOOTER"

create_issue "P1: Cross-tenant hash-lock exclusion in media eligibility (TD-020)" \
  "priority:high,bug,auto-audit" \
  "$(cat <<'EOF'
**Priority:** P1 · **Severity:** High · **Type:** bug (cross-tenant) · **Ref:** TD-020

### Problem
`src/repositories/media_repository.py:637-648`: `_apply_eligibility_filters()`
tenant-scopes the queue/lock subqueries but `locked_hashes_subquery` has **no**
tenant filter, so Tenant A's locked hashes wrongly exclude eligible media in
Tenant B.

### Fix
Add `chat_settings_id` scoping to the locked-hashes subquery.

### Acceptance criteria
- Integration test (real Postgres) proves Tenant A locks do not affect Tenant B eligibility.
EOF
)$FOOTER"

create_issue "P1: Cross-tenant duplicate-hash groups (TD-021)" \
  "priority:high,bug,auto-audit" \
  "$(cat <<'EOF'
**Priority:** P1 · **Severity:** High · **Type:** bug (cross-tenant) · **Ref:** TD-021

### Problem
`src/repositories/media_repository.py:769-778`: `get_duplicate_hash_groups()`
tenant-filters the inner subquery but the outer `query(MediaItem)` join has no
`chat_settings_id`, returning duplicate groups across all tenants (drives
`dedup-media` across tenant boundaries).

### Fix
Apply the tenant filter to the outer query as well.

### Acceptance criteria
- Dedup groups are scoped to one tenant; test proves isolation.
EOF
)$FOOTER"

create_issue "P1: Token UPSERT ignores auth_method and overwrites the wrong credential (TD-025)" \
  "priority:high,bug,auto-audit" \
  "$(cat <<'EOF'
**Priority:** P1 · **Severity:** High · **Type:** bug (credentials) · **Ref:** TD-025

### Problem
`src/repositories/token_repository.py:194-202` keys `create_or_update()` on
`(service, type, account_id)` only. After migration 040 an account can hold both
`instagram_login` and `fb_login` tokens, so refresh/reconnect can clobber the
wrong row. Also `token_refresh.py:147-149,217-302` locks/refreshes the wrong row
and `refresh_all_instagram_tokens()` refreshes the same account twice; backfill
(`instagram_backfill.py:232-234`) omits the `auth_method` filter.

### Fix
Include `auth_method` in the UPSERT key and in all token lookups/refresh paths.

### Acceptance criteria
- Dual-`auth_method` accounts refresh independently against the correct endpoints.
- Test covers two tokens on one account.
EOF
)$FOOTER"

create_issue "P1: Scheduler selection/preview/availability not tenant-scoped (TD-031)" \
  "priority:high,bug,auto-audit" \
  "$(cat <<'EOF'
**Priority:** P1 · **Severity:** High · **Type:** bug (cross-tenant) · **Ref:** TD-031

### Problem
`src/services/core/scheduler.py`: `_select_media_from_pool()` /
`_pick_category_for_slot()` (312, 792, 878-879), `get_queue_preview()` (217-234,
ignores its `telegram_chat_id` arg), and `check_availability()` (271-277) omit
`chat_settings_id`, so media/category/preview/lock checks can bleed across tenants.

### Fix
Thread `chat_settings_id` through selection, category pick, preview, and availability.

### Acceptance criteria
- Tests assert `chat_settings_id` flows through each path and that previews are tenant-isolated.
EOF
)$FOOTER"

create_issue "P1: Backfill downloader mutates ORM and commits directly (TD-040)" \
  "priority:high,bug,architecture,auto-audit" \
  "$(cat <<'EOF'
**Priority:** P1 · **Severity:** High · **Type:** arch / bug · **Ref:** TD-040

### Problem
`src/services/integrations/backfill_downloader.py:114-128`: after
`media_repo.create()` it sets fields on the ORM object and calls
`media_repo.db.commit()` directly, bypassing repository methods and transaction
boundaries.

### Fix
Add a proper repository method for the post-create update and commit through it.

### Acceptance criteria
- Downloader no longer touches `media_repo.db` directly.
EOF
)$FOOTER"

create_issue "P1: Backfill hashing uses SHA256 while the rest of the system uses MD5 (TD-046)" \
  "priority:high,bug,auto-audit" \
  "$(cat <<'EOF'
**Priority:** P1 · **Severity:** High · **Type:** bug · **Ref:** TD-046

### Problem
`src/services/integrations/backfill_downloader.py:108-128` indexes `file_hash` as
SHA256, but `utils/file_hash.py`, `LocalMediaProvider`, and Drive `md5Checksum`
use MD5 (also `google_drive_provider.py:290-294` switches algorithms on fallback).
Cross-source dedup/rename detection silently fails for backfilled items, and a
current test codifies the SHA256 expectation (`test_backfill_downloader.py:164`).

### Fix
Standardize on MD5 (or a single documented algorithm) everywhere; update the test.

### Acceptance criteria
- Backfilled items dedup against synced/ingested items.
- Test asserts hash parity across sources.
EOF
)$FOOTER"

create_issue "P1: Media-sync gate opens even when every tenant sync failed (TD-049)" \
  "priority:high,bug,auto-audit" \
  "$(cat <<'EOF'
**Priority:** P1 · **Severity:** High · **Type:** bug · **Ref:** TD-049

### Problem
`src/services/core/loops/media_sync_loop.py:45-87`: per-chat `except` logs and
continues, but the outer block still sets `initial_sync_complete = True`, letting
the scheduler post against empty/stale media when all tenant syncs failed.
Per-chat errors also don't affect `consecutive_failures`/alerts.

### Fix
Only open the gate when at least one tenant sync succeeded; count per-chat failures.

### Acceptance criteria
- All-tenant sync failure keeps the gate closed and alerts.
- Test covers total-failure and partial-failure.
EOF
)$FOOTER"

create_issue "P1: Media locks created without chat_settings_id (TD-050)" \
  "priority:high,bug,auto-audit" \
  "$(cat <<'EOF'
**Priority:** P1 · **Severity:** High · **Type:** bug (cross-tenant) · **Ref:** TD-050

### Problem
`src/services/core/media_lock.py:83-99`: `create_lock()` takes `telegram_chat_id`
for TTL lookup but never passes `chat_settings_id` to `lock_repo.create()`, so
locks are globally scoped and the audit row gets a null tenant. `is_locked()`
(79-81, 136-138) is likewise not tenant-aware.

### Fix
Resolve and persist `chat_settings_id` on lock create; make `is_locked()` tenant-aware.

### Acceptance criteria
- Locks and their audit rows carry the correct tenant.
- Tests cover per-chat TTL and tenant propagation.
EOF
)$FOOTER"

create_issue "P1: Telegram notifications always target the env channel (TD-052)" \
  "priority:high,bug,telegram,auto-audit" \
  "$(cat <<'EOF'
**Priority:** P1 · **Severity:** High · **Type:** bug (multi-tenant) · **Ref:** TD-052

### Problem
`src/services/core/telegram_notification.py:88-98,133-162`: `send_notification`
always uses `self.service.channel_id` (`TELEGRAM_CHANNEL_ID`) for settings,
account lookup, send, and tracking. In a multi-instance deployment every post
goes to the single env channel with the wrong tenant's settings. It also always
sends as a photo (video misbehaves).

### Fix
Derive the destination chat and settings from the queue item's tenant; branch on media type for photo vs video.

### Acceptance criteria
- Notification is delivered to the queue item's tenant chat with that tenant's settings.
- Video media is sent correctly. Test asserts destination.
EOF
)$FOOTER"

create_issue "P1: Prefix lookups are global (cross-tenant queue item/account) (TD-060)" \
  "priority:high,bug,telegram,auto-audit" \
  "$(cat <<'EOF'
**Priority:** P1 · **Severity:** High · **Type:** bug (cross-tenant) · **Ref:** TD-060

### Problem
`src/services/core/telegram_accounts.py:338-346,411-414` call
`get_by_id_prefix()` / `get_account_by_id_prefix()` with no tenant filter, so an
8-char UUID prefix can resolve to another instance's queue item or account during
inline posting. Also `queue_repository.py:60-82`, `history_repository.py:236-248`.

### Fix
Scope all prefix lookups by `chat_settings_id`.

### Acceptance criteria
- Prefix resolution cannot return another tenant's row; test proves it.
EOF
)$FOOTER"

create_issue "P1: Global pause uses TELEGRAM_CHANNEL_ID, not the acting chat (TD-061)" \
  "priority:high,bug,telegram,auto-audit" \
  "$(cat <<'EOF'
**Priority:** P1 · **Severity:** High · **Type:** bug (multi-tenant) · **Ref:** TD-061

### Problem
`src/services/core/telegram_service.py:155-163`: `is_paused`/`set_paused` always
read/write `settings_service.get_settings(self.channel_id)`. Resume callbacks
(`telegram_callbacks_admin.py:204,224,240`) therefore unpause the env channel, not
the chat that pressed the button.

### Fix
Pass the acting chat id into pause/resume and scope settings to it.

### Acceptance criteria
- Pausing/resuming affects only the acting chat. Test asserts target chat.
EOF
)$FOOTER"

create_issue "P1: Adopt a real migration tool (Alembic) (TD-070)" \
  "priority:high,tech-debt,auto-audit" \
  "$(cat <<'EOF'
**Priority:** P1 · **Severity:** High · **Type:** tech-debt (foundation) · **Ref:** TD-070 · **Epic:** EPIC-C

### Problem
Alembic is declared in `setup.py` but there is no `alembic.ini` / `migrations/`.
Production schema is 41 hand-written `scripts/migrations/*.sql` applied manually
via `psql`, with no in-repo runner.

### Fix
Introduce Alembic (or a scripted, CI-applied runner), import existing SQL as a
baseline, and wire migration apply into deploy + CI.

### Acceptance criteria
- Schema changes are reproducible and version-controlled via the tool.
- CI applies migrations to a fresh DB.
EOF
)$FOOTER"

create_issue "P1: Prod/test schema divergence (create_all vs SQL migrations) (TD-071)" \
  "priority:high,tech-debt,auto-audit" \
  "$(cat <<'EOF'
**Priority:** P1 · **Severity:** High · **Type:** tech-debt · **Ref:** TD-071 · **Epic:** EPIC-C

### Problem
`config/database.py:65-87` and `tests/conftest.py:99-100` build the schema from
ORM `create_all()`, so partial indexes / check constraints / backfills that exist
only in SQL migrations (021, 015, 014, 040) are never validated.

### Fix
Run migrations to build the test schema (depends on TD-070) and add a schema-parity check.

### Acceptance criteria
- Tests run against the migrated schema.
- A parity test flags ORM-vs-migration drift.
EOF
)$FOOTER"

create_issue "P1: Failed token refresh leaves a FOR UPDATE lock open (TD-078)" \
  "priority:high,bug,auto-audit" \
  "$(cat <<'EOF'
**Priority:** P1 · **Severity:** High · **Type:** bug · **Ref:** TD-078

### Problem
`src/services/integrations/token_refresh.py:247-342`: after
`get_token_for_update()` succeeds, API failure / `TokenRevokedError` / network
errors return or raise without `rollback()`, holding a `FOR UPDATE` lock and
blocking other workers via `SKIP LOCKED`. A silent skip is also counted as a
failure by callers (344-413).

### Fix
Ensure every error path rolls back / releases the row lock; distinguish "skipped
(locked elsewhere)" from "failed".

### Acceptance criteria
- Failed refresh releases the lock; test asserts no lingering transaction.
EOF
)$FOOTER"

create_issue "P1: No migration / schema-parity tests (TD-100)" \
  "priority:high,tech-debt,auto-audit" \
  "$(cat <<'EOF'
**Priority:** P1 · **Severity:** High · **Type:** test · **Ref:** TD-100 · **Epic:** EPIC-C

### Problem
CI never applies `scripts/migrations/*.sql`; no test asserts that the migrated
schema matches the ORM, nor that constraints from migrations 021/015/040 behave
as intended (`tests/conftest.py:99-100`).

### Fix
Add CI migration apply + a schema-parity/constraint-behavior test suite
(depends on TD-070/TD-071).

### Acceptance criteria
- CI fails when a migration and the ORM drift or a documented constraint regresses.
EOF
)$FOOTER"

create_issue "P1: Repo tests are mock-only; no eligibility/concurrency coverage (TD-101)" \
  "priority:high,tech-debt,auto-audit" \
  "$(cat <<'EOF'
**Priority:** P1 · **Severity:** High · **Type:** test · **Ref:** TD-101

### Problem
Nearly all repository tests patch `Session`. Missing coverage:
`_apply_eligibility_filters` / cross-tenant exclusion against real Postgres,
`FOR UPDATE SKIP LOCKED` claim behavior, dual-`auth_method` token UPSERT
(TD-025), and no dedicated tests for `AuditRepository`, `OnboardingRepository`,
`MembershipRepository`.

### Fix
Add integration tests against a real Postgres for eligibility, concurrency, and
the missing repositories.

### Acceptance criteria
- Eligibility/tenant-isolation and claim concurrency are covered by DB-backed tests.
EOF
)$FOOTER"

# =====================================================================
# P2 — Medium correctness / hardening (clustered)
# =====================================================================

create_issue "P2 cluster: Security hardening (TD-003/006/007/008/009/017/018/019/022/023/024)" \
  "priority:medium,security,auto-audit" \
  "$(cat <<'EOF'
**Priority:** P2 · **Severity:** Medium · **Type:** security · Cluster of 11 findings.

Address as a group; each can be its own commit/PR.

- [ ] **TD-003** OAuth state tokens are replayable / not single-use; `validate_state_token()` ignores `provider` — `oauth_service.py:107-137`, `instagram_login_oauth.py:109-132`, `google_drive_oauth.py:121-145`.
- [ ] **TD-006** Cross-tenant/global data on `/accounts`, `/system-status`, `/analytics/service-health` — `dashboard.py:87-118,172-188,256-265`.
- [ ] **TD-007** OAuth `/start` accepts arbitrary `chat_id` with no caller auth (notification spam) — `oauth.py:18-32,192-206`.
- [ ] **TD-008** Missing RBAC (`instance_role`) on account management + settings mutations; shared-account credential overwrite — `settings.py:138-167,255-343`.
- [ ] **TD-009** Auth material (`init_data`) passed in query strings — all onboarding GET endpoints.
- [ ] **TD-017** No replay protection (nonce/single-use) for initData / URL tokens — `webapp_auth.py`.
- [ ] **TD-018** Access tokens in Meta API query strings; full error payloads logged/stored — `instagram_api.py:228-315`, `token_refresh.py:271-299`, `oauth_service.py:317-318`.
- [ ] **TD-019** SSRF on media download / URL validation (no allowlist, redirects on) — `backfill_downloader.py:205-247`, `instagram_credentials.py:260-272`.
- [ ] **TD-022** Encryption singleton blocks runtime key rotation; reaches into `_cipher` — `utils/encryption.py:41-48`, `oauth_service.py:122-125`.
- [ ] **TD-023** Upload trusts spoofable `Content-Length`; `/upload-media` has no dedicated rate limit — `dashboard.py:330-336,405-466`.
- [ ] **TD-024** Uploads written to ephemeral `/tmp/media/uploads` — `dashboard.py:290-293,443-444`.
EOF
)$FOOTER"

create_issue "P2 cluster: Multi-tenant & data-integrity correctness (TD-026/051/034/035/036/037/065/072/073/031b)" \
  "priority:medium,bug,auto-audit" \
  "$(cat <<'EOF'
**Priority:** P2 · **Severity:** Medium · **Type:** bug · Cluster of 10 findings.

- [ ] **TD-026** `NULL auth_method` defeats `UNIQUE(..., auth_method)` — `models/api_token.py:106-112` + migration 040.
- [ ] **TD-051** Ingestion not tenant-scoped (dup-hash check + create omit `chat_settings_id`) — `media_ingestion.py:155-186`.
- [ ] **TD-034** `chat_settings.get_or_create` race (no IntegrityError retry) — `chat_settings_repository.py:42-82`.
- [ ] **TD-035** Duplicate active TTL locks possible; `get_active_lock` returns arbitrary `.first()` — `lock_repository.py:69-93`.
- [ ] **TD-036** No uniqueness on queued `media_item_id` (double-queue race) — `models/posting_queue.py:36-39`, `scheduler.py:312-363`.
- [ ] **TD-037** Daily-cap TOCTOU + counts only `posted` + `posts_per_day==0` blocks/ZeroDivision — `daily_cap.py:16-28`, `scheduler.py:65,98,156-170`.
- [ ] **TD-065** `category_mix` lacks a single-`is_current`-per-category/tenant guarantee — `models/category_mix.py:32-44`.
- [ ] **TD-072** `init_db()` omits `onboarding_session` / `user_chat_membership` imports — `config/database.py:72-85`.
- [ ] **TD-073** Constraints/indexes exist only in SQL, not ORM `__table_args__` — media_lock/api_token/media_item/posting_history/user_chat_membership.
- [ ] **TD-031b** Nullable `chat_settings_id` on tenant-owned rows — `models/media_item.py:41-44`.
EOF
)$FOOTER"

create_issue "P2 cluster: Posting / scheduler / integration correctness (TD-042/043/044/045/047/048/090/091/083/084/013)" \
  "priority:medium,bug,auto-audit" \
  "$(cat <<'EOF'
**Priority:** P2 · **Severity:** Medium · **Type:** bug · Cluster of 11 findings.

- [ ] **TD-042** Failed sends block media for up to 24h — `scheduler.py:370-374 vs 459-461`.
- [ ] **TD-043** Instagram auto-approve failure consumes the slot — `scheduler.py:575-578`.
- [ ] **TD-044** Inconsistent video detection (`file_path` vs `mime_type`) — `scheduler.py:690-695`, `telegram_autopost.py:454-458`, `google_drive_provider.py:290-294`.
- [ ] **TD-045** Unbounded download into memory (OOM) — `backfill_downloader.py:205-247`.
- [ ] **TD-047** GIF listed as supported but rejected by IG Stories; optimize flattens to JPEG — `image_processing.py:33,122-151`.
- [ ] **TD-048** No retry/backoff on transient Instagram errors; HTTP 429 not mapped; no mid-flow refresh — `instagram_api.py:218-404`, `instagram_credentials.py:80-100`.
- [ ] **TD-090** Startup overview mis-uses admin chat id as a user id — `telegram_lifecycle.py:47-48`.
- [ ] **TD-091** `startgroup` deep-link skips bot-membership check — `start_command_router.py:81-109`.
- [ ] **TD-083** `heartbeat` reports never-started loops as healthy — `loops/heartbeat.py:41-46`.
- [ ] **TD-084** `guarded()` silently stops a loop after the restart budget — `loops/guarded.py:65-80`.
- [ ] **TD-013** Batch approve ignores daily cap; cap-restore incomplete on other failures — `telegram_callbacks_admin.py:117-132`, `telegram_callbacks_queue.py:88-100`.
EOF
)$FOOTER"

create_issue "P2 cluster: DB session & transaction correctness (TD-075/076/077/038/039)" \
  "priority:medium,bug,auto-audit" \
  "$(cat <<'EOF'
**Priority:** P2 · **Severity:** Medium · **Type:** bug · Cluster of 5 findings.

- [ ] **TD-075** `use_session()` leaks the pre-swap session/generator — `base_repository.py:181-192`.
- [ ] **TD-076** Direct `self.db.commit()` bypasses the circuit-breaker heal — `base_repository.py:74-85`.
- [ ] **TD-077** Open read transactions left dangling (`get_pending` FOR UPDATE, token reads, queue cleanup loop) — `queue_repository.py:97-116`, `token_repository.py:424-446`, `loops/queue_cleanup_loop.py:24-32`, `telegram_service.py:372-434`.
- [ ] **TD-038** Mixed naive/aware datetimes feeding TZ-aware columns — repos + models (standardize on `datetime_utils.ensure_utc()`).
- [ ] **TD-039** `delete_stale()` loads all rows then deletes (use `DELETE ... RETURNING`) — `queue_repository.py:254-295`.
EOF
)$FOOTER"

# =====================================================================
# P3 — Medium architecture / debt (clustered)
# =====================================================================

create_issue "P3 cluster: Architecture & layering (TD-032/033/053/054/055/057/058/074/079/059)" \
  "priority:medium,architecture,tech-debt,auto-audit" \
  "$(cat <<'EOF'
**Priority:** P3 · **Severity:** Medium · **Type:** arch / debt · Cluster of 10 findings. See EPIC-E.

- [ ] **TD-032** `TelegramService` is a repository hub / God-facade; handlers use repos directly and pass repos into services — `telegram_service.py:66-115` and many handlers.
- [ ] **TD-033** Split, fragile callback routing + eager `query.answer()` dropping toasts — `telegram_service.py:265-434` and account/settings handlers.
- [ ] **TD-053** API routes & loops reach into repositories directly — `dashboard.py`, `settings.py`, `cloud_cleanup_loop.py`, `scheduler_loop.py`.
- [ ] **TD-054** Scheduler couples to worker loop internals (`session_state`, inline `MediaLockService`) — `scheduler.py:302-305,611-613`, `loops/lifecycle.py:20-21`.
- [ ] **TD-055** `posting.py` and loops build raw `telegram.Bot` for alerts; alert dedup only after success — `posting.py:55-94`, `scheduler_loop.py:114-272`, `guarded.py:121-124`.
- [ ] **TD-057** Unrelated onboarding cleanup embedded in the scheduler loop — `scheduler_loop.py:355-368`.
- [ ] **TD-058** `MembershipRepository` composes a second repo/session; audit not atomic — `membership_repository.py:13-26,88-131`.
- [ ] **TD-074** Per-repo lazy session instead of a Unit-of-Work — `base_repository.py:29-72` (root cause of TD-012).
- [ ] **TD-079** Pool sizing vs per-repo sessions & `expire_on_commit=False` — `settings.py:25-26`, `database.py:33-37`.
- [ ] **TD-059** Monolithic repositories/services with embedded query logic — `media_repository.py`, `history_repository.py`, `token_refresh.py`, `scheduler.py`, `telegram_commands.py`.
EOF
)$FOOTER"

create_issue "P3 cluster: Multi-worker / process-local state (TD-015/016/041/062)" \
  "priority:medium,architecture,tech-debt,auto-audit" \
  "$(cat <<'EOF'
**Priority:** P3 · **Severity:** Medium · **Type:** arch / debt · Cluster of 4 findings. See EPIC-D.

- [ ] **TD-015** Operation-state dicts grow unbounded if `cleanup()` is skipped — `telegram_operation_state.py:14-16`.
- [ ] **TD-016** Membership cache can go stale — `telegram_user_manager.py:23-62`; account-info cache — `instagram_credentials.py:29-30`.
- [ ] **TD-041** In-memory consecutive-failure / throttle counters — `scheduler.py:396-397`, `token_refresh.py:55-56`, `scheduler_loop.py:80-83`.
- [ ] **TD-062** Settings-edit & onboarding state in `context.user_data` (lost on restart) — `telegram_settings.py:248-255,385-411`, `telegram_membership.py:138-184`.
EOF
)$FOOTER"

create_issue "P3 cluster: Telegram UX & duplication debt (TD-086/087/089/092)" \
  "priority:medium,telegram,tech-debt,auto-audit" \
  "$(cat <<'EOF'
**Priority:** P3 · **Severity:** Medium · **Type:** debt / overcomplex · Cluster of 4 findings.

- [ ] **TD-086** `/cleanup` blocks the update pipeline for 5s (`asyncio.sleep(5)`) — `telegram_commands.py:464-467`.
- [ ] **TD-087** Duplicated instance-list UI across 3+ sites and duplicated keyboard/caption rebuild — `telegram_commands.py:57-97,707-755`, `start_command_router.py:266-302`, `telegram_callbacks_queue.py:213-405`.
- [ ] **TD-089** Onboarding auto-link uses a fixed 2s sleep — `telegram_membership.py:76-85`.
- [ ] **TD-092** Unbounded Telegram fan-out without rate-limit wrapper — `telegram_accounts.py:537-607`.
EOF
)$FOOTER"

create_issue "P3 cluster: Test coverage gaps (TD-102/103/104)" \
  "priority:medium,tech-debt,auto-audit" \
  "$(cat <<'EOF'
**Priority:** P3 · **Severity:** Medium · **Type:** test · Cluster of 3 findings.

- [ ] **TD-102** Missing security/authz tests: `validate_url_token`, future-`auth_date`, bound-token-without-membership, post-revocation access, cross-instance `remove-account`, factory OAuth fallback, RBAC; rate limits disabled in all API tests — `tests/src/api/conftest.py:15-20`.
- [ ] **TD-103** Missing tenant-scoping tests across services (scheduler selection/preview, media-lock create, notification target, all-tenant sync failure).
- [ ] **TD-104** Untested cross-cutting utilities & loops: `CircuitBreaker`, `cloud_storage.get_story_optimized_url()`, `telegram_operation_state`, cleanup loops.
EOF
)$FOOTER"

# =====================================================================
# P4 — Low (clustered)
# =====================================================================

create_issue "P4 cluster: Low-severity cleanup (TD-064/063b)" \
  "priority:low,tech-debt,auto-audit" \
  "$(cat <<'EOF'
**Priority:** P4 · **Severity:** Low · **Type:** debt · Cluster of 2 findings.

- [ ] **TD-064** Global process-wide DB circuit breaker (one tenant's outage opens it for all) — `resilience.py:128-133`, `base_repository.py:46-51`.
- [ ] **TD-063b** Enum-as-string throughout (status/role/lock_reason/interaction_type) instead of native/Python enums — models.
EOF
)$FOOTER"

# =====================================================================
# Nice-to-have (clustered)
# =====================================================================

create_issue "Nice-to-have cluster: Developer experience & enhancements (TD-080/081/082/085/088)" \
  "enhancement,auto-audit" \
  "$(cat <<'EOF'
**Priority:** Nice-to-have · **Type:** enhancement / cleanup · Cluster of 5 findings.

- [ ] **TD-080** Two HTTP servers (raw-socket health server + FastAPI `/health`); consolidate — `src/main.py:29-87`.
- [ ] **TD-081** Empty `src/services/domain/` package; remove or populate.
- [ ] **TD-082** No `pyproject.toml`; adopt PEP 621 packaging and pin ruff config.
- [ ] **TD-085** Sleep-before-first-run in cleanup loops; run once on startup — `queue_cleanup_loop.py:25-26`, `lock_cleanup_loop.py:17-18`.
- [ ] **TD-088** Dead code / fragile heuristics: `_get_header_emoji`, `ADD_ACCOUNT_KEYS`, `_allocate_slots_to_categories`, unused `SCHEDULE_JITTER_MINUTES`, unused `TokenRefreshService`, `_TERMINAL_CAPTION_PREFIXES`.
EOF
)$FOOTER"

echo ""
if $DRY_RUN; then
  echo "Dry-run complete: $COUNT issues would be created. Re-run with --confirm to file them."
else
  echo "Done: $COUNT issues created."
fi
