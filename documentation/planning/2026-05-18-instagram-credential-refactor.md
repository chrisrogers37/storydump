# Instagram Credential Refactor — Implementation Plan

**Status:** ✅ COMPLETED — verified 2026-07-06 (see note below). **Author:** chrisrogers37 (with Claude). **Date:** 2026-05-18.
**Related:** PR #378 (band-aid fix), follow-up to PR #341 (multi-account ingest).

> **Verification note (2026-07-06):** All 5 PRs landed, tracked across issues #380 (phases 1-3) then #468 (phases 4-5): PR-1 `2bc6908` (2026-05-19, migration 035, additive `meta_account_id`), PR-2 `bb30228` (2026-05-19, dual-write), PR-3 `fb20683` (2026-05-19, #408, migration 036 backfill + credential-keyed reads), PR-4 `6f982a8` + `fe65678` (2026-06-02, migrations 038-040), PR-5 `706fbc2` (2026-06-03, migration 041). **Two deviations from this plan:** (1) PR-4 shipped as a real `api_tokens.auth_method` + `issuing_app_id` column pair (with its own migrations) rather than inferring auth method from `service_name` differentiation as sketched in "Open decisions" #1 below. (2) PR-5 only dropped `instagram_accounts.auth_method`; `instagram_accounts.instagram_account_id` was deliberately **kept** rather than dropped — per CHANGELOG.md: "its consumers (backfill, OAuth heal logic, credential lookup) need a separate refactor... filed as a follow-up." The migration plan's PR-5 SQL sketch (dropping both columns) did not ship as written.

## Problem statement

`instagram_accounts.instagram_account_id` stores **one** Meta-side ID per account row, with a `UNIQUE` constraint. But Meta exposes the **same physical Instagram account** through two different numeric identifiers depending on which OAuth flow we use:

| Flow | Endpoint that returns the ID | ID type |
|---|---|---|
| Facebook Login (`OAuthService`) | `/me/accounts` → `instagram_business_account.id` | Instagram **Business Account ID** |
| Instagram Login (`InstagramLoginOAuthService`) | `/me?fields=id` (Instagram Graph) | Instagram **User ID** |

These are different numbers. When a user connects via FB Login and then re-authenticates via IG Login, the new flow's `get_account_by_instagram_id(ig_user_id)` returns `None` (the stored ID is the Business Account ID, not the User ID), the code takes the `add_account` path, and `_validate_new_account` hard-fails on the duplicate-username uniqueness check:

```
ValueError: Account @gatortails already exists as 'GT'
```

PR #378 patches this by falling back to `get_account_by_username` at the IG Login service callsite. That works but doesn't address the underlying schema confusion: **identity** (who is the account) and **credential metadata** (the specific Meta-side ID that maps to a particular OAuth token) are being stored on the same row.

A clean model separates them.

## First-principles design

### What an Instagram account *is*

A unique entity addressable by its `instagram_username`. That's the natural key — it's globally unique on Instagram, doesn't change without explicit user action, and is the same across all of Meta's APIs.

### What an OAuth token *is*

A credential issued by Meta that authenticates calls on behalf of an Instagram account. It has:

- **Which Meta-side identifier was used to issue it** (Business Account ID vs User ID — different per flow for the same account).
- Which **auth flow** issued it (`fb_login`, `instagram_login`, `manual`).
- An expiry, a list of scopes, an issued-at timestamp, a revoked-at timestamp.
- An encrypted token value.

The Meta-side ID is **a property of the credential**, not of the account. Same physical account, two flows, two credentials, two Meta-side IDs.

### Target shape

Two tables, clean separation:

```
instagram_accounts (identity — "what we post for")
  id                  UUID  PK
  display_name        TEXT  -- user-friendly
  instagram_username  TEXT  UNIQUE  -- natural key
  is_active           BOOL
  created_at, updated_at
  -- NO Meta-side IDs.
  -- NO auth_method (that's per-credential).

api_tokens (credential — "how we authenticate that account")
  id                       UUID  PK
  instagram_account_id     UUID  FK → instagram_accounts.id
  service_name             TEXT  -- 'instagram_fb_login' | 'instagram_login' | 'google_drive' | ...
  token_type               TEXT  -- 'access_token' | 'refresh_token'
  token_value              TEXT  -- encrypted
  meta_account_id          TEXT  -- ← NEW: the Meta-side ID that issued this token
                                  -- (Business Account ID for fb_login, User ID for instagram_login)
  scopes                   TEXT[]
  issued_at                TIMESTAMP
  expires_at               TIMESTAMP
  last_refreshed_at        TIMESTAMP
  revoked_at               TIMESTAMPTZ
  token_metadata           JSONB  -- keep for misc per-service data
  chat_settings_id         UUID  FK → chat_settings.id  -- for non-IG services
  -- existing fields preserved
```

`api_tokens` already has nearly all of this. What's missing: an explicit `meta_account_id` column to replace the implicit "we stash this in `token_metadata['account_id']`" pattern. Plus moving `auth_method` from `instagram_accounts` into `service_name` differentiation on `api_tokens`.

### Why this works for posting

`posting_service` looks up the token via `token_repo.get_token_for_account(account.id, token_type='access_token')` keyed by our internal UUID, NOT by any Meta-side ID. Already correct. After refactor, it asks for "the best access_token for this account" — by default the most recent non-revoked one, optionally filtered by service_name.

### Why this works for refresh

`TokenRefreshService` already calls `account_repo.get_by_id(...)` and reads `account.auth_method` to pick the refresh endpoint (`token_refresh.py:104-109`). After refactor, the same logic moves down a level: the token *itself* knows which service_name it came from. The refresh endpoint is derived from the token row, not the account row. This is **more correct** because an account can have both a `fb_login` and `instagram_login` token simultaneously, each needing its own refresh endpoint.

## Current code touchpoints

### Schema

- `src/models/instagram_account.py` — `instagram_account_id` (unique), `auth_method`
- `src/models/api_token.py` — `instagram_account_id` FK, `token_metadata` JSONB
- `scripts/migrations/` — all applied through `034`

### Read paths that key off `instagram_account_id` (Meta-side)

| File:line | Use |
|---|---|
| `src/services/core/oauth_service.py:192` | FB Login: "does this IG already exist?" before insert |
| `src/services/integrations/instagram_login_oauth.py:180` | IG Login: same question |
| `src/api/routes/onboarding/settings.py:303` | Manual token entry: same question |
| `src/services/core/instagram_account_service.py:232` | `_validate_new_account` (duplicate check) |
| `src/services/core/instagram_account_service.py:366` | `update_account_token` lookup |
| `src/services/core/instagram_account_service.py:425` | `get_account_by_instagram_id` (public wrapper) |
| `src/repositories/instagram_account_repository.py:66` | repo `get_by_instagram_id` |

### Write paths that store `instagram_account_id`

| File:line | Use |
|---|---|
| `src/services/core/instagram_account_service.py:189-196` | `add_account` → `_create_account_with_token` → `account_repo.create` |
| `src/repositories/instagram_account_repository.py:90-110` | `create()` writes `instagram_account_id` |

### `auth_method` read paths

| File:line | Use |
|---|---|
| `src/services/integrations/token_refresh.py:106` | Pick refresh endpoint per account |
| `src/repositories/instagram_account_repository.py:95,106` | Write at account creation |

### Posting / token retrieval (unaffected by refactor — already correct)

- `src/services/integrations/instagram_credentials.py:59-60` — `token_repo.get_token_for_account(account.id, token_type='access_token')`
- `src/services/integrations/instagram_api.py:219,254,304` — uses the token string from above

## Migration plan

Five sequenced PRs, each independently shippable and revertable. Stages are deliberately small so each can sit in production for a deploy cycle before the next merges.

### PR-1: Add new schema (additive only, no behavior change)

**Migration 035:** add columns; do not remove anything.

```sql
-- scripts/migrations/035_credential_refactor_phase_1_additive.sql
BEGIN;

-- Add explicit Meta-side ID column on api_tokens.
ALTER TABLE api_tokens
ADD COLUMN IF NOT EXISTS meta_account_id TEXT;

-- Index for fast lookups (the OAuth "do we already have this?" path).
CREATE INDEX IF NOT EXISTS api_tokens_meta_account_id_idx
    ON api_tokens (meta_account_id)
    WHERE meta_account_id IS NOT NULL;

COMMIT;
```

No app changes ship with this PR. It just lands the new column nullable. Production keeps reading from the old places.

### PR-2: Dual-write

The OAuth services write the Meta-side ID to BOTH the existing `instagram_accounts.instagram_account_id` AND the new `api_tokens.meta_account_id`. Read paths still consult the old location. This is the safety net — any rollback during this window leaves the system on its existing read path.

**`src/services/core/instagram_account_service.py::_create_account_with_token`:**

```python
def _create_account_with_token(
    self,
    display_name: str,
    instagram_account_id: str,
    instagram_username: str,
    access_token: str,
    token_expires_at: Optional[datetime] = None,
    auth_method: Optional[str] = None,
) -> InstagramAccount:
    account = self.account_repo.create(
        display_name=display_name,
        instagram_account_id=instagram_account_id,  # still written (PR-2 dual-write)
        instagram_username=instagram_username,
        auth_method=auth_method,                    # still written
    )

    encrypted_token = self.encryption.encrypt(access_token)
    self.token_repo.create_or_update(
        service_name=_service_name_for_auth_method(auth_method),  # NEW
        token_type="access_token",
        token_value=encrypted_token,
        expires_at=token_expires_at,
        instagram_account_id=str(account.id),
        meta_account_id=instagram_account_id,  # NEW: explicit
        metadata={
            "account_id": instagram_account_id,  # keep for backcompat read paths
            "username": instagram_username,
        },
    )
    return account
```

Add a small helper:

```python
# src/models/instagram_account.py  (or src/services/core/auth_flow.py)

def _service_name_for_auth_method(auth_method: Optional[str]) -> str:
    """Map the legacy auth_method enum into the new per-flow service_name."""
    if auth_method == AUTH_METHOD_INSTAGRAM_LOGIN:
        return "instagram_login"
    return "instagram"  # FB Login + manual stay as 'instagram' for backcompat
```

Update `TokenRepository.create_or_update` to accept + persist `meta_account_id`. (One added field on an existing method.)

### PR-3: Backfill + switch reads to credential-keyed lookup

**Backfill migration 036:**

```sql
-- scripts/migrations/036_credential_refactor_phase_3_backfill.sql
-- Populate api_tokens.meta_account_id from existing instagram_accounts rows.
BEGIN;

UPDATE api_tokens t
SET meta_account_id = ia.instagram_account_id
FROM instagram_accounts ia
WHERE t.instagram_account_id = ia.id
  AND t.meta_account_id IS NULL
  AND ia.instagram_account_id IS NOT NULL;

COMMIT;
```

**Repo + service changes:**

Replace the "find account by Meta-side ID" lookup so it goes through `api_tokens.meta_account_id` instead of `instagram_accounts.instagram_account_id`.

```python
# src/repositories/instagram_account_repository.py

def get_by_meta_account_id(
    self, meta_account_id: str
) -> Optional[InstagramAccount]:
    """Find an InstagramAccount via any of its api_tokens.meta_account_id."""
    return (
        self.session.query(InstagramAccount)
        .join(ApiToken, ApiToken.instagram_account_id == InstagramAccount.id)
        .filter(ApiToken.meta_account_id == meta_account_id)
        .filter(ApiToken.revoked_at.is_(None))
        .first()
    )
```

```python
# src/services/core/instagram_account_service.py

def get_account_by_meta_id(
    self, meta_account_id: str
) -> Optional[InstagramAccount]:
    """Lookup keyed by the Meta-side identifier on any credential row.

    Replaces get_account_by_instagram_id. Both Business Account ID
    (FB Login) and IG User ID (IG Login) resolve via this method
    because both land in api_tokens.meta_account_id.
    """
    return self.account_repo.get_by_meta_account_id(meta_account_id)
```

Update the **three OAuth callsites** to use `get_account_by_meta_id`:

- `oauth_service.py:192` (FB Login multi-account loop)
- `instagram_login_oauth.py:180` (IG Login)
- `api/routes/onboarding/settings.py:303` (manual)

Update `_validate_new_account` to no longer check `instagram_account_id` (no longer authoritative). Keep the username uniqueness check, but only as a tripwire — the new flow does a meta-id-or-username lookup *before* `add_account`, so the duplicate path is now rare.

`instagram_login_oauth.py` also drops the PR #378 username fallback (no longer needed — meta-id lookup now reaches across flows).

### PR-4: Move `auth_method` to the token row

The `auth_method` field on `instagram_accounts` is consulted in exactly one place (`token_refresh.py:106`) to choose the refresh endpoint. After PR-2 the same information is on `api_tokens.service_name` per credential. Switch `TokenRefreshService` to consult the token row:

```python
# src/services/integrations/token_refresh.py

def _get_refresh_endpoint(self, instagram_account_id: Optional[str]) -> str:
    """Pick the right refresh URL based on the token's service_name."""
    if instagram_account_id:
        token = self.token_repo.get_token_for_account(
            instagram_account_id, token_type="access_token"
        )
        if token and token.service_name == "instagram_login":
            return self.IG_LOGIN_REFRESH_ENDPOINT
    return f"{settings.meta_graph_base}/oauth/access_token"
```

No SQL migration needed for this phase; `auth_method` stays on `instagram_accounts` as dead-but-present. Removed in PR-5.

### PR-5: Drop the redundant columns

After PR-3 + PR-4 have soaked for at least one deploy cycle and there are zero references to the old columns in the codebase, the schema-cleanup migration removes them.

```sql
-- scripts/migrations/037_credential_refactor_phase_5_cleanup.sql
BEGIN;

ALTER TABLE instagram_accounts
    DROP COLUMN instagram_account_id,
    DROP COLUMN auth_method;

COMMIT;
```

This is the only destructive migration. Defer it explicitly behind a manual sign-off — small surface area, but irreversible without a restore.

**Pre-flight check before merging PR-5:**

```bash
grep -rn "\.instagram_account_id\b\|\.auth_method\b" src/ \
  | grep -v "ApiToken\|api_token\|token_repo\|test_" \
  | wc -l
# expected: 0
```

## Test plan per PR

| PR | New tests | Existing tests touched |
|---|---|---|
| PR-1 | Migration applies + rollback (apply, revert via ALTER...DROP) | None |
| PR-2 | `_create_account_with_token` writes both columns; assert `api_tokens.meta_account_id` matches input | `test_instagram_account_service.py` (existing add/update tests assert new column is populated) |
| PR-3 | `get_account_by_meta_id` resolves across both flows; `OAuthService` / `InstagramLoginOAuthService` use the new lookup; remove PR #378's username fallback test | All three OAuth-service test files |
| PR-4 | `_get_refresh_endpoint` picks endpoint from `token.service_name`, not `account.auth_method` | `test_token_refresh.py` |
| PR-5 | Smoke test the grep check; verify migration is idempotent (`DROP COLUMN IF EXISTS`) | None — should be a no-op behaviorally |

## Risks and mitigations

| Risk | Probability | Mitigation |
|---|---|---|
| Backfill misses tokens that aren't linked to an `instagram_accounts` row (e.g. orphans from previous incidents) | Low | The migration's `UPDATE ... FROM ...` is a left-join semantic that just skips them. Pre-flight query reports the count. |
| Existing `token_metadata['account_id']` consumers we missed | Medium | Keep the JSONB key written (PR-2 example does this). Grep for `token_metadata.get("account_id")` before PR-5. |
| Multiple credentials per account during transition cause "wrong token picked" by `get_token_for_account` | Low | `get_token_for_account` already returns the most recent non-revoked row. Confirm with explicit `ORDER BY issued_at DESC LIMIT 1` if not present. |
| `_validate_new_account` username collision is the *only* dedupe guard once Meta-ID uniqueness is gone | Low | Keep the username unique index (`instagram_accounts.instagram_username UNIQUE`). Tests pin this. |
| Old `instagram_account_id` column still has a `NOT NULL UNIQUE` constraint — can't be dropped easily | Confirmed (model has `nullable=False, unique=True`) | PR-5 drops the column outright; the unique index is dropped with it. Make `nullable=True` in PR-3 if anything blocks. |

## Out of scope

- **Multi-platform support** (TikTok/X/YouTube). The refactor accidentally makes this easier (`service_name` becomes the platform discriminator), but actually implementing those platforms is its own track.
- **Token rotation as audit log** — could keep historical tokens with `revoked_at != NULL` instead of overwriting. Cleanest with this refactor in place; deferred.
- **Replacing `chat_settings.active_instagram_account_id`** with anything fancier (per-tenant active per platform, etc.). Same column survives unchanged.

## Open decisions

1. **Naming**: `service_name = 'instagram_fb_login'` or `'instagram'`? PR-2 example keeps `'instagram'` for FB Login backcompat and adds `'instagram_login'` for the new flow. Could be cleaner to rename both (`'instagram_fb_login'` + `'instagram_login'`) but it's an extra migration. **Recommend keeping `'instagram'` for legacy and `'instagram_login'` for new** — existing `get_token_for_account` queries that filter on `service_name='instagram'` stay valid.

2. **PR-5 timing**: drop columns in the same week as PR-3+PR-4, or wait a full deploy cycle? **Recommend one deploy cycle of soak time** — these are persistent columns; if anything is missed, the bug surfaces at runtime, not at migration time.

3. **Dashboard surface**: should the Settings → Accounts tab reveal that an account has both flows' credentials, or hide it? **Recommend hide for now** — single "Connected via Instagram Login" badge based on the most recent credential. Multi-credential UI is a follow-up if it becomes useful.

## Reading list (for the implementer)

- Today's PRs: `#341` (FB Login multi-account), `#378` (IG Login cross-flow fix). These are the immediate context for *why* this refactor is needed.
- `src/models/instagram_account.py` — the docstring on the model class already aspirationally describes the right architecture; the schema doesn't match.
- `src/services/integrations/token_refresh.py:98-109` — the one place `auth_method` is load-bearing.
- `src/services/integrations/instagram_credentials.py:59-60` — the posting-time lookup. Confirm this stays untouched.
