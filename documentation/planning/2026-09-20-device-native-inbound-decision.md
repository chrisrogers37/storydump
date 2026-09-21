---
title: "Device-native inbound: where a Telegram drop's bytes live, and how a system-created source enters the posting mix (decision)"
type: decision
status: ratified
owner: chris
created: 2026-09-20
tags: [decision, media-sources, telegram, google-drive, posting-mix]
links: []
---

> **Ratified 2026-09-20** by the owner, at the first design gate of the device-native-inbound
> discovery. The spec that builds on it is
> [`2026-09-20-device-native-inbound-spec.md`](2026-09-20-device-native-inbound-spec.md). The
> build's documentation step records both picks in `03-decision-record.md` as post-ratification
> rulings; until then this page is their home.

## Summary

Two junctions, weighed across seven dimensions each, with an independent second scoring.

1. **A photo or video dropped in a bound Telegram group is relayed into the team's Google Drive**,
   into a folder Storydump creates under the workspace's grant, and enters the library through the
   existing sync (option B below). The grant gains Google's per-file write scope, `drive.file`;
   each workspace reconnects once. Storydump stays a router over the customer's storage and holds
   no media of its own.
2. **A source the system creates enters the posting mix at an explicit default share of 20%**, and
   the existing explicit shares shrink proportionally (rule M1 below). For a source a person
   connects by hand in Settings, such as an iCloud album, the connect dialog shows that default as
   an editable field.

## Why it matters

The first choice fixes four things at once: whether the Google grant changes scope, where a
tenant's bytes live, how a new source enters the mix, and what every later inbound path (an iPhone
share-sheet Shortcut, web upload, MMS) builds on. The second decides whether a drop ever posts:
under today's arithmetic, two drops beside 350 files in weighted folders draw about 0.6% of slots.

Facts the weighing rests on, read from the repository on 2026-09-20:

- The media-source port is pull-only: `list_changes`, `fetch_bytes`, `probe`
  (`2026-08-02-consolidated-design-plan/01-target-architecture.md`, "Media-source port"). D37
  records "no upload/write operation" as a deliberate non-goal with the port as the extension seam.
- Bytes are fetched from the source at publish time and staged through Cloudinary as transit, then
  destroyed (FC-3.5). Both fetch seams are Drive-bound (`src/worker.py::_publish_media_fetch`,
  `_card_media_fetch`); neither the intent row nor the card's media block carries a provider.
- The workspace's Google grant is `drive.readonly` only, by the owner ruling of 2026-09-05.
  `src/services/target/google_drive_oauth.py` records why `drive.file` alone cannot list a
  pre-existing folder.
- The webhook already receives every message in a bound group. A media message goes to
  `_observe_all`, which records the sender's membership and ignores the media;
  `membership_sync.observe` already answers `unknown_identity` for an unlinked sender. The ingress
  role holds INSERT on `jobs`, `media_items`, `media_sources`, `category_post_case_mix` and
  `channel_outbox` (migration 057).
- "Sources are the groups" (owner ruling 2026-09-08): the mix is keyed on the `media_sources` row
  (071); an unweighted folder is Automatic; `category_mix.weights` splits the automatic pool by
  file count and caps it at the smallest explicit share.
- Telegram, verified against the Bot API reference: "bots can download files of up to 20MB in
  size", the download link "will be valid for at least 1 hour", and `setMessageReaction` exists
  with a fixed list of permitted emoji. A photo sent as a photo is re-encoded (1280 px long edge,
  2560 px with the client's high-quality toggle); a file is untouched.
- Google, verified against the Drive API scopes guide: `drive.file` is a non-sensitive scope,
  "create new Drive files, or modify existing files, that you open with an app or that the user
  shares with an app while using the Google Picker API or the app's file picker"; `drive` and
  `drive.readonly` are both restricted scopes. Adding `drive.file` adds no sensitive or restricted
  scope.

## Junction 1: where a drop's bytes live and how they enter

**Options.**

- **A. Telegram keeps the bytes.** A `telegram` provider; one implicit source per workspace;
  `provider_file_ref` is the file id; `fetch_bytes` calls getFile at publish and card time.
- **B. Relay into an app-created Drive folder.** The grant adds `drive.file`; a job downloads the
  drop and uploads it into a "Telegram drops" folder the app created at the root of the connecting
  account's Drive, connected as a source; the sync ingests it.
- **C. Relay into an existing connected folder.** The same job, but the drop lands in a folder the
  team already weights, through the full `drive` scope or a Picker-granted target under
  `drive.file`.
- **D. Relay into a Storydump-owned store.** A `storydump` provider over a Cloudinary library
  folder or an R2 bucket; the same store later backs web upload and the Shortcut.
- **D′. Relay into the ledger's own database.** A `media_blobs` table under RLS, bounded at 20 MB
  a row by the bot cap. Named by the independent scorer.

| Dimension | A. Telegram keeps the bytes | B. App-created Drive folder | C. Existing connected folder | D. Storydump-owned store |
|---|---|---|---|---|
| **Elegance** | Fewest parts: ingest is a row write in the delivery's own transaction, no bytes move until publish, and the card can send by file id. But the port is pull-shaped and this pushes; sync, probe and the source state machine are dead weight for it, and the product gains a second library with no curation tool. | One library, one fetch path, one job with a plain sequence. But two hops (Telegram to Drive to a sync), plus a folder the app owns, a role marker, a two-scope grant with a reconnect state, and the mix rule. | No new folder and no mix change. But within a folder the draw is never-posted-first in shuffled order, so a drop waits its random turn among that folder's fresh files. Routing adds a setting or a caption grammar; full scope is a heavy consent; the Picker is a client-side widget plus a browser-side token. | Conceptually clean, "we hold uploads", but a second storage system with its own lifecycle, a new provider, and a Library that must grow curation because no Drive UI exists for it. |
| **Existing patterns** | D37's add-a-provider cost fits on paper, but both fetch seams need a per-provider dispatch, and the ingress path would write media, which nothing does today. | Reuses the job registry and lanes, the outbox, the egress floor, the Drive token door, `get_or_create_media_source`, `rearm_after_connect`, the demand sync and `rate_counters`. | The same reuse as B. The Picker was declined for onboarding (#327) and the web has no client-side Google pattern. | Cloudinary exists only as transit with destroy-on-post and a hard-TTL sweep over the whole `ws/` prefix; a keep-forever folder inverts that. R2 is a new host and credential on the floor. |
| **Extension** | Widens two CHECKs and adds an adapter, but introduces a push concept the port lacks and a fetch dispatcher across two seams. | A write leg on the Drive adapter, one job kind, one outbox kind, an envelope version carrying granted scopes, one mix function. No new abstraction. | B's write leg plus a drop-target setting or hashtag parsing; for the Picker, a route that stores the granted folder. | A new provider and adapter, an upload endpoint (#184), a library store with retention, and category assignment in the Library. The largest surface. |
| **DRY** | No duplicate bytes, but a second definition of where content lives and a second retention model to document. | One place bytes live, one fetch path, one dedup. | As B for bytes. Two places define where things land. | Duplicates "where bytes live" and gives Cloudinary two roles with opposite lifecycles. |
| **Separation** | Telegram becomes both channel and store: the bot's identity is a storage key, and the token-bearing download URL needs the transport's redaction discipline inside a media adapter. | Ingress recognises and enqueues; the worker relays; Drive stores; the sync ingests. The product's content lands in one person's Drive (`07` §15). | As B, and worse: drops are mixed into a folder the team curates by hand, with provenance only in a file name. | Cleanest separation, at the cost of Storydump becoming a custodian. |
| **Future-proofing** | Every later push source needs its own provider and store, or A is refactored into D. Telegram's retention is undocumented and every publish re-hits getFile. Cheap to retire only if the bytes are hashed at ingest. | The relay leg serves the Shortcut, web upload and MMS later, all as "write into the drop folder". Risks: drops sit in one admin's quota; `drive.file` access is per Google account, so a reconnect under another account cannot write to the old folder and cannot list it unless it is shared. A workspace without Drive gets nothing. | Full scope carries the broadest restricted scope through every future review, with a consent text that hurts trust with paying tenants. | The most general foundation and no dependence on Google. Commits the product to storage cost, retention, an offboarding leg, and the export and erasure obligations FC-9 repriced. |
| **Plan alignment** | FC-8 holds; D37's sync-only port is contradicted in spirit; the 2026-09-05 ruling is untouched. Dilutes "sources are the groups". | D37 amended by a recorded write leg. The 2026-09-05 ruling's letter ("same scope") is amended; its verification stance holds. Reinforces "sources are the groups"; serves #294 now and #184 in part. | Full scope contradicts `07`'s least privilege and the 2026-09-05 ruling. The Picker variant reopens #327 for one folder. | Reverses D37's non-goal outright. Serves #184 and #189 fully. Strains "simple over powerful" and "design for 200". |

**D′ against the same dimensions.** Elegance: the store is a table, so tenancy, the offboarding
cascade, backups and erasure come from the schema, and no new host touches the egress floor.
Existing patterns: RLS, the grant matrix and the unit of work already cover a new tenant table;
nothing streams large rows today. Extension: a migration, a provider, an upload endpoint, a stream
that reads a row. DRY: nothing duplicated. Separation: as D. Future-proofing: D's custodian
commitments, with database storage and pool time on 20 MB rows as the cost that grows with
tenants. Plan alignment: `01`'s "no second datastore" is arguably kept; D37's non-goal is reversed
as in D.

**Independent check.** A second analyst scored the same options without seeing this matrix and
recommended **A**, built explicitly as the D37 seam, with D if web upload were already scheduled.
Its points against B are folded into the cells above: B amends the letter of the 2026-09-05 ruling
and forces a re-consent; drops live in one person's Drive and leave the pipeline on a reconnect
under another account; a workspace without Drive gets nothing. Its points for A: ingest in the
delivery's own transaction, send-by-file-id on the card, no scope change. Where the two analyses
differed was not technical: B keeps Storydump a router over the customer's storage; A, D and D′
make Storydump, or Telegram, the holder of a tenant's bytes.

**Holistic.**

- **A.** Correct for a drop; incomplete for curation; debt rises (push against a pull port, two
  fetch dispatchers, retention risk). Fastest to ship.
- **B.** Correct and predictable, a Drive file like any other; complete from drop to post, with
  curation in Drive; debt flat.
- **C.** Lands in a real group; complete. The debt is the scope, or a client-side Picker and a
  browser-side token.
- **D and D′.** Correct; complete for uploads but not for curation until the Library grows tools;
  debt rises with a second store and its obligations.

### The pick: B

Settled from the record rather than by preference:

- The owner's FC-8 ruling describes the vision as adapters over external storage ("think dropbox,
  google drive"), and `01` records upload as a non-goal with the port as its extension seam.
- The 2026-09-08 ruling already names this case: "a folder in another provider, or a future upload
  bucket, is one more source with one more weight." B is that sentence built. C contradicts it: a
  drop into an existing folder is neither a source nor a weight.
- FC-9 reprices export and erasure under paying tenants. A custodial store adds every tenant's
  media bytes to those obligations; the router model adds nothing.
- Timing. Google verification is pending submission
  (`documentation/operations/google-oauth-verification.md`), so a second, non-sensitive scope
  joins the first submission, and two workspaces re-consent rather than two hundred.
- The owner proposed the relay and chose it when asked.

**What is lost.** Versus A: one reconnect per workspace, a Drive write leg and a two-hop ingest; A
ships faster, touches no consent, and needs no Drive grant. Versus C: a system folder appears in
the connector's Drive and the mix is rebalanced unasked; a drop is not in a "real" group until
someone drags it. Versus D and D′: uploads stay tied to a Google grant and to one person's Drive.

**What would change the pick.**

1. The product rules in onboarding without Google Drive: D′ becomes necessary, and B's relay job
   retargets to that store; rows hashed by content are adopted.
2. The app-created folder confuses teams in practice: C's Picker variant is the upgrade path.
3. The owner declines to amend the 2026-09-05 scope ruling: A, with the bytes hashed at ingest,
   as a bounded wedge.

**Sub-choices inside B.** The folder is created on the first drop, not at reconnect. A compressed
drop is accepted with a one-line hint to send as a file, at most once per sender per day. An
unlinked sender gets a one-line reply with the link instruction.

## Junction 2: how a system-created source enters the posting mix

**Options.**

- **M1. Explicit default share, rebalance the rest.** The new source enters at 20%; existing
  explicit shares scale by 0.8. A workspace with no explicit mix has its current automatic shares
  frozen into explicit numbers first.
- **M2. Automatic plus a one-time nudge.** No mix change; the first landed drop posts one notice
  with the effective share and a link to the weights card.
- **M3. A freshness floor in the automatic rule.** `weights()` gives the automatic pool its full
  cap whenever an automatic folder holds a never-posted file.

| Dimension | M1. Default share, rebalance | M2. Automatic + nudge | M3. Freshness floor |
|---|---|---|---|
| **Elegance** | One rule, one number, visible on the card. The freeze clause for an unset mix is the wart. | Zero mechanism, but the nudge names an embarrassing number and the manual step recurs per workspace. | A conditional inside the arithmetic that is hard to explain; "Posts about" jumps when a fresh file appears. |
| **Existing patterns** | `set_mix`'s supersede-then-insert, the per-workspace advisory lock, the SCD history; a NULL `created_by_user_id` already means the system. | An outbox notice like the stranded-source alert. | `weights()` is the one function for planner and card, so the change lands in one place. |
| **Extension** | One `admit_source` function beside `set_mix`; no schema change. | One notice kind. | A second count per source (never-posted eligible files) feeding `weights()`. |
| **DRY** | Reuses `normalize` and `weights`; the freeze reads today's effective shares from `weights()` itself. | Nothing duplicated. | Nothing duplicated. |
| **Separation** | A system actor writes a team's mix, audited by the SCD rows. | Clean. | Mixes freshness (time) into the mix (proportion). |
| **Future-proofing** | Any later system source uses the same admit; the 20% is one constant to tune. | Every system source needs its own nudge; starvation persists until a human acts. | Fixes every small automatic folder, but changes existing workspaces' behaviour. |
| **Plan alignment** | D23 respected; F4's automatic rule untouched; "sources are the groups" reinforced. The freeze turns an unset mix into a set one the team did not type. | Nothing touched. | Amends F4 as re-locked 2026-09-07, a ruling, and the card copy. |

### The pick: M1

The owner's ratified intent for a folder that appears after the mix was set (F4, 2026-09-07) is "a
sensible rate, never takes over, never goes silent", and that rule carried a debut post to
guarantee the last clause. The 2026-09-08 ruling dropped the debut on a stated premise: "the person
connecting a source is in the UI at that moment." A drop source is created by a job with nobody in
the UI, so the premise fails exactly there, and today's arithmetic gives such a source about 0.6%
of draws, which is silence. M1 restores the intent where the premise fails: a sensible rate, one
fixed share that is zero while the source is empty, never silent. Because a drop source holds only
drops, a fresh drop is first in its own never-posted line the moment the source is drawn.

For a source a person connects by hand, the premise holds, so the connect dialog shows the default
share as an editable field rather than applying it unseen.

**What is lost.** Versus M3: small picked folders stay starved unless weighted by hand, which is
today's behaviour and a separate ruling if wanted. Versus M2: a workspace that never set weights
sees numbers appear on its card. **What would change the pick:** if the owner amends F4 anyway, M3
replaces M1's freeze clause and M1 keeps only the default share.

## Side finding

`documentation/operations/google-oauth-verification.md` classes `drive.readonly` as Sensitive.
Google's Drive scope guide, read on 2026-09-20, lists it under Restricted scopes, which carry
restricted-scope verification. The runbook may understate what the submission requires. Flagged
for the owner; not changed by this decision.

## Related

- [`2026-08-02-consolidated-design-plan/03-decision-record.md`](2026-08-02-consolidated-design-plan/03-decision-record.md):
  D37 (the pluggable media-source port), and the post-ratification rulings of 2026-09-05 (one
  Google grant per workspace, `drive.readonly`) and 2026-09-08 (sources are the groups).
- [`2026-08-02-consolidated-design-plan/00-fixed-constraints.md`](2026-08-02-consolidated-design-plan/00-fixed-constraints.md):
  FC-3 (transit), FC-8 (full cloud, pluggable sources), FC-9 (a hosted product with paying tenants).
- [`../archive/2026-09-07-category-registry-and-full-walk/`](../archive/2026-09-07-category-registry-and-full-walk/00_EPIC.md):
  F4's ratified intent and the debut post.
- [`../operations/google-oauth-verification.md`](../operations/google-oauth-verification.md): the
  verification runbook and its pending submission.
- Issues #294 (text the bot an image), #184 (direct upload), #189 (the self-serve compound play),
  #327 (the Picker and `drive.file`).

## Origin

The device-native-inbound discovery of 2026-09-20 (`kindle`): the owner asked for ways to get
media from an iPhone into the pipeline without the download-and-move-to-Drive hop, proposed the
Drive relay mid-discovery, and invoked `weigh-development-paths` at the first design gate. The
weighing ran read-only in plan mode with one independent scorer; the owner approved the result.
