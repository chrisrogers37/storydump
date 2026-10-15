# Cloud Deployment Guide

How the hosted deployment is put together: two Railway services, one Neon
database, the variables each process reads, and the providers around them.
Storydump is one deployment that serves many workspaces
([`deployment-options.md`](deployment-options.md)); this is the operator's
reference for that deployment, and for standing up a second environment
(staging, a preview) shaped like it. The step-by-step checklist is
[`deployment.md`](deployment.md).

**Prerequisites:** GitHub, Railway and Neon accounts; a Meta developer app, a
Google Cloud project and a Cloudinary account for the providers.

---

## Architecture Overview

```
GitHub repo (main)
    |
    ├──► Railway service `worker`       python -m src.main  ->  src.worker
    |    - the clock: one elected leader, a tick every 15 s, mints each
    |      account's due slot and the recurring jobs
    |    - two job lanes (interactive, bulk): plan a slot, send cards, publish
    |      to Instagram, sync Drive folders, refresh credentials, reap
    |    - sends Telegram messages; it does not poll
    |    - /health on $PORT
    |
    ├──► Railway service `storydump`    uvicorn src.api.app:app   (the API)
    |    - /api/v1             the web's reads, and the command port
    |    - /auth               Google sign-in; the Drive and Instagram callbacks
    |    - /webhooks/telegram  the bot's one inbound door
    |    - /webhooks/meta      Meta's deauthorize and data-deletion callbacks
    |    - /health, /health/scheduling, /health/posting
    |
    ├──► Vercel (landing/)              the web front end
    |                                   (landing-vercel-deployment.md)
    └──► Neon (PostgreSQL)              the ledger; SSL required

Providers:
  - Telegram Bot API   (the webhook in; cards and notices out)
  - Instagram Login / Graph API (per-workspace OAuth tokens)
  - Google            (sign-in, and Drive read-only per workspace)
  - Cloudinary        (a story's frame in transit to Meta)
```

`python -m src.main` is the `Procfile`'s worker line and only dispatches to
`src.worker` (`src/main.py`). The job kinds are the registry in
`src/services/target/work_loop.py` (`build_registry`); the clock is
`src/services/target/scheduler.py`.

---

## 1. Database Setup (Neon)

### Create a Neon Project

1. Sign up at [console.neon.tech](https://console.neon.tech)
2. Create a new project (name: `storydump`)
3. Note your connection details from the dashboard

### Two logins

| Variable | Login | Used by |
|---|---|---|
| `DATABASE_URL` | the database **owner** — it applies DDL | the migration runner only: `railway.toml`'s `preDeployCommand`, or you at a terminal |
| `TARGET_DATABASE_URL` | the **runtime** login | the API and the worker, at run time |

The API connects as `svc_ingress` and the worker as `svc_worker`, each with a
separate runtime connection string. Row-level security binds tenant work to
`app.tenant_id`; SECURITY DEFINER functions expose bounded system operations.
The owner login applies schema changes. The API's operating details report
`db_role`, and `src.worker.run` logs the worker's database role at boot.

### Build the schema on a fresh database

As the database owner, build the schema from the models and the SQL files in
`src/models/target/ddl/`, then record the base and apply later migrations:

```bash
export DATABASE_URL="postgresql://owner:PASSWORD@db.example.invalid/storydump?sslmode=require"
python -m src.models.target.ddl init
python -m scripts.migration_runner adopt
python -m scripts.migration_runner apply
```

The owner needs `CREATEROLE` and must own the empty database. The builder runs
in one transaction and refuses an existing schema it does not recognize.
Locally, `make init-db` runs these same steps using the `DB_*` connection
components; `make create-db init-db` creates the database first.

Automatic apply reports pending `001` and `002` as owed. These cutover
steps require explicit owner application, separate constraint validation,
and confirmation of the snapshot export or backup. Follow the
[cutover runbook](../operations/schema-cutover.md), including its rehearsal
and zero-difference parity requirement.

`scripts/migrations/000_initial.sql` is an empty marker, never executable SQL.
`adopt` records it after checking the schema-code probe. Later files apply on
top of that base. `make reset-db` drops and rebuilds a local database.

### Verify the schema

```bash
# The ledger against this checkout — read-only
python -m scripts.migration_runner status

# The same through the API, with the connected role and the tables under RLS
storydump posture
```

The ledger is `runner.schema_migrations`; `storydump doctor` compares it with
the checkout.

### Connection Pool Sizing

The pool is pinned in code — 10 connections per process, no overflow
(`POOL_SIZE_SEAM`, `MAX_OVERFLOW_SEAM`, `src/services/target/unit_of_work.py`)
— and no variable sizes it. Count the processes (the worker, and one API process
per `WEB_CONCURRENCY`) against the plan's connection limit.

---

## 2. Railway Deployment

### Required dashboard start commands

Deploy two services from the same repository. For each service, open
**Settings > Deploy > Start Command** in the Railway dashboard and set its
command from this table. These per-service overrides are required before a
deploy. The `Procfile` lists available processes; this setting selects which
process the service runs. See [Railway's start-command configuration](https://docs.railway.com/deployments/start-command).

| Service | Start command | What it is |
|---|---|---|
| `worker` | `python -m src.main` | the target worker (`src.worker`) |
| `storydump` | `uvicorn src.api.app:app --host 0.0.0.0 --port ${PORT:-8000}` | the API |

The names matter: `storydump deploys` reads exactly these two
(`storydump_cli.railway.SERVICES`), and the runbooks address them with
`--service worker` and `--service storydump`. The `Procfile` lists both start
commands.

`railway.toml` is shared by both services and carries the rest:

| Key | Value | Effect |
|---|---|---|
| `buildCommand` | `pip install -r requirements.txt && pip install -e .` | no `[cli]` extra: `keyring` does not ship to a service (`setup.py`) |
| `preDeployCommand` | `python -m scripts.migration_runner apply` | applies ordinary pending migrations and reports manual cutover steps as owed; the runner's advisory lock serializes the two services, and a failing migration aborts the deploy with the old version still serving |
| `healthcheckPath` | `/health` | both services answer it (below) |
| `restartPolicyType` | `ON_FAILURE`, 10 retries | |
| `drainingSeconds` | `60` | the old deployment gets 60 s after SIGTERM |

### Setup Steps

1. **Connect the GitHub repo** in the Railway dashboard
2. **Create the two services** from the same repo, named `worker` and
   `storydump`, and set each required dashboard Start Command from the table above
3. **Generate a domain** for the `storydump` service — OAuth callbacks and
   Telegram's webhook need it. Production's is `https://api.storydump.app`
4. **Configure the variables** (Section 3). `DATABASE_URL` must be on both
   services before the first deploy: the pre-deploy step reads it

### Health Checks

- **Worker**: a small listener answers `/health` on `$PORT`
  (`worker_health.serve_health`), bound before the first database
  connection so a slow start is not marked failed. The worker is fail-fast — a
  supervised task that dies takes the process down with exit 1
  (`src.worker.supervise`) and Railway restarts it; an event loop that stops
  turning does the same with exit 3, from a watchdog thread
  (`src/services/target/loop_watchdog.py`); `/health` answers 503 only
  for a clock that is alive and no longer advancing. It stops on SIGTERM and
  SIGINT (`src.worker._run`).
- **API**: `GET /health` (`src/api/routes/health.py`) says ok, the version and
  the commit, and nothing else; whether a target engine is configured, the
  connected role, the pool and the webhook this process registered at startup
  are `GET /api/v1/ops/health`, for `OPS_USER_IDS` alone. Neither opens a
  connection, by design.
  `GET /health/scheduling` and `GET /health/posting` are the two surfaces for
  an external poller: whether the clock is advancing, and whether posts are
  going out.

---

## 3. Environment Variables

`.env.example` is the reference: it names every variable something reads, and
`tests/src/test_legacy_settings_gone.py` fails if it names one nothing does, or
misses one. No variable is required to *load* settings; a process needs what it
reads. Variables are per service on Railway.

### Both services

| Variable | Description | Example |
|---|---|---|
| `DATABASE_URL` | The database-OWNER connection string the pre-deploy migration runner applies with | `postgresql://owner:pass@ep-xxx.neon.tech/storydump?sslmode=require` |
| `TARGET_DATABASE_URL` | The runtime login. The worker refuses to boot without it (exit 2, naming it); the API answers 503 on every data route | `postgresql://app:pass@ep-xxx.neon.tech/storydump?sslmode=require` |
| `TARGET_TELEGRAM_BOT_TOKEN` | The one bot. The worker sends with it; the API registers the webhook and answers taps with it. Without it the worker runs with its Telegram channel parked | `123456:ABC-DEF1234ghIkl` |
| `TARGET_TELEGRAM_BOT_USERNAME` | That bot's @username, without the @. A token whose bot is not this one parks the channel too | `storydump_app_bot` |
| `ENCRYPTION_KEY` | Fernet key for the stored OAuth credentials (`ENCRYPTION_KEYS`, newest first, for rotation) — the SAME key on both services. Neither boots without a valid one: the deploy fails its health check and the previous deploy keeps serving | Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | The one Google OAuth client: sign-in and the Drive grant on the API, the hourly Drive token refresh on the worker — the worker warns at boot without both | `xxx.apps.googleusercontent.com`, `GOCSPX-...` |
| `WEB_APP_URL` | The web front end's origin: the one origin CORS admits, where a finished sign-in or OAuth leg lands, and the origin of the links the worker sends | `https://app.example.com` |

### The API (`storydump`)

| Variable | Description | Example |
|---|---|---|
| `TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN` | The secret the API expects Telegram to echo on every delivery. Unset, the ingress refuses every delivery | a long random string |
| `OAUTH_REDIRECT_BASE_URL` | The API's public origin — the base of every OAuth redirect URI | `https://api.storydump.app` |
| `INSTAGRAM_APP_ID`, `INSTAGRAM_APP_SECRET` | The Meta app, for Instagram Login ([`instagram-login-setup.md`](instagram-login-setup.md)) | |
| `FACEBOOK_APP_SECRET` | Optional. The second secret Meta's signed policy callbacks are verified against (`app_secrets` in `src/services/target/meta_callbacks.py`); set it only if those callbacks are registered under another Meta app | |
| `SESSION_COOKIE_DOMAIN` | The registrable domain the API and the front end share, so the front end's server side can read the session cookie | `example.com` |
| `SESSION_COOKIE_SECURE` | Optional, default `true`: the session cookie is HTTPS-only. Only a plain-http laptop setup turns it off | `true` |
| `SESSION_MAX_AGE_SECONDS` | Optional, default `2592000` (30 days): a web session's absolute lifetime from sign-in; use slides it, but never past this | `2592000` |
| `RESEND_API_KEY`, `EMAIL_FROM` | Optional; the invitation email (Resend). With both set, an invitation is emailed after it commits and its response says `sent`; without both it says `not_configured`, and the inviter sends the link. Before setting them, verify the sending domain in Resend (the SPF and DKIM records it issues, plus a DMARC record at `p=none` to start) and use a key limited to sending. Unset either to turn it off | `re_…`, `Storydump <invites@storydump.app>` |
| `TARGET_SIGNUP_OPEN` | Optional, default `false`: a new Google account signs up only when its email is admitted (`signup_admissions`) or invited (`fn_signup_admitted`). `true` lets any account sign up — a local stack's setting, never production's | `false` |
| `OPS_USER_IDS` | Set after the first sign-in (Quick Start step 11); the user ids (comma-separated, the `user` line of `storydump whoami`) that may read the API's operating details at `GET /api/v1/ops/health` — usage counts, the database login, the pool, the webhook, the queue — and `GET /api/v1/ops/posture`, and so run `storydump health` and `storydump posture`. Unset, nobody can, and `storydump health` cannot exit 0: the webhook reads `not_checked`. Public `/health` says only ok, the version and the commit. Each person it names who has linked Telegram, while their account is active, also gets a direct message from the bot for every waitlist signup the API accepts (a repeat address too): "New waitlist signup!", the address and the time. It is sent after the answer, one at a time (at most 20 waiting; past that a ping is dropped and logged), and retried on a 429; best effort, so one still waiting when the API's process stops is lost. A refusal (`bot was blocked by the user`, say) is logged on a line starting `waitlist ping:`, and so is a signup when none of those people has linked Telegram. Since the message carries the address, adding someone here also shares the waitlist with them. The message also carries ✅ Admit, so a person named here can admit an address from it, and that emails the address a link to sign up. The same people can list the waitlist and admit any address, on it or not, from the CLI: `storydump waitlist` and `storydump admit <email>` (`GET /api/v1/ops/waitlist`, `POST /api/v1/admissions`), and on the site's owner page, `/ops/waitlist`, which the dashboard header links as Owner for them alone, and the ping as Open waitlist ↗ when `WEB_APP_URL` is https. To anyone else, `/ops` is the site's 404 | a user id (a UUID) |
| `WAITLIST_SITE_SECRET` | Optional; the same value as the landing site's `WAITLIST_SITE_SECRET` on Vercel. Set, `POST /public/waitlist` refuses a call without it and limits each visitor the site names on their own; unset, every visitor shares one limit. Set it on the site first, then here | `openssl rand -hex 32` |
| `TRUSTED_PROXY_HOSTS` | Optional; the proxies whose `X-Forwarded-For` the API believes (private ranges and `100.64.0.0/10`, the range Railway's edge connects from, by default). **Never `*`** — it lets a caller choose its own IP and defeats every IP-keyed control. Leave it unset on Railway: a value without `100.64.0.0/10` makes every visitor one address to the rate limits. Never set uvicorn's `FORWARDED_ALLOW_IPS` either: its own proxy layer runs first and, at `*`, takes the caller's leftmost entry | `10.0.0.0/8,…` |
| `EDGE_HOP_HOSTS` | Optional, default `152.233.47.0/24`: the hop Railway appends after the visitor in `X-Forwarded-For`. When the header ends in one, the API removes that one entry and reads the visitor before it; never more than one, never the only entry, and never a trusted peer. A hop outside the list is kept, so its visitors share that hop's limits. **Never `*`** | `152.233.47.0/24` |
| `TARGET_TELEGRAM_WEBHOOK_URL` | Optional; the URL the API registers with Telegram. **The default is production's** `https://api.storydump.app/webhooks/telegram` (`src/services/target/vocabulary.py`), so a staging or preview API that registers a webhook — by hand with `storydump webhook register`, or by autoregistering — must set its own, and needs its own bot: one bot holds one webhook | `https://staging.example.com/webhooks/telegram` |
| `TARGET_TELEGRAM_WEBHOOK_AUTOREGISTER` | Optional; `0` stops the API registering the webhook at startup even where `RAILWAY_ENVIRONMENT_NAME` is `production` | `0` |
| `TARGET_TELEGRAM_WEBHOOK_MAX_CONNECTIONS` | Optional, default 10: `setWebhook`'s `max_connections`, the ingress's connection budget (Telegram's own default of 40 is deliberately not used) | `10` |
| `TARGET_TAP_ADMISSION_PER_MINUTE` | Optional, default 120: commands one workspace may execute from Telegram taps per minute | `120` |

### The worker

| Variable | Description | Example |
|---|---|---|
| `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET` | The transit store. Without all three the worker parks the publish kind by name (`src.worker._transit_from_env`) | `dxyz123`, … |
| `TARGET_USAGE_PRECHECK_ENABLED` | Optional, default off: the advisory read of Meta's publishing quota before a publish | `true` |
| `TARGET_ACTIVATION_NUDGE_ENABLED` | Optional, default off: the activation nudge, one email, once, to a person who stopped part-way through setup. It also needs `RESEND_API_KEY` and `EMAIL_FROM` and the web origin | `true` |
| `TARGET_ACTIVATION_NUDGE_LIMIT` | Optional, default 3: the nudge's emails per daily run. Each person is marked nudged as their email is queued, so a provider mistake uses up that run. Arm the nudge at the default once email is on and a send has been confirmed, then raise it | `20` |
| `TARGET_WORKER_INTERACTIVE_CONCURRENCY`, `TARGET_WORKER_BULK_CONCURRENCY` | Optional, defaults 3 and 2: claim-and-run tasks per lane; the worker refuses a sum the pool cannot fit (`src/worker.py`) | `3`, `2` |
| `META_GRAPH_VERSION` | Optional; overrides the Graph API version the publish adapter and the Instagram archive adapter call (`instagram_graph.graph_version`) | `v21.0` |
| `RESEND_API_KEY`, `EMAIL_FROM` | The worker's `send_email` jobs, which serve the activation nudge and admissions from the waitlist. An admission's email carries a claim link the worker draws as it sends. Set them on the worker, the same values the API has, once the claim link works at sign-in; the nudge keeps its own switch, `TARGET_ACTIVATION_NUDGE_ENABLED`. Invitations need them on the API | |
| `WORKER_LOG_LEVEL` | Optional; the API's is `LOG_LEVEL` | `INFO` |

`TARGET_TELEGRAM_API_BASE` (a test double for `api.telegram.org`) is the load
harness's seam and is refused where `RAILWAY_ENVIRONMENT_NAME` is `production`
(`src/channels/telegram_transport.py`); it never goes on a service.

Railway sets `PORT` and `RAILWAY_ENVIRONMENT_NAME` itself. The second is
load-bearing: unless `TARGET_TELEGRAM_WEBHOOK_AUTOREGISTER` says otherwise, the
API registers the Telegram webhook at startup only where it reads `production`
(`_register_webhook`, `src/api/app.py`), so a laptop or a preview holding
the token does not re-point production's webhook.

There is no `DB_*` alternative for a deployed service. The `DB_*` components
serve the test harness and `make` only.

Two variables must agree on the API's public origin: `OAUTH_REDIRECT_BASE_URL`
(the base of every OAuth redirect URI registered with Google and Meta) and the
webhook URL the API registers with Telegram (`TARGET_TELEGRAM_WEBHOOK_URL`, or
its production default). An environment that is not production sets both, or
it registers production's URLs.

### Not variables

The media source (a connected Google Drive folder), the schedule, **Pause
Posting**, **Dry Run Mode** and **Instagram API** publishing are per-workspace
settings in the ledger, set on the web (Settings › Integrations and
Settings › General).

### Reading a service's variables

List the **names**, not the values — the values are production's credentials,
and a bare `railway variables` prints them:

```bash
railway variables --service <svc> --environment production --json \
  | python -c "import sys,json; print(sorted(json.load(sys.stdin)))"
```

---

## 4. Telegram Bot Setup

The deployment runs **one** bot.

### BotFather

1. Message [@BotFather](https://t.me/botfather) and `/newbot`; keep the token
2. `/setjoingroups` → the bot → *Enable* — without it a workspace cannot add
   the bot to its group
3. `/setprivacy` → the bot → *Disable* (or make the bot an admin of each
   group): under Telegram's default privacy mode the bot sees too little of a
   group for members to be recognized

No command list is registered with `/setcommands`. The bot serves `/start` with
the two deep-link payloads the web mints — `link-` (a person's Telegram
identity) and `bind-` (a group joins a workspace), the lanes `build_router`
registers (`src/services/target/telegram_dispatch.py`) — group
membership, a group's move to a supergroup (its binding follows the new chat
id), and the buttons on the cards it sends. Commands typed in a chat are
not dispatched, and an invitation is accepted on the web, not in the
chat.

### The webhook

The bot is **webhook-fed**: Telegram delivers to `POST /webhooks/telegram` on
the API, authenticated by `TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN`. In Railway's
`production` environment the API registers the webhook itself at startup, on
every deploy; `storydump webhook status` checks the bot, the registration and
the door.

### Groups

There is no channel id to configure. Each workspace binds its own group on the
web — Settings › Integrations → **Link Telegram**, then **Add a Telegram
group** — and its approval cards go there.

---

## 5. Instagram OAuth Setup

An Instagram account is connected on the web (Settings › Accounts → **Connect
Instagram**) through Instagram Login; the callback runs on the API. The full
walkthrough — the Meta app, the redirect URI, the variables, the failure
reasons — is [`instagram-login-setup.md`](instagram-login-setup.md). In short:

1. Create an app at [developers.facebook.com](https://developers.facebook.com)
2. Add the **Instagram** product and use **API setup with Instagram business login**
3. Add the OAuth redirect URI `https://<your API host>/auth/instagram-login/callback`
   (production: `https://api.storydump.app/auth/instagram-login/callback`)
4. The scopes the flow requests are `instagram_business_basic` and
   `instagram_business_content_publish`
   (`src/services/target/ig_login_oauth.py`)
5. Set `INSTAGRAM_APP_ID`, `INSTAGRAM_APP_SECRET` and `OAUTH_REDIRECT_BASE_URL`
   on the API

### App Review

Until Meta's App Review grants Advanced Access on those two scopes, only
accounts with a role on the Meta app — and the ones Meta has allowlisted — can
connect; every other tenant's account is refused at Meta's eligibility gate.

---

## 6. Google OAuth Setup

One Google OAuth client serves two legs (`src/api/google_client.py`): **sign-in**
(scope `openid email profile`) and the workspace's **Drive grant** (scope
`drive.readonly`). Google Drive is the media source.

### Google Cloud Setup

1. Create a project at [console.cloud.google.com](https://console.cloud.google.com)
2. Enable the **Google Drive API**
3. Create an **OAuth 2.0 Client ID** (Web application type)
4. Add **both** authorized redirect URIs, on the API's host:
   - `https://<your API host>/auth/google/callback`
   - `https://<your API host>/auth/google-drive/callback`
5. Set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` on the **API and the
   worker**, and `OAUTH_REDIRECT_BASE_URL` on the API. The worker refreshes the
   hourly Drive access token from the stored refresh token and warns at
   boot without the pair (`src/worker.py`)

### How It Works

1. A person signs in on the web with Google (`GET /auth/google`); the API sets
   the session cookie
2. An admin opens Settings › Integrations and connects Google Drive — one grant
   per workspace (`POST /api/v1/workspaces/{ws}/drive/connect`,
   `src/api/routes/v1.py`)
3. The callback stores the grant, encrypted, in `oauth_credentials`
4. The admin picks folders under the grant (**Add folder**); each is a row in
   `media_sources`
5. The worker's `sync_media_source` job walks each folder to any depth and
   indexes every `image/*` and `video/*` file into `media_items`; a file's
   category is the name of the top-level folder it sits under
   (`src/services/target/google_drive_adapter.py`)

### User Experience

End users need a Google account with a Drive folder of media. They do not touch
GCP; the project is a one-time task for the operator.

---

## 7. Cloudinary Setup

Required for publishing: without all three variables the worker parks the publish kind by name.

1. Create account at [cloudinary.com](https://cloudinary.com)
2. Get credentials from Dashboard: Cloud Name, API Key, API Secret
3. Set `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET` on the worker

The worker's transit store (`src/services/target/transit.py`) uploads a story's frame for Meta to fetch, and the `reap_transit_assets` job removes it afterwards.

---

## 8. Monitoring & Operations

### Logs

Both processes log to the console, which Railway captures: the API through
`src/utils/logger.py` at `LOG_LEVEL`, the worker through `logging.basicConfig`
at `WORKER_LOG_LEVEL` (`src/worker.py`).

```bash
railway logs --service worker
railway logs --service storydump
```

### Database Backups

- **Neon**: point-in-time restore, within the project's history retention.
- **Manual**: `pg_dump "$DATABASE_URL" > backup_$(date +%Y%m%d).sql`

### Health Monitoring

From a laptop with a token minted under Settings › API tokens:
- `storydump health` — the API's three health surfaces and the bot's webhook, judged (exit 4 when not well)
- `storydump deploys` — the latest deployment of each service; `storydump doctor` for the local setup

### Service Management

- **Restart**: the Railway dashboard, or `railway redeploy --service <svc>
  --yes`. Both re-run the service's *latest* deployment, which after a
  `railway down` can be an old build — then push to `main` instead and confirm
  the commit with `storydump deploys`. A restart does not stop posting;
  `storydump pause --workspace <ws>` does
- **Logs**: `railway logs --service <svc>` or the dashboard
- **Variables**: names only, as in Section 3. `railway shell` and `railway run`
  export every production credential into a local process — use them only for
  the escape hatch described under *Reading production* in
  [`dev-environment-setup.md`](dev-environment-setup.md#reading-production)
- **The `storydump` CLI is a client of the API**, run from a laptop under your
  own token; nothing it does needs a shell on a service

### Cost

Neon, Railway and Cloudinary each have a free or starter tier and bill by
usage above it — see their pricing pages, which this guide does not track. The
Telegram Bot API, the Instagram Graph API and the Google Drive API are free and
rate- or quota-limited.

---

## 9. Security Checklist

- [ ] All secrets stored as Railway environment variables (never in code)
- [ ] `ENCRYPTION_KEY` generated and set on both services
- [ ] `TARGET_DATABASE_URL` is a runtime login, not the owner (`storydump health --json` → `data.api.db_role`)
- [ ] `TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN` is long and random; the bot token is kept secret
- [ ] `TARGET_TELEGRAM_WEBHOOK_AUTOREGISTER` is not `1` anywhere that holds the production token outside production
- [ ] The Meta app secret, the Google client secret and the Cloudinary API secret are kept secret
- [ ] `.env` file is NOT committed (verified in `.gitignore`)
- [ ] SSL/TLS for all connections (Neon requires it, all APIs use HTTPS)
- [ ] `OAUTH_REDIRECT_BASE_URL` is the API's HTTPS origin, and `WEB_APP_URL` the front end's
- [ ] Variables are listed by name only (Section 3)

---

## 10. Troubleshooting

| Problem | Solution |
|---------|----------|
| The worker exits at boot with `FATAL: TARGET_DATABASE_URL is unset` | Set it on the `worker` service (`src/worker.py`). |
| Every API data route answers 503 | `TARGET_DATABASE_URL` is unset on the `storydump` service; the operating details show `target_database: false`. |
| A deploy fails in the pre-deploy step | A migration failed, or `DATABASE_URL` is missing or is not the owner. The old version keeps serving; fix forward with a new file. |
| Database connection fails | Check `TARGET_DATABASE_URL` (the services) and `DATABASE_URL` (the migration runner); a Neon URL carries `?sslmode=require`. The `DB_*` components steer only the test harness and `make`. |
| Neon connection limit exceeded | The pool is pinned in code (10 per process, no overflow); no variable sizes it. Count the processes against the plan's connection limit. |
| No cards arrive in the group | `storydump health` reports the webhook and scheduling; `storydump outbox --since 3h` shows what is owed or lost on the chats; verify `TARGET_TELEGRAM_BOT_TOKEN` is the bot named by `TARGET_TELEGRAM_BOT_USERNAME`. Check the worker's log for a parked channel. |
| Taps on a card do nothing | `storydump webhook status`: the registration must include `callback_query`, and the door must accept the secret. |
| A deploy fails its health check, and its log shows `FATAL: the credential key ring cannot load` (worker) or `Refusing to start` (API) — e.g. `ENCRYPTION_KEY not configured` | `ENCRYPTION_KEY` is unset on that service or not a Fernet key, or `ENCRYPTION_KEYS` (which overrides it) holds a bad entry; the message names which. Set the key the other service holds — a NEW key cannot read the stored credentials. Only on a first install, generate one: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| An OAuth leg answers `503 … oauth not configured: set …` | Set the variables the message names on the API (`src/api/oauth_client.py`). |
| OAuth callback fails with Meta's or Google's "redirect_uri" error | `OAUTH_REDIRECT_BASE_URL` must be the API's public origin, and the exact callback URI must be registered with the provider. |
| An old Telegram button opens the web's sign-in page | That is the retired Mini App's URL answering as designed (`src/api/routes/retired.py`). |
| Service restarts frequently | Check memory limits in Railway. Review logs for OOM or crash loops; the worker exits 1 when a supervised task dies. |
| Instagram API rate limited | Meta caps API publishing per account over a rolling 24 h window; the limit is Meta's and no variable overrides it. The worker's advisory pre-check (`TARGET_USAGE_PRECHECK_ENABLED`, default off) reads the account's live quota (`GET /{ig-user}/content_publishing_limit`), and `storydump story <id>` shows what Meta answered for a refused publish. |

---

## Quick Start Checklist

1. [ ] Create the Neon database; on a fresh one build from code, then run `runner adopt` and `runner apply`
2. [ ] Create the Railway project with two services (`worker` + `storydump`, the API)
3. [ ] Generate the API's domain; set `OAUTH_REDIRECT_BASE_URL` and `WEB_APP_URL`
4. [ ] Set the variables of Section 3, `DATABASE_URL` on both services first
5. [ ] Create the Telegram bot via BotFather; enable groups, disable privacy mode
6. [ ] Configure the Meta app (the Instagram Login redirect URI)
7. [ ] Configure the Google client (both redirect URIs)
8. [ ] Deploy; `storydump webhook status` is well, and `storydump health` reports `api`, `scheduling` and `posting` well (the webhook reads `not_checked` until step 11)
9. [ ] Admit your own email: sign-up is invite-only (`fn_signup_admitted`) and the first account is no exception, so as the owner (the `DATABASE_URL` login), in lower case: `psql "$DATABASE_URL" -c "INSERT INTO signup_admissions (email) VALUES ('you@example.com');"`
10. [ ] Sign in on the web with that Google account, create a workspace
11. [ ] Mint an API token (Settings › API tokens), run `storydump login`, set `OPS_USER_IDS` on the API service to the `user` line of `storydump whoami`, let it redeploy; `storydump health` now exits 0
12. [ ] Settings › Integrations: link Telegram, add a Telegram group, connect Google Drive, add a folder, Sync Now
13. [ ] Settings › Accounts: Connect Instagram
14. [ ] Settings › General: set the schedule. **Instagram API** is off on a new workspace (cards offer **Posted myself**, not **Post now**); turn it on when the workspace should publish through the API. **Dry Run Mode** with it on runs the whole publish leg without calling Meta — and spends the media's rotation as a real post would (`src/services/target/publish_pipeline.py`)
