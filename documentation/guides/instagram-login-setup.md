# Instagram Login OAuth Setup Guide

This guide covers the external setup behind **Instagram Login** — the one flow a
workspace uses to connect an Instagram account: the Meta app, its redirect URI,
and the variables the API reads. It does **not** require a Facebook Page.

The flow is the target tier's (`src/services/target/ig_login_oauth.py`, #1220).
The legacy tier's Facebook Login flow, and the Telegram Mini App wizard that
used to start this one, went with that tier in the tear-out (#1216, September
2026); its data survives as the `archive.*_pre_cutover_20260917` snapshots.

---

## What This Enables

A workspace admin, signed in on the web, opens **Settings › Accounts** and
presses **Connect Instagram**. Then:

1. The API mints a one-shot state row in `oauth_states` and answers with
   Instagram's authorization URL (`POST /api/v1/workspaces/{ws}/accounts/connect`,
   `src/api/routes/v1.py:938`; a row's own **Connect Instagram** /
   **Reconnect Instagram** is `…/accounts/{account_id}/connect`, `:989`). Both
   need a web session and the admin role; an API token is refused.
2. The person authorizes the app on Instagram's consent screen.
3. Instagram returns to `GET /auth/instagram-login/callback`
   (`src/api/routes/auth.py:382`). The API consumes the state, checks the
   returning browser carries the session of the user who started, exchanges the
   code for a long-lived token, and reads the account's id and username
   (`exchange_code`, `ig_login_oauth.py:552`).
4. The account lands in `ig_accounts` — the row the state named, the row this
   account already has in the workspace, or a new scheduled one
   (`provisioning.connect_destination`, `src/services/target/provisioning.py:675`)
   — and the token is written to `oauth_credentials`, encrypted under the
   Fernet key ring (`store_credential`, `ig_login_oauth.py:352`).
5. The browser is sent back to the web's settings screen
   (`/dashboard/settings?connected=instagram`).

The worker keeps the token alive: a `refresh_credential` job comes due 7 days
after the connect and every 7 days after (`FIRST_REFRESH_INTERVAL`,
`ig_login_oauth.py:79`; `WorkerConfig.refresh_cadence_seconds`,
`src/services/target/work_loop.py:124`). A refresh Meta definitively rejects
flips the credential to `expired` and the account to `reauth_required`
(`mark_dead`, `ig_login_oauth.py:467`); the row then reads **Reconnect needed**
on the web, and the clock mints a reconnect prompt to the workspace's bound
chats (`reauth_prompt`, `src/services/target/credential_lifecycle.py:313`).

**No Facebook Page required** — users only need an Instagram Business or Creator account.

---

## Step 1: Register a Meta Developer App

1. Go to [developers.facebook.com](https://developers.facebook.com)
2. Click **My Apps** → **Create App**
3. Select **Other** as use case
4. Select **Business** as app type
5. Enter app name (e.g., "Storydump") and contact email
6. Click **Create App**

**Save these values:**
- **App ID** — shown at top of the app dashboard
- **App Secret** — found in App Settings → Basic → App Secret (click "Show")

---

## Step 2: Add Instagram Product

1. In your app dashboard, click **Add Product** in the left sidebar
2. Find **Instagram** and click **Set Up**
3. This adds Instagram API capabilities to your app

---

## Step 3: Configure OAuth Redirect URI

1. In the left sidebar go to **Products → Instagram → API setup with Instagram business login**
   (Basic Display no longer exists; the direct page is
   `https://developers.facebook.com/apps/<APP_ID>/instagram-business/API-Setup/`)
2. Under step 3, **Set up Instagram business login → Business login settings → OAuth redirect URIs**, add:
   ```
   https://api.storydump.app/auth/instagram-login/callback
   ```
   The value is `OAUTH_REDIRECT_BASE_URL` + `/auth/instagram-login/callback` — whatever host the API
   serves from (`src/api/instagram_client.py:17`, joined in `src/api/oauth_client.py:33-37`). The API
   moved from the Railway hostname to `api.storydump.app` on 2026-08-31, and a list that still held
   only the old host produced Meta's *"Invalid redirect_uri"* on 2026-09-04.
3. Click **Save Changes** — no redeploy is needed; Meta checks the list on every authorize request

> **Important:** The redirect URI must match EXACTLY — including the trailing path, no trailing slash, and the correct protocol (https).

---

## Step 4: Set Railway Environment Variables

Set these on the **API** service, `storydump`. The worker reads none of them: the
code exchange and Meta's callbacks both run on the API, and the worker's refresh
sends the token alone (`refresh_params`, `ig_login_oauth.py:322`).

| Variable | Value | Where to find it |
|----------|-------|-------------------|
| `INSTAGRAM_APP_ID` | Your Meta App ID | App Dashboard → top of page |
| `INSTAGRAM_APP_SECRET` | Your Meta App Secret | App Settings → Basic → App Secret |
| `OAUTH_REDIRECT_BASE_URL` | `https://api.storydump.app` — no trailing slash or period | the API's public host |

Those three are everything the connect route reads
(`src/api/instagram_client.py:20-28`). With any of them unset it answers
`503 instagram oauth not configured: set <the missing names>`
(`src/api/oauth_client.py:23-32`) — the flow never half-works.

Two more are read on the way:

- `ENCRYPTION_KEY` (or `ENCRYPTION_KEYS`, newest first) — the Fernet key the
  token is encrypted with (`ring`, `ig_login_oauth.py:341`). Set it on the
  worker too: the refresh and the publish decrypt with it
  (`credential_lifecycle.py:266`, `src/services/target/ig_credentials.py:103`).
- `WEB_APP_URL` — where the finished flow, and a failed one, lands. Without it
  success redirects to `/` on the API and a failure answers JSON 400
  (`_landing` and `_fail`, `src/api/routes/auth.py:130-152`).

`FACEBOOK_APP_SECRET` is **not** part of this flow. It survives as one setting
because Meta's signed policy callbacks (`POST /webhooks/meta/deauthorize`,
`POST /webhooks/meta/data-deletion`) verify against `INSTAGRAM_APP_SECRET`
first and then against it (`app_secrets`,
`src/services/target/meta_callbacks.py:116`): set it only if those callback
URLs are registered under a Meta app whose secret differs. See
[`meta-callback-endpoints.md`](../operations/meta-callback-endpoints.md).

### How to set on Railway:

1. Go to [railway.app](https://railway.app) → your project
2. Click on the **storydump** service → Variables tab
3. Add the variables above
4. The service redeploys automatically

---

## Step 5: Add Test Users

Your app starts in **Testing mode**, limited to users with explicit roles. To let testers use the OAuth flow:

1. Go to **App Roles** → **Roles** in the app dashboard
2. Click **Add People**
3. Add each tester's Facebook account (they need a Facebook account, even though the OAuth itself uses Instagram Login)
4. Assign the **Tester** role
5. Each tester must accept the invitation from their Facebook notifications

**Limits in Testing mode:**
- Up to 4 users with roles (Admin, Developer, Tester, Analytics User)
- Only these users can authorize the app
- No App Review required

---

## Step 6: Verify the Setup

After setting the variables and the redeploy:

1. Sign in on the web as an admin of the workspace
2. Open **Settings › Accounts** and press **Connect Instagram**
3. You should see Instagram's OAuth consent screen
4. Authorize the app
5. You land back on Settings with `?connected=instagram`, and the account is
   listed as connected
6. From a terminal, `storydump account <handle>` shows the account's cap, zone
   and next slot

### Troubleshooting

A failed callback lands on the web's `/auth/error?reason=<reason>&flow=instagram`
(`_fail`, `src/api/routes/auth.py:130`). The reasons are a closed set.

| Problem | Cause | Fix |
|---------|-------|-----|
| Connect fails, and the API answers `503 instagram oauth not configured: set …` | One of `INSTAGRAM_APP_ID`, `INSTAGRAM_APP_SECRET`, `OAUTH_REDIRECT_BASE_URL` is unset on the API | Set the variables the message names (Step 4) |
| "Invalid redirect_uri" from Meta | The URI the API sends is not in the app's OAuth redirect URI list | Add the exact value `https://api.storydump.app/auth/instagram-login/callback` (Step 3); the host must be the API's current one |
| `reason=denied` | The person, or Instagram, declined on the consent screen | Start again |
| `reason=missing_params` | Instagram returned without a `state` or a `code` — a hand-edited or truncated return URL (`src/api/routes/auth.py`) | Press Connect again |
| `reason=state_refused` | The state is unknown, already used, or older than 15 minutes (`STATE_TTL_SECONDS`, `ig_login_oauth.py:74`); or the returning browser is not signed in as the user who pressed Connect; or that user is no longer an admin | Press Connect again, in the browser you are signed in with, and finish within 15 minutes |
| `reason=exchange_failed` | One of the three provider calls failed — the code exchange, the long-lived exchange, or the profile read | The API's log names which, with the status Instagram answered (`instagram connect: exchange refused: …`) |
| `reason=wrong_account` | A row's connect or reconnect was authorized while signed in to a different Instagram account than the row names (`attach_connected_identity`, `src/services/target/provisioning.py:541`) | Sign in to the right Instagram account and try again, or use the header's **Connect Instagram** to add the other account as its own destination |
| `reason=already_connected` | The account is already another destination in this workspace | Reconnect that row instead |
| `reason=destination_gone` / `workspace_closing` | The row was removed, or the workspace is being offboarded | — |
| User can't see consent screen | Not added as a tester | Add them via App Roles → Roles → Add People |
| Instagram refuses the account as *ineligible* | Until App Review grants Advanced Access, every account Meta has not allowlisted is refused at Meta's eligibility gate — the failure this team hit | [`meta-app-review.md`](../operations/meta-app-review.md), *Why App Review is required* |
| A connected account reads **Reconnect needed** | A refresh was definitively rejected, or the stored token decrypts under no key in the ring (`load_credential`, `ig_login_oauth.py:437`) | Press **Reconnect Instagram** on the row |

---

## Step 7: Going Live (App Review)

To move beyond users with a role on the Meta app, the app needs **Advanced
Access** on the two scopes this flow requests
(`REQUIRED_SCOPES`, `ig_login_oauth.py:83`):

- `instagram_business_basic`
- `instagram_business_content_publish`

That submission has its own runbook, with every claim marked by how strongly it
is evidenced: [`meta-app-review.md`](../operations/meta-app-review.md).

---

## Architecture Reference

| Aspect | Instagram Login |
|--------|-----------------|
| Authorize URL | `https://api.instagram.com/oauth/authorize` |
| Code exchange | `POST https://api.instagram.com/oauth/access_token`, then `GET https://graph.instagram.com/access_token` (long-lived, 60 days), then `GET https://graph.instagram.com/me` |
| Refresh | `GET https://graph.instagram.com/refresh_access_token` — the token alone, no app secret |
| Scopes | `instagram_business_basic`, `instagram_business_content_publish` |
| Facebook Page required | **No** |
| Account discovery | Direct — `user_id` and `username` from the profile read |
| Variables | `INSTAGRAM_APP_ID`, `INSTAGRAM_APP_SECRET`, `OAUTH_REDIRECT_BASE_URL` |
| Start routes | `POST /api/v1/workspaces/{ws}/accounts/connect`, `POST /api/v1/workspaces/{ws}/accounts/{account_id}/connect` |
| Callback route | `GET /auth/instagram-login/callback` |
| State | a row in `oauth_states`, one-shot, 15 minutes; a newer state for the same account retires the older one |
| Where the account lives | `ig_accounts` (`provider_account_ref` is the real Meta id; `state` is `active`, `reauth_required`, `disabled` or `moved`) |
| Where the token lives | `oauth_credentials`, provider `ig_login`, one row per account, replaced in place on a reconnect |

The URLs are `ig_login_oauth.py:85-90`. Every provider call goes through the
egress floor (`src/services/target/egress.py`) and none runs inside a database
transaction: the state is consumed and committed before Instagram is contacted,
so a failed exchange costs a fresh click and nothing else.

There is one flow, so there is no routing between flows: a deployment without
`INSTAGRAM_APP_ID` cannot connect an account at all.

---

## Related Documentation

- [Design Spec](../archive/2026-03-31-meta-app-launch-design.md) — the original design for this feature (archived; it describes the legacy implementation)
- [`meta-app-review.md`](../operations/meta-app-review.md) — the App Review runbook
- [`meta-callback-endpoints.md`](../operations/meta-callback-endpoints.md) — Meta's deauthorize and data-deletion callbacks
- [Meta Developer Docs: Instagram Login](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/) — Official reference
