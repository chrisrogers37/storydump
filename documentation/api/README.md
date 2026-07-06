# API Reference

**Status:** New 2026-07-06 — this API existed in code with no reference doc until this audit. Scoped to what exists today; not a full OpenAPI spec.

This is the REST API served by `uvicorn src.api.app:app` (the API service — see root `CLAUDE.md`). It backs two clients: the Telegram Mini App (launched from inside Telegram, via signed `initData`) and the `landing/` Next.js web dashboard (via a BFF proxy + JWT). It is **not** a public/third-party API — there is no API-key model, no versioning, no WebSocket layer, and no SDK.

## Mounting

| Prefix | Router | File |
|--------|--------|------|
| `/auth` | `oauth_router` | `src/api/routes/oauth.py` |
| `/api/onboarding` | `onboarding_router` (aggregates 3 sub-routers) | `src/api/routes/onboarding/__init__.py` |
| `/static` | `StaticFiles` | served from `STATIC_DIR` |

## Auth model

There is no API key. Every `/api/onboarding/*` request carries `init_data` (a Telegram WebApp `initData` string, or a signed URL token for browser-launched links) plus a `chat_id`. The gate is `_validate_request()` in `src/api/routes/onboarding/helpers.py`, which resolves to one of two authorization paths:

- **Bound token** — a signed URL token, or `initData` launched from a Telegram group, cryptographically carries a `chat_id`. That binding *is* the authorization; the gate only rejects replay against a *different* `chat_id` (403 on mismatch).
- **Unbound token** — `initData` launched from a Telegram DM carries no `chat_id`, so the request-supplied `chat_id` is attacker-suppliable. The gate instead does a server-side active-membership lookup via `MembershipService.is_active_member(user_id, chat_id)` (403 if not an active member).

This split exists because of a **2026-06-28 security fix** (#511/#512, PR #519): the unbound-token path used to skip authorization entirely, letting any authenticated bot user act on any tenant's data by supplying an arbitrary `chat_id`. See [`SECURITY_REVIEW.md`](../SECURITY_REVIEW.md) §11 for the incident writeup, and [`multi-account-dashboard.md`](../planning/multi-account-dashboard.md) for how `user_chat_memberships` (the membership table this check reads) fits into the wider multi-instance design.

The web dashboard (`landing/`) does not call this API directly — it goes through a Next.js BFF proxy that holds a JWT with `activeChatId`, and the BFF is responsible for turning that into a valid `init_data`/`chat_id` pair server-side. See [`landing-vercel-deployment.md`](../guides/landing-vercel-deployment.md).

**Known gap:** as of 2026-07-06, three call sites (`SetupStateService.get_setup_state()`, `DashboardService._resolve_chat_settings_id()`, the `onboarding/init` chain) still default to creating a `chat_settings` row rather than checking membership first, so DM logins can still create phantom tenants. Not a data-isolation bug (that's fixed), but worth knowing before extending this API. See `.claude/rules/telegram.md`.

## Rate limiting

Global default: **30 requests/minute per IP** (`SlowAPIMiddleware`, `src/api/rate_limit.py`, wired in `src/api/app.py`). Tighter per-endpoint limits override the default on mutating settings endpoints:

| Limit | Endpoints |
|-------|-----------|
| 10/minute | `POST /toggle-setting`, `POST /update-setting`, `POST /update-string-setting` |
| 5/minute | `POST /sync-media`, `POST /add-account` |

## Routes

### `/auth` — OAuth callbacks (`oauth.py`)

| Method & Path | Purpose |
|---|---|
| `GET /auth/instagram/start` | Begin legacy Facebook Login OAuth flow |
| `GET /auth/instagram/callback` | Facebook Login OAuth callback |
| `GET /auth/instagram-login/callback` | Instagram Login OAuth callback (preferred flow, added in the 2026-05 credential refactor) |
| `GET /auth/google-drive/start` | Begin Google Drive OAuth flow |
| `GET /auth/google-drive/callback` | Google Drive OAuth callback |

### `/api/onboarding` — Setup wizard (`setup.py`)

| Method & Path | Purpose |
|---|---|
| `GET /init` | Bootstrap setup state for a chat |
| `GET /oauth-url/{provider}` | Get the OAuth start URL for a provider |
| `POST /media-folder` | Set the Google Drive media folder |
| `POST /start-indexing` | Kick off media indexing |
| `POST /schedule` | Set initial posting schedule during setup |
| `POST /complete` | Mark setup wizard complete |

### `/api/onboarding` — Dashboard (`dashboard.py`)

| Method & Path | Purpose |
|---|---|
| `GET /instances` | List the caller's instances (backs `/start`'s DM instance picker and the web dashboard's instance picker) |
| `GET /queue-detail`, `/history-detail` | Queue and posting-history detail views |
| `GET /media-stats`, `/media-library` | Media library stats and listing |
| `GET /accounts` | Instagram accounts for the active instance |
| `GET /analytics`, `/analytics/*` (9 sub-routes) | Dashboard analytics: schedule recommendations, categories, schedule preview, content reuse, service health, category drift, dead content, approval latency, team performance |
| `GET /system-status` | Health/status summary |
| `POST /upload-media` | Upload media directly via the dashboard |
| `GET /audit-log` | Tenant audit log (membership-gated — see Auth model) |
| `GET /media/{media_id}/thumbnail` | Thumbnail proxy |

### `/api/onboarding` — Settings (`settings.py`)

| Method & Path | Purpose |
|---|---|
| `POST /toggle-setting`, `/update-setting`, `/update-string-setting` | Mutate `chat_settings` fields (dry-run mode, posts/day, etc.) |
| `POST /switch-account`, `/add-account`, `/remove-account` | Instagram account management for the active instance |
| `POST /disconnect-gdrive`, `/sync-media` | Google Drive connection + manual sync trigger |
| `POST /queue-preview` | Preview upcoming queue slots |
| `POST /category-mix`, `/update-category-mix` | Category posting-ratio configuration |

## What's intentionally not here

- No OpenAPI/Swagger doc generation is wired up beyond FastAPI's default `/docs` (not verified whether it's disabled in production — check `app.py` before relying on it)
- No WebSocket endpoints
- No API versioning scheme
- Request/response body schemas aren't reproduced here — see `src/api/routes/onboarding/models.py` for the Pydantic models
