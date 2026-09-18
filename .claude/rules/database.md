---
paths:
  - "src/models/**"
  - "src/services/target/**"
---

# Database (the target schema)

One schema: the ledger in `public`, created by the migrations above the 051
move (`scripts/migrations/052_*` onward) and mirrored by the declarative models
in `src/models/target/`. The legacy tier was retired in the tear-out (#1216,
September 2026); its data survives only as the sixteen
`archive.<table>_pre_cutover_20260917` snapshots (078), and the `legacy` schema
itself is dropped by 079 in the owner's window
(`documentation/operations/legacy-window-close.md`). Nothing under `src/` reads
either.

## The tables

Twenty-six, in five model modules named after the migrations that create them
(`src/models/target/__init__.py`; the count and the nineteen tenant-keyed are
pinned at `tests/scripts/test_tenancy_gate.py:377`-`:378`):

| Models (migration) | Tables |
|---|---|
| `identity_and_tenancy.py` (053) | `users`, `user_identities`, `workspaces`, `workspace_members`, `workspace_invitations`, `channel_bindings`, `onboarding_sessions` |
| `accounts_sources_media.py` (054) | `ig_accounts`, `provider_quarantine`, `media_sources`, `oauth_credentials`, `media_items`, `post_locks` |
| `intent_ledger.py` (055) | `post_intents`, `post_intent_transitions`, `audit_events`, `daily_post_counts`, `category_post_case_mix` |
| `machinery.py` (056) | `jobs`, `channel_outbox`, `provider_operations`, `command_dedup`, `rate_counters` |
| `auth_plane.py` (060) | `session_tokens`, `oauth_states`, `service_tokens` |

- **`workspaces` is the tenant** (`tenant_id == workspaces.id`). Product settings
  are its columns, never environment variables: `workspaces.SETTINGS_COLUMNS`
  (`workspaces.py:54`) is the set a `settings_change` may touch —
  `dry_run_mode`, `api_publishing_enabled`, `posts_per_day`, the posting hours,
  `tz`, … — and `is_paused` moves only through `pause_workspace` /
  `resume_workspace`. `ig_accounts` carries the per-account schedule overrides
  (`ACCOUNT_SETTINGS_COLUMNS`), where NULL inherits the workspace.
- **`post_intents` is the ledger**: one story, for one account, at one slot
  (`uq_intent_slot`). `state` and `publish_step` are closed lists
  (`vocabulary.INTENT_STATES`, `PUBLISH_STEPS`, pinned to 055's CHECKs by
  `tests/src/services/target/test_vocabulary.py:63`); the legal edges are the
  rows of `post_intent_transitions`; `trg_intent_guard` refuses the rest and
  `trg_intent_audit` writes every state change to `audit_events`. **The trigger
  is the authority** — `intent_ledger.py` issues the UPDATE and translates the
  refusal; do not add a Python pre-check of the edge set.
- **`jobs`** is the work queue (kind, lane, serialization key, lease token);
  **`channel_outbox`** is the delivery record for every message to a chat;
  **`provider_operations`** is one permit per Instagram call that has an effect
  (`container_create`, `publish`), written before the call;
  **`command_dedup`** is admission idempotency; **`rate_counters`** holds the
  pacing and admission windows.
- **Secrets**: `oauth_credentials.encrypted_payload` is Fernet ciphertext
  (`ENCRYPTION_KEY`); `session_tokens` and `service_tokens` store a SHA-256
  hash, never the secret. Do not select the payload column into a log or a view.

## Tenancy: RLS, the GUC, and the gate

- Every table has RLS enabled (058, 060). The tenant policy is
  `workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid`
  (`058_rls_and_policies.sql:145`; `workspaces` keys on `id`, `:141`), so an
  unset GUC reads nothing. The exceptions are deliberate classes: `jobs` also
  exposes system rows (`workspace_id IS NULL`, `:159`), the user plane and the
  machinery counters are row-open to the runtime roles (`:169`-`:178`),
  `post_intent_transitions` is read-only reference data (`:182`).
- The runtime roles are `svc_ingress` (API) and `svc_worker`; cross-tenant work
  goes through `SECURITY DEFINER` doors owned by `svc_claim`, `svc_clock`,
  `svc_maintenance` and `svc_membership` (059 onward) — `fn_claim_job`,
  `fn_clock_tick`, `fn_reaper_sweep`, `fn_resolve_binding`, … There is no
  privileged unit of work, and none should be added.
- **Policies are not the only fence.** Production's runtime login was still the
  database owner, which bypasses RLS, when last measured (2026-09-17, 078's
  header; the switch is `documentation/operations/runtime-database-roles.md`).
  `storydump posture` and `/health`'s `db_role` report the live answer. So every
  query names its tenant: an explicit `workspace_id = :ws` predicate on each
  table it touches, as `ops_views.py` and `command_executors._intent_row` do.
- **The tenancy gate** (`scripts/tenancy_gate.py`,
  `tests/scripts/test_tenancy_gate.py`) replays the migrations and fails when a
  tenant-keyed table — one with a `workspace_id` column, or `workspaces` itself
  — lacks RLS or a policy. `ENABLE` without `FORCE` is the ratified posture
  (`FORCE_REQUIRED = False`, `:59`). A new tenant table ships its
  `ENABLE ROW LEVEL SECURITY` and its policy in the same migration.

## How queries are written

Hand-written SQL through SQLAlchemy `text()` on asyncpg, under the unit of work
(`src/services/target/unit_of_work.py`). The models exist for schema parity,
never as an ORM: no `session.query`, no relationship loading.

- `unit_of_work(engine, tenant_id, actor_kind=…, actor_user_id=…, channel=…)`
  then `async with uow.begin() as session:`. It is unconstructible without a
  tenant (`TenantContextRequired`, `:298`) and applies the GUCs with
  `apply_gucs` (`:340`) — the one spelling; `set_config(..., true)` so the value
  dies with the transaction and a pooled connection cannot inherit a tenant.
  The audit triggers refuse a state change with no `app.actor_kind`.
- **A transaction never spans a provider call**: write the checkpoint, commit,
  then call Meta, Telegram, Drive or Cloudinary. The egress floor raises
  `TransactionDisciplineError` for a floor-routed call made while
  `in_transaction()` is true (`egress.py:343`).
- Functions take the caller's executor (`session` or `conn`) first and run in
  the caller's transaction. `readers.rows` / `readers.row` are the
  execute-and-map for a read model (`ops_views.py` is written on them).
- Concurrency is settled in SQL, not by reading first: `INSERT … ON CONFLICT`
  (the slot, admission), `FOR UPDATE` on the row a decision is about, a CAS on
  the state being left, `FOR UPDATE SKIP LOCKED` for claims.
- The pool is pinned (`POOL_SIZE_SEAM = 10`, `MAX_OVERFLOW_SEAM = 0`, `:83`-`:91`)
  and the engine comes from `TARGET_DATABASE_URL` alone (`engine_url_from_env`,
  `:186`); there is no settings-built fallback.

## Bound parameters under asyncpg

asyncpg PREPARES every statement, so PostgreSQL must infer each parameter's
type from where it is used. Two shapes cannot be inferred and fail at runtime
with `could not determine data type of parameter $N` — never in the unit tests
(scripted executors), only against a real database:

- a nullable parameter tested alone: write `CAST(:p AS text) IS NULL`
  (`provisioning.attach_connected_identity`, `provisioning.py:610`), or drop the
  guard when `col = :p` already yields NULL for a NULL parameter
  (`provisioning.connect_destination`'s lookup, `:724`);
- a string that must become a timestamp: `CAST(CAST(:p AS text) AS timestamptz)`
  (`work_loop.py:253` — a bare `CAST(:p AS timestamptz)` makes asyncpg refuse the
  string).

Run the affected gate suite against a real PostgreSQL before merging (the
recipe is in `AGENTS.md` › Testing); #1221 shipped the first shape and every
per-row Instagram connect failed at the callback until #1232.

## Migrations

Writing or changing a migration has its own rule, loaded for `scripts/migrations/**`:
`.claude/rules/migrations.md`. The one line of it a service author needs: a migration that
creates or alters a table changes the model in the same PR, and a new tenant table ships its
`ENABLE ROW LEVEL SECURITY` and its policy in that same file (the tenancy gate, above).
