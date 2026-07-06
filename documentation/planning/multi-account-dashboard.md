# Multi-Account Dashboard Migration

**Status:** Implemented. All 6 phases (1a-4) were built and merged the same day this plan was consolidated (2026-04-17), with follow-up hardening PRs through 2026-06-28. The feature has been live in production for roughly 2.5 months — a cross-tenant data isolation bug in the membership/auth gate was found and fixed on 2026-06-28 (PR #511/#512/#519), consistent with real membership data being in active use. Re-verified against code on 2026-07-06: 29 of 33 checklist items are confirmed fully implemented as specified (several exceed spec). 2 confirmed gaps and 1 item that cannot be verified from static code remain — see "Verification Notes" below and the inline notes on each phase's checklist.
**Created:** 2026-04-17
**Verified against code:** 2026-07-06
**Reviewed by:** Rajan (architecture), Greg (implementation)

## Problem

When a user logs in via the web dashboard or opens the Mini App from their DM, they only see the DM-scoped instance (empty). Their real instances live in group chats (e.g., TL group with 4554 media items). There's no way to see or switch between instances.

## Current Architecture

The system already has multi-tenancy via `chat_settings` — each `telegram_chat_id` is an independent tenant with its own media, queue, history, schedule, and settings. This works great for group chats.

**The gap:** There's no `user ↔ chat_settings` relationship table. When a user logs in (web or Mini App DM), the system creates/resolves a `chat_settings` for their DM chat_id, which is a separate empty instance. It can't discover which group chat instances the user belongs to.

## Design Decisions

These decisions resolve contradictions identified during review. They are final.

1. **DM = management console + opt-in solo instances.** The DM is primarily the management console (instance picker, onboarding). But users CAN create a 1:1 DM instance via `/new` → "solo" option. This is opt-in, never auto-created. The key distinction: phantom DM `chat_settings` rows (created silently by `get_or_create`) are eliminated. Only explicitly created DM instances exist. Web login always lands on the instance picker ("System Management"), showing both group and solo instances.

2. **`display_name` lives only on `chat_settings`.** One canonical name per instance, set via `/name` in the group, visible to all members. No per-user labeling on the membership table.

3. **JWT stores `userId` and `activeChatId` only.** No instance list in the token. Instances are fetched dynamically via `GET /api/instances`. Selected instance stored as `activeChatId` in the JWT, reissued on switch.

4. **DM onboarding state lives in `onboarding_sessions` table** (separate from `chat_settings.onboarding_step` which tracks per-instance setup). Two state machines, two tables. The DM machine is short-lived (create instance, link to group, done).

5. **`my_chat_member` event is the primary group-linking mechanism**, not `startgroup` deep links. The `my_chat_member` event fires regardless of how the bot was added (deep link, manual add, invite link). `startgroup` is a convenience, `/link` is the manual fallback.

6. **`get_settings()` must be split** into `get_settings()` (returns None) and `get_or_create_settings()` (current behavior). This is a prerequisite for Phase 2, not part of it. ~15 call sites to audit. Greg recommends `create_if_missing: bool = True` parameter for backward compat, flipping callers one by one.

## Target Architecture

```
User (Telegram identity)
  └── user_chat_memberships (new join table)
       ├── Instance A: "TL Enterprises" (group chat, 4554 media, 10/day)
       ├── Instance B: "Personal Brand" (group chat, 200 media, 3/day)
       └── Instance C: "My Solo Account" (1:1 DM, opt-in, 100 media, 5/day)
```

### New Table: `user_chat_memberships`

```sql
CREATE TABLE user_chat_memberships (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    chat_settings_id UUID NOT NULL REFERENCES chat_settings(id),
    instance_role VARCHAR(20) NOT NULL DEFAULT 'member',  -- 'owner', 'admin', 'member'
    joined_at TIMESTAMP NOT NULL DEFAULT NOW(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE(user_id, chat_settings_id)
);
```

Note: field is `instance_role` (not `role`) to avoid collision with `users.role` which is a system-level concept.

### New Table: `onboarding_sessions`

```sql
CREATE TABLE onboarding_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    step VARCHAR(50) NOT NULL DEFAULT 'naming',  -- naming → awaiting_group → complete
    pending_instance_name VARCHAR(100),
    pending_chat_settings_id UUID REFERENCES chat_settings(id),
    expires_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE(user_id)  -- one active onboarding per user
);
```

### `chat_settings` Enhancement

```sql
ALTER TABLE chat_settings ADD COLUMN display_name VARCHAR(100);
```

### Auto-Population Strategy

Memberships are created automatically:
1. **On bot interaction in a group chat:** Hook into `TelegramService._get_or_create_user()` — after user resolution, check if membership exists for `(user.id, chat_settings_id)`, create if not. Already runs on every interaction.
2. **On `my_chat_member` event:** When bot is added to a group, create membership for the user who added it (with `instance_role = 'owner'`).
3. **Backfill migration:** Scan `user_interactions` for historical group chat memberships.

## Auth Flow

```
Current:
  Telegram Login → JWT{userId, chatId=userId} → Dashboard(chatId=userId) → Empty instance

New:
  Telegram Login → JWT{userId, activeChatId=null} → GET /api/instances → [{chatId, name, stats}...]
    → User picks instance → POST /api/instances/:id/select → JWT reissued with activeChatId
    → Dashboard(chatId=activeChatId) → Real instance with data
```

### BFF Proxy Guard

If `activeChatId` is null when the BFF proxy tries to forward a dashboard request, redirect to the instance picker instead of proxying. This prevents `generateUrlToken(null, userId)` from crashing `validate_url_token()` on the Python side.

### URL Token Auth Gap

URL tokens bake in `chat_id` and are valid for 1 hour. If a user is removed from a group mid-session, their token remains valid. The BFF proxy should validate `activeChatId` against active memberships on each request. Low severity but worth implementing.

## Dashboard Changes

**Web Dashboard (Next.js):**
1. After Telegram login, fetch `GET /api/instances` for the logged-in user
2. If 1 instance → go directly to that instance's dashboard
3. If multiple → show instance picker: name, media count, last post, status
4. Instance picker persists selection via JWT reissue, allows switching via header dropdown

**Mini App (Telegram):**
1. When opened from group chat → show that group's instance directly (unchanged, use presence/absence of `chat_id` in validated initData as routing signal)
2. When opened from DM → show instance picker

## Onboarding Flow (New User via DM)

### First-Time Flow

```
User sends /start in DM
  ↓
Bot: "Welcome to Storydump! Let's set up your first posting instance."
  ↓
Step 1: "What do you want to call this instance?" → user types "TL Enterprises"
  ↓
Step 2: "Add me to the group chat where your team will review posts."
        [Button: "Add to Group Chat" → t.me/storydump_bot?startgroup=setup_{session_id}]
        "Bot already in your group? Run /link in that group."
  ↓
Bot is added to group → my_chat_member event fires → auto-links pending session
  (OR: startgroup deep link fires if bot freshly added)
  (OR: user runs /link {session_id} in the group as manual fallback)
  ↓
Step 3: Bot creates chat_settings for group, sets display_name
        Creates user_chat_membership (instance_role=owner)
  ↓
Instance-level onboarding begins in the group chat (existing wizard:
  connect Instagram, connect media source, set schedule)
  ↓
Bot in DM: "You're all set! Open your dashboard:"
           [Open Dashboard → Mini App with instance pre-selected]
```

Notes:
- `startgroup` payload = `setup_{onboarding_sessions.id}` (42 chars, within Telegram's 64-char limit)
- `startgroup` silently fails if bot is already in the group — the `/link` fallback and `my_chat_member` handler cover this case
- DM conversation state persisted in `onboarding_sessions`, times out after 24h (cleaned up via scheduler loop piggyback)
- `/start` handler currently doesn't parse `context.args` — must add arg parsing for deep link payloads

### Returning User Flow (DM)

```
User sends /start in DM (has existing instances)
  ↓
Bot: "Welcome back! Your instances:"
  1. TL Enterprises (4,554 media · 10/day · last post 6h ago)
  2. Personal Brand (200 media · 3/day · paused)
  ↓
  [Manage TL Enterprises] [Manage Personal Brand] [+ New Instance]
```

### `/start` Handler Branching

Goes from 2 branches to 5. Extract to a `StartCommandRouter` class:

1. Group + `startgroup` payload → link pending onboarding session to this group
2. Group + no payload → standard group setup (existing behavior, unchanged)
3. DM + new user (0 memberships) → onboarding conversation
4. DM + returning user (1+ memberships) → instance list
5. DM + active onboarding session → resume in-progress onboarding

### Bot Commands

| Command | Context | Behavior |
|---------|---------|----------|
| `/start` | DM | Instance list (returning) or onboarding (new) |
| `/start setup_*` | Group | Link group to pending onboarding session |
| `/start` | Group | Existing group setup (unchanged) |
| `/new` | DM | Create new instance (shortcut) |
| `/instances` | DM | List + manage all instances |
| `/name <name>` | Group | Set display_name for this instance |
| `/link <session_id>` | Group | Manual fallback to link group to onboarding session |

### Offboarding: Bot Kicked from Group

Register `my_chat_member` handler for `ChatMemberUpdated` events. When bot is removed from a group:
- Mark all `user_chat_memberships` for that `chat_settings` as `is_active = false`
- Instance disappears from users' instance pickers
- `chat_settings` row preserved (data not deleted, can be restored if bot is re-added)

## Backfill Strategy

The backfill is the foundation. Must complete and verify before Phase 2 ships.

### Pre-requisite Index

```sql
CREATE INDEX CONCURRENTLY idx_user_interactions_backfill
ON user_interactions(user_id, telegram_chat_id)
WHERE user_id IS NOT NULL AND telegram_chat_id < 0;
```

### Backfill Query

```sql
INSERT INTO user_chat_memberships (user_id, chat_settings_id, instance_role, joined_at)
SELECT DISTINCT
    ui.user_id,
    cs.id,
    'member',
    MIN(ui.created_at)
FROM user_interactions ui
JOIN chat_settings cs ON cs.telegram_chat_id = ui.telegram_chat_id
WHERE ui.user_id IS NOT NULL
  AND ui.telegram_chat_id < 0  -- groups/supergroups only (no DM phantoms)
  AND ui.interaction_type IN ('command', 'callback')  -- exclude bot_response
GROUP BY ui.user_id, cs.id
ON CONFLICT (user_id, chat_settings_id) DO NOTHING;
```

### Post-Backfill: Role Promotion

Call `getChatAdministrators` for each active group, update matching memberships to `admin` or `owner`. Rate limit: Telegram allows 30 calls/sec, batch with 50ms delays.

### Verification (gate for Phase 2 deploy)

```sql
-- Must return 0 rows before Phase 2 can ship
SELECT u.id, u.telegram_username, COUNT(DISTINCT ui.telegram_chat_id) as groups
FROM users u
JOIN user_interactions ui ON ui.user_id = u.id
WHERE ui.telegram_chat_id < 0
GROUP BY u.id, u.telegram_username
HAVING COUNT(DISTINCT ui.telegram_chat_id) > 0
AND u.id NOT IN (SELECT user_id FROM user_chat_memberships);
```

### Known Gap

Backfill can't recover "who added the bot to the group" — only who interacted. Users who added the bot but never sent a command won't have memberships. The `my_chat_member` handler solves this going forward. Accept this gap.

## The `get_settings()` Split

This is the single hardest refactor and a prerequisite for Phase 2. Currently `get_settings()` calls `get_or_create()` unconditionally — ~15 call sites silently create phantom `chat_settings` rows for DM users.

### Approach

Add `create_if_missing: bool = True` parameter to `get_settings()` for backward compat, then flip callers one by one.

### Call Sites That SHOULD Still Create (group context)

- `handle_start` in group context
- `TelegramService` callback handlers (operating on an existing group)
- Scheduler loop (`get_all_active()` — doesn't call `get_settings`, already safe)

### Call Sites That MUST NOT Create (DM context)

- `handle_start` in DM (check memberships first)
- `SetupStateService.get_setup_state()` when called from DM
- `DashboardService._resolve_chat_settings_id()` — creates phantoms on every BFF proxy page load
- `onboarding/init` endpoint — `onboarding_init()` → `_get_setup_state()` → `get_settings()`
- BFF proxy requests with `activeChatId = null`

### Phantom Cleanup

After the `get_settings()` split ships, existing phantom DM `chat_settings` rows will still exist and appear in `get_all_active()` scheduler queries (no-op but wastes cycles). Add a cleanup migration to delete `chat_settings` rows where `telegram_chat_id > 0` AND no media/queue/history references exist.

## Verification Notes (2026-07-06)

A full code audit (reading actual implementation, not just checking file existence) against every checkbox below found the plan substantially implemented. Confirmed gaps, in order of importance:

1. **Phase 2a's "audit and flip ~15 call sites" was not completed.** Of the plan's 4 explicitly named "MUST NOT create" DM call sites, only `handle_start` is actually safe (structurally — it never calls `get_settings()` on the DM chat at all). `SetupStateService.get_setup_state()`, `DashboardService.resolve_chat_settings_id()` (renamed from `_resolve_chat_settings_id`, used by 8+ dashboard query classes), and the `onboarding/init` → `_get_setup_state()` chain all still call `get_settings()` at its `create_if_missing=True` default, and will still silently create phantom DM `chat_settings` rows on every hit — including, per this doc's own text, "every BFF proxy page load." This directly contradicts migration `024_cleanup_phantom_dm_chat_settings.sql`'s header comment, which asserts "Phase 2a's get_settings() split prevents new phantoms." The actual call-site count is 34 (not ~15); only 10 explicitly pass `create_if_missing=False`, and all 10 are group-callback contexts, not the DM contexts this refactor targeted. Net effect: migration 024 cleaned up historical phantoms once, but the code paths that originally created them are still capable of creating new ones.
2. **Phase 4's `/webapp/instances` Mini App picker is built but unreachable.** The page renders correctly and matches the spec's field list (name, media count, posts/day, last post, status), but nothing else in the repository links to it — no Telegram WebApp SDK / `initData` usage anywhere in the file or the `webapp/` folder, and zero repo-wide references to the route. The actual bot flow routes single-instance users to an unrelated FastAPI-served static page (`/webapp/onboarding`) and sends multi-instance DM users a plain-text message with inline-keyboard buttons — never a Mini App link to this page. Effectively dead code today.
3. **Phase 1b's prod backfill verification cannot be confirmed from code alone.** `scripts/backfill_memberships.py` has a working `--verify` mode (auto-run after `--apply`) that implements the doc's exact gating query and exits non-zero on gaps, but whether it was actually run against production and returned 0 rows is a runtime fact outside what static code can prove. No run-log, ops runbook, or execution record was found in `documentation/`. Circumstantial evidence it passed is strong (Phases 2a-4, which depend on this gate, were built and merged the same day; the feature has been live in prod for ~2.5 months with real membership data), but this should be confirmed with a live DB check before treating it as closed.

Minor, non-blocking deviations from spec (also noted inline on their checkboxes): `/link` intentionally does not take a `<session_id>` argument (always scoped to the caller's own pending session instead); `ConversationService` uses purpose-named methods (`set_instance_name()`, `link_group()`, `cleanup_expired()`, etc.) rather than the generic `advance_step()` / `get_current_step()` / `timeout_check()` named in the plan; `POST /api/instances/:id/select`'s `:id` is a `telegram_chat_id`, not the `chat_settings_id` UUID the schema names as PK; `StartCommandRouter` grew a 6th branch (mobile-login deep link) beyond the 5 originally planned.

## Implementation Plan

### Phase 1a: Migration + Model + Repository (1 PR, ~400 LOC, low risk) — DONE (PR #231, 2026-04-17)

- [x] Migration 023: `user_chat_memberships` table, `onboarding_sessions` table, `display_name` on `chat_settings` — `scripts/migrations/023_multi_account_data_layer.sql`, matches spec exactly
- [x] Migration 023: Index on `user_interactions(user_id, telegram_chat_id)` for backfill — same migration, `idx_user_interactions_backfill`, created `CONCURRENTLY` outside the transaction block
- [x] `UserChatMembership` model + repository (~120 LOC) — model fields match the migration exactly; repository is named `MembershipRepository` (`src/repositories/membership_repository.py`), not `UserChatMembershipRepository` as named in the plan, with `get_for_user`/`get_for_chat`/`get_membership`/`create_membership`/`deactivate_for_chat`/`deactivate`
- [x] `OnboardingSession` model + repository — repository is named `OnboardingRepository` (`src/repositories/onboarding_repository.py`), not `OnboardingSessionRepository`
- [x] Auto-create membership hook for group interactions — lives in `TelegramUserManager.get_or_create_user()` → `_ensure_membership()` (`src/services/core/telegram_user_manager.py`), called from `TelegramService._get_or_create_user()` which delegates to it; checks `telegram_chat_id < 0` before creating
- [x] `DashboardService.get_user_instances(telegram_user_id)` — method exists on `DashboardService` (`src/services/core/dashboard_service.py`) and delegates to `InstanceDashboardQueries.get_user_instances()` (`src/services/core/dashboard_instance_queries.py`, ~38 LOC); joins memberships → chat_settings and aggregates media count, posts/day, last post

### Phase 1b: Backfill + Verification (script, run on prod) — DONE (PR #232, 2026-04-17), prod execution unconfirmed

- [x] Backfill script with group-only filter (`telegram_chat_id < 0`) — `scripts/backfill_memberships.py`; matches the plan's SQL intent field-for-field (filters `telegram_chat_id < 0`, excludes `bot_response` interaction type, `ON CONFLICT DO NOTHING`)
- [x] Role promotion via `getChatAdministrators` — same script, `--promote` flag; calls `getChatAdministrators` per active group, maps creator/administrator → owner/admin, 50ms delay between calls
- [ ] Run verification query — must return 0 rows — **code capability confirmed** (script's `--verify` mode implements the doc's exact query and auto-runs after `--apply`, exiting non-zero on gaps), **but actual execution against production and its result cannot be confirmed from code alone.** No run-log or ops record found. Needs a live DB check to close out — see Verification Notes above.
- [x] **GATE: Phase 2 cannot deploy until backfill verified** — gate mechanism is enforced in code (non-zero exit on verification failure); Phases 2a-4 shipped the same day, consistent with the gate having passed, though this is inferred rather than directly observed

### Phase 2a: `get_settings()` Split + `/start` Refactor (1 PR, ~400 LOC, high risk) — DONE with a gap (PR #233, 2026-04-17)

- [x] Add `create_if_missing` parameter to `get_settings()` — `src/services/core/settings_service.py:58`, `def get_settings(self, telegram_chat_id: int, create_if_missing: bool = True)`; defaults `True` for backward compat, `False` returns `get_by_chat_id()` (possibly `None`) instead of `get_or_create()`
- [ ] Audit and flip ~15 call sites (DM paths → `create_if_missing=False`) — **incomplete.** 34 total call sites found (not ~15); only 10 pass `create_if_missing=False` explicitly, and all 10 are group-callback contexts, not the DM contexts this item targeted. Of the plan's 4 named "MUST NOT create" DM sites, only `handle_start` is actually safe; `SetupStateService.get_setup_state()`, `DashboardService.resolve_chat_settings_id()`, and the `onboarding/init` chain were never flipped and still auto-create phantom DM rows. See Verification Notes above.
- [x] `StartCommandRouter` class with 5-branch `/start` handler — `src/services/core/start_command_router.py`; all 5 plan branches present (internally renumbered/relabeled), plus a 6th branch added later (`login` deep-link redirect for mobile sign-in, per #455/#457) not in the original plan
- [x] `ConversationService` wrapping `onboarding_sessions` — functionality matches (advance state / query current session / expire stale sessions), but method names differ from the plan: actual public methods are `start_onboarding`, `get_current_session`, `get_session_by_id`, `set_instance_name`, `link_group`, `link_group_to_instance`, `cleanup_expired` — no `advance_step()`, `get_current_step()`, or `timeout_check()`
- [x] DM onboarding flow (naming → awaiting_group → complete) — confirmed exact step values and transitions across `OnboardingSession`, `ConversationService`, and `StartCommandRouter`
- [x] Returning user instance list in DM — `_handle_returning_user()` calls `DashboardService.get_user_instances()` and renders the list with "Manage"/"+ New Instance" buttons

### Phase 2b: Group Linking + Event Handlers (1 PR, ~400 LOC, medium risk) — DONE (PR #240, 2026-04-17)

- [x] `my_chat_member` handler: auto-link pending onboarding on bot-added, deactivate memberships on bot-kicked — `src/services/core/telegram_membership.py`, registered via `ChatMemberHandler` in `telegram_service.py`; matches spec exactly on both add and kick paths
- [x] `startgroup` deep link arg parsing in `/start` handler — `start_command_router.py`, parses `context.args[0]` for a `setup_` prefixed payload
- [x] `/link <session_id>` fallback command — implemented and registered, but **intentionally does not accept a `<session_id>` argument** (documented deviation in its own docstring); always resolves the caller's own pending `awaiting_group` session instead
- [x] `/name <name>` command for setting instance display_name — `telegram_commands.py`, updates `chat_settings.display_name` for the invoking group
- [x] `/instances` command for DM instance management — `telegram_commands.py`, lists instances via `DashboardService.get_user_instances()` with per-instance "Manage" buttons
- [x] Onboarding session timeout cleanup in scheduler loop — `ConversationService.cleanup_expired()` (24h TTL), invoked hourly from `src/services/core/loops/scheduler_loop.py`, piggybacked on the existing retention tick counter exactly as the plan described; logs dropouts before deleting (#247)

### Phase 3: API + Auth (1 PR, ~300 LOC, medium risk — can parallel with Phase 2b) — DONE (PR #235/#236, #246, 2026-04-17)

- [x] `SessionPayload.chatId` → `SessionPayload.activeChatId: number | null` — `landing/src/lib/session.ts`, field renamed exactly as specified
- [x] Auth route: set `activeChatId = null` on login (not `chatId = body.id`) — `landing/src/app/api/auth/telegram/route.ts`
- [x] `GET /api/instances` endpoint — `landing/src/app/api/instances/route.ts` calls through to `DashboardService.get_user_instances()` via `src/api/routes/onboarding/dashboard.py`
- [x] `POST /api/instances/:id/select` — reissues the JWT with `activeChatId` set, and genuinely authorizes against the caller's live active memberships before switching (403 if not a member — not a rubber stamp). Minor: `:id` is actually a `telegram_chat_id`, not the `chat_settings_id` UUID the schema names as PK — cosmetic naming mismatch, not a security issue.
- [x] BFF proxy: use `activeChatId`, redirect to picker if null — implemented, split across `landing/src/middleware.ts` (page-level redirect to `/instances`) and the proxy route (422 JSON error for API calls, since a raw redirect wouldn't render a picker for client-side fetches)
- [x] BFF proxy: validate `activeChatId` against active memberships on each request — implemented, not deferred despite being flagged "low severity" in this doc's own "URL Token Auth Gap" section; shipped same day in a follow-up (PR #246), re-checks live memberships on every proxied request and force-clears `activeChatId` if the membership has gone stale

### Phase 4: Frontend (1 PR, ~500 LOC, low risk — depends on Phase 3) — DONE except entry point (PR #235/#236, 2026-04-17)

- [x] Instance picker page/component (name, media count, posts/day, last post, status badge) — `landing/src/app/instances/page.tsx`, all 5 fields present and correctly wired to the `Instance` type
- [x] Instance switcher dropdown in dashboard header — `landing/src/components/dashboard/header.tsx`, calls `POST /api/instances/:id/select` and refreshes
- [x] Update dashboard layout to show active instance name — shown in `header.tsx` (not the layout file itself); note `sidebar.tsx` still shows a static site name rather than the active instance name
- [x] 0-instance edge case → "Set up your first instance" CTA linking to DM bot — bespoke branch in `instances/page.tsx` with a hardcoded `t.me/storydump_bot` link; does not reuse the generic `empty-state.tsx` component (that component has no zero-instance logic at all)
- [ ] Mini App DM entry point: `/webapp/instances` picker view — **built but unreachable, effectively dead code.** The page exists and renders the correct fields, but nothing in the repo links to it, it has no Telegram WebApp SDK/`initData` integration, and the real bot flow routes single-instance users to an unrelated static page (`/webapp/onboarding`) and multi-instance DM users to a plain-text message with inline-keyboard buttons — never to this page. See Verification Notes above.

### Effort Summary

| Phase | LOC | PRs | Risk | Dependencies |
|-------|-----|-----|------|-------------|
| 1a: Data Layer | ~400 | 1 | Low | None |
| 1b: Backfill | script | — | Low | Phase 1a |
| 2a: get_settings + /start | ~400 | 1 | High | Phase 1b verified |
| 2b: Group Linking | ~400 | 1 | Medium | Phase 2a |
| 3: API + Auth | ~300 | 1 | Medium | Phase 1a (can parallel 2b) |
| 4: Frontend | ~500 | 1 | Low | Phase 3 |
| **Total** | **~2000** | **5-6** | | |

Phase 2a is the critical path. Everything else can be parallelized around it.

## Open Questions

1. **Permission model** — can all group members see the dashboard, or only admins/owners?
   - Decision: `instance_role` field on memberships. Owners/admins get full access, members get read-only. Enforced at BFF proxy level.
2. **Instance creation from web dashboard** — support it?
   - Decision: DM bot only for now. Web dashboard manages existing instances.
3. **Instance limits per user?**
   - Decision: No limit for now, revisit if needed.

---

## Review Appendix

Full architecture review (Rajan) and engineering review (Greg) were conducted 2026-04-17. All 20 findings have been incorporated into the consolidated plan above:

- Rajan: 3 blockers (backfill ordering, get_or_create phantoms, startgroup failure), 7 suggestions (display_name dedup, JWT schema, backfill filter, index, onboarding table, URL token gap, role naming), 5 nits (initData routing, payload length, solo user contradiction, bot-kicked handling, backfill role promotion)
- Greg: 5 additional concerns (scheduler phantom iteration, DashboardService phantom factory, null activeChatId crash, backfill gap for bot-adders, onboarding/init phantom creation), implementation complexity analysis, code reuse inventory, effort estimates, PR sequencing
