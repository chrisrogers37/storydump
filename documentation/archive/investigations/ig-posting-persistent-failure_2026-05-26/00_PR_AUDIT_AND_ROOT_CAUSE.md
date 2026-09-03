> **Archived 2026-09-02 — SUPERSEDED.** Its storage-corruption theory was replaced by the host-routing root cause (`ig-host-routing_2026-06-02/`); its recommendations became decision D31 of the consolidated plan (#732, closed). See [`documentation/archive/README.md`](../../README.md) for the index.

# PR Audit: Why PRs #433, #436, #441 Didn't Fix "Instagram connection has expired"

| Field | Value |
|---|---|
| Date | 2026-05-26 |
| Investigator | Astrid (engineer bot) |
| Error | "Instagram connection has expired. Please reconnect your account in Settings." |
| Origin | `src/services/core/telegram_autopost.py:611` — `_get_user_friendly_error()` maps `TokenExpiredError` to this user-facing message |
| Trigger | Meta Graph API returns error code 190 "Cannot parse access token" → `instagram_api.py:348-359` raises `TokenExpiredError` |

---

## TL;DR

All three PRs fixed real bugs in the posting pipeline, but **none of them addressed the actual token corruption/invalidity that causes Meta error 190**. They fixed: (1) the auto-approve path not calling Instagram at all, (2) a DB default that blocked posting, and (3) OAuth reconnect failing for legacy accounts. The persistent error is **Bug A from the investigation doc** — the stored token value is unparseable by Meta, and no code path validates token liveness with Meta before attempting to post.

---

## PR-by-PR Audit

### PR #433 — "fix: auto-approved posts now post to Instagram"

**Merged:** commit `98c8193`
**Files changed:** `scheduler.py`, `test_scheduler.py`

**What it claimed to fix:**
The scheduler's `_auto_approve()` path was recording returning media as "posted" in `posting_history` but never calling the Instagram Graph API. Content was silently marked successful without being published.

**What it actually changed:**
1. Made `_auto_approve()` async
2. Added `_auto_approve_instagram()` method — safety check → Cloudinary upload → `instagram_service.post_story()` → cleanup
3. When `enable_instagram_api=True` and `dry_run_mode=False`, auto-approved items now run the full posting flow
4. Falls back to `auto_reapproval` recording on any failure

**Was this a real bug?** Yes. Auto-approve was completely bypassing Instagram. This was a Phase 1 → Phase 2 upgrade gap.

**Why it didn't fix the persistent error:**
It connected the auto-approve path to the Graph API, but the Graph API call itself fails with error 190 because the **stored token is unparseable by Meta**. PR #433 assumed the token was valid — it just wasn't being used. Adding the plumbing to use a broken token doesn't produce working posts.

**Assumption that was wrong:** That the token was fine and just wasn't being used by this code path.

---

### PR #436 — "fix: unblock Instagram posting — DB default + silent auto-approve failure"

**Merged:** commit `8ec0012`
**Files changed:** `scheduler.py`, `test_scheduler.py`, migration `037_fix_dry_run_mode_default.sql`

**What it claimed to fix:**
1. `dry_run_mode` PostgreSQL column had a DDL default of `true` (despite the model saying `False`), so rows created via migrations were stuck in dry-run mode, blocking all Instagram posting.
2. When `_auto_approve_instagram()` failed, the item was still recorded as `status=posted, success=true` with a 30-day repost lock — silent false success.

**What it actually changed:**
1. Migration 037: `ALTER TABLE chat_settings ALTER COLUMN dry_run_mode SET DEFAULT false` + `UPDATE chat_settings SET dry_run_mode = false WHERE dry_run_mode = true`
2. In `_auto_approve()`: when `_auto_approve_instagram()` returns `None` (failure), the item is no longer recorded as successful. Queue item is cleaned up, clock advances, but `times_posted` and locks are untouched so the item stays eligible.
3. Changed `result["posted"]` from hardcoded `True` to `result["posted"]` (the actual outcome).

**Was this a real bug?** Yes, both parts. The DB default was genuinely blocking posting, and silent false-success was hiding failures.

**Why it didn't fix the persistent error:**
The `dry_run_mode=true` DB default was one blocker — fixing it allowed posting attempts to reach the API. But reaching the API with a corrupted token just produces error 190 instead of being silently skipped. The improved failure handling (not recording false success) is correct but doesn't fix the root cause — it just makes the failure visible rather than silent.

**Assumption that was wrong:** That removing the `dry_run_mode` blocker would unblock posting. It did unblock the *attempt*, but the token itself was already broken.

---

### PR #441 — "fix: OAuth reconnect for legacy FB-Login Instagram accounts"

**Merged:** commit `03d15cd`
**Files changed:** `instagram_account_service.py`, `instagram_login_oauth.py`, tests, investigation docs

**What it claimed to fix:**
Legacy accounts whose `instagram_accounts.instagram_account_id` was set during FB Login (an IGSID) couldn't reconnect via Instagram Login. The lookup by `meta_account_id` missed because IG Login returns a different `user_id` than the FB-Login-era stored ID. Code fell through to `add_account`, hit the duplicate-username validator, and showed "Connection Failed."

**What it actually changed:**
1. New helper `InstagramAccountService.find_existing_account_for_oauth(meta_account_id, username)` — three-tier resolver: credential-keyed → legacy column → username
2. `exchange_and_store` and `update_account_token` both use the new helper
3. On cross-flow match, `meta_account_id` is self-healed to the live IG Login `user_id`
4. `get_account_by_meta_id` becomes a thin alias with narrow semantics for validators

**Was this a real bug?** Yes. This was Bug B from the investigation doc — reconnect was broken for legacy accounts.

**Why it didn't fix the persistent error:**
This PR fixes the **reconnect path** (Bug B), which is a prerequisite for the user to get a fresh token. But it doesn't fix **why the existing token is unparseable** (Bug A). The investigation doc explicitly says: "Bug B is hiding Bug A: the user can't reconnect to refresh the broken token, because the reconnect codepath is broken too."

**The intended theory of change was:** Fix reconnect (PR #441) → user reconnects → fresh token replaces broken one → posting works. **If this theory holds, posting should work after the user reconnects.** If posting still fails after reconnect, Bug A is structural (token encryption/storage issue).

**Condition where the fix is bypassed:** The user hasn't reconnected yet. PR #441 doesn't auto-heal the token — it enables the user to manually reconnect, which issues a new token. Until the user completes the OAuth flow, the broken token remains in the DB and every posting attempt will hit error 190.

---

## The Actual Root Cause (Bug A)

The persistent error traces to:

1. **`telegram_autopost.py:611`** — user sees "Instagram connection has expired"
2. **`instagram_api.py:348-359`** — Meta returns error code 190, which is classified as `TokenExpiredError`
3. **`instagram_credentials.py:62`** — safety check only checks `token_record.is_expired` (local `expires_at` column), which returns `False` because `expires_at = 2026-07-17` (token hasn't expired by calendar)
4. **`api_token.py:104-109`** — `is_expired` is purely a local time comparison: `datetime.now(UTC) > expires_at`

**The token hasn't expired by calendar (60-day window still open), but Meta can't parse it.** The `is_expired` check passes, the safety check passes, Cloudinary upload succeeds, and then the Meta API call fails.

### Why Meta can't parse the token

From the investigation doc (`00_INVESTIGATION.md:196-208`):

- The token was issued at `2026-05-19 01:12:54`
- First failure was `2026-05-19 14:10:42` — 13 hours later
- The error is "Cannot parse access token" (not "expired" or "invalid")
- Possible causes: malformed token stored during OAuth exchange, double-encryption, encryption with a stale key, or Meta-side revocation that returns "Cannot parse" instead of a revocation subcode

The investigation doc deferred this as **F3** — to be investigated after PR #441 ships and the user reconnects with a fresh token.

---

## What None of the PRs Address

### 1. No pre-post token validation against Meta

The safety check at `instagram_credentials.py:277-349` only checks:
- Is Instagram API enabled?
- Is an account configured?
- Does a non-expired token exist in DB?

It does **not** validate the token against Meta's servers (e.g., calling `/me?access_token=...`). A token can pass all local checks but be rejected by Meta.

### 2. No automatic token refresh before posting

`TokenRefreshService.refresh_instagram_token()` exists and is called periodically by the scheduler loop (`scheduler_loop.py:377`), but:
- It only refreshes tokens within 7 days of expiry (`REFRESH_BUFFER_HOURS = 168`)
- The GT token `expires_at = 2026-07-17` — that's ~52 days away, well outside the 7-day buffer
- The refresh tick therefore **skips this token every time** because `hours_until_expiry > 168`
- Even if it tried to refresh, Meta would return the same error 190 because the token is unparseable

### 3. No distinction between "expired" and "unparseable"

Meta error 190 covers both expired tokens and corrupt/unparseable tokens. The code maps all 190 errors (without a revocation subcode) to `TokenExpiredError`. The user message says "connection has expired" but the real problem is "token is corrupted/unparseable." This misleads the user into thinking they just need to wait or that something timed out, when actually a reconnect is required.

### 4. Scheduler auto-approve catches all exceptions as fallback

In `scheduler.py:675-680`, the `_auto_approve_instagram()` method catches `Exception` and returns `None`:
```python
except Exception as e:
    logger.warning(f"Auto-approve Instagram posting failed for ...")
    return None
```

This means `TokenExpiredError` from the Graph API is caught, logged as a warning, and the item falls back to `auto_reapproval` (after PR #436, it's not recorded as success — but the specific error type is lost). The scheduler doesn't escalate token issues differently from transient network errors.

---

## Timeline of What Actually Happened

| Date | Event |
|---|---|
| 2026-05-19 01:12 | Token issued via OAuth exchange for `@gatortails` |
| 2026-05-19 14:10 | First "Cannot parse access token" failure in `service_runs` |
| 2026-05-19 16:09–2026-05-25 03:44 | 13 more consecutive error-190 failures |
| 2026-05-22 | PR #433 merged — auto-approve now calls Instagram API (but token is already broken, so these calls also fail) |
| 2026-05-22 | PR #436 merged — dry_run_mode fixed, failure handling improved (failures now visible, not silently "successful") |
| 2026-05-25 12:48 | User tries to reconnect → "Connection Failed" (Bug B) |
| 2026-05-25 | PR #441 merged — reconnect path fixed for legacy accounts |
| 2026-05-26 | **User has not yet reconnected** — broken token still in DB, every post attempt still hits error 190 |

---

## What Needs to Happen

### Immediate (unblocks the user)

1. **User must reconnect `@gatortails` via Telegram.** PR #441 made this possible. The fresh token from the OAuth flow should replace the broken one. If posting works with the fresh token, Bug A was a one-off token-corruption incident.

### If fresh token also fails (Bug A is structural)

2. **Investigate token encryption/storage** — is the OAuth exchange storing the token correctly? Is there double-encryption? Is `ENCRYPTION_KEY` stable? The investigation doc notes the Google Drive side had the same class of bug (PR #370 "surface Google Drive token decrypt failures after key rotation").

### Defensive improvements (prevent recurrence)

3. **Pre-post token liveness check** — before `post_story()`, call Meta's `/me` endpoint with the token. If it returns 190, skip to error handling immediately instead of uploading to Cloudinary first. This catches corrupt/revoked tokens before wasting time on the upload step.

4. **Better error classification** — distinguish "Cannot parse access token" from "Session has expired" in the error-190 handler. The former suggests corruption; the latter suggests time-based expiry. Surface different user messages.

5. **Proactive token health monitoring** — the health check already runs `check_token_health()`, but it only checks local expiry. Add a periodic "ping Meta" check that validates the token is actually usable. Flag accounts with invalid tokens in the health check output.

6. **Scheduler escalation for persistent token failures** — if the same account fails N times consecutively with error 190, escalate from a warning to an alert (Telegram notification to admin) rather than silently retrying.

---

## Summary Table

| PR | Bug Fixed | Why It Didn't Fix the Persistent Error |
|---|---|---|
| #433 | Auto-approve path never called Instagram API | Connected the plumbing but the token was already broken |
| #436 | `dry_run_mode` DB default blocked posting; failures silently marked as success | Unblocked attempts and made failures visible, but didn't fix the token |
| #441 | OAuth reconnect broken for legacy FB-Login accounts | Fixed the reconnect path so user CAN get a fresh token, but user hasn't reconnected yet |
| *(none)* | Stored token unparseable by Meta (error 190) since 2026-05-19 | No PR addresses token corruption. Theory: reconnect via PR #441 → fresh token → works. Untested. |
