# Cloud Deployment Guide

Deploy Storydump to cloud infrastructure (Railway + Neon) for multi-tenant SaaS operation.

**Estimated time:** 2-3 hours for complete setup
**Prerequisites:** GitHub account, Railway account, Neon account

---

## Architecture Overview

```
GitHub Repo
    |
    ├──► Railway (worker)     python -m src.main
    |    - Telegram bot polling
    |    - Posting scheduler loop
    |    - Lock cleanup loop
    |    - Media sync loop
    |
    ├──► Railway (web)        uvicorn src.api.app:app
    |    - OAuth callbacks (Instagram, Google Drive)
    |    - Onboarding Mini App
    |    - API endpoints
    |
    └──► Neon (PostgreSQL)
         - All application data
         - SSL required

External APIs:
  - Telegram Bot API (polling, free)
  - Instagram Graph API (OAuth tokens, free)
  - Google Drive API (user OAuth, free)
  - Cloudinary (media hosting, free tier)
```

---

## 1. Database Setup (Neon)

### Create a Neon Project

1. Sign up at [console.neon.tech](https://console.neon.tech)
2. Create a new project (name: `storydump`)
3. Note your connection details from the dashboard

### Run Schema Setup

Connect via `psql` using the Neon connection string:

```bash
psql "postgresql://storydump_user:PASSWORD@ep-xxx.region.neon.tech/storydump?sslmode=require"
```

Run the base schema and all migrations in order:

```bash
# Base schema
psql "$DATABASE_URL" -f scripts/setup_database.sql

# All migrations, through the runner (never a psql loop): the worker's
# pre-deploy step runs the same command and keeps the ledger
python -m scripts.migration_runner apply
```

### Verify Schema

```sql
-- Check schema version
SELECT * FROM schema_version ORDER BY version;

-- Should show version 21 as latest

-- Check uuid-ossp extension
SELECT extname FROM pg_extension WHERE extname = 'uuid-ossp';
```

### Connection Pool Sizing

The pool is pinned in code — 10 connections per process, no overflow
(`src/services/target/unit_of_work.py`) — and no variable sizes it. Count the
processes (the API and the worker) against the plan's connection limit.

---

## 2. Railway Deployment

### Two-Process Architecture

Storydump requires **two processes** on Railway:

1. **Worker** (`python -m src.main`): Telegram bot + scheduler + background loops
2. **Web** (`uvicorn src.api.app:app`): OAuth callbacks + onboarding Mini App

The included `Procfile` defines both:

```
worker: python -m src.main
web: uvicorn src.api.app:app --host 0.0.0.0 --port ${PORT:-8000}
```

### Setup Steps

1. **Connect GitHub repo** in Railway dashboard
2. **Create two services** from the same repo:
   - Service 1: Set start command to `python -m src.main` (worker)
   - Service 2: Set start command to `uvicorn src.api.app:app --host 0.0.0.0 --port ${PORT:-8000}` (web)
3. **Set build command** for both: `pip install -r requirements.txt && pip install -e .`
4. **Generate a domain** for the web service (needed for OAuth callbacks)
5. **Configure environment variables** (see Section 3 below)

### Health Checks

- **Worker**: Railway monitors the process — if it exits, it restarts automatically. The app has built-in SIGTERM handling for graceful shutdown.
- **Web**: Railway health checks hit the web service automatically. FastAPI responds to requests by default.

---

## 3. Environment Variables

Configure these in the Railway dashboard for **both** services:

### Required (All Deployments)

| Variable | Description | Example |
|---|---|---|
| `DATABASE_URL` | The database-OWNER Neon connection string the migration runner applies with (includes SSL) | `postgresql://owner:pass@ep-xxx.neon.tech/storydump?sslmode=require` |
| `TARGET_DATABASE_URL` | The runtime login the API and the worker run as (no DDL rights) | `postgresql://app:pass@ep-xxx.neon.tech/storydump?sslmode=require` |
| `TARGET_TELEGRAM_BOT_TOKEN` | The bot the worker sends with, from BotFather | `123456:ABC-DEF1234ghIkl` |
| `TARGET_TELEGRAM_BOT_USERNAME` | That bot's @username, without the @ | `storydump_app_bot` |
| `TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN` | The secret the API expects Telegram to echo on every delivery | a long random string |
| `ENCRYPTION_KEY` | Fernet key for token encryption | Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |

### Database (the components)

There is no `DB_*` alternative for a deployed service: the worker refuses to
boot without `TARGET_DATABASE_URL` and the API answers 503 on every data route
without it. The `DB_*` components serve the test harness and `make` only.

### OAuth & API (Web Service)

| Variable | Description | Example |
|---|---|---|
| `OAUTH_REDIRECT_BASE_URL` | Railway web service URL | `https://your-app.up.railway.app` |
| `FACEBOOK_APP_SECRET` | Meta Developer App Secret | `abc123...` |
| `GOOGLE_CLIENT_ID` | Google OAuth Client ID | `xxx.apps.googleusercontent.com` |
| `GOOGLE_CLIENT_SECRET` | Google OAuth Client Secret | `GOCSPX-...` |

### Cloudinary (the frame's transit to Meta)

| Variable | Description | Example |
|---|---|---|
| `CLOUDINARY_CLOUD_NAME` | Cloudinary cloud name | `dxyz123` |
| `CLOUDINARY_API_KEY` | Cloudinary API key | `123456789012345` |
| `CLOUDINARY_API_SECRET` | Cloudinary API secret | `abc_secret...` |

### Media, schedule and dry run

Not variables: the media source (a connected Google Drive folder), the schedule
and **Dry Run Mode** are per-workspace settings in the ledger, set on the web
(Settings › Integrations and Settings › General).

`.env.example` is the reference for every variable the code reads — a test
keeps it in exact agreement with the tree.

---

## 4. Telegram Bot Setup

### BotFather Configuration

1. Message [@BotFather](https://t.me/botfather) on Telegram
2. `/newbot` and follow prompts to get your bot token
3. `/setcommands` to register commands:

```
start - Open Storydump (setup & config)
status - System health & media overview
setup - Quick settings & toggles
queue - View upcoming posts
next - Send next post now
pause - Pause delivery
resume - Resume delivery
history - Recent post history
sync - Sync media from Drive
cleanup - Delete recent bot messages
help - Show available commands
```

4. Add the bot as admin to your Telegram channel/group
5. Get the channel ID (send a message, check via `https://api.telegram.org/bot<TOKEN>/getUpdates`)

### Polling vs Webhooks

The target-tier bot is **webhook-fed**: the API registers the webhook itself at startup in production (`documentation/operations/telegram-webhook.md`), and `storydump webhook status` checks it. Polling is the legacy worker's mode only (retired with #1216). If you want to switch to webhooks later, set the webhook URL to your Railway domain.

---

## 5. Instagram OAuth Setup

Instagram account connection uses browser-based OAuth, which requires the web service (FastAPI) to be running.

### Meta Developer Setup

1. Create an app at [developers.facebook.com](https://developers.facebook.com)
2. Add the **Instagram** product and use **API setup with Instagram business login**
3. Configure the OAuth redirect URI: `https://<your API host>/auth/instagram-login/callback`
   (production: `https://api.storydump.app/auth/instagram-login/callback`; see
   [`instagram-login-setup.md`](instagram-login-setup.md) Step 3)
4. Required permissions: `pages_show_list`, `pages_read_engagement`, `instagram_basic`, `instagram_content_publish`, `business_management`
5. Set `INSTAGRAM_APP_ID` and `INSTAGRAM_APP_SECRET` (Instagram Login); set `FACEBOOK_APP_SECRET` too if Meta signs this app's callbacks with the Facebook app's secret (`src/services/target/meta_callbacks.py` tries both)

### How It Works (Post-Phase 04)

1. User sends `/connect` in Telegram
2. Bot replies with an "Connect Instagram" button (OAuth link)
3. User clicks, authorizes in browser, Meta redirects to callback
4. Callback exchanges code for long-lived token, stores encrypted in DB
5. Bot notifies user of successful connection

### App Review

Without Meta App Review, only users with roles on the Meta app (admins, developers, testers) can use the API. For a single-brand use case, this is sufficient.

---

## 6. Google Drive OAuth Setup

Google Drive is the recommended media source for cloud deployments.

### Google Cloud Setup

1. Create a project at [console.cloud.google.com](https://console.cloud.google.com)
2. Enable the **Google Drive API**
3. Create **OAuth 2.0 Client ID** (Web application type)
4. Add authorized redirect URI: `https://your-app.up.railway.app/auth/google-drive/callback`
5. Set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` env vars on the **API and the worker** — both read Google Drive through the same credential door, and the worker refreshes the hourly access token from the stored refresh token (#1247); a service without them refuses by naming the variables

### How It Works (Post-Phase 05)

1. User connects Google Drive via the onboarding wizard (`/start`)
2. Bot replies with "Connect Google Drive" button (OAuth link)
3. User clicks, authorizes Google account access
4. Callback exchanges code for tokens, stores encrypted per-tenant in DB
5. User's Google Drive folders become available as media sources
6. Media sync pulls files from the user's shared folder

### User Experience

End users just need a Google account with a Drive folder containing their media. They never interact with GCP or service accounts. The GCP project setup is a one-time task for the app operator.

---

## 7. Cloudinary Setup

Required for publishing: without all three variables the worker parks the publish kind by name.

1. Create account at [cloudinary.com](https://cloudinary.com) (free tier: 25 credits/month)
2. Get credentials from Dashboard: Cloud Name, API Key, API Secret
3. Set env vars: `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET`

The worker's transit store (`src/services/target/transit.py`) uploads a story's frame for Meta to fetch, and the `reap_transit_assets` job removes it afterwards.

---

## 8. Monitoring & Operations

### Logs

Railway provides log streaming in the dashboard. The app logs to stdout via the console handler, which Railway captures automatically.

### Database Backups

- **Neon**: Automatic point-in-time recovery (paid plans). Free tier has limited retention.
- **Manual**: `pg_dump "$DATABASE_URL" > backup_$(date +%Y%m%d).sql`

### Health Monitoring

From a laptop with a token minted under Settings › API tokens:
- `storydump health` — the API's three health surfaces and the bot's webhook, judged (exit 4 when not well)
- `storydump deploys` — the latest deployment of each service; `storydump doctor` for the local setup
- see `documentation/operations/monitoring.md`

### Service Management

- **Restart**: Via Railway dashboard or `railway restart`
- **Logs**: `railway logs` or dashboard
- **Shell**: `railway shell` for running CLI commands

### Cost Estimates

| Service | Tier | Cost |
|---------|------|------|
| Neon | Free | $0 (0.5 GB, 190 compute-hours) |
| Railway | Starter | ~$5-10/month (worker + web) |
| Cloudinary | Free | $0 (25 credits/month) |
| Telegram Bot API | Free | $0 |
| Instagram/Meta API | Free | $0 (rate-limited) |
| Google Drive API | Free | $0 (quota-limited) |
| **Total** | | **~$5-10/month** |

---

## 9. Security Checklist

- [ ] All secrets stored as Railway environment variables (never in code)
- [ ] `ENCRYPTION_KEY` generated and set (for token encryption in DB)
- [ ] Database password is strong and unique
- [ ] Telegram bot token is kept secret
- [ ] Instagram/Facebook app secret is kept secret
- [ ] Cloudinary API secret is kept secret
- [ ] Google OAuth client secret is kept secret
- [ ] `.env` file is NOT committed (verified in `.gitignore`)
- [ ] SSL/TLS for all connections (Neon requires it, all APIs use HTTPS)
- [ ] Connection pool sizing appropriate for Neon tier
- [ ] `OAUTH_REDIRECT_BASE_URL` points to your Railway HTTPS domain

---

## 10. Troubleshooting

| Problem | Solution |
|---------|----------|
| Database connection fails | Check `DATABASE_URL` or `DB_*` vars. Ensure `DB_SSLMODE=require` for Neon. |
| Neon connection limit exceeded | The pool is pinned in code (10 per process, no overflow); no variable sizes it. Count the processes against the plan's connection limit. |
| Telegram bot not responding | Verify `TARGET_TELEGRAM_BOT_TOKEN` is the bot named by `TARGET_TELEGRAM_BOT_USERNAME`; `storydump health` reports the webhook. Check Railway worker logs. |
| `ENCRYPTION_KEY not configured` | Generate one: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| OAuth callback fails | Check `OAUTH_REDIRECT_BASE_URL` matches your Railway web domain. |
| Mini App won't load | Ensure web service is running and domain has HTTPS. Check `OAUTH_REDIRECT_BASE_URL`. |
| Service restarts frequently | Check memory limits in Railway. Review logs for OOM or crash loops. |
| "idle in transaction" | Built-in cleanup runs every 30 seconds. Reduce pool size if persistent. |
| Instagram API rate limited | Meta caps API publishing per account over a rolling 24 h window; the limit is Meta's and no variable overrides it. The worker's advisory pre-check (`TARGET_USAGE_PRECHECK_ENABLED`, default off) reads the account's live quota (`GET /{ig-user}/content_publishing_limit`), and `storydump story <id>` shows what Meta answered for a refused publish. |

---

## Quick Start Checklist

1. [ ] Create the Neon database and apply the migrations with `python -m scripts.migration_runner apply`
2. [ ] Create the Railway project with two services (`worker` + `storydump`, the API)
3. [ ] Set all required environment variables
4. [ ] Create Telegram bot via BotFather, get token
5. [ ] Add bot to your channel/group as admin
6. [ ] Generate Railway domain for web service
7. [ ] Set `OAUTH_REDIRECT_BASE_URL` to the Railway HTTPS domain
8. [ ] Configure Meta Developer App (Instagram OAuth redirect URI)
9. [ ] Configure Google Cloud OAuth (Google Drive redirect URI)
10. [ ] Deploy and verify bot responds to `/start`
11. [ ] Test onboarding wizard opens from `/start`
12. [ ] Test Instagram OAuth flow (via `/connect` or wizard)
13. [ ] Test Google Drive OAuth flow (via onboarding wizard)
14. [ ] Turn the workspace's **Dry Run Mode** off (the web, Settings › General) when ready for live posting
