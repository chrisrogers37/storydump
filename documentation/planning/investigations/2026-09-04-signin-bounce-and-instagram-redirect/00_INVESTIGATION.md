---
title: "Investigation: Google sign-in bounced once; Instagram connect refused with Invalid redirect_uri"
type: audit
status: completed
owner: chrisrogers37
created: 2026-09-04
tags: [investigation, auth, instagram, deploy]
---

# Investigation — 2026-09-04

**Reported.** After the day's merges (#1232, #1233) the owner drove production: (1) the first Google
sign-in authenticated at Google (Google's new-sign-in email arrived) and then landed on the login
screen; a second attempt worked. (2) *Connect Instagram* from Settings › Accounts reached Instagram's
login and the "save login info?" screen, then landed on
`instagram.com/oauth/authorize/third_party/error` with *Invalid Request: Request parameters are
invalid: Invalid redirect_uri*.

**Platform.** API on Railway (`api.storydump.app`, `railway.toml` health check on `/health`,
predeploy migrations); site on Vercel (`storydump.app`); Neon Postgres. Every merge to `main`
redeploys both.

## Findings

| # | Category | Finding | Confidence | Evidence |
|---|---|---|---|---|
| 1 | Config (Meta) | The app's OAuth redirect URI list did not contain the URI the API sends. The code builds `OAUTH_REDIRECT_BASE_URL + /auth/instagram-login/callback` (`src/api/instagram_client.py`); the API has served from `api.storydump.app` since 2026-08-31; the setup guide told the operator to register the old `storydump-production.up.railway.app` host. Meta's error names `redirect_uri` specifically, so the app id was accepted. | High | guide `documentation/guides/instagram-login-setup.md` (pre-fix), the live route answering on `api.storydump.app`, Meta's error text |
| 2 | Deploy timing + site logic | The first sign-in most likely landed while the API was redeploying (#1233 merged 19:29 UTC; the API came back 19:37 UTC). The welcome and workspaces pages did `getSession().catch(() => null)` and treated "the API could not be asked" as "not signed in", redirecting a person with a good cookie to `/login`. | Medium | `landing/src/app/welcome/page.tsx`, `landing/src/app/workspaces/page.tsx` (pre-fix); `/health` uptime at 19:37 UTC; the second attempt succeeding on the same code path |

## Fixes

| # | Fix | Owner | Status |
|---|---|---|---|
| 1 | Add `https://api.storydump.app/auth/instagram-login/callback` to the Meta app's Instagram business login → OAuth redirect URIs (Use cases → Instagram → Business login settings). Not the App settings › Advanced "Authorize callback URL", which Instagram Login does not read. | operator | done 2026-09-05 — the list gained the `api.storydump.app` URI (plus the deauthorize and data-deletion URLs); the next connect succeeded end to end |
| 2 | Correct the setup guide (real host, current Meta product path, API-only variables) and `cloud-deployment.md`. | — | done, #1236 |
| 3 | Entry pages tell "unavailable" from "signed out": `resolveEntrySession`; an unreachable API renders `RouterUnavailable` with *Try again* instead of redirecting to `/login`. | — | done, #1236 |
| 4 | Zero-downtime hand-over on the API: `RAILWAY_DEPLOYMENT_OVERLAP_SECONDS=60` on the API service (no Railway fee; ~1 extra minute of API compute per deploy). | operator | proposed |

## Prevention

- `/health` should report OAuth presence booleans so a lost variable is visible at deploy time (#1229).
- The Meta redirect URI is deployment configuration that lives outside the repo; the guide now names the exact value and where it goes. A periodic check that `GET /auth/instagram-login/callback` answers on the configured base URL is cheap and could join the drift monitor.
- Every merge redeploys the API; an unattended session that starts during a deploy must read as "try again", never as "signed out" (fix 3 makes this true for the entry pages; the dashboard layout already throws to its error boundary).
