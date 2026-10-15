# Meta callback endpoints — deauthorize and data deletion

**This file covers the endpoints themselves**: what they do, what they deliberately
do not, and the bounds they run under. Submitting the app for review is a separate
process.

Both endpoints know about the [Instagram archive](instagram-archive-review.md). The
archive is off by default and runs only when activated, so until then what changes
is limited to what the key ring and the verified identity mappings switch on; each
section below says which.

## The two endpoints

Both live under `/webhooks/meta` and both verify Meta's `signed_request` before
doing anything. **They are different verbs and are deliberately not shared code
paths** — Meta treats deauthorize and deletion as different events, and
conflating them would destroy tenant data on a mere disconnect.

| | Deauthorize | Data deletion |
|---|---|---|
| URL | `POST /webhooks/meta/deauthorize` | `POST /webhooks/meta/data-deletion` |
| Fires when | a person removes the app | a person asks Meta to delete their data |
| Effect | the matching accounts' current in-scope credentials are marked `revoked`, and an archive's current generation is fenced | a **receipt** is recorded |
| Revokes the publishing credential | **yes** | **no** |
| Deletes data | **no** | **not synchronously.** With a durable receipt the worker deletes, afterwards, what the archive imported for the receipt's frozen scope, and nothing else |
| Response | `{"status": "ok"}` | `{"url": ..., "confirmation_code": ...}` |

A third, read-only door backs the returned URL:
`GET /webhooks/meta/data-deletion/status?code=<code>`. It needs no session and never
caches (`Cache-Control: no-store`). It answers `in_progress` or `completed` for a
durable receipt's code, `400` for a code of no known shape and `404` for a
well-formed one nothing issued. A 16-hex code, which a deployment without the key ring
hands out, is answered `received` without touching the database.

A deletion request does not revoke the publishing credential; deauthorize does.

## Why deletion is deferred-with-receipt

**The shape is Meta's own.** The callback's response contract is a `url` plus a
`confirmation_code` precisely so completion can be asynchronous. Returning a
receipt is what the integration specifies, not a way around it.

Three reasons it must not delete inline, in increasing order of weight:

1. **The subject is not an account.** Meta sends an app-scoped person id.
   `user_identities.provider` is CHECK-constrained to `('telegram','google')`, so no
   table of people holds a Meta person, and `ig_accounts.provider_account_ref` names an
   **account** rather than a person (sometimes a provisional `manual:<handle>` that no
   Meta id will ever equal). The only proof of which account an app-scoped id belongs
   to is the OAuth exchange itself, which reads `/me` with the new token and says both.
   `meta_identity_mappings` (`meta_identity_mappings`) records that proof, bound to the credential and the
   authorization it was verified for, where the archive is live and, for older
   credentials, once reconciliation backfills it. Anywhere else the subject cannot be
   tied to anything.
2. **The blast radius is not the requester's to spend.** An Instagram account
   lives inside a workspace that may hold other members' content. Somebody
   disconnecting an integration has no authority to erase it.
3. **The product already has the right door, and it is deliberately not
   automatic.** `offboard_workspace` is owner-only, demands an explicit
   `confirm`, and runs a 30-day grace window before anything is irreversible.
   An unauthenticated external caller must not reach a **stronger** deletion
   than the owner's own confirmed one.

So the receipt is the deliverable. What the worker completes behind it is scoped to
what the archive imported from the authorized account (below); removing a workspace
stays the owner's confirmed act through the existing door.

## What a data-deletion request does

1. **Verify.** The `signed_request` must verify against one of the two app secrets
   ([below](#the-app-secret-is-not-one-setting)). The setting that verified it names
   the app identity.
2. **No key ring** (`ARCHIVE_HMAC_KEYS` unset or malformed): the plain receipt, kept
   on purpose. The code is 16 hex characters derived from the app
   secret and the subject, logged and not stored, and the status page says only that
   the request was received. Nothing else happens.
3. **With the key ring**, the subject must be 1–64 digits (else `400`), and
   `fn_meta_deletion_receive` (`Meta deletion receipt functions`), a tenant-free SECURITY DEFINER door, takes over:
   - **Fingerprint.** The receipt stores a keyed digest of the signed payload segment,
     one per ring version, never the payload, the subject or a secret. An exact
     retry matches it under any version and is the same receipt behind a new
     capability, with no new cleanup.
   - **Scope.** A new request finds every workspace holding a verified mapping for the
     (app identity, subject), and in each, under the sources lock, classifies each
     mapping's authorization against the request's `issued_at` with a 300-second skew.
     It is `in_scope` when the authorization certainly predates the request, `excluded`
     (never frozen) when it certainly follows, and `held` otherwise. With no usable
     `issued_at`, a mapping is in scope only when it is its account's one mapping for
     the subject and no earlier receipt scoped the account. An account with more than
     one mapping (a reauthorization) holds every one, the older too, and a later episode
     holds as well: nothing says which grant the request reached, and a boundary never
     expands to a newer grant for want of evidence. Order evidence from beyond the skew,
     judged by the database's clock, is no evidence.
   - **Freeze.** In-scope and held scopes freeze the non-deleted generations of that
     authorization on that origin's archive source. A grant made later never joins the
     scope, and a newer authorization is not deleted by an old callback.
   - **State.** The receipt is `pending`, or `completed` at once when nothing is in
     scope, or `needs_review` (below).
4. **The answer** carries a fresh random 128-bit capability. The database keeps only
   its SHA-256 digest, and the status URL is `…/status?code=<capability>`. A request
   made after a reconnect is a new receipt, never a replay of the old.
5. **The cleanup is the worker's.** The clock's `archive_lifecycle_sweep` (every 60
   seconds, not gated on the archive's activation, because a deletion closes things)
   marks scopes of removed workspaces `workspace_purged` and mints at most 25
   `meta_deletion_cleanup` jobs, one per receipt and workspace with due work. Each job
   runs four steps, each its own tenant transaction, never one across a provider call:
   - **A. Fence.** The frozen generations go `deletion_pending` (a held scope's go
     `review_hold`: unavailable, not destroyed), the live stories bound to them are
     flagged `cancel_requested` and lose their card buttons, and a sync checkpoint
     naming them is cleared. The publishing credential is not touched.
   - **B. Drain transit.** Every admitted upload is looked up, settled and destroyed,
     and a lookup must then answer absent before it is recorded destroyed. A run takes
     at most 50 uploads per source, those not yet past the hour below first and then
     the oldest, so uploads the provider never accounts for cannot hide a later one it
     can settle; the rest are still owed and the next run takes them. An upload still
     unsettled after one hour on the database clock sends the receipt to review
     (`transit_unresolved`). Nothing is ever uploaded again. With no transit store
     the worker asks the provider nothing: a scope that holds no unresolved upload
     completes, and one that does waits, then goes to review after the hour.
   - **C. Redact**, all or nothing, through `fn_archive_redact_generation_records`
     (`archive lifecycle cleanup`): provenance and artifact records, then the bound stories' queued card
     payloads (once those stories are terminal and nothing is owed delivery), then
     the observations and shared item fields.
   - **D. Recount and report.** Every category of the completion predicate is counted
     from the database. Only a zero in all of them completes the scope; a successful
     call completes nothing. The report door enforces the structural part itself: it
     refuses a completion, by name, unless every generation the scope froze is deleted
     and no upload of them is unresolved.
6. A story parked for review is left to a human, and the receipt waits (`intent_live`).

## Receipt states and the public status

| Receipt state | Public status |
|---|---|
| `pending` | `in_progress` |
| `needs_review` | `in_progress` |
| `completed` | `completed` |

The public status says nothing finer: a receipt held for review is
still `in_progress`. A receipt is `needs_review` for one of these reasons:

- `ordering_ambiguous`: a scope is `held` because the request's order against a grant
  is within the skew or missing. Its generations stay unavailable and undeleted.
- `app_unrecognized`: the app identity is not `instagram:<digits>`. A callback signed
  with the legacy Facebook secret is `facebook:legacy` and an unset app id is
  `instagram:unconfigured`; neither is ever mapped to an account.
- `transit_unresolved`: an admitted upload stayed unsettled past the one-hour horizon.
  The cleanup keeps retrying it hourly, and the receipt completes by itself once that
  upload is settled and destroyed and every scope is done. No other reason does.
- `mapping_incomplete` is in the schema's list and is not produced.

**No surface resolves a held scope or any other `needs_review` reason**: the receipt
waits. Such a surface, and a privacy review of what a deletion keeps, are activation
prerequisites.

A scope's own states are `pending`, `cleaning`, `held`, `completed` and
`workspace_purged`.

## Deauthorize in detail

The subject is matched through verified identity mappings: the (app identity, subject)
a same-token OAuth exchange proved, never `provider_account_ref`
(`fn_meta_subject_mappings`, `meta_identity_mappings`). Each mapped workspace runs in its own transaction
under its tenant and the `system` actor, and each account's current authorization is
classified the way a deletion classifies it. Only an in-scope current authorization is
revoked:

- an archive whose current generation it is is fenced (the generation superseded, the
  credential revoked, the epoch bumped once);
- any other account's credential is revoked directly.

A newer or ambiguous authorization is left alone. Nothing is deleted.

**For an account no mapping names, the account-reference lookup applies.** No mapping
is recorded while the archive is not live, so an account connected then has none, and
the legacy Facebook app identity is never mapped. The account-reference lookup
(`resolve_ig_accounts`, then `revoke_for_accounts`) runs for every request, and skips
only the (workspace, account) pairs a mapping names for the subject, whatever the
mapped path decided about them (revoked, held, or left alone as newer). A subject
mapped in one workspace therefore does not cost another workspace's unmapped account
its revocation, and nothing is revoked twice. The route answers 200 even when nothing
matched: Meta retries non-2xx and there is nothing to retry.

## Cascade scope — what is and is not touched

**Deauthorize** updates `oauth_credentials.state` to `revoked`, scoped on the
`(ig_account_id, workspace_id)` **pair** (the fallback path) or on a mapped
authorization's own credential. It never touches the account row, its media, its
posting history, its workspace, or any other member's content. It fences an archive's
current generation and deletes nothing.

**Data deletion without the key ring** writes nothing. It logs the request and returns
the receipt.

**Data deletion with the key ring** writes only the receipt: a fingerprint, the scopes
and the frozen generation ids, and the capability digest. The deletion is the worker's,
over the frozen generations alone: the archive's observations and provider-derived item
fields, its provenance and artifact records, its staged transit bytes, and the queued
card payloads of its stories. **It never deletes a workspace, another origin's content,
Drive content, or the account's grant**, and a newer authorization's generation is not
in the frozen set, so a reconnect's data survives an old request.

Media already delivered to a Telegram chat or a browser cannot be retracted by this
system. The status page says so in both of its states (copies already delivered to a
Telegram chat, such as approval cards, cannot be withdrawn by Storydump), and
"completed" speaks only of what Storydump itself holds: content imported from the
Instagram accounts the person authorized. A remote erasure is never counted as done.

**The routes issue no SQL at all, and `meta_callbacks` issues no SQL `DELETE`.** AST
tests (`tests/src/api/test_meta_callbacks.py`) pin both, and that the module's one
credential statement is an `UPDATE`. The rows that are deleted are deleted in the
worker, by the redaction door (`archive lifecycle cleanup`) and the catalog's redaction.

## What a completed deletion leaves

This is the retention allowlist as the code leaves it; activation requires a
privacy review of it.

| Kept | What it holds | Why |
|---|---|---|
| The receipt, its scopes, frozen generation ids and capability digests | app identity, a keyed fingerprint, `issued_at`, receive and completion times, state and reason; per scope the workspace, account, credential and authorization ids and a counts-only result of at most 4 KB; the SHA-256 of each capability | the proof a request was handled. No foreign key to `workspaces`: a receipt outlives the workspace it scoped |
| Generation rows | ids, the source and origin account, `app_id`, `subject_id` (the account's own id), `authorized_at`, `lifecycle = deleted` | keep an old job from resurrecting a deleted generation, and order later requests |
| Identity mappings | app identity, the app-scoped subject, the account id, method, times, `is_current` | evidence for ordering and for deauthorize; they go with the credential |
| Redacted item rows | the item's id, source and ledger fields such as `last_posted_at`; every provider-derived field cleared or replaced by `redacted:<id>`; `state = removed` | the story's history and the repost locks point at the item |
| Tombstones | the item id, the ring's key version, an HMAC of the parent media id, the observation revision | a later authorized read can restore the same item, so a deleted post cannot be reposted as new |
| The story's own ledger | the intent, lock and audit rows and `provider_operations`, with the story's `ig_container_id` and `ig_media_id`; the bound outbox rows with their payloads replaced by `{"v": 1, "redacted": true, "kind": …}` | identifiers and states of Storydump's own posting, and the repost lock |

Deleted: observations with their validation and eligibility caches, provenance and
artifact records, staged transit bytes and the queued card payloads. Never stored: the
raw signed payload, the subject (in a receipt) or any secret, and, anywhere, a caption,
a credited handle, a media or thumbnail URL or a paging cursor.

## Tenant-free lookup

A Meta callback names no workspace. Direct reads of `ig_accounts`, `oauth_credentials`
and `meta_identity_mappings` under the runtime roles are tenant-scoped, so the callbacks
cannot discover ownership through an ordinary unscoped table query. Narrow SECURITY
DEFINER doors, owned by `svc_maintenance` and given only the EXECUTE grants they need,
answer instead:

- **`fleet health functions`'s `fn_meta_accounts_for_ref`**, for `svc_ingress`: the account-reference lookup
  `meta_callbacks.resolve_ig_accounts` calls. It is the deauthorize fallback.
- **`meta_identity_mappings`'s `fn_meta_subject_mappings`**, for `svc_ingress`: the verified mappings of an
  (app identity, subject), across workspaces.
- **`Meta deletion receipt functions`'s `fn_meta_deletion_receive` and `fn_meta_deletion_status`**, for
  `svc_ingress`: record a request and read a capability's status.
- **`Meta deletion receipt functions`'s `fn_meta_receipt_scope` and `fn_meta_receipt_progress`**, for `svc_worker`: a
  claimed tenant's scopes of one receipt, and the report of one scope's progress.

The four receipt tables grant the runtime roles nothing: only these doors reach them.
Revocation runs per workspace with an explicit tenant and actor.

The account-reference lookup compares an app-scoped person subject with a stored
account reference, so a zero from it is not proof that no grant exists. That is why deauthorize
prefers a verified mapping and falls back to it only for the accounts none names.
Signature verification alone never authorizes deleting unrelated workspace data.

## The app secret is not one setting

`settings` carries two — `INSTAGRAM_APP_SECRET` (annotated *preferred*) and
`FACEBOOK_APP_SECRET` (the older Facebook-app secret; `settings.py`'s note says the
callbacks verify against `INSTAGRAM_APP_SECRET`, then this). Which one signs these callbacks is
decided by **which Meta app the URLs are registered under**, a submission-time
fact the code cannot read. Keying to the wrong one refuses 100% of Meta's
requests and fails App Review, with no symptom beyond a warning line.

Both are therefore candidates, preferred first, and a request is accepted if it
verifies against either — the ordinary key-rotation posture. Every candidate is
a secret we hold, each is checked by a full constant-time comparison, and no
secret configured at all still refuses everything.

The setting that verified a request also names the **app identity** that mappings and
receipts are keyed to: `instagram:<INSTAGRAM_APP_ID>` for the Instagram secret,
`instagram:unconfigured` while that id is unset or is not digits, and `facebook:legacy`
for the legacy secret. It names the app, never the secret's value, so a secret's
rotation maps to the same app, and the two apps cannot alias on a raw subject.

**Give the two apps different secrets.** If both settings hold the same value, the
signature cannot say which app signed. Such a request is ambiguous and is named for the
legacy app, so no mapping answers to it and a deletion request is held for review
(`needs_review`, `app_unrecognized`); a warning is logged for each one. Naming the
preferred setting instead would read a request the legacy Facebook app signed as
Instagram's.

**When you register the callback URLs, note which app you used.**

## Signature verification

`meta_callbacks.verify_signed_request` verifies HMAC-SHA256 over the **raw
base64 payload string**, not the re-encoded JSON — re-serialising would produce
different bytes for the same logical payload and break honest requests.

Properties, each pinned by a test that fails when the property
is removed:

- **Fails closed.** No configured secret refuses everything rather than
  accepting everything — asserted on both callbacks, and directly on the
  primitive, since the route cannot reach the primitive's own guard.
- **The algorithm is checked against a constant**, never dispatched from the
  payload's own field — otherwise a correctly signed request declaring
  `"algorithm": "none"` would be honoured.
- **`hmac.compare_digest`**, not `==`.
- **One refusal for every failure mode.** A caller never learns whether the
  deployment holds a secret or how close their forgery was.

## The key ring

Durable receipts and the worker's cleanup need `ARCHIVE_HMAC_KEYS`. Its shape, how to
rotate it without splitting a request into two receipts, and what happens without it are
in [`instagram-archive-review.md`](instagram-archive-review.md-the-key-ring).
Without a transit store the worker still runs `meta_deletion_cleanup`: it asks the
provider nothing, so it completes only scopes that hold no unresolved upload.

## Known gaps

1. **The route-level tests run no SQL.** `tests/src/api/test_meta_callbacks.py` builds
   an engine-less app, so statement bodies never run there. The receipt,
   mapping, deauthorize and cleanup SQL does run, under the runtime roles on a
   throwaway database, in `tests/scripts/test_meta_deletion_receipts_gate.py`,
   `test_meta_identity_mapping_gate.py`, `test_meta_receipt_doors_gate.py`,
   `test_archive_lifecycle_gate.py`, `test_archive_deletion_cleanup_gate.py` and
   `test_instagram_source_gate.py` (scene 6).
2. **Nothing resolves `needs_review` or a held scope**, and activation
   requires a privacy review of the retention allowlist above.
3. **Which identifier Meta's callbacks carry is assumed.** The code takes `/me`'s `id`
   to be the app-scoped id named as `user_id`. Confirming that against Meta's test
   callback, and which Meta app the URLs are registered under, are owed.
4. **The transit store's real answers are unverified offline** (Cloudinary's Admin API
   fields, 404 as the only absence signal, destroy's `not found`), and the drain relies
   on them.
5. **`_b64url_decode` duplicates `google_oidc.py`.** Extracting a shared helper
   means editing another signature-verification path.

## See also

- [`instagram-archive-review.md`](instagram-archive-review.md) — testing, prerequisites, activation, permissions, the key ring and limits
- [`instagram-source-isolated-gate.md`](instagram-source-isolated-gate.md) — the full-composition gate, scene 6 of which drives these endpoints
- `src/services/target/meta_callbacks.py` — verification, fingerprints and the receipt doors' callers
- `src/api/routes/meta.py` — the routes
- `src/services/target/archive_lifecycle.py` — deauthorize's revocation; `archive_deletion.py` — the cleanup job
- `tests/src/api/test_meta_callbacks.py` — the signature proofs
