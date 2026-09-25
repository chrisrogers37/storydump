---
title: "Device-native inbound — phase 06: the iCloud Shared Album adapter, its connect flow, and the provider migration (PR 6)"
type: plan
status: active
owner: chris
created: 2026-09-20
tags: [plan, media-sources, icloud, api, web, migrations, docs]
links: []
---

> Phase 06 of [`00_EPIC.md`](00_EPIC.md), ratified 2026-09-20 at the forge gate. Spec: [`2026-09-20-device-native-inbound-spec.md`](../2026-09-20-device-native-inbound-spec.md). The ledger is [`RUN_LOG.md`](RUN_LOG.md); its entry for this phase records where the build departs from this text.

## Summary

The second adapter on the media-source port. An admin pastes an album's public link in Settings; the route parses the token, probes the feed, creates the source, arms it for sync, admits it to the mix at the share left in the dialog, and the existing sync brings the album's photos in through the registry of phase 04, over the hosts phase 05 admitted. One migration widens two provider CHECKs. No credential row: the album is public by link. Built only after the probe of phase 05 confirmed the feed's shape and produced the fixtures.

## Evidence

- `src/api/routes/v1.py:608-683` `create_source` — reads `root_name` `:626`, `folder_name` `:627`, `folder_ref` `:640` and `:673`; refuses `drive_not_connected` `:631`, `folder_required`/`folder_not_a_drive_folder` via `folder_ref_from` `:640` (raised at `provisioning.py:398-407`), `source_nested` `:657-662` (raised at `provisioning.py:464`, `:469`), `sources_changed` `:667-669` (`provisioning.py:439`); `get_or_create_media_source` `:670`, `rearm_after_connect` `:677`; the adapter from `_drive_adapter(request)` `:642` (`:819-825`, per request); the admin floor is `principal.admin_session` (`src/api/principal.py:369`, called at `:628`, `:663`, `:696`); `_drive_read` `:802-816`. `:414-418` `list_sources`; `:686-702` `remove_source` (pause by flag, 404 when nothing paused). `sync_now` is member-floor (`src/services/target/commands.py:112`, `:230`).
- `src/services/target/provisioning.py:358-408` `folder_ref_from` (bare id or `/folders/` URL; `folder_required`, `folder_not_a_drive_folder`); `:443-470` `refuse_nested_pick`; `:473-546` `get_or_create_media_source` (idempotent on the folder under an advisory lock); `:853` `pause_media_source`.
- `src/models/target/accounts_sources_media.py:162` `ck_sources_provider CHECK (provider IN ('gdrive'))`; `:163-165` `ck_sources_config_v`; `:166-168` `ck_sources_state`; `:137-140` `ck_quarantine_provider CHECK (provider IN ('ig','telegram','cloudinary','gdrive'))`. Declared by `scripts/migrations/054_accounts_sources_media_tables.sql:115` and `:160`. On `main` at `29acea2e` the highest file is `083` (#1381). The precedent for widening a CHECK is 065 (`DROP CONSTRAINT` then `ADD CONSTRAINT`), mirrored as `07-security-model.md` §11 (`:596`) and classified normative in `scripts/advertised_ddl_manifest.json`; the latest section is §26 (083). This phase takes the next free number and section at build time, after phase 03's.
- `src/services/target/workspaces.py:337-356` `list_sources` (columns incl. `config->>'folder_ref'`, `config->>'folder_name'`, the `removed` flag `:352` from `CONNECTED_FLAG_SQL` `:331`); `category_mix.py:47` `_LABEL`.
- `src/services/target/media_sync.py:216` `alert_stranded_sources`; `:552-604` the persistent classes flip the source to `error` (`sync_checkpoint = '{"v":2}'` `:563-564`) and fan one `notification` per binding through `outbox.fanout_notification` (`:586`); `:73` `CHUNK_ITEM_BOUND = 200`; `drive_adapter.py:109` `checkpoint_incomplete` (complete only when the checkpoint is bare `{"v": 1}`).
- Web: `landing/src/app/api/workspaces/[id]/sources/route.ts:36-88` (POST forwards `folder_ref`, `root_name`, `folder_name` at `:70-72`); `[sourceId]/route.ts:11` (DELETE, 29 lines); `landing/src/lib/dashboard-payloads.ts:121-136` `SourceRow` (`folder_ref`, `folder_name`, `removed?`), `:428` `firstSource` hard-codes `"gdrive"`. The Settings UI was split by the sprint: `landing/src/components/dashboard/settings/drive-card.tsx:60` `driveSources`, `:94` `removeFolder`, `:151` `syncSource`, the Folders block `:227-307` (heading `:229`, the picker dialog `:230-242`, a row `:253-300` with Sync Now `:283-290` and Remove `:291-298`); `drive-folder-picker.tsx:147-186` `openPicker`, `:187` `pickFolder`, the button label `:212`; `integrations-tab.tsx:45-67` the props (the file is 114 lines).
- No guide under `documentation/guides/` documents Integrations or Drive (2026-09-20 listing); Drive appears only in deployment prose (`cloud-deployment.md:199`, `:345-348` and elsewhere on that page).
- Tests: `tests/src/api/test_v1_routes.py:953` `TestSourcesUnderTheWorkspaceGrant` (`:1000` created-named-armed, `:1010` no grant, `:1074`/`:1085` nesting, `:1094`, `:1114`, `:1125`, `:1140`, `:1149` remove pauses, `:1163` 404); `tests/src/services/target/test_google_drive_adapter.py:94, :184, :209, :236, :253, :294, :427` (item mapping, checkpoint, config refusals, error routing; there are no probe tests because the adapter has no probe); `tests/scripts/test_w6_sync_gate.py:529` `TestAStrandedSourceKeepsSayingSo`, `:766`; `tests/scripts/test_customer_notice_gate.py` covers no-media and parked-intent notices only.

## Implementation Plan

### Dependencies

Phases 01, 04, 05; the probe's fixtures; F7 and F9 ratified.

### Blocks

None (the album is the last phase).

### Steps

1. **Migration `scripts/migrations/NNN_icloud_album_provider.sql`**, NNN the next free number at build time (F9): `ALTER TABLE media_sources DROP CONSTRAINT ck_sources_provider; ALTER TABLE media_sources ADD CONSTRAINT ck_sources_provider CHECK (provider IN ('gdrive','icloud_album'));` and the same pair for `provider_quarantine.ck_quarantine_provider` adding `'icloud'`; two `-- runner:postcondition` lines reading `pg_get_constraintdef` for the new values. Mirror: the next free `### §` in `07-security-model.md` with the identical fenced block (after phase 03's), its sha256 classified normative in `scripts/advertised_ddl_manifest.json`, the file added to the ratified list in `tests/scripts/test_lineage_lane.py`, the two CHECKs widened in `accounts_sources_media.py:137-140, :162`, and `scripts/migrations/adoption_manifest.json` extended as the previous migration's entry was.
2. **`src/services/target/icloud_album_adapter.py`** (new). `PROVIDER = "icloud_album"`. `album_ref_from(value)` accepts `https://www.icloud.com/sharedalbum/#<token>` or a bare token, refuses `album_link_invalid`. `validate_source_config(config)` requires `album_token` (the same module-level contract as Drive's). `class ICloudAlbumAdapter(client, *, policy)` with the Drive adapter's call shapes for the two operations they share, so the registry's callers need nothing per provider: `list_changes(config, checkpoint, *, source_id, workspace_id) -> (items, checkpoint)` and `fetch_bytes(source_id, workspace_id, file_ref, max_bytes) -> bytes`; plus the port's `probe(config) -> AlbumProbe`, which no adapter implements today (the Drive connect route checks the grant instead), called only by the connect route. Internals: `_base_url` (`p01` then the 330 redirect's host, cached in `config["host"]`), `_webstream(base, ctag)`, `_asset_urls(base, guids)` in chunks of 25 as the client does. Items: ref = the photo id; kind from the asset type; hash = the largest derivative's checksum; size; name `<id>.jpg` or `.mp4`; `category` = the album's name; `folder_path` None. Checkpoint `{v: 1, ctag, after}` (F7): ids sorted, `after` the last id handed out, `checkpoint_incomplete` true while `after` is set; a changed ctag restarts. An item absent from the listing is reported removed. Persistent failures raise `media_sync.DriveSourceGone` (the port's persistent class; its name is not renamed here). `class StubAlbumAdapter` beside it for the gates, scripted from the fixtures.
3. **Registration.** `src/worker.py:864-877`: `adapters[PROVIDER] = ICloudAlbumAdapter(client=..., policy=EgressPolicy(allowed_host_patterns=DEFAULT_ALLOWED_HOST_PATTERNS))`; the API's per-request builder beside `_drive_adapter` for the connect probe.
4. **Provisioning.** `get_or_create_album_source(executor, *, workspace_id, album_token, album_name, host)` — idempotent on `(workspace_id, provider, config->>'album_token')` under the workspace's sources lock, writing `config = {v: 1, album_token, album_name, host}`; a paused (removed) album re-picked revives, as a folder does.
5. **The route.** `create_source` reads `provider = body.get("provider", "gdrive")`. For `icloud_album`: `album_ref_from`; `probe` through the album adapter, outside `principal.admin_session` exactly as the Drive read at `v1.py:656-662` is — the API's tenant sessions are armed with the egress in-transaction tripwire (`egress.py:347-352`), so a probe inside one raises (a failure is `album_not_public` 409; a shape failure `album_unreachable` 502); an existing connected source with that token is `album_already_connected` 409 carrying `source_id`; create; `rearm_after_connect`; `category_mix.admit_source(..., ratio=body.get("ratio", DEFAULT_ADMIT_RATIO), by_user_id=principal.user_id)`; 201. The Drive path is untouched. `remove_source` and `sync_now` work unchanged. `list_sources` adds `config->>'album_name' AS album_name` (never the token). `_LABEL` becomes `COALESCE(folder_name, album_name, folder_ref, 'source')`.
6. **The web.** `SourceRow` gains `album_name`; the BFF POST forwards `provider`, `album_ref`, `ratio`. a new `album-card.tsx` beside `drive-card.tsx`, rendered by `integrations-tab.tsx` after the Drive card and mirroring the Folders block (`drive-card.tsx:227-307`) — the list (`provider === "icloud_album" && state !== "paused"`), "Add album" opening a dialog with the link field, the sentence that the album is reachable by anyone holding the link, and a share field prefilled 20; rows reuse the Sync Now and Remove handlers `drive-card.tsx` holds (`syncSource` `:151`, `removeFolder` `:94`), lifted to a shared module; refusal copy for the four names. `firstSource` (`:428`) keeps its Drive meaning.
7. **Docs.** `documentation/guides/media-sources.md` (created by phase 03) gains the albums section: the public-link fact, the Automatic-versus-20% rule, Sync Now and Remove; `AGENTS.md`'s tech-stack line names iCloud Shared Albums; the docs guard battery green.
8. **CHANGELOG** under Unreleased.

## Test Plan

- Unit, `tests/src/services/target/test_icloud_album_adapter.py` over the fixtures: `album_ref_from` shapes and refusals; `validate_source_config` first; probe success and each failure class; the listing chunked at 200 with the cursor across three calls; an unchanged ctag lists nothing new; a removed id reported; an unrecognised shape is persistent; `fetch_bytes` picks the largest derivative, honours `max_bytes`, and every request goes through the floor under the pattern policy (a fake client asserts the hosts).
- API: `create_source` with `provider: "icloud_album"` — created 201 and admitted with the given ratio; a re-connect 409 `album_already_connected`; `album_link_invalid`; `album_not_public`; the Drive body unchanged.
- Gates: `test_w6_sync_gate.py` with `StubAlbumAdapter` registered — the baseline sync ingests, chunk chaining, the error flip and one alert; the two widened constraints accept the new values and refuse others; lineage lane parity with the model.

## Verification Checklist

- [ ] The new migration replays in the lineage lane and the advertised-DDL test is green with its `07` section classified.
- [ ] In a dev workspace: connect the owner's album → the source lists with its name, the weights card shows it at 20%, Sync Now brings its photos into the Library, Remove pauses it, a bogus link is refused by name.
- [ ] `git grep -n "<the album token>"` returns nothing in the tree; logs show the token truncated.
- [ ] The docs guard battery green with the new guide and the README row.

## What NOT To Do

No credential row, no captions or contributor fields, no faster polling, no logging of the token, no building before the probe, no renaming of the port's persistent classes in this PR.

## Context

Area: services, worker, API, web, migrations, docs · Effort: L · Risk: medium (an undocumented feed; a new live page) · Priority: high for train two.
