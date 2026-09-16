---
title: "storydump v2 CLI — the developer and agent console over the target API (epic)"
type: plan
status: completed
owner: chris
created: 2026-09-15
updated: 2026-09-15
tags: [plan, cli, api, auth, devx, legacy-retirement, epic]
links: []
---

## Summary

Three PRs deliver the CLI the approved spec describes (`../2026-09-15-cli-v2-spec.md`): a pure
HTTP client of the target API named `storydump`, with **one pathway** — tokens, then the read
views the last two days of production validation needed, then the command verbs, the environment
verbs and the deletion of the legacy `cli/`. Every write is attributable to a person the token is
bound to; a service identity reads; every read is tenant-scoped under the principal's
memberships; nothing in the package can open a database connection. This epic holds the
architecture, the decision forks, the risks and the sequencing; each phase doc is one PR.
Hardened 2026-09-15 by ironclad cycle 1 (four lenses on Opus: adversarial, engineering, devex,
plan health): six blockers and eleven risks folded, ten forks locked, converged.

## Evidence

- Nothing deployed runs the legacy tier (the worker on `WORKER_IMPL=target` since 2026-08-24,
  the API since 2026-08-31); `src/main.py` is the only importer of `src/services/core`; the
  legacy CLI (`cli/`, 31 commands, `setup.py:32-34`) is its last consumer.
- `src/api/principal.py:45` `Principal(session_id, user_id)`; `presented_token` reads
  `Authorization: Bearer` first; `current_principal` (`:152`) resolves sessions only; all 28 v1
  routes take `Depends(current_principal)`.
- `src/services/target/commands.py:337-346` — `commands.execute` authorizes every write with
  `authorize_member(…, command.actor_user_id, floor)`, a `workspace_members` lookup
  (`tenant_resolution.py:138-148`); `tenant_resolution.ROLE_ORDER` (`:62`) is `member < admin <
  owner`, no operator rung; `commands.FLOORS` (`:90`) names an `operator` floor nothing in the tier
  can produce (`:116-120`, the unbuilt #1124).
- `scripts/migrations/060_auth_plane_tables.sql` — `service_tokens` (name, hash, role
  `operator|readonly`, `workspace_id NULL`, expiry, revocation, last use) with
  `p_auth_ingress_svctok` for `svc_ingress`; `055:199-203` audit actor kinds and channels (`cli`
  allowed); `056:229-240` `rate_counters` has no `workspace_id` and a `USING (true)` policy
  (`058:175-176`); `p_jobs` admits `workspace_id IS NULL` rows (`058:159-162`).
- `tests/scripts/conftest.py:595-621` `replay_advertised_stream` replays the advertised DDL into
  a fresh database that has no schema `runner`; `scripts/migration_runner.py:79-81` creates
  `runner.schema_migrations` at first contact.
- `src/api/routes/v1.py:110-116` the web's idempotency key is `<command>:<intent_id>`;
  `src/api/app.py:324-327` a replay answers `{"outcome": "replayed"}`; `:170` maps `not_a_member`
  to 404; `:279-291` only `CommandRefused` carries a `reason`.

## Architecture

```
storydump (Click, httpx)                      the API (FastAPI, svc_ingress under RLS)
  login / whoami / tokens list|revoke ─sdt_─▶  current_principal: prefix → service_tokens.resolve
  story / cards / floating / account            └─ Principal(kind=token, user, role, token id+name)
  jobs / outbox / burst / posture ──────────▶  /ops/workspaces/{ws}/<view>   (tenant-scoped SQL)
  approve … resolve / pause / sync ─────────▶  POST /workspaces/{ws}/commands/{command}
  health ────────────────────────────────────▶  /health, /health/scheduling, /health/posting
  deploys ──────────▶ Railway (F1)             webhook ──────────▶ Telegram Bot API (env token)
  doctor: token · API · Railway · config · migration ledger
  (the web's Settings › API tokens panel mints; a token never mints)
```

- **Principal kinds** (`src/api/principal.py`): a session, or a token. A token is
  **person-bound** (`user_id` set, `workspace_id` NULL: acts as the person across their
  memberships, never above the membership role, may read and write) or a **workspace service
  identity** (`workspace_id` set, `user_id` NULL: reads only in v1 — F10). Tokens are admitted to
  an explicit route allowlist; every other route stays session-only.
- **The command path is unchanged**: `_dispatch` → `commands.ingest` → the executors; the
  `Command`'s actor is the person the token is bound to, its label the token's name, the channel
  `cli` (set as the transaction's GUCs by `_open_tenant`). One direct `audit_events` row per CLI
  command, written by `_dispatch`, carries the token's id and name and the command's
  `external_ref` (F4).
- **Reads are ordinary tenant-scoped queries** run once per workspace the principal may read;
  no SECURITY DEFINER door; no table outside RLS is read. `posture` reads catalogs and the
  runner's ledger under a grant the runner itself makes (F7).
- **Third-party systems keep their own pathway**: Railway for deploys, Telegram's Bot API for
  the webhook tool.

## Decision Forks

**F1 — How `deploys` reaches Railway.** Context: `deploys [--watch]` reports each service's
latest deployment and commit; the developer machine is logged in to the `railway` CLI, and
another session's `railway login` is known to drop this machine's project link. Options: (a)
shell out to `railway deployment list --service … --json`, verifying `railway whoami` and the
linked project id first, with a version-stamped fixture in tests; (b) Railway's GraphQL API with a
provisioned project token; (c) no `deploys` in v1. Lean: (a) now, (b) when a token is provisioned.
Ratifier: owner. Status: locked — (a) hardened, owner 2026-09-15 (ironclad cycle 1).

**F2 — Where the client keeps its token.** Context: a person-bound bearer secret good for 90 days
across the person's workspaces; agents and CI use a variable; the repo's CI is headless.
Options: (a) keychain via `keyring` as first written; (b) a 0600 file by default, keychain as an
extra; (c) environment only; (d) keychain by default, fail closed (no plaintext fallback), a 0600
file by explicit opt-in, `STORYDUMP_TOKEN` overriding, `keyring` in the `storydump[cli]` extra,
secret from prompt or stdin. Lean: (d). Ratifier: owner. Status: locked — (d), owner 2026-09-15
(`decisions/F2-token-storage.md`, weigh-development-paths).

**F3 — Token format.** Context: `presented_token` reads `Authorization: Bearer` as a session
value. Options: (a) a prefixed opaque secret `sdt_<43 url-safe chars>`, routed on the prefix,
recognisable to secret scanners; (b) unprefixed, dual lookup. Lean: (a). Ratifier: owner.
Status: locked — (a), owner 2026-09-15.

**F4 — How the token's name reaches the audit trail.** Context: the ledger's audit rows are
written by triggers from three GUCs (`app.actor_kind`, `app.actor_user_id`, `app.channel`); no
label GUC. Options: (a) one direct `audit_events` row per CLI command (`event: cli_command`, the
token's id and name, the command kind and its `external_ref`), written by the API adapter
(`_dispatch`), the shape `publish_pipeline._audit_deferral` uses; `svc_ingress` already holds
`INSERT ON audit_events` (`057:115`); (b) a `app.client_label` GUC read by four trigger bodies
through the ratchet; (c) logs only. Lean: (a). Ratifier: owner. Status: locked — (a) written in
the adapter, `external_ref` in both rows, owner 2026-09-15 (ironclad cycle 1).

**F5 — CLI framework.** Options: (a) Click 8 (a dependency, the legacy CLI's framework), with
`standalone_mode=False` so exit codes are the CLI's, not Click's; (b) Typer; (c) argparse.
Lean: (a). Ratifier: owner. Status: locked — (a), owner 2026-09-15.

**F6 — Route shape for the views.** Options: (a) `src/api/routes/ops.py`:
`/ops/workspaces/{ws}/<view>` plus `/ops/posture` — one file the security review reads; (b)
inside the v1 router. Lean: (a). Ratifier: owner. Status: locked — (a), owner 2026-09-15.

**F7 — `posture`'s ledger read under the F.4 role.** Context: the runner's ledger
`runner.schema_migrations` is created by the runner at first contact, outside the advertised
stream; the gates replay that stream into a database with no `runner` schema, so a grant in a
migration fails every gate. Options: (a) `GRANT` in migration 077 (breaks the replay harness);
(b) a door; (c) the runner grants `SELECT` on its ledger to `svc_ingress` in the same step that
creates it, nothing in the stream. Lean: (c). Ratifier: owner. Status: locked — (c), owner
2026-09-15 (re-ratified after ironclad cycle 1; (a) was locked and reopened on evidence).

**F8 — When the legacy `cli/` goes.** Options: (a) PR 3, after the new verbs exist; (b) PR 1,
before anything new lands. Lean: (a). Status: locked — (a), owner 2026-09-15 (kindle ruling).

**F9 — One pathway per system.** Options: (a) our system only through the API, Railway and
Telegram through their own APIs, `psql` the runbook's escape hatch; (b) a database pathway in the
CLI for reads. Lean: (a). Status: locked — (a), owner 2026-09-15 (spec approval).

**F10 — Can a workspace service identity write?** Context: the command port authorizes writes by
workspace membership (`commands.py:337-346`); a service identity has no membership and the port
has no operator rung (#1124). Options: (a) service identities are **read-only** in v1; writes
are person-bound tokens; no port change; a write-capable service principal is its own later PR;
(b) build a non-user principal in the port in phase 01; (c) drop service identities from v1.
Lean: (a). Ratifier: owner. Status: locked — (a), owner 2026-09-15 (ironclad cycle 1).

## Companion Plans

- `../2026-09-15-cli-v2-spec.md` — the approved spec; `01_tokens.md`, `02_reads.md`,
  `03_writes-and-deletion.md` — the phases; `decisions/F2-token-storage.md`; `probes/*.sql` —
  the read views' definitions as validated against production 2026-09-13..15.
- `../2026-08-02-consolidated-design-plan/04-execution-sequence.md` — X.2 (service tokens, CLI
  over the API); `07-security-model.md` §1 and §7; `02-domain-model.md` §7.
- #1216 — retire the legacy tier: this epic delivers its step 4 and the `cli/` part of step 3.
  #1124 — the operator principal in the command port (what F10(b) would have been).
- `../2026-09-09-telegram-interaction-at-throughput/` — the command port, admission and audit
  the CLI's writes ride; `../investigations/publish-first-fetch_2026-09-11/` — the float.

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| A view written against today's owner connection fails under F.4 (`svc_ingress`, RLS) | the reads break on the posture switch | every view gate runs as `svc_ingress` with the tenant GUC; a second workspace's rows must be absent; no table outside RLS is read |
| A leaked person-bound token exposes every workspace the person belongs to | wider blast radius than a service identity | 90-day default expiry, `last_used_at`, `tokens revoke`, the `cli_command` row; keychain by default (F2) |
| Widening the principal check admits tokens where they must not go | a token accepts invitations or drives OAuth legs | tokens admitted only to an allowlist (`/ops/*`, the command route, `/me/principal`, own tokens); a gate proves every other route refuses a token |
| The bearer path change touches the web's session-as-bearer | sign-in regressions | the `sdt_` prefix routes only tokens; the session path is byte-for-byte unchanged and its tests stay |
| The never-run lists in `CLAUDE.md`/`AGENTS.md` are safety rules | a stale or weakened guard | phase 3 rewrites both lists, walks nested Click groups in the doc test, and sweeps every doc that names `storydump-cli` with a grep-for-zero test |
| A re-run write executes twice | a second publish | the CLI's idempotency key is deterministic (`<command>:<intent>`), a replay prints "already done"; `--idempotency-key` for a deliberate repeat |
| `deploys` depends on the `railway` CLI and its link | a verb that answers for the wrong account | `whoami` and the linked project id checked; `doctor` reports both and the version |
| Scope creep into the wider tear-out | the third PR balloons | phase 3 deletes `cli/` and its references only; a retired-verbs table names each command's disposition; #1216 owns the rest |

## Complexity and Sequencing

| Phase | Size | Depends on | Parallel with |
|---|---|---|---|
| 01 Tokens | L | none | — |
| 02 Reads | L | 01 | 01 (its queries may be drafted from `probes/` while 01 is in review) |
| 03 Writes, environment, deletion | M | 01, 02 | — |

Critical path: 01 → 02 → 03. Each phase is one PR under the repository's process: tests written
red first, two review lenses, a named mutation per behaviour in `tests/mutations/<phase>.sh`
committed with the PR, CI green, admin squash; a migration ships with its `07` block, manifest
row and lineage-list entry.

## Implementation Plan

### Dependencies
The approved spec; main at or after `5ea4ea6`.

### Blocks
#1216 steps 3 (the `cli/` part) and 4; the F.4 posture switch gains its first API-side consumer
proven as the login role.

### Steps
1. Phase 01 — `01_tokens.md`.
2. Phase 02 — `02_reads.md`.
3. Phase 03 — `03_writes-and-deletion.md`.
4. After 03 merges: the consolidated plan's Live status (X.2's token and CLI items ✅; #1216
   step 4 ✓) and this epic's status to completed.

## Test Plan

Per phase. Across the epic: the import-boundary test (the package reaches `src` only through
the vocabulary module and imports no database driver) from phase 01; the envelope and error
schema tests from phase 01; the route-allowlist gate from phase 01.

## Verification Checklist

- [x] `storydump login` with a token minted on the web's Settings › API tokens; `whoami` names
      the person, the effective role per workspace and the token — proven on the demo rig with a
      token minted through the API (phase 01 and 03 live samples, `RUN_LOG.md` §5); the mint on
      the production web by the owner is queued (§7).
- [x] `storydump story <id> --json` returns the envelope for a real story; `--watch` prints only
      changes; a workspace the principal is not a member of is absent — the ops gate's three arms
      and the phase 02 live sample (`--workspace <uuid not a member>` → exit 3).
- [x] `storydump skip <id>` with a `readonly` token exits 3; with a person-bound `operator` token
      the story is skipped, its audit row reads `channel = 'cli'`, and the `cli_command` row names
      the token and the same `external_ref`; running it again prints "already done" and exits 0 —
      `tests/scripts/test_cli_writes_gate.py` (the real CLI against the real app as `svc_ingress`)
      and the phase 03 live sample; against production with an owner-minted token: queued (§7).
- [x] `cli/` is gone; `storydump-cli` is not a command; no file in the repository names it (the
      CHANGELOG, the archive, the dated updates, this plan and the owner's `.claude/settings.json`
      excepted — `tests/test_legacy_cli_gone.py`); the doc test walks nested groups and passes.
- [x] All three PRs merged with the process above; the Live status updated — #1310 (`218c864`),
      #1311 (`0b0badc`) and #1312 (`4bce602`), each live on Railway; the Live status line landed
      in #1312 (`RUN_LOG.md` §4–§5).

## What NOT To Do

- No database pathway in the CLI, ever — not even for `posture`.
- No SECURITY DEFINER door for a read view; no read of a table outside RLS.
- No token minted by a migration, a script, or another token; no service identity that writes.
- No change to the command port's semantics; the adapter carries the token's facts.
- No deletion beyond `cli/`, its console script, its Makefile targets, its tests and its mentions.

## Context

area: API auth, operator surface, CLI, legacy retirement · effort: XL (three L/L/M PRs) ·
risk: medium (auth on the money path; the posture switch) · priority: P1
