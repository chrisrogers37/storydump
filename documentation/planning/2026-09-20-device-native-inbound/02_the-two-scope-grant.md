---
title: "Device-native inbound — phase 02: the Google grant asks for drive.file beside drive.readonly, and records what was granted (PR 2)"
type: plan
status: active
owner: chris
created: 2026-09-20
tags: [plan, google-drive, oauth, api, web]
links: []
---

> Phase 02 of [`00_EPIC.md`](00_EPIC.md), ratified 2026-09-20 at the forge gate and folded after ironclad cycle 1 (2026-09-25), which split it into two PRs. Spec: [`2026-09-20-device-native-inbound-spec.md`](../2026-09-20-device-native-inbound-spec.md). The ledger is [`RUN_LOG.md`](RUN_LOG.md); its entry for this phase records where the build departs from this text.

## Summary

The Drive connect leg asks for two scopes, the read-only one it asks for today and Google's non-sensitive per-file write scope, and the encrypted payload envelope records the scopes Google actually granted, because the consent screen lets a person grant one and decline the other. The two services deploy separately, and a process that knows only v1 reads a v2 envelope as a dead grant (`google_drive_oauth.py:319-320` → `DriveCredentialDead`), so the phase ships expand then contract: **02a** teaches every reader v2 while every writer still writes the version it read; **02b**, once 02a is live on both services, writes v2 for new grants and exposes `writable`. No migration: the envelope lives inside `encrypted_payload`. A grant without the write scope keeps syncing; the Settings line asking for a reconnect ships with phase 03, when there is something to reconnect for. The phase also brings the verification runbook and the design record in line with a write scope.

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

None. (F4 ratified: the envelope carries the fact.) 02b merges only after `storydump deploys` shows both services running 02a.

### Blocks

Phase 03: the relay job refuses a drop when the grant is not writable, the folder is created under the write scope, and 03's Settings line reads `writable`.

### Steps

**PR 02a — the reader.**

1. **The envelope reader.** `google_drive_oauth.py`: `SCOPE_WRITE = ".../drive.file"`; `decode_payload` accepts `v == 1` (scopes `(SCOPE,)`) and `v == 2` (`{"v": 2, "access_token", "refresh_token", "scopes": [...]}`); `DrivePayload` gains `scopes` and a `writable` property (`SCOPE_WRITE in scopes`). `encode_payload` writes the version of the payload it is given, and `_store_refreshed` (`drive_credentials.py:247`) re-encodes in the version it read, with the stored scopes and never the refresh response's — so a v2 envelope survives a refresh by 02a code, and a rollback of 02b loses nothing. No behaviour changes.

**PR 02b — the writer, after 02a is live on both services.**

2. **Scopes.** `SCOPE_READ = ".../drive.readonly"`, `SCOPES = (SCOPE_READ, SCOPE_WRITE)`; keep `SCOPE = SCOPE_READ` as the name the docs and tests cite. `authorization_url` sends `" ".join(SCOPES)` and `include_granted_scopes=true` so a reconnect keeps what was granted before.
3. **The exchange.** `exchange_code` keeps refusing a grant without `SCOPE_READ` (`scope_not_granted`) and records `granted_scopes: tuple[str, ...]` on `DriveGrant` from the token response's `scope` field (split on spaces; absent → `(SCOPE_READ,)`, the only way an exchange got this far).
4. **The envelope writer.** `PAYLOAD_VERSION = 2`; a new grant is stored as v2 with its scopes.
5. **The status.** `drive_credentials.py`: `async def grant_scopes(session, *, workspace_id) -> Optional[tuple[str, ...]]` — reads and decodes the workspace row through the ring, `None` when no active row. The status route (`v1.py:759-760`) composes `{"drive": {**status, "writable": SCOPE_WRITE in scopes}}`: `writable: null` when there is no grant, and also when the decode fails, so a bad envelope never takes the Drive card down (`drive-card.tsx:178-182`). `workspaces.drive_status` stays SQL-only.
6. **The callback.** When the stored grant lacks the write scope, the redirect (`auth.py:377-379`) carries `drops=declined` beside `connected=gdrive`, for phase 03's banner. `DriveStatus` gains `writable: boolean | null` and `driveConnectControl` learns it; nothing new renders until phase 03.
7. **The runbook.** Rewrite `documentation/operations/google-oauth-verification.md` for a write scope: the pre-submission checklist; the scope list (`:66-71`) with `drive.file` and its justification (the app writes only into the one "Telegram drops" folder it creates); the "read-only, no writes" gist of the justification copy (`:76-77`); the demo-video script, which must now show a drop landing in that folder; and a `drive.file` row in the scopes table, class Non-sensitive. Restate the #327 rationale in `google_drive_oauth.py:22-29` and `test_google_drive_oauth.py:48-52`: `drive.file` alone still cannot list a pre-existing folder, which is why both scopes are asked. Do not reclassify the `drive.readonly` row (the owner's side finding).
8. **The design record.** In `03-decision-record.md`, a post-ratification ruling amends the 2026-09-05 ruling's "same scope" clause (`:201`); in `07-security-model.md` §15 (`:905-914`), the prose that says the grant is exactly `drive.readonly` gains the write scope. Both cite the decision doc. Both edits are prose outside SQL fences, so the advertised-DDL pin is unaffected; the docs guard battery proves it.
9. **CHANGELOG** in each PR; 02b's PR note says every workspace reconnects once to enable drops, and that nothing asks them to until phase 03 ships.

## Test Plan

- 02a unit (`test_google_drive_oauth.py`, `test_drive_workspace_grant.py`): a v1 envelope decodes with `writable` false; a v2 envelope decodes with its scopes; `encode_payload` round-trips each version; a refresh of a v2 envelope re-encodes v2 with the stored scopes, and of a v1 envelope re-encodes v1.
- 02b unit: the URL carries both scopes and `include_granted_scopes`; an exchange granting both → `writable` true; granting read-only only → the grant is kept and `writable` false; no read scope → `scope_not_granted`; a new grant is stored as v2; `grant_scopes` on a v1 row, a v2 row and an undecodable row (→ `writable: null`).
- Routes: `TestDriveConnect` asserts both scopes in the URL; the status route answers `writable`; `TestDriveCallback` stores a v2 envelope and redirects with `drops=declined` when the write scope was declined.
- Gate (`test_gdrive_oauth_gate.py`): as `svc_ingress`, the stored ciphertext decodes as v2 with scopes; an existing v1 row and a v2 fixture row both serve the read door and survive a refresh in their own version.
- Mutation battery `tests/mutations/device_native_02.sh`: the version-preserving re-encode, the v2 decode, the `writable` null on a decode failure.

## Verification Checklist

- [ ] `.venv/bin/pytest tests/src/services/target/test_google_drive_oauth.py tests/src/services/target/test_drive_workspace_grant.py tests/src/api/test_v1_routes.py tests/src/api/test_auth_routes.py -k "Drive or drive" --no-cov -q` green, for each PR.
- [ ] The gate green on the real database, a v1 and a v2 fixture row included.
- [ ] Before 02b merges, `storydump deploys` shows the API and the worker both on 02a's commit.
- [ ] On a dev client the consent screen lists both scopes; declining the second yields an active grant with `writable: false` and the `drops=declined` redirect.
- [ ] `tests/mutations/device_native_02.sh` ends `ran N of N mutations` with every mutation killed.
- [ ] `git diff --stat scripts/migrations` is empty; the docs guard battery is green with the runbook and the design record amended.

## What NOT To Do

Do not ask for the full `drive` scope or build the Picker. Do not refuse a grant that lacks `drive.file`. Do not add a column for the scopes (F4). Do not write a v2 envelope from a process that some deployed process cannot read. Do not reclassify `drive.readonly` in the runbook. Do not render the reconnect line in this phase.

## Context

Area: services (`google_drive_oauth`, `drive_credentials`), API, web types, runbook, design record · Effort: M across two PRs · Risk: medium (a consent change every workspace sees once, and a deploy-order constraint) · Priority: high.
