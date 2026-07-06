---
paths:
  - "src/models/**"
  - "src/repositories/**"
  - "scripts/migrations/**"
  - "src/config/database.py"
---

# Database Architecture

## Key Tables

**Core** (Phase 1): `media_items`, `posting_queue` (ephemeral), `posting_history` (permanent audit log), `media_posting_locks`, `users`, `service_runs`, `category_post_case_mix`, `user_interactions`

**Settings & Accounts** (Phase 2): `chat_settings` (per-chat config, `.env` fallback), `instagram_accounts`

**Integration** (Phase 2+): `api_tokens` (encrypted OAuth tokens, FK to `instagram_accounts`)

**Multi-instance** (post-v1.6.0, see `documentation/planning/multi-account-dashboard.md`): `user_chat_memberships` (user ↔ `chat_settings` membership — the tenant boundary check; a 2026-06 security fix (#511/#512) makes this the required gate for onboarding/dashboard endpoints when a token doesn't cryptographically bind a chat_id), `onboarding_sessions` (short-lived DM onboarding conversation state, one active per user)

## Settings Architecture

- `chat_settings` table with `.env` as fallback
- First access bootstraps from `.env` into DB via `SettingsService.get_settings()`
- Runtime changes via Telegram `/settings`, persisted to DB
- Per-chat: each Telegram chat has independent settings
- Configurable: `dry_run_mode`, `enable_instagram_api`, `is_paused`, `posts_per_day`, `posting_hours_start/end`, `show_verbose_notifications`, `active_instagram_account_id`

## Multi-Account Instagram

- `instagram_accounts` = **Identity** (display name, Instagram ID, username)
- `api_tokens` = **Credentials** (encrypted OAuth tokens per account)
- `chat_settings.active_instagram_account_id` = **Selection** (which account per chat)

## Critical Design Patterns

1. **Queue vs History**: Queue is ephemeral (work to do), History is permanent (audit log). Enables reposting.
2. **TTL Locks** (`media_posting_locks`): Lock types: `recent_post`, `manual_hold`, `seasonal`, `permanent_reject`, `skip`. Permanent locks: `locked_until = NULL`.
3. **User Auto-Discovery**: Users created automatically from Telegram interactions. No separate registration.
4. **Type 2 SCD**: Used for `category_post_case_mix` (ratio auditing) and future `shopify_products`.
5. **Tenant Boundary**: `chat_settings` is the tenant boundary for data (all tenant FKs nullable, `NULL` = legacy single-tenant data). `user_chat_memberships` is the **access** boundary — it answers "can this user act on this tenant," which is a separate check from "is this user authenticated." Any new endpoint/handler that accepts a `chat_id`/`chat_settings_id` from the caller must verify active membership, not just authentication — this is the exact class of bug fixed in #511/#512.

## Creating Migrations

```bash
# Create: scripts/migrations/NNN_description.sql
# Apply: psql "$DATABASE_URL" -f scripts/migrations/NNN_description.sql
# Record: INSERT INTO schema_version (version, description) VALUES (N, 'Description');
```

Check existing migrations with `ls scripts/migrations/` for the next version number.
