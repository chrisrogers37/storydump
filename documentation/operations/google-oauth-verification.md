# Google OAuth Verification — Runbook

**Status:** Pending submission (as of 2026-10-06, the last time this page was touched; the console is the source of truth). **Owner:** chrisrogers37. **Closes:** #333.

The Google OAuth consent screen shows users a red **"Google hasn't verified this app"** warning when they connect Google Drive. They must click *Advanced → Go to storydump (unsafe)* to proceed. This blocks any tenant who isn't a developer of the project. This document walks through everything needed to clear it. Console paths use Google's current **Google Auth Platform** menu; an older console has the same fields under *APIs & Services → OAuth consent screen*.

## Why the warning fires

Google requires verification for **sensitive and restricted scopes** before they can be used in Production mode without warnings. Storydump's Drive integration requests:

| Scope | File | Class |
|---|---|---|
| `https://www.googleapis.com/auth/drive.readonly` | `src/services/target/google_drive_oauth.py:91` (`SCOPE`) | **Restricted** |
| `https://www.googleapis.com/auth/userinfo.email` (with `openid` and `userinfo.profile`) | `src/services/target/google_oidc.py:55` (`SCOPE = "openid email profile"`) — Google sign-in, not the Drive flow; the Drive leg dropped the older `userinfo.email` scope because nothing in the target schema stores the granting account's email (`google_drive_oauth.py:28`) | Standard |

The `drive.readonly` scope is what triggers the warning. Issue [#327](https://github.com/chrisrogers37/storydump/issues/327) audited the alternatives (`drive.file`, `drive.metadata.readonly`) and concluded that `drive.readonly` is the minimum viable scope — `drive.file` would break folder browsing (user media predates the app), and `drive.metadata.readonly` blocks file downloads (which we need to upload to Instagram). With scope-narrowing off the table, **verification submission is the only path to clear the warning** for non-developer users.

**`drive.readonly` is a restricted scope, not a sensitive one.** Google's list of Drive scopes ([Choose Google Drive API scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth), last updated 2026-09-03 UTC) classes it **Restricted**, beside `drive` and `drive.metadata.readonly`; `drive.file` is **Non-sensitive**. A restricted scope needs everything a sensitive one does plus a **security assessment**: the same page says an app that stores or transmits restricted-scope data on its servers must go through one, and storydump does both — each file's name, Drive id and content hash, which it stores, and the copy of the file a publish stages on Cloudinary (step 5a).

## What the user cap counts

Until verification clears, Google limits the app to **100 new users over its lifetime**, but only users who grant a sensitive or restricted scope count ([Unverified apps](https://support.google.com/cloud/answer/7454865)). Sign-in asks only for `openid`, `email` and `profile`, which Google exempts: no unverified-app page, no cap, no 7-day expiry ([OAuth app verification FAQ](https://support.google.com/cloud/answer/13463817)). So anyone admitted can sign in; the cap is spent by the people who **connect Google Drive**, and once it is reached the next Drive connect is refused. *Audience* shows how many of the 100 are used.

## Prerequisites checklist

Before opening the OAuth Brand / consent screen submission form:

- [x] **App Homepage URL** — `https://storydump.app` (live)
- [x] **Privacy Policy URL** — `https://storydump.app/privacy` (`landing/src/app/(marketing)/privacy/page.tsx`)
- [x] **Terms of Service URL** — `https://storydump.app/terms` (`landing/src/app/(marketing)/terms/page.tsx`)
- [ ] **App icon** — 120×120 PNG, no transparency: [`assets/app-icon/storydump-icon-120.png`](assets/app-icon/storydump-icon-120.png), drawn by `make-icon.py` beside it (#410). Made; still to upload.
- [x] **Authorized domain** — `storydump.app` verified via Google Search Console as a Domain property (2026-10-06). The Google TXT record at the registrar must stay.
- [ ] **OAuth Redirect URI registered** — `${OAUTH_REDIRECT_BASE_URL}/auth/google-drive/callback`. With `OAUTH_REDIRECT_BASE_URL = https://api.storydump.app` (the API's public origin, `guides/cloud-deployment.md`) that is `https://api.storydump.app/auth/google-drive/callback` (`src/api/routes/auth.py:327`). Add it under **Google Auth Platform → Clients → [the web client] → Authorized redirect URIs**, beside the sign-in callback `https://api.storydump.app/auth/google/callback`.
- [ ] **Scope justification copy** — short text explaining why we need `drive.readonly` (see template below).
- [ ] **Demo video** — screencast (≤ 5 min) demonstrating each requested scope in use. YouTube unlisted is fine.
- [ ] **Security assessment** — `drive.readonly` is restricted, so verification ends with one (step 5a). Budget for the assessor's fee before submitting.

## Step-by-step submission

### 1. Verify domain ownership

*Done 2026-10-06.* Kept for a re-verification.

1. Open [Google Search Console](https://search.google.com/search-console).
2. Add `storydump.app` as a property (Domain type, not URL prefix).
3. Pick **DNS verification** → copy the TXT record.
4. Add the TXT record at the registrar (whichever DNS provider hosts `storydump.app`).
5. Wait 5–60 min for propagation; click **Verify**.

### 2. Promote app to Production (if not already)

1. Open [Google Cloud Console](https://console.cloud.google.com/) → pick the storydump project.
2. **Google Auth Platform → Audience.**
3. Confirm **Publishing status: In production**. If it says **Testing**, click **Publish App**. Confirm the warning ("Your app will be available to any user with a Google Account") and submit.

> **Note:** Promoting to Production *without* verification keeps the unverified-app warning for everyone who connects Drive. The next steps clear it. Staying in **Testing** is no alternative ([Operational alternative](#operational-alternative)).

### 3. Fill the Branding fields

Under **Google Auth Platform → Branding**:

| Field | Value |
|---|---|
| App name | `Storydump` |
| User support email | `christophertrogers37@gmail.com` (or a team email) |
| App logo | upload `assets/app-icon/storydump-icon-120.png` |
| Application home page | `https://storydump.app` |
| Application privacy policy link | `https://storydump.app/privacy` |
| Application terms of service link | `https://storydump.app/terms` |
| Authorized domains | `storydump.app` (must match the verified domain in step 1) |
| Developer contact information | `christophertrogers37@gmail.com` |

Save.

### 4. Justify the scopes

Under **Google Auth Platform → Data Access**, make sure exactly these are listed (remove any other):

- `openid`, `.../auth/userinfo.email`, `.../auth/userinfo.profile` (sign-in)
- `.../auth/drive.readonly` (Drive)

Google asks for a justification per sensitive or restricted scope, on Data Access or in the submission form. **`drive.readonly` is the one Google will scrutinize.** Suggested copy:

> Storydump turns a user's Google Drive folder into daily Instagram Stories. A workspace admin connects Google Drive once, then picks one or more existing folders of photos and videos from a folder browser inside Storydump. We call `files.list` to browse folders and to list the files under each chosen folder, reading only each file's id, name, MIME type, size, modified time and checksum (the top-level subfolder a file sits in becomes its category), `files.get` for one file's size, type and name, and with `alt=media` to download its bytes when it is posted, so it can be uploaded to Instagram. To keep two chosen folders from overlapping, we also read the parent ids of the folders above a chosen one. We never write to, modify, or delete files in the user's Drive — no `files.create`, `files.update`, or `files.delete` — and we never download files outside the chosen folders. We store each file's name, Drive id and checksum to track what has been posted; the file itself is copied to our media host only for the publish, deleted once it posts or is cancelled, and otherwise removed by a time-limited cleanup sweep. `drive.file` does not work for us: it reaches only files our app created or the user picks one at a time, and our users' media already sits in folders that keep receiving new files. `drive.metadata.readonly` cannot download file contents, which posting requires.

(Adjust wording to current implementation — the gist is: read-only, narrow folder scope, no writes, no exfiltration.)

### 5. Submit for verification

**Google Auth Platform → Verification Center** → **Submit for verification** (it lists anything still missing).

Google will ask for the demo video URL. Record one that shows:

1. A user signing into Storydump.
2. Reaching the **Google OAuth consent screen** — pause long enough to clearly show the requested scopes listed, with the browser's address bar in view so the `client_id=` in the URL is readable (reviewers commonly reject videos that skip past this; they want to see the scope list and the client on-screen).
3. Granting the Drive scope.
4. Storydump listing files from the connected folder.
5. A post going out (which reads file bytes from Drive).
6. The user disconnecting / revoking access.

YouTube unlisted is the standard hosting. Aim for under 5 minutes (convention, not a hard limit — Google will accept longer if the content justifies it).

### 5a. Complete the security assessment

Because `drive.readonly` is restricted, verification ends with a **security assessment** by a Google-authorised assessor under the App Defense Alliance's CASA framework, and it repeats **at least every 12 months** after the assessor's approval ([Restricted scope verification](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification), last updated 2026-08-19 UTC). Google's pages state no fee: the assessor charges it, and assessors publish their own prices.

### 6. Wait + respond to review

Google says restricted-scope verification "can potentially take several weeks to complete", because of the security assessment ([Restricted scope verification](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification), last updated 2026-08-19 UTC); whether the assessor's time is inside that figure is not stated. The 2–6 weeks this page used to give is unsourced. The team may send back a list of clarifying questions or screencast re-records. Reply through the Cloud Console verification ticket — *do not* open a new submission.

While waiting:
- The unverified-app warning continues to show. Users still have the "Go to storydump (unsafe)" workaround.
- Each account that connects Drive for the first time counts toward the 100-user cap ([What the user cap counts](#what-the-user-cap-counts)), so beta invites are paced by the cap that is left.
- Test users do **not** skip the warning ([Operational alternative](#operational-alternative)).

### 7. After approval

- Consent screen shows the app logo and a verified badge (no red warning).
- New tenants can complete Drive connect with a clean Google flow.
- **One code change:** the Google Drive card's warning sentence, `driveConnectWarning` in `landing/src/lib/drive.ts`, becomes false. Remove it, its use in `landing/src/components/dashboard/settings/drive-card.tsx` (the `connectWarning` paragraph and the button's `aria-describedby`), and its pins in `landing/src/lib/drive.test.ts` and `landing/src/lib/condition-words-contract.test.ts`, then close #333.
- Calendar the yearly security-assessment renewal (step 5a).

## If verification is rejected

Most common reasons:

1. **Demo video incomplete** — re-record showing every requested scope.
2. **Privacy policy missing required disclosures** — the `/privacy` page already covers the [Google API Services User Data Policy Limited Use](https://developers.google.com/terms/api-services-user-data-policy#limited-use) clause. Keep the wording aligned with that policy.
3. **Scope justification too vague** — be specific about which API endpoints we call and why each is needed.

## Operational alternative

For a closed beta until verification clears, stay in production; testers take the *Advanced → Go to storydump (unsafe)* way past Google's page. No list skips that page. Google's test-user list applies only while the app is in **Testing**: only listed accounts (up to 100) can grant at all, each still sees an unverified-app page (worded as "an app that's currently being tested", with a Continue link), and every grant expires after 7 days, so each Drive connection would need a reconnect weekly. In production the list is ignored.

## See also

- [`documentation/guides/cloud-deployment.md`](../guides/cloud-deployment.md) — `OAUTH_REDIRECT_BASE_URL` and Railway env-var setup.
- [`documentation/archive/2026-03-31-meta-app-launch-design.md`](../archive/2026-03-31-meta-app-launch-design.md) — sibling Meta/Instagram OAuth verification story (different provider, similar shape).

## Related issues

- [#327](https://github.com/chrisrogers37/storydump/issues/327) — *Closed.* Drive scope audit; team kept `drive.readonly`. Captures the tradeoff context for future reference.
- [#333](https://github.com/chrisrogers37/storydump/issues/333) — This runbook closes the documentation piece. Submission itself is a manual operations task tracked there.
