---
title: "CLI v2 — phase 01: service tokens, bearer authentication, login (PR 1)"
type: plan
status: completed
owner: chris
created: 2026-09-15
tags: [plan, cli, api, auth]
links: []
---

## Summary

Make the API accept a token as a principal: one token model with two principal kinds — a
person-bound token that reads and writes as the person, a workspace service identity that reads
— minted only from a signed-in session on the web, hash-only, expiring, revocable; bearer
authentication that routes on the token's prefix, admits tokens to an explicit route allowlist
and leaves the session path untouched; the `Command` carrying the person, the token's name and
the `cli` channel; and the CLI package's skeleton with `login`, `whoami` and `tokens list|revoke`.
Everything a later phase reads or writes through the API authenticates this way.

## Evidence

*(State on 2026-09-15: the lineage now ends at 080, `07` §23 is 077, and `setup.py` carries only `storydump=storydump_cli.main:main`.)*

- `scripts/migrations/060_auth_plane_tables.sql` — `CREATE TABLE service_tokens (id, name,
  token_hash, role CHECK IN ('operator','readonly'), workspace_id UUID NULL, expires_at,
  revoked_at, last_used_at, created_at, updated_at, UNIQUE (token_hash))`; line 152
  `p_auth_ingress_svctok … FOR ALL TO svc_ingress USING (true)`. No `user_id`; no route mints.
- `src/models/target/auth_plane.py:102` — the `ServiceToken` model.
- `src/api/principal.py:45` `Principal(session_id, user_id)`; `presented_token` reads the bearer
  first, then the cookie; `current_principal` (`:152`) resolves sessions only; every v1 route
  depends on it (`src/api/routes/v1.py`, 28 routes, including `POST /invitations/{token}/accept`
  `:952` and the Drive and account connect legs `:725`, `:840`, `:891`).
- `src/services/target/sessions.py:73` `token_hash`, `:83` `issue`, `:100` `resolve` (refuses a
  non-`active` user, `:126-127`), `:132` `revoke` — the pattern the token service copies.
- `src/api/routes/v1.py:110` `CHANNEL = "web"`; `:112-116` the idempotency key
  `<command>:<intent_id>`; `:126-136` `_open_tenant` sets `actor_kind="user", channel=CHANNEL`
  as GUCs (what the audit triggers read, `055:337-339`); `:185` `_dispatch`; `:932` `run_command`.
- `src/services/target/commands.py:337-346` writes authorize by membership;
  `tenant_resolution.py:62` `ROLE_ORDER`;
  `:172` `REASONS`; `:193` `Command` (`actor_user_id: str`, `actor_label`, `binding_id`,
  `card_ref`). `src/api/app.py:170` maps `not_a_member` to 404; `:279-291` only `CommandRefused`
  carries `reason`; `:324-327` a replay answers `{"outcome": "replayed"}`.
- `src/services/target/identity.py:211-217` `GET /me` is user-shaped.
- `scripts/migration_runner.py:79-81` creates `runner.schema_migrations` at first contact;
  `tests/scripts/conftest.py:595-621` replays the advertised stream into a fresh database.
- Ratchets: manifest ordinal 20 is `07` §22 (076); `tests/scripts/test_advertised_ddl.py:289`
  pins 34; `tests/scripts/test_lineage_lane.py:368` ends at 076; `scripts/tenancy_gate.py:295`
  classifies `ADD COLUMN`, `:326` `ADD CONSTRAINT … CHECK`; `CREATE INDEX` is irrelevant-listed.
- `setup.py:32-34` `storydump-cli=cli.main:cli`; `install_requires` ships to the API and worker;
  `requirements.txt` has `click`, `httpx`, `rich`; `keyring` is in neither.
- The web app (`landing/`) has Settings › Integrations (the Telegram link, Drive), the pattern the
  tokens panel follows.

## Implementation Plan

### Dependencies
None (main at or after `5ea4ea6`).

### Blocks
Phases 02 and 03.

### Steps

1. **Migration `scripts/migrations/077_service_token_subject.sql`**, mirrored as `07` §23 with a
   manifest row (ordinal 21; `test_advertised_ddl` pin 34 → 35), appended to the lineage list:
   - `ALTER TABLE service_tokens ADD COLUMN user_id UUID NULL REFERENCES users(id) ON DELETE CASCADE;`
   - `ALTER TABLE service_tokens ADD CONSTRAINT ck_service_token_subject CHECK ((user_id IS NULL) <> (workspace_id IS NULL));`
     — exactly one subject; the 060 comment's "NULL = all workspaces" reading is retired in the
     prose beside the block.
   - `CREATE INDEX ix_service_tokens_user ON service_tokens (user_id) WHERE user_id IS NOT NULL;`
   - `-- runner:postcondition` lines for the column, the constraint and the index (a constraint
     landing on a hand-touched production table gets its own postcondition, as 060 does).
   No grant in this migration (F7). Update `src/models/target/auth_plane.py`.
2. **The runner grants its own ledger (F7c)** — `scripts/migration_runner.py`, in the step that
   creates `runner.schema_migrations`: `GRANT USAGE ON SCHEMA runner TO svc_ingress; GRANT SELECT
   ON runner.schema_migrations TO svc_ingress;` (idempotent, outside the advertised stream). A
   runner test asserts the grant; the replay harness is untouched.
3. **`src/services/target/vocabulary.py`** — dependency-free closed sets and shapes: command
   kinds, `REASONS`, intent states, token roles, audit channels, the CLI exit codes
   (`0 ok · 1 not found · 2 refused · 3 not authorized · 4 API unreachable · 5 Railway
   unreachable · 6 a watched condition ended in failure · 64 usage`), the JSON envelope
   `{"v": 1, "kind": <verb>, "data": <shape> | null, "error": null | {"code", "reason",
   "detail", "fix"}}` and its per-verb `data` shapes; the CLI's sentences per reason and outcome
   (never the Telegram adapter's words). `commands.py` imports and re-exports `REASONS`. Unit
   tests: the sets equal the migrations' `CHECK` lists (as built `tests/src/services/target/test_vocabulary.py` — the models test went with the legacy tier);
   the envelope and error shapes are schema-tested.
4. **`src/services/target/service_tokens.py`** — `mint(conn, *, name, role, user_id=None,
   workspace_id=None, expires_in_days=90) -> tuple[str, dict]` (secret `sdt_` + 32 url-safe random
   bytes; SHA-256 stored; returned once); `resolve(conn, *, token_hash) -> TokenPrincipal`:
   refuses unknown, revoked, expired; for a person-bound token joins `users` and refuses a
   non-`active` user (the gate sessions enforce, `sessions.py:126-127`); for a service identity
   requires an active workspace; stamps `last_used_at`; `list_for_user`, `list_for_workspace`,
   `revoke(conn, *, token_id, by)` (a person their own, an admin the workspace's).
5. **`src/api/principal.py`** — `Principal` gains `kind: Literal["session","token"]`,
   `token_id`, `token_name`, `token_role`, `token_workspace_id`, `channel` (`web` | `cli`).
   `current_principal`: a bearer value starting with `sdt_` resolves through
   `service_tokens.resolve`; any other value takes today's session path unchanged. A person-bound
   token yields the person's `user_id`; a service identity yields `user_id=None`.
6. **The route allowlist.** A `require_session` dependency (session principals only) on every v1
   route that is not in the token allowlist; the allowlist is `/ops/*` (phase 02),
   `POST /workspaces/{ws}/commands/{command}`, `GET /me/principal` (new: the principal's kind,
   subject, role and readable workspaces — user-shaped `GET /me` stays session-only),
   `GET /me/tokens` and `DELETE /me/tokens/{id}` for a person-bound token's own subject, and the
   workspace token list/revoke for a service identity's own workspace. A gate enumerates the
   router and proves every route outside the allowlist answers 403 `session_required` to a token.
7. **Role ceiling and the write rule** in `_open_tenant`: a service identity is refused on every
   write route and on any workspace but its own (403 `readonly_token` / `wrong_workspace`); a
   person-bound token acts as the person (the membership check is `commands.execute`'s own,
   unchanged); a `readonly` person-bound token is refused on the command route (403
   `readonly_token`); the effective role is the lesser of the token's role and the membership
   role for anything the adapter itself decides.
8. **The GUCs and the audit row** — `_open_tenant` sets `app.actor_kind` (`user` for a session or
   a person-bound token; `operator` for a service identity's reads), `app.actor_user_id` and
   `app.channel` from the principal (its own step, gated by the written audit row). `_dispatch`
   builds `Command(actor_user_id=principal.user_id, channel=principal.channel,
   actor_label=principal.token_name)` — `actor_user_id` stays `str` (F10 makes every write a
   person) — and, for a token principal, writes one direct `audit_events` row in the same
   transaction: `detail = {"v": 1, "event": "cli_command", "kind", "token_id", "token_name",
   "external_ref"}`, the idempotency key also stored on the command's own audit trail so the two
   rows pair (F4). `commands.ingest(..., principal=principal.session_id or principal.token_id)`.
9. **Routes** (session principals only): `POST /me/tokens` (body `name`, `role`,
   `expires_in_days`) → `{id, name, role, expires_at, secret}`; `GET /me/tokens`;
   `DELETE /me/tokens/{id}`; `POST /workspaces/{ws}/tokens` (admin or owner; a service identity,
   `readonly` only in v1), `GET`, `DELETE /workspaces/{ws}/tokens/{id}`.
10. **The web's Settings › API tokens panel** (`landing/`): mint (the secret shown once, with a
    copy control and the `storydump login` hint), list, revoke — through the routes above with the
    session cookie, beside Settings › Integrations. This is the first step of the first-time
    clock; a token never mints a token.
11. **`storydump_cli/`** — `main.py` (a Click group with `standalone_mode=False`; usage errors
    exit 64, `Abort` maps to the verb's exit), `client.py` (httpx; base URL from
    `~/.config/storydump/config.toml` or `STORYDUMP_API`; the bearer from the storage backend;
    refusals keyed off the body's `reason`, `not_a_member` → 3, 401/403 → 3, 409/422 → 2 with
    the reason, connection errors and 5xx → 4), `storage.py` (F2: an injectable backend —
    keychain via `keyring` by default, refusing `keyrings.alt` and any plaintext fallback; a 0600
    file under `~/.config/storydump/` only with `--insecure-storage`; `STORYDUMP_TOKEN` overrides
    both), `output.py` (the envelope, `rich` tables, exit codes, redaction of `sdt_…` values,
    database URLs and webhook secrets), `commands/auth.py` (`login` reads the secret from a
    prompt or stdin, verifies it with `GET /me/principal`, stores it; `logout`; `whoami`; `tokens
    list|revoke`). Every verb has help text with one example. Packaging: `setup.py` gains the
    `storydump[cli]` extra (`click`, `httpx`, `rich`, `keyring`) and the console script
    `storydump=storydump_cli.main:cli` beside the legacy entry (removed in phase 03);
    `install_requires` is unchanged.
12. **Docs**: `07` §1 gains the token paragraph (person-bound vs service identity, the prefix,
    the ceiling, the allowlist); `AGENTS.md` gains a "storydump CLI" section (mint on the web,
    `login`, the variable for agents); CHANGELOG.

## Test Plan

Written red first; one named mutation per behaviour in `tests/mutations/cli_v2_01.sh`, committed
with the PR.

- **Auth gate** `tests/scripts/test_service_tokens_gate.py` (real DB, the API in-process,
  connected as `svc_ingress` with the tenant GUC): mint returns the secret once and stores only
  its hash; resolve refuses an unknown, a revoked, an expired token and a disabled person's
  token, each with its own reason; `last_used_at` stamps; a service identity is refused on the
  command route and outside its workspace; a `readonly` person-bound token is refused on the
  command route and admitted on `/ops` and `/me/principal`; a person-bound `operator` token held
  by a `member` acts as a member (the port's own gate); a session presented as bearer still
  resolves as a session; every route outside the allowlist answers 403 to a token (the router
  is enumerated); `/me/tokens` mint refuses a token principal; a CLI write's intent audit row
  reads `channel='cli'` with `actor_kind='user'` and the `cli_command` row names the token and
  the same `external_ref`; a repeated command with the same key answers `replayed` and writes
  nothing.
- **Migration**: the advertised-DDL test at 35; the lineage lane at 077; the tenancy gate green;
  the runner test asserts the ledger grant.
- **Units**: vocabulary parity and the envelope/error schemas; `current_principal` routing by
  prefix (fake engine); the storage backend (keychain refuses to fall back; the file is 0600;
  the variable wins); `login`/`whoami`/`tokens` against an in-process app; exit codes 3/4/64;
  redaction; the import-boundary test.
- **Web**: the tokens panel's mint/list/revoke against a mocked API (the landing app's test
  runner), the secret rendered once.

## Verification Checklist

- [x] `pytest tests/scripts/test_service_tokens_gate.py` green as `svc_ingress` (3 passed);
      `tests/mutations/cli_v2_01.sh` reports every mutation killed (47/47 at merge; 47/47 again
      on the phase 02 and phase 03 trees).
- [x] `storydump login` (secret from the prompt) then `storydump whoami` names the person, the
      role per workspace and the token — the phase 01 live sample on the demo rig. The mint on the
      production web (Settings › API tokens, shown once) is the owner's step: queued (`RUN_LOG.md`
      §7, the Path B recording).
- [x] The command route with a `readonly` token answers 403, with a person-bound `operator` token
      the story is skipped and its audit row reads `channel = 'cli'`, with a service identity 403 —
      the tokens gate, end to end.
- [x] `…/invitations/x/accept` under a token answers 403 `session_required` — the tokens gate.
- [x] Web sign-in and the existing session tests unchanged and green — invariant I1, re-checked
      after every merge (§5).
- [ ] 077 applied by the pre-deploy runner (the worker's deploy of `218c864` SUCCESS); `07` §23,
      manifest ordinal 21, lineage list updated — done; `posture`'s grant present in production —
      BLOCKED (owner): needs an owner-minted token, `storydump posture` shows it (§7).

## What NOT To Do

- Do not let a token mint a token; do not mint from a migration or a script.
- Do not widen `p_auth_ingress_svctok`; the API already reads the table.
- Do not change the session path's behaviour; the prefix routes only `sdt_` values.
- Do not put a grant on the runner's schema into a migration (F7); do not add a fleet token.
- Do not make `Command.actor_user_id` optional; a service identity never reaches the port.
- Do not put `keyring` in `install_requires`; it lives in the `storydump[cli]` extra.

## Build notes (2026-09-15)

Built as PR `implement/cli-v2-01-tokens`. Deviations from the steps above, each deliberate:

- The client's config file is `config.json`, not `config.toml`: CI runs Python 3.10 and
  `tomllib` is 3.11+; a TOML dependency for two keys was not worth it.
- The CLI's tests live in `tests/storydump_cli/` (mirroring the package's top-level home), not
  `tests/src/cli/`; the console script is `storydump=storydump_cli.main:main` (a function
  returning the exit code; Click runs with `standalone_mode=False` inside it).
- The `cli_command` audit row and the port's transition row pair on `(entity_id, created_at,
  channel)`: both are written in one transaction and share its `now()`; the trigger row cannot
  carry `external_ref`, and the label GUC was rejected (F4 (b)).
- A service identity may revoke only itself (a kill switch, never a lever over the workspace's
  other identities); the step above said "the workspace's".
- The service identity's workspace-state check is its own tenant-scoped read (two statements
  after the token lookup): a join to `workspaces` on the authentication connection is hidden by
  RLS, which the gate caught as a 401.
- The token routes live in `src/api/routes/tokens.py` under the v1 prefix, with the allowlist
  `TOKEN_ROUTES` in `src/api/principal.py`.
- `keyring` is in the `storydump[cli]` extra only — not in `requirements.txt`, which builds the
  API and the worker on Railway (the first commit had it there; the review caught it).
- `Principal.kind` is a plain `str` (`vocabulary.PRINCIPAL_KINDS` names the two values), not the
  `Literal` the step wrote; the constructor's defaults keep every existing session principal.
- After the review: a `readonly` person-bound token may revoke only itself (the two DELETE
  routes carry the same write fence as the command route); the `cli_command` row's entity is
  the story the port acted on (the result's `intent_id`), never the body's claim; `last_used_at`
  is stamped only by an authenticated use; the CLI refuses to send a token over plain http to a
  host that is not this machine (`STORYDUMP_INSECURE_HTTP=1` for a dev server); a session value
  is re-drawn if it would start with `sdt_`; token id path segments are `uuid.UUID` like every
  other id (a malformed one is a 422).

## Context

area: API auth, ledger audit, web settings, CLI skeleton · effort: L · risk: medium (auth on
the write path) · priority: P1
