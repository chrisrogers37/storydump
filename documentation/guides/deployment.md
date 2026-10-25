# Deployment Checklist

The operator's checklist for standing up the hosted deployment — or a second
environment shaped like it (staging, a preview) — on Railway + Neon, and for
taking the first workspace from sign-in to its first posted story. Storydump is
one deployment that serves many workspaces: a new customer is a workspace on
it, not another run through this page
([`deployment-options.md`](deployment-options.md)). The reference behind each
step is [`cloud-deployment.md`](cloud-deployment.md).

## Prerequisites

- [ ] GitHub account with access to the repository
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
and the buttons on its cards, not typed commands.

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

Build a fresh database from the models and `src/models/target/ddl/`, then
adopt the empty `000_initial.sql` marker and apply later migrations. The
owner login applies schema changes and needs `CREATEROLE`. Locally,
`make create-db init-db` runs the sequence; `make reset-db` drops and rebuilds.
The explicit commands are in
[Build the schema on a fresh database](cloud-deployment.md#build-the-schema-on-a-fresh-database).
A fresh build adopts `000`, `001` and `002`, so nothing is owed. On a database
whose schema predates them, such as one converted from the retired chain, `001`
and `002` are owner-applied cutover steps, and automatic apply reports them as
owed. Follow the [cutover runbook](../operations/schema-cutover.md) for the
rehearsal, separate constraint validation and confirmed snapshot backup.

### Verify Setup

```bash
python -m scripts.migration_runner status   # the ledger against this checkout
psql "$DATABASE_URL" -c "\dt"               # the target tables, in `public`
```

### The runtime login

- [ ] Set each service's `TARGET_DATABASE_URL` to its runtime login: `svc_ingress`
  for the API and `svc_worker` for the worker. `src/models/target/ddl/01_roles.sql`
  defines these roles; each needs a password and a connection string of its own.
  The API's operating details report the login it actually holds (`db_role`,
  `storydump health --json`, for `OPS_USER_IDS`).

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
  Drive): [`cloud-deployment.md`](cloud-deployment.md#6-google-oauth-setup)
  → `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`
- [ ] **Cloudinary** — the transit store a story's frame rides to Meta:
  [Cloudinary setup](cloud-deployment.md#7-cloudinary-setup)
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
- [ ] Required dashboard setting: open **Settings > Deploy > Start Command**
  for `worker` and set `python -m src.main` before deploying.
- Start command: `python -m src.main` — the target worker (`src.worker`): the
  clock, the job lanes, the sender. It posts to Instagram for whatever
  database it is pointed at

**Service 2: `storydump` (the API)**
- Start command: `uvicorn src.api.app:app --host 0.0.0.0 --port ${PORT:-8000}`

### Generate Domain

- [ ] Generate a public domain for the `storydump` service (OAuth callbacks and
  Telegram's webhook need it)
- [ ] Note the URL (e.g., `https://your-app.up.railway.app`; production's is
  `https://api.storydump.app`)

### Configure Environment Variables

The table, by service, is the environment table in `cloud-deployment.md`; `.env.example` names every
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
# The bot
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

- [ ] Admit your own email first: sign-up is invite-only (`fn_signup_admitted`), and on a fresh
  database nobody is admitted, the first account included. As the owner (the
  `DATABASE_URL` login), in lower case:
  `psql "$DATABASE_URL" -c "INSERT INTO signup_admissions (email) VALUES ('you@example.com');"`
  By hand this once only: until your account exists and the API's
  `OPS_USER_IDS` names it (`cloud-deployment.md`'s Quick Start, step 11),
  nothing else can admit anyone. Every later admission is `storydump admit`,
  the ping's Admit or the site's `/ops/waitlist`.
- [ ] Sign in with that Google account and create a workspace
- [ ] **Settings › Integrations → Link Telegram** — attaches your Telegram
  account to your user (the link opens the bot with a `/start link-…` payload)
- [ ] **Settings › Integrations → Add a Telegram group** — opens Telegram's
  group picker, adds the bot to the group you choose and binds that group to
  the workspace. Approval cards go there
- [ ] **Settings › Integrations → Connect Google Drive**, then **Add folder**
  and pick the media folder. Subfolders are walked to any depth, every image
  and video is indexed, and a file's category is the top-level folder it sits
  under (e.g., `announcements/`, `behind-the-scenes/`)
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
  (Settings › Integrations → Link Telegram). Sign-up is gated (`fn_signup_admitted`): a new
  account needs a pending invitation addressed to its email, sent from an
  active workspace by someone still its owner or an admin (invitation legitimacy checks), or an
  admission: `storydump admit <email>`, for any address, on the waitlist or
  not (yours to run, as a person in the API's `OPS_USER_IDS`), tap Admit
  on the waitlist ping, or use the site's `/ops/waitlist` (the dashboard
  header's Owner link, or the ping's Open waitlist ↗), which admits any
  address too. `storydump waitlist` and `/ops/waitlist` show where each
  address stands. An admission's email, once the worker sends it,
  carries a one-time link: through it the person signs up with any Google
  account, and the one they choose becomes their sign-in. Signing in as the
  admitted address needs no link until the spot is used.
  `storydump admit <email> --resend`, or Send again on `/ops/waitlist`, sends
  the email again, and its new link replaces the last
- [ ] Add them to the workspace's Telegram group. Anyone with a linked Telegram
  who posts in — or is added to — a bound group becomes a member of that
  workspace, at the member role
  (`src/services/target/membership_sync.py`)
- [ ] Or invite them from the Members card under Settings › General. Where
  the API has `RESEND_API_KEY` and `EMAIL_FROM`, the invitation is emailed to
  them. Either way the card shows its join link once, and says whether it was
  emailed; send the link yourself when it was not. They accept by signing in
  with Google as the invited address. A pending invitation can be resent or
  revoked from its row
- [ ] Removing a member is the Members card too; it also revokes the pending
  invitations that member sent or was sent. Changing a member's role is
  the `change_role` command — registered, not yet built

Leaving the Telegram group removes nobody.

**Deleting a user** (an erasure request, say) is hand-run SQL as the database
owner; there is no product door for it. In order:

- [ ] **An owner first.** An owner cannot be deleted while their workspace
  exists (each workspace has exactly one): the commit fails
  (`ct_members_owner_exists`), and ownership cannot be handed over yet
  (`transfer_ownership` is not built). Delete that workspace first (Settings ›
  General) and let its grace window end, or keep the person.
- [ ] **What nothing ties to them.** Two things can outlive the person
  without a record naming them, so list both before the removals:
  - Workspace service tokens with no recorded minter, so neither a
    removal nor the delete below can tell whether this person minted one,
    even in a workspace they have since left. The first query lists every
    live one, with `theirs` true in a workspace the person ever belonged to
    (the audit trail's membership rows outlive the membership).
  - A private-chat binding is found through the person's linked Telegram
    account. If they unlinked it, the second query lists every active
    private-chat binding no linked account holds; ask the person which
    Telegram account was theirs.

  ```sql
  SELECT t.id, t.workspace_id, t.name, t.created_at,
         t.workspace_id IN (SELECT a.workspace_id FROM audit_events a
                             WHERE a.entity_kind = 'member'
                               AND a.entity_id = '…') AS theirs
    FROM service_tokens t
   WHERE t.workspace_id IS NOT NULL AND t.created_by_user_id IS NULL
     AND t.revoked_at IS NULL;
  SELECT b.id, b.workspace_id, b.external_ref, b.created_at
    FROM channel_bindings b
   WHERE b.channel = 'telegram_dm' AND b.state = 'active'
     AND NOT EXISTS (SELECT 1 FROM user_identities x
                      WHERE x.provider = 'telegram' AND x.external_id = b.external_ref);
  ```

  For a token where `theirs` is true, ask that workspace's owner whether they
  minted it themselves; revoke the ones they didn't inside the delete's (or
  the erase's) transaction below:
  `UPDATE service_tokens SET revoked_at = now() WHERE id = '<token id>';`.
  Leave the other workspaces' tokens to their owners. End a private chat that
  turns out to be theirs in the same transaction, its queue first:
  `UPDATE channel_outbox SET state = 'superseded' WHERE binding_id =
  '<binding id>' AND state IN ('pending', 'ambiguous');` then
  `UPDATE channel_bindings SET state = 'revoked' WHERE id = '<binding id>';`.

- [ ] **Remove them from every other workspace** they belong to (Settings ›
  Members → Remove). A removal revokes the workspace service tokens they
  minted and the pending invitations they sent or were sent, and records the
  removal. A plain delete does neither: `ON DELETE SET NULL` leaves those tokens
  working with no minter recorded, the invitations they sent pending with no
  inviter, and an invitation to them able to bring them back.
- [ ] **Their Google Drive**, when they connected a workspace's
  (`oauth_credentials.granted_by_user_id` names them): disconnect it
  (Settings › Integrations → Google Drive → Disconnect). Otherwise `ON DELETE
  SET NULL` clears the granter and leaves the grant active, still holding the
  deleted person's Google token, and a grant with no granter is browsable by
  the workspace's owner — the whole of that person's Drive, Shared with me
  included.
- [ ] **The delete**, always with an actor set and in one transaction, revoking
  anything the removals missed on the way, dropping the owner's admission
  of their email (`signup_admissions`), which would otherwise let a new
  account with that address straight back in unless the admission is already
  marked used (an account created at an admitted address marks it, `trg_signup_admission_consume`), and
  ending every workspace's
  Telegram binding to their private chat, which no card reaches once they are
  gone but which would otherwise stay active; its queued cards are superseded
  first, as Remove does. The bot does not remove anyone from a group: an admin removes
  them in Telegram. The delete's cascades and `SET NULL`s fire the
  governance trigger (on `workspace_members`, on `oauth_credentials` when they
  granted Drive, and on `workspaces` when they paused one), which refuses a
  write with no `app.actor_kind`; without it the delete fails and nothing is
  removed.

  ```sql
  BEGIN;
  SET LOCAL app.actor_kind = 'operator';
  UPDATE service_tokens SET revoked_at = now()
   WHERE created_by_user_id = '…' AND revoked_at IS NULL;
  UPDATE workspace_invitations SET state = 'revoked'
   WHERE state = 'pending'
     AND (invited_by_user_id = '…'
          OR lower(email) = (SELECT lower(primary_email) FROM users WHERE id = '…')
          OR invited_tg_user_id::text IN (SELECT external_id FROM user_identities
                                           WHERE user_id = '…' AND provider = 'telegram'));
  DELETE FROM signup_admissions
   WHERE email = (SELECT lower(primary_email) FROM users WHERE id = '…');
  UPDATE channel_outbox SET state = 'superseded'
   WHERE state IN ('pending', 'ambiguous')
     AND binding_id IN (SELECT id FROM channel_bindings
                         WHERE channel = 'telegram_dm' AND state = 'active'
                           AND external_ref IN (SELECT external_id FROM user_identities
                                                 WHERE user_id = '…' AND provider = 'telegram'));
  UPDATE channel_bindings SET state = 'revoked'
   WHERE channel = 'telegram_dm' AND state = 'active'
     AND external_ref IN (SELECT external_id FROM user_identities
                           WHERE user_id = '…' AND provider = 'telegram');
  DELETE FROM users WHERE id = '…';
  COMMIT;
  ```

- [ ] **If the delete is refused because a story "is terminal … and
  immutable"**, the person approved or scheduled a story that has finished,
  and finished stories are frozen with the reference to them, so the row
  cannot be deleted. Erase what identifies them instead, in one transaction:
  their sign-ins, tokens and unfinished sign-in or link attempts end, their
  identities (the Google and Telegram accounts and their display names), the
  admission of their email and the bindings to their private chat go, and
  the `users` row stays as a bare id with no email that cannot sign in. Audit
  rows keep that id, as they do after a delete. The invitations, admission
  and binding steps run before the email and identities go, because they
  find what is addressed to them by both; a revoked invitation keeps the
  address it was sent to.

  ```sql
  BEGIN;
  SET LOCAL app.actor_kind = 'operator';
  UPDATE session_tokens SET revoked_at = now()
   WHERE user_id = '…' AND revoked_at IS NULL;
  UPDATE service_tokens SET revoked_at = now()
   WHERE (user_id = '…' OR created_by_user_id = '…') AND revoked_at IS NULL;
  UPDATE workspace_invitations SET state = 'revoked'
   WHERE state = 'pending'
     AND (invited_by_user_id = '…'
          OR lower(email) = (SELECT lower(primary_email) FROM users WHERE id = '…')
          OR invited_tg_user_id::text IN (SELECT external_id FROM user_identities
                                           WHERE user_id = '…' AND provider = 'telegram'));
  DELETE FROM signup_admissions
   WHERE email = (SELECT lower(primary_email) FROM users WHERE id = '…');
  UPDATE channel_outbox SET state = 'superseded'
   WHERE state IN ('pending', 'ambiguous')
     AND binding_id IN (SELECT id FROM channel_bindings
                         WHERE channel = 'telegram_dm' AND state = 'active'
                           AND external_ref IN (SELECT external_id FROM user_identities
                                                 WHERE user_id = '…' AND provider = 'telegram'));
  UPDATE channel_bindings SET state = 'revoked'
   WHERE channel = 'telegram_dm' AND state = 'active'
     AND external_ref IN (SELECT external_id FROM user_identities
                           WHERE user_id = '…' AND provider = 'telegram');
  DELETE FROM oauth_states WHERE user_id = '…';
  DELETE FROM user_identities WHERE user_id = '…';
  UPDATE users SET primary_email = NULL, state = 'disabled' WHERE id = '…';
  COMMIT;
  ```

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

---

## 8. Monitoring Setup (10 minutes)

### Railway Dashboard

Railway provides built-in log streaming and service monitoring. `railway.toml`
sets the restart policy (`ON_FAILURE`, 10 retries): a process that exits
non-zero — the worker does, when a supervised task dies — is restarted.

### From a laptop

- `storydump health` — the API's `/health`, `/health/scheduling` and
  `/health/posting`, and the bot's webhook from the operating details (for a
  token whose person is in `OPS_USER_IDS`; otherwise `not_checked`), each
  judged; exit 4 when not well
- `storydump deploys --watch --commit <sha>` — follows a deploy of both services

### External Monitoring (Optional)

- [ ] Point an uptime checker (e.g. UptimeRobot) at the API's `/health`
- [ ] Get email/SMS alerts if the service goes down

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
# `FATAL: TARGET_DATABASE_URL names a pooled database endpoint`, `names no host`
#   or `is not a connection URL the worker can read` — give it the endpoint's
#   direct host, written in the URL (exit 2)
# `FATAL: the credential key ring cannot load` — set the key the API service
#   holds, never a newly generated one (exit 2)
# a failed pre-deploy step — a migration failed; DATABASE_URL is missing, not the
#   owner, names a pooled host or cannot be read; or another run held the
#   runner lock past five minutes. The runner's last lines on stderr name the cause
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
- `storydump health`

---

Start with **Section 1: Telegram Bot Setup** and work through the checklist.
