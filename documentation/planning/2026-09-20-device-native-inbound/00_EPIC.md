---
title: "Device-native inbound — the Telegram drop relayed into the team's Drive, the iCloud Shared Album source, and the admit rule for a system-created source (epic)"
type: plan
status: active
owner: chris
created: 2026-09-20
tags: [plan, media-sources, telegram, google-drive, icloud, posting-mix, epic]
links: []
---

> **Ratified 2026-09-20** at the forge gate; the nine decision forks below were locked with it. Spec: [`2026-09-20-device-native-inbound-spec.md`](../2026-09-20-device-native-inbound-spec.md); the junctions it builds on: [`2026-09-20-device-native-inbound-decision.md`](../2026-09-20-device-native-inbound-decision.md). The ledger is [`RUN_LOG.md`](RUN_LOG.md). Next: one `ironclad` cycle before any phase is built.

## Summary

Two ways for media to reach a workspace's library straight from a phone. A linked member sends a photo or video in a bound Telegram group; a worker job relays it into a "Telegram drops" folder Storydump creates in the connecting account's Google Drive under a newly added per-file write scope, connected as an ordinary source, and the existing sync ingests it; the bot reacts to the message once the file has landed. An admin pastes an iCloud Shared Album's public link in Settings; the album becomes the second adapter on the media-source port, pull-only and credential-free, polled on the same cadence as a folder. Both create a source nobody picked in the folder browser, and today such a source would draw about 0.6% of slots beside a large weighted folder; so both share one rule, a source the system creates enters the posting mix at an explicit 20% with the existing shares scaled to make room. Six PRs in two trains: the admit rule, the two-scope grant and the drop ship drops; the provider registry, the egress pattern rule and the album adapter ship the album. Storydump owns no media after this epic, as before it.

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
- **F2 — The Drive file's name.** Options: (a) `YYYY-MM-DD_<sender>_<message_id>.<ext>` for every drop; (b) as (a) for photos and videos, the original file name date-prefixed for a document; (c) the Telegram unique id. Lean: (b); provenance in the name, the original name kept when there is one, uniqueness by message id. Ratifier: owner. Status: locked (2026-09-20, ratified with the plan at the forge gate).
- **F3 — The relay job's lane and budget.** Context: interactive is 3 attempts and a 10-minute deadline from mint; bulk is 5 attempts and 6 hours (`jobs.py:127-130`; `enqueue` takes per-job overrides at `:306-308`); a 20 MB upload can take longer than one rung. Options: (a) interactive with the lane defaults; (b) interactive with a per-kind override, 5 attempts and a 30-minute deadline (`enqueue` accepts both); (c) bulk. Lean: (b), a person is waiting for the reaction, and a short Drive outage should not end in a silent failure. Ratifier: owner. Status: locked (2026-09-20, ratified with the plan at the forge gate).
- **F4 — Where the "writable" fact lives.** Context: `oauth_credentials` has no metadata column; the spec says the envelope records the granted scopes and calls it not a schema change. Options: (a) envelope v2 only, and `drive_status` decodes the payload through the credentials door to answer `writable`; (b) a nullable `granted_scopes text[]` column by migration, advertised, the envelope unchanged; (c) both, the envelope the truth and the column a copy. Lean: (a), honouring the spec; the API process already holds the key and decodes at refresh. Ratifier: owner. Status: locked (2026-09-20, ratified with the plan at the forge gate).
- **F5 — Where the drop folder is created.** Options: (a) at the root of the connecting account's My Drive, named "Telegram drops"; (b) inside an app-created "Storydump" parent so later app-created folders nest under one; (c) let the admin choose a parent (needs the Picker, out of scope). Lean: (a); one folder, visible, movable by the person afterwards without losing app access. Ratifier: owner. Status: locked (2026-09-20, ratified with the plan at the forge gate).
- **F6 — The egress pattern rule's shape.** Options: (a) an `allowed_host_patterns` tuple of anchored regular expressions, two entries; (b) a suffix list (`.icloud.com`, `.icloud-content.com`); (c) enumerate `p01`…`p99` and the observed asset hosts as exact names. Lean: (a); a suffix admits any subdomain, an enumeration is a guess at Apple's partitions. Ratifier: owner. Status: locked (2026-09-20, ratified with the plan at the forge gate).
- **F7 — The album checkpoint's cursor.** Context: the feed answers every photo id at once; the sync bounds a first ingest at 200 items per job. Options: (a) an offset into the feed's order; (b) the set of seen ids; (c) ids sorted, a `after` cursor, restart when the change tag moves. Lean: (c); an offset skips when the order shifts, a seen-set grows to Apple's 5,000 cap. Ratifier: owner. Status: locked (2026-09-20, ratified with the plan at the forge gate).
- **F8 — The registry's shape.** Options: (a) `adapters: Mapping[str, adapter]` on the worker's deps, callers pass the provider they read in their own tenant-scoped SQL; (b) a registry object that looks a source up by id itself. Lean: (a); a lookup by id inside the adapter layer is the cross-tenant hazard `media_sync.py` already names. Ratifier: owner. Status: locked (2026-09-20, ratified with the plan at the forge gate).
- **F9 — Migration packaging.** Options: (a) one migration widening all three CHECKs, in phase 03; (b) two, `ck_jobs_kind` with phase 03 and the two provider CHECKs with phase 06. Lean: (b); each train ships only what it uses, and each file's postcondition asserts a value its own code needs. Ratifier: owner. Status: locked (2026-09-20, ratified with the plan at the forge gate).

## Companion Plans

- `2026-09-20-device-native-inbound-spec.md` — the approved spec this plan builds.
- `2026-09-20-device-native-inbound-decision.md` — the ratified junctions; conditions that would flip them are recorded there, not here.
- `2026-08-02-consolidated-design-plan/` — `01` (the port), `02` §2 (sources and media), `03` D37 and the 2026-09-05 / 2026-09-08 rulings, `07` (the advertised sections this epic extends).
- `2026-09-09-telegram-interaction-at-throughput/` — the tap and the outbox under load; the drop reuses its admission and outbox paths.
- `../operations/google-oauth-verification.md` — the pending submission that will list both scopes.

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| A person declines `drive.file` on Google's granular consent screen | drops refused for that workspace | envelope records what was granted; `writable=false` shows the reconnect state; the refusal names it |
| The bot cannot see media in a group (privacy mode on, not an admin) | no drops arrive, silently | the same precondition membership already needs; the runbook and the rules page name it; the refusal path cannot help, so the setup guide carries a check |
| Apple changes the shared-album feed | album sources stop | persistent classification → `error` + the stranded-source alert once; every other source unaffected; the live probe before phase 06 |
| The registry refactor touches the publish pipeline's fetch, a hot path with a recent investigation | a regression in the first fetch or the float | phase 04 is a pure refactor with the existing gates green before and after, deployed alone, a burst watched with `storydump burst` |
| Drops sit in one admin's Drive quota; a reconnect under another account strands the old folder | drops leave the pipeline until shared | the next drop creates a new folder; the old source errors like any unseen folder; texts name it |
| The mix rebalance surprises a team that set 70/30 by hand | a support question | visible on the card at once, SCD history, one sentence of copy on the card |
| A slow 20 MB upload serializes later drops in the workspace | a delayed reaction | per-workspace serialization is deliberate; the budget in F3 bounds it |
| Widening the allow-list widens the SSRF surface | a new host family reachable through the floor | anchored patterns only, look-alike tests, #871's pinning already in place |

## Complexity and Sequencing

| Phase | Size | Depends on | Parallel with |
|---|---|---|---|
| 01 the admit rule | S | none | 02, 04 |
| 02 the two-scope grant | M | none | 01, 04 |
| 03 the drop | L | 01, 02 | 04, 05 |
| 04 the provider registry | M | none | 01, 02, 03 |
| 05 the egress pattern rule | S | the live probe | 03, 04 |
| 06 the album | L | 01, 04, 05 | none |

Critical path for drops: 02 → 03 (01 alongside). Critical path for the album: the probe → 05 → 06, with 04 alongside; the probe needs a real public album from the owner.

## Implementation Plan

### Dependencies

The approved spec and the ratified decision. The owner's ratification of F1–F9 at this plan's gate. For phase 06, a real public shared album.

### Blocks

The share-sheet Shortcut and web upload (both write into the drop folder through phase 03's relay leg); recording D37's write leg and the 2026-09-05 scope amendment in the design record (phase 03's docs step).

### Steps

Phases 01–06, one file and one PR each, in the sequencing table's order; each PR carries its CHANGELOG entry (a docs-only PR is exempt) and its docs.

## Test Plan

Per phase; the epic's own gate is that every phase's checklist is green and the two trains' end-to-end checks in the spec's "Verification, end to end" hold in production.

## Verification Checklist

- [ ] A photo shared from a phone to a bound group is in the Library within two minutes, its file visible in the team's Drive, the reaction on the message.
- [ ] The first drop after a quiet period is drawn within five slots on average (a 20% share), nobody having touched the weights card.
- [ ] A photo added to a connected album is in the Library after the next baseline sync, or within a minute of Sync Now.
- [ ] `git grep` shows no new provider credential kind and no new storage; `transit.py` unchanged.
- [ ] The design record carries D37's write leg and the 2026-09-05 amendment as post-ratification rulings.

## What NOT To Do

Do not store bytes anywhere Storydump owns (decision B). Do not add the full `drive` scope or the Picker (decision C rejected). Do not change `category_mix.weights` (M3 rejected). Do not build a DM drop path, MMS, web upload or the Shortcut in this epic. Do not build the album adapter before the live probe. Do not make the webhook do a provider call.

## Context

Area: services, worker, API, web Settings, migrations, docs · Effort: XL across six PRs · Risk: medium (a consent change, a security-control change, a hot-path refactor) · Priority: high (the owner's stated pain).
