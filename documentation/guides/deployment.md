# Deployment Checklist

This checklist covers everything you need to do **outside of code** to get Storydump running in production on Railway + Neon.

## Prerequisites

- [ ] GitHub account (public repository)
- [ ] Railway account ([railway.app](https://railway.app))
- [ ] Neon account ([console.neon.tech](https://console.neon.tech))
- [ ] Instagram account for your business
- [ ] Telegram account

---

## 1. Telegram Bot Setup (15 minutes)

### Create Bot with BotFather

- [ ] Open Telegram and search for **@BotFather**
- [ ] Send `/newbot` to BotFather
- [ ] Follow prompts:
  - Choose bot name (e.g., "Storydump Bot")
  - Choose bot username (e.g., "storydump_yourcompany_bot")
- [ ] **Save the bot token** (looks like `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`)

### Create Telegram Channel

- [ ] Create a new Telegram channel (not group)
  - Name it (e.g., "Storydump Queue - Internal")
  - Set to Private (only your team can see)
- [ ] Add your bot as an administrator:
  1. Go to channel info
  2. Tap "Administrators"
  3. Tap "Add Administrator"
  4. Search for your bot username
  5. Give it "Post Messages" permission

### Test Bot

- [ ] Send `/start` to your bot
- [ ] Verify it responds (if not, service isn't running yet - that's okay)

**Deliverables** (the approval group is connected per workspace on the web, so
there is no channel or admin chat to configure):
```
TARGET_TELEGRAM_BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
TARGET_TELEGRAM_BOT_USERNAME=your_bot
```

---

## 2. Database Setup - Neon (10 minutes)

### Create Neon Project

- [ ] Sign up at [console.neon.tech](https://console.neon.tech)
- [ ] Create a new project (name: `storydump`)
- [ ] Note your connection string from the dashboard

### Initialize Schema

```bash
# Set your Neon connection string
export DATABASE_URL="postgresql://user:pass@ep-xxx.neon.tech/storydump?sslmode=require"

# Apply the migrations through the runner — the same command the worker's
# pre-deploy step runs (`railway.toml`); it keeps the ledger the API's
# `storydump posture` and `storydump doctor` read. Never a psql loop.
python -m scripts.migration_runner status
python -m scripts.migration_runner apply
```

### Verify Setup

```bash
psql "$DATABASE_URL" -c "\dt"
# Should show all tables
```

### Connection Pool Sizing

The pool is pinned in code — 10 connections per process, no overflow
(`src/services/target/unit_of_work.py`) — and no variable sizes it. Count the
processes (the API and the worker) against the plan's connection limit.

**Deliverables:**
```
DATABASE_URL=postgresql://user:pass@ep-xxx.neon.tech/storydump?sslmode=require
```

---

## 3. Media Setup (5 minutes)

### Google Drive (Recommended for Cloud)

Media is sourced from Google Drive when running on Railway:

- [ ] Create a Google Drive folder for your media
- [ ] Organize subfolders by category (e.g., `memes/`, `merch/`)
- [ ] Upload your Instagram story images (JPG, JPEG, PNG, GIF)
- [ ] Note the folder ID from the URL

Google Drive OAuth will be configured during the onboarding wizard (`/start` command).

---

## 4. Railway Deployment (15 minutes)

### Create Railway Project

- [ ] Go to [railway.app](https://railway.app) and create a new project
- [ ] Connect your GitHub repository

### Create Two Services

Railway requires two services from the same repo:

**Service 1: `worker`**
- Start command: `python -m src.main`
- Build command: `pip install -r requirements.txt && pip install -e . && mkdir -p /tmp/media`

**Service 2: `storydump` (the API)**
- Start command: `uvicorn src.api.app:app --host 0.0.0.0 --port ${PORT:-8000}`
- Build command: `pip install -r requirements.txt && pip install -e . && mkdir -p /tmp/media`

### Generate Domain

- [ ] Generate a public domain for the Web service (needed for OAuth callbacks)
- [ ] Note the URL (e.g., `https://your-app.up.railway.app`)

### Configure Environment Variables

Set these on **both** services in the Railway dashboard:

```bash
# The database: the OWNER login the migration runner applies the schema with
# (railway.toml's preDeployCommand), and the runtime login the services run as
DATABASE_URL=postgresql://owner:pass@ep-xxx.neon.tech/storydump?sslmode=require
TARGET_DATABASE_URL=postgresql://app:pass@ep-xxx.neon.tech/storydump?sslmode=require

# The bot (documentation/operations/telegram-webhook.md for the webhook)
TARGET_TELEGRAM_BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
TARGET_TELEGRAM_BOT_USERNAME=your_bot
TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN=<a long random string>

ENCRYPTION_KEY=<generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">
LOG_LEVEL=INFO

# OAuth and the web front end (Web service)
OAUTH_REDIRECT_BASE_URL=https://your-app.up.railway.app
WEB_APP_URL=https://app.example.com
```

### Validate Deployment

```bash
# Check worker logs
railway logs --service worker

# Check web service is responding
curl https://your-app.up.railway.app/health
```

---

## 5. Initial Data Load (2 minutes)

### Connect Google Drive

- [ ] Send `/start` to the Telegram bot
- [ ] Follow the onboarding wizard to connect Google Drive
- [ ] Select your media folder

### Sync Media

```bash
# Via Railway shell
storydump sync <source_id> --workspace <ws>   # or Sync Now under Settings › Integrations
# the Media Library on the web lists what was indexed
```

### Create Initial Schedule

```bash
# Nothing to run by hand: the worker mints each day's slots from the schedule card (Settings › General).
```

### Verify Queue

```bash
storydump floating --workspace <ws>
# approved stories waiting to post; the web's Queue shows every slot
```

---

## 6. Team Onboarding (5 minutes per person)

### Add Team Members

Each team member needs to:

- [ ] Join the Telegram channel
- [ ] Send `/start` to the bot (creates their user account)
- [ ] Test posting workflow:
  1. Wait for notification in channel
  2. Post story to Instagram manually
  3. Click "Posted" button

### Promote Admins (Optional)

```bash
# Members live on the web: the Members card under Settings › General (invite, remove).
# Changing a member's role is the `change_role` command — registered, not yet built.
```

---

## 7. Backup Strategy (10 minutes)

### Neon Built-in Backups

Neon provides automatic point-in-time recovery on paid plans.

### Manual Backup

```bash
# Dump from Neon
pg_dump "$DATABASE_URL" -F c -f ~/backups/storydump_$(date +%Y%m%d).dump
```

### Test Restore

```bash
# Restore to a test database
pg_restore -d "$TEST_DATABASE_URL" ~/backups/storydump_YYYYMMDD.dump
```

See [backup-restore.md](../operations/backup-restore.md) for full backup procedures.

---

## 8. Monitoring Setup (10 minutes)

### Railway Dashboard

Railway provides built-in log streaming and service monitoring.

### Health Check via Telegram

Use the bot itself as a health indicator:
- `/status` shows system health, queue state, and recent activity

### External Monitoring (Optional)

- [ ] Sign up for UptimeRobot (free tier)
- [ ] Monitor your Railway web service URL
- [ ] Get email/SMS alerts if service goes down

See [monitoring.md](../operations/monitoring.md) for detailed monitoring setup.

---

## 9. Instagram Account Preparation (5 minutes)

### Business Account Setup

- [ ] Convert to Instagram Business Account (if not already)
  1. Go to Settings -> Account
  2. Switch to Professional Account
  3. Choose Business category
  4. Connect Facebook Page (optional for Phase 1)

### Story Preparation

Phase 1 is **manual posting**, so prepare your workflow:

- [ ] Keep Instagram app logged in
- [ ] Enable notifications for Telegram channel
- [ ] Have media downloading method ready (if posting from phone)

### Media Transfer Options

**Option 1: Telegram (simplest)**
- Bot already sends the image in notification
- Download from Telegram, post to Instagram
- Click "Posted" button

**Option 2: Cloud sync**
- Use Google Drive to sync media folder to phone
- Download from cloud when notification arrives

---

## 10. Testing Phase (1-2 days)

### Initial Testing Checklist

- [ ] **Day 1 Morning:**
  - Verify the workspace's **Dry Run Mode** is ON (the web, Settings › General) — it is a per-workspace setting in the ledger, not a variable
  - Verify notifications arrive in Telegram
  - Test "Posted" and "Skip" buttons
  - Check the queue via `storydump floating` or the web's Queue

- [ ] **Day 1 Afternoon:**
  - Turn the workspace's **Dry Run Mode** OFF (the web, Settings › General)
  - Wait for first real notification
  - Post ONE story to Instagram manually
  - Click "Posted" button
  - Verify posting history is recorded

- [ ] **Day 2:**
  - Monitor all scheduled posts
  - Verify team members can interact
  - Check no duplicate notifications
  - Verify posting history is recorded

### Success Criteria

- [ ] Notifications arrive at scheduled times
- [ ] Team can click Posted/Skip buttons
- [ ] No duplicate posts scheduled
- [ ] Locks prevent reposting within 30 days
- [ ] Service stays running for 24+ hours
- [ ] Logs show no errors

---

## 11. Production Launch (Go Live!)

### Final Checklist

- [ ] All tests passing
- [ ] Team trained on workflow
- [ ] Backup system verified
- [ ] Monitoring alerts configured
- [ ] Emergency contacts documented

### Go Live

```bash
# Turn the workspace's Dry Run Mode off on the web (Settings › General);
# it takes effect for posts approved from then on — no restart

# Monitor first day
railway logs --service worker
```

### First Week Monitoring

- [ ] Check logs daily via Railway dashboard
- [ ] Verify all posts going out
- [ ] Monitor team feedback
- [ ] Track any issues
- [ ] Adjust the schedule if needed (the schedule card on the web, Settings › General)

---

## Ongoing Maintenance

### Daily
- [ ] Check Telegram channel for any issues
- [ ] Verify posts are being published

### Weekly
- [ ] Add new media to Google Drive folder
- [ ] Run media sync: Sync Now under Settings › Integrations, or `storydump sync <source_id> --workspace <ws>`
- [ ] Check health: `/status` in Telegram

### Monthly
- [ ] Review posting schedule effectiveness
- [ ] Verify database backups
- [ ] Update media library
- [ ] Review team permissions

### As Needed
- [ ] Review the schedule card under Settings › General
- [ ] Invite team members from the Members card under Settings › General (role changes: `change_role`, not yet built)
- [ ] Clear old queue items if needed

---

## Troubleshooting Quick Reference

### Service Not Starting
```bash
railway logs --service worker | tail -50
# Check for missing env vars or build errors
```

### Bot Not Responding
```bash
# Verify token with Telegram API
curl https://api.telegram.org/bot<YOUR_TOKEN>/getMe

# The webhook's verdict, from the API's health report
storydump health
```

### Database Connection Failed
```bash
# Test connection
psql "$DATABASE_URL" -c "SELECT version();"
```

### No Notifications Arriving
```bash
# Check queue has items
storydump floating --workspace <ws>

# Check service is running
railway logs --service worker
```

---

## Summary: What You Need

### External Services
- Telegram bot (via @BotFather)
- Telegram channel (private)
- Instagram business account (optional for Phase 1)

### Cloud Infrastructure
- Railway account (worker + web services)
- Neon PostgreSQL database
- Google Drive (media storage)

### One-Time Setup
- Bot configuration (~15 min)
- Database setup (~10 min)
- Railway deployment (~15 min)
- Team onboarding (~5 min/person)

### Ongoing
- Add media to Google Drive weekly
- Monitor Telegram notifications daily
- Manual Instagram posting when notified

**Total setup time: ~1-2 hours**

---

Ready to go? Start with **Section 1: Telegram Bot Setup** and work through the checklist!
