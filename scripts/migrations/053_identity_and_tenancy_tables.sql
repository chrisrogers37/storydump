-- Migration: 053_identity_and_tenancy_tables.sql
-- Description: F.2.2 — plan 02 §1's identity and tenancy tables, the first
--   tables of the target schema. Seven of them: users, user_identities,
--   workspaces, workspace_members, workspace_invitations, channel_bindings,
--   onboarding_sessions, with their touch triggers, the two owner-invariant
--   constraint triggers, and three partial/plain unique indexes.
--
--   A CONTIGUOUS PREFIX OF THE ADVERTISED STREAM, and that is the whole
--   contract this file is under. `04` §0.2 arm (b) diffs the concatenated
--   target lineage against the expanded 02+07 stream as an ORDERED PREFIX, so
--   this file is statements 2..21 of that stream in that order, and 052's two
--   shared functions are statements 0..1. Nothing here is authored — the body
--   is the plan's own SQL. Edit the plan and the manifest ratchet, never this
--   file alone.
--
--   NO POLICIES, AND NO RLS — deliberately, per the #806 Fork 1 ruling (a).
--   The advertised stream creates all 26 tables before the first ENABLE ROW
--   LEVEL SECURITY (stream index 126) or CREATE POLICY (149), so a table
--   increment that carried its own policy would NOT be a stream prefix and
--   would fail arm (b) at the first policy statement. The safety property that
--   grouping used to carry is now carried by detection instead:
--   `scripts/tenancy_gate.py` is pointed at this lineage by
--   `tests/scripts/test_lineage_lane.py`, and it compares the replayed schema
--   against the tenancy state the stream's prefix of the same length implies —
--   so a tenant-keyed table that arrives here without RLS is CORRECT and a
--   tenant-keyed table that arrives one statement early is not.
--
--   FOUR OF THE SEVEN ARE TENANT-KEYED (workspace_members,
--   workspace_invitations, channel_bindings, and workspaces as the tenant root
--   itself). They carry no policy until F.2.9, and that window is the ruling's
--   stated cost, not an oversight.
--
--   DEPENDS ON 052. `ck_ws_tz_valid` calls `fn_safe_tz`, and every touch
--   trigger calls `trg_touch_updated_at` — both created by 052, which is why
--   the stream puts them first regardless of how the table increments are
--   ordered.
--
--   ABOVE THE LINEAGE BOUNDARY. 051 renames the legacy schema out of public;
--   these tables are created into the empty public it leaves behind. The
--   running application does not see them until the M.3 cutover.
--
-- Rollback: DROP TABLE IF EXISTS onboarding_sessions, workspace_invitations,
--   channel_bindings, workspace_members, workspaces, user_identities, users
--   CASCADE;
--   DROP FUNCTION IF EXISTS trg_workspaces_owner_at_insert();
--   DROP FUNCTION IF EXISTS trg_members_owner_exists();
--   Drops the two constraint-trigger functions with their tables; 052's shared
--   functions survive, since later increments depend on them.
-- Created: 2026-08-17
-- Issue: #806
-- EVERY POSTCONDITION BELOW MUST STAY TRUE FOREVER, not merely at the end of
--   this file's run. `migration_runner.py` derives a file's permanent ADOPTION
--   PROBE from its postconditions when the adoption manifest carries no entry,
--   so each line answers two questions: "did this file do its job just now" and
--   "has this file ever been applied to this database". A postcondition
--   asserting the ABSENCE of state a LATER increment adds passes the first and
--   then fails the second — at M.3 step 3a the probe would read unapplied on a
--   database plainly holding all seven tables, and the runner raises
--   `incoherent chain` naming THIS file while the real cause is the increment
--   that landed after it. So every line below is scoped to an object 053 itself
--   creates, and none asserts that something does not exist: F.2.8 enables RLS
--   on these tables and F.2.9 attaches their policies, and neither may
--   retroactively un-apply this migration.
-- runner:postcondition SELECT count(*) = 7 FROM information_schema.tables WHERE table_schema = 'public' AND table_name IN ('users','user_identities','workspaces','workspace_members','workspace_invitations','channel_bindings','onboarding_sessions')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = 'uq_members_one_owner')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = 'uq_invite_live')
-- runner:postcondition SELECT count(*) = 2 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.proname IN ('trg_members_owner_exists','trg_workspaces_owner_at_insert')

-- [§1 core identity/tenancy tables]
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

-- [§1 onboarding_sessions]
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
