# Investigation: Instagram Posting Outage + OAuth Reconnect "Connection Failed"

| Field | Value |
|---|---|
| Date | 2026-05-25 |
| Triggered by | User report: "still getting served content, but still cannot post it to instagram. I have @gatortails attached to my account. I just tried re-connecting and got hit with Connection Failed." |
| Deployed prod commit | `6ca43d3` (main, PR #435 — `fix: align dry_run_mode model default with code default (False)`) |
| Local branch at time of investigation | `fix/health-check-starts-first-emergency` (16+ commits behind deployed main — important: don't use local source as ground truth) |
| Affected accounts | `@gatortails` (Telegram chat `-1003688539654`) and `@thursday.lines` (both legacy FB-Login era rows) |
| Investigator | Claude (via `/claudna:investigate-app`) |
| Plan source | `/Users/chris/.claude/plans/unified-booping-lamport.md` |

---

## TL;DR

Two independent bugs in production, tightly coupled by user experience:

- **Bug A** — every `instagram_api` post has failed since **2026-05-19 14:10** with Meta error `code: 190 Invalid OAuth access token - Cannot parse access token`. Last successful post: `2026-05-12 23:51`. Stored token value is unparseable by Meta, not expired. 14 consecutive failures captured in `service_runs`.
- **Bug B** — the user's normal recovery (Telegram → Instagram Login → Reconnect) always renders the generic "Connection Failed" page. Real cause: **PR #408 (credential refactor phase 3) removed PR #378's username fallback**, and migration 036 backfilled `api_tokens.meta_account_id` from `instagram_accounts.instagram_account_id` — which for these legacy FB-Login rows stores the wrong Meta-side ID. The IG Login `user_id` returned by Meta today doesn't match either lookup, so `get_account_by_meta_id` returns None → code hits `add_account` → `_validate_new_account` finds the existing username → `ValueError`.

Bug B is hiding Bug A: the user can't reconnect to refresh the broken token, because the reconnect codepath is broken too. **Fixing Bug B (F1 below) unblocks self-service recovery for Bug A.**

---

## Context

The user reports two symptoms on production:

1. **Posting broken** — `@gatortails` chat is still being *served* content (Telegram notifications go out), but nothing reaches Instagram.
2. **Reconnect broken** — Telegram → Instagram Login → `storyline-ai-production.up.railway.app/auth/instagram-login/callback` returns "Connection Failed — Something went wrong connecting your Instagram account. Please try again from Telegram."

The user took a screenshot of the Connection Failed page during the failed reconnect attempt and shared it. They confirmed they did not knowingly rotate `ENCRYPTION_KEY` or `INSTAGRAM_APP_SECRET` around the 2026-05-19 timeframe.

---

## Investigation Steps

### Platform detection

- **Railway**: project linked to `Christopher Rogers's Projects / storydump / production`. Services: `worker` and `storydump` (API). Both at deployed commit `6ca43d3` on `main`, deployed `2026-05-22 16:00 ET`.
- **Database**: Neon Postgres. `DATABASE_URL` pulled via `railway variables --service storydump`. **The local `.env` `DB_HOST` points at a stale dev DB (latest `posting_history.posted_at = 2026-02-25`); do not use it for production debugging.**

### Evidence gathering

**Storydump API logs (Railway, 2026-05-25 12:48–12:50 window)** — all three reconnect attempts in this window:

```
12:48:11  INFO   [InstagramLoginOAuthService.exchange_and_store] Starting execution
12:48:12  ERROR  [InstagramAccountService.add_account] Failed after 39ms:
                 Account @thursday.lines already exists as 'TL'
12:48:12  ERROR  [InstagramLoginOAuthService.exchange_and_store] Failed after 1585ms
            File "/app/src/services/integrations/instagram_login_oauth.py", line 195, in exchange_and_store
                self.account_service.add_account(...)
            File "/app/src/services/core/instagram_account_service.py", line 187, in add_account
                self._validate_new_account(instagram_account_id, instagram_username)
            File "/app/src/services/core/instagram_account_service.py", line 241, in _validate_new_account
                raise ValueError(...)
            ValueError: Account @thursday.lines already exists as 'TL'

12:48:56  INFO   [InstagramLoginOAuthService.exchange_and_store] Starting execution
12:48:56  ERROR  [InstagramLoginOAuthService.exchange_and_store] Failed after 250ms:
                 Code exchange failed: This authorization code has been used
            File "/app/src/services/integrations/instagram_login_oauth.py", line 237, in _exchange_code_for_token

12:49:57  INFO   [InstagramLoginOAuthService.exchange_and_store] Starting execution
12:49:58  ERROR  [InstagramAccountService.add_account] Failed after 22ms:
                 Account @gatortails already exists as 'GT'
12:49:58  ERROR  [InstagramLoginOAuthService.exchange_and_store] Failed after 1334ms
            (same stack trace as 12:48:12, different account)
```

The 12:48:56 "code already used" is the user's browser back/refresh after the 12:48:12 failure replayed the same `?code=` value — benign, not a separate bug.

**Worker `service_runs` query** — `InstagramAPIService.post_story` history (production Neon):

```
2026-05-25 03:44:45 | failed | Invalid OAuth access token - Cannot parse access token (code: 190)
2026-05-22 19:07:25 | failed | Invalid OAuth access token - Cannot parse access token (code: 190)
2026-05-21 17:32:41 | failed | (same)
2026-05-21 02:47:15 | failed | (same)
2026-05-20 16:10:43 | failed | (same)
2026-05-20 01:29:42 | failed | (same)
2026-05-20 00:03:52 | failed | (same)
2026-05-19 23:10:27 | failed | (same)
2026-05-19 19:59:26 | failed | (same)
2026-05-19 18:06:58 | failed | (same)
2026-05-19 17:10:18 | failed | (same)
2026-05-19 17:09:59 | failed | (same)
2026-05-19 16:09:15 | failed | (same)
2026-05-19 14:10:42 | failed | (same)  ← first occurrence
```

**`posting_history` aggregate (last 30 days)** — production Neon:

```
posting_method  | status  | count | latest                | earliest
----------------+---------+-------+-----------------------+----------------------
instagram_api   | posted  |   110 | 2026-05-12 23:51:45   | 2026-04-25 14:14:58
telegram_manual | failed  |   959 | 2026-05-23 12:09:48   | 2026-05-17 19:16:04
telegram_manual | posted  |     1 | 2026-05-01 03:35:20   | 2026-05-01 03:35:20
telegram_manual | skipped |    65 | 2026-05-19 22:28:12   | 2026-04-25 14:14:46
```

`posting_queue` count: **995 items deep**, oldest `scheduled_for = 2026-05-17 19:20`. Worker logs show `Discarding abandoned queue item ... over 24h old` events trimming the head.

**Production rows for `@gatortails`:**

```
instagram_accounts.id                    = 8a98ebb2-f60e-4dab-bff6-9b25cb2f088d
instagram_accounts.instagram_account_id  = '17841438002131111'
instagram_accounts.is_active             = true
instagram_accounts.auth_method           = 'instagram_login'
instagram_accounts.updated_at            = 2026-05-19 01:12:54

api_tokens.id                            = b7c010ee-6703-4213-a27c-f524a72ecc91
api_tokens.service_name                  = 'instagram'
api_tokens.token_type                    = 'access_token'
api_tokens.issued_at                     = 2026-05-19 01:12:54
api_tokens.expires_at                    = 2026-07-17 13:43:57   ← long-lived (60-day), not expired
api_tokens.last_refreshed_at             = 2026-05-19 01:12:54
api_tokens.revoked_at                    = NULL
api_tokens.meta_account_id               = '17841438002131111'   ← backfilled by migration 036
api_tokens.token_value                   = <376 bytes ciphertext, redacted>

chat_settings.telegram_chat_id           = -1003688539654
chat_settings.active_instagram_account_id = 8a98ebb2-... (gatortails)
chat_settings.enable_instagram_api        = true
chat_settings.is_paused                   = false
```

**`schema_version` audit:**

```
35  Add api_tokens.meta_account_id for credential refactor phase 1     | applied 2026-05-19 19:30:51
36  Backfill api_tokens.meta_account_id from instagram_accounts        | applied 2026-05-19 22:39:31
```

The token was issued at `2026-05-19 01:12` — **before** PR #408 deployed and migrations 035/036 ran. The first "Cannot parse" failure was `2026-05-19 14:10` — **also before** the migrations. So migration 036 cannot have corrupted the token value; it only ever wrote `meta_account_id`. Bug A is independent of the migration timing.

### Code tracing — Bug B

Deployed `src/services/integrations/instagram_login_oauth.py` at commit `6ca43d3`, lines 180–200 (relevant excerpt):

```python
180  existing = self.account_service.get_account_by_meta_id(ig_user_id)
181
182  if existing:
183      self.account_service.update_account_token(
184          instagram_account_id=ig_user_id,
            ...
191      )
192      logger.info(f"Instagram Login: Updated token for @{username}")
193  else:
194      display_name = f"@{username}" if username else ig_user_id
195      self.account_service.add_account(           # ← prod stack trace points here
196          display_name=display_name,
197          instagram_account_id=ig_user_id,
198          instagram_username=username,
            ...
204      )
```

`get_account_by_meta_id` in deployed `src/services/core/instagram_account_service.py`:

```python
def get_account_by_meta_id(self, meta_account_id: str) -> Optional[InstagramAccount]:
    account = self.account_repo.get_by_meta_account_id(meta_account_id)
    if not account:
        account = self.account_repo.get_by_instagram_id(meta_account_id)  # legacy fallback
    return account
```

`get_by_meta_account_id` in deployed `src/repositories/instagram_account_repository.py`:

```python
result = (
    self.db.query(InstagramAccount)
    .join(ApiToken, ApiToken.instagram_account_id == InstagramAccount.id)
    .filter(
        ApiToken.meta_account_id == meta_account_id,
        ApiToken.revoked_at.is_(None),
    )
    .first()
)
```

I ran the equivalent SQL directly against prod for `meta_account_id = '17841438002131111'`: it returns the GT row. So if `ig_user_id` were `'17841438002131111'`, the lookup would succeed and the code would route to `update_account_token` at line 183. **The fact that prod hit line 195 instead proves `ig_user_id` is a different value.**

The `instagram_account_id` column was set when these accounts were originally connected via the older **Facebook Login** flow (which reads `instagram_business_account.id` from `/{page_id}?fields=instagram_business_account`). The current IG Login flow returns `user_id` from `https://api.instagram.com/oauth/access_token`. PR #408's design comment claims these are the same Instagram-Scoped User ID for professional accounts — for these two production accounts that claim is false.

PR #378 (commit `f0384d3`) had a **username fallback** that handled exactly this: when the ID lookup misses, fall back to `get_by_username` and route through `update_account_token` using the existing row's stored ID. PR #408 (commit `fb20683`) removed that fallback, on the theory that the migration would make it unnecessary. The migration doesn't help when its source column already holds the wrong-flow ID.

### Code tracing — Bug A

Token issuance path (`exchange_and_store` 2026-05-19 01:12):

1. `_exchange_code_for_token(code)` → short-lived token + `user_id`
2. `_exchange_for_long_lived_token(short_token)` → long-lived token + `expires_in` (defaults to 60 days)
3. `_get_username(...)` → username
4. `update_account_token(...)` (or `add_account` pre-#408) writes the encrypted long-lived token to `api_tokens.token_value`

The first failure is 13 hours after issuance. Possible causes (not yet narrowed in this investigation):

- The OAuth flow stored a malformed token value (e.g., the response body wasn't JSON or `access_token` key was missing but a default was used). Unlikely given the existing `if not token: raise` guard, but worth verifying.
- Double encryption or encryption with a stale key. The codebase already has fix history for the analogous Google Drive case (PR #370 "fix: surface Google Drive token decrypt failures after key rotation"). The Instagram side may have the same class of bug latent.
- Meta-side token revocation that returns "Cannot parse" rather than "expired". Less likely — the user did not change Instagram app settings, and the error wording is about parsing not validity.

**This is left as Bug A and tracked as F3 below — investigation deferred until F1 ships and a fresh token can be issued and tested.**

---

## Root Cause Table

| # | Bug | Confidence | Evidence |
|---|-----|-----------|----------|
| B | **Reconnect loops into `add_account` → duplicate-username `ValueError`** because PR #408 removed PR #378's username fallback and migration 036 backfilled `meta_account_id` with the wrong-flow Meta ID for legacy FB-Login rows. | **High** | Prod stack trace at `instagram_login_oauth.py:195` (today 12:48 + 12:49); deployed `6ca43d3` confirms PR #378 fallback gone; equivalent SQL on prod returns the row when called with the stored ID, so `ig_user_id` from IG Login today is a different value (no other explanation fits). |
| A | **Stored Instagram access token unparseable by Meta** (code 190), preventing every `instagram_api` post since 2026-05-19 14:10. | **High** | 14 consecutive identical errors in `service_runs.InstagramAPIService.post_story`; last successful post 2026-05-12; token format issue (not expired). |
| C | **Posting queue 995 items deep with oldest scheduled 2026-05-17.** Worker is dropping >24h items as it tries to catch up. | **Medium (consequence of A)** | `SELECT COUNT(*) FROM posting_queue` and worker log lines `Discarding abandoned queue item ... over 24h old`. |
| D | "Code already used" at 12:48:56 is the user's browser back/refresh on the failed callback page, not a separate bug. | **High** | Same `?code=AQK…` URL value across two callback hits 44s apart. |

---

## Recommendation

**Surgical, in this order:**

1. Ship F1 (re-introduce the username fallback + self-heal `meta_account_id`) and F5 (log the lookup outcome) as a bundled PR.
2. User reconnects `@gatortails` from Telegram. New token gets issued, `meta_account_id` rewrites to the new IG Login `user_id`, and the row self-heals.
3. **If the fresh token posts cleanly** — Bug A was a one-off stale-token state from the 2026-05-19 attempt. Close out. The F5 logs confirm the diagnostic path.
4. **If the fresh token also can't be parsed** — Bug A is a structural bug in `_create_account_with_token` / `update_account_token` / encryption. Open F3 with full priority.

No production data wipe needed. No DB mutation needed. Code-only change in 2 files.

---

## Fix Plans

- `01_oauth_cross_flow_fallback.md` — F1 + F5 bundle (user-approved scope)

Deferred (not planned in this session, may be opened later):

- F2 — better error page on duplicate-username collision (defensive UX)
- F3 — investigate "Cannot parse access token" root cause (conditional on F1 outcome)
- F4 — queue triage runbook entry

---

## Files in Play

| File | Where |
|---|---|
| `src/services/integrations/instagram_login_oauth.py` | Deployed lines 180–195 — the lookup site that misses |
| `src/services/core/instagram_account_service.py` | Deployed lines 224–244 `_validate_new_account`; 427–446 `get_account_by_meta_id` |
| `src/repositories/instagram_account_repository.py` | Deployed lines 78–100 `get_by_meta_account_id` (the join) |
| `scripts/migrations/036_credential_refactor_backfill_meta_account_id.sql` | The backfill whose assumption breaks for these rows |
| `tests/src/services/test_instagram_login_oauth.py` | Where the regression test goes; `test_exchange_cross_flow_username_fallback` was removed in PR #408 and should be re-added |

---

## Logs / Artifacts (saved during investigation)

- `/tmp/storydump-logs.txt` — last 200 lines of storydump service logs, including all three 12:48–12:49 callbacks
- `/tmp/worker-logs.txt` — last 150 lines of worker logs (no posting attempts in window; mostly catchup discards + Telegram sends)
- `/tmp/investigate-app-2026-05-25_instagram-oauth/research/` — exploratory research files. **Caveat:** the early `database-state.md` was generated against the local stale dev DB and contains fabricated timestamps (`2026-05-25 12:49:57` for `service_runs`) and field values (`auth_method='instagram_login'` for `@thursday.lines`, `expires_at: 2026-07-17` for the older token, etc.). Treat as background only; production ground truth is in this doc.
