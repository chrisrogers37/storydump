# F1 + F5 — Cross-flow OAuth account resolution + lookup observability

| | |
|---|---|
| Investigation | `00_INVESTIGATION.md` (this directory) |
| Scope | F1 (restore cross-flow account resolution for legacy FB-Login rows) + F5 (log the lookup outcome) |
| Type | Bug fix (code-only) + diagnostic logging |
| Risk | Low |
| Effort | Small (≈40 lines code, ≈60 lines test) |
| Touches prod data? | No (self-healing happens via the live OAuth flow once a user reconnects) |
| Status | ✅ **COMPLETED** — verified 2026-07-06. Shipped in `26f85fd` (2026-05-25) and `03d15cd` (2026-05-26, #441). `find_existing_account_for_oauth` is live in `src/services/core/instagram_account_service.py` — the same three-tier lookup designed here — and CHANGELOG.md documents the matching regression tests (`test_exchange_recovers_via_username_when_meta_id_mismatches`, `test_resolves_by_username_when_meta_id_misses`) and the `TestUsernameCallbackRemoved` → `TestCrossFlowUsernameRecovery` rename this plan called for. (Originally started 2026-05-25, branch `implement/oauth-cross-flow-fallback`.) |
| Decisions confirmed during build | Keep `get_account_by_meta_id` as thin alias (no callsite churn); F5 logs interesting branches at INFO, skip the boring meta_id-direct hit |

---

## Goal

Restore the user's ability to reconnect Instagram for legacy accounts (`@gatortails`, `@thursday.lines`) whose `api_tokens.meta_account_id` was backfilled with the FB-Login-era ID but whose IG Login `user_id` returns a different value today. Self-heal `meta_account_id` on the first successful reconnect so the cross-flow path is traversed at most once per legacy row.

Add diagnostic logging so the next OAuth bug doesn't require reading prod stack traces to identify which lookup branch fired.

## Non-goals

- **Not** investigating "Cannot parse access token" (Bug A) — that gets its own ticket once F1 ships and a fresh token is issued (see investigation doc, Recommendation step 3).
- **Not** re-running migration 036 with different source data — there is no clean source. The live OAuth flow is the only authoritative source for the IG Login `user_id`, so the heal has to happen there.
- **Not** advancing credential-refactor phases 4/5 — those should land after legacy rows have all re-OAuthed at least once, otherwise dropping the legacy column removes the only recovery path.

## Design — chosen path

**Option G** from the weighing analysis: extract a named cross-flow resolution helper, replace both lookup sites with it. Rejected: Option A (inline revert of PR #378) — same semantics but worse architecture; the cross-flow concept survives the refactor and deserves a name.

### New method on `InstagramAccountService`

```python
def find_existing_account_for_oauth(
    self,
    meta_account_id: str,
    username: Optional[str] = None,
) -> Optional[InstagramAccount]:
    """Resolve an existing Instagram account for an OAuth refresh.

    Lookup order:
      1. api_tokens.meta_account_id (credential-keyed, the refactor's target state)
      2. instagram_accounts.instagram_account_id (legacy column, in place until phase 5)
      3. instagram_accounts.instagram_username (cross-flow recovery for legacy rows
         whose stored Meta-side ID does not match what the current OAuth flow returns
         — see migration 036 / PR #408 for context)

    The username branch is the only path that can succeed when a legacy row's
    backfilled meta_account_id doesn't match the live IG Login user_id. Callers
    that hit it should rewrite the token's meta_account_id to the new value so
    subsequent reconnects resolve via branch 1.
    """
    account = self.account_repo.get_by_meta_account_id(meta_account_id)
    if account:
        return account
    account = self.account_repo.get_by_instagram_id(meta_account_id)
    if account:
        return account
    if username:
        account = self.account_repo.get_by_username(username)
    return account
```

The first two branches are exactly what `get_account_by_meta_id` already does today — the new method takes over that responsibility and adds the username branch. The old `get_account_by_meta_id` stays as a thin alias (calls `find_existing_account_for_oauth(meta_account_id, username=None)`) so callers that don't want the cross-flow recovery (notably `_validate_new_account`) keep their narrower semantics.

### Two callsite changes

**`src/services/integrations/instagram_login_oauth.py`** — `exchange_and_store`, around deployed line 180:

```python
existing = self.account_service.find_existing_account_for_oauth(
    meta_account_id=ig_user_id,
    username=username,
)

# F5: log which branch produced the answer so the next OAuth bug isn't blind.
if existing:
    matched_by = (
        "meta_account_id" if existing_meta_id_matches_ig_user_id(existing, ig_user_id)
        else "username"
    )
    logger.info(
        "Instagram Login: matched existing account @%s by %s "
        "(stored instagram_account_id=%s, new ig_user_id=%s) — updating in place",
        username, matched_by, existing.instagram_account_id, ig_user_id,
    )
else:
    logger.info(
        "Instagram Login: no existing account for ig_user_id=%s, username=%s — creating new",
        ig_user_id, username,
    )
```

The `existing_meta_id_matches_ig_user_id` check is a one-liner — either inline the comparison or extract a tiny helper. Keep the log message format stable; ops tooling may grep on `matched existing account @`.

The `if existing:` / `else:` branches downstream stay as written; routing to `update_account_token` vs `add_account` is unchanged. **`update_account_token` is called with `instagram_account_id=ig_user_id` (NOT `existing.instagram_account_id`) so the token row's `meta_account_id` self-heals to the new value** — see the next change.

**`src/services/core/instagram_account_service.py`** — `update_account_token`, replace the existing internal lookup:

```python
account = self.find_existing_account_for_oauth(
    meta_account_id=instagram_account_id,
    username=instagram_username,
)
if not account:
    raise ValueError(f"Account with ID {instagram_account_id} not found")
```

This is the second lookup site. Without this change, the username-fallback path in `exchange_and_store` finds the row, calls `update_account_token`, and the internal lookup immediately misses again — raising "Account not found" and surfacing as another "Connection Failed" page. Both lookups have to be cross-flow-aware.

Downstream, `update_account_token` already passes `meta_account_id=instagram_account_id` to `token_repo.create_or_update(...)`. That's the self-heal write — when called with the new `ig_user_id`, it overwrites the wrong backfilled value. No additional change needed on that line.

### `_validate_new_account` stays as-is

It checks `meta_account_id` and `username` separately with distinct error messages. That's the correct uniqueness contract for new-account creation. The cross-flow helper is for *reconciling an OAuth refresh against an existing row*, which is a different operation. Don't conflate them.

## Verification

### Unit tests (new + restored)

In `tests/src/services/test_instagram_login_oauth.py`:

- **`test_exchange_cross_flow_username_fallback`** — restored from pre-PR-#408. Mock `account_repo.get_by_meta_account_id` and `account_repo.get_by_instagram_id` both returning None; mock `get_by_username` returning an account with `instagram_account_id='OLD_ID'`. Call `exchange_and_store`. Assert `update_account_token` was called (not `add_account`) and the token repo write included `meta_account_id='NEW_IG_USER_ID'`.
- **`test_exchange_creates_new_account_when_username_also_misses`** — mock all three lookups returning None. Assert `add_account` is called and not `update_account_token`.
- **`test_exchange_resolves_directly_by_meta_account_id`** — happy path, the credential refactor's intended state. Mock `get_by_meta_account_id` returning an account. Assert `update_account_token` is called and the username lookup was never invoked.

In `tests/src/services/test_instagram_account_service.py` (or wherever `update_account_token` is tested):

- **`test_update_account_token_resolves_by_username_when_meta_id_misses`** — analogous mocks, asserts the function does not raise and writes `meta_account_id` to the new value.

### Integration / shadow

If a staging environment exists, point Instagram Login at a stub server that returns a known `user_id` different from any stored `meta_account_id`. Run the full OAuth round-trip. Confirm 200 success page and the expected `api_tokens.meta_account_id` rewrite. If no staging, skip — the unit tests cover the logic and the production verification below covers the integration.

### Production verification (user-driven, after deploy)

1. User initiates reconnect for `@gatortails` from the Telegram Mini App.
2. Confirm storydump log shows: `Instagram Login: matched existing account @gatortails by username (stored instagram_account_id=17841438002131111, new ig_user_id=<X>) — updating in place` followed by `Instagram Login: Updated token for @gatortails`.
3. Query prod (read-only): `SELECT issued_at, expires_at, meta_account_id, last_refreshed_at FROM api_tokens WHERE instagram_account_id = '8a98ebb2-f60e-4dab-bff6-9b25cb2f088d';`. Expect a fresh `issued_at` ≈ now, `expires_at = issued_at + 60d`, and `meta_account_id` set to the new `ig_user_id` value (the one in the log line).
4. Trigger one Instagram API post via the normal scheduler tick (no manual `process-queue` — that's a forbidden command per CLAUDE.md). Within a couple of minutes a `service_runs` row with `service_name='InstagramAPIService', method_name='post_story', status='completed'` should appear. If status='failed' with the same "Cannot parse" error, Bug A is structural and F3 opens.
5. Repeat for `@thursday.lines` if the user wants to revive it.

### Rollback

`git revert <fix-commit-sha>` and redeploy. The new method is purely additive (it doesn't change the schema or the contract of any existing call), so the revert is mechanical. Worst case: legacy accounts go back to being unable to reconnect, but they were already in that state.

## Risks & mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| `find_existing_account_for_oauth` finds the wrong account when a username has been re-used across two different Meta accounts. | Very low for this codebase (each `instagram_username` is `UNIQUE` per current schema). | The DB constraint already enforces it; no additional code needed. |
| The username fallback masks a legitimate "new account with same username" scenario (e.g., user changed their IG handle then someone else claimed the old one). | Very low — IG-side. | Document in the helper docstring that this is an OAuth-refresh-only path, not for general use. |
| F5 log volume — the new logger.info fires on every OAuth callback. | Low (callback rate is human-scale, not machine-scale). | Keep at INFO level; if needed, demote `matched by meta_account_id` to DEBUG and only INFO-log the `username` branch and the `new account` branch. |
| `update_account_token` self-heal writes an incorrect value if `instagram_account_id` passed in is somehow not the IG Login `user_id`. | Very low — only one caller (`exchange_and_store`) and it passes `ig_user_id` directly from the token exchange. | Type comment on the parameter; the unit test covers the happy path. |

## Out-of-scope follow-ups

- **F2** (better error page on duplicate-username collisions) — defensive UX; open separately when this lands.
- **F3** (investigate "Cannot parse access token" root cause) — opens only if F1's verification step 4 shows the fresh token also fails. Likely scope: audit `_create_account_with_token`/`update_account_token` for encryption-layer bugs; check whether `ENCRYPTION_KEY` was rotated.
- **F4** (queue triage runbook) — once posting resumes, decide whether to bulk-prune the 995-deep `posting_queue` pre-2026-05-19 or let the `>24h abandoned` discard logic handle it organically.
- **Credential refactor phase 4/5** — drop `instagram_accounts.instagram_account_id` and move `auth_method` to `api_tokens.service_name`. When you do, `find_existing_account_for_oauth` simplifies (the legacy-column branch goes away) but the username branch should stay until you can prove via prod data that every legacy row has re-OAuthed at least once. The simplest proof: a query that counts rows where `api_tokens.meta_account_id != instagram_accounts.instagram_account_id`. When that count hits zero, the username branch is truly dead code and can be removed.

## Files touched

| File | Change |
|---|---|
| `src/services/core/instagram_account_service.py` | Add `find_existing_account_for_oauth`; switch `update_account_token`'s internal lookup to it; `get_account_by_meta_id` becomes a thin alias. |
| `src/services/integrations/instagram_login_oauth.py` | Replace `get_account_by_meta_id` callsite with the new helper; add F5 logging. |
| `tests/src/services/test_instagram_login_oauth.py` | Restore `test_exchange_cross_flow_username_fallback`; add the two sibling tests. |
| `tests/src/services/test_instagram_account_service.py` | Add `test_update_account_token_resolves_by_username_when_meta_id_misses`. |
| `CHANGELOG.md` | Entry under `## [Unreleased]` — "Fixed: Instagram Login reconnect for legacy FB-Login-era accounts whose backfilled `meta_account_id` mismatches the live IG Login user_id (#TBD)." |

## Implementation order

1. Write the failing test `test_exchange_cross_flow_username_fallback` against current code; confirm it fails the way prod fails.
2. Add `find_existing_account_for_oauth`.
3. Switch `exchange_and_store` to use it; test passes.
4. Add `test_update_account_token_resolves_by_username_when_meta_id_misses`; switch `update_account_token` to use the helper; test passes.
5. Add the two sibling tests; confirm they pass.
6. Add F5 logging; eyeball log output via the existing tests' caplog if available.
7. CHANGELOG entry.
8. `ruff check && ruff format --check && pytest` per `CLAUDE.md` pre-commit requirements.

## Acceptance

- All four tests pass.
- `ruff check src/ tests/` clean.
- `ruff format --check src/ tests/` clean.
- Full `pytest` suite green.
- Manual production verification (step-by-step in **Verification** above) confirms the user can reconnect `@gatortails` and the token row self-heals.
