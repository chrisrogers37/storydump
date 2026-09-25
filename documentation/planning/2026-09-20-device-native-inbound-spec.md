---
title: "Device-native inbound: a Telegram drop relayed into the team's Drive, and an iCloud Shared Album as a media source (spec)"
type: spec
status: approved
owner: chris
created: 2026-09-20
approved: "2026-09-20 — owner, section by section in the kindle discovery; the spec text reviewed and approved the same day"
tags: [spec, media-sources, telegram, google-drive, icloud, posting-mix]
links: []
---

> **Status:** approved 2026-09-20 — the design approved section by section in the kindle discovery,
> the spec text reviewed by the owner the same day; handed to `forge`, whose plan is
> [`2026-09-20-device-native-inbound/`](2026-09-20-device-native-inbound/00_EPIC.md) (ratified 2026-09-20).
>
> **2026-09-25:** ironclad cycle 1 on that plan reopened three points as its open forks: §1's trigger and refusals (F11), §3's admit mechanism (F12), and whether §5's album ships at all (F10). It also corrected parts of §1 in the plan's phases — GIFs are not drops, drops are capped at the story path's limits, the drop folder is found by workspace — and where this text and the folded phases differ, the phases win. The two junctions this spec builds on were weighed
> and ratified in [`2026-09-20-device-native-inbound-decision.md`](2026-09-20-device-native-inbound-decision.md);
> this spec inherits those picks and does not re-decide them.

## Summary

Two ways for media to reach a workspace's library straight from a phone, with no download and no
trip through the Google Drive app:

1. **The Telegram drop.** A linked member sends a photo or video in a bound Telegram group. A
   worker job relays it into a "Telegram drops" folder that Storydump creates in the connecting
   account's Google Drive, connected as an ordinary source; the existing Drive sync brings it into
   the library within about a minute; the bot reacts to the message once the file has landed.
   The workspace's Google grant gains Google's per-file write scope, `drive.file`, which reaches
   only folders the app created; every workspace reconnects once.
2. **The iCloud Shared Album source.** An admin pastes a shared album's public link in Settings.
   The album becomes a second adapter on the media-source port: pull-only, no credential, polled
   on the same cadence as a Drive folder. Anyone on the team who adds a photo to the album on
   their phone has supplied the pipeline.

Both create a source the team did not pick in the folder browser, and today such a source would
draw about 0.6% of slots beside a large weighted folder. So both share one new rule: **a source
the system creates enters the posting mix at an explicit 20%**, the existing explicit shares
shrinking proportionally to make room. Storydump holds no media of its own after this spec, as
before it.

## Context

- **The pain.** Content is made on iPhones. Getting it into the pipeline today means saving it,
  opening the Drive app, and uploading it into the right connected folder. The owner's words:
  "right now we need to download and move to google drive."
- **The primitives.** The media-source port is pull-only (`list_changes`, `fetch_bytes`, `probe`;
  `2026-08-02-consolidated-design-plan/01-target-architecture.md`), with Google Drive as its one
  adapter (`src/services/target/google_drive_adapter.py`). Bytes are fetched from the source at
  publish time and staged through Cloudinary as transit (`transit.py`), then destroyed. The
  Telegram webhook already receives every message in a bound group and records the sender's
  membership (`telegram_dispatch.py`, `membership_sync.py`); a media message is otherwise
  ignored. The ingress database role may insert jobs, sources, mix rows and outbox rows
  (migration 057). The posting mix is keyed on the source (`category_mix.py`, migration 071).
- **The rulings this spec builds under.** FC-8 (full cloud; sources are a pluggable adapter
  surface) and D37 (the port; one adapter in v1; "no upload/write operation" as a non-goal with
  the port as the extension seam). The 2026-09-05 ruling (one Google grant per workspace,
  `drive.readonly`). The 2026-09-08 ruling (sources are the groups; "a folder in another
  provider, or a future upload bucket, is one more source with one more weight").
- **What the decision doc settled.** Where a drop's bytes live (the team's Drive, option B of
  five) and how a system-created source enters the mix (an explicit default share, M1 of three),
  with the dissent, the tradeoffs and the conditions that would flip either pick recorded there.
- **Platform facts, verified 2026-09-20.** Telegram bots download files of at most 20 MB, the
  download link is valid for at least an hour, and `setMessageReaction` exists with a fixed
  emoji list. A photo sent as a photo is re-encoded by Telegram (1280 px long edge, 2560 px with
  the client's high-quality toggle); a file is untouched. Google lists `drive.file` as a
  non-sensitive scope, and `drive` and `drive.readonly` as restricted; Google verification for
  the app is pending submission (`../operations/google-oauth-verification.md`). The iCloud
  shared-album feed is undocumented but stable for years and used by open-source clients: a web
  stream call answers the album's name, a change tag and every photo id; an asset-URL call answers
  expiring download URLs per photo; Apple has answered a 330 redirect to a partition host since
  2024.

## Goals

- A photo shared from an iPhone to the bound group is in the Library within two minutes with no
  desktop step, and its file is visible in the team's Drive.
- The first drop after a quiet period is drawn within five slots on average, with nobody
  touching the weights card.
- A photo added to a connected album is in the Library after the next baseline sync, or within a
  minute of Sync Now.
- Storydump owns no new storage; the transit sweep is untouched; no new credential kind exists.

## Non-goals

Drops by direct message to the bot. MMS or SMS. Web upload and an iPhone share-sheet Shortcut
(both later reuse the relay leg, writing into the same drop folder). Captions or contributor names
from either source. Post-next or any queue jumping. Any change to the automatic rule of the mix.
Dropbox, OneDrive, the Google Picker. Typed chat commands (still #854).

## Design

### 1. The Telegram drop, relayed into Drive

**The trigger.** A message in a bound group whose sender's identity is linked, carrying a
`photo`, a `video`, or a `document` whose mime type is an image or video. The webhook admits the
delivery as today. The dispatcher, at the point where it already observes group messages for
membership, recognises the media and enqueues one job, `relay_telegram_drop`, on the interactive
lane, serialized per workspace (`drop:<workspace_id>`) so drops in one workspace run one at a
time. The payload (v1): the chat ref and message id, the sender's Telegram id and user id, the
file id and unique id, size, mime type, kind, the caption, and whether it was sent as a photo, a
video or a file. Nothing is answered in the chat at this point. `membership_sync.observe` already
answers `unknown_identity` for an unlinked sender; that outcome becomes the first refusal below.

**The job**, in order, with a commit before every provider call:

1. **Find or create the drop folder.** The workspace's drop source is the `gdrive` source whose
   config carries `role: "telegram_drops"`. If none exists, the job creates a folder named
   "Telegram drops" at the root of the connecting account's Drive under the grant's `drive.file`
   scope, tagged with app properties (`storydump=telegram_drops`, the workspace id) so it stays
   recognisable if renamed or moved, creates its source row through
   `provisioning.get_or_create_media_source` with `folder_name` "Telegram drops" and the role
   marker, arms it for sync (`media_sync.rearm_after_connect`), and admits it to the mix
   (§3), all in one transaction.
2. **Download from Telegram**: `getFile`, then the file URL, through the egress floor with the
   byte cap set to Telegram's 20 MB bot limit. The token-bearing URL is never logged.
3. **Upload into the folder** with Drive's resumable protocol, two calls. The file is named by
   date, sender and message id; its description records who dropped it, in which group, when,
   and the caption text; its app properties carry the Telegram unique id, so a retry after a
   partial upload finds the existing file instead of making a second one. The sync's
   content-hash dedup catches anything else.
4. **Mint a demand sync** for the source, the same `sync_media_source{reason:'demand'}` job
   `sync_now` mints, so the item is in the Library within about a minute.
5. **React** to the original message with one emoji from Telegram's permitted reaction set
   (`setMessageReaction`; forge picks the emoji).

**Refusals** go back as a one-line reply, a `notification` outbox row to the group's binding, and
the job then counts as handled rather than retried:

| Reason | Reply names |
|---|---|
| The sender is not linked | the link instruction |
| The file is over 20 MB or not an image or video | the cap or the type |
| The Drive grant lacks the write scope, or is expired or revoked | Settings › Integrations, reconnect |
| The drop source was removed in Settings | Settings › Integrations, pick the folder again |
| The drop folder is gone in Drive | restore it in Drive, or remove the source in Settings |

Transient failures ride the interactive lane's retry ladder; the reaction is the only success
signal, so a person sees nothing until the file has landed.

**Compression.** A Telegram `photo` is always re-encoded and a `document` never is, so the job
can tell them apart. A compressed drop is accepted, and the sender gets a one-line hint to send as
a file for full quality, at most once per sender per day, paced through `rate_counters`.

**Curation.** A drop is a Drive file. The team drags it into another connected folder in the
Drive app; the sync already reads a move as a change of owner, label and path on the same row.

**Removal.** Remove on the drop source in Settings is the existing pause-with-flag: nothing is
deleted, drops are refused with the reply above, and picking the folder again in the folder
browser re-enables them.

### 2. The two-scope Google grant

The connect leg asks for `drive.readonly` and `drive.file`. The encrypted payload envelope moves
to v2 and records the scopes Google actually granted, because Google's consent screen lets a
person grant one scope and decline the other. The exchange keeps any grant that carries
`drive.readonly` (as today) and records whether `drive.file` came with it. `drive_status` reports
`writable`. A grant without the write scope keeps syncing; only drops need the reconnect, and both
the Settings card ("Reconnect to enable Telegram drops") and the drop's refusal say so. The
2026-09-05 ruling's "same scope" clause is amended by the decision doc; its verification stance
holds, since no sensitive or restricted scope is added, and the pending submission lists both
scopes from the start.

**A reconnect under a different Google account** cannot write to a drop folder the first account
owns, and cannot list it unless that person shares it. The next drop creates a new drop folder
under the new account; the old source sits in `error` like any folder the grant cannot see, its
files still in the first person's Drive. The refusal texts and the runbook name this.

### 3. The mix admit rule

One function beside the existing writer, `category_mix.admit_source(session, *, workspace_id,
source_id, ratio=DEFAULT_ADMIT_RATIO, by_user_id)`, with `DEFAULT_ADMIT_RATIO = 0.2`, under the
same per-workspace advisory lock and through the same supersede-then-insert, so the SCD history
records it:

- If explicit rows exist: the new source enters at the ratio, and every explicit ratio is scaled
  by `1 - ratio`.
- If no explicit rows exist: every connected source that has available media is first frozen at
  its current effective share (from `weights()`, the one function), scaled by `1 - ratio`; the new
  source enters at the ratio.
- Off rows stay Off. An automatic source with no media stays automatic. Rounding keeps the
  sum-to-one tolerance `normalize` enforces. Admitting an already-admitted source is a no-op.
- The relay job admits the drop source as the system (no author). The album's connect route
  admits it as the connecting admin, at the ratio they left in the dialog (default 20%, editable,
  greater than zero).

The rule restores the owner's ratified F4 intent ("a sensible rate, never takes over, never goes
silent") exactly where the 2026-09-08 premise for dropping the debut ("the person connecting a
source is in the UI at that moment") does not hold. Because a drop source holds only drops, an
empty one weighs nothing, and a fresh drop is first in its own never-posted line the moment the
source is drawn.

### 4. The provider registry

The design record promised that a second provider costs one adapter and no core change. Today the
sync executor consumes `deps.drive`, and both fetch seams in `src/worker.py`
(`_publish_media_fetch`, `_card_media_fetch`) call `drive.fetch_bytes` directly; neither the intent
row nor the card's media block (`prompts.py::render_card`) carries a provider. This section makes
the promise true: a registry keyed on `media_sources.provider`, consumed by the sync executor
(`deps.adapters[provider]`), by the publish pipeline's fetch (the intent read joins
`media_sources.provider` through the item) and by the card's fetch (the media block gains
`provider`). Drive is the registry's only entry until §5 lands. A pure refactor: the gates are
green before and after, with no behaviour change. Drops do not need it; the album does.

### 5. The iCloud Shared Album adapter

**Provider** `icloud_album`. **Config** (v1): `album_token` (the string after `#` in the public
link), `album_name` (the stream's name at connect, for display), the partition host the 330
redirect named. **Checkpoint** (v1): the change tag, and a cursor into the photo-id list while a
first ingest is chunked. No credential row: the album is public by link.

- **Probe:** one web-stream call. Success answers the name and change tag. Refused by name at
  connect: `album_link_invalid`, `album_not_public`, `album_already_connected`.
- **List changes:** the web stream answers every photo id in one response (Apple caps an album at
  5,000 items); the adapter pages its own listing through the checkpoint in chunks of 200 so a
  first ingest stays bounded per job, as Drive's does. An unchanged change tag costs one request.
  Each item: ref = the photo id; kind from Apple's asset type; content hash = the checksum of the
  largest derivative; name from the id and kind; category label = the album's name; folder path
  NULL, which migration 070 reserves for an adapter with no folders. A photo absent from the
  listing is marked `removed`, the signal a Drive file gives.
- **Fetch bytes:** asset URLs expire, so they are resolved at fetch time and never stored: one
  asset-URL call for the photo, then a download of the largest derivative under the floor's byte
  cap. Photos arrive as 2048 px JPEG derivatives, videos at 720p; HEIC never appears.
- **Failure posture:** an unrecognised feed shape, a 4xx, or a vanished album is classified
  persistent; the source flips to `error`, the stranded-source alert fires once, the planner
  already skips an error source, every other source is untouched. Removing the source is the
  kill switch.
- **The token** lives in `config` like a folder ref and is never logged whole.

**Egress.** Apple's partition hosts (`p<NN>-sharedstreams.icloud.com`) and the asset hosts the
feed names are not exact names on the floor's allow-list. The floor gains an anchored pattern
rule for the two host families. Issue #871 (address pinning) closed on 2026-08-27, so widening
is permitted; it remains a change to a security control and ships as its own reviewable unit with
its own tests. The asset host family is pinned by the live probe before the adapter is built.

**Connect flow.** Settings › Integrations gains an Albums block beneath Drive. Add album opens a
dialog with the link field, the plain statement that the album is reachable by anyone holding the
link, and the default share as an editable field. `POST /workspaces/{ws}/sources` gains a
`provider` discriminator (`gdrive` by default, `icloud_album` with `album_ref`) and keeps the
folder body unchanged. The source appears on the weights card at once. Sync Now and Remove work
as for a folder.

**Cadence.** The baseline sync, the demand sync ahead of a slot, and Sync Now. No faster polling.

### 6. Surfaces, consolidated

- Integrations, Drive block: the reconnect state; the drop folder listed among the folders under
  its name, with Sync Now and Remove as for any folder.
- Integrations, Albums block: as §5.
- The weights card: new sources appear at once; the label query learns the album's name key.
- The Library and the approval card: unchanged. A drop is a Drive file; an album item carries the
  album's name as its category label.
- The `storydump` CLI: `sync <source_id>` and the read views work unchanged over both sources.

### 7. On disk

- Migrations: `ck_sources_provider` gains `icloud_album`; `ck_quarantine_provider` gains `icloud`;
  `ck_jobs_kind` gains `relay_telegram_drop` (a tenant kind). No new table, no new outbox kind
  (`notification` carries the replies), no new credential provider.
- The Drive grant envelope v2 (inside the encrypted payload; not a schema change).
- Config keys: `role` on the drop source; the album's `album_token`, `album_name`, host.
- Two transport methods (`getFile` plus the file download, `setMessageReaction`) on the host
  already allowed. A Drive write leg on the adapter: folder create, resumable upload, a lookup by
  app property. The floor's pattern rule.
- Documentation: `.claude/rules/telegram.md` (the drop), the Telegram runbook (privacy mode is now
  load-bearing for drops as it is for membership), the design record's post-ratification rulings
  (D37's write leg; the 2026-09-05 scope amendment), the Integrations guide.

## Decisions

Locked by the decision doc, inherited here: the drop's bytes live in the team's Drive (B); a
system-created source enters at an explicit default share (M1); the folder is created on the
first drop, not at reconnect; a compressed drop is accepted with a paced hint; an unlinked sender
gets the link instruction; the album stays a pull source; the album's default share is shown
editable at connect because a person is present. Approved in discovery: drops from the bound
group only; the bot's acknowledgement is a reaction, refusals a one-line reply; the album's
public-link exposure is acceptable and stated on the dialog; picked Drive folders keep today's
Automatic entry.

Left for forge: the reaction emoji; the file-naming scheme; the pattern-rule syntax and its
tests; the album checkpoint's cursor shape; migration numbering and markers; whether the relay
job's lane budget needs a longer deadline for a 20 MB video.

## Build order

Six phases in two trains; each phase a PR with the gates green.

1. The mix admit rule (§3): the function, unit tests on the arithmetic, a database gate on the
   SCD rows as the production roles.
2. The two-scope grant (§2): the connect leg, the envelope v2, `writable`, the Settings state.
3. The drop (§1): the dispatcher branch, the relay job, the reaction, the replies, the hint, the
   rules page and the runbook.
4. The provider registry (§4): a pure refactor, Drive the only entry.
5. The egress pattern rule (§5): its own reviewable security change, its own tests.
6. The album (§5): the adapter and its stub for the gates, the connect route and dialog, the
   migrations, the Integrations guide.

Train one (phases 1 to 3) ships drops. Train two (4 to 6) ships the album, after a one-off probe
against a real public album the owner provides confirms the feed's shape and pins the asset hosts.

## Test plan

Per `.claude/rules/testing.md`: new behaviour gets a unit test with scripted executors and fakes
at the seams; anything whose truth lives in the database gets a gate.

- Unit: the admit arithmetic (explicit mix; no explicit mix; Off rows; empty sources; idempotence;
  the sum-to-one tolerance); the dispatcher on real Telegram update fixtures (photo, video,
  document, unlinked sender, a DM, a channel post); the relay job over scripted Telegram and Drive
  doors (the happy path, each refusal, a retry after a partial upload, a trashed folder); the
  envelope v2 round trip and a grant with one scope declined; the album adapter over a recorded
  feed fixture (`validate_source_config` first, then probe, chunked listing, removal, an
  unrecognised shape); the egress rule on allowed and refused hosts and on look-alikes.
- Gates: the three widened constraints; the admit rule's rows as `svc_worker` and `svc_ingress`;
  the relay job's writes under RLS with a second workspace seeded; the registry refactor's
  before-and-after parity on the existing sync and fetch gates.
- The docs pins (`tests/test_agent_docs.py` and its siblings) after every documentation edit.
- The live probe before phase 6, recorded in the plan's ledger.

## Verification, end to end

A drop from a phone lands in the Library and in Drive; the reaction appears; a compressed photo
draws the hint once; a second identical drop makes no second row; removing the source refuses the
next drop by name. An album addition appears after Sync Now; a removed photo is marked removed; a
made-up link is refused by name at connect. The weights card shows the new sources and the
rebalanced shares. `storydump sync <source_id>` works for both.

## Open owner items

- Amend the 2026-09-05 ruling's "same scope" clause and record D37's write leg in the design
  record (the build's docs step; the decision doc is the interim home).
- The verification runbook classes `drive.readonly` as Sensitive; Google's scope guide lists it
  as Restricted. Owner's page; flagged, not changed.
- A real public album for the live probe before train two.

## Related

- [`2026-09-20-device-native-inbound-decision.md`](2026-09-20-device-native-inbound-decision.md)
- [`2026-08-02-consolidated-design-plan/01-target-architecture.md`](2026-08-02-consolidated-design-plan/01-target-architecture.md), the media-source port
- [`2026-08-02-consolidated-design-plan/03-decision-record.md`](2026-08-02-consolidated-design-plan/03-decision-record.md), D37 and the 2026-09-05 and 2026-09-08 rulings
- [`../operations/google-oauth-verification.md`](../operations/google-oauth-verification.md)
- Issues #294, #184, #189, #327, #854

## Origin

The kindle discovery of 2026-09-20: the owner asked for ways to bring media in from an iPhone
natively, named iCloud shared albums and "texting in images", proposed the Drive relay
mid-discovery, raised the mix balance, and invoked `weigh-development-paths` at the first design
gate. Three design sections were approved in turn.
