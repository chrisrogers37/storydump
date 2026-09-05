# Instagram Login OAuth Setup Guide

This guide covers setting up the **Instagram Login** OAuth flow so new users can connect their Instagram accounts through the Storydump Mini App. This is the newer, simpler flow that does **not** require a Facebook Page.

> **Prerequisite:** The code for this flow is already deployed (PR #122). This guide covers the external infrastructure setup needed to activate it.

---

## What This Enables

Once configured, the "Connect Instagram" button in the Mini App wizard will:
1. Open Instagram's OAuth consent screen
2. User authorizes your app to post to their account
3. Storydump stores their token (encrypted, per-tenant)
4. User can immediately start scheduling Stories

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
   serves from. The API moved from the Railway hostname to `api.storydump.app` on 2026-08-31, and
   a list that still held only the old host produced Meta's *"Invalid redirect_uri"* on 2026-09-04.
3. Click **Save Changes** — no redeploy is needed; Meta checks the list on every authorize request

> **Important:** The redirect URI must match EXACTLY — including the trailing path, no trailing slash, and the correct protocol (https).

---

## Step 4: Set Railway Environment Variables

Set these on the **API** service (the worker does not read them — the OAuth exchange and Meta's
callbacks both run on the API):

| Variable | Value | Where to find it |
|----------|-------|-------------------|
| `INSTAGRAM_APP_ID` | Your Meta App ID | App Dashboard → top of page |
| `INSTAGRAM_APP_SECRET` | Your Meta App Secret | App Settings → Basic → App Secret |
| `OAUTH_REDIRECT_BASE_URL` | `https://api.storydump.app` — no trailing slash or period | the API's public host |

These are **separate** from `FACEBOOK_APP_ID`/`FACEBOOK_APP_SECRET` (which are for the legacy Facebook Login OAuth flow). Both can coexist.

### How to set on Railway:

1. Go to [railway.app](https://railway.app) → your project
2. Click on the **API** service → Variables tab
3. Add the three variables above
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

After setting env vars and Railway redeployment:

1. Open Telegram → send `/start` to the Storydump bot
2. Open the Mini App wizard
3. Click **Connect Instagram**
4. You should see Instagram's OAuth consent screen (not Facebook's)
5. Authorize the app
6. You should be redirected to a success page saying "Connected!"
7. Return to the Mini App — it should show your Instagram account as connected

### Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| "Connect Instagram" opens Facebook OAuth | `INSTAGRAM_APP_ID` not set on API service | Check Railway env vars on the API service |
| "OAuth not configured" error | Missing `INSTAGRAM_APP_ID` or `INSTAGRAM_APP_SECRET` | Verify both are set on API service |
| "Invalid redirect_uri" from Meta | The URI the API sends is not in the app's OAuth redirect URI list | Add the exact value `https://api.storydump.app/auth/instagram-login/callback` (Step 3); the host must be the API's current one |
| "Link Expired" on callback | State token older than 10 minutes | Try connecting again — don't wait too long on the consent screen |
| User can't see consent screen | Not added as a tester | Add them via App Roles → Roles → Add People |
| "No Business Account" error | User has a personal Instagram account | They need to switch to Business or Creator in Instagram settings |

---

## Step 7: Going Live (Future — App Review)

To move beyond 4 test users:

1. Build a screencast showing the full user flow (connect → schedule → post)
2. Write a privacy policy and terms of service
3. Submit for **App Review** requesting:
   - `instagram_business_basic`
   - `instagram_business_content_publish`
4. Wait for approval (typically 2-7 business days, may require resubmission)
5. Switch app from **Development** to **Live** mode

**Do not do this yet** — Testing mode is sufficient for the initial tester cohort.

---

## Architecture Reference

The Instagram Login OAuth flow differs from the legacy Facebook Login flow:

| Aspect | Facebook Login (legacy) | Instagram Login (new) |
|--------|------------------------|----------------------|
| Auth URL | `facebook.com/dialog/oauth` | `api.instagram.com/oauth/authorize` |
| Scopes | `instagram_basic`, `instagram_content_publish`, `pages_show_list` | `instagram_business_basic`, `instagram_business_content_publish` |
| Facebook Page required | Yes | **No** |
| Account discovery | Token → Pages → IG Business Account | Direct — `user_id` in token response |
| Env vars | `FACEBOOK_APP_ID`, `FACEBOOK_APP_SECRET` | `INSTAGRAM_APP_ID`, `INSTAGRAM_APP_SECRET` |
| Callback route | `/auth/instagram/callback` | `/auth/instagram-login/callback` |

Both flows store tokens in the same `api_tokens` + `instagram_accounts` tables. The posting service doesn't care which flow was used.

### Routing Logic

The onboarding Mini App automatically selects the right flow:
- If `INSTAGRAM_APP_ID` is set → uses Instagram Login (preferred)
- If only `FACEBOOK_APP_ID` is set → uses Facebook Login (legacy)
- If neither is set → returns error

This means you can configure both and the system will prefer the newer flow.

---

## Related Documentation

- [Design Spec](../archive/2026-03-31-meta-app-launch-design.md) — Full architecture design for this feature
- [Meta Developer Docs: Instagram Login](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/) — Official reference
