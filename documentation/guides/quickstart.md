# Quick Start

Storydump is a **hosted service**. We operate one deployment and provision
tenants on it — there is no instance for you to install, and this guide will
not ask you to create a bot, a database, or a Meta app.

Pick the path that matches why you are here.

---

## I want to use Storydump

Onboarding happens through the product, not through this repository:

1. **Start at [storydump.app](https://storydump.app)** and follow *Getting
   Started*. It walks through connecting an Instagram Business or Creator
   account, Google Drive for media, and Telegram for approvals.
2. **Open the Telegram bot and send `/start`.** That is the entry point for
   everything afterwards — the setup wizard on first contact, the dashboard
   after that.
3. **Connect your accounts from the dashboard.** Instagram and Google Drive are
   connected by signing in to them; credentials are stored per tenant and
   revoked when you leave.

Once connected, the day-to-day loop is the approval workflow: the bot sends a
card when a post is due, you tap **✅ Posted**, **⏭️ Skip**, or **🚫 Reject**,
and `/settings` adjusts posting frequency, hours, and behavior.

| Command | What it does |
|---------|--------------|
| `/start` | Setup wizard on first use, dashboard after |
| `/status` | Health, media stats, queue status |
| `/setup` / `/settings` | Posting frequency, hours, toggles |
| `/next` | Send the next post now |
| `/cleanup` | Delete recent bot messages |
| `/help` | List available commands |

---

## I want to work on Storydump

You need a development environment, not a deployment:

- **[dev-environment-setup.md](dev-environment-setup.md)** — local setup:
  virtualenv, dependencies, a local database, and the environment variables the
  test suite expects.
- **[../../CLAUDE.md](../../CLAUDE.md)** — architecture, layer boundaries, and
  the commands that are safe to run against a live system. Read the safety
  rules before running anything: some CLI commands post to Instagram.
- **[testing-guide.md](testing-guide.md)** — how the suite is organized and how
  to run it.

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
- **[instagram-login-setup.md](instagram-login-setup.md)** — the current
  Instagram Login path for the app we operate.

---

## Troubleshooting

**The bot does not respond to `/start`** — check you are messaging the right
bot, and that you have completed *Getting Started* at
[storydump.app](https://storydump.app). If a group chat is involved, the bot
must be a member of it.

**"Instagram connection has expired"** — reconnect from the dashboard
(`/start` → connect). Provider tokens expire or get revoked upstream; the
dashboard's connect flow is the supported way to re-authorize.

**No posts are being scheduled** — check `/status` for media count and queue
state. An empty media source or a paused instance both present this way.

**Something looks wrong in the product itself** — open an issue in this
repository with what you did, what you expected, and what happened.
