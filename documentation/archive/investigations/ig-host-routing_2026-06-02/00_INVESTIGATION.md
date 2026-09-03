> **Archived 2026-09-02 — RESOLVED.** All five planned PRs merged 2026-06-02 → 06-04 (#462, #476–#479; issue #468 closed). See [`documentation/archive/README.md`](../../README.md) for the index.

# Investigation: Instagram Posting Still Broken After PR #441

| Field | Value |
|---|---|
| Date | 2026-06-02 |
| Triggered by | User report: "We STILL have not figured out auth and getting stories to post through the Instagram login." |
| Prior investigation | `documentation/archive/investigations/ig-oauth-cross-flow-reconnect_2026-05-25/00_INVESTIGATION.md` |
| Deployed prod commit at start | `9a8cc36` (origin/main) |
| Affected accounts | `@gatortails` (chat `-1003688539654`); `@thursday.lines` (deactivated) |
| Investigator | Claude (via `/claudna:investigate-app`) |
| Plan source | `/Users/chris/.claude/plans/playful-finding-frost.md` (overwritten mid-session as scope evolved) |

---

## TL;DR

**Bug A from the prior investigation is structural, not stale state — and the root cause is a host-routing bug, not a token bug.**

The Instagram Login OAuth flow issues tokens that are valid only against `graph.instagram.com`. The posting code (`instagram_api.py`, `instagram_credentials.py`, `backfill_downloader.py`) hardcodes `settings.meta_graph_base` = `graph.facebook.com`. Meta rejects any IG-Login-issued token sent to `graph.facebook.com` with error code 190 "Cannot parse access token" — even though the token is fresh, valid, and matches the required scopes.

PR #441 fixed the OAuth reconnect codepath (Bug B). The reconnect now succeeds. But every post still fails because the posting path uses the wrong host.

`token_refresh.py:107` already documents and implements the correct branching pattern (IG Login tokens refresh on `graph.instagram.com`, FB Login on `graph.facebook.com`). The same dimension was never wired into posting.

---

## Today's Verification (live prod)

1. **Wiped legacy FB-Login residue.** Single transaction on prod Neon: nulled `chat_settings.active_instagram_account_id`, deleted the orphan token row (`46adc2a7…`), deleted the two FK'd tokens, deleted both `instagram_accounts` rows. Post-state: 0 IG accounts, 0 IG tokens, both chats unlinked.

2. **User reconnected `@gatortails` via `/dashboard/settings`.** Logs at 2026-06-02 02:42:46–02:42:49:
   ```
   GET /api/onboarding/oauth-url/instagram ... 200 OK
   InstagramLoginOAuthService.exchange_and_store Starting
   InstagramAccountService.add_account Starting
   Added Instagram account: @gatortails (@gatortails)
   Instagram Login: Created new account @gatortails (ig_user_id=26060527550287223)
   InstagramLoginOAuthService.exchange_and_store Completed successfully (2832ms)
   ```
   Fresh state in DB:
   ```
   instagram_accounts: id=9645a8f2..., instagram_account_id=26060527550287223,
                       auth_method=instagram_login, is_active=t
   api_tokens:         meta_account_id=26060527550287223, expires 2026-08-01,
                       token_len=312, NOT revoked
   chat_settings:      -1003688539654 → linked to 9645a8f2 ✓
   ```

3. **User triggered a post.** First `InstagramAPIService.post_story` attempt after reconnect: failed identically at 2026-06-02 03:09:21:
   ```
   error_type   = TokenCorruptError
   error_message = Invalid OAuth access token - Cannot parse access token (code: 190)
   File "/app/src/services/integrations/instagram_api.py", line 243, in _create_media_container
       self._check_response_errors(response)
   File "/app/src/services/integrations/instagram_api.py", line 367, in _check_response_errors
       raise TokenCorruptError(...)
   ```
   Cloudinary upload succeeded; failure is at the first POST to `graph.facebook.com/{ig_user_id}/media`.

4. **Confirmed against Meta's docs.** The Instagram API with Instagram Login product uses `graph.instagram.com` as its host. Stories are supported (`media_type=STORIES`) via the same two-step `/{ig-id}/media` + `/{ig-id}/media_publish` pattern this codebase implements. Per Meta docs: "Host: `graph.instagram.com` or `graph.facebook.com`" — but in practice tokens are scoped to one host based on the issuing OAuth flow.

---

## Root Cause Table

| # | Bug | Confidence | Evidence |
|---|---|---|---|
| **A** | **Wrong host in posting code.** IG-Login tokens sent to `graph.facebook.com` return code 190 "Cannot parse access token". Affects all of `_create_media_container`, `_wait_for_container_ready`, `_publish_container`, `InstagramCredentialManager.get_account_info`, and three sites in `backfill_downloader.py`. | **High** | Live reproduction at 2026-06-02 03:09:21 with a fresh-from-OAuth token; Meta docs naming the correct host; `token_refresh.py:97-110` already documents the same flow-host pairing. |
| **B** | **`get_active_account_credentials` returns a token regardless of `auth_method`.** No precondition check that the token type matches the consumer's required surface. Originally invisible because the only consumer (`InstagramAPIService`) was on `graph.facebook.com`, which matched legacy FB-Login tokens. | **High** | Code inspection: `instagram_credentials.py:57-83` short-circuits to token lookup with no auth_method guard. |
| **C** | **Schema gap — `auth_method` lives on `instagram_accounts`, not `api_tokens`.** Code must join through the account row to discover provenance; the credential row is not self-describing. Issue #380 calls this out; phases 1–3 done, 4–5 pending. | **High** | `src/models/api_token.py` has no `auth_method` column; `src/models/instagram_account.py:38` has it. `instagram_account_service.py:379` and `token_refresh.py:107` both consult the account-side column. |
| **D** | **Orphan token (`46adc2a7…`) had no FK and was generating spurious refresh-failure noise.** Refresh attempts hit `_call_meta_refresh()` which omits `client_id`/`client_secret` for the FB-host path → "Missing client_id parameter" errors. Removed during today's cleanup. | **High** | Worker logs showed `Instagram token refresh failed for legacy: 'Missing client_id parameter'` at 00:11:55. After delete, refresh runs cleanly. Latent bug in `_call_meta_refresh` remains but no longer fires. |

---

## Fix Sequence (planned — PR 1 lands with this investigation)

### PR 1 (this PR) — Unblock posting, no schema change

Route `InstagramAPIService` (and the IG-side reads in `InstagramCredentialManager.get_account_info` and `backfill_downloader`) to `graph.instagram.com` via a new `settings.meta_ig_graph_base` property. Guard `get_active_account_credentials` so non-`instagram_login` accounts return `(None, None, None)` with a clear "reconnect via /dashboard/settings" log. Production has zero `fb_login` tokens left after today's cleanup, so no fallback is needed.

### PR 2–5 (planned, separate PRs)

- **PR 2**: Add `api_tokens.auth_method` and `api_tokens.issuing_app_id`. Migration backfills from `instagram_accounts.auth_method`. UNIQUE constraint expands to include `auth_method` so an account can hold both flows simultaneously (per issue #380 acceptance criteria).
- **PR 3**: Dual-write `auth_method` and `issuing_app_id` at all 5 OAuth callback / token write sites.
- **PR 4**: Switch reads — `instagram_credentials.get_active_account_credentials` queries by `(account_id, auth_method='instagram_login')` directly; `token_refresh.py` reads from token row, not account row.
- **PR 5**: Drop `instagram_accounts.auth_method` and `instagram_accounts.instagram_account_id`. Closes issue #380.

Full first-principles design and rationale (provenance on the credential, pathway in the consumer service) in the plan file.

---

## Files Touched (PR 1)

| File | Change |
|---|---|
| `src/config/settings.py` | Added `meta_ig_graph_base` property → `https://graph.instagram.com/{version}` |
| `src/services/integrations/instagram_api.py` | Three host swaps (`_create_media_container`, `_wait_for_container_ready`, `_publish_container`) |
| `src/services/integrations/instagram_credentials.py` | Host swap in `get_account_info`; `auth_method='instagram_login'` guard in `get_active_account_credentials` |
| `src/services/integrations/backfill_downloader.py` | Three host swaps in media/stories/carousel fetches |
| `tests/src/services/test_instagram_api.py` | Two fixtures updated to set `auth_method`; 5 new tests for host routing + guard |
| `documentation/archive/investigations/ig-host-routing_2026-06-02/00_INVESTIGATION.md` | This file |
| `CHANGELOG.md` | Entry under `[Unreleased]` |

---

## Verification

- **Local**: `pytest tests/src/services/test_instagram_api.py` → 45 passed. Broader sibling suites (`test_instagram_backfill`, `test_token_refresh`, `test_credential_refactor_*`) → 107 passed. `ruff format --check` clean on all touched files.
- **Live**: After this PR ships, user triggers a post. Expected: `InstagramAPIService.post_story` returns `success=t` in `service_runs`. This will be the first successful IG post since 2026-05-12 23:51.

---

## Out of Scope

- `_call_meta_refresh()` missing `client_id`/`client_secret` for the FB-host path — latent bug, no longer reachable in prod after orphan cleanup, file as separate tech-debt.
- Multi-platform expansion (issue #186) — the proposed schema accommodates it but this work doesn't implement it.
- Encryption-key rotation telemetry (PR #346 area) — `issuing_app_id` (PR 2) helps audit, full version tracking is separate.
