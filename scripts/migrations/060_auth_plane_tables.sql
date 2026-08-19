-- Migration: 060_auth_plane_tables.sql
-- Description: F.2.9 — plan 07's auth plane, and THE LAST F.2 INCREMENT. Three
--   tables (session_tokens, oauth_states, service_tokens) with their touch
--   triggers, RLS enablement, two grants and five policies. Statements
--   241..256 — the final sixteen of the 257-statement advertised stream.
--
--   THE FIRST AND ONLY INCREMENT SOURCED FROM `07`. Measured to the statement,
--   not approximated: `02-domain-model.md` contributes stream indices 0..240
--   and `07-security-model.md` begins at 241, so every one of this file's 16
--   statements comes from `07` and none from `02`. The four `-- [§n ...]`
--   markers below are `07`'s own section numbering, which restarts at §1 — do
--   not read them as continuing `02`'s.
--
--   A CONTIGUOUS PREFIX OF THE ADVERTISED STREAM, the same contract every
--   increment since 052 has been under. Arm (b) diffs the concatenated target
--   lineage against the expanded stream as an ORDERED PREFIX; this file
--   completes it, so from here the lineage EQUALS the stream rather than
--   prefixing it. Nothing is authored — the body is the plan's own SQL.
--
--   TABLES ARRIVE WITH THEIR RLS AND POLICIES, unlike every `02` increment,
--   and that is the split's "#746's original property for free" (§3): `07`'s
--   auth plane is one contiguous run in the stream, so the structural
--   guarantee Fork 1 ruling (a) traded away for `02` is retained here at no
--   cost. Measured consequence: the tenancy gate reports 0 violations at
--   prefix 241 AND at 257 — these three tables never open a window.
--
--   THE `07` MARKER BELOW SAYS "6 policies" AND THERE ARE FIVE. Recorded
--   rather than corrected, because that text lives inside a manifest-hashed
--   block and editing it would break the sha256 bijection. Five is right,
--   confirmed three independent ways: the segment carries 5 CREATE POLICY,
--   the whole-stream delta is 58 - 53 = 5, and `07`'s raw block text contains
--   5. The likely origin of "6" is an assumed 3-ingress + 3-sweep symmetry:
--   `service_tokens` gets `p_auth_ingress_svctok` but NO sweep policy. That
--   asymmetry is deliberate and internally consistent — the maintenance GRANT
--   on the line above likewise covers only `session_tokens, oauth_states`, so
--   grant and policy agree that service tokens are not swept.
--
--   `oauth_states` HAS NO SURROGATE KEY: its primary key is the `state` TEXT
--   column itself, the 128-bit urlsafe random value the OAuth round-trip
--   carries. It is the one table in the increment that does not call `pk()`.
--
--   DEPENDS ON 052, 053 AND 057. Every touch trigger calls
--   `trg_touch_updated_at` (052); all three tables reference `users(id)` or
--   `workspaces(id)` (053); the policies name `svc_ingress` and
--   `svc_maintenance`, provisioned by the `04` window-bootstrap.
--
--   ABOVE THE LINEAGE BOUNDARY. 051 renames the legacy schema out of public;
--   these tables are created into the empty public it leaves behind. The
--   running application does not see them until the M.3 cutover.
--
-- Rollback: DROP TABLE IF EXISTS service_tokens, oauth_states, session_tokens
--   CASCADE;
--   The CASCADE takes the three touch triggers, the five policies and the two
--   grants with the tables; no separate REVOKE is needed.
-- Created: 2026-08-19
-- Issue: #806
-- EVERY POSTCONDITION BELOW MUST STAY TRUE FOREVER, not merely at the end of
--   this file's run. `migration_runner.py` derives a file's permanent ADOPTION
--   PROBE from its postconditions when the adoption manifest carries no entry,
--   so each line answers two questions: "did this file do its job just now" and
--   "has this file ever been applied to this database". None below asserts the
--   absence of state a later increment adds — and this being the last F.2
--   increment, there is no later increment in the lineage to add any.
-- runner:postcondition SELECT count(*) = 3 FROM information_schema.tables WHERE table_schema = 'public' AND table_name IN ('session_tokens','oauth_states','service_tokens')
-- runner:postcondition SELECT count(*) = 3 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relrowsecurity AND c.relname IN ('session_tokens','oauth_states','service_tokens')
-- runner:postcondition SELECT count(*) = 5 FROM pg_policies WHERE schemaname = 'public' AND policyname LIKE 'p_auth_%'
-- runner:postcondition SELECT has_table_privilege('svc_maintenance', 'session_tokens', 'DELETE') AND NOT has_table_privilege('svc_maintenance', 'service_tokens', 'DELETE')
-- runner:postcondition SELECT count(*) = 3 FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND NOT t.tgisinternal AND t.tgname IN ('tg_touch_session_tokens','tg_touch_oauth_states','tg_touch_service_tokens')

-- [§1 session_tokens + touch trigger]
CREATE TABLE session_tokens (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash    TEXT NOT NULL,                  -- SHA256 of the opaque cookie value
  expires_at    TIMESTAMPTZ NOT NULL,           -- now() + 30 days (05 seam), sliding on use
  revoked_at    TIMESTAMPTZ NULL,
  last_seen_at  TIMESTAMPTZ NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_session_token UNIQUE (token_hash)
);

CREATE TRIGGER tg_touch_session_tokens BEFORE UPDATE ON session_tokens
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

-- [§2 oauth_states + touch trigger]
CREATE TABLE oauth_states (
  state         TEXT PRIMARY KEY,               -- 128-bit urlsafe random
  user_id       UUID NULL REFERENCES users(id) ON DELETE CASCADE,
  workspace_id  UUID NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  provider      TEXT NOT NULL CONSTRAINT ck_oauth_state_provider
                CHECK (provider IN ('ig_login','gdrive','google','telegram')),
  purpose       TEXT NOT NULL CONSTRAINT ck_oauth_state_purpose
                CHECK (purpose IN ('connect','reconnect','signin','link')),
  reconnect_target UUID NULL,                   -- ig_account_id | media_source_id when purpose='reconnect'
  cookie_nonce_hash TEXT NULL,                  -- SHA256 of the browser nonce cookie (signin CSRF below)
  expires_at    TIMESTAMPTZ NOT NULL,           -- now() + state-token TTL (05 seam, all purposes)
  consumed_at   TIMESTAMPTZ NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- purpose-conditional context (the closed-CHECK convention, 02 §0): signin is anonymous by
  -- definition; link pins the user but no workspace (identity is user-plane, not tenant-plane);
  -- connect/reconnect pin both, exactly as before:
  CONSTRAINT ck_oauth_state_context CHECK (
    CASE purpose
      WHEN 'signin' THEN user_id IS NULL     AND workspace_id IS NULL
      WHEN 'link'   THEN user_id IS NOT NULL AND workspace_id IS NULL
      ELSE               user_id IS NOT NULL AND workspace_id IS NOT NULL
    END),
  CONSTRAINT ck_oauth_state_signin_nonce CHECK (
    (purpose = 'signin') = (cookie_nonce_hash IS NOT NULL))
);

CREATE TRIGGER tg_touch_oauth_states BEFORE UPDATE ON oauth_states
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

-- [§6 service_tokens + touch trigger]
CREATE TABLE service_tokens (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name         TEXT NOT NULL,                   -- 'cli-<host>', 'ops-dashboard', …
  token_hash   TEXT NOT NULL,                   -- SHA256 of the opaque bearer value
  role         TEXT NOT NULL CONSTRAINT ck_service_token_role CHECK (role IN ('operator','readonly')),
  workspace_id UUID NULL REFERENCES workspaces(id) ON DELETE CASCADE,  -- NULL = all workspaces (operator)
  expires_at   TIMESTAMPTZ NULL,
  revoked_at   TIMESTAMPTZ NULL,
  last_used_at TIMESTAMPTZ NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_service_token UNIQUE (token_hash)
);

CREATE TRIGGER tg_touch_service_tokens BEFORE UPDATE ON service_tokens
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

-- [§6b auth-plane RLS DDL (enable + grants + 6 policies)]
ALTER TABLE session_tokens ENABLE ROW LEVEL SECURITY;

ALTER TABLE oauth_states   ENABLE ROW LEVEL SECURITY;

ALTER TABLE service_tokens ENABLE ROW LEVEL SECURITY;

GRANT SELECT, INSERT, UPDATE ON session_tokens, oauth_states, service_tokens TO svc_ingress;

GRANT SELECT, DELETE ON session_tokens, oauth_states TO svc_maintenance;

CREATE POLICY p_auth_ingress_sessions ON session_tokens FOR ALL TO svc_ingress
  USING (true) WITH CHECK (true);

CREATE POLICY p_auth_ingress_states   ON oauth_states   FOR ALL TO svc_ingress
  USING (true) WITH CHECK (true);

CREATE POLICY p_auth_ingress_svctok   ON service_tokens FOR ALL TO svc_ingress
  USING (true) WITH CHECK (true);

CREATE POLICY p_auth_sweep_sessions ON session_tokens FOR ALL TO svc_maintenance
  USING (true) WITH CHECK (true);

CREATE POLICY p_auth_sweep_states   ON oauth_states   FOR ALL TO svc_maintenance
  USING (true) WITH CHECK (true);
