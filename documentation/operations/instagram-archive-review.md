# Instagram archive: testing and activation guide

The Instagram archive lets a workspace reuse a connected professional account's own
past feed posts as a posting source, through the normal approval and publishing
workflow. It is **off by default and runs only when activated**. This guide covers:
- how to test it locally with fakes;
- what must be true before it is turned on;
- how to turn it on;
- the Meta permissions it needs;
- the key ring;
- its limits.

## How it switches on

The archive runs only in a process where `archive_activation.resolve` finds both of
these:
- a valid operator activation record, bound to that service's release and to the
  mix-conversion run;
- a whole `ARCHIVE_HMAC_KEYS` ring.

Without both, each composition root wires nothing and logs
`instagram archive: inactive (<reason>)`. When armed, it logs
`instagram archive: armed (run=<run id>)`. There is no registry entry or flag to turn
on: activation is a deliberate operator step, described in step 3.

## 1. Test locally with fakes

**Run the full-composition gate.** It runs the real services over fake providers and a
throwaway PostgreSQL, from connecting an account to a posted story and on to the
deletion of what the archive imported. The recipe is in
[`instagram-source-isolated-gate.md`](instagram-source-isolated-gate.md):
- a Linux machine;
- Python 3.10;
- PostgreSQL 15 in UTC on loopback port 65433;
- a test-only `ENCRYPTION_KEY`;
- a checkout with no `.env`.

The gate itself is `tests/scripts/test_instagram_source_gate.py`, run as
`python -m pytest -p tests.isolation_guard --isolated-provider-gate …`, with the
environment the recipe sets.

**The archive's other gates** run on a throwaway database under the runtime roles, also
over fake providers. They are in `tests/scripts/`:
- `test_archive_lifecycle_gate.py`
- `test_archive_deletion_cleanup_gate.py`
- `test_meta_deletion_receipts_gate.py`
- `test_meta_identity_mapping_gate.py`
- `test_archive_reconciliation_gate.py`

Between them, they cover these properties:
- A source resolves only its own workspace and account grant.
- Off fences every producer, queued job, in-flight page commit and continuation.
- Disconnect, credential death, source removal, reconnect and workspace offboarding
  stop archive selection and fetching.
- Meta's data-deletion subject maps, through verified identity mappings, to a
  persisted receipt and status.

**The onboarding budgets** are checked against the committed manifests in
`tests/fixtures/instagram_onboarding/`. These are independent arithmetic expectations.
- **The fixtures:**
  - The ordinary fixture has three slides per carousel.
  - The carousel-heavy fixture cycles through 2, 3, 5, 10 and 20 slides, with mixed
    video, one empty carousel and one truncated page.
- **The sizes:** the defaults are 100, 500 and 2,000 posts. The 10,000-post case needs
  `RUN_ONBOARDING_10K=1`.
- **The ceilings** are counted in posts: `3N+64` Graph requests and `N+128` turns.
  Indexing makes zero CDN requests, downloads or decodes. Candidates, item exclusions
  and runway count files.
- **A disagreement** needs a review of the code and the arithmetic. A measured result
  never replaces a predetermined ceiling.

**What a green run does not show.** Every provider in these gates is a fake, so a pass
says nothing about Meta, Instagram's CDN, Cloudinary or Telegram. It satisfies none of
the prerequisites below.

**`scripts/instagram_archive_probe.py` is not a test or a readiness check.**
- Every invocation without `--help` or `-h` returns a deterministic version-1 JSON
  refusal with `refusal_code: feasibility_probe_retired` and exit code 2; `--help` or
  `-h` prints its notice and exits 0. It accepts its options without using them.
- It reads no environment or credentials, opens no database connection, issues no
  provider requests and fetches no bytes.
- Archive reads need the current source, On state, grant and generation, with
  authoritative admission and return fences, all taken under database row locks. A
  read-only probe cannot take those locks.

## 2. Prerequisites

Each item needs real evidence before activation. The fake-provider gates and an
existing app review satisfy none of them. Any live retrieval or publication needs its
own explicit approval, and the scheduler is never run to verify publishing.

1. **Meta access for this exact use.** The use is feed retrieval and reuse through a
   managed Stories publishing workflow; see step 4. An existing App Review, or an
   allowlisted account's retrieval, is not proof of general import permission.
2. **The callback identity.** The Meta app the callback URLs are registered under
   decides the app identity that verified mappings are keyed to. The code takes
   `/me`'s `id` to be the app-scoped id Meta's callbacks name as `user_id`. Confirm
   both against Meta's test callback. The endpoints are described in
   [`meta-callback-endpoints.md`](meta-callback-endpoints.md).
3. **The Graph version.** The archive uses the application's shared Graph version:
   `META_GRAPH_VERSION`, default `v21.0` (`instagram_graph.DEFAULT_GRAPH_VERSION`).
   Publishing uses the same one. Verify it against Meta's current documentation.
   Upgrading it requires publishing evidence on an owned account first.
4. **A live read-only field and pagination check** on the selected account:
   - **Fields:** which are available (`media_url`, `like_count`, `comments_count`,
     `permalink`, `owner`, and `caption` under Instagram Login).
   - **Pagination:** complete time-bound and same-second pagination. Where only an
     ambiguous time-based next link is returned for a repeated timestamp, the adapter
     refuses instead of skipping unseen posts.
   - **`until` without `since`:** whether the edge accepts it, which the walk's
     empty-window probe depends on. The launch-date floor holds either way.
5. **The carousel edge.** A read-only probe that `children` returns all slides in one
   page at `limit=20`.
6. **The mixed-video validator.** Run the real validator and runtime over a mixed
   archive, including maximum bitrate.
   - The validator pins `ffprobe` and `ffmpeg` to 7.1. The deployed image supplies them
     through `nixpacks.toml`.
   - Image-only coverage cannot satisfy this item.
7. **The transit store's real answers**, which the transit drain relies on:
   Cloudinary's Admin API fields, 404 as the only absence signal, destroy's
   `not found`, and the removal of the derived frame.
8. **Privacy review of what a deletion keeps.** The retention allowlist is in
   [`meta-callback-endpoints.md`](meta-callback-endpoints.md#what-a-completed-deletion-leaves).
   There is no surface that resolves a deletion receipt held in `needs_review`; such a
   receipt waits.
9. **Deployment and drain evidence** for both operator records, the mix-conversion
   record and the activation record: release SHAs, old-process termination and
   exhausted leases.
10. **An acceptance record**, made under explicit approval. It labels fake tests,
    read-only live retrieval and live publishing separately.

## 3. Turn it on

Activation is two operator record files and the key ring. Set all three on both the
API and the worker. Each record is a JSON file of at most 64 KiB, readable by the
process, at the path its variable names.

**a. The mix-conversion record**, at `SOURCE_MIX_CONVERSION_RECORD`:

```json
{
  "v": 1,
  "enabled": true,
  "run_id": "<1-128 of A-Z a-z 0-9 _ . ->",
  "api": {"sha": "<API release, 40 hex>", "source_mix_protocol_max": 2, "expanded_schema": true, "both_protocols": true},
  "worker": {"sha": "<worker release, 40 hex>", "source_mix_protocol_max": 2, "expanded_schema": true, "both_protocols": true},
  "lease_disposition": "exhausted",
  "termination_evidence": "<text>",
  "leases_evidence": "<text>",
  "rollback_releases": [{"sha": "<40 hex>", "source_mix_protocol_max": 2, "expanded_schema": true, "both_protocols": true}]
}
```

- `lease_disposition` is `exhausted` or `fenced`.
- Each evidence text is 1–2,048 characters.
- `rollback_releases` names at least one release a rollback may return to. Each one
  carries the same protocol fields.

**b. The archive activation record**, at `INSTAGRAM_ARCHIVE_ACTIVATION_RECORD`:

```json
{
  "v": 1,
  "enabled": true,
  "run_id": "<1-128 of A-Z a-z 0-9 _ . ->",
  "api": {"sha": "<API release, 40 hex>"},
  "worker": {"sha": "<worker release, 40 hex>"},
  "conversion_run_digest": "<SHA-256 of the mix-conversion record file, 64 hex>",
  "evidence": {
    "meta_access": "<text>",
    "graph_version": "<text>",
    "video_validation": "<text>",
    "deployment_drain": "<text>",
    "callback_identity": "<text>"
  },
  "rollback_releases": [{"sha": "<40 hex>"}]
}
```

- Each evidence string attests one prerequisite in 1–2,048 characters. The code checks
  only that it is present, and never stores, returns or logs it, because it may carry
  private references. Keep it out of the repository.
- `conversion_run_digest` is the SHA-256 of the mix-conversion record file's bytes, as
  `sha256sum <file>` prints it.

**c. The key ring**, `ARCHIVE_HMAC_KEYS` (step 5).

**Check each service.** Each process checks its own release: `api.sha` or `worker.sha`
must equal that service's `RAILWAY_GIT_COMMIT_SHA`. Then read the compose line in
each service's log:
- `instagram archive: armed (run=<run id>)` means the service is armed;
- `instagram archive: inactive (<reason>)` means it is not. The reason is the first
  check that failed, in this order: `no_record`, `record_invalid`, `release_mismatch`,
  `conversion_missing`, `conversion_mismatch`, `keys_missing`, `keys_invalid`.

**Once armed:**
- History is the History choice of the OAuth connect. It defaults On for a new
  connection, and an existing account stays Off until enabled.
- An Off archive makes no history reads.
- The source-mediated status and the explicit preview reach a provider only in an
  armed process. Elsewhere, every archive thumbnail answers the placeholder's 404.

## 4. Meta permissions and review

**Permissions.** Use the Instagram Login permissions already appropriate to each
operation:
- `instagram_business_basic` for the documented media read;
- the existing publishing permission for optional API publishing.

The archive needs no Facebook Page, no Facebook Login grant and no insights
permission.

**Describe the actual use case.** A consenting professional account selects its own
past feed posts for reuse through a managed Story publishing workflow.
- Storydump reads feed metadata.
- It resolves a fresh media URL when the bytes are needed for an approval card or for
  temporary publishing transit.
- It does not offer an archive mirror, permanent media copies, or a general Instagram
  download or backup product.

**Obtain acceptance for that flow.** Meta's developer policy §6 requires prior
permission for display, import and backup uses. Calling the product a scheduler does
not establish an exemption. Whether Meta accepts the archive's reads under the
permissions the product already holds is a question for that acceptance.

**Keep the publishing review independent.** The App Review for publishing covers the
publishing permissions only. A publishing submission must not borrow an archive
approval, and the archive must not borrow the publishing one.

**Prepare truthful evidence of the implemented flow:**
- Instagram Login consent and the connected account;
- source controls and eligibility explanations;
- review cards and the optional publishing decision;
- disconnect and reconnect;
- the data-deletion confirmation.

Describe metadata retention and temporary-byte cleanup precisely, as the limits below
and the retention allowlist do. Never record or submit a screen claiming a feature
works that has not been walked on a real account.

**Register the deauthorize and data-deletion callback URLs**
([`meta-callback-endpoints.md`](meta-callback-endpoints.md)), and note which Meta app
they are registered under.

## 5. The key ring

`ARCHIVE_HMAC_KEYS` is one variable, read by `archive_activation.load_keys` in both the
API and the worker. Set it, with the activation record, on both services.

**Shape.** `<version>:<base64url key>`, comma-separated, for example
`1:<key>,2:<key>`.

- A version is a positive integer of at most nine digits, and each appears once.
- A key is URL-safe base64 of **at least 32 bytes**: the `-` and `_` alphabet, with
  `=` padding optional but exactly right if present. Generate a fresh one with:
  `python -c "import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"`.
- **The highest version is current.** It keys new tombstones and the fingerprint a new
  receipt stores. Every other version is kept to recognize what was made under it.
- One malformed entry refuses the whole ring (`keys_invalid`). An unset or empty value
  is `keys_missing`. There is never a partial ring.

**What the ring keys.**

- *Deletion receipts, in the API.*
  - A request's fingerprint is an HMAC-SHA256 under the key, over a domain prefix, the
    app identity and the signed payload segment. It is computed once per ring version,
    current first.
  - The receipt door stores the current one, and matches a replay against any version
    the instance presents. At most 16 versions count; a longer ring is cut to the
    current version and the newest others.
  - No payload, subject or secret is stored.
- *Tombstones, in the worker.*
  - A deleted post's item identity is kept as an HMAC of its parent media id under the
    current version.
  - Landing an observation computes the digest under every version a tombstone of that
    workspace uses.
  - A version missing from the ring refuses the landing
    (`archive_tombstone_key_unavailable`) rather than quietly minting a new item.

**Rotating.**

1. Generate a new key and give it the next version number. Add it to the ring and
   **keep every older version**. Never remove, renumber or roll back a version while
   rows hashed with it live: a tombstone row naming it, or a receipt still inside its
   replay window. There is no re-key job, so a retired tombstone version stays.
2. Put the same ring on the API and the worker in one release.
3. Mind the split. The current version is simply the highest, so an instance holds the
   new key as current the moment it loads the ring; there is no separate promote step.
   - The risk: a retry can reach an instance still on the older ring. That instance
     cannot match a receipt another instance first stored under the new version, so it
     records a second receipt for the same request. That breaks the rule that an exact
     retry replays one receipt.
   - The reverse order is safe, because the newer ring still holds the older version.
   - Keep the window in which both rings answer as short as the deployment allows, and
     do not start it while a Meta deletion request is in flight.

**With no keys.**

- The API's data-deletion route answers with the plain receipt: no error and no 503.
  The receipt is the 16-hex code derived from the app secret, logged and not stored,
  and its status page says only that the request was received.
- The worker parks `meta_deletion_cleanup`, which needs the ring.
- The archive cannot arm, with or without a record.
- A ring that does not parse is not an error at boot. It loads as none, and the only
  trace is the compose line when a valid record is also present. A record problem is
  reported before a key problem.

The ring alone, with no activation record, leaves the archive off, but makes the
data-deletion receipts durable, and the worker runs their cleanup. Without a transit
store (`CLOUDINARY_*`), the cleanup asks the provider nothing and completes only scopes
that hold no unresolved upload.

## 6. Limits

**What to tell an owner.**
- **The connection.** The workflow uses the account's existing Instagram Login
  connection. It needs no Facebook Page and no Facebook Login grant. Only the connected
  professional account's own feed media is in scope.
- **The mix.** An owner chooses how much of the posting mix comes from eligible past
  posts. The existing approval, manual-publishing and API-publishing workflow applies.
- **The default rules:**
  - Default shares are equal across enabled sources: 100% / source count.
  - Every supported post that passes the configured age, attribution and repost rules
    is eligible; engagement does not rank posts.
  - The initial minimum age is max(90 days, repost TTL + 1 day).
  - Credited and unknown attribution are separate opt-ins, and the default excludes
    both.
  - A change in source membership resets the mix to equal.
  - All sources may be Off.
- **Destinations.** Origin-only destination use is a v1 policy on a separate assignment
  relation. Saved policy and media identity survive later settings or cardinality
  expansion.

**Set accurate expectations.**
- **Coverage.** Meta exposes at most the newest 10,000 feed posts. Stories are not part
  of the archive, and a scan is not a full historical backup.
- **Carousels.** Each carousel slide is its own item, identified by that slide, with its
  own repost lock. The source policy defaults to all slides, and can select only the
  first slide. Slides are ordinary items in the rotation.
- **Restricted media.** Copyright or licensed-audio restrictions can omit a media URL,
  even on the account's own post. Such an item is unavailable for reuse.
- **Likes and comments** can be unavailable. Missing values do not mean zero.
- **Videos.** Candidate videos still need duration and codec checks before they become
  eligible. The validator does not mark a video `supported`: a video that passes every
  probe ends `validation_pending` with reason `needs_spec_evidence`.
- **Captions and attribution.** Instagram Login can return a caption, but not for every
  post or grant. Credit detection cannot be promised, and attribution can be unknown.
  Excluding unknown attribution can leave an archive with no eligible items.
- **Duplicates.** Stable post IDs deduplicate retries and rescans. They do not match
  image bytes across Drive and Instagram, or separately uploaded copies of the same
  photo.

**What the archive reads.** Every read is a
`GET https://graph.instagram.com/<version>/...`, with the following properties:
- it carries the connected account's own Instagram Login token in an `Authorization`
  header;
- it goes through the egress floor, without redirects;
- it uses the application's shared Graph version. The adapter is
  `src/services/target/instagram_archive_adapter.py`.

| Request | Fields | When |
|---|---|---|
| `<account id>/media` | `id,media_type,media_url,timestamp,permalink,like_count,comments_count`, with `since`/`until` (Unix seconds, 90-day windows), `limit` (25 as composed) and an `after` cursor held in memory only | the metadata-only walk |
| `<media id>` | the listing fields plus `owner`; `caption` too when enriching a parent (a refusal of the field is retried without it and the post is `unknown`) | re-reading an interrupted parent; enrichment |
| `me` | `id,user_id` | only when a post's `owner.id` is not the stored account id, to prove the mapping with the same token |
| `<media id>/children` | `id,media_type,media_url`, `limit=20` | enriching or selecting a carousel; the walk itself defers it |
| `<media id>` | `id,owner,media_type,media_url` | a selected post, when its bytes are needed for a card or for publishing transit |
| `<media id>` | `id,owner,media_type,media_url,thumbnail_url` (a child: `id,media_type,media_url,thumbnail_url`) | an explicit preview: at most two lookups |

Nothing else is read: no stories, comments, insights, follower data or messaging, and
no Facebook Login permission. A turn makes at most 20 Graph requests and observes at
most 100 parents.

**Indexing is metadata-only.** The sync uses the adapter in its `metadata_only` mode,
and makes **zero CDN requests, downloads or decodes**.
- The walk does not read carousel children. It records the post as available with
  `incomplete_observation`, until a helper turn enriches that parent by id, which is
  still Graph-only.
- A listing's `media_url` is read only to learn that a media file exists. It is never
  retained: an observation carries no URL, caption, handle or cursor.

**What the index keeps per post:**
- the parent media id;
- the returned slide count, and whether it is complete;
- an internal grouping UUID;
- the original UTC time;
- the permalink, only when it is an `instagram.com` post URL;
- the like and comment counts (nullable);
- the attribution state (`credited`, `not_detected` or `unknown`) with the detector's
  version; the caption itself is never stored;
- the post's availability and its reason.

A separate asset row per file keeps the slide id, position, kind, availability, item
link, generation and revision, and its validation evidence.

**Coverage rules for a walk:**
- A children response with more pages keeps its returned slides, records incomplete
  coverage, and makes no second request.
- Only a complete response can retire a missing slide.
- A listing capped or windowed this way never proves a post was deleted
  (`checkpoint_whole` is false).

**Bytes are fetched only for a selected item or an explicit preview.**

- **A selected item.** Its bytes are read twice: to prepare its approval card, and
  again for publishing transit. Each read first makes a fresh Graph lookup, and retries
  once if the CDN link proves expired.
  - The CDN request never carries the Instagram token. It goes only to
    `*.cdninstagram.com` or `*.fbcdn.net`, over HTTPS.
  - The real validator judges the bytes before a card or any publishing effect.
  - A carousel selection reads the parent and its children once, then finds the
    captured slide by id. A slide missing from a complete response goes to review as
    unavailable. A truncated response is retryable, and never substitutes another
    slide.
- **A preview** is its own bound: at most two Graph lookups and one CDN download of at
  most 2 MiB, none of them retried. A video's preview is its Graph `thumbnail_url`
  image, never the video file.
- **Nothing is mirrored.** Bytes live in memory and, for publishing, as a staged transit
  copy destroyed after the post. There is no archive-wide validation queue.

**After a Meta deletion request,** what remains is the retention allowlist in
[`meta-callback-endpoints.md`](meta-callback-endpoints.md#what-a-completed-deletion-leaves).

## Official references

- [Instagram Login setup](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/get-started)
- [Media fields and Login permissions](https://developers.facebook.com/documentation/instagram-platform/reference/instagram-media)
- [User media limits and time pagination](https://developers.facebook.com/documentation/instagram-platform/instagram-graph-api/reference/ig-user/media)
- [Graph pagination](https://developers.facebook.com/docs/graph-api/results/)
- [Instagram changelog, including its media URL changes](https://developers.facebook.com/documentation/instagram-platform/changelog)
- [Developer policy](https://developers.facebook.com/devpolicy/)
- [Graph versions and availability](https://developers.facebook.com/docs/graph-api/changelog)
