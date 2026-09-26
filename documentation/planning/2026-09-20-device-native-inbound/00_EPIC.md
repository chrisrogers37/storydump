---
title: "Device-native inbound — the Telegram drop relayed into the team's Drive, the iCloud Shared Album source, and the admit rule for a system-created source (epic)"
type: plan
status: active
owner: chris
created: 2026-09-20
tags: [plan, media-sources, telegram, google-drive, icloud, posting-mix, epic]
links: []
---

> **Ratified 2026-09-20** at the forge gate (F1–F9 locked). **Ironclad cycle 1, 2026-09-25:** ten lenses found 2 critical and 15 major findings ([the review on #1334](https://github.com/chrisrogers37/storydump/pull/1334#issuecomment-5836147615)); the technical ones are folded into the phases, and three owner decisions came out of it as open forks F10–F12. Phase 01 waits on F12, phase 03's trigger and replies on F11, and train two (04–06) on F10. Spec: [`2026-09-20-device-native-inbound-spec.md`](../2026-09-20-device-native-inbound-spec.md); the junctions it builds on: [`2026-09-20-device-native-inbound-decision.md`](../2026-09-20-device-native-inbound-decision.md). The ledger is [`RUN_LOG.md`](RUN_LOG.md). Next: the owner rules F10–F12, then a confirming cycle 2.

## Summary

Two ways for media to reach a workspace's library straight from a phone. A linked member sends a photo or video in a bound Telegram group; a worker job relays it into a "Telegram drops" folder Storydump creates in the connecting account's Google Drive under a newly added per-file write scope, connected as an ordinary source, and the existing sync ingests it; the bot reacts to the message once the file has landed. An admin pastes an iCloud Shared Album's public link in Settings; the album becomes the second adapter on the media-source port, pull-only and credential-free, polled on the same cadence as a folder. Both create a source nobody picked in the folder browser, and today such a source would draw about 0.6% of slots beside a large weighted folder; so both share one rule, a source the system creates enters the posting mix at an explicit 20% with the existing shares scaled to make room. Eight PRs in two trains: the admit rule, the two-scope grant (two PRs) and the drop (two PRs) ship drops; the provider registry, the egress pattern rule and the album adapter ship the album, if F10 keeps it. Storydump owns no media after this epic, as before it.

Why build rather than point people at the Drive app's share extension, which already exists: that extension is the detour the owner named as the pain (save, open Drive, find the folder), and only people the folder is shared with can use it, while a drop needs only membership of the group. Cycle 1's reviewers put nearly all of the epic's value in train one; train two is a second phone-native path to the same metric, which is why F10 is open.

## Evidence

The epic-level facts; each phase carries its own. Every citation was re-verified on `main` at `ea788875` on 2026-09-21, after the tech-debt sprint (#1336–#1357) moved most of the cited files; the citations into the files `main` changed since were re-verified on `29acea2e` on 2026-09-25. Three of those commits changed the pattern the worker's provider-facing executors follow (f5088231, 7561f7f7) and how a chunk chain carries its deadline (bbbb6f52); phases 03 and 04 follow the new pattern.

- The media-source port is pull-only, `list_changes` / `fetch_bytes` / `probe`, and v1 implements one adapter; "there is no upload/write operation" is recorded as a non-goal with the port as the extension seam (`documentation/planning/2026-08-02-consolidated-design-plan/01-target-architecture.md:74-82`; D37 at `03-decision-record.md:151`). The Drive adapter implements `list_changes` and `fetch_bytes` only; nothing implements `probe` today.
- The Drive grant asks for exactly one scope, `drive.readonly` (`src/services/target/google_drive_oauth.py:91`), one per workspace with no owner column (`src/models/target/accounts_sources_media.py:182-246`; ruling 2026-09-05 at `03-decision-record.md:201`).
- A media message in a bound group reaches `TelegramDispatcher.__call__` and is routed to `_observe_all` for membership; the media is ignored (`src/services/target/telegram_dispatch.py:327-340`; `membership_sync.group_members_of` at `:32-57`).
- The ingress role holds SELECT/INSERT/UPDATE on `jobs`, `media_items`, `media_sources`, `category_post_case_mix`, `channel_outbox` (`scripts/migrations/057_grant_matrix_and_archive_schema.sql:100-106`).
- The posting mix is keyed on the source; `category_mix.weights` splits the automatic pool by file count and caps it at `r_min / (1 + r_min)` (`src/services/target/category_mix.py:106-145`); the card's copy states the same rule (`landing/src/components/dashboard/settings/category-weights-card.tsx:22-28`).
- Both fetch seams call the Drive adapter directly (`src/worker.py:117-133`, `:776-810`); the outbox `notification` kind exists (`src/models/target/machinery.py:153-156`) and its `{"v": 1, "text"}` envelope is built by `outbox.fanout_notification` (`src/services/target/outbox.py:232-262`); the jobs kind set is a CHECK (`:77-85`) and the executor registry is a dict whose completeness test derives from that CHECK (`work_loop.py:231`, the dict assembled at `:528-672`; `tests/src/services/target/test_work_loop.py:123`).
- The egress floor allows exact host names only (`src/services/target/egress.py:133-141`, `:286-289`); the address pinning #871 landed (`:44`, closed 2026-08-27).
- Schema changes are advertised: each migration is mirrored as a numbered section of `07-security-model.md`, classified in `scripts/advertised_ddl_manifest.json`. On `main` at `29acea2e` the latest are migration 083 and §26 (`083_clock_tick_deadlines.sql`, #1381). Both numbers moved twice while this plan was open, so the plan names none: phase 03's migration and section take the next free number and section at build time, and phase 06's the ones after. A widened CHECK is a new section carrying the `DROP CONSTRAINT` / `ADD CONSTRAINT` pair (precedent: 065, §11), listed in `tests/scripts/test_lineage_lane.py` and matched by the model in the same PR (`.claude/rules/migrations.md`).
- Platform facts verified 2026-09-20 against the primary pages: Telegram bots download at most 20 MB via getFile and `setMessageReaction` exists; Google lists `drive.file` as non-sensitive and `drive`/`drive.readonly` as restricted; Google verification is pending submission (`documentation/operations/google-oauth-verification.md:3`).

## Architecture

```
phone ──Telegram group──▶ webhook (svc_ingress) ──enqueue──▶ relay_telegram_drop (worker, interactive lane)
                                                              │ 1. find/create the drop folder in Drive        [floor]
                                                              │    then the source row and its 20% admit       [tx]
                                                              │ 2. getFile → download (≤ 20 MB)                [floor]
                                                              │ 3. Drive resumable upload into the drop folder [floor]
                                                              │ 4. mint sync_media_source{reason: demand}      [tx]
                                                              └ 5. setMessageReaction                          [floor]
                                                   Drive folder ──sync (existing)──▶ media_items ──mix──▶ slot ──▶ story

phone ──Shared Album──▶ Apple's public feed ◀──poll── sync_media_source (existing) via adapters["icloud_album"]
                                                     probe / list_changes / fetch_bytes ──▶ media_items (same path as Drive)

adapters = {"gdrive": GoogleDriveAdapter, "icloud_album": ICloudAlbumAdapter}   ← phase 04, keyed on media_sources.provider
                consumed by: media_sync (deps.adapters), worker._publish_media_fetch, worker._card_media_fetch
```

Layer boundaries hold: the API's webhook route only admits and enqueues; the worker does every provider call outside a transaction (the relay executor is marked `@own_transactions` and makes each provider call after its session block has closed, as commit f5088231 requires of every provider-facing executor); services never import channels; the CLI and the web are untouched except Settings.

## Decision Forks

- **F1 — The acknowledgement emoji.** Context: `setMessageReaction` accepts a fixed emoji set; the reaction is the drop's only success signal. Options: (a) 👌; (b) 👍; (c) 👀 while relaying then 👌 on landing (two calls). Lean: (a), one call, one meaning. Ratifier: owner. Status: locked (2026-09-20, ratified with the plan at the forge gate).
- **F2 — The Drive file's name.** Context: a drop lands in a folder people browse in the Drive app, so its name is what they see there. Options: (a) `YYYY-MM-DD_<sender>_<message_id>.<ext>` for every drop; (b) as (a) for photos and videos, the original file name date-prefixed for a document; (c) the Telegram unique id. Lean: (b); provenance in the name, the original name kept when there is one, uniqueness by message id. Ratifier: owner. Status: locked (2026-09-20, ratified with the plan at the forge gate).
- **F3 — The relay job's lane and budget.** Context: interactive is 3 attempts and a 10-minute deadline from mint; bulk is 5 attempts and 6 hours (`jobs.py:127-130`; `enqueue` takes per-job overrides at `:306-308`); a 20 MB upload can take longer than one rung. Options: (a) interactive with the lane defaults; (b) interactive with a per-kind override (`enqueue` accepts both); (c) bulk. Lean: (b), a person is waiting for the reaction, and a short Drive outage should not end in a silent failure. Ratifier: owner. Status: locked (2026-09-20, ratified with the plan at the forge gate). **Cycle 1 corrected the override's numbers so they deliver the locked intent:** 5 attempts on the interactive ladder (10/30/60 s, the last rung repeating, `jobs.py:139-142`) spend themselves in about three minutes, so the ratified 30-minute deadline never bound. The override is now 12 attempts, about ten minutes of retries, under the same 30-minute deadline, and a final failure is answered in the chat (03 › Steps 7).
- **F4 — Where the "writable" fact lives.** Context: `oauth_credentials` has no metadata column; the spec says the envelope records the granted scopes and calls it not a schema change. Options: (a) envelope v2 only, and `drive_status` decodes the payload through the credentials door to answer `writable`; (b) a nullable `granted_scopes text[]` column by migration, advertised, the envelope unchanged; (c) both, the envelope the truth and the column a copy. Lean: (a), honouring the spec; the API process already holds the key and decodes at refresh. Ratifier: owner. Status: locked (2026-09-20, ratified with the plan at the forge gate).
- **F5 — Where the drop folder is created.** Context: `drive.file` reaches only folders the app created, so the drop folder must be one the app makes, somewhere in the connecting account's Drive. Options: (a) at the root of the connecting account's My Drive, named "Telegram drops"; (b) inside an app-created "Storydump" parent so later app-created folders nest under one; (c) let the admin choose a parent (needs the Picker, out of scope). Lean: (a); one folder, visible, movable by the person afterwards without losing app access. Ratifier: owner. Status: locked (2026-09-20, ratified with the plan at the forge gate).
- **F6 — The egress pattern rule's shape.** Context: Apple's feed answers from partitioned hosts and names its asset hosts in each response, while the floor matches exact names. Options: (a) an `allowed_host_patterns` tuple of anchored regular expressions, two entries; (b) a suffix list (`.icloud.com`, `.icloud-content.com`); (c) enumerate `p01`…`p99` and the observed asset hosts as exact names. Lean: (a); a suffix admits any subdomain, an enumeration is a guess at Apple's partitions. Ratifier: owner. Status: locked (2026-09-20, ratified with the plan at the forge gate).
- **F7 — The album checkpoint's cursor.** Context: the feed answers every photo id at once; the sync bounds a first ingest at 200 items per job. Options: (a) an offset into the feed's order; (b) the set of seen ids; (c) ids sorted, a `after` cursor, restart when the change tag moves. Lean: (c); an offset skips when the order shifts, a seen-set grows to Apple's 5,000 cap. Ratifier: owner. Status: locked (2026-09-20, ratified with the plan at the forge gate).
- **F8 — The registry's shape.** Context: phase 04's dispatch needs a source's provider at three call sites that already read the source's row under their tenant. Options: (a) `adapters: Mapping[str, adapter]` on the worker's deps, callers pass the provider they read in their own tenant-scoped SQL; (b) a registry object that looks a source up by id itself. Lean: (a); a lookup by id inside the adapter layer is the cross-tenant hazard `media_sync.py` already names. Ratifier: owner. Status: locked (2026-09-20, ratified with the plan at the forge gate).
- **F9 — Migration packaging.** Context: each widened CHECK costs a migration, an advertised `07` section and a lineage-lane entry. Options: (a) one migration widening all three CHECKs, in phase 03; (b) two, `ck_jobs_kind` with phase 03 and the two provider CHECKs with phase 06. Lean: (b); each train ships only what it uses, and each file's postcondition asserts a value its own code needs. Ratifier: owner. Status: locked (2026-09-20, ratified with the plan at the forge gate). Cycle 1 added `ck_rate_scope` to phase 03's migration (the drop's paced replies) and dropped phase 06's `ck_quarantine_provider` widening (nothing reads or writes `provider_quarantine`).
- **F10 — The iCloud Shared Album (phases 04–06).** Context: a connected album must have Apple's Public Website switch on, so anyone holding the link can see it, and Apple's page names the album's owner and every contributor; the feed is undocumented; phases 04 and 05, the epic's hot-path refactor and its change to a security control, exist only for the album; cycle 1 also found the port still Drive-shaped where the album needs it (04 › Summary). Options: (a) keep 04–06 as designed, with cycle 1's fixes and a connect dialog that says Apple's page names the contributors; (b) defer 04–06, keep their docs on file, and revisit with the post-launch measurement of drops (Verification Checklist); (c) replace them with an iPhone share-sheet Shortcut — cycle 1 priced this as #184's authenticated route that accepts bytes plus a drop-only token, not a layer over phase 03, because sharing to the bound group from the iOS share sheet already is phase 03. Lean: (b); seven of ten cycle-1 lenses recommended it. Ratifier: owner. Status: open. Nothing in 04–06 is built, and no album is made public for the probe, until this is locked.
- **F11 — What counts as a drop, and who may drop.** Context: as specced, every photo or video a linked member posts in a bound group is relayed. That group is also where approval cards arrive, and the mission casts it as "a tap is the whole interface" (`PROJECT_MISSION.md:48-52`). Every existing grant reads not writable until an admin reconnects (phase 02), so without an off-state every group photo would draw a public refusal, Remove would make the bot answer every later photo, and unbinding the group — which also ends approval cards — would be the only quiet exit. A linked member's first drop also creates a source and rewrites the mix, which the web keeps behind the admin floor. Options: (a) as specced — every photo from a linked, active member — with every refusal paced and collapsed per media group; (b) an admin switch per bound group, "Accept drops", on the Telegram card, off by default and settable only while the Drive grant is writable; while it is off the bot ignores media silently, and while it is on any linked, active member may drop; (c) drops only in a direct message with the bot, where that chat is bound to one workspace — no group noise and no privacy-mode dependency, but it reopens discovery's "bound group only". Lean: (b). Ratifier: owner. Status: open. Phase 03's admission (Steps 5) and replies follow the ruling; the rest of phase 03 does not depend on it.
- **F12 — How a system-created source enters the posting mix (reopens the mechanism of the ratified M1, not its intent).** Context: M1 enters the drop folder at an explicit 20% and, in a workspace whose sources are all Automatic, first freezes their current shares into explicit rows, because `weights()` normalises explicit rows among themselves and a lone explicit 20% would take half the slots. Cycle 1 found the freeze moves the starvation instead of curing it: afterwards a folder the team picks stays Automatic and is capped at `r_min/(1+r_min)` of the smallest frozen share (`category_mix.py:138-140`) — on the 300/50 example a 500-file folder picked later draws about 10–12% instead of about 59% — frozen shares stop tracking folder growth, and the card shows weights nobody typed. Options: (a) keep M1 with the freeze, corrected by cycle 1, say so on the weights card and in the guide, and pin the behaviour with a gate case; (b) a reserved share: a system source carries its share as a reservation that `weights()` takes off the top, and every other source keeps today's rule over the rest, with no freeze and no rewrite of the team's weights; (c) no system share: a system source is Automatic like a picked folder (M2, rejected at ratification for starving the drop). Lean: (b); it keeps M1's intent — a sensible rate, never takes over, never silent — without touching the team's weights, at the cost of changing `weights()`, which the ratified decision kept stable. Ratifier: owner. Status: open. Phase 01 is written for (a) and is rewritten under (b); nothing else in the epic depends on which.

## Companion Plans

- [`2026-09-20-device-native-inbound-spec.md`](../2026-09-20-device-native-inbound-spec.md) — the approved spec this plan builds; once F11 and F12 are ruled, its §1 trigger and refusals and its §3 follow them.
- [`2026-09-20-device-native-inbound-decision.md`](../2026-09-20-device-native-inbound-decision.md) — the ratified junctions; F12 reopens Junction 2's mechanism, not its pick.
- [`2026-08-02-consolidated-design-plan/`](../2026-08-02-consolidated-design-plan/README.md) — `01` (the port), `02` §2 (sources and media), `03` D37 and the 2026-09-05 / 2026-09-08 rulings, `07` (the advertised sections this epic extends).
- [`2026-09-09-telegram-interaction-at-throughput/`](../2026-09-09-telegram-interaction-at-throughput/00_EPIC.md) — the tap and the outbox under load; the drop reuses its admission, savepoint and outbox patterns.
- [`google-oauth-verification.md`](../../operations/google-oauth-verification.md) — the pending submission; phase 02 rewrites its scopes, justification and demo script for the write scope.
- The phases: [01](01_the-admit-rule.md) · [02](02_the-two-scope-grant.md) · [03](03_the-drop.md) · [04](04_the-provider-registry.md) · [05](05_the-egress-pattern-rule.md) · [06](06_the-album.md). The cycle-1 review: [PR #1334](https://github.com/chrisrogers37/storydump/pull/1334#issuecomment-5836147615).

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Two workspaces connected to one Google account share a drop folder and ingest each other's drops (cycle 1's critical) | **High** — a cross-tenant leak | the ledger holds the folder id; Drive discovery serves only crash recovery and matches both the `storydump` and the `workspace` property; a test with two workspaces on one account (03 › Steps 2, 6) |
| The bot floods a group with replies, or relays photos nobody meant as drops | **High** — the approval chat degrades | F11; every reply paced and collapsed per media group (03 › Steps 5) |
| A deploy skew or a rollback reads a v2 grant as dead | **High** — sources flip to error, alerts fire | phase 02 ships expand then contract: the v1+v2 reader first, the v2 writer after it is live on both services |
| The registry refactor touches the publish pipeline's fetch, a hot path with a recent investigation | **High** — a regression in the first fetch or the float | phase 04 (only if F10 keeps the album) is a pure refactor with the gates green before and after, deployed alone, never touching `fn_prompts_due`, checked with `storydump burst` and `storydump health` |
| The bot cannot see media in a group (privacy mode on, not an admin) | **Medium** — no drops arrive, silently | the precondition membership already needs; the runbook, the rules page and the guide's "No reaction?" section name it; F11(c) would remove it |
| A person declines `drive.file` on Google's granular consent screen | **Medium** — drops refused for that workspace | the envelope records what was granted; the card shows the reconnect line and, after the callback, a "write permission declined" banner |
| Drops fill one person's Drive, or a reconnect moves the grant to another account | **Medium** — drops stop | a full Drive is refused by name, naming whose Drive; the relay checks the folder under the current grant before each upload and, when it cannot write it, retires the old drop source and creates a fresh folder |
| A drop the story path cannot carry | **Medium** — a 👌 on something that never posts, redrawn forever | drops capped per kind at `min(20 MiB, PUBLISH_MAX_BYTES[kind])`; GIFs, stickers and video notes are not drops |
| The album's public exposure: anyone with the link can see it, and Apple's page names the owner and every contributor | **Medium** | F10; if kept, the connect dialog says both |
| Apple changes the shared-album feed | **Medium** — album sources stop (only if F10 keeps the album) | persistent classification → `error` and the stranded-source alert once; every other source unaffected; the live probe before phase 05 |
| Widening the allow-list widens the SSRF surface | **Medium** (only if F10 keeps the album) | anchored patterns only, look-alike tests, #871's pinning already in place |
| A 20 MB relay holds one of three interactive tasks for the whole fleet | **Medium** — approval cards wait | per-workspace serialization; a burst check in 03's checklist |
| The mix rebalance surprises a team that set its weights by hand | **Low** — a support question | F12; visible on the card at once, SCD history, one sentence of copy |

## Complexity and Sequencing

| Phase | Size | Depends on | Parallel with |
|---|---|---|---|
| [01 the admit rule](01_the-admit-rule.md) | S | F12 | 02 |
| [02 the two-scope grant](02_the-two-scope-grant.md) — 02a the v1+v2 reader, then 02b the v2 writer | M | none; 02b after 02a is live on both services | 01 |
| [03 the drop](03_the-drop.md) — 03a the transport and the Drive write leg, then 03b the drop | XL | 01, 02; F11 for 03b's admission | none (03a may start alongside 01 and 02) |
| [04 the provider registry](04_the-provider-registry.md) | M | F10 | 05 |
| [05 the egress pattern rule](05_the-egress-pattern-rule.md) | S | F10; the live probe, an external gate needing a real public album from the owner | 04 |
| [06 the album](06_the-album.md) | L | F10; 01, 03, 04, 05 | none |

Critical path for drops: the F11 and F12 rulings → 01 and 02a, then 02b → 03b, with 03a alongside. Train two, only if F10 keeps it: the probe → 05 → 06, with 04 alongside.

## Implementation Plan

### Dependencies

The approved spec and the ratified decision. F1–F9 locked at the forge gate; F10–F12, opened by cycle 1, owed by the owner before the phases that wait on them. For train two, if kept, a real public shared album for the probe.

### Blocks

Web upload (#184) and a share-sheet Shortcut that uploads directly would reuse 03a's Drive write leg, each behind its own authenticated route that accepts bytes; sharing to the bound group from the iOS share sheet already works through phase 03. Recording the scope amendment (phase 02) and D37's write leg (phase 03) in the design record.

### Steps

Phases 01–06, one file each; 02 and 03 ship as two PRs each, the others as one. Each PR carries its CHANGELOG entry (a docs-only PR is exempt), its docs and its mutation battery. Nothing that waits on an open fork is built before the fork is locked.

## Test Plan

Per phase; the epic's own gate is that every phase's checklist is green and the two trains' end-to-end checks in the spec's "Verification, end to end" hold in production.

## Verification Checklist

- [ ] A photo shared from a phone to a bound group, with drops switched on as F11 rules, is in the Library within two minutes, its file in the workspace's "Telegram drops" folder, the reaction on the message.
- [ ] After the first drop, the weights card's "Posts about" column (`mix_view`'s `effective`) shows the drop source at 20%, and the clock gate's admit-rule draw test passes.
- [ ] Only if F10 keeps the album: a photo added to a connected album is in the Library after Sync Now.
- [ ] No migration the epic adds contains `CREATE TABLE` or widens `ck_credentials_provider` (`grep -E 'CREATE TABLE|ck_credentials_provider'` over those files prints nothing), and no epic PR's `gh pr diff <n> --name-only` lists `src/services/target/transit.py`.
- [ ] The design record carries the 2026-09-05 scope amendment and D37's write leg as post-ratification rulings, and neither `01-target-architecture.md` nor `07` §15 still says the app is read-only.
- [ ] Four weeks after drops ship, a read with the `storydump` read verbs (`jobs`, `outbox`, `story`) records in `RUN_LOG.md` the drops per workspace-week, the median time from drop to posted, and the refusals by reason — the input to F10.

## What NOT To Do

Do not store bytes anywhere Storydump owns (decision B). Do not add the full `drive` scope or the Picker (decision C rejected). Do not change `category_mix.weights` unless F12 rules (b). Do not build a DM drop path (unless F11 rules (c)), MMS, web upload or the Shortcut in this epic. Do not build anything in 04–06 before F10 is locked, nor the album adapter before the live probe. Do not make the webhook do a provider call, and do not let anything the drop adds to the webhook raise out of the dispatcher. Do not look a drop folder up in Drive by the `storydump` property alone.

## Context

Area: services, worker, channels, API, web Settings, migrations, docs · Effort: XL across eight PRs (five if F10 defers the album) · Risk: medium-high (a consent change and a new ingest path into a shared chat; with the album, a change to a security control and a hot-path refactor) · Priority: high (the owner's stated pain).
