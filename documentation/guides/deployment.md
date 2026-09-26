# Deployment Checklist

The operator's checklist for standing up the hosted deployment — or a second
environment shaped like it (staging, a preview) — on Railway + Neon, and for
taking the first workspace from sign-in to its first posted story. Storydump is
one deployment that serves many workspaces: a new customer is a workspace on
it, not another run through this page
([`deployment-options.md`](deployment-options.md)). The reference behind each
step is [`cloud-deployment.md`](cloud-deployment.md).

## Prerequisites

- [ ] GitHub account (public repository)
- [ ] Railway account ([railway.app](https://railway.app))
- [ ] Neon account ([console.neon.tech](https://console.neon.tech))
- [ ] Vercel account, for the web front end ([`landing-vercel-deployment.md`](landing-vercel-deployment.md))
- [ ] A Meta developer app, a Google Cloud project and a Cloudinary account
- [ ] A Telegram account

---

## 1. Telegram Bot Setup (10 minutes)

The deployment runs **one** bot for every workspace.

### Create the bot with BotFather

- [ ] Open Telegram and search for **@BotFather**
- [ ] Send `/newbot` and follow the prompts (a name, then a username)
- [ ] **Save the bot token** (looks like `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`)
- [ ] `/setjoingroups` → the bot → *Enable* — a workspace adds the bot to its own group
- [ ] `/setprivacy` → the bot → *Disable* (or plan to make the bot an admin of each group)

No channel is created here and no command list is registered: each workspace
binds its own group on the web (Section 5), and the bot serves `/start` links
and the buttons on its cards, not typed commands
([`telegram-webhook.md`](../operations/telegram-webhook.md)).

**Deliverables:**
```
TARGET_TELEGRAM_BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
TARGET_TELEGRAM_BOT_USERNAME=your_bot
TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN=<python -c "import secrets; print(secrets.token_urlsafe(48))">
```

---

## 2. Database Setup - Neon (10 minutes)

### Create Neon Project

- [ ] Sign up at [console.neon.tech](https://console.neon.tech)
- [ ] Create a new project (name: `storydump`)
- [ ] Note the owner connection string from the dashboard

### Initialize Schema

A fresh database is built by four files applied by hand and then the migration runner — the same
sequence `make init-db` runs locally (`Makefile:105-113`). The sequence, why each file is needed
(without step 0 the runner stops at 050; without the hand-made table, at 078) and what `apply`
prints for the two gated files (`owed (manual) 079 …`, `owed (manual) 080 …`) are written down
ONCE, in [`cloud-deployment.md` › Build the schema on a fresh database](cloud-deployment.md#build-the-schema-on-a-fresh-database).
Export the OWNER connection string as `DATABASE_URL` first: the runner applies DDL with it, and
keeps the ledger `storydump posture` and `storydump doctor` read. The sequence ends with
`python -m scripts.migration_runner apply`; the check below reads what it wrote.

### Verify Setup

```bash
python -m scripts.migration_runner status   # the ledger against this checkout
psql "$DATABASE_URL" -c "\dt"               # the target tables, in `public`
```

### The runtime login

- [ ] Decide what `TARGET_DATABASE_URL` connects as. The design is `svc_ingress`
  for the API and `svc_worker` for the worker, which step 0 created; giving
  them passwords and switching the services is
  [`runtime-database-roles.md`](../operations/runtime-database-roles.md).
  `/health` reports the login a service actually holds (`db_role`).

### Connection Pool Sizing

The pool is pinned in code — 10 connections per process, no overflow
(`src/services/target/unit_of_work.py`) — and no variable sizes it. Count the
processes (the API and the worker) against the plan's connection limit.

**Deliverables:**
```
DATABASE_URL=postgresql://owner:pass@ep-xxx.neon.tech/storydump?sslmode=require
TARGET_DATABASE_URL=postgresql://app:pass@ep-xxx.neon.tech/storydump?sslmode=require
```

---

## 3. Provider Apps (30 minutes)

- [ ] **Meta** — the Instagram Login app and its redirect URI:
  [`instagram-login-setup.md`](instagram-login-setup.md)
  → `INSTAGRAM_APP_ID`, `INSTAGRAM_APP_SECRET`
- [ ] **Google** — one OAuth client with both redirect URIs (sign-in and
  Drive): [`cloud-deployment.md`](cloud-deployment.md) §6
  → `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`
- [ ] **Cloudinary** — the transit store a story's frame rides to Meta:
  `cloud-deployment.md` §7
  → `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET`

---

## 4. Railway Deployment (15 minutes)

### Create Railway Project

- [ ] Go to [railway.app](https://railway.app) and create a new project
- [ ] Connect your GitHub repository

### Create Two Services

Two services from the same repo. `railway.toml` gives both the build command,
the pre-deploy migration step, the `/health` check and the restart policy.

**Service 1: `worker`**
- Start command: `python -m src.main` — the target worker (`src.worker`): the
  clock, the job lanes, the sender. It posts; see the safety rules in
  `AGENTS.md` before starting one anywhere

**Service 2: `storydump` (the API)**
- Start command: `uvicorn src.api.app:app --host 0.0.0.0 --port ${PORT:-8000}`

### Generate Domain

- [ ] Generate a public domain for the `storydump` service (OAuth callbacks and
  Telegram's webhook need it)
- [ ] Note the URL (e.g., `https://your-app.up.railway.app`; production's is
  `https://api.storydump.app`)

### Configure Environment Variables

The table, by service, is `cloud-deployment.md` §3; `.env.example` names every
variable the tree reads and is the reference. A second environment (staging, a
preview) must also set `TARGET_TELEGRAM_WEBHOOK_URL` — its default is
production's `https://api.storydump.app/webhooks/telegram` — and use its own
bot: one bot holds one webhook. The core:

```bash
# BOTH services
# The database: the OWNER login the migration runner applies the schema with
# (railway.toml's preDeployCommand), and the runtime login the services run as
DATABASE_URL=postgresql://owner:pass@ep-xxx.neon.tech/storydump?sslmode=require
TARGET_DATABASE_URL=postgresql://app:pass@ep-xxx.neon.tech/storydump?sslmode=require
# The bot (documentation/operations/telegram-webhook.md for the webhook)
TARGET_TELEGRAM_BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
TARGET_TELEGRAM_BOT_USERNAME=your_bot
# The SAME key on both services; neither starts without a valid one. Generate it
# on a first install only — a new key cannot read credentials already stored.
ENCRYPTION_KEY=<generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">
GOOGLE_CLIENT_ID=xxx.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-...
WEB_APP_URL=https://app.example.com
LOG_LEVEL=INFO

# The API only
TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN=<a long random string>
OAUTH_REDIRECT_BASE_URL=https://your-app.up.railway.app
INSTAGRAM_APP_ID=...
INSTAGRAM_APP_SECRET=...

# The worker only
CLOUDINARY_CLOUD_NAME=...
CLOUDINARY_API_KEY=...
CLOUDINARY_API_SECRET=...
```

### Validate Deployment

```bash
# The worker's boot: its database role, its live job kinds, a parked seam if any
railway logs --service worker

# The API
curl https://your-app.up.railway.app/health

# From a laptop, with a token (Settings › API tokens, then `storydump login`)
storydump health            # the three health surfaces and the webhook, judged
storydump webhook status    # the bot, the registration, the door
storydump doctor            # this laptop: token, API, config, Railway, the ledger
```

---

## 5. The First Workspace (10 minutes)

Everything here happens on the web front end, signed in with Google.

- [ ] Sign in and create a workspace
- [ ] **Settings › Integrations → Link Telegram** — attaches your Telegram
  account to your user (the link opens the bot with a `/start link-…` payload)
- [ ] **Settings › Integrations → Add a Telegram group** — opens Telegram's
  group picker, adds the bot to the group you choose and binds that group to
  the workspace. Approval cards go there
- [ ] **Settings › Integrations → Connect Google Drive**, then **Add folder**
  and pick the media folder. Subfolders are walked to any depth, every image
  and video is indexed, and a file's category is the top-level folder it sits
  under (e.g., `memes/`, `merch/`)
- [ ] **Sync Now** on the folder — or, from a laptop:

```bash
storydump sync <source_id> --workspace <ws>
# the Media Library on the web lists what was indexed
```

- [ ] **Settings › Accounts → Connect Instagram**
- [ ] **Settings › General** — the schedule card (posts per day, the window,
  the timezone), the category weights, and the toggles

Nothing is scheduled by hand: the worker's clock mints each account's next
slot from that schedule.

### Verify

```bash
storydump account <handle> --workspace <ws>   # cap, zone, next slot, recent outcomes
storydump jobs --since 3h                     # the queue, by kind, lane and state
# the web's Queue lists every story the ledger has not closed
```

---

## 6. Team Onboarding (5 minutes per person)

- [ ] Each person signs in on the web with Google and links their Telegram
  (Settings › Integrations → Link Telegram)
- [ ] Add them to the workspace's Telegram group. Anyone with a linked Telegram
  who posts in — or is added to — a bound group becomes a member of that
  workspace, at the member role
  (`src/services/target/membership_sync.py`)
- [ ] Or invite them from the Members card under Settings › General. Outbound
  email does not send yet (`AGENTS.md`, *What is deliberately not wired*), so
  an emailed invitation is created and not delivered
- [ ] Removing a member is the Members card too. Changing a member's role is
  the `change_role` command — registered, not yet built

Leaving the Telegram group removes nobody.

---

## 7. Backup Strategy (10 minutes)

### Neon Built-in Backups

Neon restores to a point in time within the project's history retention.

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

Railway provides built-in log streaming and service monitoring. `railway.toml`
sets the restart policy (`ON_FAILURE`, 10 retries): a process that exits
non-zero — the worker does, when a supervised task dies — is restarted.

### From a laptop

- `storydump health` — the API's `/health`, `/health/scheduling` and
  `/health/posting`, and the bot's webhook, judged by the fleet monitors' own
  verdicts; exit 4 when not well
- `storydump deploys --watch --commit <sha>` — follows a deploy of both services

### The fleet monitors

- [ ] Deploy the two pollers that page when scheduling stalls or posting
  fails: [`scheduling-monitor.md`](../operations/scheduling-monitor.md) and
  [`posting-monitor.md`](../operations/posting-monitor.md)

### External Monitoring (Optional)

- [ ] Point an uptime checker (e.g. UptimeRobot) at the API's `/health`
- [ ] Get email/SMS alerts if the service goes down

See [monitoring.md](../operations/monitoring.md) for detailed monitoring setup.

---

## 9. Instagram Account Preparation (5 minutes)

### Business Account Setup

- [ ] Convert to an Instagram Business or Creator account (if not already)
  1. Go to Settings -> Account
  2. Switch to Professional Account
  3. Choose a category

A Facebook Page is not required.

### How a story gets posted

Every due slot produces an approval card — the media itself, with buttons — in
the workspace's Telegram group, and the same story is actionable in the web's
Queue. What the card offers depends on the workspace's **Instagram API** toggle
(Settings › General; off on a new workspace):

| Toggle | The card offers | Posting |
|---|---|---|
| off | **✅ Posted myself** · **⏭️ Skip** · **🚫 Reject** · **📱 Open Instagram** | a person posts the story in the Instagram app, then presses **Posted myself** |
| on | the same, plus **🚀 Post now** | **Post now** approves the story and the worker publishes it through the Instagram API |

(`src/services/target/prompts.py:56-57`, `:165-173`.) What each button leaves
behind: a posted story locks its media for that account for the workspace's
repost period (30 days by default; `posted_effects`,
`src/services/target/intent_ledger.py:184`), **Skip** locks it for the skip
period (45 days by default), **Reject** locks it for good
(`src/services/target/command_executors.py:461-515`, `src/config/defaults.py:21-22`).

---

## 10. Testing Phase (1-2 days)

### Initial Testing Checklist

- [ ] **Day 1 Morning** (Instagram API off):
  - Verify a card arrives in the Telegram group when the first slot comes due
  - Post that story by hand, press **Posted myself**, and check the card is
    restated as posted
  - Test **Skip** and **Reject** on the next ones
  - Read a story's whole timeline: `storydump story <intent_id>`

- [ ] **Day 1 Afternoon** (if the workspace will publish through the API):
  - Turn **Dry Run Mode** ON, then **Instagram API** ON (Settings › General).
    Both are per-workspace settings in the ledger, not variables
  - Press **Post now** on the next card (a card sent before the toggle has no
    such button): a dry run does everything a post does — the
    day's cap, the media's rotation and lock, the card — and calls no provider;
    the story is recorded as posted with `published_via = 'dry_run'`
    (`src/services/target/publish_pipeline.py:318-327`). It spends that media's
    rotation, so use media you do not mind waiting 30 days for
  - Turn **Dry Run Mode** OFF and post ONE real story with **Post now**
  - `storydump floating --workspace <ws>` shows approved stories still waiting
    between attempts; `storydump story <intent_id>` shows what Meta answered

- [ ] **Day 2:**
  - Monitor all scheduled posts
  - Verify team members can act on cards
  - `storydump burst --since <the first slot's time>` for the day as one timeline

### Success Criteria

- [ ] Cards arrive when slots come due
- [ ] Team members can press the buttons
- [ ] No slot produces two stories
- [ ] A posted item is not offered again inside the repost period
- [ ] Both services stay up for 24+ hours
- [ ] Logs show no errors

---

## 11. Production Launch (Go Live!)

### Final Checklist

- [ ] All tests passing
- [ ] Team trained on workflow
- [ ] Backup system verified
- [ ] Monitoring alerts configured
- [ ] Emergency stop known: `storydump pause --workspace <ws>` (or **Pause
  Posting** under Settings › General) — a restart does not stop posting
- [ ] Meta App Review status understood: until Advanced Access is granted, only
  accounts with a role on the Meta app, or allowlisted by Meta, can connect
  ([`meta-app-review.md`](../operations/meta-app-review.md))

### Go Live

```bash
# Turn the workspace's Dry Run Mode off on the web (Settings › General);
# it takes effect for posts approved from then on — no restart

# Monitor first day
railway logs --service worker
storydump burst --since 3h --watch
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
- [ ] Check the Telegram group for cards nobody acted on
- [ ] Verify posts are being published

### Weekly
- [ ] Add new media to the Google Drive folder
- [ ] Run media sync: Sync Now under Settings › Integrations, or `storydump sync <source_id> --workspace <ws>`
- [ ] Check health: `storydump health`

### Monthly
- [ ] Review posting schedule effectiveness
- [ ] Verify database backups
- [ ] Update media library
- [ ] Review team permissions

### As Needed
- [ ] Review the schedule card under Settings › General
- [ ] Invite team members from the Members card under Settings › General (role changes: `change_role`, not yet built)
- [ ] Reconnect an Instagram account that reads **Reconnect needed** (Settings › Accounts)

---

## Troubleshooting Quick Reference

### Service Not Starting
```bash
railway logs --service worker | tail -50
# `FATAL: TARGET_DATABASE_URL is unset` — set it on the worker (exit 2)
# a failed pre-deploy step — a migration failed, or DATABASE_URL is not the owner
```

### Bot Not Responding
```bash
# The bot, the registration and the door, in one read (no secret is printed)
storydump webhook status

# The webhook's verdict, from the API's health report
storydump health
```

### Database Connection Failed
```bash
# Test connection
psql "$DATABASE_URL" -c "SELECT version();"
```

### No Cards Arriving
```bash
# What is owed or lost on the chats
storydump outbox --since 3h

# Whether slots are being planned and cards sent
storydump jobs --since 3h

# Whether the worker parked its Telegram channel at boot
railway logs --service worker
```

See [troubleshooting.md](../operations/troubleshooting.md) for more.

---

## Summary: What You Need

### External Services
- One Telegram bot (via @BotFather)
- A Meta developer app (Instagram Login), a Google OAuth client, a Cloudinary account

### Cloud Infrastructure
- Railway: the `worker` and `storydump` (API) services
- Neon PostgreSQL
- Vercel: the web front end

### Per Workspace (on the web, no deploy)
- A Telegram group bound to the workspace
- A Google Drive folder of media
- An Instagram Business or Creator account

### Ongoing
- Add media to Google Drive
- Act on the cards — or let **Post now** publish through the API
- `storydump health`, and the fleet monitors

---

Start with **Section 1: Telegram Bot Setup** and work through the checklist.
