---
title: "Device-native inbound — phase 02: the Google grant asks for drive.file beside drive.readonly, and records what was granted (PR 2)"
type: plan
status: active
owner: chris
created: 2026-09-20
tags: [plan, google-drive, oauth, api, web]
links: []
---

> Phase 02 of [`00_EPIC.md`](00_EPIC.md), ratified 2026-09-20 at the forge gate. Spec: [`2026-09-20-device-native-inbound-spec.md`](../2026-09-20-device-native-inbound-spec.md). The ledger is [`RUN_LOG.md`](RUN_LOG.md); its entry for this phase records where the build departs from this text.

## Summary

The Drive connect leg asks for two scopes, the read-only one it asks for today and Google's non-sensitive per-file write scope. The encrypted payload envelope moves to v2 and records the scopes Google actually granted, because the consent screen lets a person grant one and decline the other; a v1 envelope keeps decoding as read-only. The Drive status the web reads gains `writable`, and the Settings card shows "Reconnect to enable Telegram drops" on a grant without the write scope. No migration: the envelope lives inside `encrypted_payload`. A grant without the write scope keeps syncing; only drops (phase 03) need the reconnect.

## Evidence

- `src/services/target/google_drive_oauth.py:91` `SCOPE = ".../drive.readonly"` (one scope); `:95` `PAYLOAD_VERSION = 1`; `:120-134` `REDIRECT_REASON`, the total refusal vocabulary (`DriveOAuthRefused` refuses any key outside it, `:146-147`); `:156-164` `DriveGrant(access_token, refresh_token, expires_at)` whose docstring says the granted scope "is checked at the exchange and not carried"; `:167-174` `DrivePayload(access_token, refresh_token)`.
- `:177-190` `authorization_url` — `scope: SCOPE` `:185`, `access_type=offline` `:187`, `prompt=consent` `:188`. `:193-240` `exchange_code` — refresh token required `:223-229`; the scope check `:230-234` is membership only (`SCOPE not in granted.split()` → `scope_not_granted`), nothing recorded.
- `:299-308` `encode_payload` (`{"v", "access_token", "refresh_token"}`); `:311-327` `decode_payload` (rejects a version other than 1 at `:319-320`); `:330-345` `connect_purpose`; `:348-387` `store_credential` (the upsert `:362-379`, `ring().encrypt(encode_payload(grant))` `:383`).
- `src/services/target/drive_credentials.py:89-163` `token_for_workspace`, the read door: decodes at `:145`, refreshes when stale `:153-162`; `:166-244` `_refresh` (compare-and-swap on the seen ciphertext); `:247` `_store_refreshed`; `:298-308` `provider_from_engine`.
- Routes: `src/api/routes/v1.py:763-799` `connect_drive` (`principal.admin_session` at `:783`, `issue_state(... reconnect_target)` `:787-794`, returns the URL); `src/api/routes/auth.py:301-379` `google_drive_callback` (exchange `:340-346`, refusal mapping `:352-357`, `store_credential` `:369-371`, `rearm_after_connect` `:375`, redirect `:377-379`); `v1.py:751-760` the status route (member floor) → `workspaces.drive_status` (`src/services/target/workspaces.py:300-326`, answers `status` and `connected_at` from `oauth_credentials` alone).
- Web: `landing/src/lib/dashboard-payloads.ts:146-149` `DriveStatus`; `landing/src/lib/drive.ts:84-93` `driveStatusBadge`, `:101-117` `driveConnectControl`, `:37` `requestDriveConnect`. The Settings UI was split by the sprint: `landing/src/components/dashboard/settings/drive-card.tsx:63-64` the badge and control, `:80-91` `connectDrive` (its button `:207`), `:121-136` `disconnectDrive` (its button `:213-222`); `integrations-tab.tsx` (114 lines) holds only the shared banner state; the Telegram half is `telegram-card.tsx`.
- The runbook lists the scopes requested in a table (`documentation/operations/google-oauth-verification.md:9-16`) and is pending submission (`:3`).
- Tests: `tests/src/services/target/test_google_drive_oauth.py` — `TestAuthorizationUrl:36`, `TestExchangeCode:61` (`:112` a narrowed scope is refused; `:141` every reason has a redirect), `TestPayloadEnvelope:164`, `TestRefreshAccessToken:191`; the read door in `test_drive_workspace_grant.py` (`:430` `TestTheWorkspaceStatusProjection`); `tests/src/api/test_v1_routes.py:755` `TestDriveConnect`; `tests/src/api/test_auth_routes.py:741` `TestDriveCallback`; gate `tests/scripts/test_gdrive_oauth_gate.py` (`TestTheCredentialRow:118`, `TestTheRoutePairAsSvcIngress:383`).

## Implementation Plan

### Dependencies

None. (F4 ratified at the epic gate: the envelope carries the fact.)

### Blocks

Phase 03: the relay job refuses a drop when the grant is not writable, and the folder is created under the write scope.

### Steps

1. **Scopes.** `google_drive_oauth.py`: `SCOPE_READ = ".../drive.readonly"`, `SCOPE_WRITE = ".../drive.file"`, `SCOPES = (SCOPE_READ, SCOPE_WRITE)`; keep `SCOPE = SCOPE_READ` as the name the docs and tests cite. `authorization_url` sends `" ".join(SCOPES)` and `include_granted_scopes=true` so a reconnect keeps what was granted before.
2. **The exchange.** `exchange_code` keeps refusing a grant without `SCOPE_READ` (`scope_not_granted`) and records `granted_scopes: tuple[str, ...]` on `DriveGrant` from the token response's `scope` field (split on spaces; absent → `(SCOPE_READ,)`, the only way an exchange got this far).
3. **The envelope.** `PAYLOAD_VERSION = 2`; `encode_payload` writes `{"v": 2, "access_token", "refresh_token", "scopes": [...]}`; `decode_payload` accepts `v == 1` (scopes `(SCOPE_READ,)`) and `v == 2`; `DrivePayload` gains `scopes` and a `writable` property (`SCOPE_WRITE in scopes`). `_store_refreshed` (`drive_credentials.py:247`) re-encodes with the stored scopes, never the refresh response's.
4. **The status.** `drive_credentials.py`: `async def grant_scopes(session, *, workspace_id) -> Optional[tuple[str, ...]]` — reads and decodes the workspace row through the ring, `None` when no active row. the status route (`v1.py:759-760`) composes `{"drive": {**status, "writable": SCOPE_WRITE in scopes}}` (`writable: null` when there is no grant). `workspaces.drive_status` stays SQL-only.
5. **The web.** `DriveStatus` gains `writable: boolean | null`; `driveConnectControl(status, writable)` returns the reconnect control with the label "Reconnect to enable Telegram drops" when `status === "active" && writable === false`; the Drive card (`drive-card.tsx`) renders one line under the badge in that state. Nothing else on the card changes.
6. **The runbook.** Add the `drive.file` row to the scopes table, class Non-sensitive, with one sentence that the submission lists both. Do not touch the `drive.readonly` row's class (the owner's side finding).
7. **CHANGELOG** under Unreleased; a PR note that every workspace reconnects once to enable drops.

## Test Plan

- Unit (`test_google_drive_oauth.py`): the URL carries both scopes and `include_granted_scopes`; an exchange granting both → `writable` true; granting read-only only → the grant is kept and `writable` false; no read scope → `scope_not_granted`; a v1 envelope decodes with `writable` false; a v2 round trip; a refresh keeps the stored scopes. `test_drive_workspace_grant.py`: `grant_scopes` on a v1 row and a v2 row.
- Routes: `TestDriveConnect` asserts both scopes in the URL; the status route answers `writable`; `TestDriveCallback` stores a v2 envelope.
- Gate (`test_gdrive_oauth_gate.py`): the stored ciphertext decodes as v2 with scopes as `svc_ingress`; an existing v1 row still serves the read door.

## Verification Checklist

- [ ] `.venv/bin/pytest tests/src/services/target/test_google_drive_oauth.py tests/src/services/target/test_drive_workspace_grant.py tests/src/api/test_v1_routes.py -k "Drive or drive" --no-cov -q` green.
- [ ] The gate green on the real database, a v1 fixture row included.
- [ ] On a dev client the consent screen lists both scopes; declining the second yields an active grant with `writable: false` and the reconnect line on the card.
- [ ] `git diff --stat scripts/migrations` is empty.

## What NOT To Do

Do not ask for the full `drive` scope or build the Picker. Do not refuse a grant that lacks `drive.file`. Do not add a column for the scopes (F4). Do not reclassify `drive.readonly` in the runbook.

## Context

Area: services (`google_drive_oauth`, `drive_credentials`), API, web · Effort: M · Risk: medium (a consent change every workspace sees once) · Priority: high.
