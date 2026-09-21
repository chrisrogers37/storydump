---
title: "Device-native inbound — phase 04: the sync executor and both fetch seams dispatch on the source's provider (PR 4)"
type: plan
status: active
owner: chris
created: 2026-09-20
tags: [plan, worker, media-sources, refactor]
links: []
---

> Phase 04 of [`00_EPIC.md`](00_EPIC.md), ratified 2026-09-20 at the forge gate. Spec: [`2026-09-20-device-native-inbound-spec.md`](../2026-09-20-device-native-inbound-spec.md). The ledger is [`RUN_LOG.md`](RUN_LOG.md); its entry for this phase records where the build departs from this text.

## Summary

The design record promised a second provider costs one adapter and no core change. Today the sync executor consumes `deps.drive`, both fetch seams in the worker call `drive.fetch_bytes` directly, and neither the intent row nor the card's media block carries a provider. This phase introduces a registry keyed on `media_sources.provider`, threads the provider through the two reads that feed the fetch seams, and dispatches in three places. Drive is the registry's only entry. A pure refactor: the sync, pipeline and transport gates are green before and after with no behaviour change, and the PR deploys alone so a burst can be watched.

## Evidence

- `src/worker.py:117-133` `_publish_media_fetch(drive)` — `fetch(intent)` reads `media_kind`, `source_id`, `workspace_id`, `provider_file_ref` and calls `drive.fetch_bytes` (`:125`); no provider key. `:114` `PUBLISH_MAX_BYTES`. `:776-810` `_card_media_fetch(drive)` — reads `kind`, `source_id`, `workspace_id`, `ref` (`:789-796`), calls `drive.fetch_bytes` (`:793`), maps `DriveTerminalError → MediaUnavailable` (`:799-802`) and the retryable pair to `MediaTransient` (`:803-808`).
- `src/worker.py:275-277` `compose(..., drive=None)`; `:289-303` `WorkerDeps(...)` with `media_fetch=None if drive is None else _publish_media_fetch(drive)` (`:292`) and `drive=drive` (`:301`); `:864-877` the one `GoogleDriveAdapter(...)` construction (the constructor call `:864-866`), handed to the transport (`:874`) and to `compose` (`:875-877`).
- `src/services/target/work_loop.py:645-654` parks `sync_media_source` and `first_ingest_chunk` when `deps.drive is None` (`_NO_DRIVE`).
- `src/services/target/media_sync.py:546-551` the only `deps.drive` use: `list_changes(dict(row["config"]), checkpoint, source_id=..., workspace_id=...)`; `:552` `except (DriveSourceGone, DriveCredentialDead)`, the two persistent classes defined at `:78` and `:82`, provider-neutral by location despite their names.
- `src/services/target/publish_pipeline.py:391-422` `_load` — the SELECT (`:401-415`, its JOINs `:412-414`) joins `ig_accounts`, `workspaces`, `media_items` and carries `m.source_id, m.mime_type, m.media_kind, m.provider_file_ref, m.file_name`; `media_sources` is not joined. `:1114` `media = await media_fetch(dict(ctx.intent))`; `DriveTerminalError` terminal at `:1120`.
- `src/services/target/prompts.py:248-263` `render_card` builds `payload["media"] = {workspace_id, source_id, ref, kind, mime, file_name}` when `provider_file_ref` and `source_id` are present; `src/channels/telegram_transport.py:568-600` (`send`) consumes it (`_MEDIA_KINDS` defined `:81`, used `:574`; the workspace cross-check `:577-593`; the fetch itself in `_fetch_media`, `:482`).
- `src/services/target/drive_adapter.py:109` `checkpoint_incomplete`, `:122` `validate_source_config` (Drive-specific: requires `folder_ref`). The scripted `StubDriveAdapter` this file used to carry was deleted in #1325 (the file is 140 lines), so the gates script the real adapter's injected `client` instead; `fetch_bytes` lives only on `google_drive_adapter.GoogleDriveAdapter` (`:681`).
- Gates: `tests/scripts/test_w6_sync_gate.py` (`TestBaselineSyncEndToEnd:196`, `TestChunkChaining:330`, `TestFailureRouting:1045`, `TestSubfoldersAreCategories:1132`, `TestAChunkCarriesOneWalk:1288`); `tests/scripts/test_l5_pipeline_gate.py:762, :1931, :1963` (the fetch ladder); `tests/src/test_worker.py:480-509` (the class `:480`; `media_fetch` wired with Drive `:485-495`, parked without `:497-509`); `tests/src/channels/test_telegram_transport.py:388, :446, :970` (the card media path).

## Implementation Plan

### Dependencies

None (F8 ratified: callers pass the provider they read themselves).

### Blocks

Phase 06 (the album adapter registers under `icloud_album`).

### Steps

1. **`src/services/target/media_adapters.py`** (new, small): `PROVIDER_GDRIVE = "gdrive"`; `class ProviderNotWired(StorydumpError)`; `def adapter_for(adapters: Mapping[str, Any], provider: str)` returning the adapter or raising `ProviderNotWired(provider)`. No lookups by id (F8).
2. **The worker.** `compose(..., adapters: Optional[Mapping[str, Any]] = None)` replaces `drive=`; `WorkerDeps.adapters` replaces `WorkerDeps.drive`; `media_fetch = None if not adapters else _publish_media_fetch(adapters)`. `_publish_media_fetch(adapters)`: `adapter_for(adapters, intent["provider"])` then `.fetch_bytes(...)` as today; `ProviderNotWired` maps to the terminal class the rung already treats as terminal. `_card_media_fetch(adapters)`: `adapter_for(adapters, media["provider"])`, `ProviderNotWired → MediaUnavailable`. Construction `:864-877`: `adapters = {PROVIDER_GDRIVE: GoogleDriveAdapter(...)}`. `work_loop.py:645-654` parks the sync kinds when `not deps.adapters`.
3. **The sync.** `media_sync.py:546`: `adapter = adapter_for(deps.adapters, row["provider"])` (the row SELECT is on `media_sources`; add `provider` to its column list); `ProviderNotWired` is classified persistent alongside the two existing classes (the source flips to `error` with the alert, as a dead credential does). `first_ingest_chunk` shares `_run_sync`, so nothing else moves.
4. **The intent read.** `publish_pipeline._load` (the SELECT `:401-415`, beside its JOINs `:412-414`) joins `media_sources s ON s.workspace_id = m.workspace_id AND s.id = m.source_id` and selects `s.provider`. `media_items.source_id` is NOT NULL and the FK is composite, so the join is total.
5. **The card.** `prompts.render_card` adds `"provider": intent["provider"]` to the media block; every SELECT that feeds `render_card` gains the same join (enumerate at build time with `git grep -n "provider_file_ref" src/services/target`; each already selects `source_id` and `provider_file_ref`, so `provider` rides the same statement). The transport forwards the block unchanged.
6. **Docs.** `.claude/rules/scheduler.md` (the worker page) names `deps.adapters`; the module docstring of `media_sync.py` (`:13`) says "duck-typed on `deps.adapters[provider]`".
7. **CHANGELOG** under Unreleased: a refactor line.

## Test Plan

- Unit: `tests/src/test_worker.py` — `compose(adapters={})` parks the sync kinds and leaves `media_fetch` unwired; `adapters={"gdrive": stub}` wires both; `_publish_media_fetch` and `_card_media_fetch` dispatch by provider and answer the typed error for an unknown one. `tests/src/services/target/test_prompts.py`: the media block carries `provider`. Transport tests unchanged.
- Gates: `test_w6_sync_gate.py` and `test_l5_pipeline_gate.py` unchanged and green (parity); one new l5 case: an intent whose source provider is not wired fails the fetch rung as terminal with the intent's own evidence row.

## Verification Checklist

- [ ] `git grep -n "deps.drive" src/` returns nothing; `git grep -n "\"provider\"" src/services/target/publish_pipeline.py src/services/target/prompts.py` shows the join and the block key.
- [ ] The three gates green before the change (baseline run recorded in the PR) and after.
- [ ] After deploy, `storydump burst --since <deploy time>` shows a normal burst and `storydump health` well.

## What NOT To Do

No behaviour change, no album code, no renaming of `DriveSourceGone` / `DriveCredentialDead`, no lookup by source id inside the registry, no second construction site for the Drive adapter.

## Context

Area: worker, services · Effort: M · Risk: medium (the publish pipeline's fetch is the hot path the 2026-09-11 investigation lived in) · Priority: high for train two.
