---
paths:
  - "src/models/**"
  - "src/services/target/**"
---

# Database (the target schema)

One schema: the ledger in `public`, declared as code. The models in
`src/models/target/` declare the tables, columns, defaults, constraints and
indexes; the SQL files in `src/models/target/ddl/` declare the rest — roles,
functions, reference rows, triggers, RLS and policies, ownership, grants
(`ddl/__init__.py`). Besides `public` a database holds `runner` (the migration
ledger) and `archive` (built empty; `svc_maintenance` holds CREATE on it,
`ddl/02_schemas.sql`; the catalog comparison leaves both out). How a change to any of it ships is
`.claude/rules/migrations.md`.

## The tables

Forty-four, in seven model modules (`src/models/target/__init__.py` imports
them all; the count is pinned at `tests/scripts/test_schema_builder.py:92`).
Thirty-three are tenant-keyed: a `workspace_id` column, or `workspaces` itself.

| Models | Tables |
|---|---|
| `identity_and_tenancy.py` | `users`, `user_identities`, `workspaces`, `workspace_members`, `workspace_member_removals`, `signup_admissions`, `waitlist_entries`, `workspace_invitations`, `channel_bindings`, `onboarding_sessions` |
| `accounts_sources_media.py` | `ig_accounts`, `provider_quarantine`, `media_sources`, `oauth_credentials`, `media_items`, `post_locks` |
| `intent_ledger.py` | `post_intents`, `post_intent_transitions`, `audit_events`, `daily_post_counts`, `category_post_case_mix` |
| `machinery.py` | `jobs`, `channel_outbox`, `provider_operations`, `command_dedup`, `rate_counters` |
| `auth_plane.py` | `session_tokens`, `oauth_states`, `service_tokens`, `oauth_connection_results` |
| `instagram_catalog.py` | `media_source_destinations`, `instagram_past_posts_generations`, `instagram_source_policies`, `instagram_media_index`, `instagram_media_assets`, `instagram_item_tombstones`, `instagram_artifacts`, `instagram_intent_provenance`, `instagram_operation_provenance` |
| `meta_receipts.py` | `meta_identity_mappings`, `meta_deletion_receipts`, `meta_deletion_receipt_scopes`, `meta_deletion_receipt_generations`, `meta_deletion_receipt_capabilities` |

- **`workspaces` is the tenant** (`tenant_id == workspaces.id`). Product settings
  are its columns, never environment variables: `workspaces.SETTINGS_COLUMNS`
  (`src/services/target/workspaces.py:66`) is the set a `settings_change` may
  touch — `dry_run_mode`, `api_publishing_enabled`, `posts_per_day`, the posting
  hours, `tz`, … — and `is_paused` moves only through `pause_workspace` /
  `resume_workspace`. `ig_accounts` carries the per-account schedule overrides
  (`ACCOUNT_SETTINGS_COLUMNS`, `:112`), where NULL inherits the workspace.
- **`post_intents` is the ledger**: one story, for one account, at one slot
  (`uq_intent_slot`). `state` and `publish_step` are closed lists
  (`vocabulary.INTENT_STATES`, `PUBLISH_STEPS`, pinned to the models' CHECKs by
  `tests/src/services/target/test_vocabulary.py:57`); the legal edges are the
  rows of `post_intent_transitions` (`ddl/05_reference_rows.sql`);
  `tg_intent_guard` refuses the rest and `tg_intent_audit` writes every state
  change to `audit_events`. A story's `origin` is `cadence` (minted by
  `plan_slot` for a slot) or `planned` (a person chose the time). One approval
  rule holds for every story. `tg_intent_person_or_rule` fixes `origin` at
  birth and admits `awaiting_approval → approved` only for:
  - a person's actor (`user` with a user id), with `approval_rule` left NULL;
  - the system stamping the rule that approved the story (`auto`, `item` or
    `returning`) in that same UPDATE.

  `approval_rule` changes on no other update, and `ck_intent_waiting_unruled`
  keeps every waiting story one a person decides. Every insert writes
  `approval_mode = 'manual'`; another value is the workspace's mode as copied
  at mint before approval rules, history now. The slot key `uq_intent_slot` is
  cadence-only (`WHERE origin = 'cadence'`): a planned row takes no slot. A
  planned row that is due is served by `fn_prompts_due` only while it can be
  served and within the worker's late window, or expired with its reason by
  the miss leg (`fn_planned_misses`), which tells the bound chats; the reaper's
  slot expiry takes cadence rows only. A past-posts cadence row of an active,
  unpaused workspace is left to a worker that parks late ones
  (`fn_reaper_sweep`'s `p_past_posts_park_late`): its reaper parks the row for
  review, told, once it is still waiting a late window after its slot
  (`fn_reaper_late_past_posts`).

  **A planned row may carry a repeat rule**: `repeat_unit`, `repeat_every`,
  `repeat_days` and `repeat_at`. All four or none (`ck_intent_repeat`); the
  unit list stands alone in `ck_intent_repeat_unit`, pinned to
  `vocabulary.REPEAT_UNITS`. When such a row enters a terminal state other than
  `rejected`, `tg_intent_repeat` inserts its next occurrence in the same
  transaction, at `fn_repeat_times`'s next time, which after a post is on a
  later local day than the post (a series posts at most once a local day). It
  inserts nothing after a miss for any reason except `late` or `paused`, while
  the workspace is offboarding, or once the account is no longer live. So
  offboarding ends a series for good: its drain cancels the waiting story, and
  a restore un-cancels nothing. Local times are read through `fn_wall_instant`,
  which one-off planning's `_INSTANT` also calls. The trigger is total (a
  failure is a `repeat_failed` audit row, never a raise), and its INSERT names
  its columns, so every other column starts at its default
  (`ddl/04_functions.sql`, `trg_intent_repeat`). **The trigger is the
  authority** — `intent_ledger.py` issues the UPDATE and translates the
  refusal; do not add a Python pre-check of the edge set, nor of what the
  doors decide.
- **`jobs`** is the work queue (kind, lane, serialization key, lease token);
  **`channel_outbox`** is the delivery record for every message to a chat;
  **`provider_operations`** is one permit per Instagram call that has an effect
  (`container_create`, `publish`), written before the call;
  **`command_dedup`** is admission idempotency; **`rate_counters`** holds the
  pacing and admission windows.
- **Secrets**: `oauth_credentials.encrypted_payload` is Fernet ciphertext
  (`ENCRYPTION_KEY`), and so is `oauth_states.encrypted_code_verifier` (a
  PKCE verifier); `session_tokens` and `service_tokens` store a SHA-256 hash,
  never the secret. Do not select either ciphertext column into a log or a
  view.

## Tenancy: RLS, the GUC, and the gate

- Every table has RLS enabled (`src/models/target/ddl/07_row_security.sql`,
  forty-four `ENABLE` lines). The tenant policy is
  `workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid`
  (`07_row_security.sql:95`; `workspaces` keys on `id`, `:91`), so an unset GUC
  reads nothing. The exceptions are deliberate classes: `jobs` also exposes
  system rows (`workspace_id IS NULL`, `:99`), the user plane and the machinery
  counters are row-open to the runtime roles (`:105`-`:112`, `:235`-`:239`),
  `post_intent_transitions` is read-only reference data (`:114`),
  `workspace_member_removals` grants the runtime roles nothing: only the
  membership doors read or write it, and `signup_admissions` is global and
  reached only through `svc_membership`'s definer functions:
  `fn_signup_admitted` and `fn_waitlist` read it, and the admission doors
  (`fn_signup_admit`, `fn_signup_link`, `fn_signup_claim`) read and write it.
  So does `tg_users_consume_admission`, the trigger that stamps an admission
  used when an account is created at its address; its function is no door,
  and EXECUTE on it is granted to no role. The runtime roles hold no grant on
  it. `waitlist_entries` is global too: `svc_ingress` may INSERT and nothing
  else, so the public waitlist route cannot read the list. It is read only
  through `fn_waitlist`. A `user_identities` row is deleted only by
  `fn_identity_unlink`, a person removing their own Telegram identity. Before
  membership, a person reaches the invitations addressed to them only through
  `fn_invitations_for` and `fn_invitation_reissue`: an invitation is its
  workspace's row, so the tenant policy hides it from them. The four
  deletion-receipt tables grant the runtime roles nothing: only the receipt
  doors reach them, and `meta_deletion_receipts` and its capabilities carry no
  `workspace_id`, because a receipt outlives the workspace it scoped.
  `meta_identity_mappings` has the ordinary tenant policy, and Meta's
  callbacks, which name no tenant, read it through `fn_meta_subject_mappings`.
  Grants are `ddl/09_grants.sql`; door ownership is `ddl/08_ownership.sql`.
- The runtime roles are `svc_ingress` (API) and `svc_worker`; cross-tenant work
  goes through `SECURITY DEFINER` doors owned by the NOLOGIN roles `svc_claim`,
  `svc_clock`, `svc_maintenance` and `svc_membership` (`ddl/01_roles.sql`) —
  `fn_claim_job`, `fn_clock_tick`, `fn_reaper_sweep`, `fn_resolve_binding`, …
  There is no privileged unit of work, and none should be added.
- **Policies are not the only fence.** Both services connect as their own
  logins (`TARGET_DATABASE_URL`: the API as `svc_ingress`, the worker as
  `svc_worker`), so the policies are live on every deployed path. The database
  owner, which owns every table and bypasses RLS, is `DATABASE_URL`, the
  pre-deploy runner's login alone
  (`documentation/operations/schema-cutover.md`). The worker logs its login at
  boot (`connection_role`, `src/worker.py:883`); `storydump posture` and the
  API's `db_role` report the live answer. The predicate is still the first
  fence. Every query names its tenant with an explicit `workspace_id = :ws`
  predicate on each table it touches, as `ops_views.py` and
  `command_executors._intent_row` do, so a login that bypasses the policies
  (the owner, a script, a test's control) still reads one workspace. An
  estate-wide read that has no tenant is a door (the fleet-health doors
  `fn_health_*`, the worker's sweep doors), unless a policy already admits it
  with no tenant set (`p_jobs`'s `workspace_id IS NULL` rows, `p_rate`'s
  `USING (true)`). A direct read across tenants returns NOTHING under the
  runtime logins: a surface that reads tenant tables directly goes blind.
- **The tenancy gate** (`scripts/tenancy_gate.py`,
  `tests/scripts/test_tenancy_gate.py`) reads a live catalog and fails when a
  tenant-keyed table — one with a `workspace_id` column, or `workspaces`
  itself — lacks RLS or a policy, or departs from the shared FORCE posture.
  `ENABLE` without `FORCE` is the ratified posture (`FORCE_REQUIRED = False`,
  `scripts/tenancy_gate.py:37`). Its tests run it on probe tables; the built
  schema is held by the Schema consistency job's catalog comparison, which
  includes RLS and policies. A new tenant table gets its `ENABLE ROW LEVEL
  SECURITY` and its policy in `07_row_security.sql` in the same PR.

## How queries are written

Hand-written SQL through SQLAlchemy `text()` on asyncpg, under the unit of work
(`src/services/target/unit_of_work.py`). The models exist for the schema, never
as an ORM: no `session.query`, no relationship loading.

- `unit_of_work(engine, tenant_id, actor_kind=…, actor_user_id=…, channel=…)`
  then `async with uow.begin() as session:`. It is unconstructible without a
  tenant (`TenantContextRequired`, `:311`) and applies the GUCs with
  `apply_gucs` (`:348`) — the one spelling; `set_config(..., true)` so the value
  dies with the transaction and a pooled connection cannot inherit a tenant.
  The audit triggers refuse a state change with no `app.actor_kind`.
- **One block, one transaction.** The GUCs are set once, as the block opens,
  and die with its transaction. So a statement after a commit inside the block
  runs with no tenant and no actor: the policies match nothing for it, and the
  audit triggers refuse its state changes. To commit and go on, close the block
  and open another. The worker's per-job sessions and the outbox poller's
  follow the same rule (`unit_of_work.py:532-586`).
- **A transaction never spans a provider call**: write the checkpoint, commit,
  then call Meta, Telegram, Drive or Cloudinary. The egress floor raises
  `TransactionDisciplineError` for a floor-routed call made while
  `in_transaction()` is true (`src/services/target/egress.py:366`).
- Functions take the caller's executor (`session` or `conn`) first and run in
  the caller's transaction. `readers.rows` / `readers.row` are the
  execute-and-map for a read model (`ops_views.py` is written on them).
- Concurrency is settled in SQL, not by reading first: `INSERT … ON CONFLICT`
  (the slot, admission), `FOR UPDATE` on the row a decision is about, a CAS on
  the state being left, `FOR UPDATE SKIP LOCKED` for claims.
- The pool is pinned (`MAX_OVERFLOW_SEAM = 0`, `POOL_SIZE_SEAM = 10`,
  `unit_of_work.py:78`-`:86`) and the engine comes from `TARGET_DATABASE_URL`
  alone (`engine_url_from_env`, `:244`); there is no settings-built fallback.

## Bound parameters under asyncpg

asyncpg PREPARES every statement, so PostgreSQL must infer each parameter's
type from where it is used. Two shapes cannot be inferred and fail at runtime
with `could not determine data type of parameter $N` — never in the unit tests
(scripted executors), only against a real database:

- a nullable parameter tested alone: write `CAST(:p AS text) IS NULL`
  (`provisioning.attach_connected_identity`,
  `src/services/target/provisioning.py:613`), or drop the guard when
  `col = :p` already yields NULL for a NULL parameter
  (`provisioning.connect_destination`'s lookup, `:727`);
- a string that must become a timestamp: `CAST(CAST(:p AS text) AS timestamptz)`
  (`src/services/target/work_loop.py:332` — a bare `CAST(:p AS timestamptz)`
  makes asyncpg refuse the string).

Run the affected gate suite against a real PostgreSQL before merging (the
recipe is in `AGENTS.md` › Testing): the first shape once shipped green and
every per-row Instagram connect failed at the callback.

## Schema changes

Changing the schema has its own rule, loaded for the schema code and
`scripts/migrations/**`: `.claude/rules/migrations.md`. The one line of it a
service author needs: a PR that changes the schema changes the schema code
(models and `ddl/`) and adds the migration that brings an existing database to
it, and a new tenant table ships its `ENABLE ROW LEVEL SECURITY` and its policy
in that same PR (the tenancy gate, above).
