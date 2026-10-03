# Landing Site — Vercel Deployment

The Next.js landing/dashboard app lives in `landing/` and deploys to Vercel.

## Required Environment Variables

Set these in **Vercel → Project Settings → Environment Variables**:

| Variable | Type | Description |
|----------|------|-------------|
| `TARGET_API_URL` (or `BACKEND_URL`) | Server | The API's base URL, called by the server-side client (`landing/src/lib/target-api.ts:31`: `TARGET_API_URL` wins, then `BACKEND_URL`, then `http://localhost:8000`) |
| `TELEGRAM_BOT_TOKEN` | Server | The bot that posts WAITLIST-SIGNUP notifications (`landing/src/lib/telegram.ts`). This is the landing app's own variable: the API and the worker read the product bot's token under another name, `TARGET_TELEGRAM_BOT_TOKEN` |
| `ADMIN_TELEGRAM_CHAT_ID` | Server | The chat that receives those notifications |
| `WAITLIST_SITE_SECRET` | Server | Optional. The same value as the API's `WAITLIST_SITE_SECRET` (generate one with `openssl rand -hex 32`). Set, the waitlist route sends it with the visitor's address, and the API gives each visitor their own limit and refuses any other caller. Set it here and redeploy first, then on the API: the API ignores it while its own is unset |
| `NEXT_PUBLIC_TELEGRAM_BOT_NAME` | Client | The product bot's handle without `@`, for the site's `t.me` links (`landing/src/lib/telegram-bot.ts`); unset, the links are omitted rather than guessed |
| `NEXT_PUBLIC_PLAUSIBLE_DOMAIN` | Client | Plausible analytics domain; omit to disable (`landing/src/app/layout.tsx:7`) |

Sign-in is Google, through the API; the Telegram Login Widget is gone rather than hidden
(`landing/src/app/login/page.tsx`), and with it every variable that signed a session here:
nothing under `landing/src` reads `JWT_SECRET` or `NEXT_PUBLIC_SITE_URL` any more, and
`landing/.env.local.example` names neither (`tests/test_landing_env_example.py` keeps the example
file and the reads in agreement both ways).

The landing app holds no database credential. The waitlist form's server route
(`landing/src/app/api/waitlist/route.ts`) hands the address to the API's `POST /public/waitlist`
over `TARGET_API_URL`, and the API writes `waitlist_entries` (migration 100). A `DATABASE_URL`
left over in the Vercel project is read by nothing and can be deleted.

### Client vs Server Variables

- **`NEXT_PUBLIC_*`** variables are inlined at build time and visible in the browser bundle. They must be set before the build runs.
- **Server** variables are only available in API routes, middleware, and server components. They can be changed without rebuilding.

### Common Issues

- **Dashboard API calls fail**: `TARGET_API_URL` / `BACKEND_URL` is missing or wrong, or the Railway API service is down. On Vercel it must be the API's public origin (`https://api.storydump.app` in production); the example file's `http://localhost:8000` is the laptop value.
- **The waitlist form answers "Something went wrong"**: the function log has a `waitlist signup failed:` line with the API's status and reason. `target_router_unreachable` means `TARGET_API_URL` is wrong or the API is down; `http_429` means a waitlist limit was reached: the visitor's own (10 a minute) with `WAITLIST_SITE_SECRET` set on both sides, otherwise the one the whole site shares (300 a minute). `http_403` means the API has `WAITLIST_SITE_SECRET` set and the site's is missing or different.
- **A waitlist signup saves but no Telegram notification arrives**: `TELEGRAM_BOT_TOKEN` or `ADMIN_TELEGRAM_CHAT_ID` is missing — the notifier logs "Telegram notification skipped" and returns (`landing/src/lib/telegram.ts:5-10`) — or the bot is not a member of that chat.
- **The site's Telegram links are missing**: `NEXT_PUBLIC_TELEGRAM_BOT_NAME` is unset (a client variable: set it, then rebuild).
  A rebuild is a dashboard **Redeploy** with **Use project's Ignore Build Step** unchecked (see **Ignored Build Step** below).

## Vercel Project Settings

- **Root Directory**: `landing`
- **Framework Preset**: Next.js
- **Build Command**: `npm run build` (= `next build`), pinned with the install command and the framework in `landing/vercel.json`
- **Node.js Version**: 22.x (`landing/package.json` `engines`, mirrored by CI's `node-version: '22'`)
- **Ignored Build Step**: `ignoreCommand` in `landing/vercel.json` (`landing/scripts/vercel-ignore-build.sh`), which overrides the dashboard setting. A push that leaves `landing/` unchanged since the branch's last successful deployment is cancelled before the install and the build (Vercel reports "Canceled by Ignored Build Step"; the GitHub `Vercel` status still reads success). A dashboard **Redeploy** runs the step too: to rebuild the same code, for example after changing a variable, uncheck **Use project's Ignore Build Step** in the Redeploy dialog.

Vercel builds every pull request as a preview deployment, which is why CI runs no `next build`
of its own; [`operations/fetching-preview-deployments.md`](../operations/fetching-preview-deployments.md)
covers reading a preview headlessly.
