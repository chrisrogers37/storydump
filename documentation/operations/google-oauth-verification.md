# Google OAuth Verification — Runbook

**Status:** Pending submission. **Owner:** chrisrogers37. **Closes:** #333.

The Google OAuth consent screen shows users a red **"Google hasn't verified this app"** warning when they connect Google Drive. They must click *Advanced → Go to storydump (unsafe)* to proceed. This blocks any tenant who isn't a developer of the project. This document walks through everything needed to clear it.

## Why the warning fires

Google flags **sensitive scopes** for verification before they can be used in Production mode without warnings. Storydump's Drive integration requests:

| Scope | File | Class |
|---|---|---|
| `https://www.googleapis.com/auth/drive.readonly` | `src/services/integrations/google_drive_oauth.py:58` | **Sensitive** |
| `https://www.googleapis.com/auth/userinfo.email` | (same) | Standard |

The `drive.readonly` scope is what triggers the warning. Issue [#327](https://github.com/chrisrogers37/storydump/issues/327) audited the alternatives (`drive.file`, `drive.metadata.readonly`) and concluded that `drive.readonly` is the minimum viable scope — `drive.file` would break folder browsing (user media predates the app), and `drive.metadata.readonly` blocks file downloads (which we need to upload to Instagram). With scope-narrowing off the table, **verification submission is the only path to clear the warning** for non-developer users.

## Prerequisites checklist

Before opening the OAuth Brand / consent screen submission form:

- [x] **App Homepage URL** — `https://storydump.app` (live)
- [x] **Privacy Policy URL** — `https://storydump.app/privacy` (`landing/src/app/(marketing)/privacy/page.tsx`)
- [x] **Terms of Service URL** — `https://storydump.app/terms` (`landing/src/app/(marketing)/terms/page.tsx`)
- [ ] **App icon** — 120×120 PNG, no transparency. Need to design.
- [ ] **Authorized domain** — `storydump.app` verified via Google Search Console.
- [ ] **OAuth Redirect URI registered** — `${OAUTH_REDIRECT_BASE_URL}/auth/google-drive/callback`. Currently `https://storyline-ai-production.up.railway.app/auth/google-drive/callback` on Railway. Should be added under the OAuth client in Google Cloud Console.
- [ ] **Scope justification copy** — short text explaining why we need `drive.readonly` (see template below).
- [ ] **Demo video** — screencast (≤ 5 min) demonstrating each requested scope in use. YouTube unlisted is fine.

## Step-by-step submission

### 1. Verify domain ownership

1. Open [Google Search Console](https://search.google.com/search-console).
2. Add `storydump.app` as a property (Domain type, not URL prefix).
3. Pick **DNS verification** → copy the TXT record.
4. Add the TXT record at the registrar (whichever DNS provider hosts `storydump.app`).
5. Wait 5–60 min for propagation; click **Verify**.

### 2. Promote app to Production (if not already)

1. Open [Google Cloud Console](https://console.cloud.google.com/) → pick the storydump project.
2. **APIs & Services → OAuth consent screen.**
3. Confirm **Publishing status: In production**. If it says **Testing**, click **Publish App**. Confirm the warning ("Your app will be available to any user with a Google Account") and submit.

> **Note:** Promoting to Production *without* verification keeps the unverified-app warning for non-test users. The next steps clear it.

### 3. Fill OAuth consent screen fields

Still under **OAuth consent screen**:

| Field | Value |
|---|---|
| App name | `Storydump` |
| User support email | `christophertrogers37@gmail.com` (or a team email) |
| App logo | upload the 120×120 PNG |
| Application home page | `https://storydump.app` |
| Application privacy policy link | `https://storydump.app/privacy` |
| Application terms of service link | `https://storydump.app/terms` |
| Authorized domains | `storydump.app` (must match the verified domain in step 1) |
| Developer contact information | `christophertrogers37@gmail.com` |

Save.

### 4. Justify the scopes

Under **Scopes**, make sure these are listed:

- `.../auth/userinfo.email`
- `.../auth/drive.readonly`

For each, click **Edit scope** → fill in the justification. **`drive.readonly` is the one Google will scrutinize.** Suggested copy:

> Storydump is an Instagram Story scheduling tool. Users connect a Google Drive folder containing the media they want Storydump to post. We list files recursively under that single user-chosen folder to build a content catalog (filename, MIME type, thumbnail URL, category from subfolder structure) and read each file's bytes once per post to upload to Instagram. We never write to, modify, or delete files in the user's Drive. We do not access files outside the folder the user explicitly pointed us at, but the `drive.file` scope is too restrictive for this workflow because it doesn't grant folder-traversal of pre-existing user files. We use `drive.readonly` to enable read-only access to one user-chosen folder; we discard scope to any other Drive content via app-side filtering on `parents` chain.

(Adjust wording to current implementation — the gist is: read-only, narrow folder scope, no writes, no exfiltration.)

### 5. Submit for verification

Bottom of the consent screen → **Submit for verification**.

Google will ask for the demo video URL. Record one that shows:

1. A user signing into Storydump.
2. Granting the Drive scope.
3. Storydump listing files from the connected folder.
4. A post going out (which reads file bytes from Drive).
5. The user disconnecting / revoking access.

YouTube unlisted is the standard hosting. Keep the video under 5 minutes.

### 6. Wait + respond to review

Google review timeline is typically **2–6 weeks**. The team may send back a list of clarifying questions or screencast re-records. Reply through the Cloud Console verification ticket — *do not* open a new submission.

While waiting:
- The unverified-app warning continues to show. Users still have the "Go to storydump (unsafe)" workaround.
- Testing-mode allowlists (added under OAuth consent → Test users) bypass the warning for whitelisted Google accounts. Useful for letting beta testers in without the scary screen.

### 7. After approval

- Consent screen shows the app logo and a verified badge (no red warning).
- New tenants can complete Drive connect with a clean Google flow.
- **No code changes needed.**

## If verification is rejected

Most common reasons:

1. **Demo video incomplete** — re-record showing every requested scope.
2. **Privacy policy missing required disclosures** — the `/privacy` page already covers the [Google API Services User Data Policy Limited Use](https://developers.google.com/terms/api-services-user-data-policy#limited-use) clause. Keep the wording aligned with that policy.
3. **Scope justification too vague** — be specific about which API endpoints we call and why each is needed.

## Operational alternative

For internal/closed beta until verification clears:

- Add each beta tester's Google account under **OAuth consent → Test users**. Up to 100 testers. They bypass the unverified warning entirely.
- Stays valid even after re-submitting verification.

## Related issues

- [#327](https://github.com/chrisrogers37/storydump/issues/327) — *Closed.* Drive scope audit; team kept `drive.readonly`. Captures the tradeoff context for future reference.
- [#333](https://github.com/chrisrogers37/storydump/issues/333) — This runbook closes the documentation piece. Submission itself is a manual operations task tracked there.
