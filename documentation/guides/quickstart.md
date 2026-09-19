# Quick Start

Storydump is a **hosted service**. We operate one deployment and provision
tenants on it — there is no instance for you to install, and this guide will
not ask you to create a bot, a database, or a Meta app.

Pick the path that matches why you are here.

---

## I want to use Storydump

Everything is set up on the web. Telegram is where approvals arrive; it is not
a command console.

1. **Start at [storydump.app](https://storydump.app)** and read *Getting
   Started*. You need an Instagram Business or Creator account, a Google Drive
   folder with your media, and a Telegram account.
2. **Sign in with Google** and create a workspace.
3. **Connect, under Settings:**
   - **Accounts → Connect Instagram** — you authorize on Instagram's own
     screen; no Facebook Page is needed.
   - **Integrations → Connect Google Drive**, then **Add folder** and **Sync
     Now**. Everything inside a connected folder is indexed; a file's category
     is the top-level folder it sits under.
   - **Integrations → Link Telegram**, then **Add a Telegram group** — the
     group your approval cards go to.
   - **General** — the posting schedule (posts per day, the hours, the time
     zone), the category weights, and the toggles: **Pause Posting**, **Dry Run
     Mode**, **Instagram API**.

Credentials are stored encrypted, per workspace.

Once connected, the day-to-day loop is the approval card. When a slot comes
due the bot posts the media in your group with buttons — **🚀 Post now** (only
with **Instagram API** on), **✅ Posted myself**, **⏭️ Skip**, **🚫 Reject**, and
**📱 Open Instagram** — and the same story waits in the web's **Queue**.

| To … | Where |
|------|-------|
| Approve, skip or reject a story | The card in your Telegram group, or **Queue** on the web |
| See what is scheduled and what posted | **Queue** and **Calendar** on the web |
| See what was synced | **Media Library** |
| Pause or resume posting | Settings › General → **Pause Posting** |
| Change frequency, hours, time zone | Settings › General → the Posting Schedule card |
| Add, reconnect or remove an Instagram account | Settings › Accounts |
| Invite or remove people | Settings › General → Members |

The bot answers `/start` links the web hands you (linking your Telegram,
adding a group). It does not serve typed commands: the `/status`, `/next`,
`/settings` and `/cleanup` of the earlier bot went with the legacy tier, which
was retired in the tear-out (#1216, September 2026).

---

## I want to work on Storydump

You need a development environment, not a deployment:

- **[../../AGENTS.md](../../AGENTS.md)** — the canonical guide: the safety
  rules, the architecture and its layer boundaries, the command port, setup,
  testing. **Read the safety rules before running anything**: the worker
  (`python -m src.main`) posts to Instagram, and several `storydump` verbs
  write.
- **[dev-environment-setup.md](dev-environment-setup.md)** — local setup:
  virtualenv, dependencies, a local database, the environment files.
- **[testing-guide.md](testing-guide.md)** — how the suite is organized, what
  needs a PostgreSQL, and how to run it.

The developer's and the agent's console is the `storydump` CLI — a client of
the API under your own token (Settings › API tokens), not a database
connection:

```bash
pip install -e '.[cli]'
storydump login                 # paste the token; agents and CI set STORYDUMP_TOKEN instead
storydump whoami
storydump floating --watch      # approved stories waiting to post
storydump story <intent_id>     # one story's whole timeline
storydump health
storydump doctor
```

[`reading-the-ledger.md`](../operations/reading-the-ledger.md) is the guide to
the verbs.

A local environment is for development and tests. It is not a second production
system, and nothing about it is a supported way to run the product.

---

## I operate Storydump

Operator runbooks are separate from both paths above:

- **[cloud-deployment.md](cloud-deployment.md)** — the hosted deployment
  (Railway services, Neon database, environment).
- **[deployment.md](deployment.md)** — the deployment checklist.
- **[deployment-options.md](deployment-options.md)** — how deploys reach
  production, and the multitenancy model.
- **[instagram-login-setup.md](instagram-login-setup.md)** — the Instagram
  Login path for the app we operate.
- **[../operations/](../operations/)** — monitoring, the ledger, the webhook,
  the migration runner, recovery.

---

## Troubleshooting

**A `/start` link did nothing** — the links are one-shot and last 15 minutes,
and the bot stays silent when it refuses one. Mint a fresh link from Settings ›
Integrations. To add a group, your own Telegram must be linked first.

**No cards arrive in the group** — check the group is listed under Settings ›
Integrations, that the workspace is not paused (Settings › General), and that
the Media Library is not empty: a slot that finds no eligible media posts
nothing, and says so in the group at most once a day.

**An Instagram account reads "Reconnect needed"** — Instagram stopped accepting
the stored token. Press **Reconnect Instagram** on that row under Settings ›
Accounts.

**Google Drive reads "Access expired — reconnect"** — reconnect it under
Settings › Integrations; the folders stay.

**Something looks wrong in the product itself** — open an issue in this
repository with what you did, what you expected, and what happened.
