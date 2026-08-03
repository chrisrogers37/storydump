# Domain model

This file is DDL-complete: every table below is stated as executable PostgreSQL, and every invariant claimed by this plan names the database object that enforces it. Prose between DDL blocks explains *why*; the DDL is the decision. Bare R/T/H ids cite `01` §Requirements ledger; current-state column facts cite the schema ground-truth extraction (fleet working set, 2026-08-02) — its per-column legacy mapping is restated where a track needs it in `04`.

## §0. Conventions (normative — DDL below relies on these; they are decisions, not suggestions)

- **Ids:** `id UUID PRIMARY KEY DEFAULT gen_random_uuid()` on every table unless a natural PK is shown. (Legacy tables mix `uuid_generate_v4()`/`gen_random_uuid()`; new DDL uses `gen_random_uuid()` only — no extension dependency.)
- **Time:** every new timestamp column is `TIMESTAMPTZ`. Legacy columns are naive `TIMESTAMP`, UTC by convention; **every backfill converts with `AT TIME ZONE 'UTC'`** — this clause is mandatory in track DDL, and a bare cast is a review-blocking defect.
- **Stamps:** `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`, `updated_at TIMESTAMPTZ NOT NULL DEFAULT now()` on every table; `updated_at` maintained by the shared `trg_touch_updated_at` trigger (one function, applied per table). Omitted from the DDL blocks below for brevity — this paragraph is their single normative statement.
- **Enums are TEXT + named CHECK constraints**, never native `ENUM` types. Closed sets whose members must be *removable* (e.g. `fb_login_legacy` dies at G.2) make native enums a liability (`DROP VALUE` does not exist); CHECK text is also what the existing enum-SSOT parity gate (models ↔ latest migration DDL) already verifies. Adding/removing a value = enum edit + migration editing the named CHECK, held in lockstep by that gate.
- **Tenant scoping:** every tenant-scoped table has `workspace_id UUID NOT NULL`; wherever it references another tenant-scoped table it uses a **composite FK** `(workspace_id, <ref>) REFERENCES parent (workspace_id, id)` so a cross-workspace reference is inexpressible (#721 D12, kept). Composite FKs require the parent-side `UNIQUE (workspace_id, id)` — those indexes are part of the parent DDL below.
- **ON DELETE policy (three classes, no per-FK improvisation):**
  1. Workspace-rooted and tenant-child edges: `ON DELETE CASCADE`. Deletion of tenant data happens exactly once — at offboarding (T3), as `svc_maintenance` deleting the `workspaces` row after the `06` offboarding workflow; the cascade is the mechanism.
  2. `users` references from tenant tables (`approved_by_user_id`, `added_by_user_id`, …): `ON DELETE SET NULL` — history survives a departed human (columns are nullable attribution, never authorization).
  3. `audit_events.workspace_id` carries **no FK** — the one deliberate exception: audit must outlive the tenant it describes; the retention sweep (`05`) is its only deleter. RLS still applies to it.
  Runtime roles hold **no DELETE grant** on any tenant table (§7 grant matrix), so "runtime never deletes" is structural, not discipline.
- **JSONB payloads are versioned:** every JSONB document column carries `"v": <int>` at top level; readers accept `v` and `v-1` (the N-1 rule, `04` ground rules); a payload without `v` is invalid at the service boundary. Field schemas for each payload are stated at the column that owns them.
- **Named constraints only** (`ck_`, `uq_`, `fk_`, `ix_` prefixes) — auto-generated names caused the legacy api_tokens 004/008 drop-miss; every constraint below is named so later DDL can target it.
- **Writer identity GUCs:** every transaction that mutates domain state sets `app.actor_kind` (and `app.actor_user_id` / `app.channel` when a human/channel is involved) via `SET LOCAL`. The `§4` audit trigger raises on a missing `app.actor_kind` — anonymous writes are impossible, in the database, for every writer including a psql session.

## §1. Identity and tenancy (FC-1, FC-2)

```sql
CREATE TABLE users (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  primary_email  TEXT NULL,
  state          TEXT NOT NULL DEFAULT 'active'
                 CONSTRAINT ck_users_state CHECK (state IN ('active','disabled')),
  CONSTRAINT uq_users_primary_email UNIQUE (primary_email)
);
-- Platform-neutral human (FC-1.3): NO telegram columns. Telegram-only users have NULL email.
-- users.state='disabled' denies access at the ONE ingress gate; rows/memberships survive.

CREATE TABLE user_identities (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  provider     TEXT NOT NULL
               CONSTRAINT ck_user_identities_provider CHECK (provider IN ('telegram','email_otp')),
  external_id  TEXT NOT NULL,          -- tg user id (as text) | lowercased email
  display_name TEXT NULL,
  verified_at  TIMESTAMPTZ NULL,
  CONSTRAINT uq_identity_per_provider UNIQUE (provider, external_id),
  CONSTRAINT uq_user_provider         UNIQUE (user_id, provider)
);
-- One identity per provider per user (v1; widening uq_user_provider is a deliberate migration).
-- email_otp is the one non-Telegram provider X.4 requires; challenge/session mechanics in 07.

CREATE TABLE workspaces (
  id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name                    VARCHAR(100) NOT NULL,
  owner_user_id           UUID NOT NULL REFERENCES users(id),   -- ON DELETE deliberately NO ACTION:
                                                                -- an owner leaves only via transfer (06)
  state                   TEXT NOT NULL DEFAULT 'active'
                          CONSTRAINT ck_workspaces_state
                          CHECK (state IN ('active','suspended','offboarding')),
  -- product configuration (typed columns, carried from chat_settings shapes; NULL = app default
  -- from env, per the materialization contract at the end of this section):
  tz                      TEXT NOT NULL DEFAULT 'UTC',          -- IANA name; workspace default
  posts_per_day           INTEGER NOT NULL DEFAULT 3
                          CONSTRAINT ck_ws_posts_per_day CHECK (posts_per_day BETWEEN 1 AND 50),
  posting_hours_start     INTEGER NOT NULL DEFAULT 14
                          CONSTRAINT ck_ws_hours_start CHECK (posting_hours_start BETWEEN 0 AND 23),
  posting_hours_end       INTEGER NOT NULL DEFAULT 2
                          CONSTRAINT ck_ws_hours_end CHECK (posting_hours_end BETWEEN 0 AND 23),
                          -- start > end ⇒ the window wraps midnight (current semantics, kept)
  approval_mode           TEXT NOT NULL DEFAULT 'manual'
                          CONSTRAINT ck_ws_approval_mode CHECK (approval_mode IN ('manual','auto')),
  auto_reapprove_returning BOOLEAN NOT NULL DEFAULT false,      -- previously-posted media skips approval
  approval_ttl_minutes    INTEGER NULL,                         -- NULL = app default (05)
  dry_run_mode            BOOLEAN NOT NULL DEFAULT false,
  is_paused               BOOLEAN NOT NULL DEFAULT false,
  paused_at               TIMESTAMPTZ NULL,
  paused_by_user_id       UUID NULL REFERENCES users(id) ON DELETE SET NULL,
  repost_ttl_days         INTEGER NULL,
  skip_ttl_days           INTEGER NULL,
  caption_style           TEXT NULL
                          CONSTRAINT ck_ws_caption_style CHECK (caption_style IN ('enhanced','simple')),
  enable_ai_captions      BOOLEAN NOT NULL DEFAULT false
);
-- THE tenant root. tenant_id == workspaces.id everywhere. The composite-FK convention's
-- UNIQUE (workspace_id, id) parent keys start one level DOWN from here — children reference
-- workspaces(id) directly (their workspace_id column IS that reference); workspaces itself
-- needs only its PK.
-- The legacy chat_settings columns NOT carried here and where they went:
--   telegram_chat_id → channel_bindings.external_ref        display_name → name
--   media_source_type/root, gdrive_alerted_at → media_sources
--   active_instagram_account_id → dissolved (multi-account; §2)
--   last_post_sent_at → per-account slot anchor (§2 ig_accounts.next_slot_at derivation, 04 W.5)
--   onboarding_step/onboarding_completed → onboarding_sessions (§9)
--   enable_instagram_api → routing flag row (§8 of 04's ground rules; it is cohort routing, not config)
--   show_verbose_notifications, send_lifecycle_notifications → channel_bindings.settings

CREATE TABLE workspace_members (
  workspace_id     UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  user_id          UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role             TEXT NOT NULL
                   CONSTRAINT ck_members_role CHECK (role IN ('owner','admin','member')),
  added_by_user_id UUID NULL REFERENCES users(id) ON DELETE SET NULL,
  PRIMARY KEY (workspace_id, user_id)
);
CREATE UNIQUE INDEX uq_members_one_owner ON workspace_members (workspace_id) WHERE role = 'owner';
-- Exactly one owner-role member per workspace, by index. The pairing invariant —
-- workspaces.owner_user_id IS that member — is enforced by a deferred constraint trigger:

CREATE FUNCTION trg_workspace_owner_sync() RETURNS trigger ... ;
CREATE CONSTRAINT TRIGGER ct_workspace_owner_sync
  AFTER INSERT OR UPDATE ON workspaces
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION trg_workspace_owner_sync();
CREATE CONSTRAINT TRIGGER ct_members_owner_sync
  AFTER INSERT OR UPDATE OR DELETE ON workspace_members
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION trg_workspace_owner_sync();
-- Both triggers run the same check at COMMIT: for every affected workspace,
-- EXISTS a members row (workspace_id, owner_user_id, role='owner'). RAISE otherwise.
-- Ownership transfer is therefore only expressible as ONE transaction that updates
-- workspaces.owner_user_id, demotes the old owner row, promotes the new (06 §membership).
-- Last-owner protection falls out: deleting/demoting the owner row without the paired
-- workspaces update fails at commit.

CREATE TABLE workspace_invitations (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id    UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  email           TEXT NOT NULL,                -- lowercased; the email_otp identity it will bind to
  role            TEXT NOT NULL DEFAULT 'member'
                  CONSTRAINT ck_invite_role CHECK (role IN ('admin','member')),  -- never 'owner'
  invited_by_user_id UUID NULL REFERENCES users(id) ON DELETE SET NULL,
  state           TEXT NOT NULL DEFAULT 'pending'
                  CONSTRAINT ck_invite_state CHECK (state IN ('pending','accepted','revoked','expired')),
  expires_at      TIMESTAMPTZ NOT NULL          -- now() + 7 days at insert (05 seam)
);
CREATE UNIQUE INDEX uq_invite_live ON workspace_invitations (workspace_id, email)
  WHERE state = 'pending';
-- Web-side membership door (06 §membership). Telegram-side membership continues to arrive via
-- group-membership sync on the binding (current behavior, kept — the adapter upserts members).

CREATE TABLE channel_bindings (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  channel      TEXT NOT NULL
               CONSTRAINT ck_bindings_channel CHECK (channel IN ('telegram_group','telegram_dm')),
  external_ref TEXT NOT NULL,                   -- tg chat id as text
  state        TEXT NOT NULL DEFAULT 'active'
               CONSTRAINT ck_bindings_state CHECK (state IN ('active','paused','revoked')),
  settings     JSONB NOT NULL DEFAULT '{"v":1}',
               -- {v:1, verbose_notifications?:bool, lifecycle_notifications?:bool}
               -- absent key = app default (materialization contract below)
  CONSTRAINT uq_binding_external UNIQUE (channel, external_ref),
  CONSTRAINT uq_bindings_ws_id   UNIQUE (workspace_id, id)
);
-- A Telegram chat is one binding of a workspace (FC-1.2): 0..n per workspace — the widening from
-- #721's one-chat v1 is DELIBERATE under FC-1.3 (a Telegram identity manages one-to-many
-- workspaces; a web-only workspace has zero bindings) and recorded as decision D13 in 03.
-- Bindings exist ONLY for push channels; web is pull (§6). No 'web' enum value, by design.
```

**Settings materialization contract (the whole of it):** effective config = app defaults (env, one place: `src/config/defaults.py` successor) overridden by workspace typed columns (NULL column = inherit default) overridden, for notification prefs only, by `channel_bindings.settings` keys. No other layer exists; no config lives in JSONB except per-binding notification prefs; service code reads config through one `effective_settings(workspace_id [, binding_id])` resolver. Validation is at the service boundary (writes reject unknown keys/values); the DB CHECKs above are the backstop for typed columns.

## §2. Accounts, sources, media, quarantine

```sql
CREATE TABLE ig_accounts (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id         UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  provider_account_ref TEXT NOT NULL,           -- Meta IG user id (the REAL account)
  handle               VARCHAR(50) NULL,
  display_name         VARCHAR(100) NULL,
  state                TEXT NOT NULL DEFAULT 'active'
                       CONSTRAINT ck_ig_accounts_state
                       CHECK (state IN ('active','reauth_required','disabled','moved')),
  -- per-account schedule overrides; NULL = inherit the workspace column (§1):
  posts_per_day        INTEGER NULL
                       CONSTRAINT ck_iga_ppd CHECK (posts_per_day BETWEEN 1 AND 50),
  posting_hours_start  INTEGER NULL CONSTRAINT ck_iga_hs CHECK (posting_hours_start BETWEEN 0 AND 23),
  posting_hours_end    INTEGER NULL CONSTRAINT ck_iga_he CHECK (posting_hours_end BETWEEN 0 AND 23),
  tz                   TEXT NULL,
  -- scheduling state (the clock's O(due) columns, H3):
  next_slot_at         TIMESTAMPTZ NULL,
  last_posted_at       TIMESTAMPTZ NULL,
  CONSTRAINT uq_ig_accounts_ws_id UNIQUE (workspace_id, id)
);
CREATE UNIQUE INDEX uq_ig_account_live ON ig_accounts (workspace_id, provider_account_ref)
  WHERE state <> 'moved';
CREATE INDEX ix_ig_accounts_due ON ig_accounts (next_slot_at)
  WHERE state = 'active' AND next_slot_at IS NOT NULL;
-- Workspace-owned (FC-1); the schedule is PER ACCOUNT (a workspace's 4 accounts each have their
-- own cadence, window, tz, slot cursor — workspace columns are the defaults; 06 §multi-account).
-- The same real account in two workspaces is two rows — fork PA-1, ruling with the product owner;
-- default (a) independent-connections is what this DDL implements, and option (b) is exactly one
-- migration (a global unique on provider_account_ref over live rows). 'moved' rows are terminal
-- tombstones excluded from uniqueness so an account can move away and later return (06 §movement).
-- Real-account concurrency and Meta budgets key on provider_account_ref, never our row id
-- (§3 key 4, §8). Account-level fault quarantine lives ONLY in provider_quarantine.

CREATE TABLE provider_quarantine (
  workspace_id      UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  provider          TEXT NOT NULL
                    CONSTRAINT ck_quarantine_provider
                    CHECK (provider IN ('ig','telegram','cloudinary','gdrive')),
  scope_ref         TEXT NOT NULL DEFAULT '',   -- '' = whole (workspace, provider) pair;
                                                -- else account ref / binding id / source id
  quarantined_until TIMESTAMPTZ NOT NULL,
  strike_count      INTEGER NOT NULL DEFAULT 1,
  last_strike_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  alerted_at        TIMESTAMPTZ NULL,           -- notification dedup: re-alert only if > 1h old (05)
  reason            TEXT NULL,
  entered_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, provider, scope_ref)
);
-- THE generic T2 fault-quarantine mechanism, both grains. Entry/backoff/exit semantics:
--   entry: a provider adapter classifying a fault as quarantine-worthy upserts the row;
--          quarantined_until = now() + backoff(strike_count) over 1m/5m/30m/2h/24h (05 seam);
--          repeat entry within decay window increments strike_count, else resets to 1
--          (decay: last_strike_at older than 24h).
--   effect: the §5 claim query defers matching jobs. That is the ONLY effect.
--   exit:   passive — quarantined_until passes and the next claim proceeds; that claim IS the
--           probe (success rewrites nothing; the row is upserted again only on a fresh fault).
--   manual: clear-quarantine operator command deletes the row (audited).
--   NOT quarantine: credential revocation (state='revoked' → reauth flow, 07) and Meta cap
--   error 9 (a cap, §8) — neither writes here.

CREATE TABLE oauth_credentials (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id      UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  ig_account_id     UUID NULL,
  media_source_id   UUID NULL,
  provider          TEXT NOT NULL
                    CONSTRAINT ck_credentials_provider
                    CHECK (provider IN ('ig_login','fb_login_legacy','gdrive')),
  encrypted_payload TEXT NOT NULL,              -- Fernet/MultiFernet ciphertext (07 §rotation);
                                                -- never plaintext, never in logs/traces
  expires_at        TIMESTAMPTZ NULL,
  next_refresh_at   TIMESTAMPTZ NULL,
  state             TEXT NOT NULL DEFAULT 'active'
                    CONSTRAINT ck_credentials_state CHECK (state IN ('active','expired','revoked')),
  CONSTRAINT ck_credentials_one_owner CHECK (num_nonnulls(ig_account_id, media_source_id) = 1),
  CONSTRAINT fk_credentials_account FOREIGN KEY (workspace_id, ig_account_id)
    REFERENCES ig_accounts (workspace_id, id) ON DELETE CASCADE,
  CONSTRAINT fk_credentials_source  FOREIGN KEY (workspace_id, media_source_id)
    REFERENCES media_sources (workspace_id, id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX uq_credential_per_account ON oauth_credentials (workspace_id, ig_account_id, provider)
  WHERE ig_account_id IS NOT NULL;
CREATE UNIQUE INDEX uq_credential_per_source  ON oauth_credentials (workspace_id, media_source_id, provider)
  WHERE media_source_id IS NOT NULL;
CREATE INDEX ix_credentials_refresh_due ON oauth_credentials (next_refresh_at)
  WHERE state = 'active' AND next_refresh_at IS NOT NULL;
-- Typed XOR owner FKs (not polymorphic) so the composite-FK convention holds on the MOST
-- sensitive table (C4). The per-account unique keys by provider, which is what lets one account
-- hold an ig_login and a fb_login_legacy row simultaneously during G-phase (legacy 040 semantics).
-- fb_login_legacy is structurally closed to new rows at L.6:
--   ALTER TABLE oauth_credentials ADD CONSTRAINT ck_no_new_fb_legacy
--     CHECK (provider <> 'fb_login_legacy') NOT VALID;
-- (existing rows tolerated, new rows impossible — VALIDATEd then dropped with the enum value at
-- the FC-4 sunset G.2, together with uq/ix cleanup.)

CREATE TABLE media_sources (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id         UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  provider             TEXT NOT NULL
                       CONSTRAINT ck_sources_provider CHECK (provider IN ('gdrive')),
  config               JSONB NOT NULL,          -- {v:1, folder_ref:text, root_name?:text}
  sync_checkpoint      JSONB NULL,              -- {v:1, page_token?:text, cursor?:text}
  next_sync_at         TIMESTAMPTZ NULL,
  state                TEXT NOT NULL DEFAULT 'active'
                       CONSTRAINT ck_sources_state CHECK (state IN ('active','paused','error')),
  last_sync_success_at TIMESTAMPTZ NULL,
  alerted_at           TIMESTAMPTZ NULL,        -- source-disconnect alert dedup (legacy gdrive_alerted_at)
  CONSTRAINT uq_sources_ws_id UNIQUE (workspace_id, id)
);
CREATE INDEX ix_sources_sync_due ON media_sources (next_sync_at)
  WHERE state = 'active' AND next_sync_at IS NOT NULL;

CREATE TABLE media_items (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id      UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  source_id         UUID NOT NULL,
  provider_file_ref TEXT NOT NULL,              -- Drive file id (legacy source_identifier)
  content_hash      TEXT NOT NULL,              -- SHA256 (legacy file_hash)
  media_kind        TEXT NOT NULL
                    CONSTRAINT ck_media_kind CHECK (media_kind IN ('image','video')),
  mime_type         VARCHAR(100) NULL,
  file_name         TEXT NOT NULL,
  file_size         BIGINT NULL,
  category          TEXT NULL,
  title             TEXT NULL,
  caption           TEXT NULL,
  generated_caption TEXT NULL,
  link_url          TEXT NULL,
  tags              TEXT[] NULL,
  custom_metadata   JSONB NULL,
  thumbnail_url     TEXT NULL,
  state             TEXT NOT NULL DEFAULT 'available'
                    CONSTRAINT ck_media_state CHECK (state IN ('available','unsupported','removed')),
  times_posted      INTEGER NOT NULL DEFAULT 0,
  last_posted_at    TIMESTAMPTZ NULL,
  CONSTRAINT uq_media_ws_id  UNIQUE (workspace_id, id),
  CONSTRAINT fk_media_source FOREIGN KEY (workspace_id, source_id)
    REFERENCES media_sources (workspace_id, id) ON DELETE CASCADE,
  CONSTRAINT uq_media_dedup  UNIQUE (workspace_id, content_hash)
);
CREATE INDEX ix_media_selection ON media_items (workspace_id, state, category);
CREATE INDEX ix_media_provider_ref ON media_items (workspace_id, source_id, provider_file_ref);
-- uq_media_dedup: dedup is per-workspace BY SCHEMA; a global hash namespace is inexpressible (R4).
-- Legacy file_hash is NOT unique today (duplicates exist in production) — the W.3 track carries a
-- human-gated dedup remediation (existing dedup-media tooling) with a zero-duplicates gate BEFORE
-- this constraint lands (04 W.3), same pattern as 0.3's history remediation.
-- Legacy columns not carried: cloud_* transit columns (transit state is per-attempt:
-- post_intents.transit_asset_ref), instagram_media_id/backfilled_at (posted evidence is per-intent:
-- §3 ig_media_id; legacy values ride the W.4 history backfill), is_active+unsupported flags (state),
-- file_path (Drive path context folds into file_name; identity is provider_file_ref),
-- times_posted stays as a workspace-level advisory aggregate (per-account recency = post_locks).

CREATE TABLE post_locks (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id         UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  media_item_id        UUID NOT NULL,
  ig_account_id        UUID NULL,               -- NULL = workspace-wide; set = account-scoped
  kind                 TEXT NOT NULL
                       CONSTRAINT ck_locks_kind
                       CHECK (kind IN ('recent','skip','reject','unsupported','seasonal','hold')),
  expires_at           TIMESTAMPTZ NULL,        -- NULL = permanent
  created_by_intent_id UUID NULL,
  created_by_user_id   UUID NULL REFERENCES users(id) ON DELETE SET NULL,
  CONSTRAINT fk_locks_media   FOREIGN KEY (workspace_id, media_item_id)
    REFERENCES media_items (workspace_id, id) ON DELETE CASCADE,
  CONSTRAINT fk_locks_account FOREIGN KEY (workspace_id, ig_account_id)
    REFERENCES ig_accounts (workspace_id, id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX uq_lock_ws_scope ON post_locks (workspace_id, media_item_id, kind)
  WHERE ig_account_id IS NULL;
CREATE UNIQUE INDEX uq_lock_acct_scope ON post_locks (workspace_id, media_item_id, kind, ig_account_id)
  WHERE ig_account_id IS NOT NULL;
CREATE INDEX ix_locks_expiry ON post_locks (expires_at) WHERE expires_at IS NOT NULL;
-- Scope semantics (06 §multi-account): 'recent' is ACCOUNT-scoped (account A posting item X
-- yesterday does not block account B tomorrow); all human-judgment kinds ('skip','reject',
-- 'seasonal','hold') and 'unsupported' are WORKSPACE-scoped (a human said no to the content, or
-- the file is unusable — true for every account). Selection consults: workspace-wide locks
-- always; account-scoped locks for the account being scheduled.
-- Legacy kind mapping (W.3): recent_post→recent, skip→skip, permanent_reject→reject,
-- manual_hold→hold, seasonal→seasonal.
```

## §3. The intent ledger (heart of the system)

One durable row per posting attempt, from scheduling to a single immutable terminal state, replacing the `posting_queue`/`posting_history` split whose seam bred the known bug family (RF-G1). Three derivations converged on this shape (`03` D1). Terminality is **database-enforced** — the machinery is §4.

```sql
CREATE TABLE post_intents (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id         UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  ig_account_id        UUID NOT NULL,
  media_item_id        UUID NOT NULL,
  state                TEXT NOT NULL DEFAULT 'scheduled' CONSTRAINT ck_intent_state CHECK (state IN (
                         'scheduled','prompt_pending','awaiting_approval','approved',
                         'publishing','publishing_ambiguous','review_required',      -- working
                         'posted','skipped','rejected','expired','failed','cancelled' -- TERMINAL
                       )),
  cancel_requested     BOOLEAN NOT NULL DEFAULT false,     -- overlay flag, not a state (C9)
  schedule_slot_at     TIMESTAMPTZ NOT NULL,
  approval_mode        TEXT NOT NULL
                       CONSTRAINT ck_intent_approval CHECK (approval_mode IN ('auto','manual')),
  approved_by_user_id  UUID NULL REFERENCES users(id) ON DELETE SET NULL,
  provider_account_ref TEXT NOT NULL,           -- immutable copy from ig_accounts at creation (key 4)
  publish_step         TEXT NOT NULL DEFAULT 'none' CONSTRAINT ck_intent_step CHECK (publish_step IN
                         ('none','transit_uploaded','container_created','container_ready',
                          'publish_called','effect_confirmed')),
  ig_container_id      TEXT NULL,               -- persisted BEFORE the publish call (R1)
  ig_media_id          TEXT NULL,               -- the published media id — outcome evidence
  ig_permalink         TEXT NULL,
  transit_asset_ref    TEXT NULL,               -- Cloudinary public id for FC-3.5 reap
  cap_consumed_on      DATE NULL,               -- the account-local calendar day this intent debited (R2)
  cap_refunded_at      TIMESTAMPTZ NULL,        -- set iff the debit was returned (failed after debit)
  attempts_by_step     JSONB NOT NULL DEFAULT '{"v":1}',
                       -- {v:1, <step>:{count:int, generation:int, last_error_class?:text}}
  last_error           JSONB NULL,              -- {v:1, class:text, provider_code?:text, message:text,
                       --  evidence?:object}  — reconciler evidence lands here (§6)
  legacy_queue_item_id UUID NULL,               -- W.6 card-mapping column; dropped at W.5 contract
  entered_state_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT fk_intent_account FOREIGN KEY (workspace_id, ig_account_id)
    REFERENCES ig_accounts (workspace_id, id),          -- NO CASCADE: intents outlive account rows
                                                        -- only via workspace offboarding; account
                                                        -- deletion is forbidden while intents exist
                                                        -- (accounts terminalize to 'moved'/'disabled')
  CONSTRAINT fk_intent_media FOREIGN KEY (workspace_id, media_item_id)
    REFERENCES media_items (workspace_id, id),          -- NO CASCADE: same reason — media rows go
                                                        -- state='removed', never DELETE, while
                                                        -- referenced; offboarding deletes workspace-first
  -- state-completeness CHECKs: the terminal row IS the complete outcome (R3):
  CONSTRAINT ck_posted_complete CHECK (
    state <> 'posted' OR (ig_container_id IS NOT NULL AND publish_step = 'effect_confirmed'
                          AND cap_consumed_on IS NOT NULL)),
  CONSTRAINT ck_publishing_debited CHECK (
    state NOT IN ('publishing','publishing_ambiguous') OR cap_consumed_on IS NOT NULL),
  CONSTRAINT ck_ambiguous_called CHECK (
    state <> 'publishing_ambiguous' OR publish_step IN ('publish_called','container_ready','container_created')),
  CONSTRAINT ck_refund_after_debit CHECK (cap_refunded_at IS NULL OR cap_consumed_on IS NOT NULL)
);
-- NOTE on the two NO-CASCADE composite FKs: workspace offboarding still cascades intents via the
-- workspaces FK; the composite FKs to ig_accounts/media_items deliberately restrict, so nothing
-- short of offboarding can delete a row that history references. This is the ON DELETE §0 policy
-- applied: the cascade path exists exactly once, from the tenant root.

CREATE TABLE audit_events (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  workspace_id  UUID NOT NULL,                  -- NO FK (§0 exception: audit outlives the tenant)
  entity_kind   TEXT NOT NULL,                  -- 'post_intent','job','workspace','member','credential',...
  entity_id     UUID NULL,
  from_state    TEXT NULL,
  to_state      TEXT NULL,
  actor_kind    TEXT NOT NULL CONSTRAINT ck_audit_actor CHECK (actor_kind IN
                  ('user','system','clock','reaper','reconciler','operator','migration')),
  actor_user_id UUID NULL,                      -- no FK: audit survives user deletion
  channel       TEXT NULL
                CONSTRAINT ck_audit_channel CHECK (channel IN ('telegram','web','cli','system')),
  detail        JSONB NULL                      -- {v:1, ...} — never secrets (07 §hygiene)
);
CREATE INDEX ix_audit_entity ON audit_events (workspace_id, entity_kind, entity_id, id);
CREATE INDEX ix_audit_time   ON audit_events (workspace_id, created_at);
-- Append-only IN THE DATABASE: no role holds UPDATE or DELETE on this table except
-- svc_maintenance's DELETE for the retention sweep (§7 grant matrix). Channel provenance lives
-- HERE, never on domain state (FC-2).

CREATE TABLE daily_post_counts (
  workspace_id  UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  ig_account_id UUID NOT NULL,
  local_date    DATE NOT NULL,                  -- account-effective-tz calendar day (06 §multi-account)
  count         INTEGER NOT NULL DEFAULT 0 CONSTRAINT ck_dpc_nonneg CHECK (count >= 0),
  cap_at_write  INTEGER NOT NULL,               -- the cap frozen at first debit of the day
  PRIMARY KEY (workspace_id, ig_account_id, local_date),
  CONSTRAINT fk_dpc_account FOREIGN KEY (workspace_id, ig_account_id)
    REFERENCES ig_accounts (workspace_id, id) ON DELETE CASCADE
);
-- OUR product cadence cap — never Meta's (§8). Debit/refund SQL is in §4 (the transition owns it).
```

### §3-keys. Uniqueness keys (C8 — they compose; FLAG-2 fix)

```sql
-- 1. Slot idempotency (discovery dedup): re-running slot planning cannot double-create.
CREATE UNIQUE INDEX uq_intent_slot ON post_intents (workspace_id, ig_account_id, schedule_slot_at);
-- 2. One live intent per (media, account): same item may hold live intents for two accounts.
CREATE UNIQUE INDEX uq_intent_live_subject ON post_intents (workspace_id, media_item_id, ig_account_id)
  WHERE state NOT IN ('posted','skipped','rejected','expired','failed','cancelled');
-- 3. One terminal outcome ever (R3): enforced by the §4 machinery — terminal rows are frozen and
--    no transition leaves a terminal state, so a row is terminal at most once, permanently.
-- 4. Publishing exclusivity per REAL account, across workspaces (H1, G1):
CREATE UNIQUE INDEX uq_publish_exclusive ON post_intents (provider_account_ref)
  WHERE state = 'publishing';
-- The one deliberately non-workspace-leading key; its existence-oracle leak is accepted (§7).
```

Key 1 carries no intent-kind discriminator: exactly one kind exists; the closed-set convention makes a future kind a deliberate migration that widens the key (`03` C8).

## §4. Transitions: the matrix and its database enforcement (R3)

The matrix is unchanged in substance from the first pass; what is new is that **every rule below names the database object that enforces it**. Application code (one transition service) orchestrates; the database is the authority — a raw UPDATE from any client obeys the same rules.

```sql
CREATE TABLE post_intent_transitions (        -- the legal-edge reference table (seed data = matrix below)
  from_state TEXT NOT NULL,
  to_state   TEXT NOT NULL,
  PRIMARY KEY (from_state, to_state)
);

CREATE FUNCTION trg_intent_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  -- (1) TERMINAL FREEZE: a terminal row is immutable — every column, every writer.
  IF OLD.state IN ('posted','skipped','rejected','expired','failed','cancelled') THEN
    RAISE EXCEPTION 'post_intent % is terminal (%) and immutable', OLD.id, OLD.state
      USING ERRCODE = 'check_violation';
  END IF;
  -- (2) LEGALITY: a state change must be a listed edge.
  IF NEW.state IS DISTINCT FROM OLD.state AND NOT EXISTS (
       SELECT 1 FROM post_intent_transitions t
       WHERE t.from_state = OLD.state AND t.to_state = NEW.state) THEN
    RAISE EXCEPTION 'illegal transition % -> % on post_intent %', OLD.state, NEW.state, OLD.id
      USING ERRCODE = 'check_violation';
  END IF;
  -- (3) stamp maintenance
  IF NEW.state IS DISTINCT FROM OLD.state THEN NEW.entered_state_at := now(); END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER tg_intent_guard BEFORE UPDATE ON post_intents
  FOR EACH ROW EXECUTE FUNCTION trg_intent_guard();

CREATE FUNCTION trg_intent_audit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.state IS DISTINCT FROM OLD.state THEN
    IF current_setting('app.actor_kind', true) IS NULL THEN
      RAISE EXCEPTION 'state change without app.actor_kind — anonymous writes are forbidden';
    END IF;
    INSERT INTO audit_events (workspace_id, entity_kind, entity_id, from_state, to_state,
                              actor_kind, actor_user_id, channel, detail)
    VALUES (NEW.workspace_id, 'post_intent', NEW.id, OLD.state, NEW.state,
            current_setting('app.actor_kind'),
            NULLIF(current_setting('app.actor_user_id', true), '')::uuid,
            NULLIF(current_setting('app.channel', true), ''),
            NULL);
  END IF;
  RETURN NULL;
END $$;
CREATE TRIGGER tg_intent_audit AFTER UPDATE ON post_intents
  FOR EACH ROW EXECUTE FUNCTION trg_intent_audit();

CREATE FUNCTION trg_intent_insert_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  -- runtime creates intents only in 'scheduled'; terminal-state inserts are the migration
  -- backfill's privilege (W.4), gated on its GUC — history cannot be fabricated at runtime.
  IF NEW.state <> 'scheduled'
     AND COALESCE(current_setting('app.migration_mode', true), '') <> 'on' THEN
    RAISE EXCEPTION 'post_intents are born scheduled (got %)', NEW.state;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER tg_intent_insert_guard BEFORE INSERT ON post_intents
  FOR EACH ROW EXECUTE FUNCTION trg_intent_insert_guard();
```

Why this and not a separate outcomes table: R3 needs *one immutable terminal record*; a second table holding terminal truth is the queue/history two-truths seam (RF-G1) that D1 exists to kill. The frozen intent row — freeze trigger + no-exit edges + the §3 completeness CHECKs — **is** the immutable record, and there is exactly one, structurally. R3's ledger wording is amended accordingly (`01`): "database-enforced (constraint or trigger), never application discipline."

| From | To | Actor | Guard / effect (with its enforcing object) |
|---|---|---|---|
| — | scheduled | clock (slot planner) | key-1 insert (`uq_intent_slot`); caps pre-checked advisory only |
| scheduled | prompt_pending | worker | outbox rows created for active push bindings, same tx |
| scheduled | expired | reaper | slot passed unclaimed |
| scheduled | cancelled | worker | `cancel_requested`, or workspace/account disabled |
| prompt_pending | awaiting_approval | worker | prompt delivered on ≥ 1 binding, **or** workspace has web access (FC-2) |
| prompt_pending | failed | worker | no reachable surface (all deliveries failed, no web access) |
| prompt_pending | expired | reaper | expiry passed before delivery |
| awaiting_approval | approved | user or system(auto) | manual command, or `auto_reapprove_returning` policy |
| awaiting_approval | skipped / rejected | user | terminal; `rejected` upserts a workspace-scoped reject lock |
| awaiting_approval | expired | reaper | approval window passed (`approval_ttl_minutes`); prompts superseded via outbox |
| awaiting_approval | cancelled | worker | `cancel_requested` |
| approved | publishing | worker | **one transaction** (SQL below): key-4 acquisition + atomic cap debit + lease held |
| approved | expired | reaper | approval stale beyond policy |
| approved | cancelled | worker | `cancel_requested` (pre-publish it is always honorable) |
| publishing | posted | worker | `effect_confirmed`; same tx: `times_posted`++, recent lock upsert (account-scoped), FC-3.5 reap job insert |
| publishing | publishing_ambiguous | worker | timeout/crash after `publish_called` with unconfirmed effect (R8) |
| publishing | failed | worker | terminal provider error; same tx: cap refund (below) |
| publishing | review_required | worker | poison: attempts exhausted on a retryable class (G5) |
| publishing_ambiguous | posted / failed | **reconciler only** | §6 evidence contract decides; `failed` refunds the cap |
| publishing_ambiguous | review_required | reconciler | evidence budget exhausted — a human looks |
| review_required | approved / failed / cancelled | user (operator) | explicit resolution command; audited with channel + actor |

The **approved → publishing** transaction, verbatim shape (the R2 atomic debit — no check-then-act gap):

```sql
BEGIN;
SET LOCAL app.tenant_id = :workspace_id;  SET LOCAL app.actor_kind = 'system';
-- (a) cap debit: insert-or-increment, denied atomically at the cap
INSERT INTO daily_post_counts AS d (workspace_id, ig_account_id, local_date, count, cap_at_write)
VALUES (:ws, :acct, :local_date, 1, :effective_cap)
ON CONFLICT (workspace_id, ig_account_id, local_date)
  DO UPDATE SET count = d.count + 1 WHERE d.count < d.cap_at_write
RETURNING local_date;
-- zero rows returned ⇒ DENIED: leave state, reschedule the job to the account's next slot
-- (worker updates jobs.run_at; intent stays 'approved'; audit row 'cap_deferred'). Approval does
-- not expire from deferral alone — the reaper's approval-TTL clock keeps running regardless.
-- (b) state flip — the trigger validates the edge; key 4 acquires publishing exclusivity
UPDATE post_intents SET state = 'publishing', cap_consumed_on = :local_date
  WHERE id = :intent AND state = 'approved';
-- unique_violation on uq_publish_exclusive ⇒ another workspace is publishing this real account:
-- treated exactly as a cap denial (defer, no error surfaced to the user)
COMMIT;
```

Cap refund (the `publishing → failed` and ambiguous→failed companion, same tx as the terminal flip): `UPDATE daily_post_counts SET count = count - 1 WHERE (workspace_id, ig_account_id, local_date) = (:ws, :acct, intent.cap_consumed_on) AND count > 0;` plus `cap_refunded_at = now()` on the intent. The refund targets **the recorded debit day** (`cap_consumed_on`), so a timezone change or midnight crossing between debit and refund cannot touch the wrong bucket. DST and tz-change semantics: `local_date` is computed from the account's effective tz *at debit time*; a same-day tz change can shift the boundary for *later* debits by at most one slot — accepted and documented (06 §multi-account).

`cancel_requested` during `publishing`/`publishing_ambiguous` is **not** honored by force (C9: `publishing → cancelled` is forbidden — and now DB-forbidden: the edge is absent from `post_intent_transitions`); the pipeline completes or reconciles, then the reconciler consults the flag only where a real choice remains. A late interaction with any terminal intent renders the terminal state and never acts (R6).

## §5. `jobs` — execution machinery (pg-only per C3)

```sql
CREATE TABLE jobs (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  kind              TEXT NOT NULL CONSTRAINT ck_jobs_kind CHECK (kind IN (
                      -- tenant kinds (workspace_id NOT NULL):
                      'publish_pipeline','deliver_outbox','sync_media_source','first_ingest_chunk',
                      'refresh_credential','revoke_workspace_credentials','reauth_prompt',
                      -- system kinds (workspace_id NULL):
                      'reconcile_ambiguous','reap_expired','reap_transit_assets','retention_sweep',
                      'comparator_run','reencrypt_credentials')),
  workspace_id      UUID NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  lane              TEXT NOT NULL CONSTRAINT ck_jobs_lane CHECK (lane IN ('interactive','bulk')),
  serialization_key TEXT NULL,
  run_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  state             TEXT NOT NULL DEFAULT 'ready' CONSTRAINT ck_jobs_state
                    CHECK (state IN ('ready','leased','succeeded','failed','review_required','cancelled')),
  cancel_requested  BOOLEAN NOT NULL DEFAULT false,
  attempts          INTEGER NOT NULL DEFAULT 0,
  max_attempts      INTEGER NOT NULL,
  deadline_at       TIMESTAMPTZ NULL,
  locked_by         TEXT NULL,
  locked_until      TIMESTAMPTZ NULL,
  lease_token       UUID NULL,
  payload           JSONB NOT NULL DEFAULT '{"v":1}',
  CONSTRAINT ck_jobs_system_kinds CHECK (
    (workspace_id IS NOT NULL) OR kind IN
      ('reconcile_ambiguous','reap_expired','reap_transit_assets','retention_sweep',
       'comparator_run','reencrypt_credentials'))
);
CREATE INDEX ix_jobs_claim ON jobs (lane, run_at) WHERE state = 'ready';
CREATE INDEX ix_jobs_lease_expiry ON jobs (locked_until) WHERE state = 'leased';
CREATE UNIQUE INDEX uq_jobs_serialized_lease ON jobs (serialization_key) WHERE state = 'leased';
-- uq_jobs_serialized_lease is THE serialization guard: two leased jobs with one key are
-- impossible by constraint, not by claim-query discipline. The claim query merely avoids
-- most conflicts; the index makes the race lose correctly.
```

**Job-kind registry (closed; the CHECK above is its enforcement).** Per kind: payload schema (all `{v:1, …}`), producer, lane, serialization key:

| kind | payload | producer | lane | serialization_key |
|---|---|---|---|---|
| publish_pipeline | `{intent_id}` | approval flip / clock (auto mode) | bulk | `ig:<provider_account_ref>` |
| deliver_outbox | `{binding_id}` | outbox insert / sender reschedule | interactive | `tg:<binding_id>` (per-chat send ordering) |
| sync_media_source | `{source_id, reason:'pre_slot'\|'demand'\|'baseline'}` | clock / command | bulk | `src:<source_id>` |
| first_ingest_chunk | `{source_id, page_token}` | sync job (chains) | bulk | `src:<source_id>` |
| refresh_credential | `{credential_id}` | clock (next_refresh_at due) | bulk | `cred:<credential_id>` |
| revoke_workspace_credentials | `{}` (workspace-keyed) | offboarding workflow (06) | bulk | `ws:<workspace_id>` |
| reauth_prompt | `{ig_account_id}` | G.1 campaign clock | bulk | `ig:<provider_account_ref>` |
| reconcile_ambiguous | `{}` | clock (recurring) | bulk | `'reconciler'` (singleton by key) |
| reap_expired | `{}` | clock (recurring) | bulk | `'reaper'` |
| reap_transit_assets | `{}` | clock (recurring, FC-3.6) | bulk | `'transit-reaper'` |
| retention_sweep | `{}` | clock (recurring) | bulk | `'retention'` |
| comparator_run | `{track:text}` | clock (nightly, per active track) | bulk | `cmp:<track>` |
| reencrypt_credentials | `{key_generation:int}` | rotation runbook (07) | bulk | `'reencrypt'` |

Producer authorization is code-level (each producer is one named service); the DB-level guard is the kind CHECK + the nullability pairing + RLS (§7). A new kind is a migration (CHECK edit) plus a registry row here — the closed-set convention applied to work itself.

**The claim, verbatim shape (its race guard is the unique index, not the query):**

```sql
BEGIN;  -- role: svc_claim (§7); its own short transaction, THEN execution runs tenant-scoped
WITH candidate AS (
  SELECT j.id FROM jobs j
  WHERE j.state = 'ready' AND j.lane = :lane AND j.run_at <= now()
    AND NOT EXISTS (SELECT 1 FROM jobs h                     -- serialization pre-filter
                    WHERE h.serialization_key = j.serialization_key AND h.state = 'leased')
    AND NOT EXISTS (SELECT 1 FROM provider_quarantine q      -- quarantine deferral (T2)
                    WHERE q.workspace_id = j.workspace_id
                      AND q.quarantined_until > now()
                      AND q.scope_ref IN ('', j.serialization_key))
  ORDER BY j.run_at
  LIMIT 1 FOR UPDATE OF j SKIP LOCKED
)
UPDATE jobs SET state='leased', locked_by=:worker, lease_token=gen_random_uuid(),
                locked_until = now() + :lease_interval, attempts = attempts + 1
  FROM candidate WHERE jobs.id = candidate.id
RETURNING jobs.*;
COMMIT;
-- unique_violation on uq_jobs_serialized_lease (two claimers picked different rows sharing a key):
-- retry the claim excluding that serialization_key. The quarantine check is advisory (a row
-- appearing mid-claim defers the NEXT claim) — quarantine is backoff, not a correctness gate.
```

Transitions: `ready → leased` (claim above) · `leased → succeeded | failed | review_required` (finalization CAS: `UPDATE jobs SET state=:terminal WHERE id=:id AND lease_token=:token` — zero rows = fenced, the worker aborts) · `leased → ready` (lease expiry: the expiry sweep re-readies; the stale owner's later finalization fails the CAS) · `ready → review_required` (poison) · `ready → cancelled` (cooperative; a leased job is cancelled only by its own worker at a checkpoint). The domain transaction (intent flip + counters + audit) and job finalization commit **together** — that co-location is why jobs live in Postgres (C3). Job state is execution bookkeeping only; it is never the authority on whether an external effect happened (§6). Heartbeat: one independent asyncio task per worker process extends `locked_until` for all its live leases every interval (`05`) in a single UPDATE guarded by lease tokens; a worker missing two beats is presumed dead well inside the lease; provider waits never block beats (the beat task is not in the pipeline's await chain).

## §6. Outbound effects: `channel_outbox`, `provider_operations`, and the permit protocol

```sql
CREATE TABLE channel_outbox (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id         UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  binding_id           UUID NOT NULL,
  kind                 TEXT NOT NULL CONSTRAINT ck_outbox_kind
                       CHECK (kind IN ('approval_prompt','prompt_supersede','notification','ack')),
  intent_id            UUID NULL,               -- plain UUID ref (intent may be terminal-frozen)
  payload              JSONB NOT NULL,          -- {v:1, ...} channel-NEUTRAL content
  state                TEXT NOT NULL DEFAULT 'pending' CONSTRAINT ck_outbox_state
                       CHECK (state IN ('pending','sending','sent','ambiguous','failed','superseded')),
  attempts             INTEGER NOT NULL DEFAULT 0,
  external_message_ref TEXT NULL,               -- tg message id after send
  CONSTRAINT fk_outbox_binding FOREIGN KEY (workspace_id, binding_id)
    REFERENCES channel_bindings (workspace_id, id) ON DELETE CASCADE
);
CREATE INDEX ix_outbox_due ON channel_outbox (binding_id, created_at) WHERE state = 'pending';

CREATE TABLE provider_operations (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id        UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  intent_id           UUID NOT NULL,            -- plain UUID ref (see fk note on post_intents)
  provider            TEXT NOT NULL CONSTRAINT ck_ops_provider CHECK (provider IN ('ig')),
  op_kind             TEXT NOT NULL CONSTRAINT ck_ops_kind
                      CHECK (op_kind IN ('container_create','publish')),
  business_key        TEXT NOT NULL,
  generation          INTEGER NOT NULL DEFAULT 1,
  state               TEXT NOT NULL DEFAULT 'permitted' CONSTRAINT ck_ops_state
                      CHECK (state IN ('permitted','succeeded','failed','ambiguous')),
  lease_token         UUID NOT NULL,            -- the lease that authorized this permit
  request_fingerprint TEXT NULL,
  response_ref        JSONB NULL,               -- {v:1, container_id?|media_id?|status_code?|error?}
  CONSTRAINT uq_ops_business_key UNIQUE (business_key)
);
CREATE INDEX ix_ops_ambiguous ON provider_operations (created_at) WHERE state = 'ambiguous';
```

**Scope: the rail covers `ig` only.** Cloudinary is deliberately OFF the rail (first-pass design carried it; cut on review): upload and destroy are recoverable effects — a duplicate transit upload is a second short-lived asset, a missed destroy is caught by the FC-3.6 hard-TTL sweep. Their execution state lives in job attempts + `audit_events`, weightless. Instagram publish is irreversible and customer-visible; it gets the full machinery. Telegram delivery lives in the outbox (below). Drive is read-only.

**The permit protocol (stale-worker fencing at the network boundary — the DB stops the call *before* it happens):**

1. Inside the pipeline, immediately before any IG side-effecting call, the worker opens a short transaction and inserts the permit: `INSERT INTO provider_operations (…, business_key, lease_token) VALUES (…)` **in the same transaction as** a lease CAS re-check: `SELECT 1 FROM jobs WHERE id=:job AND lease_token=:token FOR SHARE` — zero rows ⇒ the lease moved on; the insert rolls back; the worker aborts with **no network call made**.
2. Only after that transaction commits may the worker issue the provider call. Crash before commit: no permit, no call, clean resume. Crash after commit, before/during the call: a `permitted` row with no `response_ref` — the resumed pipeline (new lease) finds it and **does not re-call**: it marks the op `ambiguous` and the intent `publishing_ambiguous`; the reconciler owns it from there. (For `container_create`, whose loss is recoverable, the reconciler's confirmed-safe verdict bumps `generation` and permits a fresh attempt; for `publish`, never.)
3. Completion: the worker records the outcome (`succeeded` + `response_ref`, or `failed` + error class) and advances `publish_step` in the same domain transaction (which also re-CASes the lease token — a fenced worker cannot record outcomes either).

**Business-key formats (closed):** `ig:container:<intent_id>:<generation>` · `ig:publish:<intent_id>:<generation>`. `generation` increments **only** on a confirmed-safe failure of the same op kind (provider said the previous attempt definitively did not happen); it never increments out of ambiguity. One business key = at most one intended provider effect (TT:P0-06's gate), and `uq_ops_business_key` is the object that enforces it.

**Send-state authority is single-homed:** the outbox row IS the delivery record and the only authority on "did this send" — the sender *job* carries execution state only. The `ambiguous` outbox state carries R8's no-blind-retry rule, resolved per kind, because Telegram provides no general read-back for a lost `sendMessage` response (there is no "list my sent messages" API): **`notification`/`ack`** — retry once after backoff, then `failed`; a duplicate notification is the accepted cost, bounded at one. **`approval_prompt`** — resend; two live cards for one intent are tolerable because both resolve to the same intent and terminal-state-first reads (R6) make whichever is tapped later render the terminal state; on any intent state change, supersede-all (`prompt_supersede` rows target every known `external_message_ref`; a card whose ref was lost simply ages out under R6 semantics). **Edits always go supersede-then-send** — never edit-in-place on an ambiguous ref. This is the complete ambiguity policy; there is no "stamp-heal" beyond the ref capture on a late Telegram response.

**The reconciliation algorithm (ambiguous IG publish — evidence contract, R8):**

- Evidence source 1 (authoritative): the persisted container id. `GET /{ig_container_id}?fields=status_code` — `PUBLISHED` ⇒ the effect happened: fetch media id/permalink if retrievable, terminalize `posted` (cap debit stands). `ERROR` / `EXPIRED` ⇒ effect did not happen: terminalize `failed`, refund the cap. `IN_PROGRESS`/`FINISHED` ⇒ still moving: re-poll within budget.
- Evidence source 2 (corroboration only, never sole basis for `posted`): the account's stories list (`GET /{ig_user_id}/stories`), matched on timestamp window around `publish_called` — used to annotate evidence, not to decide.
- Budget: poll source 1 at the `05` reconciler cadence until container expiry (~24 h). Expiry reached with no `PUBLISHED` observation ⇒ one final stories check for the window ⇒ still inconclusive ⇒ `review_required`, with the full evidence trail written to `last_error.evidence` (statuses seen, timestamps, stories-window result) for the operator surface (06 §failure-visibility).
- The endpoint semantics above (container `status_code` vocabulary, stories lookback validity) join 0.4's primary-doc verification list — they are platform inputs under `05`'s revision rule.
- Consequence for R1's honest wording (`01`): at-most-once *intended* publish is guaranteed by permit + fence + persisted container; the residual lost-response window always lands in `publishing_ambiguous`; a duplicate then requires both a lost response **and** an operator resolution error — the machinery never blind-retries.

`service_runs` (legacy ops bookkeeping) survives unchanged during the program and is retired at S.4 (`03`).

## §7. RLS: policies, roles, and the tenancy backstop (C4)

The first pass said "system actors use dedicated roles with explicit predicates" — insufficient (review B§1): without a policy or bypass, a role sees zero rows regardless of its predicates. The composing design:

- **No `BYPASSRLS` anywhere. No owner-role runtime connections.** Every cross-tenant capability is a named, reviewable `CREATE POLICY` statement targeting a named role.
- **Tenant roles** (`svc_ingress`, `svc_worker`): constant-expression tenant policies on every workspace-scoped table — `USING (workspace_id = current_setting('app.tenant_id')::uuid)` (and identical `WITH CHECK`). Every tenant transaction opens with `SET LOCAL app.tenant_id = …` set by the UoW factory, which takes `tenant_id` as a required constructor argument — a UoW without a tenant is unconstructible in code, and a query without one fails closed in the DB (`current_setting` on an unset GUC errors; the policy denies). Transaction-pooled connection reuse is safe because `SET LOCAL` dies with the transaction.
- **System roles, each with explicit `USING (true)` policies on an enumerated table list** — the enumeration is the security review surface:

| Role | Policies (`USING (true)`) on | Writes permitted (grants) | Used by |
|---|---|---|---|
| `svc_claim` | jobs, provider_quarantine | UPDATE jobs | the claim transaction only (§5) |
| `svc_clock` | workspaces, ig_accounts, media_sources, oauth_credentials (read); jobs, post_intents (insert) | INSERT jobs, INSERT post_intents (scheduled only — §4 insert guard), UPDATE next_* columns | scheduler-as-clock tick |
| `svc_maintenance` | post_intents, jobs, channel_outbox, provider_operations, daily_post_counts, provider_quarantine, oauth_credentials, media_items, audit_events (insert), workspaces | UPDATE the swept tables; DELETE only: retention targets (audit_events, jobs, channel_outbox, provider_operations per `05` retention) + workspaces (offboarding cascade root) | reapers, reconciler, retention, comparator, offboarding |
| `svc_migration` | all (during tracks) | per-track INSERT/UPDATE; sets `app.migration_mode` | backfills, dual-write mirrors |

- **Execution-context flow:** claim commits under `svc_claim`; the worker then opens the job's domain transactions as `svc_worker` with `SET LOCAL app.tenant_id = job.workspace_id` (system jobs run as `svc_maintenance` instead — they are the enumerated sweeps). One connection may serve both phases: `SET LOCAL ROLE` inside each transaction selects the personality; nothing survives the commit.
- **Grant matrix (beyond RLS):** runtime roles hold no DELETE on tenant tables (§0); nobody but `svc_maintenance` holds DELETE on anything; `audit_events` grants: INSERT to all service roles, UPDATE to none, DELETE to `svc_maintenance` only; `oauth_credentials.encrypted_payload` is SELECTable only by `svc_worker`/`svc_ingress` (credentials service paths) — `svc_clock` reads scheduling columns through a view without the payload column (`vw_credentials_schedule`).
- **The staged NOT NULL procedure (the only legal way this plan adds NOT NULL to a populated table — the first pass's "`NOT NULL` added `NOT VALID`" does not exist in PostgreSQL):**
  1. `ALTER TABLE t ADD CONSTRAINT ck_t_col_nn CHECK (col IS NOT NULL) NOT VALID;`
  2. backfill (batched, six-stage machine rules);
  3. `ALTER TABLE t VALIDATE CONSTRAINT ck_t_col_nn;`
  4. `ALTER TABLE t ALTER COLUMN col SET NOT NULL;`  — PostgreSQL uses the validated CHECK to skip the scan
  5. `ALTER TABLE t DROP CONSTRAINT ck_t_col_nn;`
- Enablement discipline per table: zero-NULL gates before and after cutover; the RLS harness runs as the exact runtime roles, no owner privileges, no session-affinity assumptions (#722 P0-09, kept verbatim).
- Known limit, mitigated not ignored: integrity errors bypass RLS and can act as cross-tenant existence oracles. Every unique key on tenant-scoped tables leads with `workspace_id` and ids are UUIDs, so no enumerable oracle exists; `uq_publish_exclusive` (key 4) is the one deliberate exception, leaking only "some workspace is publishing to this real account" — accepted, documented, and kept out of logs/user-visible errors by the 07 hygiene rule (internal ids in every log line and error payload, never `provider_account_ref`).

## §8. Two caps, never conflated

- **Product cadence cap** — ours: `daily_post_counts` in the account's effective tz, calendar-day semantics, atomically debited in the approved→publishing transaction (§4 SQL), refunded on failure against the recorded debit day. The only cap we count locally.
- **Meta publish cap** — theirs: 25 per rolling 24 h per real account, enforced by Meta on the publish step (error 9). Never counted locally (rolling window + out-of-band posts make local counters wrong by construction — vault doc). On error 9: defer with `available_at` derived from provider-reported usage — a cap, not a fault, so no quarantine row.
- **The advisory pre-check is lazy, inline, and shared — never a poller.** There is **no background refresh job** (no such kind exists in the §5 registry — an implementer following this plan cannot build one). The check runs inside the publish pipeline, immediately before the §4 flip transaction, against an in-process cache keyed on **`provider_account_ref`** (shared across duplicate workspace rows of one real account), TTL per `05`. Worst-case provider load is therefore ≤ one usage query per publish attempt — strictly bounded by publish traffic itself, never by account count. (First-pass defect, review B§6: a 5-min per-account cache read as an eager refresher is up to 1,000 queries/min at the FC-0 envelope against ~87 publishes/min of real work — the advisory mechanism would have manufactured the very load it advises about, buying no correctness since error 9 is authoritative regardless.) Miss/stale/error on the pre-check ⇒ proceed to the flip; error 9 remains the arbiter.

## §9. Legacy → target mapping (all 14 current tables + ledger accounted for)

Every re-key runs on the six-stage machine (`04` §Ground rules). Column-level mapping tables live in each track's spec (`04` Phases F/W) — this table is the disposition index. Legacy naive `TIMESTAMP` columns convert with `AT TIME ZONE 'UTC'` (§0), everywhere, no exceptions.

| Current (`origin/main`) | Target disposition |
|---|---|
| `users` | `users` (state from `is_active`; role dissolves into memberships) + `user_identities(provider='telegram')` (telegram_user_id/username/names) |
| `chat_settings` | split: tenant config columns → `workspaces` (§1 lists the full column mapping inline); chat facts → `channel_bindings(channel='telegram_group')`; media source config + `gdrive_alerted_at` → `media_sources`; `active_instagram_account_id` dissolves (multi-account); `last_post_sent_at` seeds per-account `next_slot_at` at W.5 cutover; onboarding columns → `onboarding_sessions` |
| `user_chat_memberships` | `workspace_members` (instance_role maps 1:1: owner/admin/member; `is_active=false` rows are dropped with an audit record — membership is presence, not a flag, in the target) |
| `user_interactions` | `audit_events` (append-only, channel-tagged; `interaction_*` → entity_kind/detail) |
| `instagram_accounts` | `ig_accounts` — **fan-out**: the legacy row is global identity + per-chat selection; the target row is per-workspace ownership. One target row per (workspace, `instagram_account_id`) pair derived from `api_tokens` ownership (W.1 mapping rule; `instagram_account_id` → `provider_account_ref`, `instagram_username` → handle) |
| `api_tokens` | `oauth_credentials` (typed owner FKs; `service_name`+`auth_method` → provider: instagram×instagram_login → `ig_login`, instagram×oauth/manual → `fb_login_legacy`, google_drive → `gdrive`; `token_value` ciphertext carried as-is; `revoked_at IS NOT NULL` → state `revoked`). Non-IG/Drive rows: none exist (verified). The 004/008 constraint residue is reconciled by the 0.2 runner-formalization fix-forward migration before any of this |
| `media_items` | `media_items` re-keyed (`chat_settings_id`→workspace via its chat; `source_type/identifier` → source_id/provider_file_ref; NULL-tenant legacy rows follow the 044 sole-tenant rule, ratified). **Precondition: per-workspace hash dedup remediation (W.3 gate)** |
| `media_posting_locks` | `post_locks` (kind mapping in §2; `locked_until`→expires_at; permanent = NULL, kept) |
| `posting_queue` | `post_intents` working states — pending→scheduled · processing→prompt_pending · sent_unconfirmed→prompt_pending · delivered→awaiting_approval · publishing→publishing · failed→failed(terminal); `instagram_container_id`→ig_container_id; telegram message/chat ids → `channel_outbox` rows with `external_message_ref` |
| `posting_history` | `post_intents` terminal states (posted/failed/skipped/rejected/expired map 1:1; `posting_method`/usernames → audit detail; `instagram_media_id`/`instagram_story_id`/permalink → ig_media_id/ig_permalink; `queue_item_id` → legacy_queue_item_id) — inserted under `app.migration_mode` (§4 insert guard) |
| `category_post_case_mix` | **kept row-shaped** (it is a Type 2 SCD table today — `workspaces.category_mix` JSONB from the first pass is struck): `category_post_case_mix` re-keyed to `workspace_id`, SCD semantics unchanged, sum-to-1 stays service-enforced (a cross-row DB constraint would need a deferred aggregate trigger; not worth its complexity — recorded trade-off) |
| `onboarding_sessions` | kept, re-keyed: `user_id` stays; `pending_chat_settings_id` → `pending_workspace_id`; step vocabulary widened for the web path (07 §signup): `naming`,`awaiting_group`,`connect_email`,`complete`; 24 h expiry + `UNIQUE(user_id)` kept |
| `audit_log` | merged into `audit_events` (entity_type/action/field/old/new → entity_kind + detail; rows migrated verbatim into `detail`) |
| `service_runs` | kept as-is + nullable `workspace_id`; retired at S.4 (`03`) |
| `schema_version` | superseded by the 0.2 runner's `schema_migrations` ledger (`04` 0.2 — richer metadata; old table retained read-only until W-phase contract) |
| `waitlist_signups` | **out of scope, permanently**: owned by the landing site's Drizzle ORM; no Python migration may touch it (existing repo rule, carried forward) |
