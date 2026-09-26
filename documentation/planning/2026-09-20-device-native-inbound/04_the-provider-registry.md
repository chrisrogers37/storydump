---
title: "Device-native inbound — phase 04: the sync executor and both fetch seams dispatch on the source's provider (PR 4)"
type: plan
status: active
owner: chris
created: 2026-09-20
tags: [plan, worker, media-sources, refactor]
links: []
---

> Phase 04 of [`00_EPIC.md`](00_EPIC.md), ratified 2026-09-20 at the forge gate and folded after ironclad cycle 1 (2026-09-25). **Waits on F10 (open): built only if the owner keeps the album.** Spec: [`2026-09-20-device-native-inbound-spec.md`](../2026-09-20-device-native-inbound-spec.md). The ledger is [`RUN_LOG.md`](RUN_LOG.md); its entry for this phase records where the build departs from this text.

## Summary

The design record promised a second provider costs one adapter and no core change. Today the sync executor consumes `deps.drive`, both fetch seams in the worker call `drive.fetch_bytes` directly, and neither the intent row nor the card's media block carries a provider. This phase introduces a registry keyed on `media_sources.provider`, threads the provider through the two reads that feed the fetch seams, and dispatches in three places. Cycle 1 found the port still Drive-shaped where a second adapter needs it — the sync owned the cursor's chaining, `fetch_bytes` received no `config`, and a failure alert quoted the exception — so this phase also makes the adapter contract provider-neutral. It also found that one feeder of the card is a SECURITY DEFINER door the phase must not touch. Drive is the registry's only entry. A pure refactor: the sync, pipeline and transport gates are green before and after with no behaviour change, and the PR deploys alone so a burst can be watched.

## Evidence

- `src/worker.py:117-133` `_publish_media_fetch(drive)` — `fetch(intent)` reads `media_kind`, `source_id`, `workspace_id`, `provider_file_ref` and calls `drive.fetch_bytes` (`:125`); no provider key. `:114` `PUBLISH_MAX_BYTES`. `:776-810` `_card_media_fetch(drive)` — reads `kind`, `source_id`, `workspace_id`, `ref` (`:789-796`), calls `drive.fetch_bytes` (`:793`), maps `DriveTerminalError → MediaUnavailable` (`:799-802`) and the retryable pair to `MediaTransient` (`:803-808`).
- `src/worker.py:275-277` `compose(..., drive=None)`; `:289-303` `WorkerDeps(...)` with `media_fetch=None if drive is None else _publish_media_fetch(drive)` (`:292`) and `drive=drive` (`:301`); `:864-877` the one `GoogleDriveAdapter(...)` construction (the constructor call `:864-866`), handed to the transport (`:874`) and to `compose` (`:875-877`).
- `src/services/target/work_loop.py:662-671` parks `sync_media_source` and `first_ingest_chunk` when `deps.drive is None` (`_NO_DRIVE`); both wrappers are marked `@own_transactions` (`:649`, `:654`, commit f5088231), so `_run_job` calls them with `session=None` and finalizes in its own short session (`:926-935`). `tests/src/services/target/test_work_loop.py`'s `TestEveryProviderFacingExecutorOwnsItsTransactions` (`:59`, its set at `:82`) holds both marks, over a `full_deps()` that passes `drive=object()` (`:51`).
- `src/services/target/media_sync.py:546-551` the only `deps.drive` call, inside `_call_provider` (`:531-605`), which runs with no session open (`_read_source`, `:403-528`, reads the source row in its own session and closes it; its SELECT is at `:421`; the row's fields travel in `_SyncStart`, `:390-400`, and reach `_call_provider` through `_run_sync`'s call at `:364-372`): `list_changes(dict(row["config"]), checkpoint, source_id=..., workspace_id=...)`; `:552` `except (DriveSourceGone, DriveCredentialDead)`, the two persistent classes defined at `:78` and `:82`, provider-neutral by location despite their names.
- `src/services/target/publish_pipeline.py:387-443` `_load` — the SELECT (`:397-411`, its JOINs `:408-410`) joins `ig_accounts`, `workspaces`, `media_items` and carries `m.source_id, m.mime_type, m.media_kind, m.provider_file_ref, m.file_name`; `media_sources` is not joined. `:1116` `media = await media_fetch(dict(ctx.intent))`; `DriveTerminalError` terminal at `:1122`.
- `src/services/target/prompts.py:248-263` `render_card` builds `payload["media"] = {workspace_id, source_id, ref, kind, mime, file_name}` when `provider_file_ref` and `source_id` are present; `src/channels/telegram_transport.py:568-600` (`send`) consumes it (`_MEDIA_KINDS` defined `:81`, used `:574`; the workspace cross-check `:577-593`; the fetch itself in `_fetch_media`, `:482`).
- `src/services/target/drive_adapter.py:109` `checkpoint_incomplete`, `:122` `validate_source_config` (Drive-specific: requires `folder_ref`). The scripted `StubDriveAdapter` this file used to carry was deleted in #1325 (the file is 140 lines), so the gates script the real adapter's injected `client` instead; `fetch_bytes` lives only on `google_drive_adapter.GoogleDriveAdapter` (`:681`).
- Gates: `tests/scripts/test_w6_sync_gate.py` (`TestBaselineSyncEndToEnd:208`, `TestChunkChaining:342`, `TestFailureRouting:1072`, `TestSubfoldersAreCategories:1159`, `TestAChunkCarriesOneWalk:1315`); `tests/scripts/test_l5_pipeline_gate.py:777, :1946, :1978` (the fetch ladder); `tests/src/test_worker.py:480-509` (the class `:480`; `media_fetch` wired with Drive `:485-495`, parked without `:497-509`); `tests/src/channels/test_telegram_transport.py:388, :446, :970` (the card media path).

## Implementation Plan

### Dependencies

F10 locked as (a). Otherwise none (F8 ratified: callers pass the provider, and now the config, they read in their own tenant-scoped SQL).

### Blocks

Phase 06.

### Steps

1. **`src/services/target/media_adapters.py`** (new, small): `class MediaAdapter(Protocol)` — `list_changes(config, checkpoint, *, source_id, workspace_id) -> (items, checkpoint)`, `fetch_bytes(config, file_ref, *, source_id, workspace_id, max_bytes) -> (bytes, name, mime)` (the tuple both seams already unpack, `worker.py:125`, `:788-798`), `checkpoint_incomplete(checkpoint) -> bool`; `class ProviderNotWired(StorydumpError)`; `def adapter_for(adapters: Mapping[str, MediaAdapter], provider: str)` returning the adapter or raising `ProviderNotWired(provider)`. Provider names come from `vocabulary.py` (`PROVIDER_GDRIVE` at `:153`); no new constant. No lookup by id (F8).
2. **The worker.** `compose(..., adapters: Optional[Mapping[str, MediaAdapter]] = None)` replaces `drive=`; `WorkerDeps.adapters` replaces `WorkerDeps.drive`; `media_fetch = None if not adapters else _publish_media_fetch(adapters)`. `_publish_media_fetch(adapters)`: `adapter_for(adapters, intent["provider"])` then `.fetch_bytes(intent["source_config"], …)`; `ProviderNotWired` maps to the terminal class the rung already treats as terminal. `_card_media_fetch(adapters)`: `adapter_for(adapters, media.get("provider", PROVIDER_GDRIVE))`, the source's `config` read by `(workspace_id, source_id)` in its own tenant-scoped read so no `config` ever rides an outbox row, `ProviderNotWired → MediaUnavailable`. `GoogleDriveAdapter` gains a `checkpoint_incomplete` method (today's module function) and accepts the `config` argument. Construction `:864-877`: `adapters = {PROVIDER_GDRIVE: GoogleDriveAdapter(...)}`. `work_loop.py:662-671` parks the sync kinds when `not deps.adapters`, keeping both `@own_transactions` marks; `test_work_loop.py`'s `full_deps()` (`:51`) passes `adapters={"gdrive": object()}` in place of `drive=object()`, or the ratchet at `:59` sees both kinds parked and fails.
3. **The sync.** Add `s.provider` to `_read_source`'s SELECT (`media_sync.py:421`), carry it in `_SyncStart` (`:390-400`) and through `_run_sync`'s call (`:364-372`) into `_call_provider`, and at the call (`:546`) use `adapter = adapter_for(deps.adapters, start.provider)` — the lookup happens before the provider call, with no session open, as the call itself does. `ProviderNotWired` raises and rides the lane's ladder, so an API that runs ahead of the worker during a deploy heals itself instead of flipping the source to error. The chunk chain asks the adapter whether a walk is incomplete (`adapter.checkpoint_incomplete`) instead of Drive's module function (imported at `media_sync.py:55`, used at `:728` and `:777`), and the sync treats the adapter's cursor as opaque (the `walk` carrier at `:454` and `:501`). `first_ingest_chunk` shares `_run_sync`, so nothing else moves.
4. **The intent read.** `publish_pipeline._load` (the SELECT `:397-411`, beside its JOINs `:408-410`) joins `media_sources s ON s.workspace_id = m.workspace_id AND s.id = m.source_id` and selects `s.provider` and `s.config AS source_config`. `media_items.source_id` is NOT NULL and the FK is composite, so the join is total.
5. **The card.** `prompts.render_card` adds `"provider": intent.get("provider", PROVIDER_GDRIVE)` to the media block, and only `_CARD_SELECT` (`prompts.py:297-308`) gains the join. The prompt sweep's other feeder is the SECURITY DEFINER door `fn_prompts_due` (`082_worker_doors.sql:105-121`, read at `prompts.py:476-488`), whose `RETURNS TABLE` cannot gain a column without a migration, a `svc_maintenance` owner bracket and an advertised section. It is not touched: the claim-time re-render (`outbox.py:930-940`, `prompts.rerender_prompt`) rewrites every approval prompt from `_CARD_SELECT` before it is sent, and the `.get` default covers the moment between. The transport forwards the block unchanged.
6. **Docs, copy and comments.** Every `deps.drive` reference becomes `deps.adapters` — the code at `media_sync.py:546`, `work_loop.py:531`, `:667`, `:670`, and the prose at `media_sync.py:13` ("duck-typed on `deps.adapters[provider]`"), `work_loop.py:647` and `google_drive_adapter.py:13`. The stranded-source and failure alerts (`media_sync.py:333`, `:591`) name the source by its label and give a remedy per provider, and never interpolate the exception. `.claude/rules/scheduler.md` names `deps.adapters` and gains an adding-a-provider checklist: the CHECK value, the `vocabulary.py` constant, the registry entry, the API's builder, provisioning, `list_sources`, `_LABEL`, the BFF and the card.
7. **CHANGELOG** under Unreleased: a refactor line.

## Test Plan

- Unit: `tests/src/services/target/test_work_loop.py` — `full_deps()` moves to `adapters`, and `TestEveryProviderFacingExecutorOwnsItsTransactions` stays green with both sync kinds marked. `tests/src/test_worker.py` — `compose(adapters={})` parks the sync kinds and leaves `media_fetch` unwired; `adapters={"gdrive": fake}` wires both; `_publish_media_fetch` and `_card_media_fetch` dispatch by provider, pass the source's `config`, and answer the typed error for an unknown one. `tests/src/services/target/test_prompts.py`: the media block carries `provider`, and an intent row from `fn_prompts_due`, which has no `provider`, renders with the Drive default. Every test that composes the worker with `drive=` moves to `adapters=` (`git grep -n "drive=" tests/`).
- A contract test: a fake second provider whose cursor is not Drive's, registered in the w6 gate, chains two chunks through `checkpoint_incomplete` and is fetched through both seams with its `config`; with the provider unregistered, the sync job retries rather than flipping the source to error.
- Gates: `test_w6_sync_gate.py` and `test_l5_pipeline_gate.py` green before and after (parity); one new l5 case: an intent whose source provider is not wired fails the fetch rung as terminal with the intent's own evidence row.
- Mutation battery `tests/mutations/device_native_04.sh`: the `.get` default on the card, the adapter-owned `checkpoint_incomplete`, `ProviderNotWired` retrying in the sync.

## Verification Checklist

- [ ] `.venv/bin/pytest tests/src/test_worker.py tests/src/services/target/test_work_loop.py tests/src/services/target/test_prompts.py --no-cov -q` green.
- [ ] `git grep -n "deps\.drive\b" src/` returns nothing; `git grep -n "fn_prompts_due" scripts/migrations/` shows no new file; `git grep -n "\"provider\"" src/services/target/publish_pipeline.py src/services/target/prompts.py` shows the join and the block key.
- [ ] The w6 and l5 gates green before the change (the baseline run recorded in the PR) and after, and the contract test green.
- [ ] After deploy, `storydump health` exits 0, and `storydump burst --since <deploy time>` shows no fetch refusal and no failed publish that the pre-merge baseline run did not also show.
- [ ] `tests/mutations/device_native_04.sh` ends `ran N of N mutations` with every mutation killed.

## What NOT To Do

No behaviour change for Drive, no album code, no change to `fn_prompts_due`, no `config` in an outbox row, no renaming of `DriveSourceGone` / `DriveCredentialDead`, no lookup by source id inside the registry, no second construction site for the Drive adapter.

## Context

Area: worker, services · Effort: M · Risk: medium (the publish pipeline's fetch is the hot path the 2026-09-11 investigation lived in) · Priority: high for train two, if F10 keeps it.
