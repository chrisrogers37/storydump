# Landing Site — Vercel Deployment

The Next.js landing/dashboard app lives in `landing/` and deploys to Vercel.

## Required Environment Variables

Set these in **Vercel → Project Settings → Environment Variables**:

| Variable | Type | Description |
|----------|------|-------------|
| `DATABASE_URL` | Server | Neon connection string for the ONE table the landing app owns: the marketing waitlist (`landing/src/lib/schema.ts`, Drizzle; `landing/src/lib/db.ts`). No Python migration manages it |
| `TARGET_API_URL` (or `BACKEND_URL`) | Server | The API's base URL, called by the server-side client (`landing/src/lib/target-api.ts:31`: `TARGET_API_URL` wins, then `BACKEND_URL`, then `http://localhost:8000`) |
| `TELEGRAM_BOT_TOKEN` | Server | The bot that posts WAITLIST-SIGNUP notifications (`landing/src/lib/telegram.ts`). This is the landing app's own variable: the API and the worker read the product bot's token under another name, `TARGET_TELEGRAM_BOT_TOKEN` |
| `ADMIN_TELEGRAM_CHAT_ID` | Server | The chat that receives those notifications |
| `NEXT_PUBLIC_TELEGRAM_BOT_NAME` | Client | The product bot's handle without `@`, for the site's `t.me` links (`landing/src/lib/telegram-bot.ts`); unset, the links are omitted rather than guessed |
| `NEXT_PUBLIC_PLAUSIBLE_DOMAIN` | Client | Plausible analytics domain; omit to disable (`landing/src/app/layout.tsx:7`) |

Sign-in is Google, through the API; the Telegram Login Widget is gone rather than hidden
(`landing/src/app/login/page.tsx`), and with it every variable that signed a session here:
nothing under `landing/src` reads `JWT_SECRET` or `NEXT_PUBLIC_SITE_URL` any more (measured
2026-09-18; both still appear in `landing/.env.local.example`).

### Client vs Server Variables

- **`NEXT_PUBLIC_*`** variables are inlined at build time and visible in the browser bundle. They must be set before the build runs.
- **Server** variables are only available in API routes, middleware, and server components. They can be changed without rebuilding.

### Common Issues

- **Dashboard API calls fail**: `TARGET_API_URL` / `BACKEND_URL` is missing or wrong, or the Railway API service is down.
- **A waitlist signup saves but no Telegram notification arrives**: `TELEGRAM_BOT_TOKEN` or `ADMIN_TELEGRAM_CHAT_ID` is missing — the notifier logs "Telegram notification skipped" and returns (`landing/src/lib/telegram.ts:5-10`) — or the bot is not a member of that chat.
- **The site's Telegram links are missing**: `NEXT_PUBLIC_TELEGRAM_BOT_NAME` is unset (a client variable: set it, then rebuild).

## Vercel Project Settings

- **Root Directory**: `landing`
- **Framework Preset**: Next.js
- **Build Command**: `next build` (default)
- **Node.js Version**: 20.x
