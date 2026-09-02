# Meta App Review — Runbook

**Status:** Business Verification starting 2026-09-02. **Owner:** chrisrogers37 (submission); engineering owns Tracks 2–3 inputs. **Closes:** the documentation half of [#410](https://github.com/chrisrogers37/storydump/issues/410).

Sibling to [`google-oauth-verification.md`](google-oauth-verification.md) — same shape, different provider. Read that one too if you are doing both; the Google submission is a smaller version of this problem.

## How to read the status markers

Every section below carries one. **They are not decoration** — a section researched from Meta's published documentation must not read as one somebody has walked, and that distinction is the whole reason this file is trustworthy.

| Marker | Means |
|---|---|
| **WALKED** | Somebody on this team performed these steps and this is what happened. |
| **DOCUMENTED-FROM-META'S-DOCS** | Taken from Meta's published requirements. Believed accurate, **nobody here has done it**. Expect the real flow to differ in detail. |
| **COMMUNITY-REPORTED** | Widely reported by other developers but **not found in Meta's own documentation**. Useful, and explicitly weaker than the row above. |
| **NOT-YET-ATTEMPTED** | Known to be required; no walkthrough exists and none is invented here. |

**THE FIRST THREE ARE RANKED, STRONGEST FIRST, AND THE RANK IS THE POINT.** `WALKED` is the strongest claim this file can make. `DOCUMENTED-FROM-META'S-DOCS` is weaker — believed, not observed. `COMMUNITY-REPORTED` is weaker still, and is the weakest thing here that is still an assertion. **Where two could apply, use the weaker one.** Four labels of apparently equal standing would give this document more categories and no more honesty, which is the failure the labels exist to prevent.

`NOT-YET-ATTEMPTED` sits outside that ranking: it reports *status* — nobody has done this — rather than where a claim came from, so it can coexist with any of the three.

**These rules are enforced, not requested.** `tests/test_meta_runbook_markers.py` pins them: a marker the legend does not define, a legend whose WALKED count disagrees with the body, an inline label inside a section already carrying it, a new section with no marker, or the ranking going missing all fail CI. **What it cannot check is whether a marker is CORRECT** — a claim labelled `DOCUMENTED-FROM-META'S-DOCS` that actually came from a forum post looks identical to a true one, and catching that took a human reading Meta's documentation. Sorting a claim into the right bucket is still yours.

**Scope:** the first three may be applied either to a whole section or inline to a single sentence. Section-level says *everything below came from here*; inline says *this one claim did, and its neighbours did not*. **Do not apply both to the same claim** — an inline label inside a section already carrying it is noise, not emphasis.

As of this draft **exactly one section is WALKED** — *Why App Review is required*, which records a failure this team actually hit. Everything else is research or not started.

*(An earlier revision of this paragraph said no section was WALKED, while the document marked one. A legend that miscounts its own labels is the same defect the labels exist to prevent, so it is corrected rather than quietly adjusted.)*

---

## ⚠ STANDING CONSTRAINT — do not drop the `legacy` schema before the demo videos are recorded

**Read this if you are working on migrations, not just if you are working on App Review.**

Track 3 requires **demo videos of a real Instagram publish**, recorded against a running system. Today that system is the **legacy** tier: `src/worker_impl.py` gates the worker on `WORKER_IMPL`, which runs the legacy scheduler unless an operator has set `WORKER_IMPL=target` on the service (a config fact, not visible from this repo — check Railway before assuming either way).

The M.3 cutover's last step is **3g `DROP SCHEMA legacy CASCADE`** (`documentation/planning/2026-08-02-consolidated-design-plan/04-execution-sequence.md:194`). Running it before the videos exist removes the only tier that can produce them.

**Precisely what is and is not at risk**, because the crude version of this warning is wrong and would be dismissed:

- **The data is not at risk.** Step 3f snapshots every legacy table to `archive.<t>_pre_cutover_<YYYYMMDD>` *before* anything is dropped.
- **The runnable system is.** A demo video needs a working publish path with a real account and real media, not a table you can `SELECT` from. After 3g, the legacy lineage is gone as a *running* thing, and the target tier can only replace it once it is actually serving.

**So the ordering constraint is: record the Track 3 videos, or arm and verify the target tier, before 3g runs.** Either satisfies it; neither is currently done.

**This paragraph is not sufficient protection and should not be treated as such.** A migration author executing the cutover reads `04-execution-sequence.md` and `scripts/m1_preflight.py`, not this runbook. A durable guard belongs at the 3g site — a preflight check that refuses while `#410` is open, or at minimum a note at `04-execution-sequence.md:194` pointing here. Filed as [#1202](https://github.com/chrisrogers37/storydump/issues/1202) rather than left as prose; until that lands, this is a convention, and conventions are exactly what get tidied away by someone acting in good faith.

---

## Why App Review is required

**Marker: WALKED** — this is the failure we actually hit.

Instagram Login OAuth works end-to-end for accounts Meta has allowlisted (`@gatortails` succeeded). Every other account is refused at the eligibility gate:

> Instagram Business API: Your Instagram account is ineligible for using Instagram Business Messaging API.

The gate is keyed off the app's **use case** in the Meta Developer Portal — *"Manage messaging & content on Instagram"* — which applies the stricter Messaging API criteria even though this app requests neither messaging permission. Until App Review grants **Advanced Access** on the two permissions below, every non-allowlisted tenant hits this wall. See [#410](https://github.com/chrisrogers37/storydump/issues/410) for the original diagnosis and [#341](https://github.com/chrisrogers37/storydump/issues/341) / [#378](https://github.com/chrisrogers37/storydump/issues/378) for the OAuth wiring.

### The permissions this app requests

| Permission | Declared at | Used for |
|---|---|---|
| `instagram_business_basic` | `src/services/target/ig_login_oauth.py:72`, `src/services/integrations/instagram_login_oauth.py:47` | Reading the connected account's own profile and its own media |
| `instagram_business_content_publish` | same | Creating and publishing media containers to the connected account |

Nothing else is requested. There is no messaging permission anywhere in the tree.

---

## Step 0 — rename the app to "Storydump"

**Marker: NOT-YET-ATTEMPTED.** Do this first; it is cheap and it is visible to every user.

The app still displays as **"Story Poster"**. Change it in **App settings → Basic → Display name** to **Storydump**.

- Users see this string on the OAuth consent screen. Submitting for review while it says "Story Poster" means every reviewer *and* every user sees a name that matches nothing else about the product.
- **COMMUNITY-REPORTED:** that a display-name change **does not trigger re-review** is the common report and is what [#410](https://github.com/chrisrogers37/storydump/issues/410) assumed. An earlier revision of this line attributed it to *Meta's documentation*; it was not sourced there. Do it before submission regardless, so the reviewer sees the name the screenshots and videos show.

---

## Track 1 — Business Verification (Chris's, in progress)

**Marker: NOT-YET-ATTEMPTED.** Deliberately a skeleton.

**No walkthrough is written here on purpose.** Chris is starting this today; the step-by-step should be filled in from what he actually hits, not from what Meta's docs imply he will. A runbook describing a flow nobody has walked is the confidently-wrong document this team keeps finding.

What Meta asks for (**DOCUMENTED-FROM-META'S-DOCS**):

- **Legal entity name** exactly as registered.
- **Business documentation** — one of: business license, certificate of incorporation, EIN assignment letter (US), or equivalent registration document.
- **Registered business address**, matching the documentation.
- **Business phone number**, which Meta verifies by call or SMS.
- **Domain ownership** of `storydump.app`, verified by DNS TXT record, HTML file upload, or meta tag.
- Verification happens in **Meta Business Manager**, not the app dashboard, and attaches to the *business* that owns the app — so the app must be correctly claimed by that business first.

**To fill in as you go:** which document Meta accepted, how long each stage took, anything it asked for that is not on the list above, and any point where the portal's wording diverged from it.

---

## Track 2 — App Verification

**Marker: mixed, per row.**

| Requirement | Status | Notes |
|---|---|---|
| Privacy Policy URL | **live** | `https://storydump.app/privacy` — `landing/src/app/(marketing)/privacy/page.tsx` |
| Terms of Service URL | **live** | `https://storydump.app/terms` — `landing/src/app/(marketing)/terms/page.tsx` |
| Deauthorize Callback URL | **built, not yet registered** | `POST /webhooks/meta/deauthorize` — shipped in #1208. Still has to be entered in App settings → Basic. |
| Data Deletion Request URL | **built, not yet registered** | `POST /webhooks/meta/data-deletion` — same PR, same remaining step. |
| App logo | **NOT-YET-ATTEMPTED, unowned** | 1024×1024 PNG, no transparency. **Nobody owns this.** It blocks submission and it is the kind of item that is discovered at the end. |

### Deauthorize Callback and Data Deletion Request

**Marker: DOCUMENTED-FROM-META'S-DOCS for the requirement; the implementation is alex's and is not duplicated here.**

Meta requires both before granting Advanced Access:

- **Deauthorize Callback URL** — Meta POSTs here when a user removes the app from their Instagram/Facebook account. The app must treat it as a revocation: stop using that account's tokens and stop scheduling for it.
- **Data Deletion Request URL** — Meta POSTs here when a user requests deletion of their data. The app must delete it and return a confirmation code plus a status URL the user can check.

Both receive a **signed request** from Meta, which must be verified against the app secret before anything is acted on — an unverified callback is an unauthenticated delete endpoint.

**Where they get registered:** App Dashboard → **App settings → Basic**, in the *Deauthorize Callback URL* and *Data Deletion Request URL* fields. Both must be `https://` and publicly reachable. **Deploy them before filling these fields in** — a URL that is not serving cannot be verified by Meta at any later point either, so there is no ordering in which deploying second helps.

> **COMMUNITY-REPORTED:** that Meta *probes the URL at save time and rejects the field outright* is widely reported by other developers and **was not found in Meta's own documentation**. The advice above does not depend on it being true.

**The paths, now that they are served** (#410 implementation PR):

| Field | URL |
|---|---|
| Deauthorize Callback URL | `https://api.storydump.app/webhooks/meta/deauthorize` |
| Data Deletion Request URL | `https://api.storydump.app/webhooks/meta/data-deletion` |

**One correction to the requirement as stated above, and it is deliberate.**
This section says the app "must delete it and return a confirmation code plus a
status URL". The implementation returns the code and the status URL, and
**deletes nothing synchronously.** Meta's response contract is a `url` plus a
`confirmation_code` precisely so completion can be asynchronous, so a receipt is
the specified shape rather than a shortfall — and three things make inline
deletion wrong here: the subject cannot be reliably identified (the schema
stores no Meta person), the blast radius is not the requester's to spend (an
Instagram account sits inside a workspace holding other members' content), and
the product's real deletion door — `offboard_workspace` — is owner-only with an
explicit confirm and a 30-day grace window, so an unauthenticated external
caller must not reach a stronger deletion than the owner's own.

**Also: which app you register these under decides which secret must verify
them.** `INSTAGRAM_APP_SECRET` and `FACEBOOK_APP_SECRET` are both accepted, so
either works — but note which one you used, because a future narrowing to a
single setting would silently refuse 100% of Meta's callbacks.

Full endpoint behaviour, cascade scope and the RLS bound:
[`meta-callback-endpoints.md`](meta-callback-endpoints.md).

---

## Track 3 — Advanced Access per scope

**Marker: NOT-YET-ATTEMPTED for the submission; the copy below is drafted and ready to paste.**

This is the longest track and the one that gates everything else, so its inputs are written now rather than when the other two clear. Both permissions need **justification copy** and a **demo video**.

### `instagram_business_basic` — justification copy

> Storydump is a scheduling tool for Instagram Stories. After a user connects their own Instagram Business account through Instagram Login, we use `instagram_business_basic` for exactly two things. First, to read that account's own profile (`GET /{ig-user-id}`) so the app can display which account is connected and store the account id the publishing calls need — without it a user with several connected accounts cannot tell them apart. Second, to read the account's own media and stories (`GET /{ig-user-id}/media`, `GET /{ig-user-id}/stories`) so the app can confirm that a story it scheduled actually published, and can avoid re-posting content that is already live. We read only the connected account's own data. We do not read other users' profiles, media, comments, or follower data, and we request no messaging permission of any kind.

### `instagram_business_content_publish` — justification copy

> Publishing is the product. A user points Storydump at a folder of their own media and sets a posting schedule; at each scheduled slot the app publishes one item to that user's own Instagram Business account as a Story. We use the standard two-step container flow: `POST /{ig-user-id}/media` with `media_type=STORIES` and an `image_url` or `video_url` pointing at the user's own media, then `POST /{ig-user-id}/media_publish` with the returned `creation_id`, polling `GET /{container_id}?fields=status_code,status` in between until the container is ready. Every publish is initiated by a schedule the account owner configured and can pause or cancel at any time; the app never publishes to an account other than the one whose owner connected it, and never publishes content the user did not place in their own connected media source.

*(Both are written against what the code actually calls — see `src/services/integrations/instagram_api.py`. If the API usage changes, change these; a justification that describes a call the app no longer makes is a rejection waiting to happen.)*

### Demo video script

**Marker: NOT-YET-ATTEMPTED.** Host on YouTube **unlisted** — the standard, and what the Google submission used.

Meta's reviewers are looking for one thing: *does the app use this permission the way the justification says it does?* Show the whole path, screen-recorded, no cuts, narrated or captioned.

**One video covering both permissions is acceptable and is easier to keep honest than two.** Record in this order:

1. **Start signed out.** Show the Storydump landing page at `storydump.app`. This establishes it is a real product, not a test harness.
2. **Sign in**, then start connecting an Instagram account.
3. **Show the consent screen in full**, long enough to read. The permission list must be legible — this is the single most-cited reason demo videos get rejected. Confirm the app name reads **Storydump** (Step 0), not "Story Poster".
4. **Complete the connection**, and show the app displaying the connected account's username and profile. *This is `instagram_business_basic` doing its job — say so in narration.*
5. **Show the media source** the user has pointed at, and the schedule they have configured.
6. **Publish a story** — either wait for a scheduled slot or trigger one — and show the app reporting success. *This is `instagram_business_content_publish`.*
7. **Open Instagram itself and show the story live on the account.** Reviewers want the effect, not the app's own success message.
8. **Show disconnecting the account**, so the revocation path is visible.

**Do not** speed up, cut between steps, or show a mocked screen. A video that skips the consent screen or shows a stubbed publish is the most common rejection.

**Prerequisite:** step 6 needs a running publish path — see the standing constraint at the top of this file.

---

## Submission flow

**Marker: DOCUMENTED-FROM-META'S-DOCS.** Nobody here has submitted; expect the portal to differ in detail and correct this section afterwards.

Order matters, because the later tracks depend on the earlier ones being accepted:

1. **Rename** (Step 0) — App settings → Basic.
2. **Business Verification** — Business Manager → Security Centre. Start early: Advanced Access is not granted while it is outstanding. *(That dependency is Meta's; **COMMUNITY-REPORTED:** that it is the longest of the three tracks is other developers' experience, not a published figure.)*
3. **Fill App settings → Basic completely** — privacy URL, terms URL, deauthorize callback, data deletion URL, app logo, category.
4. **App Review → Permissions and Features** — request Advanced Access on `instagram_business_basic` and `instagram_business_content_publish`, pasting the justification copy above into each.
5. **Attach the demo video** to the submission (both permissions can reference the same video; say so in each justification).
6. **Submit**, then watch the App Dashboard *and* the email on the developer account. *(**COMMUNITY-REPORTED:** that the dashboard notification is easy to miss is other developers' experience — watch both regardless, which costs nothing either way.)*

---

## Wait times

**Marker: COMMUNITY-REPORTED — the whole section.** Meta publishes no processing-time commitment for either track, so every number below is other developers' experience, including the one this runbook's own issue carried. Plan against them; do not quote them to anyone as Meta's.

- **App Review: 1–3 weeks** typically reported.
- **Business Verification: longer, and unbounded if documents are rejected.** Each rejection costs a full round trip.
  - An address mismatch between the uploaded document and the value entered in Business Manager is frequently cited by other developers as the cause. **Meta's own documentation does not state this**, and no frequency claim in this section is sourced — treat it as a thing worth double-checking before submitting, not as a documented failure mode.
- Treat the two as sequential for planning even though they run in parallel: Advanced Access will not be granted while Business Verification is outstanding.

---

## If review is rejected

**Marker: COMMUNITY-REPORTED — the whole section.** Meta publishes no taxonomy of rejection causes, so the specifics below are other developers' accounts and this team's inference, not documented behaviour. The instruction in the last bullet is the part that is ours and is binding.

- Meta names the specific permission and gives a reason, usually terse. Read it against the justification copy for *that* permission, not the app as a whole.
- The two failures worth pre-empting are both video failures: the consent screen not legible, and the published result not shown inside Instagram. Both are cheap to fix and cost a full review cycle. *(Commonly reported, not published by Meta. The demo-video script in Track 3 is built around these two — if they turn out to be wrong, that script is what needs revising.)*
- Resubmission does not reset Business Verification.
- **Record the rejection reason in this file** when it happens, and promote the relevant section's marker to WALKED. The next person through should not rediscover it.

---

## Operational alternative while review is pending

**Marker: DOCUMENTED-FROM-META'S-DOCS.**

Meta has the equivalent of Google's test-user list: accounts added under **App Dashboard → Roles** (as Administrators, Developers, or Testers) can use the app with **Standard Access** permissions without passing the eligibility gate. This is how `@gatortails` works today.

- Use it for closed beta only. It does not scale and it is not a substitute for review.
- Roles are per-person and require the invited account to accept.

---

## See also

- [`google-oauth-verification.md`](google-oauth-verification.md) — sibling runbook for Google Drive's `drive.readonly`. Same shape; the scope-justification section there is the model for Track 3 here.
- [`documentation/planning/2026-03-31-meta-app-launch-design.md`](../planning/2026-03-31-meta-app-launch-design.md) — the original Meta/Instagram OAuth design.
- `src/services/integrations/instagram_api.py` — every Graph call the justification copy describes.

## Related issues

- [#410](https://github.com/chrisrogers37/storydump/issues/410) — this runbook. Submission itself remains a manual operations task tracked there.
- [#371](https://github.com/chrisrogers37/storydump/issues/371) — Google OAuth verification runbook (sibling).
- [#341](https://github.com/chrisrogers37/storydump/issues/341), [#378](https://github.com/chrisrogers37/storydump/issues/378) — Instagram Login wiring; functional for allowlisted accounts.
