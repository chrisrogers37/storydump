# Domain model

This file is DDL-complete: every table below is stated as executable PostgreSQL, and every invariant claimed by this plan names the database object that enforces it. Prose between DDL blocks explains *why*; the DDL is the decision. Bare R/T/H ids cite `01` §Requirements ledger. Current-state (legacy) column facts are grounded in this repository itself — `src/models/*.py`, `scripts/migrations/001–049`, `scripts/setup_database.sql` are the verification sources — and the per-column legacy mapping each track needs is restated in `04`; no external extraction artifact is required reading.

## §0. Conventions (normative — DDL below relies on these; they are decisions, not suggestions)

- **Ids:** `id UUID PRIMARY KEY DEFAULT gen_random_uuid()` on every table unless a natural PK is shown. (Legacy tables mix `uuid_generate_v4()`/`gen_random_uuid()`; new DDL uses `gen_random_uuid()` only — no extension dependency.)
- **Time:** every new timestamp column is `TIMESTAMPTZ`. Legacy columns are naive `TIMESTAMP`, UTC by convention, **with exactly three exceptions already `TIMESTAMPTZ` on `main`** (pass-4 anchor): `chat_settings.last_post_sent_at` (migration 019), `chat_settings.gdrive_alerted_at` (031), `api_tokens.revoked_at` (032). **Every backfill of a naive column converts with `AT TIME ZONE 'UTC'`** — mandatory in track DDL, a bare cast is a review-blocking defect — and the three tz-aware columns copy **as-is**: applying the conversion clause to them would corrupt the value, so each track's mapping states per column which rule applies.
- **Stamps:** `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`, `updated_at TIMESTAMPTZ NOT NULL DEFAULT now()` on every table; `updated_at` maintained by the shared `trg_touch_updated_at` trigger (one function, defined once here; each table's DDL block prints its own one-line `CREATE TRIGGER`). Exceptions — the append/insert-only class carries `created_at` only, no touch trigger, because a touch column could never legally change: `audit_events` (UPDATE granted to nobody), `post_intent_transitions` (matrix changes are INSERT/DELETE of edge rows), `command_dedup` (rows are written once; replay handling only reads), and `rate_counters` (only `count` moves; age is immutable in `window_start`, which is what its retention keys on). `updated_at` is load-bearing on the other machinery tables: the §5 retention indexes and sweeps key terminal-row age on it. **Nothing in this file is implicit:** every stamp column and every trigger statement appears literally in the blocks below.

```sql
CREATE FUNCTION trg_touch_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END $$;
```

- **Replay order (normative):** the advertised DDL replays from empty **in file order — this file top-to-bottom, then `07`'s blocks in their file order**. Ordering is therefore load-bearing: a block references only objects printed above it. `04` 0.2's CI carries a named fixture that extracts the advertised blocks and replays them verbatim; an edit that breaks file-order replay fails that gate.
- **Enums are TEXT + named CHECK constraints**, never native `ENUM` types. Closed sets whose members must be *removable* (e.g. `fb_login_legacy` dies at G.2) make native enums a liability (`DROP VALUE` does not exist); CHECK text is also what the existing enum-SSOT parity gate (models ↔ latest migration DDL) already verifies. Adding/removing a value = enum edit + migration editing the named CHECK, held in lockstep by that gate.
- **Tenant scoping:** every tenant-scoped table has `workspace_id UUID NOT NULL`; wherever it references another tenant-scoped table it uses a **composite FK** `(workspace_id, <ref>) REFERENCES parent (workspace_id, id)` so a cross-workspace reference is inexpressible (#721 D12, kept). Composite FKs require the parent-side `UNIQUE (workspace_id, id)` — those indexes are part of the parent DDL below.
- **ON DELETE policy (three classes, no per-FK improvisation):**
  1. Workspace-rooted and tenant-child edges: `ON DELETE CASCADE`. Deletion of tenant data happens exactly once — at offboarding (T3), as `svc_maintenance` deleting the `workspaces` row after the `06` offboarding workflow; the cascade is the mechanism.
  2. `users` references from tenant tables (`approved_by_user_id`, `added_by_user_id`, …): `ON DELETE SET NULL` — history survives a departed human (columns are nullable attribution, never authorization).
  3. `audit_events.workspace_id` carries **no FK** — the one deliberate exception: audit must outlive the tenant it describes; the retention sweep (`05`) is its only deleter. RLS still applies to it.
  Runtime roles hold **no DELETE grant** on any tenant table (§7 grant matrix), so "runtime never deletes" is structural, not discipline.
- **JSONB payloads are versioned:** every JSONB document column carries `"v": <int>` at top level; readers accept `v` and `v-1` (the N-1 rule, `04` ground rules); a payload without `v` is invalid at the service boundary — and on the NOT NULL payload columns the database backstops it with a cheap shape CHECK, `jsonb_typeof(<col>->'v') = 'number'`, named per table below. Field schemas for each payload are stated at the column that owns them.
- **Named constraints only** (`ck_`, `uq_`, `fk_`, `ix_` prefixes) — auto-generated names caused the legacy api_tokens 004/008 drop-miss; every constraint below is named so later DDL can target it.
- **Writer identity GUCs:** every transaction that mutates domain state sets `app.actor_kind` (and `app.actor_user_id` / `app.channel` when a human/channel is involved) via `SET LOCAL`. Database enforcement covers a **named set**: the `§4` intent triggers (`post_intents` state changes) and the `§4` governance audit triggers (`workspaces`, `workspace_members`, `oauth_credentials`, `ig_accounts`, `channel_bindings`) raise on a missing `app.actor_kind` — on those tables anonymous writes are impossible, in the database, for every writer including a psql session. High-churn machinery tables (`jobs`, `channel_outbox`, `provider_operations`, `daily_post_counts`, `rate_counters`, `command_dedup`) are excluded by declared design: their rows are execution bookkeeping whose authority trail lives in the ledger transitions and audit rows the covered writers produce.

## §1. Identity and tenancy (FC-1, FC-2)

```sql
CREATE TABLE users (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  primary_email  TEXT NULL,
  state          TEXT NOT NULL DEFAULT 'active'
                 CONSTRAINT ck_users_state CHECK (state IN ('active','disabled')),
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_users_primary_email UNIQUE (primary_email)
);
CREATE TRIGGER tg_touch_users BEFORE UPDATE ON users
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();
-- Platform-neutral human (FC-1.3): NO telegram columns. Telegram-only users have NULL email.
-- users.state='disabled' denies access at the ONE ingress gate; rows/memberships survive.

CREATE TABLE user_identities (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  provider     TEXT NOT NULL
               CONSTRAINT ck_user_identities_provider CHECK (provider IN ('telegram','google')),
  external_id  TEXT NOT NULL,          -- tg user id (as text) | google OIDC sub (D32)
  display_name TEXT NULL,
  verified_at  TIMESTAMPTZ NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_identity_per_provider UNIQUE (provider, external_id),
  CONSTRAINT uq_user_provider         UNIQUE (user_id, provider)
);
CREATE TRIGGER tg_touch_user_identities BEFORE UPDATE ON user_identities
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();
-- One identity per provider per user (v1; widening uq_user_provider is a deliberate migration).
-- google is the one non-Telegram provider X.3 ships (FC-5; Apple re-entry = one CHECK value +
-- one flow increment, D34). external_id is the provider's IMMUTABLE SUBJECT — the OIDC sub for
-- google — never an email address (D32): emails are mutable and recyclable, so identity keyed
-- on email is an account-takeover primitive. The verified email claim is metadata refreshed at
-- sign-in. OIDC + session + linking mechanics in 07 §§1-2.

CREATE TABLE workspaces (
  id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name                    VARCHAR(100) NOT NULL,
  -- ownership has ONE home: the workspace_members row with role='owner' (below).
  -- No owner_user_id column — a second home bought only sync triggers and drift.
  state                   TEXT NOT NULL DEFAULT 'active'
                          CONSTRAINT ck_workspaces_state
                          CHECK (state IN ('active','suspended','offboarding')),
  -- product configuration (typed columns; NULL = app default from env, per the materialization
  -- contract at the end of this section). Most shapes carry from chat_settings; THREE ARE NEW
  -- (pass-4 anchor — no chat_settings counterpart exists): approval_mode,
  -- auto_reapprove_returning, approval_ttl_minutes — today auto-reapproval exists only as a
  -- posting_history.posting_method VALUE ('auto_reapproval'), behavior without a config column.
  -- Rename: legacy posting_timezone → tz.
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
  enable_ai_captions      BOOLEAN NOT NULL DEFAULT false,
  created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TRIGGER tg_touch_workspaces BEFORE UPDATE ON workspaces
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();
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
--   enable_instagram_api → routing flag row (C7 flag rows, 04 ground rules — it is cohort routing, not config)
--   show_verbose_notifications, send_lifecycle_notifications → channel_bindings.settings
--   media_sync_enabled → media_sources.state (false = 'paused'; pass-4 anchor — this column was
--     missing from the mapping)

CREATE TABLE workspace_members (
  workspace_id     UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  user_id          UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role             TEXT NOT NULL
                   CONSTRAINT ck_members_role CHECK (role IN ('owner','admin','member')),
  added_by_user_id UUID NULL REFERENCES users(id) ON DELETE SET NULL,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, user_id)
);
CREATE TRIGGER tg_touch_workspace_members BEFORE UPDATE ON workspace_members
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();
CREATE UNIQUE INDEX uq_members_one_owner ON workspace_members (workspace_id) WHERE role = 'owner';
-- AT MOST one owner per workspace, by index. AT LEAST one, by the deferred trigger PAIR below —
-- the member-side trigger catches demote/remove, the workspace-side trigger catches the creation
-- path (a workspace INSERT that commits with no owner member row — the pass-2 draft fired only on
-- member UPDATE/DELETE, so creation could commit ownerless):

CREATE FUNCTION trg_members_owner_exists() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM workspaces w WHERE w.id = OLD.workspace_id)
     AND NOT EXISTS (SELECT 1 FROM workspace_members m
                     WHERE m.workspace_id = OLD.workspace_id AND m.role = 'owner') THEN
    RAISE EXCEPTION 'workspace % has no owner at commit', OLD.workspace_id
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER ct_members_owner_exists
  AFTER UPDATE OR DELETE ON workspace_members
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
  WHEN (OLD.role = 'owner')                    -- only owner-row changes can break the invariant
  EXECUTE FUNCTION trg_members_owner_exists();

CREATE FUNCTION trg_workspaces_owner_at_insert() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM workspace_members m
                 WHERE m.workspace_id = NEW.id AND m.role = 'owner') THEN
    RAISE EXCEPTION 'workspace % created without an owner member row', NEW.id
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER ct_workspaces_owner_at_insert
  AFTER INSERT ON workspaces
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
  EXECUTE FUNCTION trg_workspaces_owner_at_insert();
-- Together: exactly one owner at every commit — uq_members_one_owner is "at most one", the
-- trigger pair is "at least one" (on every path: creation, demotion, removal). Ownership
-- transfer = demote old + promote new in one transaction (06 §2 is the only writer that
-- composes it); last-owner protection is structural — demoting/removing the owner without
-- promoting another fails at commit; creating a workspace without inserting its owner member
-- row in the same transaction fails at commit. Workspace deletion (offboarding cascade)
-- passes: the member-side trigger checks only workspaces that still exist at commit, and the
-- insert-side trigger fires on INSERT only.

CREATE TABLE workspace_invitations (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id    UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  token_hash      TEXT NOT NULL,                -- THE accept credential (FC-6/D33): SHA256 of the
                                                -- one-shot invite token, credential idiom as
                                                -- session_tokens — possession accepts; email
                                                -- never resolves an invitation
  delivery_channel TEXT NOT NULL
                  CONSTRAINT ck_invite_channel CHECK (delivery_channel IN ('email','telegram')),
  email           TEXT NULL,                    -- lowercased; B's delivery address AND the D33
                                                -- acceptance-constraint value; required when the
                                                -- delivery channel is email (CHECK below)
  invited_channel_ref TEXT NULL,                -- telegram-side hint recorded at invite time (tg
                                                -- user id / username). ADVISORY constraint data,
                                                -- never a key: acceptance never resolves an
                                                -- invitation by this column (D33)
  role            TEXT NOT NULL DEFAULT 'member'
                  CONSTRAINT ck_invite_role CHECK (role IN ('admin','member')),  -- never 'owner'
                  -- BOTH ruled (FC-6.4). role is the invitation's CEILING, not an unconditional
                  -- grant: member and admin invites are NOT the same object with a different
                  -- enum value — they carry different risk and therefore different accept rules
                  -- (D36). An admin invite grants 'admin' at accept ONLY on a matched identity
                  -- proof (accepted_email_matched = true); on the recorded-skip path it grants
                  -- 'member' + an elevation-pending notification, and admin arrives only through
                  -- the existing audited role-change gate (06 §2).
  invited_by_user_id UUID NULL REFERENCES users(id) ON DELETE SET NULL,
  state           TEXT NOT NULL DEFAULT 'pending'
                  CONSTRAINT ck_invite_state CHECK (state IN ('pending','accepted','revoked','expired')),
  accepted_by_user_id UUID NULL REFERENCES users(id) ON DELETE SET NULL,
  accepted_email_matched BOOLEAN NULL,          -- audit fact (D33): true = an identity proof ran
                                                -- and matched; false = constraint bypassed for
                                                -- lack of comparable proof (recorded skip);
                                                -- mismatch never lands — accept refuses
  expires_at      TIMESTAMPTZ NOT NULL,         -- now() + 7 days at insert (05 seam)
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_invite_token UNIQUE (token_hash),
  CONSTRAINT ck_invite_email_required CHECK (delivery_channel <> 'email' OR email IS NOT NULL)
);
CREATE TRIGGER tg_touch_workspace_invitations BEFORE UPDATE ON workspace_invitations
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();
CREATE UNIQUE INDEX uq_invite_live ON workspace_invitations (workspace_id, email)
  WHERE state = 'pending';
-- Membership door for both surfaces (06 §2 is the flow's normative home; FC-6 the ruling).
-- ACCEPT is one CAS with the member INSERT in the SAME transaction:
--   UPDATE workspace_invitations
--      SET state='accepted', accepted_by_user_id=:u, accepted_email_matched=:m
--    WHERE id=:id AND state='pending' AND expires_at > now() RETURNING id;
-- zero rows ⇒ used/revoked/expired (a re-read distinguishes "already yours" from "someone else
-- took it"); workspace_members' PK (workspace_id, user_id) is the double-membership guard —
-- already in the schema, nothing to add. THE MEMBER INSERT'S ROLE IS COMPUTED, NOT COPIED (D36):
--   role='member' invite ⇒ 'member' · role='admin' + :m = true ⇒ 'admin' ·
--   role='admin' + :m = false (recorded skip — token possession was the only proof) ⇒ 'member',
--   and the SAME transaction writes an elevation-pending outbox notification to the inviter's
--   surface (workspace binding / web), after which admin arrives only via the existing 06 §2
--   role-change gate. The D33 audit boolean is thereby also an authorization input — written and
--   read in one transaction, no TOCTOU. A forwarded or screenshotted admin link cannot silently
--   produce an admin: every path to 'admin' is a matched identity proof or an explicit
--   admin-performed role change. uq_invite_live survives the nullable email: NULLs never
-- collide, so telegram-delivery rows are exempt by construction, and re-invitation is INSERT new
-- + revoke prior in the same transaction — which the partial unique then ENFORCES rather than
-- breaks. Expired rows flip state via reap_expired (§5 remit), never delete: audit facts either
-- way. Telegram-side membership continues to arrive via group-membership sync on the binding
-- (current behavior, kept — the adapter upserts members).

CREATE TABLE channel_bindings (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  channel      TEXT NOT NULL
               CONSTRAINT ck_bindings_channel CHECK (channel IN ('telegram_group','telegram_dm')),
  external_ref TEXT NOT NULL,                   -- tg chat id as text
  state        TEXT NOT NULL DEFAULT 'active'
               CONSTRAINT ck_bindings_state CHECK (state IN ('active','revoked')),
               -- revoked = bot kicked/blocked in the chat, written by the adapter on the
               -- my_chat_member event. uq_binding_external holds across states, so re-adding the
               -- bot to the same chat flips this row back to active (upsert), preserving history.
               -- Muting notifications is settings, not state ('paused' was cut: it had no
               -- consumer — the outbox already skips non-active bindings).
  settings     JSONB NOT NULL DEFAULT '{"v":1}',
               -- {v:1, verbose_notifications?:bool, lifecycle_notifications?:bool}
               -- absent key = app default (materialization contract below)
               CONSTRAINT ck_bindings_settings_v CHECK (jsonb_typeof(settings->'v') = 'number'),
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_binding_external UNIQUE (channel, external_ref),
  CONSTRAINT uq_bindings_ws_id   UNIQUE (workspace_id, id)
);
CREATE TRIGGER tg_touch_channel_bindings BEFORE UPDATE ON channel_bindings
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();
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
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_ig_accounts_ws_id UNIQUE (workspace_id, id)
);
CREATE TRIGGER tg_touch_ig_accounts BEFORE UPDATE ON ig_accounts
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();
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
-- State transitions (complete): active → reauth_required (refresh failure / provider revocation
-- (07 §3) / definitive publish-time auth-rejection — the §2 credential liveness edge (D31), same
-- transaction as the credential flip) · reauth_required → active (reconnect swaps the credential
-- payload, 07 §2) · active ↔ disabled (user command, audited) ·
-- {active,reauth_required,disabled} → moved (06 §4, terminal). The dispatcher's due-scan
-- predicate reads state='active' only.

CREATE TABLE provider_quarantine (
  workspace_id      UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  provider          TEXT NOT NULL
                    CONSTRAINT ck_quarantine_provider
                    CHECK (provider IN ('ig','telegram','cloudinary','gdrive')),
  scope_ref         TEXT NOT NULL,              -- ALWAYS a §5 serialization key ('ig:…','tg:…',
                                                -- 'src:…','cred:…') — the exact work it defers;
                                                -- provider is observability metadata, matching
                                                -- never depends on it
  quarantined_until TIMESTAMPTZ NOT NULL,
  strike_count      INTEGER NOT NULL DEFAULT 1,
  last_strike_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  alerted_at        TIMESTAMPTZ NULL,           -- notification dedup: re-alert only if > 1h old (05)
  reason            TEXT NULL,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),  -- first entry (§0 stamps; the decay
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),  -- anchor is last_strike_at, above)
  PRIMARY KEY (workspace_id, scope_ref)
);
CREATE TRIGGER tg_touch_provider_quarantine BEFORE UPDATE ON provider_quarantine
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();
-- THE generic T2 fault-quarantine mechanism. Grain = the serialization key (pass-2 fix: the
-- earlier '' = whole-(workspace,provider) wildcard could not be matched against prefixed
-- serialization keys and silently made the fine grain a no-op; per-key rows are what adapters
-- can actually write — they always know the key of the job that faulted — and workspace-provider-
-- wide faults quarantine organically, key by key, as each fails). Semantics:
--   entry: the adapter upserts the row; quarantined_until = now() + backoff(strike_count) over
--          1m/5m/30m/2h/24h (05 seam); repeat entry within the decay window increments
--          strike_count, else resets to 1 (decay: last_strike_at older than 24h). The entry
--          transaction also pushes matching ready jobs out of the claim scan's way:
--          UPDATE jobs SET run_at = GREATEST(run_at, :until)
--            WHERE state='ready' AND serialization_key = :key;
--          (rare event, small ready set — keeps every subsequent claim from re-walking
--          quarantined work at the front of the run_at order).
--   effect: the §5 claim check defers late-inserted matching jobs. That is the ONLY effect.
--   exit:   passive — quarantined_until passes and the next claim proceeds; that claim IS the
--           probe (success rewrites nothing; the row is upserted again only on a fresh fault).
--   manual: clear-quarantine operator command deletes the row and re-readies deferred run_at
--           (audited).
--   NOT quarantine: credential revocation (state='revoked' → reauth flow, 07) and Meta cap
--   error 9 (a cap, §8) — neither writes here.

CREATE TABLE media_sources (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id         UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  provider             TEXT NOT NULL
                       CONSTRAINT ck_sources_provider CHECK (provider IN ('gdrive')),
  config               JSONB NOT NULL           -- {v:1, folder_ref:text, root_name?:text}
                       CONSTRAINT ck_sources_config_v CHECK (jsonb_typeof(config->'v') = 'number'),
  sync_checkpoint      JSONB NULL,              -- {v:1, page_token?:text, cursor?:text}
  next_sync_at         TIMESTAMPTZ NULL,
  state                TEXT NOT NULL DEFAULT 'active'
                       CONSTRAINT ck_sources_state CHECK (state IN ('active','paused','error')),
  last_sync_success_at TIMESTAMPTZ NULL,
  alerted_at           TIMESTAMPTZ NULL,        -- source-disconnect alert dedup (legacy gdrive_alerted_at)
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_sources_ws_id UNIQUE (workspace_id, id)
);
CREATE TRIGGER tg_touch_media_sources BEFORE UPDATE ON media_sources
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();
CREATE INDEX ix_sources_sync_due ON media_sources (next_sync_at)
  WHERE state = 'active' AND next_sync_at IS NOT NULL;
-- Printed BEFORE oauth_credentials because that table's composite FK targets it (replay order,
-- §0 — the pass-2 file order had these two swapped and could not replay as printed).
-- State transitions (complete): active ↔ paused (user command) · active → error (sync failure
-- classified persistent, e.g. folder gone/credential dead — alert via alerted_at dedup) ·
-- error → active (successful sync after reconnect/repair — the pre-slot or on-demand sync
-- IS the probe). The due-scan predicate reads state='active' only.

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
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_credentials_one_owner CHECK (num_nonnulls(ig_account_id, media_source_id) = 1),
  CONSTRAINT fk_credentials_account FOREIGN KEY (workspace_id, ig_account_id)
    REFERENCES ig_accounts (workspace_id, id) ON DELETE CASCADE,
  CONSTRAINT fk_credentials_source  FOREIGN KEY (workspace_id, media_source_id)
    REFERENCES media_sources (workspace_id, id) ON DELETE CASCADE
);
CREATE TRIGGER tg_touch_oauth_credentials BEFORE UPDATE ON oauth_credentials
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();
CREATE UNIQUE INDEX uq_credential_per_account ON oauth_credentials (workspace_id, ig_account_id, provider)
  WHERE ig_account_id IS NOT NULL;
CREATE UNIQUE INDEX uq_credential_per_source  ON oauth_credentials (workspace_id, media_source_id, provider)
  WHERE media_source_id IS NOT NULL;
CREATE INDEX ix_credentials_refresh_due ON oauth_credentials (next_refresh_at)
  WHERE state = 'active' AND next_refresh_at IS NOT NULL;
-- Typed XOR owner FKs (not polymorphic) so the composite-FK convention holds on the MOST
-- sensitive table (C4). The per-account unique keys by provider, which is what lets one account
-- hold an ig_login and a fb_login_legacy row simultaneously during G-phase (legacy 040 semantics).
-- State transitions (complete): active → expired (a definitive provider auth-rejection observed
-- on ANY Meta call — scheduled refresh, publish pipeline, or the §8 pre-check if enabled — or the
-- 07 §3 decrypt failure; the liveness-edge paragraph below states the discrimination) ·
-- active → revoked (user disconnect / offboarding / account movement — the move transaction
-- revokes the source row as it copies the payload to the target workspace, 06 §4: a move, not a
-- fork; exactly one active row per grant, so refresh can never diverge two copies) ·
-- {active,expired,revoked} → active (reconnect, 07 §2: an UPSERT on the owner unique key —
-- payload swapped and state flipped on the existing row, same row id, no delete, no
-- zero-credential window; the swap is audited).
-- fb_login_legacy is structurally closed to new rows at L.6:
--   ALTER TABLE oauth_credentials ADD CONSTRAINT ck_no_new_fb_legacy
--     CHECK (provider <> 'fb_login_legacy') NOT VALID;
-- (existing rows tolerated, new rows impossible — VALIDATEd then dropped with the enum value at
-- the FC-4 sunset G.2, together with uq/ix cleanup.)
-- THE CREDENTIAL LIVENESS EDGE (D31): 'expired' is a PROVIDER-VERDICT state, not a calendar
-- state — "the provider definitively rejected this credential", from whichever call observed it.
-- Definitive = an auth-class rejection (Meta error 190 without a revocation subcode; the
-- unparseable-token class per the corrupt-phrase discrimination already proven on main), never a
-- transient network/5xx/429/timeout fault — those stay in the R8 retry taxonomy and
-- provider_quarantine, which this edge does not touch. The observing worker performs BOTH flips
-- in the SAME transaction as the failure it is recording: this row → 'expired' and the owning
-- ig_accounts row → 'reauth_required' (§2 above). The dispatcher then mints no further intents
-- for the account, the reauth machinery prompts (06 §5), and reconnect (07 §2) restores
-- 'active'. Publish-time provider rejections are the abundant, free liveness signal: a
-- dead-but-calendar-valid token costs exactly ONE wasted attempt before dispatch stops, bounded
-- otherwise by the 05 refresh cadence — the scheduled refresh doubles as the liveness probe.
-- Tenant-plane writes only, by the worker that already holds these UPDATEs (it executes
-- refresh_credential): no new state, no new job kind, no new grants.

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
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_media_ws_id  UNIQUE (workspace_id, id),
  CONSTRAINT fk_media_source FOREIGN KEY (workspace_id, source_id)
    REFERENCES media_sources (workspace_id, id) ON DELETE CASCADE,
  CONSTRAINT uq_media_dedup  UNIQUE (workspace_id, content_hash)
);
CREATE TRIGGER tg_touch_media_items BEFORE UPDATE ON media_items
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();
CREATE INDEX ix_media_selection ON media_items (workspace_id, state, category);
CREATE INDEX ix_media_provider_ref ON media_items (workspace_id, source_id, provider_file_ref);
-- uq_media_dedup: dedup is per-workspace BY SCHEMA; a global hash namespace is inexpressible (R4).
-- Legacy file_hash is NOT unique today (duplicates exist in production) — the W.3 track carries a
-- human-gated dedup remediation (existing dedup-media tooling) with a zero-duplicates gate BEFORE
-- this constraint lands (04 W.3), same pattern as 0.3's history remediation.
-- Legacy columns not carried: cloud_* transit columns (transit state is per-attempt:
-- post_intents.transit_asset_ref), instagram_media_id/backfilled_at (posted evidence is per-intent:
-- §3 ig_media_id; legacy values ride the W.4 history backfill), is_active (→ state), file_path
-- (Drive path context folds into file_name; identity is provider_file_ref). No legacy
-- "unsupported" flag exists (pass-4 anchor): today Meta 9004 writes a permanent_reject LOCK, not
-- a media_items column — those rows ride the W.3 lock mapping; the target's 'unsupported' media
-- state and lock kind formalize the class going forward.
-- times_posted and last_posted_at are workspace-level ADVISORY aggregates (display/selection
-- hints; authority is the terminal intents) — per-account recency = post_locks.

CREATE TABLE post_locks (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id         UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  media_item_id        UUID NOT NULL,
  ig_account_id        UUID NULL,               -- NULL = workspace-wide; set = account-scoped
  kind                 TEXT NOT NULL
                       CONSTRAINT ck_locks_kind
                       CHECK (kind IN ('recent','skip','reject','unsupported','seasonal','hold')),
  expires_at           TIMESTAMPTZ NULL,        -- NULL = permanent
  created_by_intent_id UUID NULL,               -- plain UUID ref, §0-exemption: the intent may be
                                                -- terminal-frozen; attribution only, never joined for
                                                -- authorization
  created_by_user_id   UUID NULL REFERENCES users(id) ON DELETE SET NULL,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_locks_recent_scope CHECK ((kind = 'recent') = (ig_account_id IS NOT NULL)),
  CONSTRAINT fk_locks_media   FOREIGN KEY (workspace_id, media_item_id)
    REFERENCES media_items (workspace_id, id) ON DELETE CASCADE,
  CONSTRAINT fk_locks_account FOREIGN KEY (workspace_id, ig_account_id)
    REFERENCES ig_accounts (workspace_id, id) ON DELETE CASCADE
);
CREATE TRIGGER tg_touch_post_locks BEFORE UPDATE ON post_locks
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();
CREATE UNIQUE INDEX uq_lock_ws_scope ON post_locks (workspace_id, media_item_id, kind)
  WHERE ig_account_id IS NULL;
CREATE UNIQUE INDEX uq_lock_acct_scope ON post_locks (workspace_id, media_item_id, kind, ig_account_id)
  WHERE ig_account_id IS NOT NULL;
CREATE INDEX ix_locks_expiry ON post_locks (expires_at) WHERE expires_at IS NOT NULL;
-- Scope semantics per kind — 06 §3 is the normative home ('recent' = account-scoped, everything
-- human-judgment or file-driven = workspace-scoped, and the selection rule). ck_locks_recent_scope
-- is that rule's DB backstop (R4): a recent lock without an account, or any other kind carrying
-- one, is rejected as a constraint violation — prose/service discipline is no longer the only guard.
-- Expired rows are purged by the reap_expired sweep via ix_locks_expiry.
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
  published_via        TEXT NOT NULL DEFAULT 'api'
                       CONSTRAINT ck_intent_via CHECK (published_via IN ('api','manual','legacy_backfill')),
                       -- 'manual': the workspace posts by hand and confirms with the Posted tap —
                       -- the live phase-1 flow, carried forward (pass-2 addition: the first pass
                       -- designed only the API path; production has both). 'legacy_backfill':
                       -- W.4 history rows, exempt from evidence requirements below.
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
  attempts_by_step     JSONB NOT NULL DEFAULT '{"v":1}'
                       CONSTRAINT ck_intent_attempts_v
                       CHECK (jsonb_typeof(attempts_by_step->'v') = 'number'),
                       -- {v:1, <step>:{count:int, generation:int, last_error_class?:text}}
  last_error           JSONB NULL,              -- {v:1, class:text, provider_code?:text, message:text,
                       --  evidence?:object}  — reconciler evidence lands here (§6)
  legacy_queue_item_id UUID NULL,               -- L.9 correlation + W.6 card-mapping column;
                                                -- dropped only by W.6's mechanical condition
                                                -- (30 d after W.5 contract + zero live carriers)
  entered_state_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT fk_intent_account FOREIGN KEY (workspace_id, ig_account_id)
    REFERENCES ig_accounts (workspace_id, id),          -- NO CASCADE: intents outlive account rows
                                                        -- only via workspace offboarding; account
                                                        -- deletion is forbidden while intents exist
                                                        -- (accounts terminalize to 'moved'/'disabled')
  CONSTRAINT fk_intent_media FOREIGN KEY (workspace_id, media_item_id)
    REFERENCES media_items (workspace_id, id),          -- NO CASCADE: same reason — media rows go
                                                        -- state='removed', never DELETE, while
                                                        -- referenced; offboarding deletes workspace-first
  -- state-completeness CHECKs: the terminal row IS the complete outcome (R3), scoped by path —
  -- API posts prove themselves with provider evidence; manual posts prove a human confirmed
  -- (cap still debited); legacy backfill rows are exempt (their evidence is the migrated history):
  CONSTRAINT ck_posted_complete CHECK (
    state <> 'posted'
    OR published_via = 'legacy_backfill'
    OR (published_via = 'manual' AND cap_consumed_on IS NOT NULL)
    OR (published_via = 'api' AND ig_container_id IS NOT NULL
        AND publish_step = 'effect_confirmed' AND cap_consumed_on IS NOT NULL)),
  CONSTRAINT ck_publishing_debited CHECK (
    state NOT IN ('publishing','publishing_ambiguous') OR cap_consumed_on IS NOT NULL),
  CONSTRAINT ck_ambiguous_called CHECK (
    state <> 'publishing_ambiguous' OR publish_step = 'publish_called'),
    -- ambiguity exists ONLY for the publish effect: committing the publish permit advances
    -- publish_step to 'publish_called' in the same transaction (§6), so an ambiguous intent
    -- always carries exactly that step. Container-create loss never escalates to intent
    -- ambiguity (§6: orphan containers are inert — confirmed-safe regeneration instead).
  CONSTRAINT ck_refund_after_debit CHECK (cap_refunded_at IS NULL OR cap_consumed_on IS NOT NULL)
);
CREATE TRIGGER tg_touch_post_intents BEFORE UPDATE ON post_intents
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();
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
  detail        JSONB NULL,                     -- {v:1, ...} — never secrets (07 §hygiene)
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
  -- created_at ONLY — no updated_at, no touch trigger (§0 exception: append-only)
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
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, ig_account_id, local_date),
  CONSTRAINT fk_dpc_account FOREIGN KEY (workspace_id, ig_account_id)
    REFERENCES ig_accounts (workspace_id, id) ON DELETE CASCADE
);
CREATE TRIGGER tg_touch_daily_post_counts BEFORE UPDATE ON daily_post_counts
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();
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
  WHERE state IN ('publishing','publishing_ambiguous');
-- The one deliberately non-workspace-leading key; its existence-oracle leak is accepted (§7).
-- The predicate covers publishing_ambiguous too (pass-3 widening): an unresolved publish blocks
-- that real account's NEXT publish until the reconciler terminalizes it — correct product
-- behavior, since the ambiguous attempt may have consumed the story slot. review_required
-- DELIBERATELY releases the key: the operator is already paged (05 parked-intent alarm), the
-- account should not be frozen for the human's response time, and the residual risk — a second
-- publish while an unresolved one later proves posted — is the same operator-resolution-error
-- window R1 already names. Recorded, not accidental.
```

Key 1 carries no intent-kind discriminator: exactly one kind exists; the closed-set convention makes a future kind a deliberate migration that widens the key (`03` C8).

## §4. Transitions: the matrix and its database enforcement (R3)

The matrix is unchanged in substance from the first pass; what is new is that **every rule below names the database object that enforces it**. Application code (one transition service) orchestrates; the database is the authority — a raw UPDATE from any client obeys the same rules.

```sql
CREATE TABLE post_intent_transitions (        -- the legal-edge reference table (seed data = matrix below)
  from_state TEXT NOT NULL,
  to_state   TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),  -- §0 insert-only class: edges are added or
  PRIMARY KEY (from_state, to_state)              -- deleted, never updated — no touch machinery
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
  FOR EACH ROW
  WHEN (OLD.state IS DISTINCT FROM NEW.state)   -- checkpoint updates skip the trigger entirely
  EXECUTE FUNCTION trg_intent_audit();

CREATE FUNCTION trg_intent_insert_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  -- runtime creates intents only in 'scheduled'; terminal-state inserts are the migration
  -- backfill's privilege (W.4), recognized by the actor GUC it already must set — no second
  -- mechanism: history cannot be fabricated at runtime.
  IF NEW.state <> 'scheduled'
     AND COALESCE(current_setting('app.actor_kind', true), '') <> 'migration' THEN
    RAISE EXCEPTION 'post_intents are born scheduled (got %)', NEW.state;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER tg_intent_insert_guard BEFORE INSERT ON post_intents
  FOR EACH ROW EXECUTE FUNCTION trg_intent_insert_guard();

-- GOVERNANCE AUDIT (pass 3): one generic required-actor audit trigger applied to the five
-- governance tables, making 06's "every membership/credential/account mutation is audited"
-- DB-true rather than writer-path prose. §0 names the covered set and the deliberate
-- machinery-table exclusions. Installed at L.1 with audit_events itself (04); from that
-- increment on, every governance writer — including the F.3 dual-write mirror services —
-- sets the actor GUCs.
CREATE FUNCTION trg_governance_audit() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  r    RECORD;
  kind TEXT;
  ws   UUID;
  ent  UUID;
  fs   TEXT;
  ts   TEXT;
BEGIN
  IF current_setting('app.actor_kind', true) IS NULL THEN
    RAISE EXCEPTION 'governance mutation on % without app.actor_kind — anonymous writes are forbidden',
      TG_TABLE_NAME;
  END IF;
  -- Machinery-column early-exit (§0's exclusion applied at COLUMN grain): two governance tables
  -- are dual-role — the clock/worker advance their scheduling columns at publish frequency.
  -- Those advances still require an actor (the RAISE above) but write no audit row: their
  -- authority trail is the intent ledger, and auditing them would mint from=to noise at
  -- publish rate, retained 400 d. Any change to a governance column below still audits.
  IF TG_OP = 'UPDATE' AND TG_TABLE_NAME = 'ig_accounts'
     AND ROW(NEW.workspace_id, NEW.provider_account_ref, NEW.handle, NEW.display_name, NEW.state,
             NEW.posts_per_day, NEW.posting_hours_start, NEW.posting_hours_end, NEW.tz)
         IS NOT DISTINCT FROM
         ROW(OLD.workspace_id, OLD.provider_account_ref, OLD.handle, OLD.display_name, OLD.state,
             OLD.posts_per_day, OLD.posting_hours_start, OLD.posting_hours_end, OLD.tz) THEN
    RETURN NULL;                                 -- next_slot_at / last_posted_at advance only
  END IF;
  IF TG_OP = 'UPDATE' AND TG_TABLE_NAME = 'oauth_credentials'
     AND ROW(NEW.workspace_id, NEW.ig_account_id, NEW.media_source_id, NEW.provider,
             NEW.encrypted_payload, NEW.state)
         IS NOT DISTINCT FROM
         ROW(OLD.workspace_id, OLD.ig_account_id, OLD.media_source_id, OLD.provider,
             OLD.encrypted_payload, OLD.state) THEN
    RETURN NULL;                                 -- next_refresh_at / expires_at advance only
  END IF;
  IF TG_OP = 'DELETE' THEN r := OLD; ELSE r := NEW; END IF;
  kind := CASE TG_TABLE_NAME
            WHEN 'workspaces'        THEN 'workspace'
            WHEN 'workspace_members' THEN 'member'
            WHEN 'oauth_credentials' THEN 'credential'
            WHEN 'ig_accounts'       THEN 'ig_account'
            WHEN 'channel_bindings'  THEN 'channel_binding'
          END;
  IF TG_TABLE_NAME = 'workspaces' THEN
    ws := r.id;           ent := r.id;
  ELSIF TG_TABLE_NAME = 'workspace_members' THEN
    ws := r.workspace_id; ent := r.user_id;
  ELSE
    ws := r.workspace_id; ent := r.id;
  END IF;
  IF TG_OP = 'UPDATE' THEN
    IF TG_TABLE_NAME = 'workspace_members' THEN fs := OLD.role;  ts := NEW.role;
    ELSE                                        fs := OLD.state; ts := NEW.state;
    END IF;
  ELSIF TG_OP = 'INSERT' THEN
    IF TG_TABLE_NAME = 'workspace_members' THEN ts := NEW.role;  ELSE ts := NEW.state; END IF;
  ELSE
    IF TG_TABLE_NAME = 'workspace_members' THEN fs := OLD.role;  ELSE fs := OLD.state; END IF;
  END IF;
  INSERT INTO audit_events (workspace_id, entity_kind, entity_id, from_state, to_state,
                            actor_kind, actor_user_id, channel, detail)
  VALUES (ws, kind, ent, fs, ts,
          current_setting('app.actor_kind'),
          NULLIF(current_setting('app.actor_user_id', true), '')::uuid,
          NULLIF(current_setting('app.channel', true), ''),
          jsonb_build_object('v', 1, 'op', TG_OP));
  RETURN NULL;
END $$;
CREATE TRIGGER tg_audit_workspaces        AFTER INSERT OR UPDATE OR DELETE ON workspaces
  FOR EACH ROW EXECUTE FUNCTION trg_governance_audit();
CREATE TRIGGER tg_audit_workspace_members AFTER INSERT OR UPDATE OR DELETE ON workspace_members
  FOR EACH ROW EXECUTE FUNCTION trg_governance_audit();
CREATE TRIGGER tg_audit_oauth_credentials AFTER INSERT OR UPDATE OR DELETE ON oauth_credentials
  FOR EACH ROW EXECUTE FUNCTION trg_governance_audit();
CREATE TRIGGER tg_audit_ig_accounts       AFTER INSERT OR UPDATE OR DELETE ON ig_accounts
  FOR EACH ROW EXECUTE FUNCTION trg_governance_audit();
CREATE TRIGGER tg_audit_channel_bindings  AFTER INSERT OR UPDATE OR DELETE ON channel_bindings
  FOR EACH ROW EXECUTE FUNCTION trg_governance_audit();
-- UPDATE audits record the state/role edge (other GOVERNANCE-column edits still audit, with
-- from = to — presence matters more than diff granularity; detail carries the operation;
-- machinery-column-only advances are the one early-exit, above). A credential reconnect's
-- payload swap audits via the encrypted_payload compare — but encrypted_payload itself never
-- appears in audit rows (07 §hygiene): the trigger writes states and ids only.
```

Matrix actors map onto the `ck_audit_actor` GUC vocabulary as: `clock`/`reaper`/`reconciler`/`operator` → themselves; **`worker` → `'system'`**; `user` → `'user'`; backfill inserts → `'migration'`. This line is the mapping's single statement.

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
| prompt_pending | cancelled | worker | `cancel_requested`, or workspace/account disabled (pass-2: this edge was missing — offboarding must be able to terminalize every working state) |
| awaiting_approval | approved | user or system(auto) | manual command, or `auto_reapprove_returning` policy |
| awaiting_approval | **posted** | user | **the manual-mode path** (pass-2: live phase-1 flow carried forward — the human posted by hand and taps Posted): same tx sets `published_via='manual'`, debits the cap (`cap_consumed_on`), `times_posted`++, account recent lock, `ig_accounts.last_posted_at` |
| awaiting_approval | skipped / rejected | user | terminal; `rejected` upserts a workspace-scoped reject lock |
| awaiting_approval | expired | reaper | approval window passed (`approval_ttl_minutes`); prompts superseded via outbox |
| awaiting_approval | cancelled | worker | `cancel_requested` |
| approved | publishing | worker | **one transaction** (SQL below): key-4 acquisition + atomic cap debit + lease held |
| approved | expired | reaper | approval stale beyond policy |
| approved | cancelled | worker | `cancel_requested` (pre-publish it is always honorable) |
| publishing | posted | worker | `effect_confirmed`; same tx: `times_posted`++, recent lock upsert (account-scoped), `ig_accounts.last_posted_at`; after commit: best-effort inline FC-3.5 transit destroy (no job — the FC-3.6 sweep is the guarantee; a per-asset reap job would double jobs-table volume for coverage the sweep already owns) |
| publishing | publishing_ambiguous | worker | timeout/crash after `publish_called` with unconfirmed effect (R8) |
| publishing | failed | worker | terminal provider error; same tx: cap refund (below) |
| publishing | review_required | worker | poison: attempts exhausted on a retryable class (G5) |
| publishing_ambiguous | posted / failed | **reconciler only** | §6 evidence contract decides; `failed` refunds the cap |
| publishing_ambiguous | review_required | reconciler | evidence budget exhausted — a human looks |
| review_required | posted | user (operator) | **resolve-posted** (pass-2: the missing likeliest resolution — "it did publish"): legal only when a publish permit exists (`publish_step='publish_called'`, container present); the resolution UPDATE sets `publish_step='effect_confirmed'` + any evidence (`ig_media_id`) in the same statement, satisfying `ck_posted_complete`; cap debit stands |
| review_required | approved / failed / cancelled | user (operator) | resolve-retry (new generation) / resolve-failed (refunds the cap) / cancel; audited with channel + actor |

The **approved → publishing** transaction, verbatim shape (the R2 atomic debit — no check-then-act gap, and the flip's row count is asserted so a consumed cap slot can never leak):

```sql
BEGIN;
SET LOCAL app.tenant_id = :workspace_id;  SET LOCAL app.actor_kind = 'system';
WITH debit AS (
  -- cap debit: insert-or-increment, denied atomically at the cap
  INSERT INTO daily_post_counts AS d (workspace_id, ig_account_id, local_date, count, cap_at_write)
  VALUES (:ws, :acct, :local_date, 1, :effective_cap)
  ON CONFLICT (workspace_id, ig_account_id, local_date)
    DO UPDATE SET count = d.count + 1 WHERE d.count < d.cap_at_write
  RETURNING local_date
),
flip AS (
  -- state flip, COUPLED to the debit in one statement: no debit row ⇒ no flip attempt.
  -- The §4 trigger validates the edge; key 4 acquires publishing exclusivity.
  UPDATE post_intents
     SET state = 'publishing', cap_consumed_on = (SELECT local_date FROM debit)
   WHERE id = :intent AND state = 'approved' AND EXISTS (SELECT 1 FROM debit)
  RETURNING id
)
SELECT (SELECT count(*) FROM debit) AS debited,
       (SELECT count(*) FROM flip)  AS flipped;
-- (0, 0) ⇒ cap DENIED — the statement wrote nothing; COMMIT is a no-op. Defer: the worker
--   reschedules the job to the account's next slot (jobs.run_at); the intent stays 'approved';
--   audit row 'cap_deferred'. Approval does not expire from deferral alone — the reaper's
--   approval-TTL clock keeps running regardless.
-- (1, 1) ⇒ proceed: COMMIT.
-- (1, 0) ⇒ the intent was not 'approved' (a race terminalized or moved it). The service RAISES
--   and the transaction ROLLS BACK — the debit rolls back with it. Pass-3 fix: the pass-2
--   two-statement form could commit a debit around a zero-row flip, consuming capacity without
--   entering publishing; the CTE coupling plus the asserted row count closes that leak (R2).
-- unique_violation on uq_publish_exclusive ⇒ the real account already has a publishing or
--   publishing_ambiguous intent in some workspace: treated exactly as a cap denial (transaction
--   rolls back — debit included — defer, no error surfaced to the user).
COMMIT;  -- or ROLLBACK per the outcome above
```

Cap refund (the `publishing → failed` and ambiguous→failed companion, same tx as the terminal flip): `UPDATE daily_post_counts SET count = count - 1 WHERE (workspace_id, ig_account_id, local_date) = (:ws, :acct, intent.cap_consumed_on) AND count > 0;` plus `cap_refunded_at = now()` on the intent. The refund targets **the recorded debit day** (`cap_consumed_on`), so a timezone change or midnight crossing between debit and refund cannot touch the wrong bucket. `local_date` is computed in the account's effective tz *at debit time*; the product-facing DST/tz-change and mid-day-cadence-change rules live in 06 §3 — the one home.

`cancel_requested` during `publishing`/`publishing_ambiguous` is **not** honored by force (C9: `publishing → cancelled` is forbidden — and now DB-forbidden: the edge is absent from `post_intent_transitions`); the pipeline completes or reconciles, then the reconciler consults the flag only where a real choice remains. A late interaction with any terminal intent renders the terminal state and never acts (R6).

## §5. `jobs` — execution machinery (pg-only per C3)

```sql
CREATE TABLE jobs (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  kind              TEXT NOT NULL CONSTRAINT ck_jobs_kind CHECK (kind IN (
                      -- tenant kinds (workspace_id NOT NULL):
                      'publish_pipeline','deliver_outbox','sync_media_source','first_ingest_chunk',
                      'refresh_credential','offboard_workspace','revoke_workspace_credentials',
                      'reauth_prompt',
                      -- system kinds (workspace_id NULL):
                      'reconcile_ambiguous','reap_expired','reap_transit_assets','retention_sweep',
                      'comparator_run','reencrypt_credentials','send_email')),
  workspace_id      UUID NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  lane              TEXT NOT NULL CONSTRAINT ck_jobs_lane CHECK (lane IN ('interactive','bulk')),
  serialization_key TEXT NOT NULL,              -- every registry kind names its key (pass-3:
                                                -- nullability was vestigial, and a NULL key
                                                -- bypassed both serialization guards)
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
  payload           JSONB NOT NULL DEFAULT '{"v":1}'
                    CONSTRAINT ck_jobs_payload_v CHECK (jsonb_typeof(payload->'v') = 'number'),
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_jobs_system_kinds CHECK (
    (workspace_id IS NULL) = (kind IN
      ('reconcile_ambiguous','reap_expired','reap_transit_assets','retention_sweep',
       'comparator_run','reencrypt_credentials','send_email')))
);
CREATE TRIGGER tg_touch_jobs BEFORE UPDATE ON jobs
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();
CREATE INDEX ix_jobs_claim ON jobs (lane, run_at) WHERE state = 'ready';
CREATE INDEX ix_jobs_lease_expiry ON jobs (locked_until) WHERE state = 'leased';
CREATE UNIQUE INDEX uq_jobs_serialized_lease ON jobs (serialization_key) WHERE state = 'leased';
CREATE INDEX ix_jobs_retire ON jobs (updated_at)
  WHERE state IN ('succeeded','cancelled','failed','review_required');
-- ix_jobs_retire gives the retention sweep an age-ordered walk over exactly the swept rows
-- (updated_at = terminalization time — §0 stamps); without it every sweep batch re-scans the
-- millions of terminal rows that accrue at envelope. Same pattern on channel_outbox and
-- provider_operations below.
-- uq_jobs_serialized_lease is THE serialization guard: two leased jobs with one key are
-- impossible by constraint, not by claim-query discipline. The claim query merely avoids
-- most conflicts; the index makes the race lose correctly. ck_jobs_system_kinds is an
-- EQUIVALENCE (pass 3): a system kind with a workspace, or a tenant kind without one, is a
-- constraint violation — the one-way form let malformed producer rows through in both
-- directions the registry never intended.
```

**Job-kind registry (closed; the CHECK above is its enforcement).** Per kind: payload schema (all `{v:1, …}`), producer, lane, serialization key:

| kind | payload | producer | lane | serialization_key |
|---|---|---|---|---|
| publish_pipeline | `{intent_id}` | approval flip / clock (auto mode) | bulk | `ig:<provider_account_ref>` |
| deliver_outbox | `{binding_id}` | outbox insert / sender reschedule | interactive | `tg:<binding_id>` (per-chat send ordering) |
| sync_media_source | `{source_id, reason:'pre_slot'\|'demand'\|'baseline'}` | clock / command | bulk | `src:<source_id>` |
| first_ingest_chunk | `{source_id, page_token}` | sync job (chains) | bulk | `src:<source_id>` |
| refresh_credential | `{credential_id}` | clock (next_refresh_at due) | bulk | `cred:<credential_id>` |
| offboard_workspace | `{}` (workspace-keyed) | offboarding command (06 §1 — orchestrates the workflow legs: enqueues revocation, terminalizes intents, drains, reaps transit, schedules final deletion) | bulk | `ws:<workspace_id>` |
| revoke_workspace_credentials | `{}` (workspace-keyed) | offboard_workspace | bulk | `ws:<workspace_id>` |
| reauth_prompt | `{ig_account_id}` | G.1 campaign clock | bulk | `ig:<provider_account_ref>` |
| reconcile_ambiguous | `{}` | clock (recurring) | bulk | `'reconciler'` (singleton by key) |
| reap_expired | `{}` | clock (recurring) | bulk | `'reaper'` — remit: every expiry class in one bounded sweep — intent expiries (§4 reaper edges), expired `post_locks` (via `ix_locks_expiry`), `workspace_invitations`, `oauth_states` — **staged by table availability** (pass 3): each class lands in the increment that creates its table, which extends the executor in the same PR, so the reaper can never name a table that does not exist (`04` names the increments). Auth-plane classes sweep through `fn_auth_plane_sweep` (§7 door) |
| reap_transit_assets | `{}` | clock (recurring, FC-3.6) | bulk | `'transit-reaper'` |
| retention_sweep | `{}` | clock (recurring) | bulk | `'retention'` |
| comparator_run | `{track:text}` | clock (nightly, per active track) | bulk | `cmp:<track>` |
| reencrypt_credentials | `{key_generation:int}` | rotation runbook (07) | bulk | `'reencrypt'` |
| send_email | `{v:1, to:text, template:text, params:object, ref:uuid}` — everything the send needs; no tenant reads at send time | invitation create / bounce handler (07 §1, FC-6) | interactive (the inviter is mid-flow awaiting send confirmation) | `email:<ref>` (one key per send — retry ordering only, no cross-send serialization) |

Producer authorization is code-level (each producer is one named service); the DB-level guard is the kind CHECK + the nullability pairing + RLS (§7). A new kind is a migration (CHECK edit) plus a registry row here — the closed-set convention applied to work itself. **Kind classing rule (why `send_email` is a system kind even though a tenant flow enqueues it):** a kind is system iff its executor is payload-complete — zero tenant reads or writes at execution time — regardless of who produced it; the send executor reads nothing but its payload, and the equivalence CHECK must hold for every row of a kind. Acknowledged residue: the tenant-originated send escapes the workspace cascade, so an invitation email job can outlive its workspace — the executor tolerates a dangling `ref` (send fires or fails on its own retry budget; nothing joins tenant tables). That is the payload-complete, workspace-outliving case this classing rule exists to name. **Interactive commands are not jobs**: approve/skip/reject/settings/etc. are single-transaction state flips executed inline in ingress (the ack IS the transaction, R5); a command reaches this table only when it spawns real work, and then as its specific kind above (sync-now → `sync_media_source`, offboard → `offboard_workspace`, …) — there is deliberately no generic `run_command` kind.

**The claim, verbatim shape (its race guard is the unique index, not the query):**

```sql
BEGIN;  -- the body of fn_claim_job(:lane) — SECURITY DEFINER, owner svc_claim (§7), EXECUTE
        -- granted to svc_worker; its own short transaction, THEN execution runs tenant-scoped
WITH candidate AS (
  SELECT j.id FROM jobs j
  WHERE j.state = 'ready' AND j.lane = :lane AND j.run_at <= now()
    AND NOT EXISTS (SELECT 1 FROM jobs h                     -- serialization pre-filter
                    WHERE h.serialization_key = j.serialization_key AND h.state = 'leased')
    AND NOT EXISTS (SELECT 1 FROM provider_quarantine q      -- quarantine deferral (T2):
                    WHERE q.workspace_id = j.workspace_id    --   exact per-key probe (PK) —
                      AND q.scope_ref = j.serialization_key  --   backstop for jobs inserted
                      AND q.quarantined_until > now())       --   after the entry-time run_at push
    AND (j.workspace_id IS NULL OR                           -- per-workspace lane cap (05 #3):
         (SELECT count(*) FROM jobs w                        --   leased set is small (≤ global
          WHERE w.state = 'leased' AND w.lane = j.lane       --   concurrency), so the correlated
            AND w.workspace_id = j.workspace_id) < :ws_lane_cap)  -- count is cheap
  ORDER BY j.run_at
  LIMIT 1 FOR UPDATE OF j SKIP LOCKED
)
UPDATE jobs SET state='leased', locked_by=:worker, lease_token=gen_random_uuid(),
                locked_until = now() + :lease_interval, attempts = attempts + 1
  FROM candidate WHERE jobs.id = candidate.id
RETURNING jobs.*;
COMMIT;
-- unique_violation on uq_jobs_serialized_lease (two claimers picked different rows sharing a key):
-- retry the claim excluding that serialization_key. The quarantine and per-workspace checks are
-- advisory backoff/fairness, not correctness gates — races defer the NEXT claim, never corrupt.
```

Transitions: `ready → leased` (claim above) · `leased → succeeded | failed | review_required` (finalization CAS: `UPDATE jobs SET state=:terminal WHERE id=:id AND lease_token=:token` — zero rows = fenced, the worker aborts) · `leased → ready` (lease expiry: the expiry sweep re-readies; the stale owner's later finalization fails the CAS) · `ready → review_required` (poison) · `ready → cancelled` (cooperative; a leased job is cancelled only by its own worker at a checkpoint). The domain transaction (intent flip + counters + audit) and job finalization commit **together** — that co-location is why jobs live in Postgres (C3). Job state is execution bookkeeping only; it is never the authority on whether an external effect happened (§6). Heartbeat: one independent asyncio task per worker process extends `locked_until` for all its live leases every interval (`05`) in a single `fn_extend_leases` call (§7 door) guarded by lease tokens; a worker missing two beats is presumed dead well inside the lease; provider waits never block beats (the beat task is not in the pipeline's await chain).

**Transaction discipline (normative, the L.0 gate tests it): a database transaction never spans a provider call.** Pipelines run transaction-per-checkpoint — open, write the checkpoint/permit, commit, *then* talk to the provider. This is both the correctness seam (the §6 permit protocol depends on permit-commit-before-call) and the connection-budget arithmetic's premise (`05` tasks-vs-connections).

## §6. Outbound effects: `channel_outbox`, `provider_operations`, and the permit protocol

```sql
CREATE TABLE channel_outbox (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id         UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  binding_id           UUID NOT NULL,
  kind                 TEXT NOT NULL CONSTRAINT ck_outbox_kind
                       CHECK (kind IN ('approval_prompt','prompt_supersede','notification','ack',
                                       'invitation')),
  intent_id            UUID NULL,               -- plain UUID ref (intent may be terminal-frozen)
  payload              JSONB NOT NULL           -- {v:1, ...} channel-NEUTRAL content
                       CONSTRAINT ck_outbox_payload_v CHECK (jsonb_typeof(payload->'v') = 'number'),
  state                TEXT NOT NULL DEFAULT 'pending' CONSTRAINT ck_outbox_state
                       CHECK (state IN ('pending','sending','sent','ambiguous','failed','superseded')),
  attempts             INTEGER NOT NULL DEFAULT 0,
  external_message_ref TEXT NULL,               -- tg message id after send
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT fk_outbox_binding FOREIGN KEY (workspace_id, binding_id)
    REFERENCES channel_bindings (workspace_id, id) ON DELETE CASCADE
);
CREATE TRIGGER tg_touch_channel_outbox BEFORE UPDATE ON channel_outbox
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();
CREATE INDEX ix_outbox_due ON channel_outbox (binding_id, created_at) WHERE state = 'pending';
CREATE INDEX ix_outbox_retire ON channel_outbox (updated_at)
  WHERE state IN ('sent','superseded','failed','ambiguous');   -- retention walk (§5 pattern)

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
  response_ref        JSONB NULL,               -- {v:1, container_id?|media_id?|status_code?|error?}
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_ops_business_key UNIQUE (business_key)
);
CREATE TRIGGER tg_touch_provider_operations BEFORE UPDATE ON provider_operations
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();
CREATE INDEX ix_ops_retire ON provider_operations (updated_at)
  WHERE state IN ('succeeded','failed');        -- retention walk; ambiguous rows are excluded
                                                -- until the reconciler terminalizes them

-- The reconciler and the parked-intent alarm drive off post_intents, not this table:
CREATE INDEX ix_intents_parked ON post_intents (entered_state_at)
  WHERE state IN ('publishing_ambiguous','review_required');
-- (one partial index serves the 60 s reconciler sweep, the 15 min alarm, and the 06 §5 operator
-- list — without it those recurring scans would ride the live-subject index and filter the whole
-- live set forever.)
```

**Scope: the rail covers `ig` only.** Cloudinary is deliberately OFF the rail (first-pass design carried it; cut on review): upload and destroy are recoverable effects — a duplicate transit upload is a second short-lived asset, a missed destroy is caught by the FC-3.6 hard-TTL sweep. Their execution state lives in job attempts + `audit_events`, weightless. Instagram publish is irreversible and customer-visible; it gets the full machinery. Telegram delivery lives in the outbox (below). Drive is read-only.

**The permit protocol (stale-worker fencing at the permit boundary).** What the database enforces, stated exactly (pass-3 rewording — the pass-2 claim "the DB stops the call *before* it happens" overstated the mechanism): the database **denies permits to fenced workers** (the lease CAS below) and **denies second permits per generation** (`uq_ops_business_key`). It does not literally stop an already-permitted call — a permit-holder whose lease expires or is reassigned *after* the permit commits can still place its one authorized call. At-most-once survives because that call was authorized exactly once and an unresolved publish permit is never re-called (the successor rule), not because a stale call is physically prevented.

1. Inside the pipeline, immediately before any IG side-effecting call, the worker opens a short transaction and inserts the permit: `INSERT INTO provider_operations (…, business_key, lease_token) VALUES (…)` **in the same transaction as** a lease CAS re-check: `SELECT 1 FROM jobs WHERE id=:job AND lease_token=:token AND state='leased' AND locked_until > now() FOR SHARE` — zero rows ⇒ the lease moved on, expired, or was finalized (pass 3: state and expiry joined the check; the pass-2 id+token form passed a lease that had expired without reassignment); the insert rolls back; the worker aborts with **no network call made**. For the `publish` op, the same transaction advances `publish_step` to `'publish_called'` — the permit IS the durable record that a publish may have been attempted (this is what makes `ck_ambiguous_called` exact).
2. Only after that transaction commits may the worker issue the provider call. Crash before commit: no permit, no call, clean resume. Crash after commit with no recorded outcome — **the two op kinds diverge, by blast radius**:
   - `container_create`: an orphaned container is inert (nothing published, containers expire unused, Meta caps count the publish step only). The resumed pipeline marks the unresolved op `failed` (`response_ref` notes `lost_response`), bumps `generation`, permits a fresh create. **Never intent-level ambiguity** — recoverable effects do not park the pipeline.
   - `publish`: the resumed pipeline **does not re-call**: it marks the op `ambiguous` and the intent `publishing_ambiguous`; the reconciler owns it from there, and `generation` never bumps out of ambiguity.
3. Completion: the worker records the outcome (`succeeded` + `response_ref`, or `failed` + error class) and advances `publish_step` in the same domain transaction (which also re-CASes the lease token — a fenced worker cannot record outcomes either).

**Business-key formats (closed):** `ig:container:<intent_id>:<generation>` · `ig:publish:<intent_id>:<generation>`. `generation` increments **only** on a confirmed-safe failure of the same op kind (provider said the previous attempt definitively did not happen); it never increments out of ambiguity. One business key = at most one intended provider effect (TT:P0-06's gate), and `uq_ops_business_key` is the object that enforces it.

**Send-state authority is single-homed:** the outbox row IS the delivery record and the only authority on "did this send" — the sender *job* carries execution state only. The `ambiguous` outbox state carries R8's no-blind-retry rule, resolved per kind, because Telegram provides no general read-back for a lost `sendMessage` response (there is no "list my sent messages" API): **`notification`/`ack`** — retry once after backoff, then `failed`; a duplicate notification is the accepted cost, bounded at one. **`approval_prompt`** — resend; two live cards for one intent are tolerable because both resolve to the same intent and terminal-state-first reads (R6) make whichever is tapped later render the terminal state; on any intent state change, supersede-all (`prompt_supersede` rows target every known `external_message_ref`; a card whose ref was lost simply ages out under R6 semantics). **`invitation`** (FC-6 Telegram delivery) — approval-prompt semantics: resend on ambiguity, tolerate a duplicate card, and on ANY terminal transition of the invitation (accepted / revoked / expired) **supersede-all** — `prompt_supersede` rows target every known card ref. The card is addressed to the workspace's `telegram_group` binding, carries the accept button (callback → ingress inline transaction, R5) and the `t.me/<bot>?start=inv-<token>` deep link (`07` §2's start-token door), and is rendered and delivered by the existing sender under the same `tg:<binding_id>` serialization — zero new senders, zero parallel delivery paths. **Edits always go supersede-then-send** — never edit-in-place on an ambiguous ref. This is the complete ambiguity policy; there is no "stamp-heal" beyond the ref capture on a late Telegram response.

**The reconciliation contract (ambiguous IG publish — evidence, R8).** Pass 3 stops assuming the platform fact 0.4 exists to verify: whether an Instagram container exposes an observable **post-publish** terminal status at all. Meta's Instagram collection documents `FINISHED` as "ready to be published", and `PUBLISHED` is documented for **Threads** containers — so the pass-2 contract may have borrowed its authoritative evidence from the wrong API (R3 review §6.11, marked uncertain, not false). **One contract, parameterized by evidence authority:** the config seam `reconciler_evidence_mode` records 0.4's doc-cited verdict as the sets of container `status_code` values that are **authoritative after `publish_called`** — which value (if any) is authoritative-positive (the effect happened) and which (if any) are authoritative-negative (it definitively did not). Setting the seam is a config change, never a design change. The machinery is mode-independent:

- **Poll ladder:** per-intent exponential backoff (rungs and worst-case call budget in `05` — the ladder kills the flat-poll pathology of ~1,440 calls per ambiguous intent), polling `GET /{ig_container_id}?fields=status_code`, capped at container expiry (~24 h). The 60 s reconciler *sweep* cadence (`05`) is unchanged: `last_error.evidence` records `{checks:int, last_checked_at}`, and each sweep touches only intents whose next ladder step is due.
- **Verdicts:** an observed **authoritative-positive** value terminalizes `posted` (fetch media id/permalink if retrievable; cap debit stands). An observed **authoritative-negative** value terminalizes `failed` and refunds the cap. Every other value — including `ERROR`/`EXPIRED` sightings whose post-`publish_called` meaning 0.4 could not confirm — is recorded as evidence, never acted on, and the ladder continues.
- **Stories read-back** (`GET /{ig_user_id}/stories`, matched on the window around `publish_called`): evidence annotation only — never the sole basis for `posted`, in any mode.
- **Ladder exhausted:** one final stories check, the full trail into `last_error.evidence`, park `review_required` for the operator surface (06 §5).
- The two 0.4 outcomes are just the seam's values, named for `04`'s gates: **container-verdict mode** (a post-publish-observable value is confirmed — the authoritative-positive set is non-empty, and most ambiguities terminalize mechanically) and **evidence-capture mode** (0.4 finds none — both authoritative sets are empty, so the machine never self-terminalizes and every exhausted ladder parks `review_required` with the captured trail). `04` L.3's gate carries a test per mode.
- Every reconciler (or operator-resolution) verdict also terminalizes the `provider_operations` row it judged — `ambiguous → succeeded|failed` with the evidence in `response_ref` — so no op row stays ambiguous after its intent resolves, and the `ix_ops_retire` retention class eventually covers everything.
- The endpoint semantics (container `status_code` vocabulary **and which values, if any, are authoritative post-publish**, stories lookback validity) are 0.4 primary-doc verification items — platform inputs under `05`'s revision rule — and 0.4 is a named prerequisite of L.3/L.5 (`04`).
- Consequence for R1's honest wording (`01`), mode-independent: at-most-once *intended* publish is guaranteed by permit + fence + persisted container; the residual lost-response window always lands in `publishing_ambiguous`; a duplicate requires both a lost response **and** an operator resolution error — the machinery never blind-retries. Evidence-capture mode narrows what the machine claims, not the guarantee: it parks more and decides less.

**Inbound admission dedup (the L.8 replay gate's schema home — pass-2 addition; `01` §Process roles cites this):**

```sql
CREATE TABLE command_dedup (
  channel      TEXT NOT NULL,                   -- 'telegram' | 'web' | 'cli'
  principal    TEXT NOT NULL,                   -- '' for telegram (update ids are issued
                                                -- bot-globally by Telegram, already unique);
                                                -- session id (web) / service-token id (cli),
                                                -- so a key collision across tenants is
                                                -- structurally impossible (pass 3)
  external_ref TEXT NOT NULL,                   -- telegram update_id; web/cli idempotency token
  fingerprint  TEXT NOT NULL,                   -- SHA256 over the normalized command payload
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),  -- §0 insert-only class: rows never update
  PRIMARY KEY (channel, principal, external_ref)
);
-- Idempotent admission: the adapter inserts before dispatching the command. A duplicate delivery
-- hits the PK; the adapter then compares fingerprints (pass 3 — request binding): SAME
-- fingerprint ⇒ a true replay, acknowledged without re-execution (200 replayed callbacks ⇒ one
-- command); DIFFERENT fingerprint ⇒ the key was reused for different content — rejected as a
-- 409 conflict, never silently swallowed as a replay (a reused web/cli idempotency token with a
-- new command body is a caller bug or an attack; treating it as a replay would silently drop
-- the second command). Rows age out via retention (`05`) — the replay window Telegram can
-- produce is hours, the retention class keeps days.
```

**Durable pacing/admission counters (pass 3 — the one schema shared by L.4 sender pacing, S.2 admission, and 07 §1 pre-auth guards; previously each had numbers but no durable home):**

```sql
CREATE TABLE rate_counters (
  scope          TEXT NOT NULL
                 CONSTRAINT ck_rate_scope CHECK (scope IN
                   ('tg_chat','tg_global','ws_admission','preauth_ip')),
  key            TEXT NOT NULL,                 -- per scope: binding id | '' (one global row) |
                                                -- workspace id | client ip
  window_start   TIMESTAMPTZ NOT NULL,          -- fixed window: now() truncated to the scope's
                                                -- window length (05 owns lengths and limits)
  count          INTEGER NOT NULL DEFAULT 0 CONSTRAINT ck_rate_nonneg CHECK (count >= 0),
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),  -- §0 insert-only-class stamps: only count
  PRIMARY KEY (scope, key, window_start)              -- moves; age is immutable in window_start
);
CREATE INDEX ix_rate_retire ON rate_counters (window_start);  -- retention walk (§5 pattern)
```

The increment-and-check, verbatim — deliberately the same ON CONFLICT shape as the §4 cap debit, one idiom for every counter in the system:

```sql
INSERT INTO rate_counters AS rc (scope, key, window_start, count)
VALUES (:scope, :key, :window_start, 1)
ON CONFLICT (scope, key, window_start)
  DO UPDATE SET count = rc.count + 1 WHERE rc.count < :limit
RETURNING count;
-- zero rows ⇒ over limit for this window: the consuming site defers (sender pacing) or rejects
-- (admission / pre-auth guard). Atomic — never a lock, never check-then-act. :limit is the CURRENT
-- config value (05), compared at each hit — deliberately NOT frozen per window: cap_at_write's
-- freeze (§3) exists for refund integrity, counters have no refunds, and an abuse limit
-- tightened mid-attack should bite immediately, not next window.
```

Scope ↔ consumer map (every number lives in `05`): `tg_chat`/`tg_global` — L.4 Telegram sender pacing, the durable home the AIORateLimiter budgets move into; `ws_admission` — S.2 per-workspace command admission; `preauth_ip` — the 07 §1 pre-auth guard (the Google sign-in endpoints and every other unauthenticated surface). Ships at L.2 with the jobs machinery; rows age out via retention (`05` — windows are minutes to hours, the class keeps days). **Client-IP source rule (stated once, here; 07 and `05` cite it):** the client IP for `*_ip` scopes derives from the platform-terminated proxy header chain resolved **right-to-left past Railway's own trusted hops** — never the raw leftmost `X-Forwarded-For` entry, which the sender controls. The deployment's actual header behavior is verified in the real environment at L.8's gate (the same check that verifies webhook header trust) **before any IP-keyed guard is load-bearing**.

`service_runs` (legacy ops bookkeeping) survives unchanged during the program and is retired at S.4 (`03`).

## §7. RLS: policies, roles, and the tenancy backstop (C4)

The first pass said "system actors use dedicated roles with explicit predicates" — insufficient (review B§1): without a policy or bypass, a role sees zero rows regardless of its predicates. The second pass gave every personality policies but let one login `SET LOCAL ROLE` between them — which defeats the separation (R3 review §1.2/§6.7: a login that can assume every personality *is* every personality, and `svc_maintenance` held DELETEs on auth-plane tables whose rows its policies could never see). The pass-3 model is **non-escalatable by construction: per-process logins + SECURITY DEFINER doors, no `SET ROLE` anywhere.**

- **Login roles — one per process class, and none is a member of any other role** (so `SET ROLE` to anything is impossible in the database, not merely forbidden by convention):
  - `svc_ingress` — the ingress replicas' login.
  - `svc_worker` — the worker replicas' login. The elected clock runs inside a worker process on this same login: its extra privilege lives in a door, not in the login.
  - `svc_migration` — the runner/backfill login (Railway predeploy, six-stage tracks); broad per-track policies while tracks run; identifies itself via `app.actor_kind='migration'` (§4 insert guard).
- **No `BYPASSRLS` anywhere. No owner-role runtime connections. No role memberships.** Every cross-tenant capability is a named, reviewable object: a `CREATE POLICY` on a login role, or a `SECURITY DEFINER` door owned by a NOLOGIN system role.
- **Tenant policies** (on `svc_ingress`, `svc_worker`): constant-expression policies on every workspace-scoped table — `USING (workspace_id = current_setting('app.tenant_id')::uuid)` (and identical `WITH CHECK`). Every tenant transaction opens with `SET LOCAL app.tenant_id = …` set by the UoW factory, which takes `tenant_id` as a required constructor argument — a UoW without a tenant is unconstructible in code, and a query without one fails closed in the DB (`current_setting` on an unset GUC errors; the policy denies). Transaction-pooled connection reuse is safe because `SET LOCAL` dies with the transaction. **Machinery exception, stated exactly (`jobs`):** both logins' `jobs` policies read `USING (workspace_id IS NULL OR workspace_id = current_setting('app.tenant_id', true)::uuid)` — a worker must finalize and reschedule the system jobs it executes, and system-kind rows carry `workspace_id NULL` regardless of which context produced them (the §5 classing rule — the FC-6 invitation send is enqueued post-auth but its row is still NULL-workspace); the missing-GUC form (`, true` → NULL) then exposes **only system rows**, never another tenant's. `rate_counters` (§6) carries plain role-scoped `USING (true)` policies for both logins — its keys are deliberately not all tenant-shaped — and no login may DELETE from it.
- **NOLOGIN system roles own the cross-tenant capabilities as SECURITY DEFINER doors.** Every door: `SET search_path = pg_catalog, public`, owned by its system role, `EXECUTE` revoked from PUBLIC and granted **only** to the one login that drives it. The system role holds exactly the `USING (true)` policies and grants its door bodies need — **the door inventory below is the enumeration the security review reads**; a new door is a migration plus a row here. Each door sets its own actor GUC (`set_config('app.actor_kind', …, true)`) at entry, so the §4 audit machinery names the operation regardless of caller.

| Door (owner) | Body / effect | EXECUTE |
|---|---|---|
| `fn_claim_job(lane)` (`svc_claim`) | the §5 claim query, verbatim | `svc_worker` |
| `fn_extend_leases(tokens uuid[])` (`svc_claim`) | heartbeat: one UPDATE extending `locked_until` on live leases matching the caller's lease tokens | `svc_worker` |
| `fn_clock_tick()` (`svc_clock`) | the L.7 tick: due-scan over `ix_ig_accounts_due` + idempotent intent/job inserts (≤ the `05` per-tick bound); refresh scheduling reads `vw_credentials_schedule`, the payload-free view its owner role reads instead of the table | `svc_worker` (the elected clock) |
| `fn_reconciler_sweep(lim int)` (`svc_maintenance`) | returns the ladder-due batch of parked intents (§6) with their workspace ids — a cross-tenant **read**; each verdict then writes tenant-scoped as `svc_worker` (the intent's workspace is known), so the write path needs no cross-tenant privilege | `svc_worker` |
| `fn_reaper_sweep(lim int)` (`svc_maintenance`) | the §5 reap classes living in tenant tables — intent expiry flips, expired `post_locks`, stale `workspace_invitations` — plus the `jobs` expired-lease re-ready (`leased → ready` via `ix_jobs_lease_expiry`); bounded, inside the door | `svc_worker` |
| `fn_retention_batch(class text)` (`svc_maintenance`) | one `05` retention class per call: age-qualified DELETE walking its `ix_*_retire` index; the `audit_events` class COPY-exports into the archive schema first and **aborts if the export fails** (07 §4) | `svc_worker` |
| `fn_comparator_run(track text)` (`svc_maintenance`) | the six-stage shadow-read comparator for one track (counts + per-row checksums over the track's canonical mapping) | `svc_worker` |
| `fn_offboard_finalize(ws uuid)` (`svc_maintenance`) | the final-deletion leg of 06 §1, guarded inside the body (state='offboarding', grace window elapsed, zero live intents) → DELETE the `workspaces` row; the §0 cascade does the rest | `svc_worker` (the `offboard_workspace` executor) |
| `fn_auth_plane_sweep()` (`svc_maintenance`) | expiry/retention deletes on `oauth_states`, `session_tokens`, `command_dedup`. `svc_maintenance` carries three enumerated auth-plane `USING (true)` policies + DELETEs **for exactly this door** — resolving the pass-2 contradiction (R3 §6.7, maintenance held DELETEs its policies could never see) by adding the missing policies to the NOLOGIN sweep owner, never by handing a login the DELETEs (a `svc_ingress`-owned definer body would have put auth-plane DELETE privileges on the internet-exposed login — falsifying the grant matrix below) | `svc_worker` (the reaper/retention schedule drives it) |

- **The boundary, stated honestly:** a compromised worker request path can call the doors its login holds (claim jobs, tick the clock, drive sweeps whose bodies are fixed SQL) and can write within tenant policies on the tenant it sets. It cannot `SET ROLE` (no memberships exist to assume), cannot DELETE anything directly (no login holds DELETE), cannot read auth-plane rows or credential ciphertext beyond its column grants, and cannot enlarge a sweep (door bodies are fixed, `search_path`-pinned SQL). Maintenance capability lives in door bodies, not in any login.
- **Auth-plane tables** (`07`: `session_tokens`, `oauth_states`, `service_tokens`, plus `command_dedup` above) are deliberately **not tenant-RLS'd** — they are the door tenant context walks through (a sign-in or OAuth callback happens before any `app.tenant_id` exists). They carry role-scoped `USING (true)` policies for `svc_ingress` (the runtime reader/writer) and for `svc_maintenance` (the sweep-door owner, DELETE included) — no other role, and **no login but `svc_ingress`** touches them; their sweep is `fn_auth_plane_sweep`. `workspace_invitations` IS tenant-scoped (accept runs post-auth, in tenant context) and follows the normal tenant policies.
- **Execution-context flow:** the worker claims via `fn_claim_job`, then opens the job's domain transactions as itself — `svc_worker` with `SET LOCAL app.tenant_id = job.workspace_id`; system jobs execute their doors on schedule. Nothing survives a commit, and no personality switch exists to leak.
- **Grant matrix (beyond RLS):** **no login holds DELETE on anything** — tenant deletes happen only inside `svc_maintenance`-owned door bodies, auth-plane deletes only inside `fn_auth_plane_sweep` (§0's "runtime never deletes" is thereby structural for every login, not just tenant roles); `audit_events` grants: INSERT to all three logins and the system roles (trigger inserts run as the mutating role), UPDATE to none, DELETE only inside `fn_retention_batch`; `oauth_credentials.encrypted_payload` is column-SELECTable only by `svc_worker`/`svc_ingress` (credentials service paths) — clock scheduling reads happen inside `fn_clock_tick` through `vw_credentials_schedule`, which omits the column.
- **The staged NOT NULL procedure (the only legal way this plan adds NOT NULL to a populated table — the first pass's "`NOT NULL` added `NOT VALID`" does not exist in PostgreSQL):**
  1. `ALTER TABLE t ADD CONSTRAINT ck_t_col_nn CHECK (col IS NOT NULL) NOT VALID;`
  2. backfill (batched, six-stage machine rules);
  3. `ALTER TABLE t VALIDATE CONSTRAINT ck_t_col_nn;`
  4. `ALTER TABLE t ALTER COLUMN col SET NOT NULL;`  — PostgreSQL uses the validated CHECK to skip the scan
  5. `ALTER TABLE t DROP CONSTRAINT ck_t_col_nn;`
- Enablement discipline per table: zero-NULL gates before and after cutover; the RLS harness runs as the exact runtime roles, no owner privileges, no session-affinity assumptions (#722 P0-09, kept verbatim).
- Known limit, mitigated not ignored: integrity errors bypass RLS and can act as cross-tenant existence oracles. Unique keys on tenant-scoped tables lead with `workspace_id` and ids are UUIDs, so the general surface has no enumerable oracle. The complete inventory of deliberate exceptions (pass-2 correction — there are three, not one): **`uq_publish_exclusive`** (key 4) leaks "some workspace is publishing to this real account" — swallowed into the defer path, never user-visible; **`uq_binding_external`** leaks "this Telegram chat is already bound somewhere" on enumerable chat ids — inherent to the product (a chat cannot be double-bound, and the person probing must already be adding the bot to that chat); **`uq_ops_business_key`** is keyed on unguessable intent UUIDs — structurally not an oracle. Log/error hygiene per the 07 rule (internal ids everywhere, never `provider_account_ref`).

## §8. Two caps, never conflated

- **Product cadence cap** — ours: `daily_post_counts` in the account's effective tz, calendar-day semantics, atomically debited in the approved→publishing transaction (§4 SQL), refunded on failure against the recorded debit day. The only cap we count locally.
- **Meta publish cap** — theirs (`05` platform inputs: 100 / rolling 24 h / real account, corrected at the pass-4 anchor; the authoritative per-account value is the live `content_publishing_limit` read), enforced by Meta on the publish step (error 9). Never counted locally, and for the **right** reason (pass-3 correction of a self-contradiction, R3 review §6.10 — the pass-2 text blamed phone posts and then said the cap covers API publishes only; a phone post cannot consume an API-publishing quota): a local counter is wrong by construction because **other API grants on the same real account consume the same quota** — the account connected in other workspaces (PA-1(a)) and any other tool the customer has authorized — on top of the rolling-window shape. Our ledger sees only our own publishes; Meta's counter sees them all. 0.4 verifies the cap's shape against Meta's primary documentation. On error 9: defer — a cap, not a fault, so no quarantine row. **`available_at` derivation is uncertain pending 0.4:** the usage endpoint's documented shape (a count over a duration) does not obviously expose the oldest-publish timestamp an exact next-free-slot calculation needs; if 0.4 finds no usable shape, the stated fallback is a conservative deferral to the account's next product slot — correctness never depends on the derivation, because error 9 re-arbitrates on every attempt. Manual-mode posts (§4) debit only OUR cap; Meta's applies to API publishes alone.
- **The advisory pre-check is lazy, inline, shared — and ships behind a default-off flag.** There is **no background refresh job** (no such kind exists in the §5 registry — an implementer following this plan cannot build one). When enabled, the check runs inside the publish pipeline, immediately before the §4 flip transaction, against an in-process cache keyed on **`provider_account_ref`** (shared across duplicate workspace rows of one real account), TTL per `05`. Worst-case provider load is therefore ≤ one usage query per publish attempt — strictly bounded by publish traffic itself, never by account count. (First-pass defect, review B§6, quoted at its pass-1 envelope: up to 1,000 queries/min against ~87 publishes/min; at `05`'s corrected multi-account inputs the same eager reading is ~1,500/min against ~130/min — see D21; at the pass-4 cap correction (25→100, `05` platform inputs) the absolute-ceiling rate is ~520/min, and the eager reading still exceeds the fleet's entire real work at full cap. The advisory mechanism would have manufactured the very load it advises about, buying no correctness since error 9 is authoritative regardless.) Miss/stale/error/flag-off on the pre-check ⇒ proceed to the flip; error 9 remains the arbiter. **The flag is OFF by default** (process-class flag, C7): even one read per attempt is a cost the check has not yet earned — the S.5 canary measures whether skipping doomed container/transit work pays for the calls, and the flag flips only on that evidence (`04` S.5).

## §9. Legacy → target mapping (all 14 current tables + ledger accounted for)

Every re-key runs on the six-stage machine (`04` §Ground rules). Column-level mapping tables live in each track's spec (`04` Phases F/W) — this table is the disposition index. Legacy **naive** `TIMESTAMP` columns convert with `AT TIME ZONE 'UTC'` (§0), no exceptions among the naive set; the three already-`TIMESTAMPTZ` columns §0 names copy as-is.

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
| `posting_history` | `post_intents` terminal states (posted/failed/skipped/rejected/expired map 1:1; `posting_method`/usernames → audit detail; `instagram_media_id`/`instagram_story_id`/permalink → ig_media_id/ig_permalink; `queue_item_id` → legacy_queue_item_id) — inserted as actor `migration` (§4 insert guard) |
| `category_post_case_mix` | **kept row-shaped** (it is a Type 2 SCD table today — `workspaces.category_mix` JSONB from the first pass is struck): `category_post_case_mix` re-keyed to `workspace_id`, SCD semantics unchanged, sum-to-1 stays service-enforced (a cross-row DB constraint would need a deferred aggregate trigger; not worth its complexity — recorded trade-off) |
| `onboarding_sessions` | kept, re-keyed: `user_id` stays; `pending_chat_settings_id` → `pending_workspace_id`; step vocabulary widened for the web sign-in path (07 §§1–2): `naming`,`awaiting_group`,`connect_identity`,`complete` — this row is the step list's normative home (`connect_identity` = link a web-capable identity, the 07 §2 `link` flow; the pass-3 name `connect_email` died with OTP); 24 h expiry + `UNIQUE(user_id)` (one live session per user) kept |
| `audit_log` | merged into `audit_events` (entity_type/action/field/old/new → entity_kind + detail; rows migrated verbatim into `detail`) |
| `service_runs` | kept as-is + nullable `workspace_id`; retired at S.4 (`03`) |
| `schema_version` | superseded by the 0.2 runner's `schema_migrations` ledger (`04` 0.2 — richer metadata; old table retained read-only until W-phase contract) |
| `waitlist_signups` | **out of scope, permanently**: owned by the landing site's Drizzle ORM; no Python migration may touch it (existing repo rule, carried forward) |
