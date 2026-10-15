```sql
CREATE FUNCTION trg_touch_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END $$;

-- The one tz gate (pass 6 — R5): returns its argument when PostgreSQL recognizes it as a time
-- zone, 'UTC' otherwise. Two consumers, one rule: the ck_*_tz_valid CHECKs below make a bad
-- value unstorable at write, and fn_next_slot converts through it so a stored value that later
-- DECAYS (tzdata removals are rare but real — the reason this is not IMMUTABLE) degrades that
-- one row to UTC instead of aborting the caller's whole set-based statement. The legacy
-- scheduler caught invalid zones per row and fell back to UTC; R5's regression finding is the
-- general rule — replacing a per-row path with a set-based one must not turn a survivable
-- per-row fault into a whole-batch abort.
CREATE FUNCTION fn_safe_tz(p_tz text) RETURNS text LANGUAGE plpgsql STABLE AS $$
BEGIN
  PERFORM now() AT TIME ZONE p_tz;
  RETURN p_tz;
EXCEPTION WHEN invalid_parameter_value THEN
  RETURN 'UTC';
END $$;
```

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
  tz                      TEXT NOT NULL DEFAULT 'UTC'           -- IANA name; workspace default
                          CONSTRAINT ck_ws_tz_valid CHECK (fn_safe_tz(tz) = tz),
                          -- write-time backstop (R5: tz was unconstrained; the service boundary
                          -- validates first, per the materialization contract; decay-after-write
                          -- is the §0 fn_safe_tz read-side case)
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
  api_publishing_enabled  BOOLEAN NOT NULL DEFAULT false,
                          -- the manual-vs-API mode flag (06 §3 semantics: off = manual-mode cards,
                          -- on = hybrid with the publish pipeline). FC-7 successor of the C7
                          -- cohort-routing flag row: with the cutover flags dead, the one
                          -- surviving per-workspace routing fact is plain product config, and a
                          -- workspaces column is its home (legacy chat_settings.enable_instagram_api)
  offboarding_at          TIMESTAMPTZ NULL,
                          -- set when state enters 'offboarding'; cleared on restore. The durable
                          -- grace-window anchor fn_offboard_finalize guards on (§7-DDL) — audit
                          -- rows record the transition but a door must not parse audit to tell time
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
--   last_post_sent_at → per-account slot anchor (§2 ig_accounts.next_slot_at derivation, 04 M.1)
--   onboarding_step/onboarding_completed → onboarding_sessions (§9)
--   enable_instagram_api → workspaces.api_publishing_enabled (pass 5/FC-7: the cohort-routing
--     flag table died with the live cutover; the surviving per-workspace mode flag is product
--     config, homed above)
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
  invited_tg_user_id  BIGINT NULL,              -- the ONLY value the D33 Telegram acceptance
                                                -- constraint may match: the provider's IMMUTABLE
                                                -- numeric user id (the D33/D32 no-mutable-
                                                -- identifier rule). Never a lookup key —
                                                -- acceptance resolves by token_hash alone
  invited_channel_hint TEXT NULL,               -- display/delivery data only (a username, a
                                                -- name); never an authorization input — hint-only
                                                -- invitations take the recorded-skip path (D33/D36)
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
-- ACCEPT runs inside fn_invitation_accept (§7-DDL — the pre-membership door R4 found missing:
-- the acceptor is authenticated but not yet a member, holds only the token, and tenant RLS
-- structurally cannot resolve token_hash → workspace_id for them). The body is one CAS with the
-- member INSERT in the SAME transaction:
--   UPDATE workspace_invitations
--      SET state='accepted', accepted_by_user_id=:u, accepted_email_matched=:m
--    WHERE id=:id AND state='pending' AND expires_at > now() RETURNING id;
-- zero rows ⇒ used/revoked/expired (a re-read distinguishes "already yours" from "someone else
-- took it"); workspace_members' PK (workspace_id, user_id) is the double-membership guard —
-- already in the schema, nothing to add. The match fact :m is computed inside the door per the
-- D33 constraint table (the door body in §7-DDL is the executable home).
-- THE MEMBER INSERT'S ROLE IS COMPUTED, NOT COPIED (D36):
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

```sql
CREATE TABLE onboarding_sessions (            -- target DDL (pass 5 — R4 finding 3e; §9 row).
                                              -- Printed after workspaces: the FK below needs it
  id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id                UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  step                   TEXT NOT NULL DEFAULT 'naming'
                         CONSTRAINT ck_onboarding_step
                         CHECK (step IN ('naming','awaiting_group','connect_identity','complete')),
  pending_workspace_name TEXT NULL,            -- legacy pending_instance_name, target vocabulary
  pending_workspace_id   UUID NULL REFERENCES workspaces(id) ON DELETE SET NULL,
  expires_at             TIMESTAMPTZ NOT NULL, -- now() + 24 h at insert (05 seam)
  created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_onboarding_one_per_user UNIQUE (user_id)   -- one live session per user (§9)
);
CREATE TRIGGER tg_touch_onboarding_sessions BEFORE UPDATE ON onboarding_sessions
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();
-- User-plane (no workspace key — onboarding precedes the workspace it creates); §7-DDL policy
-- class 3. The step CHECK above is the step list's one normative home (§9 cites it):
-- connect_identity = link a web-capable identity (the 07 §2 link flow). Stale sessions expire
-- via reap_expired (§5 remit).
```

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
  tz                   TEXT NULL
                       CONSTRAINT ck_iga_tz_valid CHECK (tz IS NULL OR fn_safe_tz(tz) = tz),
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
--   manual: clear-quarantine operator command EXPIRES the row — UPDATE SET quarantined_until =
--           now(), strike_count = 0 — and re-readies deferred run_at (audited). Expiry-by-update,
--           not DELETE: no login holds DELETE (§7); the row is overwritten by the next fault.
--   NOT quarantine: credential revocation (state='revoked' → reauth flow, 07) and Meta cap
--   error 9 (a cap, §8) — neither writes here.

CREATE TABLE media_sources (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id         UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  provider             TEXT NOT NULL
                       CONSTRAINT ck_sources_provider CHECK (provider IN ('gdrive')),
                       -- FC-8/D37: a pluggable adapter surface behind a closed CHECK (§0/D15);
                       -- the open set is D37's stated add cost. v1 ships exactly 'gdrive'
  config               JSONB NOT NULL           -- adapter-defined, versioned; gdrive: {v:1,
                       -- folder_ref:text, root_name?:text}. Each provider's shape is a D37
                       -- contract-table row
                       CONSTRAINT ck_sources_config_v CHECK (jsonb_typeof(config->'v') = 'number'),
  sync_checkpoint      JSONB NULL,              -- adapter-defined, versioned; gdrive: {v:1,
                       -- page_token?:text}; cursor-style providers use their own keys
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
                    CHECK (provider IN ('ig_login','gdrive')),
                    -- no fb_login_legacy value exists (pass 5 — `00` FC-4 application note under
                    -- FC-7). A media-source credential's provider always equals its source's
                    -- provider (D37 invariant, service-enforced; the per-source unique below
                    -- already keys by provider). Media providers extend per D37's add cost
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
-- sensitive table (C4). The per-account unique keys by provider — one credential per (account,
-- provider); with the target ig-side set at exactly 'ig_login' this is one live IG credential
-- per account row in practice, and the key shape needs no change if a provider is ever added.
-- State transitions (complete): active → expired (a definitive provider auth-rejection observed
-- on ANY Meta call — scheduled refresh, publish pipeline, or the §8 pre-check if enabled — or the
-- 07 §3 decrypt failure; the liveness-edge paragraph below states the discrimination) ·
-- active → revoked (user disconnect / offboarding / account movement — the move transaction
-- revokes the source row as it copies the payload to the target workspace, 06 §4: a move, not a
-- fork; exactly one active row per grant, so refresh can never diverge two copies) ·
-- {active,expired,revoked} → active (reconnect, 07 §2: an UPSERT on the owner unique key —
-- payload swapped and state flipped on the existing row, same row id, no delete, no
-- zero-credential window; the swap is audited).
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
  provider_file_ref TEXT NOT NULL,              -- the adapter's CANONICAL STABLE item identifier
                                                -- (legacy source_identifier), adapter-defined
                                                -- format, opaque to core. ADAPTER CONTRACT (D37,
                                                -- normative): stable across provider-side
                                                -- rename/move and unique within the source —
                                                -- gdrive: the file id; a path is never a legal
                                                -- ref (an unstable ref makes sync mint a fresh
                                                -- row on rename, resetting recency and evading
                                                -- the media_item_id-keyed recent locks, R4/D37)
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
-- Legacy file_hash is NOT unique today (duplicates exist in production) — the M window carries a
-- human-gated dedup remediation precondition (existing dedup-media tooling) with a
-- zero-duplicates gate BEFORE the transform runs (04 M.1), same pattern as 0.3's remediation.
-- Legacy columns not carried: cloud_* transit columns (transit state is per-attempt:
-- post_intents.transit_asset_ref), instagram_media_id/backfilled_at (posted evidence is per-intent:
-- §3 ig_media_id; legacy values ride the M.1 history transform), is_active (→ state), file_path
-- (Drive path context folds into file_name; identity is provider_file_ref). No legacy
-- "unsupported" flag exists (pass-4 anchor): today Meta 9004 writes a permanent_reject LOCK, not
-- a media_items column — those rows ride the M.1 lock mapping; the target's 'unsupported' media
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
-- Legacy kind mapping (M.1 transform): recent_post→recent, skip→skip, permanent_reject→reject,
-- manual_hold→hold, seasonal→seasonal.

CREATE TABLE category_post_case_mix (         -- target DDL (pass 5 — R4 finding 3e; §9 row, D23:
                                              -- kept row-shaped, Type 2 SCD semantics unchanged)
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id       UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  category           TEXT NOT NULL,            -- matches media_items.category
  ratio              NUMERIC(5,4) NOT NULL
                     CONSTRAINT ck_case_mix_ratio CHECK (ratio >= 0),
  effective_from     TIMESTAMPTZ NOT NULL DEFAULT now(),
  effective_to       TIMESTAMPTZ NULL,         -- NULL = the current row (SCD-2, legacy semantics;
                                               -- the legacy is_current boolean is redundant with
                                               -- this and is not carried — the M.1 mapping states it)
  created_by_user_id UUID NULL REFERENCES users(id) ON DELETE SET NULL,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TRIGGER tg_touch_category_post_case_mix BEFORE UPDATE ON category_post_case_mix
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();
CREATE UNIQUE INDEX uq_case_mix_current ON category_post_case_mix (workspace_id, category)
  WHERE effective_to IS NULL;
CREATE INDEX ix_case_mix_current ON category_post_case_mix (workspace_id)
  WHERE effective_to IS NULL;
-- Supersede = set effective_to on the current row + INSERT the new one, one transaction (the
-- partial unique makes two current rows for a category impossible). Sum-to-1 across a
-- workspace's current rows stays service-enforced (D23).
```

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
                       -- designed only the API path; both are supported). 'legacy_backfill':
                       -- M.1 history-transform rows, exempt from evidence requirements below.
  provider_account_ref TEXT NOT NULL,           -- immutable copy from ig_accounts at creation (key 4)
  publish_step         TEXT NOT NULL DEFAULT 'none' CONSTRAINT ck_intent_step CHECK (publish_step IN
                         ('none','transit_uploaded','container_created','container_ready',
                          'publish_called','effect_confirmed')),
  ig_container_id      TEXT NULL,               -- persisted BEFORE the publish call (R1)
  ig_media_id          TEXT NULL,               -- the published media id — outcome evidence
  ig_permalink         TEXT NULL,
  transit_asset_ref    TEXT NULL,               -- Cloudinary public id for FC-3.5 reap
  cap_consumed_on      DATE NULL,               -- the account-local calendar day this intent debited (R2)
  cap_refunded_at      TIMESTAMPTZ NULL,        -- set iff the debit was returned (failed after
                                                -- debit); cleared with cap_consumed_on on
                                                -- resolve-retry so the row reads debit-neutral (§4)
  attempts_by_step     JSONB NOT NULL DEFAULT '{"v":1}'
                       CONSTRAINT ck_intent_attempts_v
                       CHECK (jsonb_typeof(attempts_by_step->'v') = 'number'),
                       -- {v:1, <step>:{count:int, generation:int, last_error_class?:text}}
  last_error           JSONB NULL,              -- {v:1, class:text, provider_code?:text, message:text,
                       --  evidence?:object}  — reconciler evidence lands here (§6)
  legacy_queue_item_id UUID NULL,               -- transform provenance (pass 5/FC-7: populated
                                                -- only by M.3's history transform, whose rows
                                                -- are terminal at insert). Drop rule, mechanical:
                                                -- 30 d after M.3 → plain column-drop migration
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
CREATE INDEX ix_audit_retire ON audit_events (created_at);   -- the retention export's oldest-first
                                                             -- walk (§7-DDL; the §5 ix_*_retire pattern)
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
CREATE INDEX ix_dpc_retire ON daily_post_counts (local_date);  -- retention walk (§5 pattern; the
                                                               -- PK leads with workspace_id, so
                                                               -- age-ordered sweeps need this)
-- OUR product cadence cap — never Meta's (§8). Debit/refund SQL is in §4 (the transition owns it).
```

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

```sql
-- Reaper access paths (pass 5 — the H5 discipline applied to the reaper's own scans: post_intents
-- is kept forever (05 retention), so an unindexed working-state scan would re-walk the entire
-- ledger every sweep. Partial indexes bound each scan to the live working rows — a set that is
-- small at any instant regardless of ledger age):
CREATE INDEX ix_intents_reap_slot ON post_intents (schedule_slot_at)
  WHERE state IN ('scheduled','prompt_pending');
CREATE INDEX ix_intents_reap_age ON post_intents (entered_state_at)
  WHERE state IN ('awaiting_approval','approved');
```

```sql
CREATE TABLE post_intent_transitions (        -- the legal-edge reference table, seeded below
  from_state TEXT NOT NULL,
  to_state   TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),  -- §0 insert-only class: edges are added or
  PRIMARY KEY (from_state, to_state)              -- deleted, never updated — no touch machinery
);

-- THE SEED ROWS (pass 5 — R4 finding: the matrix was printed but never seeded, so a replayed
-- schema rejected every transition while replaying green). These 27 rows ARE the matrix below,
-- one row per edge; the matrix table remains the annotated normative statement (actors, guards,
-- effects), and the advertised_ddl_replay fixture (04 0.2) replays this block and then asserts
-- one legal transition succeeds and one illegal transition raises — text and seeds cannot drift.
INSERT INTO post_intent_transitions (from_state, to_state) VALUES
  ('scheduled','prompt_pending'), ('scheduled','expired'), ('scheduled','cancelled'),
  ('prompt_pending','awaiting_approval'), ('prompt_pending','failed'),
  ('prompt_pending','expired'), ('prompt_pending','cancelled'),
  ('awaiting_approval','approved'), ('awaiting_approval','posted'),
  ('awaiting_approval','skipped'), ('awaiting_approval','rejected'),
  ('awaiting_approval','expired'), ('awaiting_approval','cancelled'),
  ('approved','publishing'), ('approved','expired'), ('approved','cancelled'),
  ('publishing','posted'), ('publishing','publishing_ambiguous'),
  ('publishing','failed'), ('publishing','review_required'),
  ('publishing_ambiguous','posted'), ('publishing_ambiguous','failed'),
  ('publishing_ambiguous','review_required'),
  ('review_required','posted'), ('review_required','approved'),
  ('review_required','failed'), ('review_required','cancelled');

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
  -- transform's privilege (M.1), recognized by the actor GUC it already must set — no second
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
-- increment on, every governance writer sets the actor GUCs.
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
  -- PL/pgSQL RULE THIS SHAPE DEPENDS ON (normative for every generic multi-table trigger in
  -- this plan): a NEW./OLD. field reference is resolved when its enclosing EXPRESSION is set
  -- up for the firing table's row type — a false left conjunct short-circuits the VALUE, never
  -- the FIELD resolution. `TG_TABLE_NAME = 'x' AND NEW.<x-only field> …` as ONE expression
  -- therefore errors on every OTHER table (`record "new" has no field …`, the R5 P0). Table
  -- dispatch must be an IF STATEMENT, whose branch body is parsed only when reached for a row
  -- type that has the fields — which is why the exits below are nested, not AND-chained.
  IF TG_OP = 'UPDATE' THEN
    IF TG_TABLE_NAME = 'ig_accounts' THEN
      IF ROW(NEW.workspace_id, NEW.provider_account_ref, NEW.handle, NEW.display_name, NEW.state,
             NEW.posts_per_day, NEW.posting_hours_start, NEW.posting_hours_end, NEW.tz)
         IS NOT DISTINCT FROM
         ROW(OLD.workspace_id, OLD.provider_account_ref, OLD.handle, OLD.display_name, OLD.state,
             OLD.posts_per_day, OLD.posting_hours_start, OLD.posting_hours_end, OLD.tz) THEN
        RETURN NULL;                             -- next_slot_at / last_posted_at advance only
      END IF;
    ELSIF TG_TABLE_NAME = 'oauth_credentials' THEN
      IF ROW(NEW.workspace_id, NEW.ig_account_id, NEW.media_source_id, NEW.provider,
             NEW.encrypted_payload, NEW.state)
         IS NOT DISTINCT FROM
         ROW(OLD.workspace_id, OLD.ig_account_id, OLD.media_source_id, OLD.provider,
             OLD.encrypted_payload, OLD.state) THEN
        RETURN NULL;                             -- next_refresh_at / expires_at advance only
      END IF;
    END IF;
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

```sql
BEGIN;
SET LOCAL app.tenant_id = :workspace_id;  SET LOCAL app.actor_kind = 'operator';
-- refund the recorded day FIRST (the UPDATE below clears the pointer it targets):
UPDATE daily_post_counts SET count = count - 1
 WHERE (workspace_id, ig_account_id, local_date) = (:ws, :acct,
        (SELECT cap_consumed_on FROM post_intents WHERE id = :intent))
   AND count > 0;
UPDATE post_intents
   SET state = 'approved',
       cap_consumed_on = NULL, cap_refunded_at = NULL,   -- debit-neutral: the row re-enters the
                                                          -- working states as if freshly approved
       publish_step = 'none', ig_container_id = NULL, transit_asset_ref = NULL,
       attempts_by_step = :attempts_with_generation_bumped  -- service-computed; fresh business keys
 WHERE id = :intent AND state = 'review_required';
-- zero rows on the intent UPDATE ⇒ a race resolved it first: ROLLBACK (the refund rolls back too).
COMMIT;
```

```sql
-- (this table doubles as the 04 step-8 identity guards' target-only marker — it has no
-- legacy counterpart; renaming it is a change to those guards)
CREATE TABLE jobs (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  kind              TEXT NOT NULL CONSTRAINT ck_jobs_kind CHECK (kind IN (
                      -- tenant kinds (workspace_id NOT NULL):
                      'plan_slot','publish_pipeline','deliver_outbox','sync_media_source',
                      'first_ingest_chunk','refresh_credential','offboard_workspace',
                      'revoke_workspace_credentials','reauth_prompt',
                      -- system kinds (workspace_id NULL):
                      'reconcile_ambiguous','reap_expired','reap_transit_assets','retention_sweep',
                      'reencrypt_credentials','send_email')),
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
       'reencrypt_credentials','send_email')))
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

```sql
CREATE TABLE rate_counters (
  scope          TEXT NOT NULL
                 CONSTRAINT ck_rate_scope CHECK (scope IN
                   ('tg_chat','tg_global','ws_admission','preauth_ip','email_global')),
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

```sql
-- ROLES ARE NOT CREATED HERE (pass 7 — R6's P0; the split and its rejected alternatives: D40).
-- This stream executes as svc_migration, which must not hold role DDL's cluster privileges;
-- the seven service roles (inventory: §7 above) are provisioned by the 04 window-bootstrap
-- artifact, the one place guarded/conditional DDL is legal — the pass-6 "production adoption
-- skips roles that exist" prose escape is dead, and this stream stays byte-fixed AND
-- executable by its declared production actor.
-- Passwords and connection strings are deployment env, never DDL.

-- GRANT BASELINE: nothing by default, then narrow verb grants. RLS then narrows rows.
-- (Pass-5 simplification: the pass-3 payload-free VIEW is gone — svc_clock's column-level
-- SELECT grant below enforces the identical property with one column list instead of two,
-- and one fewer object. The clock reads oauth_credentials directly; encrypted_payload is
-- simply not in its grant, so selecting it fails at the privilege layer.)
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public
  TO svc_ingress, svc_worker, svc_claim, svc_clock, svc_maintenance, svc_membership;

-- Login verb grants. NO DELETE APPEARS ANYWHERE IN THIS BLOCK — §0's "runtime never deletes",
-- structural: deletes exist only inside door bodies below, as their NOLOGIN owners.
GRANT SELECT, INSERT, UPDATE ON
  workspaces, workspace_members, channel_bindings, workspace_invitations,
  ig_accounts, provider_quarantine, media_sources, oauth_credentials,
  media_items, post_locks, post_intents, daily_post_counts,
  channel_outbox, provider_operations, category_post_case_mix,
  jobs, users, user_identities, onboarding_sessions
  TO svc_ingress, svc_worker;
GRANT SELECT, INSERT, UPDATE ON rate_counters TO svc_ingress, svc_worker;
GRANT SELECT, INSERT ON command_dedup TO svc_ingress;
GRANT SELECT ON post_intent_transitions
  TO svc_ingress, svc_worker, svc_claim, svc_clock, svc_maintenance, svc_membership;
GRANT INSERT ON audit_events
  TO svc_ingress, svc_worker, svc_claim, svc_clock, svc_maintenance, svc_membership;
GRANT SELECT ON audit_events TO svc_ingress, svc_worker;   -- the web audit view (06 §7)

-- Door-owner verb grants (each enumerated privilege backs exactly one body below).
GRANT SELECT, UPDATE ON jobs TO svc_claim;
GRANT SELECT ON provider_quarantine TO svc_claim;
GRANT SELECT ON workspaces, ig_accounts, media_sources TO svc_clock;
GRANT UPDATE (next_slot_at)    ON ig_accounts   TO svc_clock;  -- column-grant: the tick advances
GRANT UPDATE (next_sync_at)    ON media_sources TO svc_clock;  -- machinery columns and nothing else
GRANT SELECT (id, workspace_id, ig_account_id, media_source_id, provider,
              expires_at, next_refresh_at, state) ON oauth_credentials TO svc_clock;
GRANT UPDATE (next_refresh_at) ON oauth_credentials TO svc_clock;
GRANT SELECT, INSERT ON jobs TO svc_clock;
GRANT SELECT, UPDATE ON post_intents TO svc_maintenance;               -- reaper edge flips
GRANT SELECT, DELETE ON post_locks TO svc_maintenance;                 -- expired-lock purge
GRANT SELECT, UPDATE ON workspace_invitations TO svc_maintenance;      -- expiry state flips
GRANT SELECT, DELETE ON onboarding_sessions TO svc_maintenance;        -- stale-session purge
GRANT SELECT, UPDATE, DELETE ON jobs TO svc_maintenance;               -- lease re-ready + retention
GRANT SELECT, DELETE ON channel_outbox, provider_operations,
                        daily_post_counts, rate_counters TO svc_maintenance;  -- retention classes
GRANT SELECT, DELETE ON command_dedup TO svc_maintenance;  -- fn_auth_plane_sweep's third leg
                        -- (R5: the p_maint_dedup policy existed with no table grant behind it —
                        -- a policy narrows rows but cannot confer the verb; the sweep hit
                        -- permission denied at the GRANT layer)
GRANT SELECT, DELETE ON audit_events TO svc_maintenance;               -- export-then-delete only
GRANT SELECT, DELETE ON workspaces TO svc_maintenance;                 -- fn_offboard_finalize
GRANT SELECT ON workspaces TO svc_membership;
GRANT SELECT, UPDATE ON workspace_invitations TO svc_membership;
GRANT SELECT, INSERT ON workspace_members TO svc_membership;
GRANT SELECT ON channel_bindings TO svc_membership;
GRANT INSERT ON channel_outbox TO svc_membership;
-- The archive schema: created empty here; M.3 snapshot migrations ALTER their tables' OWNER TO
-- svc_maintenance so retention can DROP them on expiry (05). No login role receives any grant.
CREATE SCHEMA archive;
GRANT USAGE, CREATE ON SCHEMA archive TO svc_maintenance;

-- ROW LEVEL SECURITY: enabled on every table in this file, at creation (FC-7 killed the staged
-- W-phase completion — there is no "later"). ENABLE without FORCE, deliberately: svc_migration
-- owns the tables and owner-bypass is what lets the runner transform without blanket migration
-- policies; the control is that svc_migration's credential exists only in the runner's deploy
-- context (F.4 gate asserts runtime env carries exactly svc_ingress and svc_worker).
ALTER TABLE users                   ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_identities         ENABLE ROW LEVEL SECURITY;
ALTER TABLE onboarding_sessions     ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspaces              ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace_members       ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace_invitations   ENABLE ROW LEVEL SECURITY;
ALTER TABLE channel_bindings        ENABLE ROW LEVEL SECURITY;
ALTER TABLE ig_accounts             ENABLE ROW LEVEL SECURITY;
ALTER TABLE provider_quarantine     ENABLE ROW LEVEL SECURITY;
ALTER TABLE media_sources           ENABLE ROW LEVEL SECURITY;
ALTER TABLE oauth_credentials       ENABLE ROW LEVEL SECURITY;
ALTER TABLE media_items             ENABLE ROW LEVEL SECURITY;
ALTER TABLE post_locks              ENABLE ROW LEVEL SECURITY;
ALTER TABLE category_post_case_mix  ENABLE ROW LEVEL SECURITY;
ALTER TABLE post_intents            ENABLE ROW LEVEL SECURITY;
ALTER TABLE post_intent_transitions ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_events            ENABLE ROW LEVEL SECURITY;
ALTER TABLE daily_post_counts       ENABLE ROW LEVEL SECURITY;
ALTER TABLE jobs                    ENABLE ROW LEVEL SECURITY;
ALTER TABLE channel_outbox          ENABLE ROW LEVEL SECURITY;
ALTER TABLE provider_operations     ENABLE ROW LEVEL SECURITY;
ALTER TABLE command_dedup           ENABLE ROW LEVEL SECURITY;
ALTER TABLE rate_counters           ENABLE ROW LEVEL SECURITY;

-- POLICIES, by class. The tenant predicate is NULL-safe by construction: an unset GUC yields
-- NULL, NULL matches no row, and "absent context reads nothing" is exactly the F.4 gate.

-- Class 1 — tenant-plane, the standard pair. workspaces keys on id (it IS the tenant); signup
-- pre-assigns the new id and sets the GUC before INSERT (the UoW factory requires tenant_id).
CREATE POLICY p_tenant_workspaces ON workspaces FOR ALL TO svc_ingress, svc_worker
  USING      (id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
CREATE POLICY p_tenant ON workspace_members FOR ALL TO svc_ingress, svc_worker
  USING      (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
-- …the same two-line policy repeats, verbatim except the table name, on: workspace_invitations,
-- channel_bindings, ig_accounts, provider_quarantine, media_sources, oauth_credentials,
-- media_items, post_locks, category_post_case_mix, post_intents, daily_post_counts,
-- channel_outbox, provider_operations. The migration file spells all thirteen out; this document
-- states the pattern once and the replay fixture generates them from this list, so the plan text
-- cannot silently diverge from the file (the list IS the normative enumeration).

-- Class 2 — jobs: tenant rows in tenant context, system rows (workspace_id IS NULL) visible to
-- both logins — workers execute system kinds, and ingress produces the payload-complete
-- send_email system kind (§5 classing rule).
CREATE POLICY p_jobs ON jobs FOR ALL TO svc_ingress, svc_worker
  USING      (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
              OR workspace_id IS NULL)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
              OR workspace_id IS NULL);

-- Class 3 — user-plane (users, user_identities, onboarding_sessions): no workspace key exists —
-- identity precedes tenancy (sign-in, identity upsert, onboarding all run before any
-- app.tenant_id can be set). Role-scoped row-open policies; the isolation authority for these
-- tables is the one central authorization gate (01 §Process roles), stated as a deliberate class.
CREATE POLICY p_user_plane ON users FOR ALL TO svc_ingress, svc_worker
  USING (true) WITH CHECK (true);
-- …repeated on user_identities and onboarding_sessions (same normative-list rule as class 1).

-- Class 4 — machinery counters and admission dedup.
CREATE POLICY p_rate ON rate_counters FOR ALL TO svc_ingress, svc_worker
  USING (true) WITH CHECK (true);
CREATE POLICY p_dedup ON command_dedup FOR ALL TO svc_ingress
  USING (true) WITH CHECK (true);

-- Class 5 — reference data: readable by everyone, writable by migrations only (owner bypass).
CREATE POLICY p_transitions_read ON post_intent_transitions FOR SELECT
  TO svc_ingress, svc_worker, svc_claim, svc_clock, svc_maintenance, svc_membership
  USING (true);

-- Class 6 — audit: tenant-scoped INSERT/SELECT for logins; row-open INSERT for door owners
-- (their bodies' triggers insert as the owner); retention's SELECT/DELETE for the sweep owner.
CREATE POLICY p_audit_ins ON audit_events FOR INSERT TO svc_ingress, svc_worker
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
CREATE POLICY p_audit_sel ON audit_events FOR SELECT TO svc_ingress, svc_worker
  USING (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
CREATE POLICY p_audit_sys ON audit_events FOR INSERT
  TO svc_claim, svc_clock, svc_maintenance, svc_membership WITH CHECK (true);
CREATE POLICY p_audit_retention_sel ON audit_events FOR SELECT TO svc_maintenance USING (true);
CREATE POLICY p_audit_retention_del ON audit_events FOR DELETE TO svc_maintenance USING (true);

-- Class 7 — door-owner enumerated policies (each backs one body below; the complete list, per
-- §7's rule that every cross-tenant capability is a named reviewable object):
CREATE POLICY p_claim_jobs   ON jobs FOR ALL TO svc_claim USING (true) WITH CHECK (true);
CREATE POLICY p_claim_quar   ON provider_quarantine FOR SELECT TO svc_claim USING (true);
CREATE POLICY p_clock_ws     ON workspaces      FOR SELECT TO svc_clock USING (true);
CREATE POLICY p_clock_acct_s ON ig_accounts     FOR SELECT TO svc_clock USING (true);
CREATE POLICY p_clock_acct_u ON ig_accounts     FOR UPDATE TO svc_clock USING (true) WITH CHECK (true);
CREATE POLICY p_clock_src_s  ON media_sources   FOR SELECT TO svc_clock USING (true);
CREATE POLICY p_clock_src_u  ON media_sources   FOR UPDATE TO svc_clock USING (true) WITH CHECK (true);
CREATE POLICY p_clock_cred_s ON oauth_credentials FOR SELECT TO svc_clock USING (true);
CREATE POLICY p_clock_cred_u ON oauth_credentials FOR UPDATE TO svc_clock USING (true) WITH CHECK (true);
CREATE POLICY p_clock_jobs   ON jobs FOR ALL TO svc_clock USING (true) WITH CHECK (true);
CREATE POLICY p_maint_intents ON post_intents FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);
CREATE POLICY p_maint_locks   ON post_locks   FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);
CREATE POLICY p_maint_invites ON workspace_invitations FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);
CREATE POLICY p_maint_onboard ON onboarding_sessions   FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);
CREATE POLICY p_maint_jobs    ON jobs FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);
CREATE POLICY p_maint_outbox  ON channel_outbox      FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);
CREATE POLICY p_maint_ops     ON provider_operations FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);
CREATE POLICY p_maint_dpc     ON daily_post_counts   FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);
CREATE POLICY p_maint_rate    ON rate_counters       FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);
CREATE POLICY p_maint_ws      ON workspaces FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);
CREATE POLICY p_member_invites ON workspace_invitations FOR ALL TO svc_membership USING (true) WITH CHECK (true);
CREATE POLICY p_member_members ON workspace_members FOR ALL TO svc_membership USING (true) WITH CHECK (true);
CREATE POLICY p_member_binds   ON channel_bindings FOR SELECT TO svc_membership USING (true);
CREATE POLICY p_member_outbox  ON channel_outbox FOR INSERT TO svc_membership WITH CHECK (true);
CREATE POLICY p_member_ws      ON workspaces FOR SELECT TO svc_membership USING (true);
-- (command_dedup, session_tokens, oauth_states: the auth-plane policies for svc_ingress and the
-- sweep owner are printed with those tables — command_dedup above, the 07 pair in 07.)
CREATE POLICY p_maint_dedup ON command_dedup FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);
```

```sql
-- TRANSIENT, BRACKETED (pass 7 — R6's P0, empirically forced): ALTER FUNCTION … OWNER TO
-- requires the executor to be a member of the new owning role (bootstrap grants svc_migration
-- those memberships for the window, 04) AND the new owner to hold CREATE on the schema —
-- membership alone fails with "permission denied for schema public". Granted here by the
-- schema's owner, revoked as this section's last statement: the steady-state grant matrix
-- never carries CREATE for a door owner, and the stream is self-cleaning either way.
GRANT CREATE ON SCHEMA public TO svc_claim, svc_clock, svc_maintenance, svc_membership;

-- 1/9 fn_claim_job — the §5 claim, verbatim, as the function it always claimed to be.
CREATE FUNCTION fn_claim_job(p_lane text, p_worker text, p_lease interval, p_ws_lane_cap int)
RETURNS SETOF jobs LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  RETURN QUERY
  WITH candidate AS (
    SELECT j.id FROM jobs j
    WHERE j.state = 'ready' AND j.lane = p_lane AND j.run_at <= now()
      AND NOT EXISTS (SELECT 1 FROM jobs h
                      WHERE h.serialization_key = j.serialization_key AND h.state = 'leased')
      AND NOT EXISTS (SELECT 1 FROM provider_quarantine q
                      WHERE q.workspace_id = j.workspace_id
                        AND q.scope_ref = j.serialization_key
                        AND q.quarantined_until > now())
      AND (j.workspace_id IS NULL OR
           (SELECT count(*) FROM jobs w
            WHERE w.state = 'leased' AND w.lane = j.lane
              AND w.workspace_id = j.workspace_id) < p_ws_lane_cap)
    ORDER BY j.run_at
    LIMIT 1 FOR UPDATE OF j SKIP LOCKED
  )
  UPDATE jobs SET state = 'leased', locked_by = p_worker, lease_token = gen_random_uuid(),
                  locked_until = now() + p_lease, attempts = attempts + 1
    FROM candidate WHERE jobs.id = candidate.id
  RETURNING jobs.*;
END $$;
ALTER FUNCTION fn_claim_job(text, text, interval, int) OWNER TO svc_claim;
REVOKE ALL ON FUNCTION fn_claim_job(text, text, interval, int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_claim_job(text, text, interval, int) TO svc_worker;

-- 2/9 fn_extend_leases — the heartbeat. Extends only live leases matching the caller's tokens;
-- an expired or reassigned lease is silently not extended (the count return lets the worker see it).
CREATE FUNCTION fn_extend_leases(p_tokens uuid[], p_lease interval)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE n int;
BEGIN
  UPDATE jobs SET locked_until = now() + p_lease
   WHERE lease_token = ANY (p_tokens) AND state = 'leased' AND locked_until > now();
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;
ALTER FUNCTION fn_extend_leases(uuid[], interval) OWNER TO svc_claim;
REVOKE ALL ON FUNCTION fn_extend_leases(uuid[], interval) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_extend_leases(uuid[], interval) TO svc_worker;

-- Slot arithmetic (the tick's helper; v1 policy = uniform spacing inside the account-local
-- posting window, the legacy create-schedule semantics carried forward; start = end ⇒ 24 h window,
-- start > end ⇒ the window wraps midnight, both current semantics).
-- REWRITTEN AT PASS 6 (R5: 7,344 of the 28,800 legal (start,end,ppd) combinations produced an
-- extra boundary slot — reproduced exactly). The defect class: accumulate a fractional interval,
-- then test the boundary on hour+minute — microsecond rounding lands 03:59:59.999998 "inside" a
-- window that ends 04:00. The rewrite makes the per-cycle count structural: slots are computed
-- BY INDEX on the cycle grid (slot i = cycle_start + i·window/ppd, i ∈ [0, ppd)), so a cycle
-- holds exactly ppd slots by integer arithmetic and no time-precision boundary test exists to
-- get wrong; rounding cannot accumulate because every slot derives from cycle_start, not from
-- its predecessor. round() (not floor) recovers the index: a slot re-read from µs-rounded
-- storage sits within ±1 µs of its grid point and nearest-index snapping absorbs both
-- directions — floor would step a µs-early value back one slot and re-mint it immediately,
-- 1 µs apart, which uq_intent_slot (exact-timestamp key) would NOT dedup.
-- STABLE, not IMMUTABLE: the result depends on the tz database via AT TIME ZONE.
CREATE FUNCTION fn_next_slot(p_after timestamptz, p_tz text,
                             p_start_h int, p_end_h int, p_ppd int)
RETURNS timestamptz LANGUAGE plpgsql STABLE AS $$
DECLARE
  tz         text      := fn_safe_tz(p_tz);     -- §0 tz rule: a decayed stored zone degrades
                                                -- this row to UTC, never aborts the caller's
                                                -- set-based tick statement (R5 regression note)
  win_hours  numeric   := CASE WHEN p_end_h = p_start_h THEN 24
                               ELSE ((p_end_h - p_start_h + 24) % 24) END;
  aft_local  timestamp := p_after AT TIME ZONE tz;
  anchor     timestamp := date_trunc('day', aft_local) + p_start_h * interval '1 hour';
  cyc_start  timestamp;                         -- latest window start at or before p_after
  idx        int;
BEGIN
  cyc_start := CASE WHEN aft_local >= anchor THEN anchor ELSE anchor - interval '1 day' END;
  idx := round(EXTRACT(epoch FROM (aft_local - cyc_start)) * p_ppd / (win_hours * 3600.0));
  IF idx + 1 >= p_ppd THEN
    RETURN (cyc_start + interval '1 day') AT TIME ZONE tz;        -- next cycle's first slot
  END IF;
  RETURN (cyc_start + ((idx + 1) * win_hours / p_ppd) * interval '1 hour') AT TIME ZONE tz;
END $$;

-- 3/9 fn_clock_tick — one indexed pass per concern; every heavy step becomes a job. The tick
-- INSERTS plan_slot jobs (never intents — §5 registry, pass 5), schedules refreshes off the
-- payload-free column grant, schedules due syncs, and keeps the recurring system singletons alive.
-- BUDGET IS STRUCTURAL (pass 6 — R5: four independent LIMIT p_max legs permitted 3×p_max +
-- recurring inserts against 05's "≤ 500 inserts/tick" promise): p_max is the tick's TOTAL
-- insert budget, enforced per the §7 door bound rule — one running remainder, every leg
-- LIMITed by it, so the sum cannot exceed p_max under any configuration. Priority order —
-- recurring singletons (they keep the reconciler/reapers/retention alive and are bounded by
-- the registry's kind count), then plan_slot (the product's visible output), then credential
-- refreshes, then baseline syncs (both deferrable-by-ticks at their 05 cadences). A starved
-- class drains on later ticks (the 05 tick cadence). This comment is the priority order's one
-- home; the 05 rows cite it.
CREATE FUNCTION fn_clock_tick(p_max int, p_refresh_cadence interval,
                              p_recurring jsonb)  -- {v:1, "<kind>": seconds, …} (05 seam)
RETURNS TABLE (o_slot_jobs int, o_refresh_jobs int, o_sync_jobs int, o_recurring_jobs int)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE k text; cadence interval; last_done timestamptz; rem int;
        n1 int := 0; n2 int := 0; n3 int := 0; n4 int := 0;
BEGIN
  PERFORM set_config('app.actor_kind', 'clock', true);
  -- (1) recurring system singletons: if no ready/leased row holds the kind's singleton key,
  -- insert the next run at last-completion + cadence (or now, whichever is later):
  FOR k, cadence IN
    SELECT key, (value::text)::numeric * interval '1 second'
      FROM jsonb_each(p_recurring) WHERE key <> 'v'
  LOOP
    EXIT WHEN n4 >= p_max;
    IF NOT EXISTS (SELECT 1 FROM jobs
                   WHERE kind = k AND state IN ('ready','leased')) THEN
      SELECT max(updated_at) INTO last_done FROM jobs
       WHERE kind = k AND state = 'succeeded';
      INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, payload)
      VALUES (k, NULL, 'bulk', k,            -- system singletons key on their kind (§5 registry)
              GREATEST(now(), COALESCE(last_done + cadence, now())), 3,
              jsonb_build_object('v', 1));
      n4 := n4 + 1;
    END IF;
  END LOOP;
  rem := GREATEST(p_max - n4, 0);            -- the running remainder every later leg draws on;
                                             -- each leg's LIMIT keeps its count ≤ rem, so the
                                             -- plain subtractions below cannot go negative
  -- (2) due accounts → plan_slot jobs + slot-cursor advance, one set-based statement
  -- (the O(due) scan, H3; ix_ig_accounts_due serves it):
  WITH due AS (
    SELECT a.id, a.workspace_id, a.next_slot_at,
           COALESCE(a.tz, w.tz)                                   AS eff_tz,
           COALESCE(a.posts_per_day, w.posts_per_day)             AS eff_ppd,
           COALESCE(a.posting_hours_start, w.posting_hours_start) AS eff_start,
           COALESCE(a.posting_hours_end, w.posting_hours_end)     AS eff_end
      FROM ig_accounts a JOIN workspaces w ON w.id = a.workspace_id
     WHERE a.state = 'active' AND a.next_slot_at IS NOT NULL AND a.next_slot_at <= now()
       AND w.state = 'active' AND NOT w.is_paused
     ORDER BY a.next_slot_at LIMIT rem
  ), ins AS (
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, payload)
    SELECT 'plan_slot', d.workspace_id, 'bulk', 'acct:' || d.id, now(), 3,
           jsonb_build_object('v', 1, 'ig_account_id', d.id, 'slot_at', d.next_slot_at)
      FROM due d
  )
  UPDATE ig_accounts a
     SET next_slot_at = fn_next_slot(d.next_slot_at, d.eff_tz, d.eff_start, d.eff_end, d.eff_ppd)
    FROM due d WHERE a.id = d.id;
  GET DIAGNOSTICS n1 = ROW_COUNT;
  rem := rem - n1;
  -- (3) due credential refreshes — one set-based statement (D31: the scheduled refresh is also
  -- the liveness probe; the cadence is decoupled from expiry proximity). Reads ride svc_clock's
  -- payload-free column grant; ix_credentials_refresh_due serves the scan:
  WITH due AS (
    SELECT id, workspace_id FROM oauth_credentials
     WHERE state = 'active' AND next_refresh_at IS NOT NULL AND next_refresh_at <= now()
     LIMIT rem
  ), ins AS (
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, payload)
    SELECT 'refresh_credential', d.workspace_id, 'bulk', 'cred:' || d.id, now(), 5,
           jsonb_build_object('v', 1, 'credential_id', d.id)
      FROM due d
  )
  UPDATE oauth_credentials c SET next_refresh_at = now() + p_refresh_cadence
    FROM due d WHERE c.id = d.id;
  GET DIAGNOSTICS n2 = ROW_COUNT;
  rem := rem - n2;
  -- (4) due source syncs — same shape (H4's slow jittered baseline; pre-slot/demand syncs are
  -- produced by their own sites — the tick owns only the baseline). ix_sources_sync_due serves it:
  WITH due AS (
    SELECT id, workspace_id FROM media_sources
     WHERE state = 'active' AND next_sync_at IS NOT NULL AND next_sync_at <= now()
     LIMIT rem
  ), ins AS (
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, payload)
    SELECT 'sync_media_source', d.workspace_id, 'bulk', 'src:' || d.id, now(), 5,
           jsonb_build_object('v', 1, 'source_id', d.id, 'reason', 'baseline')
      FROM due d
  )
  UPDATE media_sources s SET next_sync_at = NULL                   -- the sync executor re-arms it
    FROM due d WHERE s.id = d.id;
  GET DIAGNOSTICS n3 = ROW_COUNT;
  RETURN QUERY SELECT n1, n2, n3, n4;
END $$;
ALTER FUNCTION fn_clock_tick(int, interval, jsonb) OWNER TO svc_clock;
REVOKE ALL ON FUNCTION fn_clock_tick(int, interval, jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_clock_tick(int, interval, jsonb) TO svc_worker;

-- 4/9 fn_reconciler_sweep — the cross-tenant READ half of reconciliation (§6): returns the
-- ladder-due ambiguous intents plus the parked review_required rows whose customer-notification
-- window has passed; every verdict/notification WRITE then runs tenant-scoped as svc_worker.
-- BUDGET IS STRUCTURAL (pass 6 — R5: LIMIT p_lim on each UNION branch permitted 2×p_lim rows
-- against 05's "LIMIT 50" promise): p_lim bounds the whole sweep; ladder-due rows take
-- priority — an unresolved ambiguity blocks its real account's next publish via
-- uq_publish_exclusive, while a notify-window row is informational — and notify-window rows
-- fill only the remaining budget.
CREATE FUNCTION fn_reconciler_sweep(p_lim int, p_rungs interval[], p_notify_after interval)
RETURNS TABLE (o_intent_id uuid, o_workspace_id uuid, o_reason text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  RETURN QUERY
  WITH ladder AS (
    SELECT i.id, i.workspace_id, 'ladder_due'::text AS reason
      FROM post_intents i
     WHERE i.state = 'publishing_ambiguous'
       AND COALESCE(
             (i.last_error->'evidence'->>'last_checked_at')::timestamptz
               + p_rungs[LEAST(COALESCE((i.last_error->'evidence'->>'checks')::int, 0) + 1,
                               array_length(p_rungs, 1))],
             i.entered_state_at) <= now()
     ORDER BY i.entered_state_at LIMIT p_lim
  ), notify AS (
    SELECT i.id, i.workspace_id, 'notify_window'::text AS reason
      FROM post_intents i
     WHERE i.state = 'review_required'
       AND i.entered_state_at + p_notify_after <= now()
       AND NOT COALESCE((i.last_error->'evidence'->>'customer_notified')::boolean, false)
     ORDER BY i.entered_state_at
     LIMIT GREATEST(p_lim - (SELECT count(*) FROM ladder), 0)
  )
  SELECT * FROM ladder UNION ALL SELECT * FROM notify;
END $$;
ALTER FUNCTION fn_reconciler_sweep(int, interval[], interval) OWNER TO svc_maintenance;
REVOKE ALL ON FUNCTION fn_reconciler_sweep(int, interval[], interval) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_reconciler_sweep(int, interval[], interval) TO svc_worker;

-- 5/9 fn_reaper_sweep — the tenant-table expiry classes, one bounded pass per class. Intent
-- expiry TTLs: slot lapse for scheduled/prompt_pending (the slot passed unclaimed/undelivered),
-- the workspace's approval TTL (else p_approval_ttl) for awaiting_approval, p_approved_ttl for
-- stale approvals. Every intent flip fires the §4 guard + audit triggers as this owner.
-- BUDGET IS STRUCTURAL (pass 7 — R6: seven independent LIMIT p_lim legs permitted 7×p_lim
-- against the §7 bound rule's TOTAL default): p_lim is the sweep's TOTAL row budget — one
-- running remainder, every leg's LIMIT draws it down, sum cannot exceed p_lim (the `05` row).
-- Priority order is liveness-first: the jobs expired-lease re-ready leg runs FIRST — a crashed
-- worker's leased jobs are invisible to claiming until re-readied, so this leg gates all lane
-- throughput; every other leg is expiry bookkeeping whose latency only lengthens a TTL. Then
-- intent slot-lapse (a lapsed slot must not fire late), the two intent age TTLs, then the
-- small-table classes. A leg starved at p_lim carries to the next sweep on the recurring
-- reap_expired schedule — residue is latency, never loss.
CREATE FUNCTION fn_reaper_sweep(p_lim int, p_approval_ttl interval, p_approved_ttl interval)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE n int := 0; c int; rem int := GREATEST(p_lim, 0);
BEGIN
  PERFORM set_config('app.actor_kind', 'reaper', true);
  UPDATE jobs SET state = 'ready', locked_by = NULL, lease_token = NULL, locked_until = NULL
   WHERE id IN (SELECT id FROM jobs
                 WHERE state = 'leased' AND locked_until < now() LIMIT rem);
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;   -- ix_jobs_lease_expiry
  -- Intent expiry legs ride the §3 partial reap indexes: each scan touches only live working
  -- rows (small at any instant), never the kept-forever terminal ledger. Fixed-TTL predicates
  -- are written sargable (col < now() - ttl); the per-workspace-TTL leg's bound varies per row,
  -- so its cost bound is the partial index itself.
  UPDATE post_intents SET state = 'expired'
   WHERE id IN (SELECT id FROM post_intents
                 WHERE state IN ('scheduled','prompt_pending') AND schedule_slot_at < now()
                 LIMIT rem);                                    -- ix_intents_reap_slot
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  UPDATE post_intents i SET state = 'expired'
   WHERE i.id IN (
     SELECT i2.id FROM post_intents i2 JOIN workspaces w ON w.id = i2.workspace_id
      WHERE i2.state = 'awaiting_approval'
        AND i2.entered_state_at
            < now() - COALESCE(w.approval_ttl_minutes * interval '1 minute', p_approval_ttl)
      LIMIT rem);                                               -- ix_intents_reap_age
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  UPDATE post_intents SET state = 'expired'
   WHERE id IN (SELECT id FROM post_intents
                 WHERE state = 'approved' AND entered_state_at < now() - p_approved_ttl
                 LIMIT rem);                                    -- ix_intents_reap_age
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  DELETE FROM post_locks
   WHERE id IN (SELECT id FROM post_locks
                 WHERE expires_at IS NOT NULL AND expires_at < now() LIMIT rem);
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  UPDATE workspace_invitations SET state = 'expired'
   WHERE id IN (SELECT id FROM workspace_invitations
                 WHERE state = 'pending' AND expires_at < now() LIMIT rem);
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  DELETE FROM onboarding_sessions
   WHERE id IN (SELECT id FROM onboarding_sessions WHERE expires_at < now() LIMIT rem);
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c;
  RETURN n;
END $$;
ALTER FUNCTION fn_reaper_sweep(int, interval, interval) OWNER TO svc_maintenance;
REVOKE ALL ON FUNCTION fn_reaper_sweep(int, interval, interval) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_reaper_sweep(int, interval, interval) TO svc_worker;

-- 6/9 fn_retention_batch — one 05 retention class per call; the audit class exports before it
-- deletes and ABORTS if the export fails (raise propagates, transaction rolls back — 07 §4).
-- Auth-plane classes are NOT here — they sweep through fn_auth_plane_sweep on the same schedule.
CREATE FUNCTION fn_retention_batch(p_class text, p_keep interval, p_batch int)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE n int := 0; tbl text;
BEGIN
  PERFORM set_config('app.actor_kind', 'reaper', true);
  IF p_class = 'audit_events' THEN
    -- clock_timestamp(), never now(): now() is transaction-stable, so two batches in one
    -- transaction would mint the same name and collide (R5). Microsecond suffix keeps
    -- same-second batches distinct; the archive_audit age test reads only the first 8 digits.
    tbl := 'audit_export_' || to_char(clock_timestamp(), 'YYYYMMDD_HH24MISS_US');
    EXECUTE format(
      'CREATE TABLE archive.%I AS SELECT * FROM audit_events
        WHERE created_at < now() - %L::interval ORDER BY id LIMIT %s',
      tbl, p_keep, p_batch);                    -- export-or-abort: failure raises out
    EXECUTE format(
      'DELETE FROM audit_events WHERE id IN (SELECT id FROM archive.%I)', tbl);
    GET DIAGNOSTICS n = ROW_COUNT;
  ELSIF p_class = 'jobs_ok' THEN
    DELETE FROM jobs WHERE id IN (SELECT id FROM jobs
      WHERE state IN ('succeeded','cancelled') AND updated_at < now() - p_keep LIMIT p_batch);
    GET DIAGNOSTICS n = ROW_COUNT;
  ELSIF p_class = 'jobs_bad' THEN
    DELETE FROM jobs WHERE id IN (SELECT id FROM jobs
      WHERE state IN ('failed','review_required') AND updated_at < now() - p_keep LIMIT p_batch);
    GET DIAGNOSTICS n = ROW_COUNT;
  ELSIF p_class = 'provider_operations' THEN
    DELETE FROM provider_operations WHERE id IN (SELECT id FROM provider_operations
      WHERE state IN ('succeeded','failed') AND updated_at < now() - p_keep LIMIT p_batch);
    GET DIAGNOSTICS n = ROW_COUNT;
  ELSIF p_class = 'channel_outbox' THEN
    DELETE FROM channel_outbox WHERE id IN (SELECT id FROM channel_outbox
      WHERE state IN ('sent','superseded','failed','ambiguous')
        AND updated_at < now() - p_keep LIMIT p_batch);
    GET DIAGNOSTICS n = ROW_COUNT;
  ELSIF p_class = 'daily_post_counts' THEN
    DELETE FROM daily_post_counts WHERE (workspace_id, ig_account_id, local_date) IN (
      SELECT workspace_id, ig_account_id, local_date FROM daily_post_counts
       WHERE local_date < (now() - p_keep)::date LIMIT p_batch);
    GET DIAGNOSTICS n = ROW_COUNT;
  ELSIF p_class = 'rate_counters' THEN
    DELETE FROM rate_counters WHERE (scope, key, window_start) IN (
      SELECT scope, key, window_start FROM rate_counters
       WHERE window_start < now() - p_keep LIMIT p_batch);
    GET DIAGNOSTICS n = ROW_COUNT;
  ELSIF p_class IN ('archive_audit', 'archive_snapshots') THEN
    -- Every archive table's name encodes its date (audit_export_<ts>; <table>_pre_cutover_<ymd>
    -- — the 04 snapshot naming rule exists exactly so this one mechanism covers both families),
    -- so age is a name comparison and no second drop mechanism is needed. p_batch bounds the
    -- drops per call like every other class (pass 7 — R6: the loop ignored it), oldest first
    -- so a starved backlog converges from the aged end; relname tiebreak keeps order total.
    FOR tbl IN SELECT c.relname FROM pg_class c JOIN pg_namespace ns ON ns.oid = c.relnamespace
                WHERE ns.nspname = 'archive' AND c.relkind = 'r'
                  AND c.relname LIKE CASE p_class WHEN 'archive_audit' THEN 'audit\_export\_%'
                                                  ELSE '%\_pre\_cutover\_%' END
                  AND substring(c.relname FROM '[0-9]{8}')
                      < to_char(now() - p_keep, 'YYYYMMDD')
                ORDER BY substring(c.relname FROM '[0-9]{8}'), c.relname
                LIMIT p_batch
    LOOP
      EXECUTE format('DROP TABLE archive.%I', tbl); n := n + 1;
    END LOOP;
  ELSE
    RAISE EXCEPTION 'unknown retention class %', p_class;
  END IF;
  RETURN n;
END $$;
ALTER FUNCTION fn_retention_batch(text, interval, int) OWNER TO svc_maintenance;
REVOKE ALL ON FUNCTION fn_retention_batch(text, interval, int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_retention_batch(text, interval, int) TO svc_worker;

-- 7/9 fn_offboard_finalize — the final-deletion leg of 06 §1, guards INSIDE the body: the
-- workspace is offboarding, the grace window (anchored on workspaces.offboarding_at) has
-- elapsed, and zero live intents remain. The §0 cascade does the rest; audit_events survives.
CREATE FUNCTION fn_offboard_finalize(p_ws uuid, p_grace interval)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  PERFORM set_config('app.actor_kind', 'system', true);
  IF NOT EXISTS (SELECT 1 FROM workspaces
                  WHERE id = p_ws AND state = 'offboarding'
                    AND offboarding_at IS NOT NULL AND offboarding_at + p_grace <= now()) THEN
    RAISE EXCEPTION 'workspace % is not finalizable (state/grace guard)', p_ws;
  END IF;
  IF EXISTS (SELECT 1 FROM post_intents WHERE workspace_id = p_ws
              AND state NOT IN ('posted','skipped','rejected','expired','failed','cancelled')) THEN
    RAISE EXCEPTION 'workspace % still holds live intents', p_ws;
  END IF;
  DELETE FROM workspaces WHERE id = p_ws;
END $$;
ALTER FUNCTION fn_offboard_finalize(uuid, interval) OWNER TO svc_maintenance;
REVOKE ALL ON FUNCTION fn_offboard_finalize(uuid, interval) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_offboard_finalize(uuid, interval) TO svc_worker;

-- 8/9 fn_auth_plane_sweep — expiry/retention deletes on the auth-plane trio. svc_maintenance
-- carries exactly the enumerated auth-plane policies this body needs (07 prints the two for its
-- tables; command_dedup's is above) — the R3 §6.7 resolution, kept.
CREATE FUNCTION fn_auth_plane_sweep(p_state_slack interval, p_session_keep interval,
                                    p_dedup_keep interval, p_batch int)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE n int := 0; c int;
BEGIN
  DELETE FROM oauth_states WHERE state IN (
    SELECT state FROM oauth_states
     WHERE (consumed_at IS NOT NULL AND consumed_at < now() - p_state_slack)
        OR expires_at < now() - p_state_slack
     LIMIT p_batch);
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c;
  DELETE FROM session_tokens WHERE id IN (
    SELECT id FROM session_tokens
     WHERE (revoked_at IS NOT NULL AND revoked_at < now() - p_session_keep)
        OR expires_at < now() - p_session_keep
     LIMIT p_batch);
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c;
  DELETE FROM command_dedup WHERE (channel, principal, external_ref) IN (
    SELECT channel, principal, external_ref FROM command_dedup
     WHERE created_at < now() - p_dedup_keep LIMIT p_batch);
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c;
  RETURN n;
END $$;
ALTER FUNCTION fn_auth_plane_sweep(interval, interval, interval, int) OWNER TO svc_maintenance;
REVOKE ALL ON FUNCTION fn_auth_plane_sweep(interval, interval, interval, int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_auth_plane_sweep(interval, interval, interval, int) TO svc_worker;

-- 9/9 fn_invitation_accept — the pre-membership door (pass 5, R4 finding 7: the acceptor is
-- authenticated but not yet a member; tenant RLS structurally cannot resolve token → workspace
-- for them, and the central membership gate would refuse them). The D33 match fact is computed
-- HERE, from raw inputs — google: the verified id_token email; telegram: the tapping identity's
-- immutable numeric id vs invited_tg_user_id (never the hint column) — so the authorization
-- fact is computed where it is consumed. Caller (ingress) rate-limits via the preauth_ip scope.
CREATE FUNCTION fn_invitation_accept(p_token_hash text, p_user uuid, p_provider text,
                                     p_verified_email text, p_tg_user_id bigint, p_channel text)
RETURNS TABLE (o_workspace_id uuid, o_granted_role text, o_matched boolean)
-- o_ outputs per the §7 output-name rule (R5's reproduction was this door's ON CONFLICT list)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE inv record; m boolean; grant_role text; bind uuid;
BEGIN
  PERFORM set_config('app.actor_kind', 'user', true);
  PERFORM set_config('app.actor_user_id', p_user::text, true);
  PERFORM set_config('app.channel', COALESCE(p_channel, 'web'), true);
  SELECT * INTO inv FROM workspace_invitations i
   WHERE i.token_hash = p_token_hash AND i.state = 'pending' AND i.expires_at > now()
   FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'invitation not acceptable (used, revoked, expired, or unknown)'
      USING ERRCODE = 'no_data_found';
  END IF;
  -- D33 per-provider acceptance constraint, evaluated in-body:
  IF p_provider = 'google' THEN
    IF inv.email IS NOT NULL THEN
      IF lower(p_verified_email) IS DISTINCT FROM lower(inv.email) THEN
        RAISE EXCEPTION 'identity proof mismatch' USING ERRCODE = 'check_violation';
      END IF;
      m := true;
    ELSE m := false;                             -- no comparable proof: recorded skip
    END IF;
  ELSIF p_provider = 'telegram' THEN
    IF inv.invited_tg_user_id IS NOT NULL THEN
      IF p_tg_user_id IS DISTINCT FROM inv.invited_tg_user_id THEN
        RAISE EXCEPTION 'identity proof mismatch' USING ERRCODE = 'check_violation';
      END IF;
      m := true;
    ELSE m := false;                             -- hint-only or bare token: recorded skip (D36)
    END IF;
  ELSE
    RAISE EXCEPTION 'unknown acceptance provider %', p_provider;
  END IF;
  UPDATE workspace_invitations
     SET state = 'accepted', accepted_by_user_id = p_user, accepted_email_matched = m
   WHERE id = inv.id;
  grant_role := CASE WHEN inv.role = 'admin' AND m THEN 'admin' ELSE 'member' END;
  INSERT INTO workspace_members (workspace_id, user_id, role, added_by_user_id)
  VALUES (inv.workspace_id, p_user, grant_role, inv.invited_by_user_id)
  ON CONFLICT (workspace_id, user_id) DO NOTHING;  -- already a member: invite consumed, the
                                                    -- existing role stands (role changes go
                                                    -- through the 06 §2 gate)
  IF inv.role = 'admin' AND NOT m THEN             -- D36 elevation-pending, same transaction
    SELECT b.id INTO bind FROM channel_bindings b
     WHERE b.workspace_id = inv.workspace_id AND b.state = 'active'
     ORDER BY b.created_at LIMIT 1;
    IF bind IS NOT NULL THEN
      INSERT INTO channel_outbox (workspace_id, binding_id, kind, payload)
      VALUES (inv.workspace_id, bind, 'notification',
              jsonb_build_object('v', 1, 'template', 'elevation_pending',
                                 'invitation_id', inv.id, 'accepted_by', p_user));
    END IF;                                        -- zero-binding workspace: the pending
  END IF;                                          -- elevation is visible on the web surface
  RETURN QUERY SELECT inv.workspace_id, grant_role, m;
END $$;
ALTER FUNCTION fn_invitation_accept(text, uuid, text, text, bigint, text) OWNER TO svc_membership;
REVOKE ALL ON FUNCTION fn_invitation_accept(text, uuid, text, text, bigint, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_invitation_accept(text, uuid, text, text, bigint, text) TO svc_ingress;

-- Close the transient bracket opened before door 1/9: every ownership transfer above is done,
-- and no door owner keeps CREATE. The replay gate asserts this end state (04 0.2).
REVOKE CREATE ON SCHEMA public FROM svc_claim, svc_clock, svc_maintenance, svc_membership;
```
