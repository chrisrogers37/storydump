# Domain model

Conventions: all ids are UUIDs unless noted; every tenant-scoped table has `workspace_id UUID NOT NULL` plus a composite FK `(workspace_id, <parent_id>) REFERENCES parent(workspace_id, id)` wherever it references another tenant-scoped table, so a cross-workspace reference is inexpressible (#721 D12, kept). `created_at`/`updated_at` on every table, omitted below for brevity. Enum values are closed sets — adding one is a migration, not a string. Bare R/T/H ids cite `01` §Requirements ledger.

## §1. Identity and tenancy (FC-1, FC-2)

```
users              id, primary_email TEXT NULL UNIQUE, state ENUM(active|disabled)
                   -- platform-neutral human. NO telegram columns (FC-1.3).

user_identities    id, user_id NOT NULL → users,
                   provider ENUM(telegram|email_otp), external_id TEXT NOT NULL,
                   display_name TEXT, verified_at TIMESTAMPTZ
                   UNIQUE(provider, external_id)
                   -- how a human proves who they are on a surface; Telegram user ids live here.
                   -- email_otp is the one non-Telegram provider X.4 requires; further providers
                   -- (google, …) are plain enum migrations when product chooses them.

workspaces         id, name TEXT NOT NULL, owner_user_id NOT NULL → users,
                   state ENUM(active|suspended|offboarding),
                   tz TEXT NOT NULL, posting_window JSONB, posts_per_day INT,
                   category_mix JSONB, approval_policy JSONB,
                   next_slot_at TIMESTAMPTZ NULL (indexed)
                   -- THE tenant root. tenant_id == workspaces.id everywhere.

workspace_members  (workspace_id, user_id) PK, role ENUM(owner|admin|member) NOT NULL,
                   added_by_user_id NULL → users
                   -- invariant: owner_user_id always has a member row with role=owner.
                   -- the ONE authorization gate (ingress) reads exactly this table.

channel_bindings   id, workspace_id NOT NULL,
                   channel ENUM(telegram_group|telegram_dm),
                   external_ref TEXT NOT NULL,      -- tg chat id
                   state ENUM(active|paused|revoked),
                   settings JSONB                   -- per-binding notification prefs
                   UNIQUE(channel, external_ref)
                   -- a Telegram chat is one binding of a workspace (FC-1.2). 0..n per workspace;
                   -- a web-only workspace simply has none. Bindings exist ONLY for push channels —
                   -- web is pull (§6), so there is deliberately no 'web' enum value; a future push
                   -- channel adds its value by migration.
```

## §2. Accounts, sources, media, quarantine

```
ig_accounts        id, workspace_id NOT NULL,
                   provider_account_ref TEXT NOT NULL,   -- Meta IG user id (the REAL account)
                   handle TEXT, state ENUM(active|reauth_required|disabled)
                   UNIQUE(workspace_id, provider_account_ref)
                   -- workspace-owned (FC-1). The same real account in two workspaces is two
                   -- rows (isolation by schema); real-account concurrency and Meta budgets
                   -- key on provider_account_ref, not our row id (§3 key 4, §8).
                   -- account-level fault quarantine is NOT a column here — see provider_quarantine.

provider_quarantine (workspace_id, provider, scope_ref) PK,
                   provider ENUM(ig|telegram|cloudinary|gdrive),
                   scope_ref TEXT NOT NULL DEFAULT '',   -- '' = whole (workspace, provider) pair;
                                                         -- else the account/binding/source ref
                   quarantined_until TIMESTAMPTZ NOT NULL, reason TEXT, entered_at TIMESTAMPTZ
                   -- THE generic T2 fault-quarantine mechanism at both grains. The jobs claim
                   -- query defers any job whose (workspace, provider[, scope_ref]) matches a
                   -- live row; one alert fires on quarantine ENTRY. No per-table quarantine
                   -- columns exist anywhere else — this is the single home.

oauth_credentials  id, workspace_id NOT NULL,
                   ig_account_id UUID NULL,     -- composite FK (workspace_id, ig_account_id) → ig_accounts
                   media_source_id UUID NULL,   -- composite FK (workspace_id, media_source_id) → media_sources
                   CHECK (num_nonnulls(ig_account_id, media_source_id) = 1),   -- typed XOR owner
                   provider ENUM(ig_login|fb_login_legacy|gdrive),
                   encrypted_payload BYTEA NOT NULL,     -- never plaintext, never in logs/traces
                   expires_at TIMESTAMPTZ NULL, next_refresh_at TIMESTAMPTZ NULL (indexed),
                   state ENUM(active|expired|revoked)
                   UNIQUE(workspace_id, ig_account_id, provider) WHERE ig_account_id IS NOT NULL
                   UNIQUE(workspace_id, media_source_id, provider) WHERE media_source_id IS NOT NULL
                   -- typed owner FKs (not a polymorphic owner_id) so the composite-FK isolation
                   -- convention holds on the MOST sensitive table, and both unique keys lead with
                   -- workspace_id. This is the explicit key #721's FLAG-1 contradiction resolves to.
                   -- fb_login_legacy: structurally closed to new rows by
                   -- CHECK (provider <> 'fb_login_legacy') added NOT VALID at L.6 (existing rows
                   -- tolerated, new rows impossible — the NOT VALID→VALIDATE pattern of §7);
                   -- VALIDATEd then dropped with the enum value at the FC-4 sunset (G.2).

media_sources      id, workspace_id NOT NULL, provider ENUM(gdrive),
                   config JSONB NOT NULL,                -- folder ref etc.
                   sync_checkpoint JSONB, next_sync_at TIMESTAMPTZ NULL (indexed),
                   state ENUM(active|paused|error), last_sync_success_at TIMESTAMPTZ NULL

media_items        id, workspace_id NOT NULL, source_id NOT NULL → media_sources (composite FK),
                   content_hash TEXT NOT NULL, media_kind ENUM(image|video),
                   provider_file_ref TEXT NOT NULL, category TEXT NULL,
                   state ENUM(available|unsupported|removed),
                   times_posted INT NOT NULL DEFAULT 0, last_posted_at TIMESTAMPTZ NULL
                   UNIQUE(workspace_id, content_hash)    -- dedup is per-workspace BY SCHEMA;
                                                         -- a global hash namespace is inexpressible (R4)

post_locks         (workspace_id, media_item_id, kind) PK,
                   kind ENUM(recent|skip|reject|unsupported),
                   expires_at TIMESTAMPTZ NULL,          -- NULL = permanent
                   created_by_intent_id UUID NULL
```

## §3. The intent ledger (heart of the system)

One durable row per posting attempt, from scheduling to a single immutable terminal state. This replaces the `posting_queue` (ephemeral, delete-on-completion) + `posting_history` (separate insert) split whose seam bred the known bug family (dangling `queue_item_id`, duplicate terminal rows, "Queue item not found" taps). Three derivations converged on this shape independently (#721's intent decomposition, the cold design, the shipped delivery-state trajectory) — see `03` D1; it also closes RF-G1.

```
post_intents       id, workspace_id NOT NULL,
                   ig_account_id NOT NULL → ig_accounts (composite FK),
                   media_item_id NOT NULL → media_items (composite FK),
                   state ENUM(
                     scheduled | prompt_pending | awaiting_approval | approved |
                     publishing | publishing_ambiguous | review_required |          -- working states
                     posted | skipped | rejected | expired | failed | cancelled     -- TERMINAL states
                   ),
                   cancel_requested BOOL NOT NULL DEFAULT false,   -- overlay flag, not a state (C9)
                   schedule_slot_at TIMESTAMPTZ NOT NULL,
                   approval_mode ENUM(auto|manual), approved_by_user_id NULL → users,
                   provider_account_ref TEXT NOT NULL,   -- immutable copy from ig_accounts at creation;
                                                         -- carries key 4 below
                   ig_container_id TEXT NULL,            -- persisted BEFORE the publish call (R1)
                   publish_step ENUM(none|transit_uploaded|container_created|container_ready|publish_called|effect_confirmed),
                   transit_asset_ref TEXT NULL,          -- Cloudinary public id for FC-3.5 reap
                   attempts_by_step JSONB, last_error JSONB,
                   entered_state_at TIMESTAMPTZ NOT NULL

audit_events       id BIGSERIAL, workspace_id NOT NULL, entity_kind TEXT, entity_id UUID,
                   from_state TEXT NULL, to_state TEXT NULL,
                   actor_kind ENUM(user|system|reaper|reconciler),
                   actor_user_id NULL, channel ENUM(telegram|web|cli|system) NULL,
                   detail JSONB
                   -- append-only. Channel provenance lives HERE, never on domain state (FC-2).

daily_post_counts  (workspace_id, ig_account_id, local_date) PK, count INT NOT NULL, cap_at_write INT NOT NULL
                   -- OUR product cadence cap, workspace-tz calendar semantics. NOT Meta's cap (§8).
```

### §3-keys. Uniqueness keys (C8 — they compose; this is the FLAG-2 fix)

1. **Slot idempotency (discovery dedup):** `UNIQUE(workspace_id, ig_account_id, schedule_slot_at)` — re-running slot planning cannot double-create an intent. Full unique: a slot occurs once; a later slot is a new `schedule_slot_at`, never a reuse. (No `kind` discriminator: exactly one intent kind exists today; the closed-enum convention makes introducing one a plain migration that widens this key deliberately — `03` C8.)
2. **One live intent per subject:** `UNIQUE(workspace_id, media_item_id, ig_account_id) WHERE state NOT IN (terminal set)` — includes the account, so the same media item may hold live intents for two different accounts of one workspace (multi-account is first-class, which is what #721's key wrongly blocked).
3. **One terminal outcome ever (R3):** terminal state is reached exactly once — enforced by the §4 transition guards plus the append-only audit trail; the interim uniqueness remediation for legacy `posting_history` is C10 in `03`.
4. **Publishing exclusivity per REAL account (H1, G1 in `03`):** `UNIQUE(provider_account_ref) WHERE state = 'publishing'` — one in-flight publish per real Instagram account, across workspaces, by constraint.

## §4. `post_intents` transition matrix (complete — the FLAG-3 fix)

Any (from, to) pair not listed is **forbidden and DB-guarded** (transition function checks, not application discipline). "clock/worker/reconciler/reaper" are system actors; "user" arrives only via an adapter command that already passed the ingress gate.

| From | To | Actor | Guard / effect |
|---|---|---|---|
| — | scheduled | clock (slot planner) | key 1 insert; caps pre-checked advisory |
| scheduled | prompt_pending | worker | outbox rows created for active push bindings |
| scheduled | expired | reaper | slot passed unclaimed |
| scheduled | cancelled | worker | cancel_requested, or workspace/account disabled |
| prompt_pending | awaiting_approval | worker | approval is reachable: prompt delivered on ≥ 1 binding, **or** no delivery succeeded but the workspace's web surface makes it visible (FC-2: web is a first-class surface) |
| prompt_pending | failed | worker | no reachable surface: all deliveries failed and no web access exists |
| prompt_pending | expired | reaper | expiry passed before delivery |
| awaiting_approval | approved | user or system(auto) | manual tap/click, or approval_policy auto-approve for returning media |
| awaiting_approval | skipped / rejected | user | terminal; rejected upserts a reject lock (post_locks) |
| awaiting_approval | expired | reaper | approval window passed; prompts superseded via outbox |
| awaiting_approval | cancelled | worker | cancel_requested |
| approved | publishing | worker | **one transaction**: acquire key 4 + `daily_post_counts` atomic increment-if-below-cap (R2) + job lease held. Denied increment ⇒ stays approved, deferred to next slot |
| approved | expired | reaper | approval stale beyond policy |
| approved | cancelled | worker | cancel_requested (pre-publish it is always honorable) |
| publishing | posted | worker | effect_confirmed; same transaction: times_posted++, recent lock upsert, FC-3.5 transit reap job |
| publishing | publishing_ambiguous | worker | timeout/crash after publish_called with unconfirmed effect (R8 ambiguous) |
| publishing | failed | worker | terminal provider error (R8 terminal class) |
| publishing | review_required | worker | poison: attempts exhausted on a retryable class (G5) |
| publishing_ambiguous | posted / failed | **reconciler only** | read-back of container status / recent media decides; never a blind retry |
| publishing_ambiguous | review_required | reconciler | read-back inconclusive after budget — a human looks |
| review_required | approved / failed / cancelled | user (operator) | explicit resolution command; audit_events records channel + actor |

`cancel_requested` during `publishing`/`publishing_ambiguous` is **not** honored by force (C9: `publishing → cancelled` is forbidden); the pipeline completes or reconciles, then the reconciler consults the flag only where a real choice remains. A late tap on any terminal intent renders the terminal state and never acts (R6).

## §5. `jobs` (execution machinery — #722 semantics, pg-only per C3)

```
jobs               id, kind TEXT NOT NULL, workspace_id UUID NULL (system jobs),
                   lane ENUM(interactive|bulk) NOT NULL,
                   serialization_key TEXT NULL, run_at TIMESTAMPTZ NOT NULL,
                   state ENUM(ready|leased|succeeded|failed|review_required|cancelled),
                   cancel_requested BOOL NOT NULL DEFAULT false,
                   attempts INT, max_attempts INT, deadline_at TIMESTAMPTZ,
                   locked_by TEXT NULL, locked_until TIMESTAMPTZ NULL, lease_token UUID NULL,
                   payload JSONB
                   -- indexes: (lane, state, run_at) partial WHERE state='ready'; (locked_until) WHERE state='leased'
```

Transitions: `ready → leased` (claim: SKIP LOCKED; serialization_key has no leased peer; no matching `provider_quarantine` row), `leased → succeeded | failed | review_required`, `leased → ready` (lease expiry — fenced: a stale `lease_token` cannot finalize), `ready → review_required` (poison edge — #722 intra-fix), `ready → cancelled` (cooperative; a leased job is only cancelled by its own worker at a checkpoint). The domain transaction (intent flip + counters + audit) and the job finalization commit together — that co-location is why jobs live in Postgres (C3). Job state is execution bookkeeping only; it is never the authority on whether an external effect happened (§6).

## §6. `channel_outbox` (FC-2 outbound port) and `provider_operations`

```
channel_outbox     id, workspace_id NOT NULL, binding_id NOT NULL → channel_bindings (composite FK),
                   kind ENUM(approval_prompt|prompt_supersede|notification|ack),
                   intent_id UUID NULL, payload JSONB NOT NULL,   -- channel-NEUTRAL content
                   state ENUM(pending|sending|sent|ambiguous|failed|superseded),
                   attempts INT, external_message_ref TEXT NULL   -- tg message id after send
                   -- rows exist only for PUSH channels; web is pull (reads intents via API).

provider_operations id, workspace_id NULL, provider ENUM(ig|cloudinary),
                   op_kind TEXT NOT NULL, business_key TEXT NOT NULL UNIQUE,
                   state ENUM(pending|in_flight|succeeded|failed|ambiguous),
                   request_fingerprint TEXT, response_ref JSONB
                   -- the external-effect rail (D1): only a lease holder writes here;
                   -- one business_key = at most one intended provider effect (P0-06 gate).
```

**Send-state authority is single-homed:** the outbox row IS the delivery record and the only authority on "did this send" — its `ambiguous` state carries the R8 no-blind-retry rule (resolve by read-back/stamp-heal, or supersede-then-send for edits); the sender *job* carries execution state only. `provider_operations` covers exactly the providers whose effects need the at-most-once rail: `ig` (publish steps) and `cloudinary` (upload/destroy) — Telegram delivery lives in the outbox, and Drive is read-only (no effects to guard). The outbox never *authorizes* effects (D2): it is a delivery record plus wake-up hint; authority for domain effects lives in jobs + the rail.

`service_runs` (legacy ops bookkeeping) survives unchanged during the program and is retired at S.4 when the last background loop moves onto jobs + `audit_events` — decision recorded in `03`.

## §7. RLS and the tenancy backstop (C4)

- RLS enabled on every workspace-scoped table above, keyed on `SET LOCAL app.tenant_id`; policies are **constant-expression** (`workspace_id = current_setting('app.tenant_id')::uuid`) — membership is ingress's job, RLS only scopes rows.
- Enablement discipline: zero-NULL gates before and after each table's cutover; `NOT NULL` added as `NOT VALID` then `VALIDATE`d; the RLS test harness runs as the exact runtime role, without owner privileges or session-affinity assumptions (#722 P0-09, kept verbatim).
- System actors (clock, reapers, reconciler, migration/backfill) use dedicated roles with explicit `workspace_id` predicates in every statement — no ambient-bypass role in runtime code paths.
- Known limit, mitigated not ignored: PostgreSQL integrity errors bypass RLS and can act as cross-tenant existence oracles. All unique keys on tenant-scoped tables lead with `workspace_id` and all ids are UUIDs, so no enumerable oracle exists; `provider_account_ref`'s global key 4 is the one deliberate exception and leaks only "some workspace is publishing to this real account" — accepted and documented.

## §8. Two caps, never conflated

- **Product cadence cap** — ours: `daily_post_counts` in workspace tz, calendar-day semantics, atomically incremented in the approved→publishing transaction (R2). This is the only cap we count locally.
- **Meta publish cap** — theirs: 25 per rolling 24 h per real account, enforced by Meta on the publish step (error 9). Never counted locally (rolling window + out-of-band posts make local counters wrong by construction — vault doc). Pre-check via the `IG User Content Publishing Limit` usage endpoint as an advisory gate; on error 9, defer with `available_at` derived from provider-reported usage — a cap, not a fault, so no quarantine row.

## §9. Legacy → target mapping (all 14 current tables accounted for)

Every re-key below runs on the six-stage migration machine exactly as specified in `04` §Ground rules (the single home for its stages, stop rules, and comparator).

| Current (`origin/main`) | Target disposition |
|---|---|
| `users` | `users` (stripped of Telegram identity) + `user_identities(provider='telegram')` |
| `chat_settings` | split: tenant config → `workspaces`; chat facts → `channel_bindings(channel='telegram_group')` |
| `user_chat_memberships` | `workspace_members` (roles mapped; per-chat notification prefs → binding `settings`) |
| `user_interactions` | `audit_events` (append-only, channel-tagged) |
| `instagram_accounts` | `ig_accounts` (workspace-owned; global-identity + per-tenant-selection split dissolved) |
| `api_tokens` | `oauth_credentials` (typed owner FKs, encrypted; provider `ig_login` / `fb_login_legacy`) |
| `media_items` | `media_items` (re-keyed `workspace_id`, per-workspace hash unique) |
| `media_posting_locks` | `post_locks` |
| `posting_queue` | `post_intents` (working states) |
| `posting_history` | `post_intents` (terminal states) + `audit_events`; legacy rows backfilled as terminal intents |
| `category_post_case_mix` | `workspaces.category_mix` JSONB (or a child table if row-shaped today — implementer keeps the current shape, re-keyed) |
| `onboarding_sessions` | kept, re-keyed to `workspace_id` + `user_id` (D10 onboarding-merge adjudication in `03`) |
| `audit_log` | merged into `audit_events` (one append-only trail; legacy rows migrated verbatim into `detail`) |
| `service_runs` | kept as-is + nullable `workspace_id`; retired at S.4 (§6, `03`) |
