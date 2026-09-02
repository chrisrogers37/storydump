# Meta callback endpoints — deauthorize and data deletion

The implementation half of #410's App Verification track. The submission
runbook is [`meta-app-review.md`](meta-app-review.md) and owns the process:
which app, which fields, the wait times, the demo videos. **This file owns the
endpoints themselves** — what they do, what they deliberately do not, and the
bounds they run under. That split is #1203's, which says outright that the
implementation "is alex's and is not duplicated here".

## The two endpoints

Both live under `/webhooks/meta` and both verify Meta's `signed_request` before
doing anything. **They are different verbs and are deliberately not shared code
paths** — Meta treats deauthorize and deletion as different events, and
conflating them would destroy tenant data on a mere disconnect.

| | Deauthorize | Data deletion |
|---|---|---|
| URL | `POST /webhooks/meta/deauthorize` | `POST /webhooks/meta/data-deletion` |
| Fires when | a person removes the app | a person asks Meta to delete their data |
| Effect | credentials marked `revoked` | a **receipt** is recorded |
| Deletes data | **no** | **not synchronously — see below** |
| Response | `{"status": "ok"}` | `{"url": ..., "confirmation_code": ...}` |

A third, read-only door backs the returned URL:
`GET /webhooks/meta/data-deletion/status?code=<code>`.

## Why deletion is deferred-with-receipt

**The shape is Meta's own.** The callback's response contract is a `url` plus a
`confirmation_code` precisely so completion can be asynchronous. Returning a
receipt is what the integration specifies, not a way around it.

Three reasons it must not delete inline, in increasing order of weight:

1. **The subject cannot be reliably identified.** Meta sends an app-scoped
   person id. The target schema stores no Meta person:
   `user_identities.provider` is CHECK-constrained to `('telegram','google')`
   and `oauth_credentials` is keyed to an `ig_account_id`. The only
   Meta-shaped identifier held is `ig_accounts.provider_account_ref`, which
   names an **account** rather than a person and is sometimes a provisional
   `manual:<handle>` that no Meta id will ever equal.
2. **The blast radius is not the requester's to spend.** An Instagram account
   lives inside a workspace that may hold other members' content. Somebody
   disconnecting an integration has no authority to erase it.
3. **The product already has the right door, and it is deliberately not
   automatic.** `offboard_workspace` is owner-only, demands an explicit
   `confirm`, and runs a 30-day grace window before anything is irreversible.
   An unauthenticated external caller must not reach a **stronger** deletion
   than the owner's own confirmed one.

So the receipt is the deliverable, and completing it stays a human,
owner-confirmed act through the existing door.

## Cascade scope — what is and is not touched

**Deauthorize** updates `oauth_credentials.state` to `revoked` for the matching
`ig_accounts` rows, scoped on the `(ig_account_id, workspace_id)` **pair**.
Nothing else: not the account row, its media, its posting history, its
workspace, or any other member's content.

**Data deletion** writes nothing. It logs the request and returns the receipt.

**Neither endpoint issues a SQL `DELETE`.** A test parses both modules' AST and
asserts it.

## The RLS bound — read this before believing a zero

Every table these callbacks touch is tenant-scoped under row-level security:
`ig_accounts` and `oauth_credentials` (`058_rls_and_policies.sql:266,278`) and
`audit_events` (`:188-192`), all keyed on
`current_setting('app.tenant_id')` for `svc_ingress` and `svc_worker`.

**A Meta callback names no workspace**, so there is no tenant to set. Two
consequences, and they point in opposite directions:

- **As `svc_ingress` or `svc_worker`**, the lookup returns **zero rows
  regardless of what is stored**, and the revoke updates nothing. The endpoint
  answers 200 and looks healthy while being structurally blind.
- **As a table owner**, RLS is bypassed — no migration anywhere declares
  `FORCE ROW LEVEL SECURITY` — so the statements run unfiltered. This is why
  the revoke predicate carries the workspace explicitly: without it, an
  owner-role connection would make this a cross-tenant credential write.

So **a zero from these endpoints is "not established", never "no such
account"**, and the logs say exactly that. The durable fix is a named,
reviewable, tenant-free door — a SECURITY DEFINER function in the shape of
`fn_invitation_accept` — which needs a migration and is deliberately **not**
in this change.

That same bound is why the deletion receipt is **logged rather than stored**.
`audit_events` is the natural home (no foreign key, outlives a workspace's
cascade), but a row could be neither written nor read back by confirmation
code without a tenant context. An endpoint that queried anyway would answer
"not found" for every genuine receipt.

## The app secret is not one setting

`settings` carries two — `INSTAGRAM_APP_SECRET` (annotated *preferred*) and
`FACEBOOK_APP_SECRET` (annotated *legacy*). Which one signs these callbacks is
decided by **which Meta app the URLs are registered under**, a submission-time
fact the code cannot read. Keying to the wrong one refuses 100% of Meta's
requests and fails App Review, with no symptom beyond a warning line.

Both are therefore candidates, preferred first, and a request is accepted if it
verifies against either — the ordinary key-rotation posture. Every candidate is
a secret we hold, each is checked by a full constant-time comparison, and no
secret configured at all still refuses everything.

**When you register the URLs (step 4), note which app you used.**

## Signature verification

`meta_callbacks.parse_signed_request` verifies HMAC-SHA256 over the **raw
base64 payload string**, not the re-encoded JSON — re-serialising would produce
different bytes for the same logical payload and break honest requests.

Properties, each pinned by a test that was confirmed to fail when the property
is removed:

- **Fails closed.** No configured secret refuses everything rather than
  accepting everything — asserted on both callbacks, and directly on the
  primitive, since the route can no longer reach the primitive's own guard.
- **The algorithm is checked against a constant**, never dispatched from the
  payload's own field — otherwise a correctly signed request declaring
  `"algorithm": "none"` would be honoured.
- **`hmac.compare_digest`**, not `==`.
- **One refusal for every failure mode.** A caller never learns whether the
  deployment holds a secret or how close their forgery was.

## Known gaps, filed rather than hidden

1. **No test executes SQL.** The suite builds an engine-less app, so statement
   bodies never run — which is how a `channel = 'meta'` value violating
   `ck_audit_channel` survived the first draft. An integration test against the
   test database is the fix, and the highest-value follow-up here.
2. **The tenant-free door** described under the RLS bound. Until it exists,
   these endpoints cannot reliably resolve a subject on a tenant-scoped role.
3. **`_b64url_decode` duplicates `google_oidc.py`.** Extracting a shared helper
   means editing another signature-verification path, which does not belong in
   the same change as this one.

## See also

- [`meta-app-review.md`](meta-app-review.md) — the submission runbook (#1203)
- `src/services/target/meta_callbacks.py` — verification and effects
- `src/api/routes/meta.py` — the routes
- `tests/src/api/test_meta_callbacks.py` — the signature proofs
