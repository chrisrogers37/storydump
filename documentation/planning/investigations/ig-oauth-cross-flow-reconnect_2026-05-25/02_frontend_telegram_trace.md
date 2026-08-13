# Frontend & Telegram Bot Flow Trace

| Field | Value |
|---|---|
| Track | Frontend + Telegram bot analysis |
| Investigator | virgil |
| Date | 2026-05-25 |
| Parent | `00_INVESTIGATION.md` |

---

## Trace 1: "Retry Auto Post" Button

**Full chain:**

1. **Button creation** — `src/services/core/telegram_utils.py`: `build_queue_action_keyboard()` emits an inline keyboard button with `callback_data=f"autopost:{queue_id}"`. Only shown when `enable_instagram_api=True` and `error_recovery=True`.

2. **Dispatch** — `src/services/core/telegram_service.py`: callback query dispatch table maps prefix `"autopost"` to `self.autopost.handle_autopost`.

3. **Handler** — `src/services/core/telegram_autopost.py`: `handle_autopost()`:
   - Acquires a per-queue-item lock
   - Removes inline keyboard buttons (prevents double-tap)
   - Spawns `_autopost_background()` as a background task
   - `_autopost_background()` calls `_do_autopost()`: safety checks (queue item exists, not already posted, chat has IG API enabled) → Cloudinary upload → `instagram_service.post_story()` → record posting history + cleanup queue item
   - On failure: `_handle_autopost_error()` rebuilds the keyboard with `error_recovery=True`, so the user can retry again

4. **Where it fails today** — `instagram_service.post_story()` calls `_check_response_errors()` which sees Meta error code 190 "Cannot parse access token" and raises `TokenExpiredError`. The autopost error handler catches this and shows the error message to the user in Telegram.

**Key observation:** The "Retry Auto Post" button will keep failing until Bug A (unparseable token) is resolved. Each retry hits the same code 190 error.

---

## Trace 2: Settings Page Reconnect Flow

**Full chain:**

1. **Frontend trigger** — `landing/src/components/dashboard/settings/accounts-tab.tsx`: `connectInstagram()` calls `openOAuthWindow("instagram")`. Uses a `visibilitychange` event listener to call `router.refresh()` when the user returns from the OAuth tab.

2. **OAuth URL generation** — `landing/src/lib/dashboard-api.ts`: `openOAuthWindow(provider)` fetches `GET /api/dashboard/oauth-url/{provider}`, extracts `auth_url` from the JSON response, opens it in a new browser tab via `window.open()`.

3. **Backend URL endpoint** — `src/api/routes/onboarding/setup.py`: `/oauth-url/{provider}` endpoint checks for `INSTAGRAM_APP_ID` env var. If present, uses `InstagramLoginOAuthService.generate_authorization_url()`. If absent, falls back to legacy `OAuthService`.

4. **Authorization URL** — `src/services/integrations/instagram_login_oauth.py`: `generate_authorization_url()` builds URL to `https://api.instagram.com/oauth/authorize` with `client_id`, `redirect_uri`, `response_type=code`, `scope=instagram_basic,instagram_content_publish,instagram_manage_insights`, and a CSRF state token stored in DB.

5. **Callback** — `src/api/routes/oauth.py`: `/auth/instagram-login/callback` validates the state token, then calls `exchange_and_store()`.

6. **Token exchange** — `instagram_login_oauth.py`: `exchange_and_store()` does a 4-step flow:
   - Exchange authorization code for short-lived token
   - Exchange short-lived for long-lived token (60-day expiry)
   - Fetch Instagram username via Graph API
   - Call `account_service.find_existing_account_for_oauth()` to locate existing account

7. **Account lookup (the break point)** — `src/services/core/instagram_account_service.py`: `find_existing_account_for_oauth()` does a 3-tier lookup:
   - Tier 1: `api_tokens.meta_account_id` match (via join)
   - Tier 2: `instagram_accounts.instagram_account_id` match
   - Tier 3: `instagram_accounts.instagram_username` match

   **Production behavior for legacy FB-Login accounts:** Tier 1 fails because `meta_account_id` was backfilled from `instagram_account_id` (which holds the FB-flow Meta ID, not the IG Login `user_id`). Tier 2 also fails for the same reason. Tier 3 (username fallback) was removed by PR #408. Code falls through to `add_account()` which hits `_validate_new_account()` → `ValueError: Account @gatortails already exists`.

8. **Error rendering** — `oauth.py`: The catch-all exception handler in the callback route renders `_error_html_page()` with title "Connection Failed" and message "Something went wrong connecting your Instagram account. Please try again from Telegram."

**Current state of the code (local branch):** The username fallback (Tier 3) appears to have been re-added in the local codebase via `find_existing_account_for_oauth()`. However, the deployed production code at commit `6ca43d3` does NOT have this fallback — it was removed in PR #408. The fix in `01_oauth_cross_flow_fallback.md` addresses this.

---

## Trace 3: Telegram Bot Posting Flow

**Full chain from scheduled post to Instagram API:**

1. **Scheduler tick** — `src/services/core/loops/scheduler_loop.py`: runs every 60 seconds, checks `posting_queue` for items due.

2. **Credentials loading** — `src/services/integrations/instagram_credentials.py`: `get_active_account_credentials()`:
   - Loads the active Instagram account for the chat
   - Gets the token record from `api_tokens`
   - Checks `is_expired` (compares `expires_at` against `datetime.utcnow()`)
   - Decrypts token via `src/utils/encryption.py` (MultiFernet with key fallback)

3. **Token refresh** — `src/services/integrations/token_refresh.py`: attempts refresh if token is within refresh window. Detects revocation via Meta error subcodes 458 (user changed password), 460 (session invalidated), 467 (access token invalid).

4. **Story posting** — `src/services/integrations/instagram_api.py`: `post_story()` does 3-step Meta Graph API flow:
   - Step 1: Create media container (`POST /{ig_user_id}/media` with `image_url` + `media_type=STORIES`)
   - Step 2: Poll container status until `FINISHED`
   - Step 3: Publish (`POST /{ig_user_id}/media_publish` with `creation_id`)

5. **Error handling** — `_check_response_errors()`: maps error responses:
   - Code 190 + subcodes {458, 460, 467} → `TokenRevokedError`
   - Code 190 without recognized subcode → `TokenExpiredError`
   - Other errors → generic `InstagramAPIError`

6. **Scheduler error handling** — `scheduler_loop.py` catches `TokenRevokedError` specifically (logs + potentially pauses). `TokenExpiredError` is caught more generically.

---

## Trace 4: Is the Error Message Misleading?

**The user reports:** "Instagram connection has expired. Please reconnect your account in Settings."

**What actually exists in code:**

| Exception | Message | File |
|---|---|---|
| `TokenExpiredError` | "Instagram access token has expired" | `src/exceptions/instagram.py` |
| `TokenRevokedError` | "Instagram account has been disconnected. Please reconnect." | `src/exceptions/instagram.py` |
| OAuth callback catch-all | "Something went wrong connecting your Instagram account. Please try again from Telegram." | `src/api/routes/oauth.py` |

**The exact phrase "Instagram connection has expired. Please reconnect your account in Settings." was NOT found in any backend source file.** It may originate from:

- The Telegram bot's error formatting layer (where the exception message gets wrapped into a user-facing Telegram message with additional context)
- The Mini App frontend (dashboard UI)
- A slight paraphrase by the user when reporting the error

**The actual failure is NOT token expiration.** The token's `expires_at` is `2026-07-17` — nearly two months away. Meta is returning code 190 "Cannot parse access token", which the error-handling code maps to `TokenExpiredError` because it has code 190 but no recognized subcode (458/460/467). This is a **misleading error classification** — "cannot parse" is not "expired."

**Recommendation:** The `_check_response_errors()` mapping should distinguish "Cannot parse" (code 190, no subcode) from actual expiration. Consider a separate `TokenCorruptedError` or at minimum a different user-facing message that doesn't suggest waiting will fix it.

---

## Summary of Findings

1. **"Retry Auto Post"** will keep failing — it goes straight to `post_story()` which hits the same unparseable token (Bug A). No reconnect logic in the retry path.

2. **Settings reconnect flow** is architecturally sound but broken in production by the missing username fallback (Bug B, PR #408). The OAuth URL generation, state token CSRF, and token exchange all work correctly. The failure is in account matching on return.

3. **Telegram posting flow** works correctly end-to-end when the token is valid. The failure is purely at the Meta API boundary due to the unparseable token.

4. **The error message is misleading.** The token is not expired (expires 2026-07-17). Meta can't parse it (code 190). The error-handling code misclassifies this as `TokenExpiredError` because it's a code-190 without a recognized revocation subcode. The user sees messaging about reconnecting, but reconnecting is itself broken (Bug B).

**Both bugs must be fixed in sequence:** F1 (username fallback) unblocks reconnect, which lets the user get a fresh token, which should resolve Bug A if it was a one-off corruption.
