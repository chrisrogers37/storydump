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
- [ ] **OAuth Redirect URI registered** — `${OAUTH_REDIRECT_BASE_URL}/auth/google-drive/callback`. Currently `https://storyline-ai-production.up.railway.app/auth/google-drive/callback` on Railway. Add it under **APIs & Services → Credentials → [OAuth 2.0 Client] → Authorized redirect URIs**. (`OAUTH_REDIRECT_BASE_URL` is documented in [`documentation/guides/cloud-deployment.md`](../guides/cloud-deployment.md).)
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

> Storydump is an Instagram Story scheduling tool. Users connect their Google Drive once, then pick the folder(s) containing the media they want Storydump to post from a browser of that Drive. We use `files.list` to enumerate files recursively under each user-chosen folder (building a content catalog of filename, MIME type, thumbnail URL, and category from subfolder structure) and `files.get` with `alt=media` to read each file's bytes once per post for upload to Instagram. We never write to, modify, or delete files in the user's Drive — no `files.create`, `files.update`, or `files.delete`. We restrict access to the user-chosen folders via app-side filtering on the `parents` chain and never read files outside it. The `drive.file` scope was evaluated and rejected because it does not grant traversal of pre-existing user files, only files the app itself creates or the user opens via the Picker — incompatible with our "pick an existing folder" workflow, in which the folders (and the files later added to them) already exist and change without the app.

(Adjust wording to current implementation — the gist is: read-only, narrow folder scope, no writes, no exfiltration.)

### 5. Submit for verification

Bottom of the consent screen → **Submit for verification**.

Google will ask for the demo video URL. Record one that shows:

1. A user signing into Storydump.
2. Reaching the **Google OAuth consent screen** — pause long enough to clearly show the requested scopes listed (reviewers commonly reject videos that skip past this; they want to see the scope list on-screen).
3. Granting the Drive scope.
4. Storydump listing files from the connected folder.
5. A post going out (which reads file bytes from Drive).
6. The user disconnecting / revoking access.

YouTube unlisted is the standard hosting. Aim for under 5 minutes (convention, not a hard limit — Google will accept longer if the content justifies it).

### 6. Wait + respond to review

Google review timeline is typically **2–6 weeks**. The team may send back a list of clarifying questions or screencast re-records. Reply through the Cloud Console verification ticket — *do not* open a new submission.

While waiting:
- The unverified-app warning continues to show. Users still have the "Go to storydump (unsafe)" workaround.
- Google accounts added under **OAuth consent → Test users** (up to 100) **bypass the warning entirely** — they see a clean consent screen. Every other user sees the red "Google hasn't verified this app" page. Useful for letting beta testers in without the scary screen.

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

## See also

- [`documentation/guides/cloud-deployment.md`](../guides/cloud-deployment.md) — `OAUTH_REDIRECT_BASE_URL` and Railway env-var setup.
- [`documentation/archive/2026-03-31-meta-app-launch-design.md`](../archive/2026-03-31-meta-app-launch-design.md) — sibling Meta/Instagram OAuth verification story (different provider, similar shape).

## Related issues

- [#327](https://github.com/chrisrogers37/storydump/issues/327) — *Closed.* Drive scope audit; team kept `drive.readonly`. Captures the tradeoff context for future reference.
- [#333](https://github.com/chrisrogers37/storydump/issues/333) — This runbook closes the documentation piece. Submission itself is a manual operations task tracked there.
