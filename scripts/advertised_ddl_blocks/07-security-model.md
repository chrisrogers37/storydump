```sql
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
```

```sql
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
```

```sql
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
```

```sql
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
-- service_tokens has NO sweep policy and svc_maintenance holds no grant on it: issuance and
-- revocation are operator actions (§6), rows are few, and no retention class exists — a sweep
-- door privilege nobody's schedule drives would be an unreviewed capability.
```

```sql
CREATE FUNCTION trg_intent_no_self_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'same-state write to post_intent % (state %) — a transition that did not happen', OLD.id, OLD.state
    USING ERRCODE = 'check_violation';
END $$;

CREATE TRIGGER tg_intent_no_self_transition BEFORE UPDATE OF state ON post_intents
  FOR EACH ROW WHEN (NEW.state IS NOT DISTINCT FROM OLD.state)
  EXECUTE FUNCTION trg_intent_no_self_transition();
```

```sql
-- Migration 062: the reauth-prompt clock leg + the marker it keys on (W5e half
-- of the credential lifecycle; `02` §5 :1165 declared the leg, nothing
-- implemented it — reauth_prompt had NO producer anywhere).
--
-- Also the arming index note: `store_credential` now arms `next_refresh_at`
-- at store time (Python side, same change set) — without that, a credential
-- stored at reconnect is invisible to the refresh leg forever, which is the
-- second pin of the silent-death landmine this change set pulls.
--
-- The cadence is `05`'s operational number (reauth-prompt cadence: 1 prompt /
-- account / week) and is deliberately NOT a parameter: the tick's signature
-- stays stable, and the number's home is `05`, not a config knob.

ALTER TABLE ig_accounts
  ADD COLUMN last_reauth_prompt_at TIMESTAMPTZ NULL;

COMMENT ON COLUMN ig_accounts.last_reauth_prompt_at IS
  'When the reauth-prompt clock leg last minted a prompt for this account. '
  'NULL = never prompted. Stamped at MINT (not delivery), symmetric with the '
  'refresh leg re-arming next_refresh_at at mint.';

CREATE INDEX ix_ig_accounts_reauth_due
  ON ig_accounts (last_reauth_prompt_at)
  WHERE state = 'reauth_required';

-- 057's matrix gives svc_clock full-table SELECT on ig_accounts and column
-- UPDATE on exactly next_slot_at (:129); the new marker needs the same
-- column-scoped shape:
GRANT UPDATE (last_reauth_prompt_at) ON ig_accounts TO svc_clock;

-- The return shape gains o_reauth_jobs, so this is DROP + CREATE (CREATE OR
-- REPLACE cannot change a RETURNS TABLE shape). Owner/grants re-established
-- below, matching 059's posture exactly — including its TRANSIENT, BRACKETED
-- CREATE grant: ALTER FUNCTION … OWNER TO needs the new owner to hold CREATE
-- on the schema (membership alone fails with "permission denied for schema
-- public" — 059's own words, re-confirmed by this migration failing exactly
-- that way without the bracket). Granted here, revoked below; the
-- steady-state matrix never carries CREATE for a door owner.
GRANT CREATE ON SCHEMA public TO svc_clock;

DROP FUNCTION fn_clock_tick(int, interval, jsonb);

CREATE FUNCTION fn_clock_tick(p_max int, p_refresh_cadence interval,
                              p_recurring jsonb)  -- {v:1, "<kind>": seconds, …} (05 seam)
RETURNS TABLE (o_slot_jobs int, o_refresh_jobs int, o_sync_jobs int,
               o_recurring_jobs int, o_reauth_jobs int)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE k text; cadence interval; last_done timestamptz; rem int;
        n1 int := 0; n2 int := 0; n3 int := 0; n4 int := 0; n5 int := 0;
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
  rem := rem - n3;
  -- (5) reauth prompts for accounts sitting reauth_required (`02` §5 :1165; `05`: 1/week).
  -- Marker stamped at MINT, symmetric with legs 2-4's re-arm-at-mint shape; the NOT EXISTS
  -- guards a still-open prompt job so a slow executor cannot pile up prompts for one account.
  -- ix_ig_accounts_reauth_due serves the scan:
  WITH due AS (
    SELECT a.id, a.workspace_id, a.provider_account_ref
      FROM ig_accounts a
     WHERE a.state = 'reauth_required'
       AND (a.last_reauth_prompt_at IS NULL
            OR a.last_reauth_prompt_at <= now() - interval '7 days')
       AND NOT EXISTS (SELECT 1 FROM jobs j
                        WHERE j.kind = 'reauth_prompt'
                          AND j.serialization_key = 'ig:' || a.provider_account_ref
                          AND j.state IN ('ready','leased'))
     LIMIT rem
  ), ins AS (
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, payload)
    SELECT 'reauth_prompt', d.workspace_id, 'bulk', 'ig:' || d.provider_account_ref, now(), 3,
           jsonb_build_object('v', 1, 'ig_account_id', d.id)
      FROM due d
  )
  UPDATE ig_accounts a SET last_reauth_prompt_at = now()
    FROM due d WHERE a.id = d.id;
  GET DIAGNOSTICS n5 = ROW_COUNT;
  RETURN QUERY SELECT n1, n2, n3, n4, n5;
END $$;

ALTER FUNCTION fn_clock_tick(int, interval, jsonb) OWNER TO svc_clock;

REVOKE CREATE ON SCHEMA public FROM svc_clock;

REVOKE ALL ON FUNCTION fn_clock_tick(int, interval, jsonb) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_clock_tick(int, interval, jsonb) TO svc_worker;
```

```sql
-- The CREATE bracket is 062's, carried forward for the same reason it gave:
-- `ALTER FUNCTION … OWNER TO` needs the incoming owner to hold CREATE on the
-- schema, and the steady-state grant matrix never leaves CREATE with a door
-- owner. Granted here, revoked below. Dropping this bracket makes the migration
-- fail at the ALTER — 062 records having failed exactly that way.
GRANT CREATE ON SCHEMA public TO svc_clock;

DROP FUNCTION fn_clock_tick(int, interval, jsonb);

CREATE FUNCTION fn_clock_tick(p_max int, p_refresh_cadence interval,
                              p_recurring jsonb)  -- {v:1, "<kind>": seconds, …} (05 seam)
RETURNS TABLE (o_slot_jobs int, o_refresh_jobs int, o_sync_jobs int,
               o_recurring_jobs int, o_reauth_jobs int)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE k text; cadence interval; last_done timestamptz; rem int;
        n1 int := 0; n2 int := 0; n3 int := 0; n4 int := 0; n5 int := 0;
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
       AND provider = 'ig_login'   -- 063: see the header. Fails CLOSED for any new provider.
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
  rem := rem - n3;
  -- (5) reauth prompts for accounts sitting reauth_required (`02` §5 :1165; `05`: 1/week).
  -- Marker stamped at MINT, symmetric with legs 2-4's re-arm-at-mint shape; the NOT EXISTS
  -- guards a still-open prompt job so a slow executor cannot pile up prompts for one account.
  -- ix_ig_accounts_reauth_due serves the scan:
  WITH due AS (
    SELECT a.id, a.workspace_id, a.provider_account_ref
      FROM ig_accounts a
     WHERE a.state = 'reauth_required'
       AND (a.last_reauth_prompt_at IS NULL
            OR a.last_reauth_prompt_at <= now() - interval '7 days')
       AND NOT EXISTS (SELECT 1 FROM jobs j
                        WHERE j.kind = 'reauth_prompt'
                          AND j.serialization_key = 'ig:' || a.provider_account_ref
                          AND j.state IN ('ready','leased'))
     LIMIT rem
  ), ins AS (
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, payload)
    SELECT 'reauth_prompt', d.workspace_id, 'bulk', 'ig:' || d.provider_account_ref, now(), 3,
           jsonb_build_object('v', 1, 'ig_account_id', d.id)
      FROM due d
  )
  UPDATE ig_accounts a SET last_reauth_prompt_at = now()
    FROM due d WHERE a.id = d.id;
  GET DIAGNOSTICS n5 = ROW_COUNT;
  RETURN QUERY SELECT n1, n2, n3, n4, n5;
END $$;

COMMENT ON FUNCTION fn_clock_tick(int, interval, jsonb) IS
  'The scheduled clock tick: a SECURITY DEFINER producer of due work, owned by '
  'svc_clock with EXECUTE granted to svc_worker, and pinned to '
  'search_path = pg_catalog, public. One call runs five legs in order - '
  'recurring system singletons, due account slots, due credential refreshes, '
  'due source syncs, and reauth prompts. The legs share one budget: p_max caps '
  'the first, and each later leg draws only on what the ones before it left, so '
  'one call mints at most p_max rows. It is NOT the only writer of jobs - '
  'application services enqueue directly as well. The refresh leg is scoped to '
  'provider ig_login and fails closed: a provider with no refresh door of its '
  'own is skipped rather than minted.';

ALTER FUNCTION fn_clock_tick(int, interval, jsonb) OWNER TO svc_clock;

REVOKE CREATE ON SCHEMA public FROM svc_clock;

REVOKE ALL ON FUNCTION fn_clock_tick(int, interval, jsonb) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_clock_tick(int, interval, jsonb) TO svc_worker;
```

```sql
-- 10/10 fn_memberships_for_caller — the user-plane door the web surface reads its workspace
-- list through (#1037; the gap was found twice — #1031 writer-side, #1035 router-side — and
-- confirmed one gap). p_tenant on workspace_members is workspace_id = app.tenant_id, so "which
-- workspaces does this user belong to" has no pre-context read path for svc_ingress: with no
-- tenant set the table reads EMPTY, and an empty list is indistinguishable from the greenfield's
-- normal signed-in-with-no-workspace state. Both lanes therefore REFUSED
-- (membership_list_unreadable) until this door existed. The caller is read from
-- app.actor_user_id INTERNALLY, never taken as a parameter: one trust point — the GUC the
-- audit triggers already attribute every governance write to — and an unset GUC fails closed
-- to zero rows. Owned by svc_membership (already USING (true) on workspace_members and SELECT
-- on workspaces); it returns exactly the four columns the surface renders, never a widened read.
-- The CREATE bracket is 062's, for the reason it gave: ALTER FUNCTION … OWNER TO needs the
-- incoming owner to hold CREATE on the schema, and the steady state never leaves it there.
GRANT CREATE ON SCHEMA public TO svc_membership;

CREATE FUNCTION fn_memberships_for_caller()
RETURNS TABLE (o_workspace_id uuid, o_name varchar, o_state text, o_role text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT m.workspace_id, w.name, w.state, m.role
    FROM workspace_members m JOIN workspaces w ON w.id = m.workspace_id
   WHERE m.user_id = NULLIF(current_setting('app.actor_user_id', true), '')::uuid
   ORDER BY w.created_at, w.id
$$;

COMMENT ON FUNCTION fn_memberships_for_caller() IS
  'The calling user''s workspace memberships: a SECURITY DEFINER user-plane read owned by '
  'svc_membership with EXECUTE granted to svc_ingress. The caller is app.actor_user_id, read '
  'internally and never a parameter; unset, it returns no rows. Exists because p_tenant on '
  'workspace_members cannot answer a cross-tenant question for one user.';

ALTER FUNCTION fn_memberships_for_caller() OWNER TO svc_membership;

REVOKE CREATE ON SCHEMA public FROM svc_membership;

REVOKE ALL ON FUNCTION fn_memberships_for_caller() FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_memberships_for_caller() TO svc_ingress;

-- The read the door serves had no index: workspace_members' primary key is (workspace_id,
-- user_id), so a by-user lookup was a sequential scan.
CREATE INDEX ix_members_user ON workspace_members (user_id);
```

```sql
-- alert_stranded_sources (#1061): a system singleton that re-alerts sources stranded in
-- `error`. BOTH constraints gate the insert — the vocabulary and the workspace_id
-- biconditional — so both are widened, and the kind joins the SYSTEM group in each.
-- fn_clock_tick is deliberately untouched: fork F4 rejected widening what the tick SELECTS,
-- and adding an allowed VALUE to a constraint is a different act from changing which rows
-- the tick selects.
ALTER TABLE jobs DROP CONSTRAINT ck_jobs_kind;
ALTER TABLE jobs ADD CONSTRAINT ck_jobs_kind CHECK (kind IN (
  -- tenant kinds (workspace_id NOT NULL):
  'plan_slot','publish_pipeline','deliver_outbox','sync_media_source',
  'first_ingest_chunk','refresh_credential','offboard_workspace',
  'revoke_workspace_credentials','reauth_prompt',
  -- system kinds (workspace_id NULL):
  'reconcile_ambiguous','reap_expired','reap_transit_assets','retention_sweep',
  'reencrypt_credentials','send_email','alert_stranded_sources'));

ALTER TABLE jobs DROP CONSTRAINT ck_jobs_system_kinds;
ALTER TABLE jobs ADD CONSTRAINT ck_jobs_system_kinds CHECK (
  (workspace_id IS NULL) = (kind IN
    ('reconcile_ambiguous','reap_expired','reap_transit_assets','retention_sweep',
     'reencrypt_credentials','send_email','alert_stranded_sources')));
```

```sql
-- The no-media notice dedup marker (#1090 D3). The window itself (05: 24 h) is a
-- WorkerConfig parameter, not a constant here: the column records WHEN the notice
-- last fired, never HOW OFTEN it may.
ALTER TABLE ig_accounts
  ADD COLUMN last_no_media_notice_at TIMESTAMPTZ NULL;

COMMENT ON COLUMN ig_accounts.last_no_media_notice_at IS
  'When this account last told its workspace that a slot found no media. '
  'NULL = never told. Stamped by the slot planner at NOTICE time, in the same '
  'transaction as the outbox row, so a rolled-back plan takes its notice with '
  'it. The dedup window is 05 (24 h) and lives in WorkerConfig, not here: the '
  'column records WHEN, never HOW OFTEN.';
```

```sql
-- The bind purpose (#1175 D-3, owner ruling 2026-09-05): an admin's one-shot
-- `startgroup` link binds the group it is opened in to the pinned workspace.
-- `ck_oauth_state_context`'s ELSE branch already requires BOTH user_id and
-- workspace_id for any purpose that is not signin or link, which is exactly
-- what a bind state must pin. Drop-and-add is the repo's shape for a CHECK
-- edit (042, 045, 046, 049, 065).
ALTER TABLE oauth_states DROP CONSTRAINT ck_oauth_state_purpose;
ALTER TABLE oauth_states ADD CONSTRAINT ck_oauth_state_purpose
  CHECK (purpose IN ('connect','reconnect','signin','link','bind'));
```

```sql
-- 11/13 fn_resolve_binding — the chat-inbound resolver door (#854 (a); #1242). p_tenant on
-- channel_bindings is workspace_id = app.tenant_id, so "which workspace is this chat" has no
-- pre-context read path for svc_ingress: the row exists and the read comes back EMPTY, and
-- resolve_chat refused as unknown_binding by measurement (the harness pinned it). A definer
-- read owned by svc_membership (already SELECT-true on channel_bindings) that exposes exactly
-- ONE row — the named chat's — where a lookup policy would expose every workspace's topology.
-- The CREATE bracket is 062's, for the reason it gave.
GRANT CREATE ON SCHEMA public TO svc_membership;

CREATE FUNCTION fn_resolve_binding(p_channel text, p_external_ref text)
RETURNS TABLE (o_workspace_id uuid, o_binding_id uuid, o_state text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT b.workspace_id, b.id, b.state
    FROM channel_bindings b
   WHERE b.channel = p_channel AND b.external_ref = p_external_ref
$$;

COMMENT ON FUNCTION fn_resolve_binding(text, text) IS
  'Chat inbound -> workspace: the one row for a (channel, external_ref), any state, so the '
  'caller can tell revoked from never-bound. SECURITY DEFINER owned by svc_membership with '
  'EXECUTE granted to svc_ingress; exists because p_tenant on channel_bindings cannot answer a '
  'pre-context question about one chat.';

-- 12/13 fn_group_member_seen — the 06 Telegram join path (#1242): a person seen in a BOUND
-- group becomes a member at role ''member''. THE CALLER PROVES THE PERSON: p_user is the user
-- the ingress adapter resolved from the sender''s linked Telegram identity (user_identities is
-- role-open to it), and this door trusts that resolution the way the worker trusts
-- job.workspace_id — the contract is stated here rather than re-checked in-body. Never a
-- downgrade (ON CONFLICT DO NOTHING: an owner or admin stays what they are); never a write for
-- an unbound or revoked chat, an inactive workspace, or a user that vanished between
-- resolution and insert (a disabled user is the caller's refusal: users is user-plane to it). The actor GUCs are set HERE (transaction-LOCAL: this must be
-- the last governance write in its transaction) so the audit trigger attributes the row to the
-- joiner as ''user'' — the same convention fn_invitation_accept records for an acceptor.
CREATE FUNCTION fn_group_member_seen(p_channel text, p_external_ref text, p_user uuid)
RETURNS TABLE (o_workspace_id uuid, o_outcome text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
  v_ws uuid;
  v_state text;
  v_ws_state text;
  v_inserted int;
BEGIN
  SELECT b.workspace_id, b.state INTO v_ws, v_state
    FROM channel_bindings b
   WHERE b.channel = p_channel AND b.external_ref = p_external_ref;
  IF v_ws IS NULL THEN
    RETURN QUERY SELECT NULL::uuid, 'unbound_chat'::text; RETURN;
  END IF;
  IF v_state <> 'active' THEN
    RETURN QUERY SELECT v_ws, 'revoked_chat'::text; RETURN;
  END IF;
  SELECT w.state INTO v_ws_state FROM workspaces w WHERE w.id = v_ws;
  IF v_ws_state IS DISTINCT FROM 'active' THEN
    RETURN QUERY SELECT v_ws, 'workspace_inactive'::text; RETURN;
  END IF;
  PERFORM set_config('app.actor_kind', 'user', true);
  PERFORM set_config('app.actor_user_id', p_user::text, true);
  PERFORM set_config('app.channel', 'telegram', true);
  BEGIN
    INSERT INTO workspace_members (workspace_id, user_id, role)
    VALUES (v_ws, p_user, 'member')
    ON CONFLICT (workspace_id, user_id) DO NOTHING;
    GET DIAGNOSTICS v_inserted = ROW_COUNT;
  EXCEPTION WHEN foreign_key_violation THEN
    -- The user vanished between the caller's resolution and this insert:
    -- a named outcome, never a raise the ingress route would turn into a
    -- redelivery loop.
    RETURN QUERY SELECT v_ws, 'unknown_user'::text; RETURN;
  END;
  RETURN QUERY SELECT v_ws, CASE WHEN v_inserted > 0 THEN 'joined' ELSE 'already_member' END;
END $$;

COMMENT ON FUNCTION fn_group_member_seen(text, text, uuid) IS
  'The 06 Telegram join path: a person seen in a bound, active group becomes a member (role '
  'member; never a downgrade). p_user is the caller''s resolution of the sender''s linked '
  'Telegram identity — the caller proves the person, this door trusts it. Sets the actor GUCs '
  'transaction-locally, so it must be the last governance write in its transaction. SECURITY '
  'DEFINER owned by svc_membership with EXECUTE granted to svc_ingress. Outcomes: joined, '
  'already_member, unbound_chat, revoked_chat, workspace_inactive, unknown_user.';

-- 13/13 fn_member_remove — the revoke for every join edge (06: "an admin removes membership
-- explicitly"; #1242 review). The 057 grant matrix is structural about this: NO login role
-- deletes, a delete lives only inside a door body as its NOLOGIN owner — so the one DELETE
-- svc_membership receives here backs exactly this body. THE CALLER PROVES THE ADMIN: p_by_user
-- is the command port's actor, already held to the admin floor; the door refuses by name what
-- no admin may do — remove the owner (transfer_ownership is that edge) or remove themselves —
-- and answers not_found for a non-member. The actor GUCs are the caller's (a web command's
-- unit of work sets them), so the governance audit row names the admin who did it.
GRANT DELETE ON workspace_members TO svc_membership;

CREATE FUNCTION fn_member_remove(p_workspace uuid, p_user uuid, p_by_user uuid)
RETURNS TABLE (o_outcome text, o_role text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  v_role text;
BEGIN
  IF p_user = p_by_user THEN
    RETURN QUERY SELECT 'self'::text, NULL::text; RETURN;
  END IF;
  SELECT m.role INTO v_role FROM workspace_members m
   WHERE m.workspace_id = p_workspace AND m.user_id = p_user;
  IF v_role IS NULL THEN
    RETURN QUERY SELECT 'not_found'::text, NULL::text; RETURN;
  END IF;
  IF v_role = 'owner' THEN
    RETURN QUERY SELECT 'owner'::text, v_role; RETURN;
  END IF;
  DELETE FROM workspace_members m WHERE m.workspace_id = p_workspace AND m.user_id = p_user;
  RETURN QUERY SELECT 'removed'::text, v_role;
END $$;

COMMENT ON FUNCTION fn_member_remove(uuid, uuid, uuid) IS
  'The revoke for every join edge (06): an admin removes a member explicitly. The one DELETE on '
  'workspace_members in the system lives here (057: no login role deletes). p_by_user is the '
  'command port''s actor, already held to the admin floor — the caller proves the admin. '
  'Outcomes: removed, not_found, owner (never removable here), self (never through this door). '
  'SECURITY DEFINER owned by svc_membership with EXECUTE granted to svc_ingress.';

ALTER FUNCTION fn_resolve_binding(text, text) OWNER TO svc_membership;
ALTER FUNCTION fn_group_member_seen(text, text, uuid) OWNER TO svc_membership;
ALTER FUNCTION fn_member_remove(uuid, uuid, uuid) OWNER TO svc_membership;

REVOKE CREATE ON SCHEMA public FROM svc_membership;

REVOKE ALL ON FUNCTION fn_resolve_binding(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION fn_group_member_seen(text, text, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION fn_member_remove(uuid, uuid, uuid) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_resolve_binding(text, text) TO svc_ingress;
GRANT EXECUTE ON FUNCTION fn_group_member_seen(text, text, uuid) TO svc_ingress;
GRANT EXECUTE ON FUNCTION fn_member_remove(uuid, uuid, uuid) TO svc_ingress;
```

```sql
-- The workspace-level Drive grant (owner ruling 2026-09-05; #1165's lean (b) — the same
-- drive.readonly scope, the order inverted). ONE Google grant per workspace, reused by every
-- folder it picks: a gdrive credential names NO owner column — the workspace is its owner — so
-- the owner XOR becomes provider-conditional, and the per-source unique key becomes a
-- per-workspace one. media_source_id stays (nullable, its FK intact) with no writer: every
-- gdrive row is a workspace row from here on. The ADD CONSTRAINT validates the whole table, so
-- a per-source gdrive row anywhere would abort this file (DROP and ADD roll back together);
-- none exists — the target tier never completed a production Drive connect before this ruling.
ALTER TABLE oauth_credentials DROP CONSTRAINT ck_credentials_one_owner;

ALTER TABLE oauth_credentials ADD CONSTRAINT ck_credentials_one_owner CHECK (
  CASE provider
    WHEN 'ig_login' THEN ig_account_id IS NOT NULL AND media_source_id IS NULL
    ELSE                 ig_account_id IS NULL     AND media_source_id IS NULL
  END);

DROP INDEX uq_credential_per_source;

CREATE UNIQUE INDEX uq_credential_per_workspace ON oauth_credentials (workspace_id, provider)
  WHERE ig_account_id IS NULL AND media_source_id IS NULL;
```

```sql
-- The folder path of a media item under its connected folder (owner ruling 2026-09-08 — sources
-- are the groups; every folder under a connected folder is walked, to any depth, lazily). "" for
-- a file directly in the connected folder; NULL for an adapter that has no notion of folders. A
-- label the sync refreshes on every walk beside `category` (the TOP-LEVEL folder's name): nothing
-- is keyed by either — the group that carries a weight is the source (`media_sources`), next PR.
ALTER TABLE media_items ADD COLUMN folder_path TEXT NULL;
```

```sql
-- The posting mix keyed on the CONNECTED FOLDER (owner ruling 2026-09-08 — sources are the
-- groups): a weight belongs to a media_sources row, never to a folder name. `category` stays as
-- the label the card shows (the source's folder_name at save time). Rows written before this
-- migration carry no source_id: the planner ignores them and the first save supersedes them.
-- No FK: the tenancy lane refuses ADD FOREIGN KEY, and a source is removed by flag, never deleted.
ALTER TABLE category_post_case_mix ADD COLUMN source_id UUID NULL;

DROP INDEX uq_case_mix_current;

CREATE UNIQUE INDEX uq_case_mix_current_by_source ON category_post_case_mix (workspace_id, source_id)
  WHERE effective_to IS NULL AND source_id IS NOT NULL;
```

```sql
-- The outbox rows of one intent, for the tap's supersede-everywhere (phase 1 of the 2026-09-09 tap
-- plan): every flip retires the intent's cards in every binding with `WHERE … intent_id = :i`, and
-- the settled-card sweep joins live cards to their intents. Without this index each was a sequential
-- scan of the outbox on the tap's own transaction. Partial: notifications and acks carry no intent.
CREATE INDEX ix_outbox_intent ON channel_outbox (intent_id) WHERE intent_id IS NOT NULL;
```

```sql
-- Dry Run Mode (Settings › General, owner ruling 2026-09-10): an approved post completes as if
-- published — the cap debit, the rotation, the card's line — and nothing reaches Instagram. The
-- row says so in its own column: `published_via = 'dry_run'`, no container, no media id from Meta.
-- The two checks that pin what a posted row must carry gain the branch; nothing else moves.
ALTER TABLE post_intents DROP CONSTRAINT ck_intent_via;
ALTER TABLE post_intents ADD CONSTRAINT ck_intent_via
  CHECK (published_via IN ('api','manual','legacy_backfill','dry_run'));
ALTER TABLE post_intents DROP CONSTRAINT ck_posted_complete;
ALTER TABLE post_intents ADD CONSTRAINT ck_posted_complete CHECK (
  state <> 'posted'
  OR published_via = 'legacy_backfill'
  OR (published_via = 'manual' AND cap_consumed_on IS NOT NULL)
  OR (published_via = 'dry_run' AND publish_step = 'effect_confirmed' AND cap_consumed_on IS NOT NULL)
  OR (published_via = 'api' AND ig_container_id IS NOT NULL
      AND publish_step = 'effect_confirmed' AND cap_consumed_on IS NOT NULL));
```

```sql
-- Phase 3b of the 2026-09-09 tap plan (K claim-and-run tasks per lane): `fn_claim_job` counts
-- a workspace's leased rows on the lane for the per-workspace cap on EVERY claim, and K claimers
-- per lane make that K times as many counts. The partial index serves exactly that predicate;
-- `uq_jobs_serialized_lease` (the one-key-one-runner proof) and the claim/expiry indexes stand.
CREATE INDEX ix_jobs_leased_lane_ws ON jobs (lane, workspace_id) WHERE state = 'leased';
```

```sql
-- #1297 (the one-edit tap): a live sender's lost answer is resolved after a backoff — the sender
-- sweep (every 3 s, estate-wide) asks each binding for an `ambiguous` row older than it, and the
-- binding's sender lists its aged rows on every tick. Neither predicate had an index of its own:
-- `ix_outbox_due` is partial on `pending`, `ix_outbox_retire` is `(updated_at)` alone, so the sweep
-- became a full scan of an unbounded table. The partial index serves exactly both predicates.
CREATE INDEX ix_outbox_ambiguous_age ON channel_outbox (binding_id, updated_at) WHERE state = 'ambiguous';
```

```sql
-- The float (plan 03, 2026-09-14): a story that must wait between attempts steps back from
-- `publishing` to `approved`, keeping its progress and its cap debit, so the account's publish
-- slot (key 4) is free while it waits; and a decision the workspace made is never silently
-- undone — a dead job or the reaper's safety net parks an approved story for the workspace's
-- review instead of expiring it. Two edges; the `02` §4 matrix becomes 29.
INSERT INTO post_intent_transitions (from_state, to_state)
VALUES ('publishing', 'approved'), ('approved', 'review_required')
ON CONFLICT DO NOTHING;

-- The reaper's `approved → expired` leg is withdrawn: an approved story past `p_approved_ttl` is
-- parked for review by the reap executor (`scheduler.execute_reap_expired`), which can restate
-- the card and tell the workspace — a SQL door cannot. The parameter stays so the signature, the
-- grant and the executor's call are unchanged. Every other leg is 059's, verbatim.
CREATE OR REPLACE FUNCTION fn_reaper_sweep(p_lim int, p_approval_ttl interval, p_approved_ttl interval)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE n int := 0; c int; rem int := GREATEST(p_lim, 0);
BEGIN
  PERFORM set_config('app.actor_kind', 'reaper', true);
  UPDATE jobs SET state = 'ready', locked_by = NULL, lease_token = NULL, locked_until = NULL
   WHERE id IN (SELECT id FROM jobs
                 WHERE state = 'leased' AND locked_until < now() LIMIT rem);
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  UPDATE post_intents SET state = 'expired'
   WHERE id IN (SELECT id FROM post_intents
                 WHERE state IN ('scheduled','prompt_pending') AND schedule_slot_at < now()
                 LIMIT rem);
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  UPDATE post_intents i SET state = 'expired'
   WHERE i.id IN (
     SELECT i2.id FROM post_intents i2 JOIN workspaces w ON w.id = i2.workspace_id
      WHERE i2.state = 'awaiting_approval'
        AND i2.entered_state_at
            < now() - COALESCE(w.approval_ttl_minutes * interval '1 minute', p_approval_ttl)
      LIMIT rem);
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

-- The listing door for the executor's approved leg. The reap job is a system singleton
-- (`app.tenant_id = ''`), under which a plain SELECT over post_intents matches nothing once the
-- worker runs as svc_worker (`p_tenant`; the reconciler's own lesson, `reconciler.py`). SECURITY
-- DEFINER as svc_maintenance, which reads every row (`p_maint_intents`, `p_maint_ws`); the
-- executor asserts each row's tenant before it parks or cancels it. A paused workspace's stories
-- wait on purpose and are not listed. The fourteenth `02` §7 door. Bracketed as 059 and 062:
-- ALTER FUNCTION … OWNER TO needs the new owner to hold CREATE on the schema, granted here and
-- revoked below; the steady-state matrix never carries CREATE for a door owner.
GRANT CREATE ON SCHEMA public TO svc_maintenance;

CREATE FUNCTION fn_reaper_stale_approved(p_approved_ttl interval, p_lim int)
RETURNS TABLE (o_intent_id uuid, o_workspace_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT i.id, i.workspace_id
    FROM post_intents i JOIN workspaces w ON w.id = i.workspace_id
   WHERE i.state = 'approved' AND NOT w.is_paused
     AND i.entered_state_at < now() - p_approved_ttl
   ORDER BY i.entered_state_at
   LIMIT GREATEST(p_lim, 0)
$$;

ALTER FUNCTION fn_reaper_stale_approved(interval, int) OWNER TO svc_maintenance;

REVOKE CREATE ON SCHEMA public FROM svc_maintenance;

REVOKE ALL ON FUNCTION fn_reaper_stale_approved(interval, int) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_reaper_stale_approved(interval, int) TO svc_worker;
```

```sql
-- [§23 service_tokens: the person-bound subject beside the workspace one]
-- A token acts for exactly one subject: a person (user_id set, workspace_id NULL — acts as that
-- person across their memberships, never above the membership role) or a workspace (a service
-- identity: workspace_id set, user_id NULL — reads its one workspace under its own name). The 060
-- reading of "workspace_id NULL = all workspaces (operator)" is retired: a token with no
-- workspace is a person's, and nothing acts across every workspace.
ALTER TABLE service_tokens ADD COLUMN user_id UUID NULL REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE service_tokens ADD CONSTRAINT ck_service_token_subject CHECK ((user_id IS NULL) <> (workspace_id IS NULL));
CREATE INDEX ix_service_tokens_user ON service_tokens (user_id) WHERE user_id IS NOT NULL;
```

```sql
-- [§24 the estate-health doors: the two health surfaces and the Meta callback read the estate through svc_maintenance]
-- /health/scheduling and /health/posting count across every workspace, and both rested on the
-- owner login's BYPASSRLS: under svc_ingress with no tenant set every policy-covered table reads
-- empty, the surfaces answer never-posted / no-signal for a live estate, and monitoring that
-- relies on them goes blind (#751). The Meta deauthorize callback's account lookup
-- names no tenant either. Nine definer doors, each the module's own query moved verbatim; owned by
-- svc_maintenance, which held USING (true) on every table but ig_accounts. EXECUTE for svc_ingress
-- on the eight it serves; the three backpressure reads also for svc_worker, whose status line
-- renders the same snapshot; the one read that NAMES a tenant for svc_worker only.
-- The CREATE bracket is 062's, for the reason it gave.
GRANT SELECT ON ig_accounts TO svc_maintenance;

CREATE POLICY p_maint_accts ON ig_accounts FOR SELECT TO svc_maintenance USING (true);

GRANT CREATE ON SCHEMA public TO svc_maintenance;

CREATE FUNCTION fn_health_posting_freshness()
RETURNS TABLE (o_posted_ever bigint, o_last_post_age_seconds numeric, o_intents_ever bigint, o_oldest_intent_age_seconds numeric)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT count(*) FILTER (WHERE state = 'posted' AND published_via NOT IN ('legacy_backfill', 'dry_run')),
         min(EXTRACT(EPOCH FROM now() - entered_state_at))
           FILTER (WHERE state = 'posted' AND published_via NOT IN ('legacy_backfill', 'dry_run')),
         count(*),
         max(EXTRACT(EPOCH FROM now() - created_at))
    FROM post_intents
$$;

COMMENT ON FUNCTION fn_health_posting_freshness() IS
  'Landings and their freshness, estate-wide, for /health/posting: confirmed posts (never a legacy_backfill or dry_run row), the age of the freshest, every intent ever and the age of the oldest. A SECURITY DEFINER read owned by svc_maintenance; EXECUTE for svc_ingress, the surface''s login. Exists because p_tenant hides every row from a tenant-less read (081, #751).';

ALTER FUNCTION fn_health_posting_freshness() OWNER TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_health_posting_freshness() FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_health_posting_freshness() TO svc_ingress;

CREATE FUNCTION fn_health_publish_attempts()
RETURNS TABLE (o_debited_total bigint, o_ledger_days bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT coalesce(sum(count), 0), count(*)
    FROM daily_post_counts
$$;

COMMENT ON FUNCTION fn_health_publish_attempts() IS
  'The cap ledger''s attempt record for /health/posting: what was debited in total and how many (workspace, account, day) buckets ever debited. Owned by svc_maintenance; EXECUTE for svc_ingress (081, #751).';

ALTER FUNCTION fn_health_publish_attempts() OWNER TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_health_publish_attempts() FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_health_publish_attempts() TO svc_ingress;

CREATE FUNCTION fn_health_destinations()
RETURNS TABLE (o_accounts_active bigint, o_oldest_active_destination_age_seconds numeric)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT count(*), max(EXTRACT(EPOCH FROM now() - created_at))
    FROM ig_accounts WHERE state = 'active'
$$;

COMMENT ON FUNCTION fn_health_destinations() IS
  'The destinations that could receive a post, for /health/posting: a count and the age of the oldest. Owned by svc_maintenance; EXECUTE for svc_ingress (081, #751).';

ALTER FUNCTION fn_health_destinations() OWNER TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_health_destinations() FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_health_destinations() TO svc_ingress;

CREATE FUNCTION fn_health_scheduling_lag()
RETURNS TABLE (o_stalled bigint, o_accounts_active bigint, o_max_lag_seconds numeric)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT count(*) FILTER (WHERE next_slot_at IS NOT NULL AND next_slot_at <= now()),
         count(*),
         max(EXTRACT(EPOCH FROM now() - next_slot_at))
           FILTER (WHERE next_slot_at IS NOT NULL AND next_slot_at <= now())
    FROM ig_accounts WHERE state = 'active'
$$;

COMMENT ON FUNCTION fn_health_scheduling_lag() IS
  'The cursor lag for /health/scheduling: active accounts whose slot is due and unadvanced, every active account, and the worst lag. Owned by svc_maintenance; EXECUTE for svc_ingress (081, #751).';

ALTER FUNCTION fn_health_scheduling_lag() OWNER TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_health_scheduling_lag() FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_health_scheduling_lag() TO svc_ingress;

CREATE FUNCTION fn_health_ready_lanes()
RETURNS TABLE (o_lane text, o_ready bigint, o_oldest_age numeric)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT lane, count(*), EXTRACT(EPOCH FROM max(now() - run_at))
    FROM jobs WHERE state = 'ready' AND run_at <= now()
   GROUP BY lane
$$;

COMMENT ON FUNCTION fn_health_ready_lanes() IS
  'The backpressure signal''s ready lanes: due, unclaimed jobs per lane and the oldest wait, every tenant''s included. Owned by svc_maintenance; EXECUTE for svc_ingress (/health/scheduling) and svc_worker (its own status line) (081, #751).';

ALTER FUNCTION fn_health_ready_lanes() OWNER TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_health_ready_lanes() FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_health_ready_lanes() TO svc_ingress, svc_worker;

CREATE FUNCTION fn_health_outbox_pending()
RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT count(*) FROM channel_outbox WHERE state = 'pending'
$$;

COMMENT ON FUNCTION fn_health_outbox_pending() IS
  'The backpressure signal''s pending outbox rows, every tenant''s included. Owned by svc_maintenance; EXECUTE for svc_ingress and svc_worker (081, #751).';

ALTER FUNCTION fn_health_outbox_pending() OWNER TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_health_outbox_pending() FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_health_outbox_pending() TO svc_ingress, svc_worker;

CREATE FUNCTION fn_health_oldest_tenant_wait()
RETURNS TABLE (o_wait numeric)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT EXTRACT(EPOCH FROM now() - min(run_at))
    FROM jobs WHERE state = 'ready' AND run_at <= now() AND workspace_id IS NOT NULL
   GROUP BY workspace_id ORDER BY 1 DESC LIMIT 1
$$;

COMMENT ON FUNCTION fn_health_oldest_tenant_wait() IS
  'The backpressure signal''s longest tenant wait, in seconds, naming no tenant: the shape the unauthenticated surface may hold. Owned by svc_maintenance; EXECUTE for svc_ingress and svc_worker (081, #751).';

ALTER FUNCTION fn_health_oldest_tenant_wait() OWNER TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_health_oldest_tenant_wait() FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_health_oldest_tenant_wait() TO svc_ingress, svc_worker;

CREATE FUNCTION fn_health_oldest_tenant_wait_named()
RETURNS TABLE (o_workspace_id uuid, o_wait numeric)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT workspace_id, EXTRACT(EPOCH FROM now() - min(run_at))
    FROM jobs WHERE state = 'ready' AND run_at <= now() AND workspace_id IS NOT NULL
   GROUP BY workspace_id ORDER BY 2 DESC LIMIT 1
$$;

COMMENT ON FUNCTION fn_health_oldest_tenant_wait_named() IS
  'The backpressure signal''s longest-waiting tenant, NAMED — for the worker''s own log and nothing else: EXECUTE for svc_worker only, so the API''s login never holds a foreign workspace id. Owned by svc_maintenance (081, #751).';

ALTER FUNCTION fn_health_oldest_tenant_wait_named() OWNER TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_health_oldest_tenant_wait_named() FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_health_oldest_tenant_wait_named() TO svc_worker;

CREATE FUNCTION fn_meta_accounts_for_ref(p_ref text)
RETURNS TABLE (o_ig_account_id uuid, o_workspace_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT id, workspace_id FROM ig_accounts WHERE provider_account_ref = p_ref
$$;

COMMENT ON FUNCTION fn_meta_accounts_for_ref(p_ref text) IS
  'The accounts a Meta callback''s subject names, across every workspace: a deauthorize or data-deletion request carries no tenant, so p_tenant would hide every row from the API''s login. A parameterised SECURITY DEFINER read owned by svc_maintenance, an equality on one bound value; EXECUTE for svc_ingress (081, #751).';

ALTER FUNCTION fn_meta_accounts_for_ref(p_ref text) OWNER TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_meta_accounts_for_ref(p_ref text) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_meta_accounts_for_ref(p_ref text) TO svc_ingress;

REVOKE CREATE ON SCHEMA public FROM svc_maintenance;
```

```sql
-- [§25 the worker's doors: the tenant-less sweeps read the estate through svc_maintenance]
-- The worker's sender sweep, prompt sweep, settled-card sweep, stranded-source alert and the
-- reconciler's container poll and ladder count opened a session with an empty tenant and ran SQL on
-- policy-covered tables — the class that blinded the health surfaces under svc_ingress (#751). Under
-- svc_worker the sender sweep minted nothing, the prompt sweep found nothing, no ended story's card
-- lost its buttons and the ladder never climbed; the writes would have been refused. The reads
-- become doors owned by svc_maintenance, which lacked channel_bindings, media_items and
-- media_sources (SELECT) and INSERT on jobs; the writes stay under each workspace's own tenant, and
-- a sweep hands its caller's scope back. The sender sweep is one door because its statement is one
-- statement: the NOT EXISTS live-job check evaluated with the mint is what makes it idempotent. The
-- CREATE bracket is 062's.
GRANT SELECT ON channel_bindings, media_items, media_sources TO svc_maintenance;

GRANT INSERT ON jobs TO svc_maintenance;

CREATE POLICY p_maint_bindings ON channel_bindings FOR SELECT TO svc_maintenance USING (true);

CREATE POLICY p_maint_media ON media_items FOR SELECT TO svc_maintenance USING (true);

CREATE POLICY p_maint_sources ON media_sources FOR SELECT TO svc_maintenance USING (true);

GRANT CREATE ON SCHEMA public TO svc_maintenance;

CREATE FUNCTION fn_sender_sweep(p_prefix text, p_attempts int, p_deadline numeric, p_age numeric, p_limit int)
RETURNS int
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  WITH minted AS (
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key,
                      run_at, max_attempts, deadline_at, payload)
    SELECT 'deliver_outbox', b.workspace_id, 'interactive',
           p_prefix || b.id, now(), p_attempts,
           now() + make_interval(secs => p_deadline),
           jsonb_build_object('v', 1, 'binding_id', b.id)
      FROM channel_bindings b
     WHERE b.state = 'active' AND b.channel LIKE 'telegram%'
       AND (EXISTS (SELECT 1 FROM channel_outbox o
                     WHERE o.binding_id = b.id AND o.state = 'pending')
            OR EXISTS (SELECT 1 FROM channel_outbox o
                        WHERE o.binding_id = b.id AND o.state = 'ambiguous'
                          AND o.updated_at <= now() - make_interval(secs => p_age)))
       AND NOT EXISTS (SELECT 1 FROM jobs j
                        WHERE j.serialization_key = p_prefix || b.id
                          AND j.state IN ('ready', 'leased'))
     LIMIT p_limit
    RETURNING 1)
  SELECT count(*)::int FROM minted
$$;

COMMENT ON FUNCTION fn_sender_sweep(p_prefix text, p_attempts int, p_deadline numeric, p_age numeric, p_limit int) IS
  'The outbox sender sweep: one deliver_outbox job per pushable Telegram binding with pending (or aged ambiguous) outbox rows and no live sender job, every workspace''s, minted in the one statement whose NOT EXISTS is its idempotence. The worker''s former statement verbatim — the key prefix, the lane budget and the bound are its parameters, spelled once in Python; the binding predicate is bindings.push_binding_where, pinned to this body by a test. A SECURITY DEFINER door owned by svc_maintenance; EXECUTE for svc_worker (082, #751).';

ALTER FUNCTION fn_sender_sweep(p_prefix text, p_attempts int, p_deadline numeric, p_age numeric, p_limit int) OWNER TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_sender_sweep(p_prefix text, p_attempts int, p_deadline numeric, p_age numeric, p_limit int) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_sender_sweep(p_prefix text, p_attempts int, p_deadline numeric, p_age numeric, p_limit int) TO svc_worker;

CREATE FUNCTION fn_prompts_due(p_limit int)
RETURNS TABLE (o_id uuid, o_state text, o_workspace_id uuid, o_schedule_slot_at timestamptz, o_file_name text, o_media_kind text, o_mime_type varchar, o_source_id uuid, o_provider_file_ref text, o_handle varchar, o_tz text, o_api_publishing_enabled boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT i.id, i.state, i.workspace_id, i.schedule_slot_at,
         m.file_name, m.media_kind, m.mime_type,
         m.source_id, m.provider_file_ref, a.handle, w.tz,
         w.api_publishing_enabled
    FROM post_intents i
    JOIN workspaces w ON w.id = i.workspace_id
    JOIN media_items m ON m.id = i.media_item_id
     AND m.workspace_id = i.workspace_id
    LEFT JOIN ig_accounts a ON a.id = i.ig_account_id
     AND a.workspace_id = i.workspace_id
   WHERE i.state = 'scheduled' AND i.schedule_slot_at <= now()
     AND w.state = 'active' AND NOT w.is_paused
   ORDER BY i.schedule_slot_at LIMIT p_limit
$$;

COMMENT ON FUNCTION fn_prompts_due(p_limit int) IS
  'The prompt sweep''s due stories across every workspace — the card''s columns for each scheduled intent whose slot has come in an active, unpaused workspace. A SECURITY DEFINER read owned by svc_maintenance; the prompting itself happens per workspace under that workspace''s tenant. EXECUTE for svc_worker (082, #751).';

ALTER FUNCTION fn_prompts_due(p_limit int) OWNER TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_prompts_due(p_limit int) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_prompts_due(p_limit int) TO svc_worker;

CREATE FUNCTION fn_prompts_pending(p_limit int)
RETURNS TABLE (o_id uuid, o_workspace_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT i.id, i.workspace_id FROM post_intents i
   WHERE i.state = 'prompt_pending'
   LIMIT p_limit
$$;

COMMENT ON FUNCTION fn_prompts_pending(p_limit int) IS
  'The prompt sweep''s second phase across every workspace: intents whose card is out and whose state must advance to awaiting_approval, advanced per workspace under its tenant. Owned by svc_maintenance; EXECUTE for svc_worker (082, #751).';

ALTER FUNCTION fn_prompts_pending(p_limit int) OWNER TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_prompts_pending(p_limit int) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_prompts_pending(p_limit int) TO svc_worker;

CREATE FUNCTION fn_settled_cards(p_terminal text[], p_limit int)
RETURNS TABLE (o_intent_id uuid, o_workspace_id uuid, o_binding_id uuid, o_state text, o_entered_state_at timestamptz, o_tz text, o_since timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT o.intent_id, o.workspace_id, o.binding_id, i.state,
         i.entered_state_at, w.tz, min(o.created_at) AS since
    FROM channel_outbox o
    JOIN channel_bindings b ON b.id = o.binding_id AND b.state = 'active'
    JOIN post_intents i ON i.id = o.intent_id
    JOIN workspaces w ON w.id = i.workspace_id
   WHERE o.kind = 'approval_prompt'
     AND o.state IN ('pending', 'sending', 'sent', 'ambiguous')
     AND i.state = ANY(p_terminal)
   GROUP BY o.intent_id, o.workspace_id, o.binding_id, i.state,
            i.entered_state_at, w.tz
   ORDER BY since
   LIMIT p_limit
$$;

COMMENT ON FUNCTION fn_settled_cards(p_terminal text[], p_limit int) IS
  'The settled-card sweep''s selection across every workspace: live approval cards on active bindings whose story has ended, oldest first — the terminal states are the caller''s argument (intent_ledger.TERMINAL_STATES, spelled once). The settlement read and the supersede happen per row under that row''s workspace. Owned by svc_maintenance; EXECUTE for svc_worker (082, #751).';

ALTER FUNCTION fn_settled_cards(p_terminal text[], p_limit int) OWNER TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_settled_cards(p_terminal text[], p_limit int) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_settled_cards(p_terminal text[], p_limit int) TO svc_worker;

CREATE FUNCTION fn_stranded_sources(p_age numeric, p_limit int)
RETURNS TABLE (o_id uuid, o_workspace_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT id, workspace_id FROM media_sources
   WHERE state = 'error'
     AND (alerted_at IS NULL
          OR alerted_at < now() - make_interval(secs => p_age))
   ORDER BY alerted_at NULLS FIRST
   LIMIT p_limit
$$;

COMMENT ON FUNCTION fn_stranded_sources(p_age numeric, p_limit int) IS
  'The stranded-source alert''s selection across every workspace: sources in error not alerted within the window. The alerted_at stamp and the card are written per workspace under its tenant. Owned by svc_maintenance; EXECUTE for svc_worker (082, #751).';

ALTER FUNCTION fn_stranded_sources(p_age numeric, p_limit int) OWNER TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_stranded_sources(p_age numeric, p_limit int) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_stranded_sources(p_age numeric, p_limit int) TO svc_worker;

REVOKE CREATE ON SCHEMA public FROM svc_maintenance;
```

```sql
-- [§26 the clock's five legs mint with a deadline]

GRANT CREATE ON SCHEMA public TO svc_clock;

DROP FUNCTION fn_clock_tick(int, interval, jsonb);

CREATE FUNCTION fn_clock_tick(p_max int, p_refresh_cadence interval,
                              p_recurring jsonb)  -- {v:1, "<kind>": seconds, …} (05 seam)
RETURNS TABLE (o_slot_jobs int, o_refresh_jobs int, o_sync_jobs int,
               o_recurring_jobs int, o_reauth_jobs int)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE k text; cadence interval; last_done timestamptz; rem int;
        due_at timestamptz;   -- 083: the singleton's run_at, named so its
                              -- deadline can be measured FROM it
        n1 int := 0; n2 int := 0; n3 int := 0; n4 int := 0; n5 int := 0;
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
      -- 083: from run_at, NOT from now(). A singleton's run_at is its last
      -- completion plus its cadence, which for a slow cadence is well ahead of
      -- this tick; a deadline anchored at mint would be spent before the job
      -- was ever claimable and would end it on its first run.
      due_at := GREATEST(now(), COALESCE(last_done + cadence, now()));
      INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, deadline_at, payload)
      VALUES (k, NULL, 'bulk', k,            -- system singletons key on their kind (§5 registry)
              due_at, 3,
              due_at + interval '6 hours',   -- LANE_BUDGETS['bulk'][1]
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
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, deadline_at, payload)
    SELECT 'plan_slot', d.workspace_id, 'bulk', 'acct:' || d.id, now(), 3, now() + interval '6 hours',
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
       AND provider = 'ig_login'   -- 063: see the header. Fails CLOSED for any new provider.
     LIMIT rem
  ), ins AS (
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, deadline_at, payload)
    SELECT 'refresh_credential', d.workspace_id, 'bulk', 'cred:' || d.id, now(), 5, now() + interval '6 hours',
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
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, deadline_at, payload)
    SELECT 'sync_media_source', d.workspace_id, 'bulk', 'src:' || d.id, now(), 5, now() + interval '6 hours',
           jsonb_build_object('v', 1, 'source_id', d.id, 'reason', 'baseline')
      FROM due d
  )
  UPDATE media_sources s SET next_sync_at = NULL                   -- the sync executor re-arms it
    FROM due d WHERE s.id = d.id;
  GET DIAGNOSTICS n3 = ROW_COUNT;
  rem := rem - n3;
  -- (5) reauth prompts for accounts sitting reauth_required (`02` §5 :1165; `05`: 1/week).
  -- Marker stamped at MINT, symmetric with legs 2-4's re-arm-at-mint shape; the NOT EXISTS
  -- guards a still-open prompt job so a slow executor cannot pile up prompts for one account.
  -- ix_ig_accounts_reauth_due serves the scan:
  WITH due AS (
    SELECT a.id, a.workspace_id, a.provider_account_ref
      FROM ig_accounts a
     WHERE a.state = 'reauth_required'
       AND (a.last_reauth_prompt_at IS NULL
            OR a.last_reauth_prompt_at <= now() - interval '7 days')
       AND NOT EXISTS (SELECT 1 FROM jobs j
                        WHERE j.kind = 'reauth_prompt'
                          AND j.serialization_key = 'ig:' || a.provider_account_ref
                          AND j.state IN ('ready','leased'))
     LIMIT rem
  ), ins AS (
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, deadline_at, payload)
    SELECT 'reauth_prompt', d.workspace_id, 'bulk', 'ig:' || d.provider_account_ref, now(), 3, now() + interval '6 hours',
           jsonb_build_object('v', 1, 'ig_account_id', d.id)
      FROM due d
  )
  UPDATE ig_accounts a SET last_reauth_prompt_at = now()
    FROM due d WHERE a.id = d.id;
  GET DIAGNOSTICS n5 = ROW_COUNT;
  RETURN QUERY SELECT n1, n2, n3, n4, n5;
END $$;

COMMENT ON FUNCTION fn_clock_tick(int, interval, jsonb) IS
  'The scheduled clock tick: a SECURITY DEFINER producer of due work, owned by '
  'svc_clock with EXECUTE granted to svc_worker, and pinned to '
  'search_path = pg_catalog, public. One call runs five legs in order - '
  'recurring system singletons, due account slots, due credential refreshes, '
  'due source syncs, and reauth prompts. The legs share one budget: p_max caps '
  'the first, and each later leg draws only on what the ones before it left, so '
  'one call mints at most p_max rows. It is NOT the only writer of jobs - '
  'application services enqueue directly as well. The refresh leg is scoped to '
  'provider ig_login and fails closed: a provider with no refresh door of its '
  'own is skipped rather than minted. Every leg mints with a deadline measured '
  'from the job''s own run_at, so a job the worker never completes is ended by '
  'the reaper rather than held for ever.';

ALTER FUNCTION fn_clock_tick(int, interval, jsonb) OWNER TO svc_clock;

REVOKE CREATE ON SCHEMA public FROM svc_clock;

REVOKE ALL ON FUNCTION fn_clock_tick(int, interval, jsonb) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_clock_tick(int, interval, jsonb) TO svc_worker;
```

```sql
-- [§27 the clock revives its own singletons' expired leases]

GRANT UPDATE (state, locked_by, lease_token, locked_until) ON jobs TO svc_clock;

GRANT CREATE ON SCHEMA public TO svc_clock;

DROP FUNCTION fn_clock_tick(int, interval, jsonb);

CREATE FUNCTION fn_clock_tick(p_max int, p_refresh_cadence interval,
                              p_recurring jsonb)  -- {v:1, "<kind>": seconds, …} (05 seam)
RETURNS TABLE (o_slot_jobs int, o_refresh_jobs int, o_sync_jobs int,
               o_recurring_jobs int, o_reauth_jobs int)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE k text; cadence interval; last_done timestamptz; rem int;
        due_at timestamptz;   -- 083: the singleton's run_at, named so its
                              -- deadline can be measured FROM it
        n1 int := 0; n2 int := 0; n3 int := 0; n4 int := 0; n5 int := 0;
BEGIN
  PERFORM set_config('app.actor_kind', 'clock', true);
  -- (1) recurring system singletons: if no ready/leased row holds the kind's singleton key,
  -- insert the next run at last-completion + cadence (or now, whichever is later):
  FOR k, cadence IN
    SELECT key, (value::text)::numeric * interval '1 second'
      FROM jsonb_each(p_recurring) WHERE key <> 'v'
  LOOP
    EXIT WHEN n4 >= p_max;
    -- 084: this kind's own expired lease goes back to `ready` BEFORE the guard
    -- reads it (#1329). The reaper re-readies every other kind's dead lease,
    -- but it is one of these singletons and cannot revive itself; left
    -- `leased`, a dead lease reads as live to the guard below for ever. The
    -- four assignments are the reaper's first leg's (076), scoped to the kind:
    UPDATE jobs SET state = 'ready', locked_by = NULL, lease_token = NULL, locked_until = NULL
     WHERE kind = k AND workspace_id IS NULL
       AND state = 'leased' AND locked_until < now();
    IF NOT EXISTS (SELECT 1 FROM jobs
                   WHERE kind = k AND state IN ('ready','leased')) THEN
      SELECT max(updated_at) INTO last_done FROM jobs
       WHERE kind = k AND state = 'succeeded';
      -- 083: from run_at, NOT from now(). A singleton's run_at is its last
      -- completion plus its cadence, which for a slow cadence is well ahead of
      -- this tick; a deadline anchored at mint would be spent before the job
      -- was ever claimable and would end it on its first run.
      due_at := GREATEST(now(), COALESCE(last_done + cadence, now()));
      INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, deadline_at, payload)
      VALUES (k, NULL, 'bulk', k,            -- system singletons key on their kind (§5 registry)
              due_at, 3,
              due_at + interval '6 hours',   -- LANE_BUDGETS['bulk'][1]
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
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, deadline_at, payload)
    SELECT 'plan_slot', d.workspace_id, 'bulk', 'acct:' || d.id, now(), 3, now() + interval '6 hours',
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
       AND provider = 'ig_login'   -- 063: see the header. Fails CLOSED for any new provider.
     LIMIT rem
  ), ins AS (
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, deadline_at, payload)
    SELECT 'refresh_credential', d.workspace_id, 'bulk', 'cred:' || d.id, now(), 5, now() + interval '6 hours',
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
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, deadline_at, payload)
    SELECT 'sync_media_source', d.workspace_id, 'bulk', 'src:' || d.id, now(), 5, now() + interval '6 hours',
           jsonb_build_object('v', 1, 'source_id', d.id, 'reason', 'baseline')
      FROM due d
  )
  UPDATE media_sources s SET next_sync_at = NULL                   -- the sync executor re-arms it
    FROM due d WHERE s.id = d.id;
  GET DIAGNOSTICS n3 = ROW_COUNT;
  rem := rem - n3;
  -- (5) reauth prompts for accounts sitting reauth_required (`02` §5 :1165; `05`: 1/week).
  -- Marker stamped at MINT, symmetric with legs 2-4's re-arm-at-mint shape; the NOT EXISTS
  -- guards a still-open prompt job so a slow executor cannot pile up prompts for one account.
  -- ix_ig_accounts_reauth_due serves the scan:
  WITH due AS (
    SELECT a.id, a.workspace_id, a.provider_account_ref
      FROM ig_accounts a
     WHERE a.state = 'reauth_required'
       AND (a.last_reauth_prompt_at IS NULL
            OR a.last_reauth_prompt_at <= now() - interval '7 days')
       AND NOT EXISTS (SELECT 1 FROM jobs j
                        WHERE j.kind = 'reauth_prompt'
                          AND j.serialization_key = 'ig:' || a.provider_account_ref
                          AND j.state IN ('ready','leased'))
     LIMIT rem
  ), ins AS (
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, deadline_at, payload)
    SELECT 'reauth_prompt', d.workspace_id, 'bulk', 'ig:' || d.provider_account_ref, now(), 3, now() + interval '6 hours',
           jsonb_build_object('v', 1, 'ig_account_id', d.id)
      FROM due d
  )
  UPDATE ig_accounts a SET last_reauth_prompt_at = now()
    FROM due d WHERE a.id = d.id;
  GET DIAGNOSTICS n5 = ROW_COUNT;
  RETURN QUERY SELECT n1, n2, n3, n4, n5;
END $$;

COMMENT ON FUNCTION fn_clock_tick(int, interval, jsonb) IS
  'The scheduled clock tick: a SECURITY DEFINER producer of due work, owned by '
  'svc_clock with EXECUTE granted to svc_worker, and pinned to '
  'search_path = pg_catalog, public. One call runs five legs in order - '
  'recurring system singletons, due account slots, due credential refreshes, '
  'due source syncs, and reauth prompts. The legs share one budget: p_max caps '
  'the first, and each later leg draws only on what the ones before it left, so '
  'one call mints at most p_max rows. It is NOT the only writer of jobs - '
  'application services enqueue directly as well. The refresh leg is scoped to '
  'provider ig_login and fails closed: a provider with no refresh door of its '
  'own is skipped rather than minted. Every leg mints with a deadline measured '
  'from the job''s own run_at, so a job the worker never completes is ended by '
  'the reaper rather than held for ever. The recurring leg first returns an '
  'expired lease of its own kind to ready: the reaper re-readies every other '
  'kind''s expired lease, but it is one of these singletons and cannot revive '
  'itself.';

ALTER FUNCTION fn_clock_tick(int, interval, jsonb) OWNER TO svc_clock;

REVOKE CREATE ON SCHEMA public FROM svc_clock;

REVOKE ALL ON FUNCTION fn_clock_tick(int, interval, jsonb) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_clock_tick(int, interval, jsonb) TO svc_worker;
```

```sql
-- [§28 the audit triggers read an empty actor as unset]

CREATE OR REPLACE FUNCTION trg_intent_audit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.state IS DISTINCT FROM OLD.state THEN
    -- 085: '' is unset too. A pooled connection that claimed an actor in an
    -- earlier transaction reads the setting back as '', not NULL (#1421).
    IF NULLIF(current_setting('app.actor_kind', true), '') IS NULL THEN
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

CREATE OR REPLACE FUNCTION trg_governance_audit() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  r    RECORD;
  kind TEXT;
  ws   UUID;
  ent  UUID;
  fs   TEXT;
  ts   TEXT;
BEGIN
  -- 085: '' is unset too. A pooled connection that claimed an actor in an
  -- earlier transaction reads the setting back as '', not NULL (#1421).
  IF NULLIF(current_setting('app.actor_kind', true), '') IS NULL THEN
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
```

```sql
-- [§29 the reaper ends a ready job past its deadline]

GRANT UPDATE (next_sync_at) ON media_sources TO svc_maintenance;

CREATE POLICY p_maint_sources_rearm ON media_sources FOR UPDATE TO svc_maintenance
  USING (true) WITH CHECK (true);

CREATE OR REPLACE FUNCTION fn_reaper_sweep(p_lim int, p_approval_ttl interval, p_approved_ttl interval)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE n int := 0; c int; rem int := GREATEST(p_lim, 0);
BEGIN
  PERFORM set_config('app.actor_kind', 'reaper', true);
  UPDATE jobs SET state = 'ready', locked_by = NULL, lease_token = NULL, locked_until = NULL
   WHERE id IN (SELECT id FROM jobs
                 WHERE state = 'leased' AND locked_until < now() LIMIT rem);
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  UPDATE post_intents SET state = 'expired'
   WHERE id IN (SELECT id FROM post_intents
                 WHERE state IN ('scheduled','prompt_pending') AND schedule_slot_at < now()
                 LIMIT rem);
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  UPDATE post_intents i SET state = 'expired'
   WHERE i.id IN (
     SELECT i2.id FROM post_intents i2 JOIN workspaces w ON w.id = i2.workspace_id
      WHERE i2.state = 'awaiting_approval'
        AND i2.entered_state_at
            < now() - COALESCE(w.approval_ttl_minutes * interval '1 minute', p_approval_ttl)
      LIMIT rem);
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
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  -- 086: a `ready` job past its deadline ends `failed`, and says so (#1429),
  -- for the kinds a sweep re-mints; a sync kind's source is re-armed, as
  -- `work_loop._rearm_source` does on a spent budget. Last, so a backlog of
  -- expired jobs cannot take the budget of the legs above.
  WITH ended AS (
    UPDATE jobs SET state = 'failed',
                    payload = payload || jsonb_build_object('ended', 'deadline')
     WHERE id IN (SELECT id FROM jobs
                   WHERE state = 'ready' AND deadline_at <= now()
                     AND kind IN ('reap_expired', 'reconcile_ambiguous',
                                  'alert_stranded_sources', 'reap_transit_assets',
                                  'plan_slot', 'refresh_credential', 'reauth_prompt',
                                  'deliver_outbox', 'sync_media_source',
                                  'first_ingest_chunk')   -- the kinds a sweep re-mints
                   ORDER BY deadline_at LIMIT rem)
       AND state = 'ready' AND deadline_at <= now()   -- rechecked on a changed row
    RETURNING kind, workspace_id, payload->>'source_id' AS source_id
  ), rearmed AS (
    UPDATE media_sources s
       SET next_sync_at = now() + interval '24 hours'           -- work_loop.REARM_AFTER_SECONDS
      FROM ended e
     WHERE e.kind IN ('sync_media_source', 'first_ingest_chunk')  -- work_loop._SYNC_KINDS
       AND s.id::text = e.source_id   -- as text: a malformed payload must not abort the sweep
       AND s.workspace_id = e.workspace_id
       AND s.state = 'active' AND s.next_sync_at IS NULL
  )
  SELECT count(*) INTO c FROM ended;
  n := n + c;
  RETURN n;
END $$;
```

```sql
-- [§30 the reaper ends a cancel the user asked for]

CREATE OR REPLACE FUNCTION fn_reaper_sweep(p_lim int, p_approval_ttl interval, p_approved_ttl interval)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE n int := 0; c int; rem int := GREATEST(p_lim, 0);
BEGIN
  PERFORM set_config('app.actor_kind', 'reaper', true);
  UPDATE jobs SET state = 'ready', locked_by = NULL, lease_token = NULL, locked_until = NULL
   WHERE id IN (SELECT id FROM jobs
                 WHERE state = 'leased' AND locked_until < now() LIMIT rem);
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  -- 087: a cancel the user asked for ends `cancelled` (#1235), ahead of the
  -- expiry legs; a debited row is left to the paths that refund it first.
  UPDATE post_intents SET state = 'cancelled'
   WHERE id IN (SELECT id FROM post_intents
                 WHERE cancel_requested AND cap_consumed_on IS NULL
                   AND state IN ('scheduled','prompt_pending','awaiting_approval','approved')
                 LIMIT rem)
     AND state IN ('scheduled','prompt_pending','awaiting_approval','approved');  -- rechecked on a changed row
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  UPDATE post_intents SET state = 'expired'
   WHERE id IN (SELECT id FROM post_intents
                 WHERE state IN ('scheduled','prompt_pending') AND schedule_slot_at < now()
                 LIMIT rem);
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  UPDATE post_intents i SET state = 'expired'
   WHERE i.id IN (
     SELECT i2.id FROM post_intents i2 JOIN workspaces w ON w.id = i2.workspace_id
      WHERE i2.state = 'awaiting_approval'
        AND i2.entered_state_at
            < now() - COALESCE(w.approval_ttl_minutes * interval '1 minute', p_approval_ttl)
      LIMIT rem);
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
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  -- 086: a `ready` job past its deadline ends `failed`, and says so (#1429),
  -- for the kinds a sweep re-mints; a sync kind's source is re-armed, as
  -- `work_loop._rearm_source` does on a spent budget. Last, so a backlog of
  -- expired jobs cannot take the budget of the legs above.
  WITH ended AS (
    UPDATE jobs SET state = 'failed',
                    payload = payload || jsonb_build_object('ended', 'deadline')
     WHERE id IN (SELECT id FROM jobs
                   WHERE state = 'ready' AND deadline_at <= now()
                     AND kind IN ('reap_expired', 'reconcile_ambiguous',
                                  'alert_stranded_sources', 'reap_transit_assets',
                                  'plan_slot', 'refresh_credential', 'reauth_prompt',
                                  'deliver_outbox', 'sync_media_source',
                                  'first_ingest_chunk')   -- the kinds a sweep re-mints
                   ORDER BY deadline_at LIMIT rem)
       AND state = 'ready' AND deadline_at <= now()   -- rechecked on a changed row
    RETURNING kind, workspace_id, payload->>'source_id' AS source_id
  ), rearmed AS (
    UPDATE media_sources s
       SET next_sync_at = now() + interval '24 hours'           -- work_loop.REARM_AFTER_SECONDS
      FROM ended e
     WHERE e.kind IN ('sync_media_source', 'first_ingest_chunk')  -- work_loop._SYNC_KINDS
       AND s.id::text = e.source_id   -- as text: a malformed payload must not abort the sweep
       AND s.workspace_id = e.workspace_id
       AND s.state = 'active' AND s.next_sync_at IS NULL
  )
  SELECT count(*) INTO c FROM ended;
  n := n + c;
  RETURN n;
END $$;
```

```sql
-- [§31 the ledger learns 'planned']

ALTER TABLE post_intents ADD COLUMN origin TEXT NOT NULL DEFAULT 'cadence'
  CONSTRAINT ck_intent_origin CHECK (origin IN ('cadence','planned'));

ALTER TABLE post_intents ADD COLUMN scheduled_by_user_id UUID NULL REFERENCES users(id) ON DELETE SET NULL;

ALTER TABLE post_intents ADD CONSTRAINT ck_intent_planned_manual
  CHECK (origin = 'cadence' OR approval_mode = 'manual');

CREATE UNIQUE INDEX uq_intent_slot_cadence ON post_intents (workspace_id, ig_account_id, schedule_slot_at)
  WHERE origin = 'cadence';

CREATE FUNCTION trg_intent_planned_person() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.origin IS DISTINCT FROM OLD.origin THEN
    RAISE EXCEPTION 'post_intent % origin is fixed at birth (% -> %)', OLD.id, OLD.origin, NEW.origin
      USING ERRCODE = 'check_violation';
  END IF;
  -- The trigger's WHEN admits only that change or a planned row moving
  -- awaiting_approval -> approved, so reaching here is the approval itself.
  IF COALESCE(current_setting('app.actor_kind', true), '') <> 'user'
     OR NULLIF(current_setting('app.actor_user_id', true), '') IS NULL THEN
    RAISE EXCEPTION 'post_intent % is planned: only a person approves it (actor %)',
      OLD.id, COALESCE(NULLIF(current_setting('app.actor_kind', true), ''), 'none')
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER tg_intent_planned_person BEFORE UPDATE OF state, origin ON post_intents
  FOR EACH ROW
  WHEN (NEW.origin IS DISTINCT FROM OLD.origin
        OR (OLD.origin = 'planned' AND OLD.state = 'awaiting_approval' AND NEW.state = 'approved'))
  EXECUTE FUNCTION trg_intent_planned_person();
```

```sql
-- [§32 planned stories are served on time or missed out loud]

DROP INDEX uq_intent_slot;

ALTER INDEX uq_intent_slot_cadence RENAME TO uq_intent_slot;

CREATE OR REPLACE FUNCTION fn_reaper_sweep(p_lim int, p_approval_ttl interval, p_approved_ttl interval)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE n int := 0; c int; rem int := GREATEST(p_lim, 0);
BEGIN
  PERFORM set_config('app.actor_kind', 'reaper', true);
  UPDATE jobs SET state = 'ready', locked_by = NULL, lease_token = NULL, locked_until = NULL
   WHERE id IN (SELECT id FROM jobs
                 WHERE state = 'leased' AND locked_until < now() LIMIT rem);
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  -- 087: a cancel the user asked for ends `cancelled` (#1235), ahead of the
  -- expiry legs; a debited row is left to the paths that refund it first.
  UPDATE post_intents SET state = 'cancelled'
   WHERE id IN (SELECT id FROM post_intents
                 WHERE cancel_requested AND cap_consumed_on IS NULL
                   AND state IN ('scheduled','prompt_pending','awaiting_approval','approved')
                 LIMIT rem)
     AND state IN ('scheduled','prompt_pending','awaiting_approval','approved');  -- rechecked on a changed row
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  -- 089: the slot expiry takes cadence rows only (#1413). A planned row
  -- that is due and unserved ends through the miss door
  -- (fn_planned_misses), which tells the bound chats why; ended here, it
  -- would end in silence.
  UPDATE post_intents SET state = 'expired'
   WHERE id IN (SELECT id FROM post_intents
                 WHERE state IN ('scheduled','prompt_pending') AND schedule_slot_at < now()
                   AND origin = 'cadence'
                 LIMIT rem);
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  UPDATE post_intents i SET state = 'expired'
   WHERE i.id IN (
     SELECT i2.id FROM post_intents i2 JOIN workspaces w ON w.id = i2.workspace_id
      WHERE i2.state = 'awaiting_approval'
        AND i2.entered_state_at
            < now() - COALESCE(w.approval_ttl_minutes * interval '1 minute', p_approval_ttl)
      LIMIT rem);
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
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  -- 086: a `ready` job past its deadline ends `failed`, and says so (#1429),
  -- for the kinds a sweep re-mints; a sync kind's source is re-armed, as
  -- `work_loop._rearm_source` does on a spent budget. Last, so a backlog of
  -- expired jobs cannot take the budget of the legs above.
  WITH ended AS (
    UPDATE jobs SET state = 'failed',
                    payload = payload || jsonb_build_object('ended', 'deadline')
     WHERE id IN (SELECT id FROM jobs
                   WHERE state = 'ready' AND deadline_at <= now()
                     AND kind IN ('reap_expired', 'reconcile_ambiguous',
                                  'alert_stranded_sources', 'reap_transit_assets',
                                  'plan_slot', 'refresh_credential', 'reauth_prompt',
                                  'deliver_outbox', 'sync_media_source',
                                  'first_ingest_chunk')   -- the kinds a sweep re-mints
                   ORDER BY deadline_at LIMIT rem)
       AND state = 'ready' AND deadline_at <= now()   -- rechecked on a changed row
    RETURNING kind, workspace_id, payload->>'source_id' AS source_id
  ), rearmed AS (
    UPDATE media_sources s
       SET next_sync_at = now() + interval '24 hours'           -- work_loop.REARM_AFTER_SECONDS
      FROM ended e
     WHERE e.kind IN ('sync_media_source', 'first_ingest_chunk')  -- work_loop._SYNC_KINDS
       AND s.id::text = e.source_id   -- as text: a malformed payload must not abort the sweep
       AND s.workspace_id = e.workspace_id
       AND s.state = 'active' AND s.next_sync_at IS NULL
  )
  SELECT count(*) INTO c FROM ended;
  n := n + c;
  RETURN n;
END $$;

GRANT CREATE ON SCHEMA public TO svc_maintenance;

DROP FUNCTION fn_prompts_due(int);

CREATE FUNCTION fn_prompts_due(p_limit int, p_late interval DEFAULT NULL)
RETURNS TABLE (o_id uuid, o_state text, o_workspace_id uuid, o_schedule_slot_at timestamptz, o_file_name text, o_media_kind text, o_mime_type varchar, o_source_id uuid, o_provider_file_ref text, o_handle varchar, o_tz text, o_api_publishing_enabled boolean, o_origin text, o_scheduled_by_user_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT i.id, i.state, i.workspace_id, i.schedule_slot_at,
         m.file_name, m.media_kind, m.mime_type,
         m.source_id, m.provider_file_ref, a.handle, w.tz,
         w.api_publishing_enabled, i.origin, i.scheduled_by_user_id
    FROM post_intents i
    JOIN workspaces w ON w.id = i.workspace_id
    JOIN media_items m ON m.id = i.media_item_id
     AND m.workspace_id = i.workspace_id
    LEFT JOIN ig_accounts a ON a.id = i.ig_account_id
     AND a.workspace_id = i.workspace_id
   WHERE i.state = 'scheduled' AND i.schedule_slot_at <= now()
     AND NOT i.cancel_requested
     AND w.state = 'active' AND NOT w.is_paused
     AND (i.origin = 'cadence'
          OR (i.schedule_slot_at > now() - p_late
              AND m.state = 'available'
              AND a.state IN ('active', 'reauth_required')
              AND NOT EXISTS (SELECT 1 FROM post_locks l
                               WHERE l.workspace_id = i.workspace_id
                                 AND l.ig_account_id IS NULL
                                 AND l.media_item_id = i.media_item_id
                                 AND l.kind IN ('reject', 'unsupported', 'hold', 'seasonal')
                                 AND (l.expires_at IS NULL OR l.expires_at > now()))))
   ORDER BY i.schedule_slot_at LIMIT p_limit
$$;

COMMENT ON FUNCTION fn_prompts_due(p_limit int, p_late interval) IS
  'The prompt sweep''s due stories across every workspace — the card''s columns, origin and scheduler for each scheduled, unflagged intent whose time has come in an active, unpaused workspace: every cadence row, and a planned row only while it can be served (its media available, its account active or awaiting reconnection, no live reject, unsupported, hold or seasonal lock) and within p_late of its time. A NULL p_late, the default, serves no planned row. fn_planned_misses lists the complement. A SECURITY DEFINER read owned by svc_maintenance; the prompting happens per workspace under that workspace''s tenant. EXECUTE for svc_worker (082, #751; 089, #1413).';

ALTER FUNCTION fn_prompts_due(p_limit int, p_late interval) OWNER TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_prompts_due(p_limit int, p_late interval) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_prompts_due(p_limit int, p_late interval) TO svc_worker;

CREATE FUNCTION fn_planned_misses(p_limit int, p_late interval)
RETURNS TABLE (o_id uuid, o_workspace_id uuid, o_reason text, o_schedule_slot_at timestamptz, o_file_name text, o_handle varchar, o_tz text, o_scheduled_by_user_id uuid)
LANGUAGE sql STABLE STRICT SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT d.id, d.workspace_id, d.reason, d.schedule_slot_at, d.file_name, d.handle, d.tz,
         d.scheduled_by_user_id
    FROM (SELECT i.id, i.workspace_id, i.schedule_slot_at, i.scheduled_by_user_id,
                 m.file_name, a.handle, w.tz,
                 CASE
                   WHEN m.id IS NULL OR m.state = 'removed' THEN 'item_removed'
                   WHEN m.state = 'unsupported' THEN 'item_unsupported'
                   WHEN EXISTS (SELECT 1 FROM post_locks l
                                 WHERE l.workspace_id = i.workspace_id
                                   AND l.ig_account_id IS NULL
                                   AND l.media_item_id = i.media_item_id
                                   AND l.kind IN ('reject', 'unsupported', 'hold', 'seasonal')
                                   AND (l.expires_at IS NULL OR l.expires_at > now()))
                     THEN 'item_locked'
                   WHEN a.state IS NULL OR a.state NOT IN ('active', 'reauth_required')
                     THEN 'account_removed'
                   WHEN i.schedule_slot_at > now() - p_late THEN NULL
                   WHEN w.state <> 'active' OR w.is_paused THEN 'paused'
                   ELSE 'late'
                 END AS reason
            FROM post_intents i
            JOIN workspaces w ON w.id = i.workspace_id
            LEFT JOIN media_items m ON m.id = i.media_item_id
             AND m.workspace_id = i.workspace_id
            LEFT JOIN ig_accounts a ON a.id = i.ig_account_id
             AND a.workspace_id = i.workspace_id
           WHERE i.state = 'scheduled' AND i.origin = 'planned'
             AND i.schedule_slot_at <= now() AND NOT i.cancel_requested) d
   WHERE d.reason IS NOT NULL
   ORDER BY d.schedule_slot_at LIMIT p_limit
$$;

COMMENT ON FUNCTION fn_planned_misses(p_limit int, p_late interval) IS
  'The planned stories that will not be served, across every workspace: each due, scheduled, unflagged planned intent that cannot be served (item_removed, item_unsupported, item_locked, account_removed, in that precedence) or whose p_late window has passed (paused when its workspace was not taking posts, else late) — the complement of fn_prompts_due''s planned rows. The worker expires each with its reason and tells the bound chats in the same transaction, per workspace under that workspace''s tenant. STRICT: a NULL window lists nothing. A SECURITY DEFINER read owned by svc_maintenance; EXECUTE for svc_worker (089, #1413).';

ALTER FUNCTION fn_planned_misses(p_limit int, p_late interval) OWNER TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_planned_misses(p_limit int, p_late interval) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_planned_misses(p_limit int, p_late interval) TO svc_worker;

REVOKE CREATE ON SCHEMA public FROM svc_maintenance;
```

```sql
-- [§33 a removal sticks, and a removed admin's tokens go with them]

CREATE TABLE workspace_member_removals (
  workspace_id       uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  user_id            uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  removed_by_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
  removed_at         timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, user_id)
);

ALTER TABLE workspace_member_removals ENABLE ROW LEVEL SECURITY;

GRANT SELECT ON workspace_member_removals TO svc_ingress, svc_worker;
GRANT SELECT, INSERT, UPDATE ON workspace_member_removals TO svc_membership;

CREATE POLICY p_tenant_read ON workspace_member_removals FOR SELECT TO svc_ingress, svc_worker
  USING (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
CREATE POLICY p_member_removals ON workspace_member_removals FOR ALL TO svc_membership
  USING (true) WITH CHECK (true);

ALTER TABLE service_tokens
  ADD COLUMN created_by_user_id uuid REFERENCES users(id) ON DELETE SET NULL;

GRANT CREATE ON SCHEMA public TO svc_membership;

CREATE OR REPLACE FUNCTION fn_group_member_seen(p_channel text, p_external_ref text, p_user uuid)
RETURNS TABLE (o_workspace_id uuid, o_outcome text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
  v_ws uuid;
  v_state text;
  v_ws_state text;
  v_inserted int;
BEGIN
  SELECT b.workspace_id, b.state INTO v_ws, v_state
    FROM channel_bindings b
   WHERE b.channel = p_channel AND b.external_ref = p_external_ref;
  IF v_ws IS NULL THEN
    RETURN QUERY SELECT NULL::uuid, 'unbound_chat'::text; RETURN;
  END IF;
  IF v_state <> 'active' THEN
    RETURN QUERY SELECT v_ws, 'revoked_chat'::text; RETURN;
  END IF;
  SELECT w.state INTO v_ws_state FROM workspaces w WHERE w.id = v_ws;
  IF v_ws_state IS DISTINCT FROM 'active' THEN
    RETURN QUERY SELECT v_ws, 'workspace_inactive'::text; RETURN;
  END IF;
  IF EXISTS (SELECT 1 FROM workspace_member_removals r
              WHERE r.workspace_id = v_ws AND r.user_id = p_user)
     AND NOT EXISTS (SELECT 1 FROM workspace_members m
                      WHERE m.workspace_id = v_ws AND m.user_id = p_user) THEN
    RETURN QUERY SELECT v_ws, 'removed'::text; RETURN;
  END IF;
  PERFORM set_config('app.actor_kind', 'user', true);
  PERFORM set_config('app.actor_user_id', p_user::text, true);
  PERFORM set_config('app.channel', 'telegram', true);
  BEGIN
    INSERT INTO workspace_members (workspace_id, user_id, role)
    VALUES (v_ws, p_user, 'member')
    ON CONFLICT (workspace_id, user_id) DO NOTHING;
    GET DIAGNOSTICS v_inserted = ROW_COUNT;
  EXCEPTION WHEN foreign_key_violation THEN
    RETURN QUERY SELECT v_ws, 'unknown_user'::text; RETURN;
  END;
  RETURN QUERY SELECT v_ws, CASE WHEN v_inserted > 0 THEN 'joined' ELSE 'already_member' END;
END $$;

COMMENT ON FUNCTION fn_group_member_seen(text, text, uuid) IS
  'The 06 Telegram join path: a person seen in a bound, active group becomes a member (role '
  'member; never a downgrade), unless an admin removed them and they have not been invited back '
  '(090). p_user is the caller''s resolution of the sender''s linked Telegram identity — the '
  'caller proves the person, this door trusts it. Sets the actor GUCs transaction-locally, so it '
  'must be the last governance write in its transaction. SECURITY DEFINER owned by '
  'svc_membership with EXECUTE granted to svc_ingress. Outcomes: joined, already_member, '
  'removed, unbound_chat, revoked_chat, workspace_inactive, unknown_user.';

CREATE OR REPLACE FUNCTION fn_member_remove(p_workspace uuid, p_user uuid, p_by_user uuid)
RETURNS TABLE (o_outcome text, o_role text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  v_role text;
BEGIN
  IF p_user = p_by_user THEN
    RETURN QUERY SELECT 'self'::text, NULL::text; RETURN;
  END IF;
  SELECT m.role INTO v_role FROM workspace_members m
   WHERE m.workspace_id = p_workspace AND m.user_id = p_user;
  IF v_role IS NULL THEN
    RETURN QUERY SELECT 'not_found'::text, NULL::text; RETURN;
  END IF;
  IF v_role = 'owner' THEN
    RETURN QUERY SELECT 'owner'::text, v_role; RETURN;
  END IF;
  DELETE FROM workspace_members m WHERE m.workspace_id = p_workspace AND m.user_id = p_user;
  INSERT INTO workspace_member_removals (workspace_id, user_id, removed_by_user_id)
  VALUES (p_workspace, p_user, p_by_user)
  ON CONFLICT (workspace_id, user_id)
  DO UPDATE SET removed_by_user_id = EXCLUDED.removed_by_user_id, removed_at = now();
  RETURN QUERY SELECT 'removed'::text, v_role;
END $$;

COMMENT ON FUNCTION fn_member_remove(uuid, uuid, uuid) IS
  'The revoke for every join edge (06): an admin removes a member explicitly, and the removal is '
  'recorded so the Telegram join path cannot undo it (090). The one DELETE on workspace_members '
  'in the system lives here (057: no login role deletes). p_by_user is the command port''s actor, '
  'already held to the admin floor — the caller proves the admin. Outcomes: removed, not_found, '
  'owner (never removable here), self (never through this door). SECURITY DEFINER owned by '
  'svc_membership with EXECUTE granted to svc_ingress.';

REVOKE CREATE ON SCHEMA public FROM svc_membership;
```

```sql
-- [§34 the Drive grant records who granted it]

ALTER TABLE oauth_credentials
  ADD COLUMN granted_by_user_id uuid REFERENCES users(id) ON DELETE SET NULL;
```

```sql
-- [§35 a new account needs a way in while in beta]

CREATE TABLE signup_admissions (
  email       text PRIMARY KEY CONSTRAINT ck_signup_admissions_email CHECK (
                email = lower(email) AND email ~ '^[^[:space:]@]+@[^[:space:]@]+$'
                AND email !~ '[\u0080-\u00a0\u00ad\u180e\u2000-\u200f\u2028-\u202f\u205f-\u2064\u3000\ufeff]'),
  admitted_at timestamptz NOT NULL DEFAULT now(),
  note        text
);

ALTER TABLE signup_admissions ENABLE ROW LEVEL SECURITY;

GRANT SELECT ON signup_admissions TO svc_membership;

CREATE POLICY p_member_admissions ON signup_admissions FOR SELECT TO svc_membership USING (true);

GRANT CREATE ON SCHEMA public TO svc_membership;

CREATE FUNCTION fn_signup_admitted(p_email text)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT EXISTS (SELECT 1 FROM signup_admissions a WHERE a.email = lower(p_email))
      OR EXISTS (SELECT 1 FROM workspace_invitations i
                  WHERE lower(i.email) = lower(p_email)
                    AND i.state = 'pending' AND i.expires_at > now())
$$;

COMMENT ON FUNCTION fn_signup_admitted(text) IS
  'May a new Google account with this verified email create its user (092)? True when the owner '
  'admitted the address (signup_admissions) or a pending, unexpired invitation is addressed to it; '
  'false for NULL. Compares lower() on both sides. Answers one boolean, never which workspace '
  'invited the address. SECURITY DEFINER owned by svc_membership with EXECUTE granted to '
  'svc_ingress.';

ALTER FUNCTION fn_signup_admitted(text) OWNER TO svc_membership;

REVOKE CREATE ON SCHEMA public FROM svc_membership;

REVOKE ALL ON FUNCTION fn_signup_admitted(text) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_signup_admitted(text) TO svc_ingress;
```

```sql
-- [§41 an invitation admits only while it is legitimate]

GRANT SELECT (id, state) ON users TO svc_membership;

CREATE POLICY p_member_users ON users FOR SELECT TO svc_membership USING (true);

UPDATE workspace_invitations i SET state = 'revoked'
 WHERE i.state = 'pending'
   AND (EXISTS (SELECT 1 FROM workspace_member_removals r
                 WHERE r.workspace_id = i.workspace_id AND r.user_id = i.invited_by_user_id
                   AND NOT EXISTS (SELECT 1 FROM workspace_members m
                                    WHERE m.workspace_id = i.workspace_id
                                      AND m.user_id = i.invited_by_user_id))
        OR EXISTS (SELECT 1 FROM workspace_member_removals r
                     JOIN users u ON u.id = r.user_id
                    WHERE r.workspace_id = i.workspace_id AND r.removed_at >= i.created_at
                      AND (lower(u.primary_email) = lower(i.email)
                           OR i.invited_tg_user_id::text IN
                              (SELECT x.external_id FROM user_identities x
                                WHERE x.user_id = r.user_id AND x.provider = 'telegram'))));

GRANT CREATE ON SCHEMA public TO svc_membership;

CREATE OR REPLACE FUNCTION fn_signup_admitted(p_email text)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT EXISTS (SELECT 1 FROM signup_admissions a WHERE a.email = lower(p_email))
      OR EXISTS (SELECT 1 FROM workspace_invitations i
                   JOIN workspaces w ON w.id = i.workspace_id AND w.state = 'active'
                   JOIN workspace_members m ON m.workspace_id = i.workspace_id
                                           AND m.user_id = i.invited_by_user_id
                                           AND m.role IN ('owner', 'admin')
                   JOIN users u ON u.id = i.invited_by_user_id AND u.state = 'active'
                  WHERE lower(i.email) = lower(p_email)
                    AND i.state = 'pending' AND i.expires_at > now())
$$;

COMMENT ON FUNCTION fn_signup_admitted(text) IS
  'May a new Google account with this verified email create its user (092)? True when the owner '
  'admitted the address (signup_admissions) or a pending, unexpired invitation is addressed to it '
  'from an active workspace by someone still its owner or an admin, with an active account (098); '
  'false for NULL. Compares lower() on both sides. Answers one boolean, never which workspace '
  'invited the address. SECURITY DEFINER owned by svc_membership with EXECUTE granted to '
  'svc_ingress.';

CREATE OR REPLACE FUNCTION fn_invitation_accept(p_token_hash text, p_user uuid, p_provider text,
                                                p_verified_email text, p_tg_user_id bigint,
                                                p_channel text)
RETURNS TABLE (o_workspace_id uuid, o_granted_role text, o_matched boolean)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
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
  IF NOT EXISTS (SELECT 1 FROM workspaces w
                     WHERE w.id = inv.workspace_id AND w.state = 'active')
     OR NOT EXISTS (SELECT 1 FROM workspace_members a
                      JOIN users u ON u.id = a.user_id AND u.state = 'active'
                     WHERE a.workspace_id = inv.workspace_id
                       AND a.user_id = inv.invited_by_user_id
                       AND a.role IN ('owner', 'admin'))
     OR EXISTS (SELECT 1 FROM workspace_member_removals r
                 WHERE r.workspace_id = inv.workspace_id AND r.user_id = p_user
                   AND r.removed_at >= inv.created_at) THEN
    RAISE EXCEPTION 'invitation not acceptable (no longer legitimate)'
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
  -- The removal check above read without a lock: a removal that committed while this INSERT
  -- waited on the member row it deleted would be undone. Under READ COMMITTED each statement
  -- takes a fresh snapshot, so read it again now and refuse the whole acceptance if it landed.
  IF EXISTS (SELECT 1 FROM workspace_member_removals r
              WHERE r.workspace_id = inv.workspace_id AND r.user_id = p_user
                AND r.removed_at >= inv.created_at) THEN
    RAISE EXCEPTION 'invitation not acceptable (no longer legitimate)'
      USING ERRCODE = 'no_data_found';
  END IF;
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

COMMENT ON FUNCTION fn_invitation_accept(text, uuid, text, text, bigint, text) IS
  'The pre-membership door (059): accept a pending, unexpired invitation by its token hash, with '
  'the D33 identity proof computed here. Refuses (no_data_found) an invitation that is no longer '
  'legitimate (098): its workspace is not active, its inviter is no longer an owner or admin '
  'there with an active account, or the acceptor was removed from the workspace after it was '
  'sent. SECURITY DEFINER owned by svc_membership with EXECUTE granted to svc_ingress.';

REVOKE CREATE ON SCHEMA public FROM svc_membership;
```

```sql
-- [§42 a person can unlink their own Telegram identity]

GRANT SELECT, DELETE ON user_identities TO svc_membership;

CREATE POLICY p_member_identities ON user_identities FOR ALL TO svc_membership
  USING (true) WITH CHECK (true);

GRANT CREATE ON SCHEMA public TO svc_membership;

CREATE FUNCTION fn_identity_unlink(p_user uuid, p_provider text)
RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  v_deleted int;
BEGIN
  IF p_provider IS DISTINCT FROM 'telegram' THEN
    RAISE EXCEPTION 'only a telegram identity is unlinked here, not %', p_provider
      USING ERRCODE = 'invalid_parameter_value';
  END IF;
  DELETE FROM user_identities i
   WHERE i.user_id = p_user AND i.provider = p_provider
     AND EXISTS (SELECT 1 FROM user_identities o
                  WHERE o.user_id = p_user AND o.provider <> p_provider);
  GET DIAGNOSTICS v_deleted = ROW_COUNT;
  IF v_deleted > 0 THEN
    RETURN 'unlinked';
  END IF;
  IF EXISTS (SELECT 1 FROM user_identities i
              WHERE i.user_id = p_user AND i.provider = p_provider) THEN
    RETURN 'last_identity';
  END IF;
  RETURN 'not_linked';
END $$;

COMMENT ON FUNCTION fn_identity_unlink(uuid, text) IS
  'A person unlinks their own Telegram identity (099). Removes the user''s telegram row in '
  'user_identities only while the user keeps another identity; refuses any other provider by '
  'raising. Memberships are untouched. p_user is the caller''s session user — the caller proves '
  'the person, this door trusts it. Outcomes: unlinked, not_linked, last_identity. SECURITY '
  'DEFINER owned by svc_membership with EXECUTE granted to svc_ingress.';

ALTER FUNCTION fn_identity_unlink(uuid, text) OWNER TO svc_membership;

REVOKE CREATE ON SCHEMA public FROM svc_membership;

REVOKE ALL ON FUNCTION fn_identity_unlink(uuid, text) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_identity_unlink(uuid, text) TO svc_ingress;
```

```sql
-- [§43 the waitlist is written by the API, not by the site]

CREATE TABLE waitlist_entries (
  email     text PRIMARY KEY CONSTRAINT ck_waitlist_entries_email CHECK (
              email = lower(email) AND length(email) <= 254
              AND email ~ '^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$'
              AND email !~ '[[:cntrl:]]'
              AND email !~ '[\u061c\u2066-\u2069\ufff9-\ufffb\U000e0000-\U000e007f]'
              AND email !~ '[\u0080-\u00a0\u00ad\u180e\u2000-\u200f\u2028-\u202f\u205f-\u2064\u3000\ufeff]'),
  joined_at timestamptz NOT NULL DEFAULT now(),
  utm       jsonb CONSTRAINT ck_waitlist_entries_utm CHECK (
              utm IS NULL OR (jsonb_typeof(utm) = 'object' AND length(utm::text) <= 2048))
);

ALTER TABLE waitlist_entries ENABLE ROW LEVEL SECURITY;

GRANT INSERT ON waitlist_entries TO svc_ingress;

CREATE POLICY p_ingress_waitlist ON waitlist_entries FOR INSERT TO svc_ingress WITH CHECK (true);
```

```sql
-- [§44 the outbox records why a delivery failed]

ALTER TABLE channel_outbox ADD COLUMN last_failure_class TEXT NULL
  CONSTRAINT ck_outbox_failure_class
  CHECK (last_failure_class IN ('rate_limited','destination_gone','refused','credential_dead','ambiguous'));

ALTER TABLE channel_outbox ADD COLUMN last_error_code INTEGER NULL;

ALTER TABLE channel_outbox ADD COLUMN last_failed_at TIMESTAMPTZ NULL;

CREATE INDEX ix_outbox_last_failed ON channel_outbox (last_failed_at) WHERE last_failed_at IS NOT NULL;

GRANT CREATE ON SCHEMA public TO svc_maintenance;

CREATE FUNCTION fn_health_outbox_failures(p_window_seconds integer)
RETURNS TABLE (o_failure_class text, o_error_code integer, o_rows bigint, o_alerting_rows bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT last_failure_class, last_error_code, count(*),
         count(*) FILTER (WHERE state IN ('failed', 'ambiguous'))
    FROM channel_outbox
   WHERE last_failed_at >= now() - make_interval(secs => LEAST(GREATEST(p_window_seconds, 60), 86400))
   GROUP BY last_failure_class, last_error_code
$$;

COMMENT ON FUNCTION fn_health_outbox_failures(p_window_seconds integer) IS
  'Outbox rows whose last failure falls in the window, every tenant''s included, by class and code; o_alerting_rows ended failed or sit ambiguous. Counts only. Window clamped to [60 s, 24 h]. Owned by svc_maintenance; EXECUTE for svc_ingress and svc_worker (101, #1482).';

ALTER FUNCTION fn_health_outbox_failures(p_window_seconds integer) OWNER TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_health_outbox_failures(p_window_seconds integer) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_health_outbox_failures(p_window_seconds integer) TO svc_ingress, svc_worker;

CREATE FUNCTION fn_health_outbox_sent(p_window_seconds integer)
RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT count(*) FROM channel_outbox
   WHERE state = 'sent'
     AND updated_at >= now() - make_interval(secs => LEAST(GREATEST(p_window_seconds, 60), 86400))
$$;

COMMENT ON FUNCTION fn_health_outbox_sent(p_window_seconds integer) IS
  'Outbox rows sent in the window, every tenant''s included: the traffic the failure count is read against. Window clamped to [60 s, 24 h]. Owned by svc_maintenance; EXECUTE for svc_ingress and svc_worker (101, #1482).';

ALTER FUNCTION fn_health_outbox_sent(p_window_seconds integer) OWNER TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_health_outbox_sent(p_window_seconds integer) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_health_outbox_sent(p_window_seconds integer) TO svc_ingress, svc_worker;

REVOKE CREATE ON SCHEMA public FROM svc_maintenance;
```

```sql
-- [§45 a removal holds]

REVOKE SELECT ON workspace_member_removals FROM svc_ingress, svc_worker;

DROP POLICY p_tenant_read ON workspace_member_removals;

INSERT INTO workspace_member_removals (workspace_id, user_id, removed_by_user_id, removed_at)
SELECT latest.workspace_id, latest.entity_id, u.id, latest.created_at
  FROM (SELECT DISTINCT ON (e.workspace_id, e.entity_id)
               e.workspace_id, e.entity_id, e.actor_kind, e.actor_user_id, e.created_at
          FROM audit_events e
         WHERE e.entity_kind = 'member'
           AND e.detail ->> 'op' = 'DELETE'
         ORDER BY e.workspace_id, e.entity_id, e.created_at DESC, e.id DESC) latest
  LEFT JOIN users u ON u.id = latest.actor_user_id
 WHERE latest.actor_kind = 'user'
   AND latest.actor_user_id IS DISTINCT FROM latest.entity_id
   AND EXISTS (SELECT 1 FROM workspaces w WHERE w.id = latest.workspace_id)
   AND EXISTS (SELECT 1 FROM users p WHERE p.id = latest.entity_id)
   AND NOT EXISTS (SELECT 1 FROM workspace_members m
                    WHERE m.workspace_id = latest.workspace_id AND m.user_id = latest.entity_id)
ON CONFLICT (workspace_id, user_id) DO NOTHING;

UPDATE workspace_invitations i SET state = 'revoked'
 WHERE i.state = 'pending'
   AND (EXISTS (SELECT 1 FROM workspace_member_removals r
                 WHERE r.workspace_id = i.workspace_id AND r.user_id = i.invited_by_user_id
                   AND NOT EXISTS (SELECT 1 FROM workspace_members m
                                    WHERE m.workspace_id = i.workspace_id
                                      AND m.user_id = i.invited_by_user_id))
        OR EXISTS (SELECT 1 FROM workspace_member_removals r
                     JOIN users u ON u.id = r.user_id
                    WHERE r.workspace_id = i.workspace_id AND r.removed_at >= i.created_at
                      AND (lower(u.primary_email) = lower(i.email)
                           OR i.invited_tg_user_id::text IN
                              (SELECT x.external_id FROM user_identities x
                                WHERE x.user_id = r.user_id AND x.provider = 'telegram'))));

GRANT CREATE ON SCHEMA public TO svc_membership;

ALTER FUNCTION fn_group_member_seen(text, text, uuid)
  SET search_path = pg_catalog, public, pg_temp;

CREATE OR REPLACE FUNCTION fn_member_remove(p_workspace uuid, p_user uuid, p_by_user uuid)
RETURNS TABLE (o_outcome text, o_role text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  v_role text;
BEGIN
  IF p_workspace IS DISTINCT FROM NULLIF(current_setting('app.tenant_id', true), '')::uuid
     OR NOT EXISTS (SELECT 1 FROM workspace_members a
                     WHERE a.workspace_id = p_workspace AND a.user_id = p_by_user
                       AND a.role IN ('owner', 'admin')) THEN
    RAISE EXCEPTION 'member removal outside the caller''s admin scope';
  END IF;
  IF p_user = p_by_user THEN
    RETURN QUERY SELECT 'self'::text, NULL::text; RETURN;
  END IF;
  SELECT m.role INTO v_role FROM workspace_members m
   WHERE m.workspace_id = p_workspace AND m.user_id = p_user;
  IF v_role IS NULL THEN
    RETURN QUERY SELECT 'not_found'::text, NULL::text; RETURN;
  END IF;
  IF v_role = 'owner' THEN
    RETURN QUERY SELECT 'owner'::text, v_role; RETURN;
  END IF;
  DELETE FROM workspace_members m WHERE m.workspace_id = p_workspace AND m.user_id = p_user;
  INSERT INTO workspace_member_removals (workspace_id, user_id, removed_by_user_id)
  VALUES (p_workspace, p_user, p_by_user)
  ON CONFLICT (workspace_id, user_id)
  DO UPDATE SET removed_by_user_id = EXCLUDED.removed_by_user_id, removed_at = now();
  RETURN QUERY SELECT 'removed'::text, v_role;
END $$;

COMMENT ON FUNCTION fn_member_remove(uuid, uuid, uuid) IS
  'The revoke for every join edge (06): an admin removes a member explicitly, and the removal is '
  'recorded so the Telegram join path cannot undo it (090). The one DELETE on workspace_members '
  'in the system lives here (057: no login role deletes). The door checks its caller itself '
  '(102): p_workspace must be the claimed tenant (app.tenant_id) and p_by_user an owner or admin '
  'of it, or it raises. Outcomes: removed, not_found, owner (never removable here), self (never '
  'through this door). SECURITY DEFINER owned by svc_membership with EXECUTE granted to '
  'svc_ingress.';

REVOKE CREATE ON SCHEMA public FROM svc_membership;
```

```sql
ALTER FUNCTION fn_auth_plane_sweep(interval, interval, interval, integer) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_claim_job(text, text, interval, integer) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_clock_tick(integer, interval, jsonb) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_extend_leases(uuid[], interval) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_group_member_seen(text, text, uuid) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_health_destinations() SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_health_oldest_tenant_wait() SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_health_oldest_tenant_wait_named() SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_health_outbox_failures(integer) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_health_outbox_pending() SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_health_outbox_sent(integer) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_health_posting_freshness() SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_health_publish_attempts() SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_health_ready_lanes() SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_health_scheduling_lag() SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_identity_unlink(uuid, text) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_invitation_accept(text, uuid, text, text, bigint, text) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_member_remove(uuid, uuid, uuid) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_memberships_for_caller() SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_meta_accounts_for_ref(text) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_offboard_finalize(uuid, interval) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_planned_misses(integer, interval) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_prompts_due(integer, interval) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_prompts_pending(integer) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_reaper_stale_approved(interval, integer) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_reaper_sweep(integer, interval, interval) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_reconciler_sweep(integer, interval[], interval) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_resolve_binding(text, text) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_retention_batch(text, interval, integer) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_sender_sweep(text, integer, numeric, numeric, integer) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_settled_cards(text[], integer) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_signup_admitted(text) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_stranded_sources(numeric, integer) SET search_path = pg_catalog, public, pg_temp;
```

```sql
-- [§47 the activation funnel door: how far the people who signed up got, counted across every workspace through svc_maintenance]
-- How many people who signed up since p_since created a workspace, connected Instagram, added a
-- folder and approved a first story, and how many stalled at each stage: a read across every
-- workspace that no runtime login can make: the tenant-scoped tables it joins hide other tenants' rows.
-- One definer door, counts only. svc_maintenance lacked users and workspace_members (SELECT, and a
-- USING (true) policy each); EXECUTE for svc_worker alone, the login the psql escape hatch connects
-- as, and for no API principal until an operator principal exists (#1124). The CREATE bracket is
-- 062's.
GRANT SELECT ON users TO svc_maintenance;

GRANT SELECT ON workspace_members TO svc_maintenance;

CREATE POLICY p_maint_users ON users FOR SELECT TO svc_maintenance USING (true);

CREATE POLICY p_maint_members ON workspace_members FOR SELECT TO svc_maintenance USING (true);

GRANT CREATE ON SCHEMA public TO svc_maintenance;

CREATE FUNCTION fn_activation_funnel(p_since timestamptz, p_stall interval DEFAULT interval '72 hours')
RETURNS TABLE (o_ordinal int, o_stage text, o_reached bigint, o_stalled bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  WITH owned_ws AS (
    SELECT m.user_id, w.id AS workspace_id, w.created_at
      FROM workspace_members m
      JOIN workspaces w ON w.id = m.workspace_id
     WHERE m.role = 'owner' AND w.state = 'active'
  ), stages AS (
    SELECT u.created_at AS s1,
           (SELECT min(o.created_at) FROM owned_ws o WHERE o.user_id = u.id) AS s2,
           (SELECT min(a.created_at) FROM owned_ws o
              JOIN ig_accounts a ON a.workspace_id = o.workspace_id
             WHERE o.user_id = u.id) AS s3,
           (SELECT min(src.created_at) FROM owned_ws o
              JOIN media_sources src ON src.workspace_id = o.workspace_id
             WHERE o.user_id = u.id) AS s4,
           (SELECT min(e.created_at) FROM owned_ws o
              JOIN audit_events e ON e.workspace_id = o.workspace_id
             WHERE o.user_id = u.id
               AND e.entity_kind = 'post_intent' AND e.detail IS NULL
               AND e.from_state = 'awaiting_approval'
               AND e.to_state IN ('approved', 'posted')) AS s5,
           EXISTS (SELECT 1 FROM workspace_members m
                     JOIN workspaces w ON w.id = m.workspace_id
                    WHERE m.user_id = u.id AND m.role <> 'owner'
                      AND w.state = 'active') AS joined
      FROM users u
     WHERE u.state = 'active' AND u.created_at >= p_since
  ), placed AS (
    SELECT s2, s3, s4, s5,
           CASE WHEN s2 IS NULL AND joined THEN NULL
                WHEN s2 IS NULL THEN 2
                WHEN s3 IS NULL THEN 3
                WHEN s4 IS NULL THEN 4
                WHEN s5 IS NULL THEN 5
           END AS first_missing,
           GREATEST(s1, s2, s3, s4, s5) < now() - p_stall AS idle
      FROM stages
  ), totals AS (
    SELECT count(*) AS r1, count(s2) AS r2, count(s3) AS r3, count(s4) AS r4, count(s5) AS r5,
           count(*) FILTER (WHERE first_missing = 2 AND idle) AS t1,
           count(*) FILTER (WHERE first_missing = 3 AND idle) AS t2,
           count(*) FILTER (WHERE first_missing = 4 AND idle) AS t3,
           count(*) FILTER (WHERE first_missing = 5 AND idle) AS t4
      FROM placed
  )
  SELECT k.ordinal, k.stage, k.reached, k.stalled
    FROM totals t
   CROSS JOIN LATERAL (VALUES (1, 'signed in', t.r1, t.t1),
                              (2, 'workspace created', t.r2, t.t2),
                              (3, 'Instagram connected', t.r3, t.t3),
                              (4, 'folder added', t.r4, t.t4),
                              (5, 'first approval', t.r5, 0::bigint))
         AS k (ordinal, stage, reached, stalled)
   ORDER BY k.ordinal
$$;

COMMENT ON FUNCTION fn_activation_funnel(p_since timestamptz, p_stall interval) IS
  'The activation funnel across every workspace: one row per stage (signed in, workspace created, Instagram connected, folder added, first approval) with how many people who signed up since p_since reached it, each stage counted on its own, and how many stalled there: a later stage missing and nothing new from them in p_stall, counted against the stage before the missing one. A person who owns no active workspace but belongs to someone else''s counts as signed in and never as stalled. Counts only: no id, name or email leaves it. A SECURITY DEFINER read owned by svc_maintenance; EXECUTE for svc_worker alone, the login the psql escape hatch connects as, and for no API principal until an operator principal exists (#1124) (104, #1481).';

ALTER FUNCTION fn_activation_funnel(p_since timestamptz, p_stall interval) OWNER TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_activation_funnel(p_since timestamptz, p_stall interval) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_activation_funnel(p_since timestamptz, p_stall interval) TO svc_worker;

REVOKE CREATE ON SCHEMA public FROM svc_maintenance;
```

```sql
-- [§48 the sync retires what the publish can never fetch]

ALTER TABLE media_items ADD COLUMN last_listed_at TIMESTAMPTZ NULL;

ALTER TABLE media_items DROP CONSTRAINT ck_media_state;

ALTER TABLE media_items ADD CONSTRAINT ck_media_state
  CHECK (state IN ('available','unsupported','removed','missing'));

CREATE OR REPLACE FUNCTION fn_planned_misses(p_limit int, p_late interval)
RETURNS TABLE (o_id uuid, o_workspace_id uuid, o_reason text, o_schedule_slot_at timestamptz, o_file_name text, o_handle varchar, o_tz text, o_scheduled_by_user_id uuid)
LANGUAGE sql STABLE STRICT SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT d.id, d.workspace_id, d.reason, d.schedule_slot_at, d.file_name, d.handle, d.tz,
         d.scheduled_by_user_id
    FROM (SELECT i.id, i.workspace_id, i.schedule_slot_at, i.scheduled_by_user_id,
                 m.file_name, a.handle, w.tz,
                 CASE
                   WHEN m.id IS NULL OR m.state = 'removed' THEN 'item_removed'
                   WHEN m.state = 'unsupported' THEN 'item_unsupported'
                   WHEN m.state = 'missing' THEN 'item_missing'
                   WHEN EXISTS (SELECT 1 FROM post_locks l
                                 WHERE l.workspace_id = i.workspace_id
                                   AND l.ig_account_id IS NULL
                                   AND l.media_item_id = i.media_item_id
                                   AND l.kind IN ('reject', 'unsupported', 'hold', 'seasonal')
                                   AND (l.expires_at IS NULL OR l.expires_at > now()))
                     THEN 'item_locked'
                   WHEN a.state IS NULL OR a.state NOT IN ('active', 'reauth_required')
                     THEN 'account_removed'
                   WHEN i.schedule_slot_at > now() - p_late THEN NULL
                   WHEN w.state <> 'active' OR w.is_paused THEN 'paused'
                   ELSE 'late'
                 END AS reason
            FROM post_intents i
            JOIN workspaces w ON w.id = i.workspace_id
            LEFT JOIN media_items m ON m.id = i.media_item_id
             AND m.workspace_id = i.workspace_id
            LEFT JOIN ig_accounts a ON a.id = i.ig_account_id
             AND a.workspace_id = i.workspace_id
           WHERE i.state = 'scheduled' AND i.origin = 'planned'
             AND i.schedule_slot_at <= now() AND NOT i.cancel_requested) d
   WHERE d.reason IS NOT NULL
   ORDER BY d.schedule_slot_at LIMIT p_limit
$$;

COMMENT ON FUNCTION fn_planned_misses(p_limit int, p_late interval) IS
  'The planned stories that will not be served, across every workspace: each due, scheduled, unflagged planned intent that cannot be served (item_removed, item_unsupported, item_missing, item_locked, account_removed, in that precedence) or whose p_late window has passed (paused when its workspace was not taking posts, else late) — the complement of fn_prompts_due''s planned rows. The worker expires each with its reason and tells the bound chats in the same transaction, per workspace under that workspace''s tenant. STRICT: a NULL window lists nothing. A SECURITY DEFINER read owned by svc_maintenance; EXECUTE for svc_worker (089, #1413; 105, #1545).';
```

```sql
-- [§49 the activation nudge: one email, once, to a person who stopped setting up, and the stage definition the funnel and the nudge share]
-- users.activation_nudge_at is the once-per-person latch the nudge sweep sets as it enqueues the
-- email. activation_nudge_sweep joins both job-kind constraints as a system kind. The per-person
-- stage timestamps move out of fn_activation_funnel into fn_activation_stages, an invoker helper
-- only the two doors can call; fn_activation_funnel is replaced over it with the same result; and
-- fn_activation_stalled lists who is owed a nudge, user ids and a stage only. EXECUTE for svc_worker
-- alone, as for 104. The CREATE bracket is 062's.
ALTER TABLE users ADD COLUMN activation_nudge_at TIMESTAMPTZ NULL;

ALTER TABLE jobs DROP CONSTRAINT ck_jobs_kind;

ALTER TABLE jobs ADD CONSTRAINT ck_jobs_kind CHECK (kind IN (
  'plan_slot','publish_pipeline','deliver_outbox','sync_media_source',
  'first_ingest_chunk','refresh_credential','offboard_workspace',
  'revoke_workspace_credentials','reauth_prompt',
  'reconcile_ambiguous','reap_expired','reap_transit_assets','retention_sweep',
  'reencrypt_credentials','send_email','alert_stranded_sources',
  'activation_nudge_sweep'));

ALTER TABLE jobs DROP CONSTRAINT ck_jobs_system_kinds;

ALTER TABLE jobs ADD CONSTRAINT ck_jobs_system_kinds CHECK (
  (workspace_id IS NULL) = (kind IN
    ('reconcile_ambiguous','reap_expired','reap_transit_assets','retention_sweep',
     'reencrypt_credentials','send_email','alert_stranded_sources',
     'activation_nudge_sweep')));

GRANT CREATE ON SCHEMA public TO svc_maintenance;

CREATE FUNCTION fn_activation_stages(p_since timestamptz)
RETURNS TABLE (o_user_id uuid, o_s1 timestamptz, o_s2 timestamptz, o_s3 timestamptz,
               o_s4 timestamptz, o_s5 timestamptz, o_first_card timestamptz, o_joined boolean,
               o_first_missing int, o_last_active timestamptz)
LANGUAGE sql STABLE SET search_path = pg_catalog, public, pg_temp AS $$
  WITH owned_ws AS (
    SELECT m.user_id, w.id AS workspace_id, w.created_at
      FROM workspace_members m
      JOIN workspaces w ON w.id = m.workspace_id
     WHERE m.role = 'owner' AND w.state = 'active'
  ), stages AS (
    SELECT u.id AS user_id,
           u.created_at AS s1,
           (SELECT min(o.created_at) FROM owned_ws o WHERE o.user_id = u.id) AS s2,
           (SELECT min(a.created_at) FROM owned_ws o
              JOIN ig_accounts a ON a.workspace_id = o.workspace_id
             WHERE o.user_id = u.id) AS s3,
           (SELECT min(src.created_at) FROM owned_ws o
              JOIN media_sources src ON src.workspace_id = o.workspace_id
             WHERE o.user_id = u.id) AS s4,
           (SELECT min(e.created_at) FROM owned_ws o
              JOIN audit_events e ON e.workspace_id = o.workspace_id
             WHERE o.user_id = u.id
               AND e.entity_kind = 'post_intent' AND e.detail IS NULL
               AND e.from_state = 'awaiting_approval'
               AND e.to_state IN ('approved', 'posted')) AS s5,
           (SELECT min(e.created_at) FROM owned_ws o
              JOIN audit_events e ON e.workspace_id = o.workspace_id
             WHERE o.user_id = u.id
               AND e.entity_kind = 'post_intent' AND e.detail IS NULL
               AND e.to_state = 'awaiting_approval') AS first_card,
           EXISTS (SELECT 1 FROM workspace_members m
                     JOIN workspaces w ON w.id = m.workspace_id
                    WHERE m.user_id = u.id AND m.role <> 'owner'
                      AND w.state = 'active') AS joined
      FROM users u
     WHERE u.state = 'active' AND u.created_at >= p_since
  )
  SELECT user_id, s1, s2, s3, s4, s5, first_card, joined,
         CASE WHEN s2 IS NULL AND joined THEN NULL
              WHEN s2 IS NULL THEN 2
              WHEN s3 IS NULL THEN 3
              WHEN s4 IS NULL THEN 4
              WHEN s5 IS NULL THEN 5
         END,
         GREATEST(s1, s2, s3, s4, s5)
    FROM stages
$$;

COMMENT ON FUNCTION fn_activation_stages(p_since timestamptz) IS
  'Each active person who signed up at or after p_since, with the five activation stage timestamps across the active workspaces they own (signed in, first owned workspace, first ig_accounts row, first media_sources row, first approval of a card), the first card they were offered, whether they belong to someone else''s workspace, their first missing stage (NULL when they own none but joined a team) and their latest stage. The one definition both doors read: fn_activation_funnel counts it and fn_activation_stalled lists from it. An invoker helper owned by svc_maintenance with EXECUTE revoked from PUBLIC, so only those doors, which run as svc_maintenance, can call it (106, #1481).';

ALTER FUNCTION fn_activation_stages(p_since timestamptz) OWNER TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_activation_stages(p_since timestamptz) FROM PUBLIC;

CREATE OR REPLACE FUNCTION fn_activation_funnel(p_since timestamptz, p_stall interval DEFAULT interval '72 hours')
RETURNS TABLE (o_ordinal int, o_stage text, o_reached bigint, o_stalled bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  WITH placed AS (
    SELECT o_s2 AS s2, o_s3 AS s3, o_s4 AS s4, o_s5 AS s5, o_first_missing AS first_missing,
           o_last_active < now() - p_stall AS idle
      FROM fn_activation_stages(p_since)
  ), totals AS (
    SELECT count(*) AS r1, count(s2) AS r2, count(s3) AS r3, count(s4) AS r4, count(s5) AS r5,
           count(*) FILTER (WHERE first_missing = 2 AND idle) AS t1,
           count(*) FILTER (WHERE first_missing = 3 AND idle) AS t2,
           count(*) FILTER (WHERE first_missing = 4 AND idle) AS t3,
           count(*) FILTER (WHERE first_missing = 5 AND idle) AS t4
      FROM placed
  )
  SELECT k.ordinal, k.stage, k.reached, k.stalled
    FROM totals t
   CROSS JOIN LATERAL (VALUES (1, 'signed in', t.r1, t.t1),
                              (2, 'workspace created', t.r2, t.t2),
                              (3, 'Instagram connected', t.r3, t.t3),
                              (4, 'folder added', t.r4, t.t4),
                              (5, 'first approval', t.r5, 0::bigint))
         AS k (ordinal, stage, reached, stalled)
   ORDER BY k.ordinal
$$;

CREATE FUNCTION fn_activation_stalled(p_since timestamptz, p_stall interval, p_limit int)
RETURNS TABLE (o_user_id uuid, o_stage int)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT s.o_user_id, s.o_first_missing
    FROM fn_activation_stages(p_since) s
    JOIN users u ON u.id = s.o_user_id
   WHERE u.activation_nudge_at IS NULL
     AND u.primary_email IS NOT NULL
     AND s.o_first_missing IN (3, 4, 5)
     AND (s.o_first_missing < 5 OR s.o_first_card IS NOT NULL)
     AND GREATEST(s.o_last_active, s.o_first_card) < now() - p_stall
   ORDER BY GREATEST(s.o_last_active, s.o_first_card) DESC, s.o_user_id
   LIMIT p_limit
$$;

COMMENT ON FUNCTION fn_activation_stalled(p_since timestamptz, p_stall interval, p_limit int) IS
  'Who is owed the activation nudge: at most p_limit active people who signed up at or after p_since, have an email and were never nudged, whose first missing stage is 3 (Instagram), 4 (a folder) or 5 (a first approval, only once a card was offered), and who have done nothing for p_stall, the first card counting as activity. Most recent stalls first. User ids and a stage only: the sweep reads the address itself as it sets users.activation_nudge_at. A SECURITY DEFINER read owned by svc_maintenance; EXECUTE for svc_worker alone (106, #1481).';

ALTER FUNCTION fn_activation_stalled(p_since timestamptz, p_stall interval, p_limit int) OWNER TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_activation_stalled(p_since timestamptz, p_stall interval, p_limit int) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_activation_stalled(p_since timestamptz, p_stall interval, p_limit int) TO svc_worker;

REVOKE CREATE ON SCHEMA public FROM svc_maintenance;
```

```sql
-- [§50 the ledger indexed by workspace and slot: the calendar's month, its day view and the Overview's recent activity read a workspace's stories by schedule_slot_at]
-- An index on post_intents (workspace_id, schedule_slot_at) over every state, so a month or a day
-- of a workspace is one range of it and newest first is a backward walk of it, rather than a walk
-- of the workspace's whole history (#1640).
CREATE INDEX ix_intents_workspace_slot ON post_intents (workspace_id, schedule_slot_at);
```

```sql
-- [§51 oauth_states: the PKCE code verifier, encrypted]
-- RFC 7636: the code verifier minted with a Drive connect state, stored as ciphertext under the
-- credential ring (§3) and never as itself. NULL on a state minted without one.
ALTER TABLE oauth_states ADD COLUMN encrypted_code_verifier TEXT NULL;
```

```sql
-- [§53 one approval rule for every story: approval_rule, the waiting CHECK, tg_intent_person_or_rule, the approval positions, the item flag and the posted-via-API index]
-- The positions: a value nothing ever honored is not consent, so every workspace asks (F4).
SET LOCAL app.actor_kind = 'migration';
UPDATE workspaces SET approval_mode = 'manual' WHERE approval_mode <> 'manual';
SET LOCAL app.actor_kind = '';
ALTER TABLE workspaces DROP CONSTRAINT ck_ws_approval_mode;
ALTER TABLE workspaces ADD CONSTRAINT ck_ws_approval_mode
  CHECK (approval_mode IN ('manual','returning','auto'));
ALTER TABLE media_sources ADD COLUMN approval_mode TEXT NULL
  CONSTRAINT ck_source_approval_mode CHECK (approval_mode IN ('manual','returning','auto'));
-- The item flag: false until a member sets it.
ALTER TABLE media_items ADD COLUMN auto_approve BOOLEAN NOT NULL DEFAULT false;

-- The record: the rule that approved a story, NULL when a person did or nothing has yet.
ALTER TABLE post_intents ADD COLUMN approval_rule TEXT NULL
  CONSTRAINT ck_intent_approval_rule CHECK (approval_rule IN ('auto','item','returning'));
-- A waiting story is always one a person decides: a rule appears only with the approval.
ALTER TABLE post_intents ADD CONSTRAINT ck_intent_waiting_unruled
  CHECK (approval_rule IS NULL OR state NOT IN ('scheduled','prompt_pending','awaiting_approval'));
ALTER TABLE post_intents DROP CONSTRAINT ck_intent_planned_manual;
-- "Posted through the API before": one index probe per serve.
CREATE INDEX ix_intents_api_posted ON post_intents (workspace_id, media_item_id)
  WHERE state = 'posted' AND published_via = 'api';

-- One approval rule for every story, replacing 088's planned-only one.
DROP TRIGGER tg_intent_planned_person ON post_intents;
DROP FUNCTION trg_intent_planned_person();

CREATE FUNCTION trg_intent_person_or_rule() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  approving boolean := OLD.state = 'awaiting_approval' AND NEW.state = 'approved';
  actor text := COALESCE(NULLIF(current_setting('app.actor_kind', true), ''), 'none');
BEGIN
  IF NEW.origin IS DISTINCT FROM OLD.origin THEN
    RAISE EXCEPTION 'post_intent % origin is fixed at birth (% -> %)', OLD.id, OLD.origin, NEW.origin
      USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.approval_rule IS DISTINCT FROM OLD.approval_rule AND NOT approving THEN
    RAISE EXCEPTION 'post_intent % approval_rule is stamped only by the approval it names', OLD.id
      USING ERRCODE = 'check_violation';
  END IF;
  IF approving AND NEW.approval_rule IS NULL
     AND (actor <> 'user' OR NULLIF(current_setting('app.actor_user_id', true), '') IS NULL) THEN
    RAISE EXCEPTION 'post_intent %: only a person approves it (actor %)', OLD.id, actor
      USING ERRCODE = 'check_violation';
  END IF;
  IF approving AND NEW.approval_rule IS NOT NULL AND actor <> 'system' THEN
    RAISE EXCEPTION 'post_intent %: an automatic approval (%) is the system''s (actor %)',
      OLD.id, NEW.approval_rule, actor USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER tg_intent_person_or_rule BEFORE UPDATE OF state, origin, approval_rule ON post_intents
  FOR EACH ROW
  WHEN (NEW.origin IS DISTINCT FROM OLD.origin
        OR NEW.approval_rule IS DISTINCT FROM OLD.approval_rule
        OR (OLD.state = 'awaiting_approval' AND NEW.state = 'approved'))
  EXECUTE FUNCTION trg_intent_person_or_rule();
```

```sql
-- [§56 auto-approval's contract half: workspaces.auto_reapprove_returning dropped, its meaning now the returning position]
-- Retired by 110: the `returning` position carries what the column stored, and nothing reads it.
ALTER TABLE workspaces DROP COLUMN auto_reapprove_returning;
```

```sql
-- [§58 an invited person's own invitations: the ones the accept door would honour, and a fresh link to the invited address]
-- Two definer doors owned by svc_membership, EXECUTE for svc_ingress alone. The caller proves the
-- person: p_user is the session's user and p_email their verified address. Neither door accepts
-- an invitation or answers a token. The list is indexed on lower(email) over pending invitations.

CREATE INDEX ix_invite_pending_email ON workspace_invitations (lower(email))
  WHERE state = 'pending';

GRANT CREATE ON SCHEMA public TO svc_membership;

CREATE FUNCTION fn_invitations_for(p_user uuid, p_email text, p_limit integer)
RETURNS TABLE (o_invitation_id uuid, o_workspace_name text, o_role text, o_invited_by uuid,
               o_expires_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT i.id, w.name, i.role, i.invited_by_user_id, i.expires_at
    FROM workspace_invitations i
    JOIN workspaces w ON w.id = i.workspace_id AND w.state = 'active'
    JOIN workspace_members a ON a.workspace_id = i.workspace_id
                            AND a.user_id = i.invited_by_user_id
                            AND a.role IN ('owner', 'admin')
    JOIN users u ON u.id = i.invited_by_user_id AND u.state = 'active'
   WHERE i.delivery_channel = 'email' AND lower(i.email) = lower(p_email)
     AND i.state = 'pending' AND i.expires_at > now()
     AND NOT EXISTS (SELECT 1 FROM workspace_members m
                      WHERE m.workspace_id = i.workspace_id AND m.user_id = p_user)
   ORDER BY i.created_at DESC, i.id
   LIMIT p_limit
$$;

COMMENT ON FUNCTION fn_invitations_for(uuid, text, integer) IS
  'The invitations an invited person could still accept (115): email invitations, pending and '
  'unexpired, addressed to p_email (lower() on both sides), from an active workspace whose inviter '
  'is still its owner or an admin with an active account, in a workspace p_user does not belong '
  'to. Newest first, at most p_limit. Answers the id, workspace name, role, '
  'inviter and expiry, never the token hash. The caller proves the person. SECURITY DEFINER owned '
  'by svc_membership with EXECUTE granted to svc_ingress.';

ALTER FUNCTION fn_invitations_for(uuid, text, integer) OWNER TO svc_membership;

CREATE FUNCTION fn_invitation_reissue(p_invitation_id uuid, p_email text, p_token_hash text)
RETURNS boolean
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  WITH rotated AS (
    UPDATE workspace_invitations i SET token_hash = p_token_hash
     WHERE i.id = p_invitation_id
       AND i.delivery_channel = 'email' AND lower(i.email) = lower(p_email)
       AND i.state = 'pending' AND i.expires_at > now()
    RETURNING i.id
  )
  SELECT EXISTS (SELECT 1 FROM rotated)
$$;

COMMENT ON FUNCTION fn_invitation_reissue(uuid, text, text) IS
  'Email me the link again (115): stores a fresh token hash on one invitation, in place, when it '
  'is a pending, unexpired email invitation addressed to p_email (lower() on both sides); true '
  'when it did. Every guard is in the UPDATE''s own WHERE, so an accept or a revoke that commits '
  'first wins. created_at and expires_at are kept. The API calls it only after the email carrying '
  'the new link was sent, and only for an invitation fn_invitations_for lists for the caller, so '
  'a failed send leaves the old link working. The caller proves the person. SECURITY DEFINER '
  'owned by svc_membership with EXECUTE granted to svc_ingress.';

ALTER FUNCTION fn_invitation_reissue(uuid, text, text) OWNER TO svc_membership;

REVOKE CREATE ON SCHEMA public FROM svc_membership;

REVOKE ALL ON FUNCTION fn_invitations_for(uuid, text, integer) FROM PUBLIC;

REVOKE ALL ON FUNCTION fn_invitation_reissue(uuid, text, text) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_invitations_for(uuid, text, integer) TO svc_ingress;

GRANT EXECUTE ON FUNCTION fn_invitation_reissue(uuid, text, text) TO svc_ingress;
```

```sql
-- [§59 a planned story repeats]
-- The rule: four columns, all or none, on planned stories only. The table's lock is taken first.
ALTER TABLE post_intents ADD COLUMN repeat_unit TEXT NULL;
ALTER TABLE post_intents ADD COLUMN repeat_every SMALLINT NULL;
ALTER TABLE post_intents ADD COLUMN repeat_days SMALLINT[] NULL;
ALTER TABLE post_intents ADD COLUMN repeat_at TIME NULL;
ALTER TABLE post_intents ADD CONSTRAINT ck_intent_repeat_unit CHECK (repeat_unit IN ('day','week'));
ALTER TABLE post_intents ADD CONSTRAINT ck_intent_repeat CHECK (
  CASE
    WHEN repeat_unit IS NULL
      THEN repeat_every IS NULL AND repeat_days IS NULL AND repeat_at IS NULL
    WHEN origin <> 'planned' OR repeat_every IS NULL OR repeat_at IS NULL
      THEN false
    WHEN repeat_unit = 'day'
      THEN repeat_days IS NULL AND repeat_every BETWEEN 1 AND 365
    WHEN repeat_unit = 'week'
      THEN repeat_days IS NOT NULL AND repeat_every BETWEEN 1 AND 52
       AND array_ndims(repeat_days) = 1 AND cardinality(repeat_days) BETWEEN 1 AND 7
       AND repeat_days <@ ARRAY[1, 2, 3, 4, 5, 6, 7]::smallint[]
    ELSE true
  END);

-- The wall time: the first of two readings that reads back as the local time, or NULL if none does.
CREATE FUNCTION fn_wall_instant(p_local timestamp, p_tz text)
RETURNS timestamptz LANGUAGE sql STABLE AS $$
  SELECT min(c.v)
    FROM (VALUES (p_local AT TIME ZONE p_tz),
                 ((p_local - interval '1 day') AT TIME ZONE p_tz + interval '24 hours')) c(v)
   WHERE c.v AT TIME ZONE p_tz = p_local
$$;

-- The grid: the rule's times in [p_lo, p_hi), or with p_hi NULL up to two local days past one
-- period after p_lo, which always holds the next.
CREATE FUNCTION fn_repeat_times(p_unit text, p_every int, p_days smallint[], p_at time,
                                p_anchor timestamptz, p_tz text,
                                p_lo timestamptz, p_hi timestamptz)
RETURNS SETOF timestamptz LANGUAGE sql STABLE AS $$
  WITH b AS MATERIALIZED (
    SELECT z.tz, x.a, x.a - EXTRACT(isodow FROM x.a)::int + 1 AS w0,
           GREATEST((p_lo AT TIME ZONE z.tz)::date - 1, x.a) AS d0,
           COALESCE((p_hi AT TIME ZONE z.tz)::date,
                    (p_lo AT TIME ZONE z.tz)::date
                      + CASE p_unit WHEN 'week' THEN 7 ELSE 1 END * p_every + 1) + 1 AS d1
      FROM (SELECT fn_safe_tz(p_tz) AS tz) z,
           LATERAL (SELECT (p_anchor AT TIME ZONE z.tz)::date AS a) x
  ), t AS MATERIALIZED (
    SELECT COALESCE(fn_wall_instant(dd.d + p_at, b.tz), (dd.d + p_at) AT TIME ZONE b.tz) AS ts
      FROM b, generate_series(0, b.d1 - b.d0) AS g,
           LATERAL (SELECT b.d0 + g AS d) dd
     WHERE p_every >= 1
       AND CASE p_unit
             WHEN 'day' THEN (dd.d - b.a) % p_every = 0
             WHEN 'week' THEN ((dd.d - b.w0) / 7) % p_every = 0
                          AND EXTRACT(isodow FROM dd.d)::smallint = ANY (p_days)
             ELSE false
           END
  )
  SELECT t.ts FROM t WHERE t.ts >= p_lo AND (p_hi IS NULL OR t.ts < p_hi) ORDER BY t.ts
$$;

-- The successor: made when an occurrence ends, unless the series has ended or lost its home.
CREATE FUNCTION trg_intent_repeat() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_ws text; v_tz text; v_ref text; v_lo timestamptz; v_day timestamp; v_next timestamptz;
        v_id uuid;
BEGIN
  IF NEW.last_error->>'class' = 'planned_missed'
     AND NEW.last_error->>'message' NOT IN ('late', 'paused') THEN
    RETURN NULL;
  END IF;
  BEGIN
    SELECT w.state, fn_safe_tz(COALESCE(a.tz, w.tz)), a.provider_account_ref
      INTO v_ws, v_tz, v_ref
      FROM workspaces w
      JOIN ig_accounts a ON a.id = NEW.ig_account_id AND a.workspace_id = w.id
                        AND a.state IN ('active', 'reauth_required')
     WHERE w.id = NEW.workspace_id;
    IF NOT FOUND OR v_ws = 'offboarding' THEN
      RETURN NULL;
    END IF;
    v_lo := CASE WHEN NEW.last_error->>'class' = 'planned_missed' THEN now()
                 ELSE NEW.schedule_slot_at END + interval '1 microsecond';
    IF NEW.state = 'posted' THEN
      v_day := (now() AT TIME ZONE v_tz)::date + 1;
      v_lo := GREATEST(v_lo, COALESCE(fn_wall_instant(v_day, v_tz), v_day AT TIME ZONE v_tz));
    END IF;
    SELECT min(t) INTO v_next
      FROM fn_repeat_times(NEW.repeat_unit, NEW.repeat_every, NEW.repeat_days, NEW.repeat_at,
                           NEW.schedule_slot_at, v_tz, v_lo, NULL) AS t;
    INSERT INTO post_intents (workspace_id, ig_account_id, media_item_id, provider_account_ref,
                              approval_mode, schedule_slot_at, state, origin,
                              scheduled_by_user_id, repeat_unit, repeat_every, repeat_days,
                              repeat_at)
    VALUES (NEW.workspace_id, NEW.ig_account_id, NEW.media_item_id, v_ref,
            'manual', v_next, 'scheduled', 'planned',
            NEW.scheduled_by_user_id, NEW.repeat_unit, NEW.repeat_every, NEW.repeat_days,
            NEW.repeat_at)
    RETURNING id INTO v_id;
    INSERT INTO audit_events (workspace_id, entity_kind, entity_id, from_state, to_state,
                              actor_kind, actor_user_id, channel, detail)
    VALUES (NEW.workspace_id, 'post_intent', v_id, NULL, 'scheduled',
            current_setting('app.actor_kind'),
            NULLIF(current_setting('app.actor_user_id', true), '')::uuid,
            NULLIF(current_setting('app.channel', true), ''),
            jsonb_build_object('v', 1, 'event', 'repeated', 'follows', NEW.id, 'at', v_next));
  EXCEPTION WHEN OTHERS THEN
    RAISE WARNING 'post_intent %: its next occurrence was not created (%)', NEW.id, SQLSTATE;
    BEGIN
      INSERT INTO audit_events (workspace_id, entity_kind, entity_id, from_state, to_state,
                                actor_kind, actor_user_id, channel, detail)
      VALUES (NEW.workspace_id, 'post_intent', NEW.id, NEW.state, NEW.state,
              COALESCE(NULLIF(current_setting('app.actor_kind', true), ''), 'system'),
              NULLIF(current_setting('app.actor_user_id', true), '')::uuid,
              NULLIF(current_setting('app.channel', true), ''),
              jsonb_build_object('v', 1, 'event', 'repeat_failed', 'sqlstate', SQLSTATE));
    EXCEPTION WHEN OTHERS THEN
      NULL;
    END;
  END;
  RETURN NULL;
END $$;

CREATE TRIGGER tg_intent_repeat AFTER UPDATE OF state ON post_intents
  FOR EACH ROW
  WHEN (NEW.repeat_unit IS NOT NULL AND OLD.state IS DISTINCT FROM NEW.state
        AND NEW.state IN ('posted','skipped','expired','failed','cancelled'))
  EXECUTE FUNCTION trg_intent_repeat();

COMMENT ON FUNCTION fn_wall_instant(timestamp, text) IS
  'A local time in a zone as an instant: the earliest of two readings that reads back as the local time, which is the first occurrence of a repeated hour, or NULL for a time the clocks skip. The zone is used as given. One-off planning (command_executors._INSTANT) and the repeat grid (fn_repeat_times) both read local times through it. Plain and STABLE (116, #485).';

COMMENT ON FUNCTION fn_repeat_times(text, int, smallint[], time, timestamptz, text, timestamptz, timestamptz) IS
  'A repeat rule''s times in [p_lo, p_hi), or, with p_hi NULL, its times up to two local days past one period after p_lo, a window that always holds the first time at or after p_lo: every p_every days, or every p_every weeks on the ISO weekdays p_days, at the local time p_at in the zone p_tz, the grid anchored on p_anchor''s local date. The one definition of the grid: tg_intent_repeat creates the next occurrence with it, and the calendar projects with it. Local times read through fn_wall_instant; a skipped one takes the offset before the jump; a decayed zone degrades to UTC (fn_safe_tz). Plain and STABLE (116, #485).';

COMMENT ON FUNCTION trg_intent_repeat() IS
  'tg_intent_repeat: when a story carrying a repeat rule enters a terminal state other than rejected, insert its next occurrence (a planned story, scheduled at the rule''s next time) in the same transaction. No successor when the miss sweep recorded a reason other than late or paused, when the workspace is offboarding, or when the account is not live. After a late or paused miss the next time follows now; otherwise it follows the ended occurrence''s time. After a post it is also on a later local day than the post, in the story''s zone, so a series posts at most once a local day. The INSERT names its columns, so every other column starts at its default, and approval_mode is written as ''manual''. Total: a failure records repeat_failed and a warning, and the ending stands (116, #485).';

GRANT INSERT ON post_intents TO svc_maintenance;
```

```sql
-- [§72 admitting from the waitlist: the claim link's columns, the list, the admission, the link stored after its email went, and the claim]
-- Four definer doors owned by svc_membership: fn_waitlist, fn_signup_admit and fn_signup_claim
-- for svc_ingress, fn_signup_link for svc_worker. "Held" means an account's primary_email is the
-- address. fn_signup_admitted stops counting a claimed admission. The runtime roles gain nothing
-- on signup_admissions or waitlist_entries.

ALTER TABLE signup_admissions ADD COLUMN claim_token_hash text;

ALTER TABLE signup_admissions ADD COLUMN emailed_at timestamptz;

ALTER TABLE signup_admissions ADD COLUMN claimed_at timestamptz;

CREATE UNIQUE INDEX uq_signup_admissions_claim ON signup_admissions (claim_token_hash)
  WHERE claim_token_hash IS NOT NULL;

GRANT INSERT, UPDATE ON signup_admissions TO svc_membership;

CREATE POLICY p_member_admissions_ins ON signup_admissions FOR INSERT TO svc_membership
  WITH CHECK (true);

CREATE POLICY p_member_admissions_upd ON signup_admissions FOR UPDATE TO svc_membership
  USING (true) WITH CHECK (true);

GRANT SELECT ON waitlist_entries TO svc_membership;

CREATE POLICY p_member_waitlist ON waitlist_entries FOR SELECT TO svc_membership USING (true);

GRANT SELECT (primary_email) ON users TO svc_membership;

GRANT CREATE ON SCHEMA public TO svc_membership;

CREATE FUNCTION fn_waitlist(p_limit integer, p_ref text DEFAULT NULL)
RETURNS TABLE (o_email text, o_joined_at timestamptz, o_admitted_at timestamptz,
               o_emailed_at timestamptz, o_signed_up boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT email, w.joined_at, a.admitted_at, a.emailed_at,
         a.claimed_at IS NOT NULL
           OR EXISTS (SELECT 1 FROM users u WHERE lower(u.primary_email) = email)
    FROM waitlist_entries w FULL JOIN signup_admissions a USING (email)
   WHERE p_ref IS NULL
      OR left(encode(sha256(convert_to(email, 'UTF8')), 'hex'), 32) = p_ref
   ORDER BY COALESCE(w.joined_at, a.admitted_at) DESC, email
   LIMIT GREATEST(p_limit, 0)
$$;

COMMENT ON FUNCTION fn_waitlist(integer, text) IS
  'The owner''s waitlist (129): every waitlist entry, and every admission that has none, newest '
  'first by when it joined or, off the list, when it was admitted, at most p_limit rows. Each row '
  'is the address, when it joined (NULL off the list), when it was admitted and emailed, and '
  'whether it signed up (a claimed admission, or an account holding the address). With p_ref, '
  'only the row whose address''s SHA-256 hex starts with p_ref''s 32 characters. Never a claim '
  'token''s hash. SECURITY DEFINER owned by svc_membership with EXECUTE granted to svc_ingress.';

ALTER FUNCTION fn_waitlist(integer, text) OWNER TO svc_membership;

CREATE FUNCTION fn_signup_admit(p_email text)
RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM users u WHERE lower(u.primary_email) = lower(p_email))
     OR EXISTS (SELECT 1 FROM signup_admissions a
                 WHERE a.email = lower(p_email) AND a.claimed_at IS NOT NULL) THEN
    RETURN 'signed_up';
  END IF;
  INSERT INTO signup_admissions (email) VALUES (lower(p_email)) ON CONFLICT (email) DO NOTHING;
  IF FOUND THEN
    RETURN 'admitted';
  END IF;
  RETURN 'admitted_before';
END $$;

COMMENT ON FUNCTION fn_signup_admit(text) IS
  'Admit an address (129), on the waitlist or not: signed_up when an account holds it or its '
  'admission was claimed, writing nothing; otherwise admitted, or admitted_before when it was '
  'already admitted. The table''s CHECK is the authority on what an address is, and its refusal '
  'reaches the caller. SECURITY DEFINER owned by svc_membership with EXECUTE granted to '
  'svc_ingress.';

ALTER FUNCTION fn_signup_admit(text) OWNER TO svc_membership;

CREATE FUNCTION fn_signup_link(p_email text, p_token_hash text)
RETURNS boolean
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  WITH linked AS (
    UPDATE signup_admissions a SET claim_token_hash = p_token_hash, emailed_at = now()
     WHERE a.email = lower(p_email) AND a.claimed_at IS NULL
       AND NOT EXISTS (SELECT 1 FROM users u WHERE lower(u.primary_email) = a.email)
    RETURNING a.email
  )
  SELECT EXISTS (SELECT 1 FROM linked)
$$;

COMMENT ON FUNCTION fn_signup_link(text, text) IS
  'Store the claim link an admission email carried (129): its token''s hash and emailed_at '
  'together, replacing an earlier link, on an admission not yet claimed whose address no account '
  'holds; true when it did. The worker calls it only after the provider accepted the email, so a '
  'failed send leaves the last link working. SECURITY DEFINER owned by svc_membership with '
  'EXECUTE granted to svc_worker.';

ALTER FUNCTION fn_signup_link(text, text) OWNER TO svc_membership;

CREATE FUNCTION fn_signup_claim(p_token_hash text)
RETURNS boolean
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  WITH claimed AS (
    UPDATE signup_admissions a SET claimed_at = now(), claim_token_hash = NULL
     WHERE a.claim_token_hash = p_token_hash AND a.claimed_at IS NULL
       AND NOT EXISTS (SELECT 1 FROM users u WHERE lower(u.primary_email) = a.email)
    RETURNING a.email
  )
  SELECT EXISTS (SELECT 1 FROM claimed)
$$;

COMMENT ON FUNCTION fn_signup_claim(text) IS
  'Spend a claim link (129): sets claimed_at and clears the hash on the admission whose link it '
  'is, while it is unclaimed and no account holds its address; true when it did. Single use, '
  'and dead once the address has an account: one spot, one account. SECURITY DEFINER owned by '
  'svc_membership with EXECUTE granted to svc_ingress.';

ALTER FUNCTION fn_signup_claim(text) OWNER TO svc_membership;

CREATE OR REPLACE FUNCTION fn_signup_admitted(p_email text)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT EXISTS (SELECT 1 FROM signup_admissions a WHERE a.email = lower(p_email) AND a.claimed_at IS NULL)
      OR EXISTS (SELECT 1 FROM workspace_invitations i
                   JOIN workspaces w ON w.id = i.workspace_id AND w.state = 'active'
                   JOIN workspace_members m ON m.workspace_id = i.workspace_id
                                           AND m.user_id = i.invited_by_user_id
                                           AND m.role IN ('owner', 'admin')
                   JOIN users u ON u.id = i.invited_by_user_id AND u.state = 'active'
                  WHERE lower(i.email) = lower(p_email)
                    AND i.state = 'pending' AND i.expires_at > now())
$$;

COMMENT ON FUNCTION fn_signup_admitted(text) IS
  'May a new Google account with this verified email create its user (092)? True when the owner '
  'admitted the address (signup_admissions) and its admission was not claimed through its link '
  '(129), or a pending, unexpired invitation is addressed to it from an active workspace by '
  'someone still its owner or an admin, with an active account (098); false for NULL. Compares '
  'lower() on both sides. Answers one boolean, never which workspace invited the address. '
  'SECURITY DEFINER owned by svc_membership with EXECUTE granted to svc_ingress.';

REVOKE CREATE ON SCHEMA public FROM svc_membership;

REVOKE ALL ON FUNCTION fn_waitlist(integer, text) FROM PUBLIC;

REVOKE ALL ON FUNCTION fn_signup_admit(text) FROM PUBLIC;

REVOKE ALL ON FUNCTION fn_signup_link(text, text) FROM PUBLIC;

REVOKE ALL ON FUNCTION fn_signup_claim(text) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_waitlist(integer, text) TO svc_ingress;

GRANT EXECUTE ON FUNCTION fn_signup_admit(text) TO svc_ingress;

GRANT EXECUTE ON FUNCTION fn_signup_link(text, text) TO svc_worker;

GRANT EXECUTE ON FUNCTION fn_signup_claim(text) TO svc_ingress;
```

```sql
-- [§73 an admission is used up by the account created at its address: the trigger, and the held admissions stamped once]
-- tg_users_consume_admission stamps claimed_at and clears the link's hash on the unconsumed
-- admission at a new user's address, in the user's own transaction, through a SECURITY DEFINER
-- function owned by svc_membership, with EXECUTE granted to no role. The lock comes first; the
-- backfill stamps the admissions an account holds now.

LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE;

GRANT CREATE ON SCHEMA public TO svc_membership;

CREATE FUNCTION trg_users_consume_admission()
RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  UPDATE signup_admissions
     SET claimed_at = now(), claim_token_hash = NULL
   WHERE email = lower(NEW.primary_email) AND claimed_at IS NULL;
  RETURN NEW;
END $$;

COMMENT ON FUNCTION trg_users_consume_admission() IS
  'tg_users_consume_admission (133, #1740): when a user is created with an address, the '
  'unconsumed admission at that address, compared with lower(), is used up in the same '
  'transaction: claimed_at is stamped and its claim link''s hash cleared. One spot, one account, '
  'whether the account came through the link or as the address. Birth only: an address changed '
  'later consumes nothing. No admission, no change; an error fails the user''s creation. '
  'SECURITY DEFINER owned by svc_membership, under its grants and policies on '
  'signup_admissions. EXECUTE is granted to no role: PostgreSQL asks for it when a trigger is '
  'created, never when an INSERT fires one.';

CREATE TRIGGER tg_users_consume_admission AFTER INSERT ON users
  FOR EACH ROW WHEN (NEW.primary_email IS NOT NULL)
  EXECUTE FUNCTION trg_users_consume_admission();

ALTER FUNCTION trg_users_consume_admission() OWNER TO svc_membership;

REVOKE CREATE ON SCHEMA public FROM svc_membership;

REVOKE ALL ON FUNCTION trg_users_consume_admission() FROM PUBLIC;

-- The backfill comes last, after the trigger. Either order is the same: the lock taken first
-- holds every write to users off until this file commits, so the accounts read here are the
-- accounts at commit, and every account created after the commit fires the trigger.
UPDATE signup_admissions a
   SET claimed_at = now(), claim_token_hash = NULL
 WHERE a.claimed_at IS NULL
   AND EXISTS (SELECT 1 FROM users u WHERE lower(u.primary_email) = a.email);
```

```sql
-- [§77 Instagram archive catalog: ownership, policy and generation provenance]
ALTER TABLE media_sources DROP CONSTRAINT ck_sources_provider;
ALTER TABLE media_sources ADD CONSTRAINT ck_sources_provider CHECK (provider IN ('gdrive','instagram_archive'));
ALTER TABLE media_sources ADD COLUMN origin_ig_account_id UUID NULL;
ALTER TABLE media_sources ADD COLUMN source_use_epoch BIGINT NOT NULL DEFAULT 0;
ALTER TABLE media_sources ADD COLUMN source_epoch_transaction_id BIGINT NULL;
ALTER TABLE media_sources ADD COLUMN current_authorization_generation_id UUID NULL;
ALTER TABLE media_sources ADD CONSTRAINT ck_sources_current_authorization CHECK (current_authorization_generation_id IS NULL OR provider = 'instagram_archive');
ALTER TABLE media_sources ADD CONSTRAINT ck_sources_origin CHECK ((provider = 'instagram_archive') = (origin_ig_account_id IS NOT NULL));
ALTER TABLE media_sources ADD CONSTRAINT ck_sources_epoch CHECK (source_use_epoch >= 0);
ALTER TABLE media_sources ADD CONSTRAINT uq_sources_ws_id_origin UNIQUE (workspace_id, id, origin_ig_account_id);
ALTER TABLE media_sources ADD CONSTRAINT fk_sources_origin FOREIGN KEY (workspace_id, origin_ig_account_id) REFERENCES ig_accounts(workspace_id, id) ON DELETE RESTRICT;
CREATE UNIQUE INDEX uq_archive_origin ON media_sources (workspace_id, origin_ig_account_id) WHERE provider = 'instagram_archive';
ALTER TABLE oauth_credentials ADD CONSTRAINT uq_credentials_ws_account_id UNIQUE (workspace_id, ig_account_id, id);
ALTER TABLE media_items ADD COLUMN source_data_generation_id UUID NULL;
ALTER TABLE media_items ADD COLUMN source_projection_redacted BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE media_items ADD CONSTRAINT ck_media_redacted_projection CHECK (NOT source_projection_redacted OR (source_data_generation_id IS NULL AND state = 'removed' AND media_kind = 'image' AND provider_file_ref = 'redacted:' || id::text AND content_hash = 'redacted:' || id::text AND file_name = 'redacted:' || id::text AND mime_type IS NULL AND file_size IS NULL AND category IS NULL AND folder_path IS NULL AND last_listed_at IS NULL AND title IS NULL AND caption IS NULL AND generated_caption IS NULL AND link_url IS NULL AND tags IS NULL AND custom_metadata IS NULL AND thumbnail_url IS NULL));
ALTER TABLE media_items ADD CONSTRAINT uq_media_ws_source_id UNIQUE (workspace_id, source_id, id);
ALTER TABLE media_items ADD CONSTRAINT uq_media_ws_source_generation_id UNIQUE (workspace_id, source_id, source_data_generation_id, id);
ALTER TABLE post_intents ADD CONSTRAINT uq_intents_ws_id UNIQUE (workspace_id, id);
ALTER TABLE post_intents ADD CONSTRAINT uq_intents_ws_id_media UNIQUE (workspace_id, id, media_item_id);
ALTER TABLE provider_operations ADD CONSTRAINT uq_ops_ws_id_intent UNIQUE (workspace_id, id, intent_id);
ALTER TABLE workspaces ADD COLUMN source_mix_mode TEXT NULL;
ALTER TABLE workspaces ADD COLUMN source_mix_policy_version INTEGER NULL;
ALTER TABLE workspaces ADD COLUMN source_membership_policy TEXT NULL;
ALTER TABLE workspaces ADD COLUMN source_mix_revision BIGINT NULL;
ALTER TABLE workspaces ADD COLUMN source_mix_conversion_version INTEGER NULL;
ALTER TABLE workspaces ADD COLUMN source_mix_converted_at TIMESTAMPTZ NULL;
ALTER TABLE workspaces ADD CONSTRAINT ck_ws_source_mix_mode CHECK (source_mix_mode IN ('equal','custom'));
ALTER TABLE workspaces ADD CONSTRAINT ck_ws_source_mix_versions CHECK (source_mix_policy_version > 0 AND source_mix_revision >= 0 AND source_mix_conversion_version > 0);

CREATE TABLE media_source_destinations (
  workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  source_id UUID NOT NULL,
  ig_account_id UUID NOT NULL,
  state TEXT NOT NULL DEFAULT 'active',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, source_id, ig_account_id),
  CONSTRAINT ck_source_destinations_state CHECK (state IN ('active','retired')),
  CONSTRAINT fk_source_destinations_source FOREIGN KEY (workspace_id, source_id) REFERENCES media_sources(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_source_destinations_account FOREIGN KEY (workspace_id, ig_account_id) REFERENCES ig_accounts(workspace_id, id) ON DELETE RESTRICT
);
CREATE INDEX ix_source_destinations_account ON media_source_destinations (workspace_id, ig_account_id, source_id) WHERE state = 'active';

CREATE TABLE instagram_archive_generations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  source_id UUID NOT NULL,
  origin_ig_account_id UUID NOT NULL,
  credential_id UUID NOT NULL,
  authorization_id UUID NOT NULL,
  app_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  authorized_at TIMESTAMPTZ NOT NULL,
  lifecycle TEXT NOT NULL DEFAULT 'active',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_archive_generations_ws_source_id UNIQUE (workspace_id, source_id, id),
  CONSTRAINT uq_archive_authorization UNIQUE (workspace_id, source_id, authorization_id),
  CONSTRAINT ck_archive_generation_lifecycle CHECK (lifecycle IN ('active','superseded','deletion_pending','deleted')),
  CONSTRAINT fk_archive_generation_origin FOREIGN KEY (workspace_id, source_id, origin_ig_account_id) REFERENCES media_sources(workspace_id, id, origin_ig_account_id) ON DELETE RESTRICT,
  CONSTRAINT fk_archive_generation_credential FOREIGN KEY (workspace_id, origin_ig_account_id, credential_id) REFERENCES oauth_credentials(workspace_id, ig_account_id, id) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX uq_archive_active_generation ON instagram_archive_generations (workspace_id, source_id) WHERE lifecycle = 'active';
-- Authorization ownership survives lifecycle fencing; receipts must not clear it.
ALTER TABLE media_sources ADD CONSTRAINT fk_sources_current_authorization FOREIGN KEY (workspace_id, id, current_authorization_generation_id) REFERENCES instagram_archive_generations(workspace_id, source_id, id) ON DELETE RESTRICT;
ALTER TABLE media_items ADD CONSTRAINT fk_media_generation FOREIGN KEY (workspace_id, source_id, source_data_generation_id) REFERENCES instagram_archive_generations(workspace_id, source_id, id) ON DELETE RESTRICT;

CREATE TABLE instagram_source_policies (
  workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  source_id UUID NOT NULL,
  schema_version INTEGER NOT NULL DEFAULT 1,
  revision BIGINT NOT NULL DEFAULT 1,
  minimum_age_days INTEGER NOT NULL DEFAULT 90,
  include_credited BOOLEAN NOT NULL DEFAULT false,
  include_unknown BOOLEAN NOT NULL DEFAULT false,
  eligibility TEXT NOT NULL DEFAULT 'supported',
  ranking TEXT NOT NULL DEFAULT 'none',
  destination_scope TEXT NOT NULL DEFAULT 'origin_only',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, source_id),
  CONSTRAINT ck_archive_policy_version CHECK (schema_version > 0 AND revision > 0),
  CONSTRAINT ck_archive_policy_age CHECK (minimum_age_days BETWEEN 0 AND 36500),
  CONSTRAINT ck_archive_policy_modes CHECK (eligibility = 'supported' AND ranking = 'none' AND destination_scope = 'origin_only'),
  CONSTRAINT fk_archive_policy_source FOREIGN KEY (workspace_id, source_id) REFERENCES media_sources(workspace_id, id) ON DELETE RESTRICT
);

CREATE TABLE instagram_media_index (
  workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  source_id UUID NOT NULL,
  parent_media_id TEXT NOT NULL,
  data_generation_id UUID NOT NULL,
  observation_revision BIGINT NOT NULL DEFAULT 1,
  media_item_id UUID NULL,
  first_child_id TEXT NULL,
  original_at TIMESTAMPTZ NOT NULL,
  permalink TEXT NULL,
  like_count BIGINT NULL,
  comments_count BIGINT NULL,
  observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  attribution TEXT NOT NULL DEFAULT 'unknown',
  attribution_detector_version INTEGER NOT NULL DEFAULT 1,
  availability TEXT NOT NULL DEFAULT 'available',
  exclusion_reason TEXT NULL,
  validation_state TEXT NOT NULL DEFAULT 'pending',
  validation_generation_id UUID NULL,
  validation_observation_revision BIGINT NULL,
  validation_fingerprint TEXT NULL,
  validated_at TIMESTAMPTZ NULL,
  cache_generation_id UUID NULL,
  cache_observation_revision BIGINT NULL,
  cache_policy_revision BIGINT NULL,
  cached_eligible BOOLEAN NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, source_id, parent_media_id),
  CONSTRAINT ck_archive_observation_revision CHECK (observation_revision > 0),
  CONSTRAINT ck_archive_observation_counts CHECK (like_count >= 0 AND comments_count >= 0),
  CONSTRAINT ck_archive_attribution CHECK (attribution IN ('not_detected','credited','unknown') AND attribution_detector_version > 0),
  CONSTRAINT ck_archive_availability CHECK (availability IN ('available','unavailable','excluded')),
  CONSTRAINT ck_archive_validation CHECK (validation_state IN ('pending','supported','unsupported')),
  CONSTRAINT ck_archive_validation_evidence CHECK ((validation_state = 'pending' AND validation_generation_id IS NULL AND validation_observation_revision IS NULL AND validation_fingerprint IS NULL AND validated_at IS NULL) OR (validation_state <> 'pending' AND validation_generation_id IS NOT NULL AND validation_observation_revision IS NOT NULL AND validation_generation_id = data_generation_id AND validation_observation_revision = observation_revision AND validation_fingerprint IS NOT NULL AND validated_at IS NOT NULL)),
  CONSTRAINT fk_archive_index_generation FOREIGN KEY (workspace_id, source_id, data_generation_id) REFERENCES instagram_archive_generations(workspace_id, source_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_archive_index_item FOREIGN KEY (workspace_id, source_id, data_generation_id, media_item_id) REFERENCES media_items(workspace_id, source_id, source_data_generation_id, id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED
);
CREATE UNIQUE INDEX uq_archive_index_item ON instagram_media_index (workspace_id, source_id, media_item_id) WHERE media_item_id IS NOT NULL;
CREATE INDEX ix_archive_original_time ON instagram_media_index (workspace_id, source_id, original_at, parent_media_id);
CREATE INDEX ix_archive_suitability ON instagram_media_index (workspace_id, source_id, data_generation_id, original_at, media_item_id) WHERE availability = 'available' AND validation_state = 'supported';

CREATE TABLE instagram_item_tombstones (
  workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  source_id UUID NOT NULL,
  media_item_id UUID NOT NULL,
  key_version INTEGER NOT NULL,
  parent_hmac TEXT NOT NULL,
  observation_revision BIGINT NOT NULL DEFAULT 1,
  CONSTRAINT ck_archive_tombstone_revision CHECK (observation_revision > 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, source_id, key_version, parent_hmac),
  CONSTRAINT ck_archive_tombstone_key CHECK (key_version > 0 AND parent_hmac ~ '^[a-f0-9]{64}$'),
  CONSTRAINT fk_archive_tombstone_item FOREIGN KEY (workspace_id, source_id, media_item_id) REFERENCES media_items(workspace_id, source_id, id) ON DELETE RESTRICT
);

CREATE TABLE instagram_artifacts (
  artifact_ref UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  source_id UUID NOT NULL,
  data_generation_id UUID NOT NULL,
  observation_revision BIGINT NOT NULL,
  media_item_id UUID NOT NULL,
  intent_id UUID NULL,
  kind TEXT NOT NULL,
  capture_epoch BIGINT NOT NULL,
  policy_revision BIGINT NOT NULL,
  selected_asset_identity TEXT NOT NULL,
  validation_fingerprint TEXT NULL,
  upload_attempt_id UUID NULL,
  admitted_at TIMESTAMPTZ NULL,
  outcome TEXT NULL,
  created_asset_ref TEXT NULL,
  settled_at TIMESTAMPTZ NULL,
  cleanup_state TEXT NOT NULL DEFAULT 'pending',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_archive_artifact_ws_ref UNIQUE (workspace_id, artifact_ref),
  CONSTRAINT uq_archive_artifact_capture UNIQUE (workspace_id, source_id, data_generation_id, intent_id, media_item_id, artifact_ref, observation_revision, selected_asset_identity, validation_fingerprint, kind),
  CONSTRAINT uq_archive_upload_attempt UNIQUE (workspace_id, upload_attempt_id),
  CONSTRAINT ck_archive_artifact_kind CHECK (kind IN ('download','validation','temporary','transit','stage')),
  CONSTRAINT ck_archive_artifact_versions CHECK (observation_revision > 0 AND capture_epoch >= 0 AND policy_revision > 0),
  CONSTRAINT ck_archive_artifact_cleanup CHECK (cleanup_state IN ('pending','destroyed','no_asset')),
  CONSTRAINT ck_archive_artifact_attempt CHECK ((kind = 'transit' AND intent_id IS NOT NULL AND upload_attempt_id IS NOT NULL AND admitted_at IS NOT NULL AND outcome IS NOT NULL AND outcome IN ('unsettled','settled','uncertain')) OR (kind <> 'transit' AND upload_attempt_id IS NULL AND admitted_at IS NULL AND outcome IS NULL)),
  CONSTRAINT ck_archive_artifact_settlement CHECK ((outcome IS NOT DISTINCT FROM 'settled') = (settled_at IS NOT NULL)),
  CONSTRAINT fk_archive_artifact_generation FOREIGN KEY (workspace_id, source_id, data_generation_id) REFERENCES instagram_archive_generations(workspace_id, source_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_archive_artifact_item FOREIGN KEY (workspace_id, source_id, media_item_id) REFERENCES media_items(workspace_id, source_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_archive_artifact_intent FOREIGN KEY (workspace_id, intent_id, media_item_id) REFERENCES post_intents(workspace_id, id, media_item_id) ON DELETE RESTRICT
);
CREATE INDEX ix_archive_cleanup_generation ON instagram_artifacts (workspace_id, source_id, data_generation_id, cleanup_state, outcome);

CREATE TABLE instagram_intent_provenance (
  workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  intent_id UUID NOT NULL,
  source_id UUID NOT NULL,
  media_item_id UUID NOT NULL,
  origin_ig_account_id UUID NOT NULL,
  selection_generation_id UUID NOT NULL,
  selection_observation_revision BIGINT NOT NULL,
  capture_generation_id UUID NOT NULL,
  capture_observation_revision BIGINT NOT NULL,
  staged_generation_id UUID NULL,
  staged_observation_revision BIGINT NULL,
  selected_asset_identity TEXT NOT NULL,
  validation_fingerprint TEXT NULL,
  transit_artifact_ref UUID NULL,
  transit_artifact_kind TEXT NOT NULL DEFAULT 'transit',
  capture_epoch BIGINT NOT NULL,
  last_authorized_policy_revision BIGINT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, intent_id),
  CONSTRAINT ck_archive_intent_transit_kind CHECK (transit_artifact_kind = 'transit'),
  CONSTRAINT ck_archive_intent_versions CHECK (selection_observation_revision > 0 AND capture_observation_revision > 0 AND capture_epoch >= 0 AND last_authorized_policy_revision > 0),
  CONSTRAINT ck_archive_staged_bundle CHECK ((staged_generation_id IS NULL AND staged_observation_revision IS NULL AND transit_artifact_ref IS NULL) OR (staged_generation_id IS NOT NULL AND staged_observation_revision IS NOT NULL AND staged_observation_revision > 0 AND transit_artifact_ref IS NOT NULL AND validation_fingerprint IS NOT NULL)),
  CONSTRAINT fk_archive_intent_origin FOREIGN KEY (workspace_id, source_id, origin_ig_account_id) REFERENCES media_sources(workspace_id, id, origin_ig_account_id) ON DELETE RESTRICT,
  CONSTRAINT fk_archive_intent_item FOREIGN KEY (workspace_id, source_id, media_item_id) REFERENCES media_items(workspace_id, source_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_archive_intent_intent FOREIGN KEY (workspace_id, intent_id, media_item_id) REFERENCES post_intents(workspace_id, id, media_item_id) ON DELETE RESTRICT,
  CONSTRAINT fk_archive_intent_selection FOREIGN KEY (workspace_id, source_id, selection_generation_id) REFERENCES instagram_archive_generations(workspace_id, source_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_archive_intent_capture FOREIGN KEY (workspace_id, source_id, capture_generation_id) REFERENCES instagram_archive_generations(workspace_id, source_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_archive_intent_stage FOREIGN KEY (workspace_id, source_id, staged_generation_id, intent_id, media_item_id, transit_artifact_ref, staged_observation_revision, selected_asset_identity, validation_fingerprint, transit_artifact_kind) REFERENCES instagram_artifacts(workspace_id, source_id, data_generation_id, intent_id, media_item_id, artifact_ref, observation_revision, selected_asset_identity, validation_fingerprint, kind) ON DELETE RESTRICT
);

CREATE TABLE instagram_operation_provenance (
  workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  operation_id UUID NOT NULL,
  intent_id UUID NOT NULL,
  source_id UUID NOT NULL,
  media_item_id UUID NOT NULL,
  origin_ig_account_id UUID NOT NULL,
  data_generation_id UUID NOT NULL,
  observation_revision BIGINT NOT NULL,
  policy_revision BIGINT NOT NULL,
  capture_epoch BIGINT NOT NULL,
  selected_asset_identity TEXT NOT NULL,
  validation_fingerprint TEXT NOT NULL,
  transit_artifact_ref UUID NOT NULL,
  transit_artifact_kind TEXT NOT NULL DEFAULT 'transit',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, operation_id),
  CONSTRAINT ck_archive_operation_transit_kind CHECK (transit_artifact_kind = 'transit'),
  CONSTRAINT ck_archive_operation_versions CHECK (observation_revision > 0 AND policy_revision > 0 AND capture_epoch >= 0),
  CONSTRAINT fk_archive_operation_operation FOREIGN KEY (workspace_id, operation_id, intent_id) REFERENCES provider_operations(workspace_id, id, intent_id) ON DELETE RESTRICT,
  CONSTRAINT fk_archive_operation_origin FOREIGN KEY (workspace_id, source_id, origin_ig_account_id) REFERENCES media_sources(workspace_id, id, origin_ig_account_id) ON DELETE RESTRICT,
  CONSTRAINT fk_archive_operation_item FOREIGN KEY (workspace_id, source_id, media_item_id) REFERENCES media_items(workspace_id, source_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_archive_operation_intent FOREIGN KEY (workspace_id, intent_id, media_item_id) REFERENCES post_intents(workspace_id, id, media_item_id) ON DELETE RESTRICT,
  CONSTRAINT fk_archive_operation_artifact FOREIGN KEY (workspace_id, source_id, data_generation_id, intent_id, media_item_id, transit_artifact_ref, observation_revision, selected_asset_identity, validation_fingerprint, transit_artifact_kind) REFERENCES instagram_artifacts(workspace_id, source_id, data_generation_id, intent_id, media_item_id, artifact_ref, observation_revision, selected_asset_identity, validation_fingerprint, kind) ON DELETE RESTRICT
);

CREATE FUNCTION trg_archive_generation_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.workspace_id, NEW.source_id, NEW.id, NEW.origin_ig_account_id, NEW.credential_id, NEW.authorization_id, NEW.app_id, NEW.subject_id, NEW.authorized_at)
     IS DISTINCT FROM (OLD.workspace_id, OLD.source_id, OLD.id, OLD.origin_ig_account_id, OLD.credential_id, OLD.authorization_id, OLD.app_id, OLD.subject_id, OLD.authorized_at) THEN
    RAISE EXCEPTION 'archive generation identity is immutable' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END; $$;
CREATE TRIGGER tg_archive_generation_immutable BEFORE UPDATE ON instagram_archive_generations FOR EACH ROW EXECUTE FUNCTION trg_archive_generation_immutable();

CREATE FUNCTION trg_archive_artifact_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.workspace_id, NEW.artifact_ref, NEW.source_id, NEW.data_generation_id, NEW.observation_revision, NEW.media_item_id, NEW.intent_id, NEW.kind, NEW.capture_epoch, NEW.policy_revision, NEW.selected_asset_identity, NEW.validation_fingerprint, NEW.upload_attempt_id, NEW.admitted_at)
     IS DISTINCT FROM (OLD.workspace_id, OLD.artifact_ref, OLD.source_id, OLD.data_generation_id, OLD.observation_revision, OLD.media_item_id, OLD.intent_id, OLD.kind, OLD.capture_epoch, OLD.policy_revision, OLD.selected_asset_identity, OLD.validation_fingerprint, OLD.upload_attempt_id, OLD.admitted_at) THEN
    RAISE EXCEPTION 'archive artifact capture is immutable' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END; $$;
CREATE TRIGGER tg_archive_artifact_immutable BEFORE UPDATE ON instagram_artifacts FOR EACH ROW EXECUTE FUNCTION trg_archive_artifact_immutable();

CREATE FUNCTION trg_archive_operation_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW IS DISTINCT FROM OLD THEN
    RAISE EXCEPTION 'archive issued operation capture is immutable' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END; $$;
CREATE TRIGGER tg_archive_operation_immutable BEFORE UPDATE ON instagram_operation_provenance FOR EACH ROW EXECUTE FUNCTION trg_archive_operation_immutable();

ALTER TABLE media_source_destinations ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_tenant ON media_source_destinations FOR ALL TO svc_ingress, svc_worker
  USING (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE ON media_source_destinations TO svc_ingress, svc_worker;
CREATE TRIGGER tg_touch_media_source_destinations BEFORE UPDATE ON media_source_destinations FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

ALTER TABLE instagram_archive_generations ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_tenant ON instagram_archive_generations FOR ALL TO svc_ingress, svc_worker
  USING (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE ON instagram_archive_generations TO svc_ingress, svc_worker;
CREATE TRIGGER tg_touch_instagram_archive_generations BEFORE UPDATE ON instagram_archive_generations FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

ALTER TABLE instagram_source_policies ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_tenant ON instagram_source_policies FOR ALL TO svc_ingress, svc_worker
  USING (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE ON instagram_source_policies TO svc_ingress, svc_worker;
CREATE TRIGGER tg_touch_instagram_source_policies BEFORE UPDATE ON instagram_source_policies FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

ALTER TABLE instagram_media_index ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_tenant ON instagram_media_index FOR ALL TO svc_ingress, svc_worker
  USING (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON instagram_media_index TO svc_ingress, svc_worker;
CREATE TRIGGER tg_touch_instagram_media_index BEFORE UPDATE ON instagram_media_index FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

ALTER TABLE instagram_item_tombstones ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_tenant ON instagram_item_tombstones FOR ALL TO svc_ingress, svc_worker
  USING (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE ON instagram_item_tombstones TO svc_ingress, svc_worker;

ALTER TABLE instagram_artifacts ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_tenant ON instagram_artifacts FOR ALL TO svc_ingress, svc_worker
  USING (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE ON instagram_artifacts TO svc_ingress, svc_worker;
CREATE TRIGGER tg_touch_instagram_artifacts BEFORE UPDATE ON instagram_artifacts FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

ALTER TABLE instagram_intent_provenance ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_tenant ON instagram_intent_provenance FOR ALL TO svc_ingress, svc_worker
  USING (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE ON instagram_intent_provenance TO svc_ingress, svc_worker;
CREATE TRIGGER tg_touch_instagram_intent_provenance BEFORE UPDATE ON instagram_intent_provenance FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

ALTER TABLE instagram_operation_provenance ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_tenant ON instagram_operation_provenance FOR ALL TO svc_ingress, svc_worker
  USING (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE ON instagram_operation_provenance TO svc_ingress, svc_worker;

CREATE FUNCTION trg_archive_media_generation() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE source_provider TEXT;
BEGIN
  SELECT provider INTO source_provider FROM media_sources
    WHERE workspace_id = NEW.workspace_id AND id = NEW.source_id;
  IF (source_provider = 'instagram_archive') <> (NEW.source_data_generation_id IS NOT NULL OR NEW.source_projection_redacted) THEN
    RAISE EXCEPTION 'archive projection requires its authorization generation' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END; $$;
CREATE TRIGGER tg_archive_media_generation BEFORE INSERT OR UPDATE OF source_id, source_data_generation_id, source_projection_redacted ON media_items FOR EACH ROW EXECUTE FUNCTION trg_archive_media_generation();

CREATE FUNCTION trg_archive_observation_revision() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.observation_revision < OLD.observation_revision THEN
    RAISE EXCEPTION 'archive observation revision cannot go backwards' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END; $$;
CREATE TRIGGER tg_archive_observation_revision BEFORE UPDATE ON instagram_media_index FOR EACH ROW EXECUTE FUNCTION trg_archive_observation_revision();

-- Trusted source_policy context supplies the one Python-resolved TTL and DB time
-- while retaining canonical workspace/source locks. No SQL TTL fallback or client
-- time/activation override. This read relation never replaces the INSERT arbiter.
CREATE FUNCTION fn_source_asset_suitability(
  p_workspace_id uuid, p_destination_id uuid, p_resolved_repost_ttl_days integer,
  p_evaluated_at timestamptz, p_archive_enabled boolean,
  p_source_id uuid DEFAULT NULL, p_media_item_id uuid DEFAULT NULL,
  p_existing_intent_id uuid DEFAULT NULL)
RETURNS TABLE (source_id uuid, media_item_id uuid, provider text,
  suitable boolean, base_suitable boolean, reason text,
  data_generation_id uuid, observation_revision bigint, policy_revision bigint,
  source_use_epoch bigint, effective_minimum_age_days bigint,
  live_intent_conflict boolean, blocking_locks text[], warning_locks text[])
LANGUAGE sql STABLE SECURITY INVOKER
SET search_path = pg_catalog, public, pg_temp AS $$
WITH source_facts AS (
  SELECT s.*, x.ratio, p.schema_version, p.revision AS policy_revision,
    p.minimum_age_days, p.include_credited, p.include_unknown,
    p.eligibility, p.ranking, p.destination_scope,
    g.id AS generation_id, g.lifecycle, c.state AS credential_state,
    c.expires_at AS credential_expires_at, c.provider AS credential_provider,
    a.state AS origin_state, a.provider_account_ref AS origin_ref,
    EXISTS (SELECT 1 FROM ig_accounts d WHERE d.workspace_id=s.workspace_id
      AND d.id=p_destination_id AND d.state='active') AS destination_active,
    EXISTS (SELECT 1 FROM media_source_destinations d WHERE d.workspace_id=s.workspace_id
      AND d.source_id=s.id AND d.ig_account_id=p_destination_id AND d.state='active') AS assigned,
    GREATEST(p.minimum_age_days::bigint,p_resolved_repost_ttl_days::bigint+1) AS effective_age
  FROM media_sources s
  LEFT JOIN category_post_case_mix x ON x.workspace_id=s.workspace_id AND x.source_id=s.id AND x.effective_to IS NULL
  LEFT JOIN instagram_source_policies p ON p.workspace_id=s.workspace_id AND p.source_id=s.id
  LEFT JOIN instagram_archive_generations g ON g.workspace_id=s.workspace_id AND g.source_id=s.id AND g.id=s.current_authorization_generation_id
  LEFT JOIN oauth_credentials c ON c.workspace_id=g.workspace_id AND c.ig_account_id=g.origin_ig_account_id AND c.id=g.credential_id
  LEFT JOIN ig_accounts a ON a.workspace_id=s.workspace_id AND a.id=s.origin_ig_account_id
  WHERE s.workspace_id=p_workspace_id AND (p_source_id IS NULL OR s.id=p_source_id)
), assets AS (
  SELECT s.id AS source_id, m.id AS media_item_id, s.provider, s.source_use_epoch,
    NULL::uuid AS data_generation_id, NULL::bigint AS observation_revision,
    NULL::bigint AS policy_revision, NULL::bigint AS effective_age,
    CASE
      WHEN p_evaluated_at IS NULL OR p_resolved_repost_ttl_days IS NULL THEN 'context_unavailable'
      WHEN s.state='error' OR COALESCE((s.config->>'removed')::boolean,false) THEN 'source_unavailable'
      WHEN s.ratio=0 THEN 'source_off'
      WHEN NOT s.destination_active THEN 'destination_not_allowed'
      WHEN m.state<>'available' THEN 'unavailable'
    END AS base_reason
  FROM source_facts s JOIN media_items m ON m.workspace_id=s.workspace_id AND m.source_id=s.id
  WHERE s.provider='gdrive' AND (p_media_item_id IS NULL OR m.id=p_media_item_id)
  UNION ALL
  SELECT s.id, m.id, s.provider, s.source_use_epoch,
    i.data_generation_id, i.observation_revision, s.policy_revision, s.effective_age,
    CASE
      WHEN p_archive_enabled IS NOT TRUE THEN 'archive_not_activated'
      WHEN p_evaluated_at IS NULL OR p_resolved_repost_ttl_days IS NULL THEN 'context_unavailable'
      WHEN s.state<>'active' OR COALESCE((s.config->>'removed')::boolean,false) THEN 'source_unavailable'
      WHEN COALESCE(s.ratio>0,false) IS NOT TRUE THEN 'source_off'
      WHEN s.origin_state IS DISTINCT FROM 'active' OR s.origin_ref LIKE 'manual:%' THEN 'origin_unavailable'
      WHEN s.generation_id IS NULL OR s.lifecycle<>'active' THEN 'generation_unavailable'
      WHEN s.credential_state IS DISTINCT FROM 'active' OR s.credential_provider IS DISTINCT FROM 'ig_login'
        OR s.credential_expires_at<=p_evaluated_at THEN 'credential_unavailable'
      WHEN s.schema_version IS DISTINCT FROM 1 OR s.eligibility IS DISTINCT FROM 'supported'
        OR s.ranking IS DISTINCT FROM 'none' OR s.destination_scope IS DISTINCT FROM 'origin_only' THEN 'unsupported_source_policy'
      WHEN i.data_generation_id IS DISTINCT FROM s.generation_id THEN 'awaiting_reobservation'
      WHEN i.exclusion_reason='origin_identity_conflict' OR m.source_projection_redacted IS TRUE THEN 'identity_conflict_or_deleted'
      WHEN i.availability='excluded' AND i.exclusion_reason='unsupported_media' THEN 'unsupported'
      WHEN i.availability IS DISTINCT FROM 'available' OR (m.id IS NOT NULL AND m.state<>'available') THEN 'unavailable'
      WHEN i.validation_state='unsupported' OR i.exclusion_reason='unsupported_media' THEN 'unsupported'
      WHEN m.id IS NULL OR i.validation_state IS DISTINCT FROM 'supported'
        OR i.validation_generation_id IS DISTINCT FROM i.data_generation_id
        OR i.validation_observation_revision IS DISTINCT FROM i.observation_revision
        OR i.validation_fingerprint IS NULL OR i.validated_at IS NULL
        OR m.source_data_generation_id IS DISTINCT FROM i.data_generation_id THEN 'validation_pending'
      WHEN p_evaluated_at-i.original_at < s.effective_age*interval '1 day' THEN 'too_recent'
      WHEN i.attribution='credited' AND s.include_credited IS NOT TRUE THEN 'credited_excluded'
      WHEN i.attribution='unknown' AND s.include_unknown IS NOT TRUE THEN 'attribution_unknown'
      WHEN NOT s.destination_active OR s.origin_ig_account_id<>p_destination_id OR NOT s.assigned THEN 'destination_not_allowed'
    END
  FROM source_facts s JOIN instagram_media_index i ON i.workspace_id=s.workspace_id AND i.source_id=s.id
  LEFT JOIN media_items m ON m.workspace_id=i.workspace_id AND m.source_id=i.source_id AND m.id=i.media_item_id
  WHERE s.provider='instagram_archive' AND (p_media_item_id IS NULL OR m.id=p_media_item_id)
), context AS (
  SELECT a.*,
    EXISTS (SELECT 1 FROM post_intents p WHERE p.workspace_id=p_workspace_id
      AND p.media_item_id=a.media_item_id AND p.ig_account_id=p_destination_id
      AND p.state NOT IN ('posted','skipped','rejected','expired','failed','cancelled')
      AND (p_existing_intent_id IS NULL OR p.id<>p_existing_intent_id)) AS live_intent_conflict,
    ARRAY(SELECT DISTINCT l.kind FROM post_locks l WHERE l.workspace_id=p_workspace_id
      AND l.media_item_id=a.media_item_id AND (l.ig_account_id IS NULL OR l.ig_account_id=p_destination_id)
      AND (l.expires_at IS NULL OR l.expires_at>p_evaluated_at)
      AND l.kind IN ('reject','unsupported','hold','seasonal') ORDER BY l.kind) AS blocking_locks,
    ARRAY(SELECT DISTINCT l.kind FROM post_locks l WHERE l.workspace_id=p_workspace_id
      AND l.media_item_id=a.media_item_id AND (l.ig_account_id IS NULL OR l.ig_account_id=p_destination_id)
      AND (l.expires_at IS NULL OR l.expires_at>p_evaluated_at)
      AND l.kind IN ('skip','recent') ORDER BY l.kind) AS warning_locks
  FROM assets a
)
SELECT c.source_id,c.media_item_id,c.provider,
  c.base_reason IS NULL AND NOT c.live_intent_conflict AND cardinality(c.blocking_locks)=0 AND cardinality(c.warning_locks)=0,
  c.base_reason IS NULL,
  COALESCE(c.base_reason,CASE WHEN c.live_intent_conflict THEN 'live_intent'
    WHEN cardinality(c.blocking_locks)>0 OR cardinality(c.warning_locks)>0 THEN 'locked' END),
  c.data_generation_id,c.observation_revision,c.policy_revision,c.source_use_epoch,c.effective_age,
  c.live_intent_conflict,c.blocking_locks,c.warning_locks
FROM context c
$$;
REVOKE ALL ON FUNCTION fn_source_asset_suitability(uuid,uuid,integer,timestamptz,boolean,uuid,uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_source_asset_suitability(uuid,uuid,integer,timestamptz,boolean,uuid,uuid,uuid) TO svc_ingress,svc_worker;
COMMENT ON FUNCTION fn_source_asset_suitability(uuid,uuid,integer,timestamptz,boolean,uuid,uuid,uuid) IS
  'Invoker/RLS source and asset verdicts. Internal source_policy context owns DB evaluation time, Python-resolved workspace TTL and activation under canonical retained locks. Drive remains workspace-wide. Existing-intent context excludes only its own matching live intent; lock facts do not override base suitability or the database duplicate INSERT arbiter. No phase-1 consumer wiring.';

-- 10K real-role EXPLAIN demonstrates these exact lookup paths; no ranking indexes.
CREATE INDEX ix_archive_tombstone_lookup ON instagram_item_tombstones (workspace_id,key_version,parent_hmac);
CREATE INDEX ix_archive_generation_lookup ON instagram_media_index (workspace_id,source_id,data_generation_id);
```

```sql
-- Current source scanning authority and canonical source-before-lease job doors.
-- No production archive capability is enabled by this migration.
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc WHERE oid='fn_source_read_authority(uuid,uuid,boolean)'::regprocedure AND NOT prosecdef)
CREATE FUNCTION fn_source_read_authority(p_workspace_id uuid,p_source_id uuid,p_activated boolean)
RETURNS TABLE(source_id uuid,provider text,admitted boolean,reason text,
 source_use_epoch bigint,generation_id uuid,credential_id uuid,account_ref text)
LANGUAGE sql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public,pg_temp AS $$
WITH facts AS (
 SELECT s.id,s.provider,s.source_use_epoch,g.id AS generation_id,c.id AS credential_id,
 a.provider_account_ref AS account_ref,
 CASE
 WHEN s.provider='gdrive' THEN NULL
 WHEN s.provider<>'instagram_archive' THEN 'unsupported_source_provider'
 WHEN p_activated IS NOT TRUE OR w.source_mix_policy_version IS DISTINCT FROM 1
  OR w.source_mix_conversion_version IS DISTINCT FROM 1 OR w.source_mix_revision IS NULL
  OR w.source_mix_converted_at IS NULL OR (w.source_mix_mode IN ('equal','custom')) IS NOT TRUE
  OR w.source_membership_policy IS DISTINCT FROM 'reset_equal' THEN 'archive_not_activated'
 WHEN s.state<>'active' OR COALESCE((s.config->>'removed')::boolean,false) THEN 'source_unavailable'
 WHEN COALESCE(x.ratio>0,false) IS NOT TRUE THEN 'source_off'
 WHEN a.state IS DISTINCT FROM 'active' OR a.provider_account_ref LIKE 'manual:%'
  OR NOT EXISTS(SELECT 1 FROM media_source_destinations d WHERE d.workspace_id=s.workspace_id
   AND d.source_id=s.id AND d.ig_account_id=s.origin_ig_account_id AND d.state='active') THEN 'origin_unavailable'
 WHEN g.id IS NULL OR g.lifecycle<>'active' THEN 'generation_unavailable'
 WHEN c.state IS DISTINCT FROM 'active' OR c.provider IS DISTINCT FROM 'ig_login'
  OR c.expires_at<=clock_timestamp() THEN 'credential_unavailable'
 END AS reason
 FROM media_sources s JOIN workspaces w ON w.id=s.workspace_id
 LEFT JOIN category_post_case_mix x ON x.workspace_id=s.workspace_id AND x.source_id=s.id AND x.effective_to IS NULL
 LEFT JOIN ig_accounts a ON a.workspace_id=s.workspace_id AND a.id=s.origin_ig_account_id
 LEFT JOIN instagram_archive_generations g ON g.workspace_id=s.workspace_id AND g.source_id=s.id AND g.id=s.current_authorization_generation_id
 LEFT JOIN oauth_credentials c ON c.workspace_id=g.workspace_id AND c.ig_account_id=g.origin_ig_account_id AND c.id=g.credential_id
 WHERE s.workspace_id=p_workspace_id AND s.id=p_source_id
)
SELECT id,provider,reason IS NULL,reason,source_use_epoch,generation_id,credential_id,account_ref FROM facts
$$;
REVOKE ALL ON FUNCTION fn_source_read_authority(uuid,uuid,boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_source_read_authority(uuid,uuid,boolean) TO svc_ingress,svc_worker,svc_claim,svc_clock,svc_maintenance;
COMMENT ON FUNCTION fn_source_read_authority(uuid,uuid,boolean) IS 'Internal current source-read facts only; no item, age, attribution, credit or destination-pool prerequisite. Invoker RLS and explicit tenant predicates remain authoritative. Expiry uses fresh database wall time.';

GRANT SELECT (id,source_mix_policy_version,source_mix_conversion_version,source_mix_revision,source_mix_converted_at,source_mix_mode,source_membership_policy) ON workspaces TO svc_claim,svc_clock,svc_maintenance;
CREATE POLICY p_source_read ON workspaces FOR SELECT TO svc_claim,svc_clock,svc_maintenance USING (true);
GRANT UPDATE (id) ON workspaces TO svc_claim,svc_clock,svc_maintenance;
CREATE POLICY p_source_read_lock ON workspaces FOR UPDATE TO svc_claim,svc_clock,svc_maintenance USING (true) WITH CHECK (true);

GRANT SELECT (id,workspace_id,state,provider_account_ref) ON ig_accounts TO svc_claim,svc_clock,svc_maintenance;
CREATE POLICY p_source_read ON ig_accounts FOR SELECT TO svc_claim,svc_clock,svc_maintenance USING (true);
GRANT UPDATE (id) ON ig_accounts TO svc_claim,svc_clock,svc_maintenance;
CREATE POLICY p_source_read_lock ON ig_accounts FOR UPDATE TO svc_claim,svc_clock,svc_maintenance USING (true) WITH CHECK (true);

GRANT SELECT (id,workspace_id,provider,state,config,origin_ig_account_id,current_authorization_generation_id,source_use_epoch,next_sync_at) ON media_sources TO svc_claim,svc_clock,svc_maintenance;
CREATE POLICY p_source_read ON media_sources FOR SELECT TO svc_claim,svc_clock,svc_maintenance USING (true);
GRANT UPDATE (id) ON media_sources TO svc_claim,svc_clock,svc_maintenance;
CREATE POLICY p_source_read_lock ON media_sources FOR UPDATE TO svc_claim,svc_clock,svc_maintenance USING (true) WITH CHECK (true);

GRANT SELECT (id,workspace_id,ig_account_id,provider,state,expires_at) ON oauth_credentials TO svc_claim,svc_clock,svc_maintenance;
CREATE POLICY p_source_read ON oauth_credentials FOR SELECT TO svc_claim,svc_clock,svc_maintenance USING (true);
GRANT UPDATE (id) ON oauth_credentials TO svc_claim,svc_clock,svc_maintenance;
CREATE POLICY p_source_read_lock ON oauth_credentials FOR UPDATE TO svc_claim,svc_clock,svc_maintenance USING (true) WITH CHECK (true);

GRANT SELECT (id,workspace_id,source_id,origin_ig_account_id,credential_id,lifecycle) ON instagram_archive_generations TO svc_claim,svc_clock,svc_maintenance;
CREATE POLICY p_source_read ON instagram_archive_generations FOR SELECT TO svc_claim,svc_clock,svc_maintenance USING (true);
GRANT UPDATE (id) ON instagram_archive_generations TO svc_claim,svc_clock,svc_maintenance;
CREATE POLICY p_source_read_lock ON instagram_archive_generations FOR UPDATE TO svc_claim,svc_clock,svc_maintenance USING (true) WITH CHECK (true);

GRANT SELECT (workspace_id,source_id,ratio,effective_to) ON category_post_case_mix TO svc_claim,svc_clock,svc_maintenance;
CREATE POLICY p_source_read ON category_post_case_mix FOR SELECT TO svc_claim,svc_clock,svc_maintenance USING (true);

GRANT SELECT (workspace_id,source_id,ig_account_id,state) ON media_source_destinations TO svc_claim,svc_clock,svc_maintenance;
CREATE POLICY p_source_read ON media_source_destinations FOR SELECT TO svc_claim,svc_clock,svc_maintenance USING (true);

CREATE FUNCTION fn_lock_source_read_prefix(p_ws uuid)
RETURNS void LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended('sources:'||p_ws,0));
 PERFORM pg_advisory_xact_lock(hashtextextended('case_mix:'||p_ws,0));
 PERFORM id FROM workspaces WHERE id=p_ws FOR UPDATE;
 PERFORM id FROM ig_accounts WHERE workspace_id=p_ws ORDER BY id FOR UPDATE;
 PERFORM id FROM media_sources WHERE workspace_id=p_ws ORDER BY id FOR UPDATE;
 PERFORM id FROM oauth_credentials WHERE workspace_id=p_ws ORDER BY id FOR UPDATE;
 PERFORM id FROM instagram_archive_generations WHERE workspace_id=p_ws ORDER BY id FOR UPDATE;
END $$;
REVOKE ALL ON FUNCTION fn_lock_source_read_prefix(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_lock_source_read_prefix(uuid) TO svc_claim,svc_clock,svc_maintenance;

CREATE FUNCTION fn_source_read_capture(p_ws uuid,p_sid uuid,p_activated boolean)
RETURNS jsonb LANGUAGE sql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT jsonb_build_object('v',1,'workspace_id',p_ws,'source_id',source_id,'provider',provider,
 'source_use_epoch',source_use_epoch,'generation_id',generation_id,'credential_id',credential_id,'account_ref',account_ref)
 FROM fn_source_read_authority(p_ws,p_sid,p_activated) WHERE admitted
$$;
REVOKE ALL ON FUNCTION fn_source_read_capture(uuid,uuid,boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_source_read_capture(uuid,uuid,boolean) TO svc_claim,svc_clock,svc_maintenance;

CREATE FUNCTION fn_source_job_read_reason(p_ws uuid,p_payload jsonb,p_activated boolean)
RETURNS text LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE a record; capture jsonb:=p_payload->'source_read_capture';
BEGIN
 SELECT r.* INTO a FROM media_sources s CROSS JOIN LATERAL fn_source_read_authority(p_ws,s.id,p_activated) r
 WHERE s.workspace_id=p_ws AND s.id::text=p_payload->>'source_id';
 IF NOT FOUND OR a.provider<>'instagram_archive' THEN RETURN NULL; END IF;
 IF jsonb_typeof(capture) IS DISTINCT FROM 'object' OR capture->'v' IS DISTINCT FROM '1'::jsonb
  OR NOT capture ?& ARRAY['workspace_id','source_id','provider','source_use_epoch','generation_id','credential_id','account_ref']
  OR capture->>'source_use_epoch' IS NULL OR capture->>'generation_id' IS NULL OR capture->>'credential_id' IS NULL OR capture->>'account_ref' IS NULL
  THEN RETURN 'source_capture_missing'; END IF;
 IF NOT a.admitted THEN RETURN a.reason; END IF;
 IF capture->>'workspace_id' IS DISTINCT FROM p_ws::text OR capture->>'source_id' IS DISTINCT FROM a.source_id::text
  OR capture->>'provider' IS DISTINCT FROM a.provider THEN RETURN 'source_capture_mismatch'; END IF;
 IF capture->>'generation_id' IS DISTINCT FROM a.generation_id::text THEN RETURN 'source_generation_changed'; END IF;
 IF capture->>'source_use_epoch' IS DISTINCT FROM a.source_use_epoch::text THEN RETURN 'source_epoch_changed'; END IF;
 IF capture->>'credential_id' IS DISTINCT FROM a.credential_id::text OR capture->>'account_ref' IS DISTINCT FROM a.account_ref
  THEN RETURN 'source_credential_changed'; END IF;
 RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION fn_source_job_read_reason(uuid,jsonb,boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_source_job_read_reason(uuid,jsonb,boolean) TO svc_claim,svc_clock,svc_maintenance;

GRANT CREATE ON SCHEMA public TO svc_claim,svc_clock,svc_maintenance;
CREATE FUNCTION fn_claim_job(p_lane text,p_worker text,p_lease interval,p_ws_lane_cap int,p_archive_enabled boolean)
RETURNS SETOF jobs LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE candidate record; selected jobs; refusal text; source_prefix_applied boolean;
BEGIN
 -- Discovery owns no job lock. Source-read kinds take the canonical prefix before
 -- the LAST-class lease. Other kinds retain the raw unique-index race mechanism.
 -- Publish/effect consumers adopt their own prefix only in phase 3.
 FOR candidate IN SELECT j.id,j.workspace_id,j.kind FROM jobs j
  WHERE j.state='ready' AND j.lane=p_lane AND j.run_at<=now()
  ORDER BY j.run_at,j.id
 LOOP
  BEGIN
   source_prefix_applied:=false;
   IF candidate.workspace_id IS NOT NULL AND candidate.kind IN ('sync_media_source','first_ingest_chunk') THEN
    IF NOT pg_try_advisory_xact_lock(hashtextextended('sources:'||candidate.workspace_id,0)) THEN
     RAISE SQLSTATE 'P0002';
    END IF;
    PERFORM fn_lock_source_read_prefix(candidate.workspace_id);
    source_prefix_applied:=true;
   END IF;
   SELECT j.* INTO selected FROM jobs j WHERE j.id=candidate.id AND j.state='ready' AND j.run_at<=now()
    AND NOT EXISTS(SELECT 1 FROM jobs h WHERE h.serialization_key=j.serialization_key AND h.state='leased')
    AND NOT EXISTS(SELECT 1 FROM provider_quarantine q WHERE q.workspace_id=j.workspace_id
     AND q.scope_ref=j.serialization_key AND q.quarantined_until>now())
    AND (j.workspace_id IS NULL OR (SELECT count(*) FROM jobs w WHERE w.state='leased' AND w.lane=j.lane
     AND w.workspace_id=j.workspace_id)<p_ws_lane_cap)
    FOR UPDATE OF j SKIP LOCKED;
   IF NOT FOUND THEN RAISE SQLSTATE 'P0002'; END IF;
   IF selected.kind IN ('sync_media_source','first_ingest_chunk') AND
    (NOT source_prefix_applied OR selected.workspace_id IS DISTINCT FROM candidate.workspace_id) THEN
    RAISE SQLSTATE 'P0002'; -- changed identity: release the job row; discover afresh next turn
   END IF;
   refusal:=NULL;
   IF selected.kind IN ('sync_media_source','first_ingest_chunk') THEN
    refusal:=fn_source_job_read_reason(selected.workspace_id,selected.payload,p_archive_enabled);
   END IF;
   IF refusal IS NOT NULL THEN
    UPDATE jobs SET state='failed',payload=payload||jsonb_build_object('source_read_refusal',refusal)
     WHERE id=selected.id AND state='ready';
    CONTINUE;
   END IF;
   RETURN QUERY UPDATE jobs SET state='leased',locked_by=p_worker,lease_token=gen_random_uuid(),
    locked_until=now()+p_lease,attempts=attempts+1 WHERE id=selected.id AND state='ready' RETURNING jobs.*;
   RETURN;
  EXCEPTION WHEN no_data_found THEN NULL;
  END;
 END LOOP;
END $$;
ALTER FUNCTION fn_claim_job(text,text,interval,int,boolean) OWNER TO svc_claim;
REVOKE ALL ON FUNCTION fn_claim_job(text,text,interval,int,boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_claim_job(text,text,interval,int,boolean) TO svc_worker;
CREATE OR REPLACE FUNCTION fn_claim_job(p_lane text,p_worker text,p_lease interval,p_ws_lane_cap int)
RETURNS SETOF jobs LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT * FROM fn_claim_job(p_lane,p_worker,p_lease,p_ws_lane_cap,false)
$$;

CREATE FUNCTION fn_clock_tick(p_max int, p_refresh_cadence interval,
                              p_recurring jsonb, p_archive_enabled boolean)  -- {v:1, "<kind>": seconds, …} (05 seam)
RETURNS TABLE (o_slot_jobs int, o_refresh_jobs int, o_sync_jobs int,
               o_recurring_jobs int, archive_reauth_jobs int)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE locked_workspaces uuid[]:='{}'; source_due record; source_capture jsonb; lock_ws uuid; k text; cadence interval; last_done timestamptz; rem int;
        due_at timestamptz;   -- 083: the singleton's run_at, named so its
                              -- deadline can be measured FROM it
        n1 int := 0; n2 int := 0; n3 int := 0; n4 int := 0; n5 int := 0;
BEGIN
  PERFORM set_config('app.actor_kind', 'clock', true);
  -- Lock a bounded discovery set before the recurring job/account/credential legs.
  FOR lock_ws IN SELECT DISTINCT workspace_id FROM (
   (SELECT a.workspace_id FROM ig_accounts a JOIN workspaces w ON w.id=a.workspace_id
    WHERE a.state='active' AND a.next_slot_at IS NOT NULL AND a.next_slot_at<=now()
    AND w.state='active' AND NOT w.is_paused ORDER BY a.next_slot_at,a.id LIMIT GREATEST(p_max,0))
   UNION (SELECT workspace_id FROM ig_accounts WHERE state='reauth_required'
    AND (last_reauth_prompt_at IS NULL OR last_reauth_prompt_at<=now()-interval '7 days')
    ORDER BY id LIMIT GREATEST(p_max,0))
   UNION (SELECT workspace_id FROM oauth_credentials WHERE state='active' AND provider='ig_login'
    AND next_refresh_at IS NOT NULL AND next_refresh_at<=now() ORDER BY id LIMIT GREATEST(p_max,0))
   UNION (SELECT s.workspace_id FROM media_sources s WHERE s.state='active' AND s.next_sync_at IS NOT NULL
    AND s.next_sync_at<=now() AND EXISTS(SELECT 1 FROM fn_source_read_authority(s.workspace_id,s.id,p_archive_enabled) r WHERE r.admitted)
    ORDER BY s.next_sync_at,s.id LIMIT GREATEST(p_max,0))
  ) candidate ORDER BY workspace_id LOOP
   PERFORM fn_lock_source_read_prefix(lock_ws);
   locked_workspaces:=array_append(locked_workspaces,lock_ws);
  END LOOP;
  -- (1) recurring system singletons: if no ready/leased row holds the kind's singleton key,
  -- insert the next run at last-completion + cadence (or now, whichever is later):
  FOR k, cadence IN
    SELECT key, (value::text)::numeric * interval '1 second'
      FROM jsonb_each(p_recurring) WHERE key <> 'v'
  LOOP
    EXIT WHEN n4 >= p_max;
    -- 084: this kind's own expired lease goes back to `ready` BEFORE the guard
    -- reads it (#1329). The reaper re-readies every other kind's dead lease,
    -- but it is one of these singletons and cannot revive itself; left
    -- `leased`, a dead lease reads as live to the guard below for ever. The
    -- four assignments are the reaper's first leg's (076), scoped to the kind:
    UPDATE jobs SET state = 'ready', locked_by = NULL, lease_token = NULL, locked_until = NULL
     WHERE kind = k AND workspace_id IS NULL
       AND state = 'leased' AND locked_until < now();
    IF NOT EXISTS (SELECT 1 FROM jobs
                   WHERE kind = k AND state IN ('ready','leased')) THEN
      SELECT max(updated_at) INTO last_done FROM jobs
       WHERE kind = k AND state = 'succeeded';
      -- 083: from run_at, NOT from now(). A singleton's run_at is its last
      -- completion plus its cadence, which for a slow cadence is well ahead of
      -- this tick; a deadline anchored at mint would be spent before the job
      -- was ever claimable and would end it on its first run.
      due_at := GREATEST(now(), COALESCE(last_done + cadence, now()));
      INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, deadline_at, payload)
      VALUES (k, NULL, 'bulk', k,            -- system singletons key on their kind (§5 registry)
              due_at, 3,
              due_at + interval '6 hours',   -- LANE_BUDGETS['bulk'][1]
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
     WHERE a.workspace_id=ANY(locked_workspaces) AND a.state = 'active' AND a.next_slot_at IS NOT NULL AND a.next_slot_at <= now()
       AND w.state = 'active' AND NOT w.is_paused
     ORDER BY a.next_slot_at,a.id LIMIT rem
  ), ins AS (
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, deadline_at, payload)
    SELECT 'plan_slot', d.workspace_id, 'bulk', 'acct:' || d.id, now(), 3, now() + interval '6 hours',
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
     WHERE workspace_id=ANY(locked_workspaces) AND state = 'active' AND next_refresh_at IS NOT NULL AND next_refresh_at <= now()
       AND provider = 'ig_login'   -- 063: see the header. Fails CLOSED for any new provider.
     ORDER BY id LIMIT rem
  ), ins AS (
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, deadline_at, payload)
    SELECT 'refresh_credential', d.workspace_id, 'bulk', 'cred:' || d.id, now(), 5, now() + interval '6 hours',
           jsonb_build_object('v', 1, 'credential_id', d.id)
      FROM due d
  )
  UPDATE oauth_credentials c SET next_refresh_at = now() + p_refresh_cadence
    FROM due d WHERE c.id = d.id;
  GET DIAGNOSTICS n2 = ROW_COUNT;
  rem := rem - n2;
  -- (4) due source syncs — same shape (H4's slow jittered baseline; pre-slot/demand syncs are
  -- produced by their own sites — the tick owns only the baseline). ix_sources_sync_due serves it:
  FOR source_due IN SELECT s.id,s.workspace_id FROM media_sources s
   WHERE s.workspace_id=ANY(locked_workspaces) AND s.state='active' AND s.next_sync_at IS NOT NULL AND s.next_sync_at<=now()
   AND EXISTS(SELECT 1 FROM fn_source_read_authority(s.workspace_id,s.id,p_archive_enabled) r WHERE r.admitted)
   ORDER BY s.next_sync_at,s.id LIMIT rem
  LOOP
   source_capture:=fn_source_read_capture(source_due.workspace_id,source_due.id,p_archive_enabled);
   IF source_capture IS NULL THEN CONTINUE; END IF;
   INSERT INTO jobs(kind,workspace_id,lane,serialization_key,run_at,max_attempts,deadline_at,payload)
    VALUES('sync_media_source',source_due.workspace_id,'bulk','src:'||source_due.id,now(),5,now()+interval '6 hours',
     jsonb_build_object('v',1,'source_id',source_due.id,'reason','baseline') ||
     CASE WHEN source_capture->>'provider'='instagram_archive' THEN jsonb_build_object('source_read_capture',source_capture) ELSE '{}'::jsonb END);
   UPDATE media_sources SET next_sync_at=NULL WHERE id=source_due.id AND workspace_id=source_due.workspace_id;
   n3:=n3+1;
  END LOOP;
  rem := rem - n3;
  -- (5) reauth prompts for accounts sitting reauth_required (`02` §5 :1165; `05`: 1/week).
  -- Marker stamped at MINT, symmetric with legs 2-4's re-arm-at-mint shape; the NOT EXISTS
  -- guards a still-open prompt job so a slow executor cannot pile up prompts for one account.
  -- ix_ig_accounts_reauth_due serves the scan:
  WITH due AS (
    SELECT a.id, a.workspace_id, a.provider_account_ref
      FROM ig_accounts a
     WHERE a.workspace_id=ANY(locked_workspaces) AND a.state = 'reauth_required'
       AND (a.last_reauth_prompt_at IS NULL
            OR a.last_reauth_prompt_at <= now() - interval '7 days')
       AND NOT EXISTS (SELECT 1 FROM jobs j
                        WHERE j.kind = 'reauth_prompt'
                          AND j.serialization_key = 'ig:' || a.provider_account_ref
                          AND j.state IN ('ready','leased'))
     ORDER BY a.id LIMIT rem
  ), ins AS (
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, deadline_at, payload)
    SELECT 'reauth_prompt', d.workspace_id, 'bulk', 'ig:' || d.provider_account_ref, now(), 3, now() + interval '6 hours',
           jsonb_build_object('v', 1, 'ig_account_id', d.id)
      FROM due d
  )
  UPDATE ig_accounts a SET last_reauth_prompt_at = now()
    FROM due d WHERE a.id = d.id;
  GET DIAGNOSTICS n5 = ROW_COUNT;
  RETURN QUERY SELECT n1, n2, n3, n4, n5;
END $$;


ALTER FUNCTION fn_clock_tick(int,interval,jsonb,boolean) OWNER TO svc_clock;
REVOKE ALL ON FUNCTION fn_clock_tick(int,interval,jsonb,boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_clock_tick(int,interval,jsonb,boolean) TO svc_worker;
CREATE OR REPLACE FUNCTION fn_clock_tick(p_max int, p_refresh_cadence interval,
                              p_recurring jsonb)  -- {v:1, "<kind>": seconds, …} (05 seam)
RETURNS TABLE (o_slot_jobs int, o_refresh_jobs int, o_sync_jobs int,
               o_recurring_jobs int, o_reauth_jobs int)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE p_archive_enabled boolean := false; locked_workspaces uuid[]:='{}'; source_due record; source_capture jsonb; lock_ws uuid; k text; cadence interval; last_done timestamptz; rem int;
        due_at timestamptz;   -- 083: the singleton's run_at, named so its
                              -- deadline can be measured FROM it
        n1 int := 0; n2 int := 0; n3 int := 0; n4 int := 0; n5 int := 0;
BEGIN
  PERFORM set_config('app.actor_kind', 'clock', true);
  -- Lock a bounded discovery set before the recurring job/account/credential legs.
  FOR lock_ws IN SELECT DISTINCT workspace_id FROM (
   (SELECT a.workspace_id FROM ig_accounts a JOIN workspaces w ON w.id=a.workspace_id
    WHERE a.state='active' AND a.next_slot_at IS NOT NULL AND a.next_slot_at<=now()
    AND w.state='active' AND NOT w.is_paused ORDER BY a.next_slot_at,a.id LIMIT GREATEST(p_max,0))
   UNION (SELECT workspace_id FROM ig_accounts WHERE state='reauth_required'
    AND (last_reauth_prompt_at IS NULL OR last_reauth_prompt_at<=now()-interval '7 days')
    ORDER BY id LIMIT GREATEST(p_max,0))
   UNION (SELECT workspace_id FROM oauth_credentials WHERE state='active' AND provider='ig_login'
    AND next_refresh_at IS NOT NULL AND next_refresh_at<=now() ORDER BY id LIMIT GREATEST(p_max,0))
   UNION (SELECT s.workspace_id FROM media_sources s WHERE s.state='active' AND s.next_sync_at IS NOT NULL
    AND s.next_sync_at<=now() AND EXISTS(SELECT 1 FROM fn_source_read_authority(s.workspace_id,s.id,p_archive_enabled) r WHERE r.admitted)
    ORDER BY s.next_sync_at,s.id LIMIT GREATEST(p_max,0))
  ) candidate ORDER BY workspace_id LOOP
   PERFORM fn_lock_source_read_prefix(lock_ws);
   locked_workspaces:=array_append(locked_workspaces,lock_ws);
  END LOOP;
  -- (1) recurring system singletons: if no ready/leased row holds the kind's singleton key,
  -- insert the next run at last-completion + cadence (or now, whichever is later):
  FOR k, cadence IN
    SELECT key, (value::text)::numeric * interval '1 second'
      FROM jsonb_each(p_recurring) WHERE key <> 'v'
  LOOP
    EXIT WHEN n4 >= p_max;
    -- 084: this kind's own expired lease goes back to `ready` BEFORE the guard
    -- reads it (#1329). The reaper re-readies every other kind's dead lease,
    -- but it is one of these singletons and cannot revive itself; left
    -- `leased`, a dead lease reads as live to the guard below for ever. The
    -- four assignments are the reaper's first leg's (076), scoped to the kind:
    UPDATE jobs SET state = 'ready', locked_by = NULL, lease_token = NULL, locked_until = NULL
     WHERE kind = k AND workspace_id IS NULL
       AND state = 'leased' AND locked_until < now();
    IF NOT EXISTS (SELECT 1 FROM jobs
                   WHERE kind = k AND state IN ('ready','leased')) THEN
      SELECT max(updated_at) INTO last_done FROM jobs
       WHERE kind = k AND state = 'succeeded';
      -- 083: from run_at, NOT from now(). A singleton's run_at is its last
      -- completion plus its cadence, which for a slow cadence is well ahead of
      -- this tick; a deadline anchored at mint would be spent before the job
      -- was ever claimable and would end it on its first run.
      due_at := GREATEST(now(), COALESCE(last_done + cadence, now()));
      INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, deadline_at, payload)
      VALUES (k, NULL, 'bulk', k,            -- system singletons key on their kind (§5 registry)
              due_at, 3,
              due_at + interval '6 hours',   -- LANE_BUDGETS['bulk'][1]
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
     WHERE a.workspace_id=ANY(locked_workspaces) AND a.state = 'active' AND a.next_slot_at IS NOT NULL AND a.next_slot_at <= now()
       AND w.state = 'active' AND NOT w.is_paused
     ORDER BY a.next_slot_at,a.id LIMIT rem
  ), ins AS (
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, deadline_at, payload)
    SELECT 'plan_slot', d.workspace_id, 'bulk', 'acct:' || d.id, now(), 3, now() + interval '6 hours',
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
     WHERE workspace_id=ANY(locked_workspaces) AND state = 'active' AND next_refresh_at IS NOT NULL AND next_refresh_at <= now()
       AND provider = 'ig_login'   -- 063: see the header. Fails CLOSED for any new provider.
     ORDER BY id LIMIT rem
  ), ins AS (
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, deadline_at, payload)
    SELECT 'refresh_credential', d.workspace_id, 'bulk', 'cred:' || d.id, now(), 5, now() + interval '6 hours',
           jsonb_build_object('v', 1, 'credential_id', d.id)
      FROM due d
  )
  UPDATE oauth_credentials c SET next_refresh_at = now() + p_refresh_cadence
    FROM due d WHERE c.id = d.id;
  GET DIAGNOSTICS n2 = ROW_COUNT;
  rem := rem - n2;
  -- (4) due source syncs — same shape (H4's slow jittered baseline; pre-slot/demand syncs are
  -- produced by their own sites — the tick owns only the baseline). ix_sources_sync_due serves it:
  FOR source_due IN SELECT s.id,s.workspace_id FROM media_sources s
   WHERE s.workspace_id=ANY(locked_workspaces) AND s.state='active' AND s.next_sync_at IS NOT NULL AND s.next_sync_at<=now()
   AND EXISTS(SELECT 1 FROM fn_source_read_authority(s.workspace_id,s.id,p_archive_enabled) r WHERE r.admitted)
   ORDER BY s.next_sync_at,s.id LIMIT rem
  LOOP
   source_capture:=fn_source_read_capture(source_due.workspace_id,source_due.id,p_archive_enabled);
   IF source_capture IS NULL THEN CONTINUE; END IF;
   INSERT INTO jobs(kind,workspace_id,lane,serialization_key,run_at,max_attempts,deadline_at,payload)
    VALUES('sync_media_source',source_due.workspace_id,'bulk','src:'||source_due.id,now(),5,now()+interval '6 hours',
     jsonb_build_object('v',1,'source_id',source_due.id,'reason','baseline') ||
     CASE WHEN source_capture->>'provider'='instagram_archive' THEN jsonb_build_object('source_read_capture',source_capture) ELSE '{}'::jsonb END);
   UPDATE media_sources SET next_sync_at=NULL WHERE id=source_due.id AND workspace_id=source_due.workspace_id;
   n3:=n3+1;
  END LOOP;
  rem := rem - n3;
  -- (5) reauth prompts for accounts sitting reauth_required (`02` §5 :1165; `05`: 1/week).
  -- Marker stamped at MINT, symmetric with legs 2-4's re-arm-at-mint shape; the NOT EXISTS
  -- guards a still-open prompt job so a slow executor cannot pile up prompts for one account.
  -- ix_ig_accounts_reauth_due serves the scan:
  WITH due AS (
    SELECT a.id, a.workspace_id, a.provider_account_ref
      FROM ig_accounts a
     WHERE a.workspace_id=ANY(locked_workspaces) AND a.state = 'reauth_required'
       AND (a.last_reauth_prompt_at IS NULL
            OR a.last_reauth_prompt_at <= now() - interval '7 days')
       AND NOT EXISTS (SELECT 1 FROM jobs j
                        WHERE j.kind = 'reauth_prompt'
                          AND j.serialization_key = 'ig:' || a.provider_account_ref
                          AND j.state IN ('ready','leased'))
     ORDER BY a.id LIMIT rem
  ), ins AS (
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, deadline_at, payload)
    SELECT 'reauth_prompt', d.workspace_id, 'bulk', 'ig:' || d.provider_account_ref, now(), 3, now() + interval '6 hours',
           jsonb_build_object('v', 1, 'ig_account_id', d.id)
      FROM due d
  )
  UPDATE ig_accounts a SET last_reauth_prompt_at = now()
    FROM due d WHERE a.id = d.id;
  GET DIAGNOSTICS n5 = ROW_COUNT;
  RETURN QUERY SELECT n1, n2, n3, n4, n5;
END $$;



CREATE FUNCTION fn_reaper_sweep(p_lim int, p_approval_ttl interval, p_approved_ttl interval,p_archive_enabled boolean)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE locked_workspaces uuid[]:='{}'; lock_ws uuid; expired_job record; refusal text; n int := 0; c int; rem int := GREATEST(p_lim, 0);
BEGIN
  PERFORM set_config('app.actor_kind', 'reaper', true);
  -- Lock all bounded candidate workspaces before any intent/item/lease rows.
  FOR lock_ws IN SELECT DISTINCT workspace_id FROM (
   (SELECT workspace_id FROM jobs WHERE state='leased' AND locked_until<now() ORDER BY id LIMIT GREATEST(p_lim,0))
   UNION (SELECT workspace_id FROM jobs WHERE state='ready' AND deadline_at<=now()
    AND kind IN ('reap_expired','reconcile_ambiguous','alert_stranded_sources','reap_transit_assets',
     'plan_slot','refresh_credential','reauth_prompt','deliver_outbox','sync_media_source','first_ingest_chunk') ORDER BY id LIMIT GREATEST(p_lim,0))
   UNION (SELECT workspace_id FROM post_intents WHERE cancel_requested AND cap_consumed_on IS NULL
    AND state IN ('scheduled','prompt_pending','awaiting_approval','approved') ORDER BY id LIMIT GREATEST(p_lim,0))
   UNION (SELECT workspace_id FROM post_intents WHERE state IN ('scheduled','prompt_pending')
    AND schedule_slot_at<now() AND origin='cadence' ORDER BY id LIMIT GREATEST(p_lim,0))
   UNION (SELECT i.workspace_id FROM post_intents i JOIN workspaces w ON w.id=i.workspace_id
    WHERE i.state='awaiting_approval' AND i.entered_state_at<now()-COALESCE(w.approval_ttl_minutes*interval '1 minute',p_approval_ttl)
    ORDER BY i.id LIMIT GREATEST(p_lim,0))
   UNION (SELECT workspace_id FROM post_locks WHERE expires_at IS NOT NULL AND expires_at<now() ORDER BY id LIMIT GREATEST(p_lim,0))
  ) candidate WHERE workspace_id IS NOT NULL ORDER BY workspace_id LOOP
   PERFORM fn_lock_source_read_prefix(lock_ws);
   locked_workspaces:=array_append(locked_workspaces,lock_ws);
  END LOOP;
  c:=0;
  FOR expired_job IN SELECT id,workspace_id,kind,payload FROM jobs
   WHERE (workspace_id IS NULL OR workspace_id=ANY(locked_workspaces)) AND state='leased' AND locked_until<now() ORDER BY id LIMIT rem
  LOOP
   refusal:=NULL;
   IF expired_job.kind IN ('sync_media_source','first_ingest_chunk') THEN
    refusal:=fn_source_job_read_reason(expired_job.workspace_id,expired_job.payload,p_archive_enabled);
   END IF;
   UPDATE jobs SET state=CASE WHEN refusal IS NULL THEN 'ready' ELSE 'failed' END,
    locked_by=NULL,lease_token=NULL,locked_until=NULL,
    payload=CASE WHEN refusal IS NULL THEN payload ELSE payload||jsonb_build_object('source_read_refusal',refusal) END
   WHERE id=expired_job.id AND state='leased' AND locked_until<now();
   IF FOUND THEN c:=c+1; END IF;
  END LOOP;
  n:=n+c;rem:=rem-c;
  -- 087: a cancel the user asked for ends `cancelled` (#1235), ahead of the
  -- expiry legs; a debited row is left to the paths that refund it first.
  UPDATE post_intents SET state = 'cancelled'
   WHERE id IN (SELECT id FROM post_intents
                 WHERE workspace_id=ANY(locked_workspaces) AND cancel_requested AND cap_consumed_on IS NULL
                   AND state IN ('scheduled','prompt_pending','awaiting_approval','approved')
                 ORDER BY id LIMIT rem)
     AND state IN ('scheduled','prompt_pending','awaiting_approval','approved');  -- rechecked on a changed row
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  -- 089: the slot expiry takes cadence rows only (#1413). A planned row
  -- that is due and unserved ends through the miss door
  -- (fn_planned_misses), which tells the bound chats why; ended here, it
  -- would end in silence.
  UPDATE post_intents SET state = 'expired'
   WHERE id IN (SELECT id FROM post_intents
                 WHERE workspace_id=ANY(locked_workspaces) AND state IN ('scheduled','prompt_pending') AND schedule_slot_at < now()
                   AND origin = 'cadence'
                 ORDER BY id LIMIT rem);
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  UPDATE post_intents i SET state = 'expired'
   WHERE i.id IN (
     SELECT i2.id FROM post_intents i2 JOIN workspaces w ON w.id = i2.workspace_id
      WHERE i2.workspace_id=ANY(locked_workspaces) AND i2.state = 'awaiting_approval'
        AND i2.entered_state_at
            < now() - COALESCE(w.approval_ttl_minutes * interval '1 minute', p_approval_ttl)
      ORDER BY i2.id LIMIT rem);
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  DELETE FROM post_locks
   WHERE id IN (SELECT id FROM post_locks
                 WHERE workspace_id=ANY(locked_workspaces) AND expires_at IS NOT NULL AND expires_at < now() ORDER BY id LIMIT rem);
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  UPDATE workspace_invitations SET state = 'expired'
   WHERE id IN (SELECT id FROM workspace_invitations
                 WHERE state = 'pending' AND expires_at < now() ORDER BY id LIMIT rem);
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  DELETE FROM onboarding_sessions
   WHERE id IN (SELECT id FROM onboarding_sessions WHERE expires_at < now() ORDER BY id LIMIT rem);
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  -- 086: a `ready` job past its deadline ends `failed`, and says so (#1429),
  -- for the kinds a sweep re-mints; a sync kind's source is re-armed, as
  -- `work_loop._rearm_source` does on a spent budget. Last, so a backlog of
  -- expired jobs cannot take the budget of the legs above.
  WITH ended AS (
    UPDATE jobs SET state = 'failed',
                    payload = payload || jsonb_build_object('ended', 'deadline')
     WHERE id IN (SELECT id FROM jobs
                   WHERE (workspace_id IS NULL OR workspace_id=ANY(locked_workspaces)) AND state = 'ready' AND deadline_at <= now()
                     AND kind IN ('reap_expired', 'reconcile_ambiguous',
                                  'alert_stranded_sources', 'reap_transit_assets',
                                  'plan_slot', 'refresh_credential', 'reauth_prompt',
                                  'deliver_outbox', 'sync_media_source',
                                  'first_ingest_chunk')   -- the kinds a sweep re-mints
                   ORDER BY id LIMIT rem)
       AND state = 'ready' AND deadline_at <= now()   -- rechecked on a changed row
    RETURNING kind, workspace_id, payload, payload->>'source_id' AS source_id
  ), rearmed AS (
    UPDATE media_sources s
       SET next_sync_at = now() + interval '24 hours'           -- work_loop.REARM_AFTER_SECONDS
      FROM ended e
     WHERE e.kind IN ('sync_media_source', 'first_ingest_chunk')  -- work_loop._SYNC_KINDS
       AND s.id::text = e.source_id   -- as text: a malformed payload must not abort the sweep
       AND s.workspace_id = e.workspace_id
       AND s.state = 'active' AND s.next_sync_at IS NULL
       AND fn_source_job_read_reason(e.workspace_id,e.payload,p_archive_enabled) IS NULL
       AND EXISTS(SELECT 1 FROM fn_source_read_authority(s.workspace_id,s.id,p_archive_enabled) a WHERE a.admitted)
  )
  SELECT count(*) INTO c FROM ended;
  n := n + c;
  RETURN n;
END $$;


ALTER FUNCTION fn_reaper_sweep(int,interval,interval,boolean) OWNER TO svc_maintenance;
REVOKE ALL ON FUNCTION fn_reaper_sweep(int,interval,interval,boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_reaper_sweep(int,interval,interval,boolean) TO svc_worker;
CREATE OR REPLACE FUNCTION fn_reaper_sweep(p_lim int,p_approval_ttl interval,p_approved_ttl interval)
RETURNS int LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT fn_reaper_sweep(p_lim,p_approval_ttl,p_approved_ttl,false)
$$;
REVOKE CREATE ON SCHEMA public FROM svc_claim,svc_clock,svc_maintenance;
-- runner:postcondition SELECT has_function_privilege('svc_worker','fn_claim_job(text,text,interval,int,boolean)','EXECUTE')
-- runner:postcondition SELECT has_function_privilege('svc_worker','fn_clock_tick(int,interval,jsonb,boolean)','EXECUTE')
-- runner:postcondition SELECT has_function_privilege('svc_worker','fn_reaper_sweep(int,interval,interval,boolean)','EXECUTE')
```

```sql
-- Versioned format evidence is independent of age and attribution policy.
-- runner:postcondition SELECT count(*) = 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='instagram_media_index' AND column_name='validation_capture'
ALTER TABLE instagram_media_index ADD COLUMN validation_capture JSONB NULL;
ALTER TABLE instagram_media_index ADD CONSTRAINT ck_archive_validation_capture CHECK (
 validation_capture IS NULL OR ((
 jsonb_typeof(validation_capture)='object'
 AND validation_capture->>'generation_id'=data_generation_id::text
 AND (validation_capture->>'observation_revision')::bigint=observation_revision
 AND validation_capture->>'selected_asset_id'=COALESCE(first_child_id,parent_media_id)
 AND validation_capture->>'state' IN ('supported','unsupported','validation_pending')
 ) IS TRUE));
CREATE FUNCTION trg_archive_validation_capture_reset() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF (NEW.data_generation_id,NEW.observation_revision,NEW.first_child_id)
 IS DISTINCT FROM (OLD.data_generation_id,OLD.observation_revision,OLD.first_child_id) THEN
  NEW.validation_capture := NULL;
  NEW.validation_state := 'pending';
  NEW.validation_generation_id := NULL;
  NEW.validation_observation_revision := NULL;
  NEW.validation_fingerprint := NULL;
  NEW.validated_at := NULL;
  NEW.cache_generation_id := NULL;
  NEW.cache_observation_revision := NULL;
  NEW.cache_policy_revision := NULL;
  NEW.cached_eligible := NULL;
 END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION trg_archive_validation_capture_reset() FROM PUBLIC;
CREATE TRIGGER archive_validation_capture_reset BEFORE UPDATE ON instagram_media_index
FOR EACH ROW EXECUTE FUNCTION trg_archive_validation_capture_reset();
```

```sql
ALTER TABLE category_post_case_mix ALTER COLUMN ratio TYPE NUMERIC;

-- One named tenant job. Source/mix authority remains the existing converter.
ALTER TABLE jobs DROP CONSTRAINT ck_jobs_kind;
ALTER TABLE jobs ADD CONSTRAINT ck_jobs_kind CHECK (kind IN (
 'plan_slot','publish_pipeline','deliver_outbox','sync_media_source',
 'first_ingest_chunk','refresh_credential','offboard_workspace',
 'revoke_workspace_credentials','reauth_prompt','reconcile_ambiguous',
 'reap_expired','reap_transit_assets','retention_sweep','reencrypt_credentials',
 'send_email','alert_stranded_sources','activation_nudge_sweep','convert_source_mix'));

GRANT CREATE ON SCHEMA public TO svc_clock;
CREATE FUNCTION fn_mix_conversion_jobs(p_limit int,p_run_id text,p_run_digest text)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE minted int;
BEGIN
 IF p_run_id IS NULL OR p_run_id !~ '^[a-zA-Z0-9_.-]{1,128}$'
  OR p_run_digest IS NULL OR p_run_digest !~ '^[0-9a-f]{64}$' THEN RETURN 0; END IF;
 -- Discovery is a hint only; the tenant executor rereads after canonical locks.
 -- Terminal failure in this run stays visible rather than being reminted forever.
 WITH pending AS (
  SELECT w.id FROM workspaces w
  WHERE w.source_mix_mode IS NULL AND w.source_mix_policy_version IS NULL
   AND w.source_membership_policy IS NULL AND w.source_mix_revision IS NULL
   AND w.source_mix_conversion_version IS NULL AND w.source_mix_converted_at IS NULL
   AND NOT EXISTS(SELECT 1 FROM jobs j WHERE j.workspace_id=w.id
    AND j.kind='convert_source_mix' AND j.payload->>'run_digest'=p_run_digest)
  ORDER BY w.id LIMIT LEAST(GREATEST(p_limit,0),1)
 )
 INSERT INTO jobs(kind,workspace_id,lane,serialization_key,run_at,max_attempts,deadline_at,payload)
 SELECT 'convert_source_mix',id,'bulk','mix:'||id||':convert',now(),5,now()+interval '6 hours',
  jsonb_build_object('v',1,'run_id',p_run_id,'run_digest',p_run_digest,'protocol_version',2)
 FROM pending;
 GET DIAGNOSTICS minted=ROW_COUNT;
 RETURN minted;
END $$;
ALTER FUNCTION fn_mix_conversion_jobs(int,text,text) OWNER TO svc_clock;
REVOKE ALL ON FUNCTION fn_mix_conversion_jobs(int,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_mix_conversion_jobs(int,text,text) TO svc_worker;
REVOKE CREATE ON SCHEMA public FROM svc_clock;

CREATE OR REPLACE FUNCTION fn_claim_job(p_lane text,p_worker text,p_lease interval,p_ws_lane_cap int,p_archive_enabled boolean)
RETURNS SETOF jobs LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE candidate record; selected jobs; refusal text; source_prefix_applied boolean;
BEGIN
 -- Discovery owns no job lock. Source-read kinds take the canonical prefix before
 -- the LAST-class lease. Other kinds retain the raw unique-index race mechanism.
 -- Publish/effect consumers adopt their own prefix only in phase 3.
 FOR candidate IN SELECT j.id,j.workspace_id,j.kind FROM jobs j
  WHERE j.state='ready' AND j.lane=p_lane AND j.run_at<=now()
  ORDER BY j.run_at,j.id
 LOOP
  BEGIN
   source_prefix_applied:=false;
   IF candidate.workspace_id IS NOT NULL AND candidate.kind IN ('sync_media_source','first_ingest_chunk','convert_source_mix') THEN
    IF NOT pg_try_advisory_xact_lock(hashtextextended('sources:'||candidate.workspace_id,0)) THEN
     RAISE SQLSTATE 'P0002';
    END IF;
    PERFORM fn_lock_source_read_prefix(candidate.workspace_id);
    source_prefix_applied:=true;
   END IF;
   SELECT j.* INTO selected FROM jobs j WHERE j.id=candidate.id AND j.state='ready' AND j.run_at<=now()
    AND NOT EXISTS(SELECT 1 FROM jobs h WHERE h.serialization_key=j.serialization_key AND h.state='leased')
    AND NOT EXISTS(SELECT 1 FROM provider_quarantine q WHERE q.workspace_id=j.workspace_id
     AND q.scope_ref=j.serialization_key AND q.quarantined_until>now())
    AND (j.workspace_id IS NULL OR (SELECT count(*) FROM jobs w WHERE w.state='leased' AND w.lane=j.lane
     AND w.workspace_id=j.workspace_id)<p_ws_lane_cap)
    FOR UPDATE OF j SKIP LOCKED;
   IF NOT FOUND THEN RAISE SQLSTATE 'P0002'; END IF;
   IF selected.kind IN ('sync_media_source','first_ingest_chunk','convert_source_mix') AND
    (NOT source_prefix_applied OR selected.workspace_id IS DISTINCT FROM candidate.workspace_id) THEN
    RAISE SQLSTATE 'P0002'; -- changed identity: release the job row; discover afresh next turn
   END IF;
   refusal:=NULL;
   IF selected.kind IN ('sync_media_source','first_ingest_chunk') THEN
    refusal:=fn_source_job_read_reason(selected.workspace_id,selected.payload,p_archive_enabled);
   END IF;
   IF refusal IS NOT NULL THEN
    UPDATE jobs SET state='failed',payload=payload||jsonb_build_object('source_read_refusal',refusal)
     WHERE id=selected.id AND state='ready';
    CONTINUE;
   END IF;
   RETURN QUERY UPDATE jobs SET state='leased',locked_by=p_worker,lease_token=gen_random_uuid(),
    locked_until=now()+p_lease,attempts=attempts+1 WHERE id=selected.id AND state='ready' RETURNING jobs.*;
   RETURN;
  EXCEPTION WHEN no_data_found THEN NULL;
  END;
 END LOOP;
END $$;
ALTER FUNCTION fn_claim_job(text,text,interval,int,boolean) OWNER TO svc_claim;
REVOKE ALL ON FUNCTION fn_claim_job(text,text,interval,int,boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_claim_job(text,text,interval,int,boolean) TO svc_worker;
CREATE OR REPLACE FUNCTION fn_reaper_sweep(p_lim int, p_approval_ttl interval, p_approved_ttl interval,p_archive_enabled boolean)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE locked_workspaces uuid[]:='{}'; lock_ws uuid; expired_job record; refusal text; n int := 0; c int; rem int := GREATEST(p_lim, 0);
BEGIN
  PERFORM set_config('app.actor_kind', 'reaper', true);
  -- Lock all bounded candidate workspaces before any intent/item/lease rows.
  FOR lock_ws IN SELECT DISTINCT workspace_id FROM (
   (SELECT workspace_id FROM jobs WHERE state='leased' AND locked_until<now() ORDER BY id LIMIT GREATEST(p_lim,0))
   UNION (SELECT workspace_id FROM jobs WHERE state='ready' AND deadline_at<=now()
    AND kind IN ('reap_expired','reconcile_ambiguous','alert_stranded_sources','reap_transit_assets',
     'plan_slot','refresh_credential','reauth_prompt','deliver_outbox','sync_media_source','first_ingest_chunk','convert_source_mix') ORDER BY id LIMIT GREATEST(p_lim,0))
   UNION (SELECT workspace_id FROM post_intents WHERE cancel_requested AND cap_consumed_on IS NULL
    AND state IN ('scheduled','prompt_pending','awaiting_approval','approved') ORDER BY id LIMIT GREATEST(p_lim,0))
   UNION (SELECT workspace_id FROM post_intents WHERE state IN ('scheduled','prompt_pending')
    AND schedule_slot_at<now() AND origin='cadence' ORDER BY id LIMIT GREATEST(p_lim,0))
   UNION (SELECT i.workspace_id FROM post_intents i JOIN workspaces w ON w.id=i.workspace_id
    WHERE i.state='awaiting_approval' AND i.entered_state_at<now()-COALESCE(w.approval_ttl_minutes*interval '1 minute',p_approval_ttl)
    ORDER BY i.id LIMIT GREATEST(p_lim,0))
   UNION (SELECT workspace_id FROM post_locks WHERE expires_at IS NOT NULL AND expires_at<now() ORDER BY id LIMIT GREATEST(p_lim,0))
  ) candidate WHERE workspace_id IS NOT NULL ORDER BY workspace_id LOOP
   PERFORM fn_lock_source_read_prefix(lock_ws);
   locked_workspaces:=array_append(locked_workspaces,lock_ws);
  END LOOP;
  c:=0;
  FOR expired_job IN SELECT id,workspace_id,kind,payload FROM jobs
   WHERE (workspace_id IS NULL OR workspace_id=ANY(locked_workspaces)) AND state='leased' AND locked_until<now() ORDER BY id LIMIT rem
  LOOP
   refusal:=NULL;
   IF expired_job.kind IN ('sync_media_source','first_ingest_chunk') THEN
    refusal:=fn_source_job_read_reason(expired_job.workspace_id,expired_job.payload,p_archive_enabled);
   END IF;
   UPDATE jobs SET state=CASE WHEN refusal IS NULL THEN 'ready' ELSE 'failed' END,
    locked_by=NULL,lease_token=NULL,locked_until=NULL,
    payload=CASE WHEN refusal IS NULL THEN payload ELSE payload||jsonb_build_object('source_read_refusal',refusal) END
   WHERE id=expired_job.id AND state='leased' AND locked_until<now();
   IF FOUND THEN c:=c+1; END IF;
  END LOOP;
  n:=n+c;rem:=rem-c;
  -- 087: a cancel the user asked for ends `cancelled` (#1235), ahead of the
  -- expiry legs; a debited row is left to the paths that refund it first.
  UPDATE post_intents SET state = 'cancelled'
   WHERE id IN (SELECT id FROM post_intents
                 WHERE workspace_id=ANY(locked_workspaces) AND cancel_requested AND cap_consumed_on IS NULL
                   AND state IN ('scheduled','prompt_pending','awaiting_approval','approved')
                 ORDER BY id LIMIT rem)
     AND state IN ('scheduled','prompt_pending','awaiting_approval','approved');  -- rechecked on a changed row
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  -- 089: the slot expiry takes cadence rows only (#1413). A planned row
  -- that is due and unserved ends through the miss door
  -- (fn_planned_misses), which tells the bound chats why; ended here, it
  -- would end in silence.
  UPDATE post_intents SET state = 'expired'
   WHERE id IN (SELECT id FROM post_intents
                 WHERE workspace_id=ANY(locked_workspaces) AND state IN ('scheduled','prompt_pending') AND schedule_slot_at < now()
                   AND origin = 'cadence'
                 ORDER BY id LIMIT rem);
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  UPDATE post_intents i SET state = 'expired'
   WHERE i.id IN (
     SELECT i2.id FROM post_intents i2 JOIN workspaces w ON w.id = i2.workspace_id
      WHERE i2.workspace_id=ANY(locked_workspaces) AND i2.state = 'awaiting_approval'
        AND i2.entered_state_at
            < now() - COALESCE(w.approval_ttl_minutes * interval '1 minute', p_approval_ttl)
      ORDER BY i2.id LIMIT rem);
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  DELETE FROM post_locks
   WHERE id IN (SELECT id FROM post_locks
                 WHERE workspace_id=ANY(locked_workspaces) AND expires_at IS NOT NULL AND expires_at < now() ORDER BY id LIMIT rem);
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  UPDATE workspace_invitations SET state = 'expired'
   WHERE id IN (SELECT id FROM workspace_invitations
                 WHERE state = 'pending' AND expires_at < now() ORDER BY id LIMIT rem);
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  DELETE FROM onboarding_sessions
   WHERE id IN (SELECT id FROM onboarding_sessions WHERE expires_at < now() ORDER BY id LIMIT rem);
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  -- 086: a `ready` job past its deadline ends `failed`, and says so (#1429),
  -- for the kinds a sweep re-mints; a sync kind's source is re-armed, as
  -- `work_loop._rearm_source` does on a spent budget. Last, so a backlog of
  -- expired jobs cannot take the budget of the legs above.
  WITH ended AS (
    UPDATE jobs SET state = 'failed',
                    payload = payload || jsonb_build_object('ended', 'deadline')
     WHERE id IN (SELECT id FROM jobs
                   WHERE (workspace_id IS NULL OR workspace_id=ANY(locked_workspaces)) AND state = 'ready' AND deadline_at <= now()
                     AND kind IN ('reap_expired', 'reconcile_ambiguous',
                                  'alert_stranded_sources', 'reap_transit_assets',
                                  'plan_slot', 'refresh_credential', 'reauth_prompt',
                                  'deliver_outbox', 'sync_media_source',
                                  'first_ingest_chunk','convert_source_mix')   -- the kinds a sweep re-mints
                   ORDER BY id LIMIT rem)
       AND state = 'ready' AND deadline_at <= now()   -- rechecked on a changed row
    RETURNING kind, workspace_id, payload, payload->>'source_id' AS source_id
  ), rearmed AS (
    UPDATE media_sources s
       SET next_sync_at = now() + interval '24 hours'           -- work_loop.REARM_AFTER_SECONDS
      FROM ended e
     WHERE e.kind IN ('sync_media_source', 'first_ingest_chunk')  -- work_loop._SYNC_KINDS
       AND s.id::text = e.source_id   -- as text: a malformed payload must not abort the sweep
       AND s.workspace_id = e.workspace_id
       AND s.state = 'active' AND s.next_sync_at IS NULL
       AND fn_source_job_read_reason(e.workspace_id,e.payload,p_archive_enabled) IS NULL
       AND EXISTS(SELECT 1 FROM fn_source_read_authority(s.workspace_id,s.id,p_archive_enabled) a WHERE a.admitted)
  )
  SELECT count(*) INTO c FROM ended;
  n := n + c;
  RETURN n;
END $$;


ALTER FUNCTION fn_reaper_sweep(int,interval,interval,boolean) OWNER TO svc_maintenance;
REVOKE ALL ON FUNCTION fn_reaper_sweep(int,interval,interval,boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_reaper_sweep(int,interval,interval,boolean) TO svc_worker;
```

```sql

CREATE FUNCTION fn_source_asset_suitability(
  p_workspace_id uuid, p_destination_id uuid, p_resolved_repost_ttl_days integer,
  p_evaluated_at timestamptz, p_archive_enabled boolean,
  p_source_id uuid, p_media_item_id uuid,
  p_existing_intent_id uuid, p_require_validation boolean)
RETURNS TABLE (source_id uuid, media_item_id uuid, provider text,
  suitable boolean, base_suitable boolean, reason text,
  data_generation_id uuid, observation_revision bigint, policy_revision bigint,
  source_use_epoch bigint, effective_minimum_age_days bigint,
  live_intent_conflict boolean, blocking_locks text[], warning_locks text[])
LANGUAGE sql STABLE SECURITY INVOKER
SET search_path = pg_catalog, public, pg_temp AS $$
WITH source_facts AS (
  SELECT s.*, x.ratio, p.schema_version, p.revision AS policy_revision,
    p.minimum_age_days, p.include_credited, p.include_unknown,
    p.eligibility, p.ranking, p.destination_scope,
    g.id AS generation_id, g.lifecycle, c.state AS credential_state,
    c.expires_at AS credential_expires_at, c.provider AS credential_provider,
    a.state AS origin_state, a.provider_account_ref AS origin_ref,
    EXISTS (SELECT 1 FROM ig_accounts d WHERE d.workspace_id=s.workspace_id
      AND d.id=p_destination_id AND (d.state='active' OR (s.provider='gdrive' AND p_require_validation IS FALSE AND d.state='reauth_required'))) AS destination_active,
    EXISTS (SELECT 1 FROM media_source_destinations d WHERE d.workspace_id=s.workspace_id
      AND d.source_id=s.id AND d.ig_account_id=p_destination_id AND d.state='active') AS assigned,
    GREATEST(p.minimum_age_days::bigint,p_resolved_repost_ttl_days::bigint+1) AS effective_age
  FROM media_sources s
  LEFT JOIN category_post_case_mix x ON x.workspace_id=s.workspace_id AND x.source_id=s.id AND x.effective_to IS NULL
  LEFT JOIN instagram_source_policies p ON p.workspace_id=s.workspace_id AND p.source_id=s.id
  LEFT JOIN instagram_archive_generations g ON g.workspace_id=s.workspace_id AND g.source_id=s.id AND g.id=s.current_authorization_generation_id
  LEFT JOIN oauth_credentials c ON c.workspace_id=g.workspace_id AND c.ig_account_id=g.origin_ig_account_id AND c.id=g.credential_id
  LEFT JOIN ig_accounts a ON a.workspace_id=s.workspace_id AND a.id=s.origin_ig_account_id
  WHERE s.workspace_id=p_workspace_id AND (p_source_id IS NULL OR s.id=p_source_id)
), assets AS (
  SELECT s.id AS source_id, m.id AS media_item_id, s.provider, s.source_use_epoch,
    NULL::uuid AS data_generation_id, NULL::bigint AS observation_revision,
    NULL::bigint AS policy_revision, NULL::bigint AS effective_age,
    CASE
      WHEN p_evaluated_at IS NULL OR p_resolved_repost_ttl_days IS NULL THEN 'context_unavailable'
      WHEN s.state='error' OR COALESCE((s.config->>'removed')::boolean,false) THEN 'source_unavailable'
      WHEN s.ratio=0 THEN 'source_off'
      WHEN NOT s.destination_active THEN 'destination_not_allowed'
      WHEN m.state<>'available' THEN 'unavailable'
    END AS base_reason
  FROM source_facts s JOIN media_items m ON m.workspace_id=s.workspace_id AND m.source_id=s.id
  WHERE s.provider='gdrive' AND (p_media_item_id IS NULL OR m.id=p_media_item_id)
  UNION ALL
  SELECT s.id, m.id, s.provider, s.source_use_epoch,
    i.data_generation_id, i.observation_revision, s.policy_revision, s.effective_age,
    CASE
      WHEN p_archive_enabled IS NOT TRUE THEN 'archive_not_activated'
      WHEN p_evaluated_at IS NULL OR p_resolved_repost_ttl_days IS NULL THEN 'context_unavailable'
      WHEN s.state<>'active' OR COALESCE((s.config->>'removed')::boolean,false) THEN 'source_unavailable'
      WHEN COALESCE(s.ratio>0,false) IS NOT TRUE THEN 'source_off'
      WHEN s.origin_state IS DISTINCT FROM 'active' OR s.origin_ref LIKE 'manual:%' THEN 'origin_unavailable'
      WHEN s.generation_id IS NULL OR s.lifecycle<>'active' THEN 'generation_unavailable'
      WHEN s.credential_state IS DISTINCT FROM 'active' OR s.credential_provider IS DISTINCT FROM 'ig_login'
        OR s.credential_expires_at<=p_evaluated_at THEN 'credential_unavailable'
      WHEN s.schema_version IS DISTINCT FROM 1 OR s.eligibility IS DISTINCT FROM 'supported'
        OR s.ranking IS DISTINCT FROM 'none' OR s.destination_scope IS DISTINCT FROM 'origin_only' THEN 'unsupported_source_policy'
      WHEN i.data_generation_id IS DISTINCT FROM s.generation_id THEN 'awaiting_reobservation'
      WHEN i.exclusion_reason='origin_identity_conflict' OR m.source_projection_redacted IS TRUE THEN 'identity_conflict_or_deleted'
      WHEN i.availability='excluded' AND i.exclusion_reason='unsupported_media' THEN 'unsupported'
      WHEN i.availability IS DISTINCT FROM 'available' OR (m.id IS NOT NULL AND m.state<>'available') THEN 'unavailable'
      WHEN i.validation_state='unsupported' OR i.exclusion_reason='unsupported_media' THEN 'unsupported'
      WHEN i.validation_capture->>'generation_id'=s.generation_id::text
        AND i.validation_capture->>'observation_revision'=i.observation_revision::text
        AND i.validation_capture->>'selected_asset_id'=COALESCE(i.first_child_id,i.parent_media_id)
        AND i.validation_capture->>'state'='unsupported' THEN 'unsupported'
      WHEN i.validation_capture->>'generation_id'=s.generation_id::text
        AND i.validation_capture->>'observation_revision'=i.observation_revision::text
        AND i.validation_capture->>'selected_asset_id'=COALESCE(i.first_child_id,i.parent_media_id)
        AND i.validation_capture->>'state'='validation_pending'
        AND i.validation_capture->>'reason' IN ('helper_unavailable','asset_unavailable')
        THEN i.validation_capture->>'reason'
      WHEN m.id IS NULL OR m.source_data_generation_id IS DISTINCT FROM i.data_generation_id THEN 'validation_pending'
      WHEN p_require_validation IS DISTINCT FROM false AND (
        i.validation_state IS DISTINCT FROM 'supported'
        OR i.validation_generation_id IS DISTINCT FROM i.data_generation_id
        OR i.validation_observation_revision IS DISTINCT FROM i.observation_revision
        OR i.validation_fingerprint IS NULL OR i.validated_at IS NULL) THEN 'validation_pending'
      WHEN EXISTS (SELECT 1 FROM instagram_intent_provenance b WHERE b.workspace_id=p_workspace_id
        AND b.intent_id=p_existing_intent_id AND (b.source_id IS DISTINCT FROM s.id
          OR b.media_item_id IS DISTINCT FROM m.id OR b.origin_ig_account_id IS DISTINCT FROM s.origin_ig_account_id
          OR b.selected_asset_identity IS DISTINCT FROM COALESCE(i.first_child_id,i.parent_media_id))) THEN 'selected_asset_changed'
      WHEN p_require_validation IS DISTINCT FROM false AND EXISTS (SELECT 1 FROM instagram_intent_provenance b
        WHERE b.workspace_id=p_workspace_id AND b.intent_id=p_existing_intent_id
        AND (b.capture_generation_id IS DISTINCT FROM i.data_generation_id
          OR b.capture_observation_revision IS DISTINCT FROM i.observation_revision
          OR b.validation_fingerprint IS DISTINCT FROM i.validation_fingerprint)) THEN 'archive_revalidation_required'
      WHEN p_evaluated_at-i.original_at < s.effective_age*interval '1 day' THEN 'too_recent'
      WHEN i.attribution='credited' AND s.include_credited IS NOT TRUE THEN 'credited_excluded'
      WHEN i.attribution='unknown' AND s.include_unknown IS NOT TRUE THEN 'attribution_unknown'
      WHEN NOT s.destination_active OR s.origin_ig_account_id<>p_destination_id OR NOT s.assigned THEN 'destination_not_allowed'
    END
  FROM source_facts s JOIN instagram_media_index i ON i.workspace_id=s.workspace_id AND i.source_id=s.id
  LEFT JOIN media_items m ON m.workspace_id=i.workspace_id AND m.source_id=i.source_id AND m.id=i.media_item_id
  WHERE s.provider='instagram_archive' AND (p_media_item_id IS NULL OR m.id=p_media_item_id)
), context AS (
  SELECT a.*,
    EXISTS (SELECT 1 FROM post_intents p WHERE p.workspace_id=p_workspace_id
      AND p.id=p_existing_intent_id AND p.media_item_id=a.media_item_id
      AND p.ig_account_id=p_destination_id AND p.origin='planned' AND NOT p.cancel_requested
      AND p.state NOT IN ('posted','skipped','rejected','expired','failed','cancelled')) AS planned_warning_override,
    EXISTS (SELECT 1 FROM post_intents p WHERE p.workspace_id=p_workspace_id
      AND p.media_item_id=a.media_item_id AND p.ig_account_id=p_destination_id
      AND p.state NOT IN ('posted','skipped','rejected','expired','failed','cancelled')
      AND (p_existing_intent_id IS NULL OR p.id<>p_existing_intent_id)) AS live_intent_conflict,
    ARRAY(SELECT DISTINCT l.kind FROM post_locks l WHERE l.workspace_id=p_workspace_id
      AND l.media_item_id=a.media_item_id AND (l.ig_account_id IS NULL OR l.ig_account_id=p_destination_id)
      AND (l.expires_at IS NULL OR l.expires_at>p_evaluated_at)
      AND l.kind IN ('reject','unsupported','hold','seasonal') ORDER BY l.kind) AS blocking_locks,
    ARRAY(SELECT DISTINCT l.kind FROM post_locks l WHERE l.workspace_id=p_workspace_id
      AND l.media_item_id=a.media_item_id AND (l.ig_account_id IS NULL OR l.ig_account_id=p_destination_id)
      AND (l.expires_at IS NULL OR l.expires_at>p_evaluated_at)
      AND l.kind IN ('skip','recent') ORDER BY l.kind) AS warning_locks
  FROM assets a
)
SELECT c.source_id,c.media_item_id,c.provider,
  c.base_reason IS NULL AND NOT c.live_intent_conflict AND cardinality(c.blocking_locks)=0 AND (cardinality(c.warning_locks)=0 OR c.planned_warning_override),
  c.base_reason IS NULL,
  COALESCE(c.base_reason,CASE WHEN c.live_intent_conflict THEN 'live_intent'
    WHEN cardinality(c.blocking_locks)>0 OR (cardinality(c.warning_locks)>0 AND NOT c.planned_warning_override) THEN 'locked' END),
  c.data_generation_id,c.observation_revision,c.policy_revision,c.source_use_epoch,c.effective_age,
  c.live_intent_conflict,c.blocking_locks,c.warning_locks
FROM context c
$$;

REVOKE ALL ON FUNCTION fn_source_asset_suitability(uuid,uuid,integer,timestamptz,boolean,uuid,uuid,uuid,boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_source_asset_suitability(uuid,uuid,integer,timestamptz,boolean,uuid,uuid,uuid,boolean) TO svc_ingress,svc_worker;

CREATE OR REPLACE FUNCTION fn_source_asset_suitability(
  p_workspace_id uuid, p_destination_id uuid, p_resolved_repost_ttl_days integer,
  p_evaluated_at timestamptz, p_archive_enabled boolean,
  p_source_id uuid DEFAULT NULL, p_media_item_id uuid DEFAULT NULL,
  p_existing_intent_id uuid DEFAULT NULL)
RETURNS TABLE (source_id uuid, media_item_id uuid, provider text,
  suitable boolean, base_suitable boolean, reason text,
  data_generation_id uuid, observation_revision bigint, policy_revision bigint,
  source_use_epoch bigint, effective_minimum_age_days bigint,
  live_intent_conflict boolean, blocking_locks text[], warning_locks text[])
LANGUAGE sql STABLE SECURITY INVOKER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT * FROM fn_source_asset_suitability(p_workspace_id,p_destination_id,
  p_resolved_repost_ttl_days,p_evaluated_at,p_archive_enabled,p_source_id,
  p_media_item_id,p_existing_intent_id,true)
$$;


-- Read-only selected preparation discovery. Not a policy/admission authority.
-- Matched byte/binding proof needs no preparation work; this is discovery only.
-- Epoch/policy authorization is freshly checked by callback/send admission.
GRANT SELECT (workspace_id,intent_id,source_id,media_item_id,origin_ig_account_id,
 selected_asset_identity,capture_generation_id,capture_observation_revision,
 validation_fingerprint) ON instagram_intent_provenance TO svc_maintenance;
CREATE POLICY p_selected_preparation_read ON instagram_intent_provenance
 FOR SELECT TO svc_maintenance USING (true);
GRANT SELECT (workspace_id,source_id,media_item_id,data_generation_id,
 observation_revision,first_child_id,parent_media_id,validation_fingerprint,
 validation_state,validation_generation_id,validation_observation_revision,
 validated_at) ON instagram_media_index TO svc_maintenance;
CREATE POLICY p_selected_preparation_read ON instagram_media_index
 FOR SELECT TO svc_maintenance USING (true);
CREATE FUNCTION fn_selected_preparation_due(p_limit integer)
RETURNS TABLE(intent_id uuid,workspace_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT i.id,i.workspace_id FROM post_intents i JOIN workspaces w ON w.id=i.workspace_id
 JOIN media_items m ON m.workspace_id=i.workspace_id AND m.id=i.media_item_id
 JOIN media_sources s ON s.workspace_id=m.workspace_id AND s.id=m.source_id
 WHERE s.provider='instagram_archive'
 AND w.state='active' AND NOT w.is_paused AND NOT i.cancel_requested
 AND NOT EXISTS (
 SELECT 1 FROM instagram_intent_provenance p
 JOIN instagram_media_index x ON x.workspace_id=p.workspace_id
   AND x.source_id=p.source_id AND x.media_item_id=p.media_item_id
 JOIN instagram_archive_generations g ON g.workspace_id=s.workspace_id
   AND g.source_id=s.id AND g.id=s.current_authorization_generation_id
 WHERE p.workspace_id=i.workspace_id AND p.intent_id=i.id
 AND p.source_id=s.id AND p.media_item_id=m.id
 AND p.origin_ig_account_id=s.origin_ig_account_id
 AND g.lifecycle='active' AND x.data_generation_id=g.id
 AND m.source_data_generation_id=x.data_generation_id
 AND p.selected_asset_identity=COALESCE(x.first_child_id,x.parent_media_id)
 AND p.capture_generation_id=x.data_generation_id
 AND p.capture_observation_revision=x.observation_revision
 AND p.validation_fingerprint=x.validation_fingerprint
 AND x.validation_state='supported'
 AND x.validation_generation_id=x.data_generation_id
 AND x.validation_observation_revision=x.observation_revision
 AND x.validation_fingerprint IS NOT NULL AND x.validated_at IS NOT NULL)
 AND ((i.state IN ('scheduled','prompt_pending') AND i.schedule_slot_at<=now())
 OR (i.state='awaiting_approval' AND EXISTS(SELECT 1 FROM channel_outbox o
 WHERE o.workspace_id=i.workspace_id AND o.intent_id=i.id AND o.kind='approval_prompt'
 AND o.state IN ('pending','ambiguous'))))
 ORDER BY i.schedule_slot_at,i.id LIMIT p_limit
$$;
GRANT CREATE ON SCHEMA public TO svc_maintenance;
ALTER FUNCTION fn_selected_preparation_due(integer) OWNER TO svc_maintenance;
REVOKE ALL ON FUNCTION fn_selected_preparation_due(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_selected_preparation_due(integer) TO svc_worker;

REVOKE CREATE ON SCHEMA public FROM svc_maintenance;

-- Pending approval deferrals must use wall time, even in older transactions.
CREATE FUNCTION trg_touch_outbox_updated_at() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 NEW.updated_at := clock_timestamp();
 RETURN NEW;
END;
$$;
DROP TRIGGER tg_touch_channel_outbox ON channel_outbox;
CREATE TRIGGER tg_touch_channel_outbox BEFORE UPDATE ON channel_outbox
FOR EACH ROW EXECUTE FUNCTION trg_touch_outbox_updated_at();
```

```sql
-- Existing pre-extension rows retain unknown admitted references/evidence.
-- runner:postcondition SELECT to_regclass('public.uq_archive_admitted_owner') IS NOT NULL
-- runner:postcondition SELECT to_regclass('public.uq_archive_unsettled_intent') IS NOT NULL
-- runner:postcondition SELECT count(*) = 3 FROM information_schema.columns WHERE table_schema='public' AND table_name='instagram_artifacts' AND column_name IN ('admitted_asset_ref','admitted_resource_type','creation_evidence')
ALTER TABLE instagram_artifacts ADD COLUMN admitted_asset_ref TEXT NULL;
ALTER TABLE instagram_artifacts ADD COLUMN admitted_resource_type TEXT NULL;
ALTER TABLE instagram_artifacts ADD COLUMN creation_evidence JSONB NULL;
ALTER TABLE instagram_artifacts ADD CONSTRAINT ck_archive_admitted_reference CHECK ((admitted_asset_ref IS NULL AND admitted_resource_type IS NULL AND creation_evidence IS NULL) OR (kind = 'transit' AND admitted_asset_ref ~ ('^ws/' || workspace_id::text || '/[0-9a-f]{20,64}$') AND admitted_resource_type IN ('image','video')) IS TRUE);
ALTER TABLE instagram_artifacts ADD CONSTRAINT ck_archive_creation_evidence CHECK (creation_evidence IS NULL OR (jsonb_typeof(creation_evidence) = 'object' AND octet_length(creation_evidence::text) <= 2048 AND creation_evidence ?& ARRAY['public_id','asset_id','version','resource_type','delivery_type','signature'] AND creation_evidence - ARRAY['public_id','asset_id','version','resource_type','delivery_type','signature'] = '{}'::jsonb AND creation_evidence->>'public_id' = admitted_asset_ref AND creation_evidence->>'resource_type' = admitted_resource_type AND creation_evidence->>'delivery_type' = 'authenticated' AND jsonb_typeof(creation_evidence->'version') = 'number' AND creation_evidence->>'version' ~ '^[1-9][0-9]{0,18}$' AND (creation_evidence->>'version')::numeric <= 9223372036854775807 AND jsonb_typeof(creation_evidence->'asset_id') = 'string' AND jsonb_typeof(creation_evidence->'signature') = 'string' AND creation_evidence->>'asset_id' ~ '^[a-zA-Z0-9_-]{1,128}$' AND creation_evidence->>'signature' ~ '^[0-9a-f]{40}$') IS TRUE);
ALTER TABLE instagram_artifacts ADD CONSTRAINT ck_archive_managed_completion CHECK (admitted_asset_ref IS NULL OR ((outcome IN ('unsettled','uncertain') AND created_asset_ref IS NULL AND creation_evidence IS NULL AND cleanup_state = 'pending') OR (outcome = 'settled' AND created_asset_ref = admitted_asset_ref AND creation_evidence IS NOT NULL AND cleanup_state IN ('pending','destroyed'))) IS TRUE);
CREATE UNIQUE INDEX uq_archive_unsettled_intent ON instagram_artifacts (workspace_id,intent_id)
 WHERE kind='transit' AND outcome IN ('unsettled','uncertain');
CREATE UNIQUE INDEX uq_archive_admitted_owner ON instagram_artifacts (workspace_id,admitted_asset_ref,admitted_resource_type)
 WHERE admitted_asset_ref IS NOT NULL;
CREATE OR REPLACE FUNCTION trg_archive_artifact_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.workspace_id, NEW.artifact_ref, NEW.source_id, NEW.data_generation_id, NEW.observation_revision, NEW.media_item_id, NEW.intent_id, NEW.kind, NEW.capture_epoch, NEW.policy_revision, NEW.selected_asset_identity, NEW.validation_fingerprint, NEW.upload_attempt_id, NEW.admitted_at, NEW.admitted_asset_ref, NEW.admitted_resource_type)
     IS DISTINCT FROM (OLD.workspace_id, OLD.artifact_ref, OLD.source_id, OLD.data_generation_id, OLD.observation_revision, OLD.media_item_id, OLD.intent_id, OLD.kind, OLD.capture_epoch, OLD.policy_revision, OLD.selected_asset_identity, OLD.validation_fingerprint, OLD.upload_attempt_id, OLD.admitted_at, OLD.admitted_asset_ref, OLD.admitted_resource_type) THEN
    RAISE EXCEPTION 'archive artifact capture is immutable' USING ERRCODE = '23514';
  END IF;
  IF OLD.creation_evidence IS NOT NULL AND (NEW.creation_evidence,NEW.created_asset_ref,NEW.outcome,NEW.settled_at)
    IS DISTINCT FROM (OLD.creation_evidence,OLD.created_asset_ref,OLD.outcome,OLD.settled_at) THEN
    RAISE EXCEPTION 'archive creation evidence is immutable' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END; $$;
```

```sql
-- Bound early archive cards can lose selected content before approval.
-- runner:postcondition SELECT count(*) = 3 FROM post_intent_transitions WHERE from_state IN ('scheduled','prompt_pending','awaiting_approval') AND to_state='review_required'
INSERT INTO post_intent_transitions (from_state,to_state) VALUES
 ('scheduled','review_required'),
 ('prompt_pending','review_required'),
 ('awaiting_approval','review_required')
ON CONFLICT DO NOTHING;
```

```sql
-- The history choice an Instagram connect carries in its one-shot state, and the applied result its callback records (#1644).
-- runner:postcondition SELECT count(*) = 4 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'oauth_states' AND column_name IN ('archive_entry','archive_use_history','archive_expected_mix_revision','archive_preview_version')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relname = 'oauth_connection_results' AND c.relkind = 'r' AND c.relrowsecurity)
-- runner:postcondition SELECT count(*) >= 1 FROM pg_policies WHERE schemaname = 'public' AND tablename = 'oauth_connection_results'
ALTER TABLE oauth_states ADD COLUMN archive_entry TEXT NULL;
ALTER TABLE oauth_states ADD COLUMN archive_use_history BOOLEAN NULL;
ALTER TABLE oauth_states ADD COLUMN archive_expected_mix_revision BIGINT NULL;
ALTER TABLE oauth_states ADD COLUMN archive_preview_version INTEGER NULL;
ALTER TABLE oauth_states ADD CONSTRAINT ck_oauth_state_archive_values CHECK (archive_entry IN ('add','first_connect','reconnect') AND archive_expected_mix_revision >= 0 AND archive_preview_version = 1);
-- A choice is all three facts or none, a reconnect carries none, and the entry names the start: Add pins no destination, a row start pins one.
ALTER TABLE oauth_states ADD CONSTRAINT ck_oauth_state_archive_choice CHECK ((archive_use_history IS NULL) = (archive_expected_mix_revision IS NULL) AND (archive_use_history IS NULL) = (archive_preview_version IS NULL) AND (archive_use_history IS NULL OR archive_entry IN ('add','first_connect')) IS TRUE AND (archive_entry IS NULL OR (provider = 'ig_login' AND (archive_entry = 'add') = (reconnect_target IS NULL))));

CREATE TABLE oauth_connection_results (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  session_id UUID NOT NULL REFERENCES session_tokens(id) ON DELETE CASCADE,
  ig_account_id UUID NOT NULL,
  source_id UUID NULL,
  outcome TEXT NOT NULL,
  history_enabled BOOLEAN NOT NULL,
  mix_revision BIGINT NOT NULL,
  mix_reset_applied BOOLEAN NOT NULL,
  next_action TEXT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ NOT NULL,
  CONSTRAINT ck_connection_result_outcome CHECK (outcome IN ('connected_history_on','connected_history_off','connected_history_off_mix_conflict','reconnected_preserved')),
  CONSTRAINT ck_connection_result_next_action CHECK (next_action IS NOT DISTINCT FROM CASE WHEN outcome = 'connected_history_off_mix_conflict' THEN 'review_mix' END),
  CONSTRAINT ck_connection_result_revision CHECK (mix_revision >= 0),
  CONSTRAINT fk_connection_result_account FOREIGN KEY (workspace_id, ig_account_id) REFERENCES ig_accounts(workspace_id, id) ON DELETE CASCADE,
  CONSTRAINT fk_connection_result_source FOREIGN KEY (workspace_id, source_id, ig_account_id) REFERENCES media_sources(workspace_id, id, origin_ig_account_id) ON DELETE CASCADE
);

-- The API's alone: the callback's tenant unit of work writes a result once, and the initiating member session reads it.
ALTER TABLE oauth_connection_results ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_ingress_connection_results ON oauth_connection_results FOR ALL TO svc_ingress
  USING (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT ON oauth_connection_results TO svc_ingress;
```

```sql
-- 142: verified Meta identity mappings: the (app, subject) a Meta callback names, bound to the credential and authorization it was verified for (#1644).
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relname = 'meta_identity_mappings' AND c.relkind = 'r' AND c.relrowsecurity)
-- runner:postcondition SELECT count(*) >= 3 FROM pg_policies WHERE schemaname = 'public' AND tablename = 'meta_identity_mappings'
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_roles r ON r.oid = p.proowner WHERE n.nspname = 'public' AND p.proname = 'fn_meta_subject_mappings' AND p.prosecdef AND r.rolname = 'svc_maintenance' AND 'search_path=pg_catalog, public, pg_temp' = ANY (p.proconfig))
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace, aclexplode(p.proacl) a JOIN pg_roles g ON g.oid = a.grantee WHERE n.nspname = 'public' AND p.proname = 'fn_meta_subject_mappings' AND a.privilege_type = 'EXECUTE' AND g.rolname = 'svc_ingress')
CREATE TABLE meta_identity_mappings (
  workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  ig_account_id UUID NOT NULL,
  credential_id UUID NOT NULL,
  authorization_id UUID NOT NULL,
  app_identity TEXT NOT NULL,
  subject_ref TEXT NOT NULL,
  account_ref TEXT NOT NULL,
  method TEXT NOT NULL,
  authorized_at TIMESTAMPTZ NULL,
  verified_at TIMESTAMPTZ NOT NULL,
  is_current BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, credential_id, authorization_id),
  CONSTRAINT ck_meta_mapping_app CHECK (app_identity ~ '^instagram:[0-9]{1,32}$'),
  CONSTRAINT ck_meta_mapping_refs CHECK (subject_ref ~ '^[0-9]{1,64}$' AND account_ref ~ '^[0-9]{1,64}$'),
  CONSTRAINT ck_meta_mapping_method CHECK ((method = 'oauth_exchange' AND authorized_at IS NOT NULL) OR (method = 'reconciled_read' AND authorized_at IS NULL)),
  CONSTRAINT fk_meta_mapping_credential FOREIGN KEY (workspace_id, ig_account_id, credential_id) REFERENCES oauth_credentials(workspace_id, ig_account_id, id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX uq_meta_mapping_current ON meta_identity_mappings (workspace_id, credential_id) WHERE is_current;
CREATE INDEX ix_meta_mapping_subject ON meta_identity_mappings (app_identity, subject_ref);

-- A mapping is evidence: only the current flag moves, and only from true to false.
CREATE FUNCTION trg_meta_mapping_immutable() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  IF (NEW.workspace_id, NEW.ig_account_id, NEW.credential_id, NEW.authorization_id, NEW.app_identity, NEW.subject_ref, NEW.account_ref, NEW.method, NEW.authorized_at, NEW.verified_at, NEW.created_at)
     IS DISTINCT FROM (OLD.workspace_id, OLD.ig_account_id, OLD.credential_id, OLD.authorization_id, OLD.app_identity, OLD.subject_ref, OLD.account_ref, OLD.method, OLD.authorized_at, OLD.verified_at, OLD.created_at)
     OR (NEW.is_current AND NOT OLD.is_current) THEN
    RAISE EXCEPTION 'meta identity mapping is immutable' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END; $$;
REVOKE ALL ON FUNCTION trg_meta_mapping_immutable() FROM PUBLIC;
CREATE TRIGGER tg_meta_mapping_immutable BEFORE UPDATE ON meta_identity_mappings FOR EACH ROW EXECUTE FUNCTION trg_meta_mapping_immutable();

-- The runtime logins write their own tenant's rows (no DELETE: a credential's deletion cascades);
-- the deletion and reconciliation doors read across tenants.
ALTER TABLE meta_identity_mappings ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_tenant ON meta_identity_mappings FOR ALL TO svc_ingress, svc_worker
  USING (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE (is_current) ON meta_identity_mappings TO svc_ingress, svc_worker;
GRANT SELECT ON meta_identity_mappings TO svc_maintenance, svc_clock;
CREATE POLICY p_maint_meta_mappings ON meta_identity_mappings FOR SELECT TO svc_maintenance USING (true);
CREATE POLICY p_clock_meta_mappings ON meta_identity_mappings FOR SELECT TO svc_clock USING (true);

GRANT CREATE ON SCHEMA public TO svc_maintenance;
CREATE FUNCTION fn_meta_subject_mappings(p_app_identity text, p_subject text)
RETURNS TABLE (o_workspace_id uuid, o_ig_account_id uuid, o_credential_id uuid, o_authorization_id uuid,
  o_authorized_at timestamptz, o_verified_at timestamptz, o_is_current boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT workspace_id, ig_account_id, credential_id, authorization_id, authorized_at, verified_at, is_current
    FROM meta_identity_mappings
   WHERE app_identity = p_app_identity AND subject_ref = p_subject
   ORDER BY workspace_id, verified_at, authorization_id
$$;
COMMENT ON FUNCTION fn_meta_subject_mappings(text, text) IS
  'The verified mappings a Meta deauthorize callback''s (app identity, subject) names, across every workspace: the callback carries no tenant, so p_tenant would hide every row from the API''s login. An equality on the indexed pair, ordered by workspace and verification time. SECURITY DEFINER owned by svc_maintenance; EXECUTE for svc_ingress (142, #1644).';
ALTER FUNCTION fn_meta_subject_mappings(text, text) OWNER TO svc_maintenance;
REVOKE ALL ON FUNCTION fn_meta_subject_mappings(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_meta_subject_mappings(text, text) TO svc_ingress;
REVOKE CREATE ON SCHEMA public FROM svc_maintenance;
```

```sql
-- 143: durable Meta deletion receipts: tenant-free episodes, the generations each scope froze, and status capabilities (#1644).
-- runner:postcondition SELECT count(*) = 4 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relname IN ('meta_deletion_receipts','meta_deletion_receipt_scopes','meta_deletion_receipt_generations','meta_deletion_receipt_capabilities') AND c.relkind = 'r' AND c.relrowsecurity
-- runner:postcondition SELECT count(*) = 4 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_roles r ON r.oid = p.proowner WHERE n.nspname = 'public' AND p.proname IN ('fn_meta_deletion_receive','fn_meta_deletion_status','fn_meta_receipt_scope','fn_meta_receipt_progress') AND p.prosecdef AND r.rolname = 'svc_maintenance' AND 'search_path=pg_catalog, public, pg_temp' = ANY (p.proconfig)
-- runner:postcondition SELECT count(*) = 4 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace, aclexplode(p.proacl) a JOIN pg_roles g ON g.oid = a.grantee WHERE n.nspname = 'public' AND a.privilege_type = 'EXECUTE' AND ((p.proname IN ('fn_meta_deletion_receive','fn_meta_deletion_status') AND g.rolname = 'svc_ingress') OR (p.proname IN ('fn_meta_receipt_scope','fn_meta_receipt_progress') AND g.rolname = 'svc_worker'))
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace, aclexplode(a.attacl) x JOIN pg_roles r ON r.oid = x.grantee WHERE n.nspname = 'public' AND c.relname = 'instagram_archive_generations' AND a.attname = 'authorization_id' AND r.rolname = 'svc_maintenance' AND x.privilege_type = 'SELECT')
-- No foreign key to workspaces: a receipt outlives the workspace it scoped, so offboarding never erases the proof.
CREATE TABLE meta_deletion_receipts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  app_identity TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL,
  fingerprint_key_version INTEGER NOT NULL,
  issued_at TIMESTAMPTZ NULL,
  received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  state TEXT NOT NULL DEFAULT 'pending',
  review_reason TEXT NULL,
  completed_at TIMESTAMPTZ NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_meta_receipt_fingerprint UNIQUE (app_identity, request_fingerprint),
  CONSTRAINT ck_meta_receipt_app CHECK (app_identity ~ '^(instagram:[0-9]{1,32}|instagram:unconfigured|facebook:legacy)$'),
  CONSTRAINT ck_meta_receipt_fingerprint CHECK (request_fingerprint ~ '^[0-9a-f]{64}$' AND fingerprint_key_version > 0),
  CONSTRAINT ck_meta_receipt_state CHECK (state IN ('pending','needs_review','completed')),
  CONSTRAINT ck_meta_receipt_review_reason CHECK (review_reason IN ('ordering_ambiguous','mapping_incomplete','app_unrecognized','transit_unresolved')),
  CONSTRAINT ck_meta_receipt_review CHECK ((state = 'needs_review') = (review_reason IS NOT NULL)),
  CONSTRAINT ck_meta_receipt_completed CHECK ((state = 'completed') = (completed_at IS NOT NULL))
);

CREATE TABLE meta_deletion_receipt_scopes (
  receipt_id UUID NOT NULL REFERENCES meta_deletion_receipts(id) ON DELETE RESTRICT,
  workspace_id UUID NOT NULL,
  ig_account_id UUID NOT NULL,
  credential_id UUID NOT NULL,
  authorization_id UUID NOT NULL,
  disposition TEXT NOT NULL,
  state TEXT NOT NULL,
  results JSONB NOT NULL DEFAULT '{}'::jsonb,
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (receipt_id, workspace_id, credential_id, authorization_id),
  CONSTRAINT ck_meta_scope_disposition CHECK (disposition IN ('in_scope','held')),
  CONSTRAINT ck_meta_scope_state CHECK (state IN ('pending','cleaning','held','completed','workspace_purged')),
  CONSTRAINT ck_meta_scope_held CHECK (state = 'workspace_purged' OR (disposition = 'held') = (state = 'held')),
  CONSTRAINT ck_meta_scope_results CHECK (jsonb_typeof(results) = 'object' AND octet_length(results::text) <= 4096)
);
CREATE INDEX ix_meta_scope_due ON meta_deletion_receipt_scopes (next_attempt_at) WHERE state IN ('pending','cleaning');

CREATE TABLE meta_deletion_receipt_generations (
  receipt_id UUID NOT NULL,
  workspace_id UUID NOT NULL,
  credential_id UUID NOT NULL,
  authorization_id UUID NOT NULL,
  source_id UUID NOT NULL,
  generation_id UUID NOT NULL,
  PRIMARY KEY (receipt_id, workspace_id, generation_id),
  CONSTRAINT fk_meta_receipt_generation_scope FOREIGN KEY (receipt_id, workspace_id, credential_id, authorization_id) REFERENCES meta_deletion_receipt_scopes(receipt_id, workspace_id, credential_id, authorization_id) ON DELETE RESTRICT
);

CREATE TABLE meta_deletion_receipt_capabilities (
  capability_hash TEXT PRIMARY KEY,
  receipt_id UUID NOT NULL REFERENCES meta_deletion_receipts(id) ON DELETE RESTRICT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_meta_capability_hash CHECK (capability_hash ~ '^[0-9a-f]{64}$')
);
CREATE INDEX ix_meta_capability_receipt ON meta_deletion_receipt_capabilities (receipt_id);

CREATE TRIGGER tg_touch_meta_deletion_receipts BEFORE UPDATE ON meta_deletion_receipts FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();
CREATE TRIGGER tg_touch_meta_deletion_receipt_scopes BEFORE UPDATE ON meta_deletion_receipt_scopes FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

-- The doors' alone: the runtime logins hold no grant on any of the four.
ALTER TABLE meta_deletion_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE meta_deletion_receipt_scopes ENABLE ROW LEVEL SECURITY;
ALTER TABLE meta_deletion_receipt_generations ENABLE ROW LEVEL SECURITY;
ALTER TABLE meta_deletion_receipt_capabilities ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_maint_meta_receipts ON meta_deletion_receipts FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);
CREATE POLICY p_maint_meta_scopes ON meta_deletion_receipt_scopes FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);
CREATE POLICY p_maint_meta_generations ON meta_deletion_receipt_generations FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);
CREATE POLICY p_maint_meta_capabilities ON meta_deletion_receipt_capabilities FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);
GRANT SELECT, INSERT, UPDATE ON meta_deletion_receipts, meta_deletion_receipt_scopes TO svc_maintenance;
GRANT SELECT, INSERT ON meta_deletion_receipt_generations, meta_deletion_receipt_capabilities TO svc_maintenance;
-- The clock's cleanup sweep (145) purges scopes, completes receipts and reads the frozen generations.
CREATE POLICY p_clock_meta_receipts_s ON meta_deletion_receipts FOR SELECT TO svc_clock USING (true);
CREATE POLICY p_clock_meta_receipts_u ON meta_deletion_receipts FOR UPDATE TO svc_clock USING (true) WITH CHECK (true);
CREATE POLICY p_clock_meta_scopes_s ON meta_deletion_receipt_scopes FOR SELECT TO svc_clock USING (true);
CREATE POLICY p_clock_meta_scopes_u ON meta_deletion_receipt_scopes FOR UPDATE TO svc_clock USING (true) WITH CHECK (true);
CREATE POLICY p_clock_meta_generations ON meta_deletion_receipt_generations FOR SELECT TO svc_clock USING (true);
GRANT SELECT, UPDATE (state, completed_at, updated_at) ON meta_deletion_receipts TO svc_clock;
GRANT SELECT, UPDATE (state, next_attempt_at, updated_at) ON meta_deletion_receipt_scopes TO svc_clock;
GRANT SELECT ON meta_deletion_receipt_generations TO svc_clock;
-- 135 granted the other columns and the row policy; the receive door freezes by authorization.
GRANT SELECT (authorization_id) ON instagram_archive_generations TO svc_maintenance;

GRANT CREATE ON SCHEMA public TO svc_maintenance;
-- p_fingerprints[1] is the request's fingerprint under the current key (p_key_version); later
-- elements are older key versions, matched for replays only.
CREATE FUNCTION fn_meta_deletion_receive(p_app_identity text, p_fingerprints text[], p_key_version int,
  p_subject text, p_issued_at timestamptz, p_capability_hash text, p_skew_seconds int)
RETURNS TABLE (o_receipt_id uuid, o_replay boolean, o_state text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  v_receipt uuid;
  v_state text;
  v_reason text;
  v_issued timestamptz;
  v_skew interval;
  v_ws uuid;
  v_disposition text;
  m record;
BEGIN
  IF p_app_identity IS NULL OR p_app_identity !~ '^(instagram:[0-9]{1,32}|instagram:unconfigured|facebook:legacy)$'
     OR p_fingerprints IS NULL OR array_ndims(p_fingerprints) IS DISTINCT FROM 1
     OR array_lower(p_fingerprints, 1) <> 1 OR cardinality(p_fingerprints) > 16
     OR array_position(p_fingerprints, NULL) IS NOT NULL
     OR EXISTS (SELECT 1 FROM unnest(p_fingerprints) f WHERE f !~ '^[0-9a-f]{64}$')
     OR p_key_version IS NULL OR p_key_version < 1
     OR p_subject IS NULL OR p_subject !~ '^[0-9]{1,64}$'
     OR p_capability_hash IS NULL OR p_capability_hash !~ '^[0-9a-f]{64}$'
     OR p_skew_seconds IS NULL OR p_skew_seconds NOT BETWEEN 0 AND 3600 THEN
    RAISE EXCEPTION 'meta receipt refused: invalid input' USING ERRCODE = 'P0001';
  END IF;
  v_skew := make_interval(secs => p_skew_seconds);
  -- Order evidence from beyond the skew is no evidence: the door owns the clock.
  v_issued := CASE WHEN p_issued_at <= now() + v_skew THEN p_issued_at END;
  PERFORM pg_advisory_xact_lock(hashtextextended('meta_receipt:' || p_app_identity || ':' || p_fingerprints[1], 0));
  SELECT r.id, r.state INTO v_receipt, v_state FROM meta_deletion_receipts r
   WHERE r.app_identity = p_app_identity AND r.request_fingerprint = ANY (p_fingerprints)
   ORDER BY r.received_at, r.id LIMIT 1;
  IF FOUND THEN
    INSERT INTO meta_deletion_receipt_capabilities (capability_hash, receipt_id) VALUES (p_capability_hash, v_receipt);
    RETURN QUERY SELECT v_receipt, true, v_state;
    RETURN;
  END IF;
  INSERT INTO meta_deletion_receipts (app_identity, request_fingerprint, fingerprint_key_version, issued_at)
  VALUES (p_app_identity, p_fingerprints[1], p_key_version, v_issued)
  RETURNING id INTO v_receipt;
  FOR v_ws IN SELECT DISTINCT workspace_id FROM meta_identity_mappings
               WHERE app_identity = p_app_identity AND subject_ref = p_subject ORDER BY workspace_id LOOP
    PERFORM pg_advisory_xact_lock(hashtextextended('sources:' || v_ws, 0));
    FOR m IN SELECT * FROM meta_identity_mappings
              WHERE workspace_id = v_ws AND app_identity = p_app_identity AND subject_ref = p_subject
              ORDER BY ig_account_id, verified_at, authorization_id LOOP
      IF v_issued IS NULL THEN
        -- No order evidence: in scope only while this is the account's one grant for the subject and its
        -- one episode. With a second grant (a reauthorization) or an earlier receipt, nothing says which a
        -- request reached, and a boundary never widens to a newer grant: held, so the receipt needs review.
        v_disposition := CASE WHEN (
            SELECT count(*) FROM meta_identity_mappings o
             WHERE o.workspace_id = v_ws AND o.ig_account_id = m.ig_account_id
               AND o.app_identity = p_app_identity AND o.subject_ref = p_subject) > 1
          OR EXISTS (
            SELECT 1 FROM meta_deletion_receipt_scopes ps
              JOIN meta_identity_mappings pm ON pm.workspace_id = ps.workspace_id
               AND pm.credential_id = ps.credential_id AND pm.authorization_id = ps.authorization_id
             WHERE ps.workspace_id = v_ws AND ps.ig_account_id = m.ig_account_id
               AND ps.receipt_id <> v_receipt
               AND pm.app_identity = p_app_identity AND pm.subject_ref = p_subject)
          THEN 'held' ELSE 'in_scope' END;
      ELSIF COALESCE(m.authorized_at, m.verified_at) <= v_issued - v_skew THEN
        v_disposition := 'in_scope';
      ELSIF m.authorized_at >= v_issued + v_skew THEN
        v_disposition := 'excluded';
      ELSE
        v_disposition := 'held';
      END IF;
      CONTINUE WHEN v_disposition = 'excluded';
      INSERT INTO meta_deletion_receipt_scopes (receipt_id, workspace_id, ig_account_id, credential_id,
        authorization_id, disposition, state)
      VALUES (v_receipt, v_ws, m.ig_account_id, m.credential_id, m.authorization_id, v_disposition,
        CASE WHEN v_disposition = 'held' THEN 'held' ELSE 'pending' END);
      -- Frozen now: a grant made later never joins this scope.
      INSERT INTO meta_deletion_receipt_generations (receipt_id, workspace_id, credential_id,
        authorization_id, source_id, generation_id)
      SELECT v_receipt, v_ws, m.credential_id, m.authorization_id, g.source_id, g.id
        FROM instagram_archive_generations g
        JOIN media_sources s ON s.workspace_id = g.workspace_id AND s.id = g.source_id
       WHERE g.workspace_id = v_ws AND g.authorization_id = m.authorization_id
         AND s.provider = 'instagram_archive' AND s.origin_ig_account_id = m.ig_account_id
         AND g.lifecycle <> 'deleted';
    END LOOP;
  END LOOP;
  v_reason := CASE
    WHEN p_app_identity !~ '^instagram:[0-9]{1,32}$' THEN 'app_unrecognized'
    WHEN EXISTS (SELECT 1 FROM meta_deletion_receipt_scopes
                  WHERE receipt_id = v_receipt AND disposition = 'held') THEN 'ordering_ambiguous'
  END;
  v_state := CASE
    WHEN v_reason IS NOT NULL THEN 'needs_review'
    WHEN NOT EXISTS (SELECT 1 FROM meta_deletion_receipt_scopes WHERE receipt_id = v_receipt) THEN 'completed'
    ELSE 'pending'
  END;
  UPDATE meta_deletion_receipts
     SET state = v_state, review_reason = v_reason,
         completed_at = CASE WHEN v_state = 'completed' THEN now() END
   WHERE id = v_receipt;
  INSERT INTO meta_deletion_receipt_capabilities (capability_hash, receipt_id) VALUES (p_capability_hash, v_receipt);
  RETURN QUERY SELECT v_receipt, false, v_state;
END $$;
COMMENT ON FUNCTION fn_meta_deletion_receive(text, text[], integer, text, timestamptz, text, integer) IS
  'A Meta data-deletion request, tenant-free. A fingerprint any key version matches is a replay: the same receipt and a new capability, no new cleanup. Otherwise a new receipt: per mapped workspace, ascending, under the sources advisory lock, each mapping of (app identity, subject) is classified by its authorization against issued_at with the skew (in_scope, held, or excluded and never frozen), and in_scope and held scopes freeze the non-deleted generations of that authorization on that origin''s source. Without a credible issued_at a mapping is in_scope only when it is the account''s one mapping for the subject and no earlier receipt scoped the account, and held otherwise, so a newer grant is never reached for want of order evidence. Review reasons: app_unrecognized, ordering_ambiguous. Stores no raw payload, subject or secret. SECURITY DEFINER owned by svc_maintenance; EXECUTE for svc_ingress (143, #1644).';
ALTER FUNCTION fn_meta_deletion_receive(text, text[], integer, text, timestamptz, text, integer) OWNER TO svc_maintenance;
REVOKE ALL ON FUNCTION fn_meta_deletion_receive(text, text[], integer, text, timestamptz, text, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_meta_deletion_receive(text, text[], integer, text, timestamptz, text, integer) TO svc_ingress;

CREATE FUNCTION fn_meta_deletion_status(p_capability_hash text)
RETURNS TABLE (o_state text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT CASE WHEN r.state = 'completed' THEN 'completed' ELSE 'in_progress' END
    FROM meta_deletion_receipt_capabilities c
    JOIN meta_deletion_receipts r ON r.id = c.receipt_id
   WHERE c.capability_hash = p_capability_hash
$$;
COMMENT ON FUNCTION fn_meta_deletion_status(text) IS
  'The public status of the receipt a capability names: completed or in_progress, nothing else; an unknown capability returns no row. SECURITY DEFINER owned by svc_maintenance; EXECUTE for svc_ingress (143, #1644).';
ALTER FUNCTION fn_meta_deletion_status(text) OWNER TO svc_maintenance;
REVOKE ALL ON FUNCTION fn_meta_deletion_status(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_meta_deletion_status(text) TO svc_ingress;

CREATE FUNCTION fn_meta_receipt_scope(p_receipt uuid)
RETURNS TABLE (o_ig_account_id uuid, o_credential_id uuid, o_authorization_id uuid, o_disposition text,
  o_state text, o_results jsonb, o_source_id uuid, o_generation_ids uuid[])
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT s.ig_account_id, s.credential_id, s.authorization_id, s.disposition, s.state, s.results, g.source_id,
         COALESCE(array_agg(g.generation_id ORDER BY g.generation_id) FILTER (WHERE g.generation_id IS NOT NULL), '{}')
    FROM meta_deletion_receipt_scopes s
    LEFT JOIN meta_deletion_receipt_generations g ON g.receipt_id = s.receipt_id AND g.workspace_id = s.workspace_id
     AND g.credential_id = s.credential_id AND g.authorization_id = s.authorization_id
   WHERE s.receipt_id = p_receipt
     AND s.workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
   GROUP BY s.receipt_id, s.workspace_id, s.credential_id, s.authorization_id, g.source_id
   ORDER BY s.ig_account_id, s.authorization_id, g.source_id
$$;
COMMENT ON FUNCTION fn_meta_receipt_scope(uuid) IS
  'The claimed tenant''s scopes of one receipt, each with its source and the generation ids it froze; no tenant reads nothing. SECURITY DEFINER owned by svc_maintenance; EXECUTE for svc_worker (143, #1644).';
ALTER FUNCTION fn_meta_receipt_scope(uuid) OWNER TO svc_maintenance;
REVOKE ALL ON FUNCTION fn_meta_receipt_scope(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_meta_receipt_scope(uuid) TO svc_worker;

CREATE FUNCTION fn_meta_receipt_progress(p_receipt uuid, p_credential uuid, p_authorization uuid,
  p_state text, p_results jsonb, p_review_reason text, p_retry_after_seconds int)
RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  v_ws uuid := NULLIF(current_setting('app.tenant_id', true), '')::uuid;
  v_from text;
  v_state text;
  v_reason text;
BEGIN
  IF p_state IS NULL OR p_state NOT IN ('cleaning','completed')
     OR p_results IS NULL OR jsonb_typeof(p_results) <> 'object' OR octet_length(p_results::text) > 4096
     OR (p_review_reason IS NOT NULL AND (p_review_reason <> 'transit_unresolved' OR p_state = 'completed')) THEN
    RAISE EXCEPTION 'meta receipt progress refused: invalid input' USING ERRCODE = 'P0001';
  END IF;
  SELECT s.state INTO v_from FROM meta_deletion_receipt_scopes s
   WHERE s.receipt_id = p_receipt AND s.workspace_id = v_ws
     AND s.credential_id = p_credential AND s.authorization_id = p_authorization
   FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'meta receipt progress refused: scope_unavailable' USING ERRCODE = 'P0001';
  END IF;
  IF v_from NOT IN ('pending','cleaning') THEN
    RAISE EXCEPTION 'meta receipt progress refused: scope_%', v_from USING ERRCODE = 'P0001';
  END IF;
  IF p_state = 'completed' THEN
    IF EXISTS (SELECT 1 FROM meta_deletion_receipt_generations x
                 LEFT JOIN instagram_archive_generations g ON g.workspace_id = x.workspace_id
                  AND g.source_id = x.source_id AND g.id = x.generation_id
                WHERE x.receipt_id = p_receipt AND x.workspace_id = v_ws
                  AND x.credential_id = p_credential AND x.authorization_id = p_authorization
                  AND g.lifecycle IS DISTINCT FROM 'deleted') THEN
      RAISE EXCEPTION 'meta receipt progress refused: generations_not_deleted' USING ERRCODE = 'P0001';
    END IF;
    IF EXISTS (SELECT 1 FROM instagram_artifacts a
                WHERE a.workspace_id = v_ws AND a.kind = 'transit'
                  AND a.data_generation_id IN (SELECT x.generation_id FROM meta_deletion_receipt_generations x
                        WHERE x.receipt_id = p_receipt AND x.workspace_id = v_ws
                          AND x.credential_id = p_credential AND x.authorization_id = p_authorization)
                  AND NOT (a.outcome = 'settled' AND a.cleanup_state = 'destroyed')) THEN
      RAISE EXCEPTION 'meta receipt progress refused: transit_unresolved' USING ERRCODE = 'P0001';
    END IF;
  END IF;
  UPDATE meta_deletion_receipt_scopes
     SET state = p_state, results = p_results,
         next_attempt_at = now() + make_interval(secs => LEAST(GREATEST(COALESCE(p_retry_after_seconds, 60), 60), 3600))
   WHERE receipt_id = p_receipt AND workspace_id = v_ws
     AND credential_id = p_credential AND authorization_id = p_authorization;
  SELECT r.state, r.review_reason INTO v_state, v_reason FROM meta_deletion_receipts r WHERE r.id = p_receipt FOR UPDATE;
  IF p_review_reason IS NOT NULL AND v_state <> 'needs_review' THEN
    UPDATE meta_deletion_receipts SET state = 'needs_review', review_reason = p_review_reason WHERE id = p_receipt;
    v_state := 'needs_review';
  ELSIF (v_state = 'pending' OR (v_state = 'needs_review' AND v_reason = 'transit_unresolved'))
        AND NOT EXISTS (SELECT 1 FROM meta_deletion_receipt_scopes s
         WHERE s.receipt_id = p_receipt AND s.state NOT IN ('completed','workspace_purged')) THEN
    UPDATE meta_deletion_receipts SET state = 'completed', review_reason = NULL, completed_at = now() WHERE id = p_receipt;
    v_state := 'completed';
  END IF;
  RETURN v_state;
END $$;
COMMENT ON FUNCTION fn_meta_receipt_progress(uuid, uuid, uuid, text, jsonb, text, integer) IS
  'Reports one of the claimed tenant''s scopes: pending to cleaning or completed, cleaning to cleaning or completed; a held, completed or purged scope refuses by name. Completion is structural and refuses by name (generations_not_deleted, transit_unresolved) unless every generation the scope froze is deleted and no transit upload of them is unresolved. Due again after the retry, clamped to 60..3600 seconds. A transit_unresolved reason moves a pending receipt to needs_review; the receipt completes once every scope is completed or purged, from pending or from needs_review for transit_unresolved alone, and never from any other review reason. Returns the receipt state. SECURITY DEFINER owned by svc_maintenance; EXECUTE for svc_worker (143, #1644).';
ALTER FUNCTION fn_meta_receipt_progress(uuid, uuid, uuid, text, jsonb, text, integer) OWNER TO svc_maintenance;
REVOKE ALL ON FUNCTION fn_meta_receipt_progress(uuid, uuid, uuid, text, jsonb, text, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_meta_receipt_progress(uuid, uuid, uuid, text, jsonb, text, integer) TO svc_worker;
REVOKE CREATE ON SCHEMA public FROM svc_maintenance;
```

```sql
-- 144: deletion cleanup and offboarding obligations: the review hold, verified transit destruction, the reviewed redaction door and the finalize transit guard (#1644).
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relname = 'instagram_archive_generations' AND k.conname = 'ck_archive_generation_lifecycle' AND pg_get_constraintdef(k.oid) LIKE '%review_hold%')
-- runner:postcondition SELECT count(*) = 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'instagram_artifacts' AND column_name = 'destroyed_at'
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_roles r ON r.oid = p.proowner, aclexplode(p.proacl) a JOIN pg_roles g ON g.oid = a.grantee WHERE n.nspname = 'public' AND p.proname = 'fn_archive_redact_generation_records' AND p.prosecdef AND r.rolname = 'svc_maintenance' AND 'search_path=pg_catalog, public, pg_temp' = ANY (p.proconfig) AND a.privilege_type = 'EXECUTE' AND g.rolname = 'svc_worker')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.proname = 'fn_offboard_finalize' AND position('transit cleanup obligations' IN p.prosrc) > 0)
-- A held generation is unavailable but not destroyed: admission and suitability already refuse every lifecycle but active.
ALTER TABLE instagram_archive_generations DROP CONSTRAINT ck_archive_generation_lifecycle;
ALTER TABLE instagram_archive_generations ADD CONSTRAINT ck_archive_generation_lifecycle CHECK (lifecycle IN ('active','superseded','review_hold','deletion_pending','deleted'));
-- Destruction is recorded only once a lookup proved the admitted asset absent.
ALTER TABLE instagram_artifacts ADD COLUMN destroyed_at TIMESTAMPTZ NULL;
ALTER TABLE instagram_artifacts ADD CONSTRAINT ck_archive_artifact_destroyed CHECK (admitted_asset_ref IS NULL OR ((cleanup_state = 'destroyed') = (destroyed_at IS NOT NULL)));

-- Provenance and artifact captures are immutable and the runtime logins cannot delete them;
-- the redaction door alone does, once nothing can still use them.
GRANT SELECT, DELETE ON instagram_operation_provenance, instagram_intent_provenance, instagram_artifacts TO svc_maintenance;
CREATE POLICY p_maint_redact_read ON instagram_operation_provenance FOR SELECT TO svc_maintenance USING (true);
CREATE POLICY p_maint_redact_delete ON instagram_operation_provenance FOR DELETE TO svc_maintenance USING (true);
CREATE POLICY p_maint_redact_delete ON instagram_intent_provenance FOR DELETE TO svc_maintenance USING (true);
CREATE POLICY p_maint_redact_read ON instagram_artifacts FOR SELECT TO svc_maintenance USING (true);
CREATE POLICY p_maint_redact_delete ON instagram_artifacts FOR DELETE TO svc_maintenance USING (true);

GRANT CREATE ON SCHEMA public TO svc_maintenance;
CREATE FUNCTION fn_archive_redact_generation_records(p_source uuid, p_generation_ids uuid[])
RETURNS TABLE (o_operation_rows int, o_intent_rows int, o_artifact_rows int)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  v_ws uuid := NULLIF(current_setting('app.tenant_id', true), '')::uuid;
  v_operations int;
  v_intents int;
  v_artifacts int;
BEGIN
  IF v_ws IS NULL OR p_source IS NULL OR p_generation_ids IS NULL OR cardinality(p_generation_ids) = 0
     OR array_position(p_generation_ids, NULL) IS NOT NULL
     OR (SELECT count(*) FROM instagram_archive_generations g
          WHERE g.workspace_id = v_ws AND g.source_id = p_source AND g.id = ANY (p_generation_ids)
            AND g.lifecycle IN ('deletion_pending','deleted'))
        <> (SELECT count(DISTINCT x) FROM unnest(p_generation_ids) x) THEN
    RAISE EXCEPTION 'archive redaction refused: scope_unavailable' USING ERRCODE = 'P0001';
  END IF;
  IF EXISTS (SELECT 1 FROM instagram_operation_provenance p
               JOIN provider_operations o ON o.workspace_id = p.workspace_id AND o.id = p.operation_id
              WHERE p.workspace_id = v_ws AND p.data_generation_id = ANY (p_generation_ids)
                AND o.state IN ('permitted','ambiguous')) THEN
    RAISE EXCEPTION 'archive redaction refused: operation_unresolved' USING ERRCODE = 'P0001';
  END IF;
  IF EXISTS (SELECT 1 FROM instagram_artifacts a
              WHERE a.workspace_id = v_ws AND a.data_generation_id = ANY (p_generation_ids) AND a.kind = 'transit'
                AND NOT (a.outcome = 'settled' AND a.cleanup_state = 'destroyed')) THEN
    RAISE EXCEPTION 'archive redaction refused: transit_unresolved' USING ERRCODE = 'P0001';
  END IF;
  IF EXISTS (SELECT 1 FROM instagram_intent_provenance p
               JOIN post_intents i ON i.workspace_id = p.workspace_id AND i.id = p.intent_id
              WHERE p.workspace_id = v_ws
                AND (p.capture_generation_id = ANY (p_generation_ids) OR p.staged_generation_id = ANY (p_generation_ids))
                AND i.state NOT IN ('posted','skipped','rejected','expired','failed','cancelled')) THEN
    RAISE EXCEPTION 'archive redaction refused: intent_live' USING ERRCODE = 'P0001';
  END IF;
  -- Dependency order: both provenance kinds reference the artifacts. A selection-only link stays.
  DELETE FROM instagram_operation_provenance
   WHERE workspace_id = v_ws AND data_generation_id = ANY (p_generation_ids);
  GET DIAGNOSTICS v_operations = ROW_COUNT;
  DELETE FROM instagram_intent_provenance
   WHERE workspace_id = v_ws
     AND (capture_generation_id = ANY (p_generation_ids) OR staged_generation_id = ANY (p_generation_ids));
  GET DIAGNOSTICS v_intents = ROW_COUNT;
  DELETE FROM instagram_artifacts
   WHERE workspace_id = v_ws AND data_generation_id = ANY (p_generation_ids);
  GET DIAGNOSTICS v_artifacts = ROW_COUNT;
  RETURN QUERY SELECT v_operations, v_intents, v_artifacts;
END $$;
COMMENT ON FUNCTION fn_archive_redact_generation_records(uuid, uuid[]) IS
  'Deletes the claimed tenant''s immutable records of fenced generations: operation provenance, then intent provenance captured or staged in the set, then artifacts. Refuses by name: scope_unavailable (no tenant, an empty set, or a generation not of this source or not deletion_pending/deleted), operation_unresolved (a permitted or ambiguous operation), transit_unresolved (a transit attempt not settled and destroyed), intent_live (a non-terminal intent captured or staged in the set). SECURITY DEFINER owned by svc_maintenance; EXECUTE for svc_worker (144, #1644).';
ALTER FUNCTION fn_archive_redact_generation_records(uuid, uuid[]) OWNER TO svc_maintenance;
REVOKE ALL ON FUNCTION fn_archive_redact_generation_records(uuid, uuid[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_archive_redact_generation_records(uuid, uuid[]) TO svc_worker;

-- The workspace's deletion must not orphan an upload nobody can destroy any more.
CREATE OR REPLACE FUNCTION fn_offboard_finalize(p_ws uuid, p_grace interval)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
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
  IF EXISTS (SELECT 1 FROM instagram_artifacts WHERE workspace_id = p_ws AND kind = 'transit'
              AND (outcome IN ('unsettled','uncertain') OR cleanup_state = 'pending')) THEN
    RAISE EXCEPTION 'workspace % still holds archive transit cleanup obligations', p_ws;
  END IF;
  DELETE FROM workspaces WHERE id = p_ws;
END $$;
REVOKE CREATE ON SCHEMA public FROM svc_maintenance;
```

```sql
-- 145: the archive lifecycle sweep, deletion cleanup jobs and existing-account reconciliation jobs (#1644).
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relname = 'jobs' AND k.conname = 'ck_jobs_kind' AND pg_get_constraintdef(k.oid) LIKE '%reconcile_archive_accounts%')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relname = 'jobs' AND k.conname = 'ck_jobs_system_kinds' AND pg_get_constraintdef(k.oid) LIKE '%archive_lifecycle_sweep%')
-- runner:postcondition SELECT count(*) = 2 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_roles r ON r.oid = p.proowner, aclexplode(p.proacl) a JOIN pg_roles g ON g.oid = a.grantee WHERE n.nspname = 'public' AND p.proname IN ('fn_meta_receipt_cleanup_jobs','fn_archive_reconcile_jobs') AND p.prosecdef AND r.rolname = 'svc_clock' AND 'search_path=pg_catalog, public, pg_temp' = ANY (p.proconfig) AND a.privilege_type = 'EXECUTE' AND g.rolname = 'svc_worker'
ALTER TABLE jobs DROP CONSTRAINT ck_jobs_kind;
ALTER TABLE jobs ADD CONSTRAINT ck_jobs_kind CHECK (kind IN (
 'plan_slot','publish_pipeline','deliver_outbox','sync_media_source',
 'first_ingest_chunk','refresh_credential','offboard_workspace',
 'revoke_workspace_credentials','reauth_prompt','reconcile_ambiguous',
 'reap_expired','reap_transit_assets','retention_sweep','reencrypt_credentials',
 'send_email','alert_stranded_sources','activation_nudge_sweep','convert_source_mix',
 'archive_lifecycle_sweep','meta_deletion_cleanup','reconcile_archive_accounts'));
ALTER TABLE jobs DROP CONSTRAINT ck_jobs_system_kinds;
ALTER TABLE jobs ADD CONSTRAINT ck_jobs_system_kinds CHECK (
  (workspace_id IS NULL) = (kind IN
    ('reconcile_ambiguous','reap_expired','reap_transit_assets','retention_sweep',
     'reencrypt_credentials','send_email','alert_stranded_sources',
     'activation_nudge_sweep','archive_lifecycle_sweep')));

GRANT CREATE ON SCHEMA public TO svc_clock;
CREATE FUNCTION fn_meta_receipt_cleanup_jobs(p_limit int)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE minted int;
BEGIN
 -- A purged workspace's cascade took every row a scope froze: nothing is left to clean.
 UPDATE meta_deletion_receipt_scopes s SET state = 'workspace_purged'
  WHERE s.state IN ('pending','cleaning','held')
    AND NOT EXISTS (SELECT 1 FROM workspaces w WHERE w.id = s.workspace_id);
 UPDATE meta_deletion_receipts r SET state = 'completed', completed_at = now()
  WHERE r.state = 'pending' AND r.review_reason IS NULL
    AND NOT EXISTS (SELECT 1 FROM meta_deletion_receipt_scopes s
                     WHERE s.receipt_id = r.id AND s.state NOT IN ('completed','workspace_purged'));
 -- Due work is a pending or cleaning scope, or a held scope whose frozen generations are not yet fenced.
 WITH due AS (
  SELECT s.receipt_id, s.workspace_id FROM meta_deletion_receipt_scopes s
   WHERE s.next_attempt_at <= now()
     AND (s.state IN ('pending','cleaning')
      OR (s.state = 'held' AND EXISTS (SELECT 1 FROM meta_deletion_receipt_generations x
           JOIN instagram_archive_generations g ON g.workspace_id = x.workspace_id
            AND g.source_id = x.source_id AND g.id = x.generation_id
          WHERE x.receipt_id = s.receipt_id AND x.workspace_id = s.workspace_id
            AND x.credential_id = s.credential_id AND x.authorization_id = s.authorization_id
            AND g.lifecycle IN ('active','superseded'))))
     AND EXISTS (SELECT 1 FROM workspaces w WHERE w.id = s.workspace_id)
     AND NOT EXISTS (SELECT 1 FROM jobs j
                      WHERE j.serialization_key = 'receipt:' || s.receipt_id || ':' || s.workspace_id
                        AND j.state IN ('ready','leased'))
   GROUP BY s.receipt_id, s.workspace_id
   ORDER BY min(s.next_attempt_at), s.receipt_id, s.workspace_id
   LIMIT LEAST(GREATEST(p_limit, 0), 25)
 )
 INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, deadline_at, payload)
 SELECT 'meta_deletion_cleanup', workspace_id, 'bulk', 'receipt:' || receipt_id || ':' || workspace_id, now(), 5,
  now() + interval '6 hours', jsonb_build_object('v', 1, 'receipt_id', receipt_id)
 FROM due;
 GET DIAGNOSTICS minted = ROW_COUNT;
 RETURN minted;
END $$;
COMMENT ON FUNCTION fn_meta_receipt_cleanup_jobs(integer) IS
  'The lifecycle sweep''s deletion leg: marks scopes of purged workspaces workspace_purged, completes pending receipts with nothing left, and mints at most LEAST(p_limit, 25) meta_deletion_cleanup jobs, one per (receipt, workspace) with due work and no ready or leased job on its key. SECURITY DEFINER owned by svc_clock; EXECUTE for svc_worker (145, #1644).';
ALTER FUNCTION fn_meta_receipt_cleanup_jobs(integer) OWNER TO svc_clock;
REVOKE ALL ON FUNCTION fn_meta_receipt_cleanup_jobs(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_meta_receipt_cleanup_jobs(integer) TO svc_worker;

CREATE FUNCTION fn_archive_reconcile_jobs(p_limit int, p_run_id text, p_run_digest text)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE minted int;
BEGIN
 IF p_run_id IS NULL OR p_run_id !~ '^[a-zA-Z0-9_.-]{1,128}$'
  OR p_run_digest IS NULL OR p_run_digest !~ '^[0-9a-f]{64}$' THEN RETURN 0; END IF;
 -- Discovery is a hint only; the tenant executor rereads under the canonical locks.
 -- One job per workspace per activation digest: a repeated deploy resets nothing.
 WITH pending AS (
  SELECT w.id FROM workspaces w
  WHERE w.state = 'active'
   AND w.source_mix_converted_at IS NOT NULL AND w.source_mix_revision IS NOT NULL
   AND w.source_mix_mode IN ('equal','custom') AND w.source_membership_policy = 'reset_equal'
   AND EXISTS (SELECT 1 FROM ig_accounts a
     JOIN oauth_credentials c ON c.workspace_id = a.workspace_id AND c.ig_account_id = a.id
    WHERE a.workspace_id = w.id AND a.state = 'active' AND a.provider_account_ref NOT LIKE 'manual:%'
     AND c.provider = 'ig_login' AND c.state = 'active'
     AND (NOT EXISTS (SELECT 1 FROM meta_identity_mappings m
           WHERE m.workspace_id = c.workspace_id AND m.credential_id = c.id AND m.is_current)
      OR NOT EXISTS (SELECT 1 FROM media_sources s
           WHERE s.workspace_id = a.workspace_id AND s.provider = 'instagram_archive'
            AND s.origin_ig_account_id = a.id)))
   AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.workspace_id = w.id
    AND j.kind = 'reconcile_archive_accounts' AND j.payload->>'run_digest' = p_run_digest)
  ORDER BY w.id LIMIT LEAST(GREATEST(p_limit, 0), 5)
 )
 INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, deadline_at, payload)
 SELECT 'reconcile_archive_accounts', id, 'bulk', 'archive-reconcile:' || id, now(), 5, now() + interval '6 hours',
  jsonb_build_object('v', 1, 'run_id', p_run_id, 'run_digest', p_run_digest)
 FROM pending;
 GET DIAGNOSTICS minted = ROW_COUNT;
 RETURN minted;
END $$;
COMMENT ON FUNCTION fn_archive_reconcile_jobs(integer, text, text) IS
  'The lifecycle sweep''s reconciliation leg, called only with activation evidence: at most LEAST(p_limit, 5) reconcile_archive_accounts jobs for active, converted workspaces holding an active non-manual account whose active ig_login credential lacks a current mapping or whose account lacks an archive source, never twice for one run digest. SECURITY DEFINER owned by svc_clock; EXECUTE for svc_worker (145, #1644).';
ALTER FUNCTION fn_archive_reconcile_jobs(integer, text, text) OWNER TO svc_clock;
REVOKE ALL ON FUNCTION fn_archive_reconcile_jobs(integer, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_archive_reconcile_jobs(integer, text, text) TO svc_worker;
REVOKE CREATE ON SCHEMA public FROM svc_clock;
```

```sql
ALTER TABLE instagram_source_policies ADD COLUMN carousel_slides TEXT NOT NULL DEFAULT 'all';
ALTER TABLE instagram_source_policies ADD CONSTRAINT ck_archive_policy_carousel_slides CHECK (carousel_slides IN ('all','first'));

ALTER TABLE instagram_media_index ADD COLUMN slide_count INTEGER NULL;
ALTER TABLE instagram_media_index ADD COLUMN slides_complete BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE instagram_media_index ADD COLUMN post_ref UUID NOT NULL DEFAULT gen_random_uuid();
ALTER TABLE instagram_media_index ADD CONSTRAINT ck_archive_slide_count CHECK (slide_count IS NULL OR slide_count BETWEEN 0 AND 20);
ALTER TABLE instagram_media_index ADD CONSTRAINT uq_archive_index_bundle UNIQUE (workspace_id,source_id,parent_media_id,data_generation_id,observation_revision);

CREATE TABLE instagram_media_assets (
 workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
 source_id UUID NOT NULL,
 parent_media_id TEXT NOT NULL,
 asset_media_id TEXT NOT NULL,
 data_generation_id UUID NOT NULL,
 observation_revision BIGINT NOT NULL,
 position INTEGER NOT NULL,
 media_kind TEXT NOT NULL,
 availability TEXT NOT NULL DEFAULT 'available',
 exclusion_reason TEXT NULL,
 media_item_id UUID NULL,
 validation_state TEXT NOT NULL DEFAULT 'pending',
 validation_generation_id UUID NULL,
 validation_observation_revision BIGINT NULL,
 validation_fingerprint TEXT NULL,
 validated_at TIMESTAMPTZ NULL,
 validation_capture JSONB NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY (workspace_id,source_id,asset_media_id),
 CONSTRAINT uq_archive_asset_position UNIQUE (workspace_id,source_id,parent_media_id,position) DEFERRABLE INITIALLY DEFERRED,
 CONSTRAINT uq_archive_asset_item UNIQUE (workspace_id,source_id,media_item_id),
 CONSTRAINT ck_archive_asset_revision CHECK (observation_revision > 0 AND position > 0),
 CONSTRAINT ck_archive_asset_kind CHECK (media_kind IN ('image','video') OR (media_kind='unknown' AND availability='excluded' AND exclusion_reason='unsupported_media' AND media_item_id IS NULL)),
 CONSTRAINT ck_archive_asset_availability CHECK (availability IN ('available','unavailable','excluded')),
 CONSTRAINT ck_archive_asset_validation CHECK (validation_state IN ('pending','supported','unsupported')),
 CONSTRAINT ck_archive_asset_validation_evidence CHECK ((validation_state = 'pending' AND validation_generation_id IS NULL AND validation_observation_revision IS NULL AND validation_fingerprint IS NULL AND validated_at IS NULL) OR (validation_state <> 'pending' AND validation_generation_id IS NOT NULL AND validation_observation_revision IS NOT NULL AND validation_generation_id = data_generation_id AND validation_observation_revision = observation_revision AND validation_fingerprint IS NOT NULL AND validated_at IS NOT NULL)),
 CONSTRAINT ck_archive_asset_validation_capture CHECK (validation_capture IS NULL OR ((jsonb_typeof(validation_capture)='object' AND validation_capture->>'generation_id'=data_generation_id::text AND (validation_capture->>'observation_revision')::bigint=observation_revision AND validation_capture->>'selected_asset_id'=asset_media_id AND validation_capture->>'state' IN ('supported','unsupported','validation_pending')) IS TRUE)),
 CONSTRAINT fk_archive_asset_bundle FOREIGN KEY (workspace_id,source_id,parent_media_id,data_generation_id,observation_revision) REFERENCES instagram_media_index(workspace_id,source_id,parent_media_id,data_generation_id,observation_revision) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED,
 CONSTRAINT fk_archive_asset_generation FOREIGN KEY (workspace_id,source_id,data_generation_id) REFERENCES instagram_archive_generations(workspace_id,source_id,id) ON DELETE RESTRICT,
 CONSTRAINT fk_archive_asset_item FOREIGN KEY (workspace_id,source_id,data_generation_id,media_item_id) REFERENCES media_items(workspace_id,source_id,source_data_generation_id,id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX ix_archive_asset_bundle ON instagram_media_assets (workspace_id,source_id,parent_media_id,position);
CREATE INDEX ix_archive_asset_suitability ON instagram_media_assets (workspace_id,source_id,data_generation_id,media_item_id) WHERE availability='available' AND validation_state='supported';

CREATE FUNCTION trg_instagram_media_assets_validation_reset() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF (NEW.data_generation_id,NEW.observation_revision,NEW.asset_media_id)
 IS DISTINCT FROM (OLD.data_generation_id,OLD.observation_revision,OLD.asset_media_id) THEN
  NEW.validation_capture := NULL;
  NEW.validation_state := 'pending';
  NEW.validation_generation_id := NULL;
  NEW.validation_observation_revision := NULL;
  NEW.validation_fingerprint := NULL;
  NEW.validated_at := NULL;
 END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION trg_instagram_media_assets_validation_reset() FROM PUBLIC;
CREATE TRIGGER tg_instagram_media_assets_validation_reset BEFORE UPDATE ON instagram_media_assets
 FOR EACH ROW EXECUTE FUNCTION trg_instagram_media_assets_validation_reset();
CREATE TRIGGER tg_touch_instagram_media_assets BEFORE UPDATE ON instagram_media_assets
 FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

ALTER TABLE instagram_media_assets ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_tenant ON instagram_media_assets FOR ALL TO svc_ingress,svc_worker
 USING (workspace_id=NULLIF(current_setting('app.tenant_id',true),'')::uuid)
 WITH CHECK (workspace_id=NULLIF(current_setting('app.tenant_id',true),'')::uuid);
GRANT SELECT,INSERT,UPDATE,DELETE ON instagram_media_assets TO svc_ingress,svc_worker;
GRANT SELECT (workspace_id,source_id,media_item_id,asset_media_id,data_generation_id,
 observation_revision,validation_fingerprint,validation_state,
 validation_generation_id,validation_observation_revision,validated_at)
 ON instagram_media_assets TO svc_maintenance;
CREATE POLICY p_selected_preparation_read ON instagram_media_assets
 FOR SELECT TO svc_maintenance USING (true);

CREATE OR REPLACE FUNCTION fn_selected_preparation_due(p_limit integer)
RETURNS TABLE(intent_id uuid,workspace_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT i.id,i.workspace_id FROM post_intents i JOIN workspaces w ON w.id=i.workspace_id
 JOIN media_items m ON m.workspace_id=i.workspace_id AND m.id=i.media_item_id
 JOIN media_sources s ON s.workspace_id=m.workspace_id AND s.id=m.source_id
 WHERE s.provider='instagram_archive'
 AND w.state='active' AND NOT w.is_paused AND NOT i.cancel_requested
 AND NOT EXISTS (
 SELECT 1 FROM instagram_intent_provenance p
 JOIN instagram_media_assets a ON a.workspace_id=p.workspace_id
   AND a.source_id=p.source_id AND a.media_item_id=p.media_item_id
 JOIN instagram_archive_generations g ON g.workspace_id=s.workspace_id
   AND g.source_id=s.id AND g.id=s.current_authorization_generation_id
 WHERE p.workspace_id=i.workspace_id AND p.intent_id=i.id
 AND p.source_id=s.id AND p.media_item_id=m.id
 AND p.origin_ig_account_id=s.origin_ig_account_id
 AND g.lifecycle='active' AND a.data_generation_id=g.id
 AND m.source_data_generation_id=a.data_generation_id
 AND p.selected_asset_identity=a.asset_media_id
 AND p.capture_generation_id=a.data_generation_id
 AND p.capture_observation_revision=a.observation_revision
 AND p.validation_fingerprint=a.validation_fingerprint
 AND a.validation_state='supported'
 AND a.validation_generation_id=a.data_generation_id
 AND a.validation_observation_revision=a.observation_revision
 AND a.validation_fingerprint IS NOT NULL AND a.validated_at IS NOT NULL)
 AND ((i.state IN ('scheduled','prompt_pending') AND i.schedule_slot_at<=now())
 OR (i.state='awaiting_approval' AND EXISTS(SELECT 1 FROM channel_outbox o
 WHERE o.workspace_id=i.workspace_id AND o.intent_id=i.id AND o.kind='approval_prompt'
 AND o.state IN ('pending','ambiguous'))))
 ORDER BY i.schedule_slot_at,i.id LIMIT p_limit
$$;
GRANT CREATE ON SCHEMA public TO svc_maintenance;
ALTER FUNCTION fn_selected_preparation_due(integer) OWNER TO svc_maintenance;
REVOKE CREATE ON SCHEMA public FROM svc_maintenance;
REVOKE ALL ON FUNCTION fn_selected_preparation_due(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_selected_preparation_due(integer) TO svc_worker;
```

```sql
CREATE OR REPLACE FUNCTION fn_source_asset_suitability(
  p_workspace_id uuid, p_destination_id uuid, p_resolved_repost_ttl_days integer,
  p_evaluated_at timestamptz, p_archive_enabled boolean,
  p_source_id uuid, p_media_item_id uuid,
  p_existing_intent_id uuid, p_require_validation boolean)
RETURNS TABLE (source_id uuid, media_item_id uuid, provider text,
  suitable boolean, base_suitable boolean, reason text,
  data_generation_id uuid, observation_revision bigint, policy_revision bigint,
  source_use_epoch bigint, effective_minimum_age_days bigint,
  live_intent_conflict boolean, blocking_locks text[], warning_locks text[])
LANGUAGE sql STABLE SECURITY INVOKER
SET search_path = pg_catalog, public, pg_temp AS $$
WITH source_facts AS (
  SELECT s.*, x.ratio, p.schema_version, p.revision AS policy_revision,
    p.minimum_age_days, p.include_credited, p.include_unknown,
    p.eligibility, p.ranking, p.destination_scope, p.carousel_slides,
    g.id AS generation_id, g.lifecycle, c.state AS credential_state,
    c.expires_at AS credential_expires_at, c.provider AS credential_provider,
    a.state AS origin_state, a.provider_account_ref AS origin_ref,
    EXISTS (SELECT 1 FROM ig_accounts d WHERE d.workspace_id=s.workspace_id
      AND d.id=p_destination_id AND (d.state='active' OR (s.provider='gdrive' AND p_require_validation IS FALSE AND d.state='reauth_required'))) AS destination_active,
    EXISTS (SELECT 1 FROM media_source_destinations d WHERE d.workspace_id=s.workspace_id
      AND d.source_id=s.id AND d.ig_account_id=p_destination_id AND d.state='active') AS assigned,
    GREATEST(p.minimum_age_days::bigint,p_resolved_repost_ttl_days::bigint+1) AS effective_age
  FROM media_sources s
  LEFT JOIN category_post_case_mix x ON x.workspace_id=s.workspace_id AND x.source_id=s.id AND x.effective_to IS NULL
  LEFT JOIN instagram_source_policies p ON p.workspace_id=s.workspace_id AND p.source_id=s.id
  LEFT JOIN instagram_archive_generations g ON g.workspace_id=s.workspace_id AND g.source_id=s.id AND g.id=s.current_authorization_generation_id
  LEFT JOIN oauth_credentials c ON c.workspace_id=g.workspace_id AND c.ig_account_id=g.origin_ig_account_id AND c.id=g.credential_id
  LEFT JOIN ig_accounts a ON a.workspace_id=s.workspace_id AND a.id=s.origin_ig_account_id
  WHERE s.workspace_id=p_workspace_id AND (p_source_id IS NULL OR s.id=p_source_id)
), assets AS (
  SELECT s.id AS source_id, m.id AS media_item_id, s.provider, s.source_use_epoch,
    NULL::uuid AS data_generation_id, NULL::bigint AS observation_revision,
    NULL::bigint AS policy_revision, NULL::bigint AS effective_age,
    CASE
      WHEN p_evaluated_at IS NULL OR p_resolved_repost_ttl_days IS NULL THEN 'context_unavailable'
      WHEN s.state='error' OR COALESCE((s.config->>'removed')::boolean,false) THEN 'source_unavailable'
      WHEN s.ratio=0 THEN 'source_off'
      WHEN NOT s.destination_active THEN 'destination_not_allowed'
      WHEN m.state<>'available' THEN 'unavailable'
    END AS base_reason
  FROM source_facts s JOIN media_items m ON m.workspace_id=s.workspace_id AND m.source_id=s.id
  WHERE s.provider='gdrive' AND (p_media_item_id IS NULL OR m.id=p_media_item_id)
  UNION ALL
  SELECT s.id, m.id, s.provider, s.source_use_epoch,
    i.data_generation_id, i.observation_revision, s.policy_revision, s.effective_age,
    CASE
      WHEN p_archive_enabled IS NOT TRUE THEN 'archive_not_activated'
      WHEN p_evaluated_at IS NULL OR p_resolved_repost_ttl_days IS NULL THEN 'context_unavailable'
      WHEN s.state<>'active' OR COALESCE((s.config->>'removed')::boolean,false) THEN 'source_unavailable'
      WHEN COALESCE(s.ratio>0,false) IS NOT TRUE THEN 'source_off'
      WHEN s.origin_state IS DISTINCT FROM 'active' OR s.origin_ref LIKE 'manual:%' THEN 'origin_unavailable'
      WHEN s.generation_id IS NULL OR s.lifecycle<>'active' THEN 'generation_unavailable'
      WHEN s.credential_state IS DISTINCT FROM 'active' OR s.credential_provider IS DISTINCT FROM 'ig_login'
        OR s.credential_expires_at<=p_evaluated_at THEN 'credential_unavailable'
      WHEN (s.schema_version IS DISTINCT FROM 1 AND s.schema_version IS DISTINCT FROM 2)
        OR s.carousel_slides NOT IN ('all','first') OR s.carousel_slides IS NULL
        OR s.eligibility IS DISTINCT FROM 'supported'
        OR s.ranking IS DISTINCT FROM 'none' OR s.destination_scope IS DISTINCT FROM 'origin_only' THEN 'unsupported_source_policy'
      WHEN i.data_generation_id IS DISTINCT FROM s.generation_id THEN 'awaiting_reobservation'
      WHEN i.exclusion_reason='origin_identity_conflict' OR m.source_projection_redacted IS TRUE THEN 'identity_conflict_or_deleted'
      WHEN i.availability='excluded' AND i.exclusion_reason='unsupported_media' THEN 'unsupported'
      WHEN i.availability IS DISTINCT FROM 'available' THEN 'unavailable'
      WHEN a.exclusion_reason='origin_identity_conflict' THEN 'identity_conflict_or_deleted'
      WHEN a.availability='excluded' AND a.exclusion_reason='unsupported_media' THEN 'unsupported'
      WHEN a.availability IS NOT NULL AND a.availability<>'available' THEN 'unavailable'
      WHEN m.id IS NOT NULL AND m.state<>'available' THEN 'unavailable'
      WHEN s.carousel_slides='first' AND a.position>1 THEN 'slide_not_first'
      WHEN a.validation_state='unsupported' OR a.exclusion_reason='unsupported_media' THEN 'unsupported'
      WHEN a.validation_capture->>'generation_id'=s.generation_id::text
        AND a.validation_capture->>'observation_revision'=a.observation_revision::text
        AND a.validation_capture->>'selected_asset_id'=a.asset_media_id
        AND a.validation_capture->>'state'='unsupported' THEN 'unsupported'
      WHEN a.validation_capture->>'generation_id'=s.generation_id::text
        AND a.validation_capture->>'observation_revision'=a.observation_revision::text
        AND a.validation_capture->>'selected_asset_id'=a.asset_media_id
        AND a.validation_capture->>'state'='validation_pending'
        AND a.validation_capture->>'reason' IN ('helper_unavailable','asset_unavailable')
        THEN a.validation_capture->>'reason'
      WHEN m.id IS NULL OR m.source_data_generation_id IS DISTINCT FROM a.data_generation_id THEN 'validation_pending'
      WHEN p_require_validation IS DISTINCT FROM false AND (
        a.validation_state IS DISTINCT FROM 'supported'
        OR a.validation_generation_id IS DISTINCT FROM a.data_generation_id
        OR a.validation_observation_revision IS DISTINCT FROM a.observation_revision
        OR a.validation_fingerprint IS NULL OR a.validated_at IS NULL) THEN 'validation_pending'
      WHEN EXISTS (SELECT 1 FROM instagram_intent_provenance b WHERE b.workspace_id=p_workspace_id
        AND b.intent_id=p_existing_intent_id AND (b.source_id IS DISTINCT FROM s.id
          OR b.media_item_id IS DISTINCT FROM m.id OR b.origin_ig_account_id IS DISTINCT FROM s.origin_ig_account_id
          OR b.selected_asset_identity IS DISTINCT FROM a.asset_media_id)) THEN 'selected_asset_changed'
      WHEN p_require_validation IS DISTINCT FROM false AND EXISTS (SELECT 1 FROM instagram_intent_provenance b
        WHERE b.workspace_id=p_workspace_id AND b.intent_id=p_existing_intent_id
        AND (b.capture_generation_id IS DISTINCT FROM a.data_generation_id
          OR b.capture_observation_revision IS DISTINCT FROM a.observation_revision
          OR b.validation_fingerprint IS DISTINCT FROM a.validation_fingerprint)) THEN 'archive_revalidation_required'
      WHEN p_evaluated_at-i.original_at < s.effective_age*interval '1 day' THEN 'too_recent'
      WHEN i.attribution='credited' AND s.include_credited IS NOT TRUE THEN 'credited_excluded'
      WHEN i.attribution='unknown' AND s.include_unknown IS NOT TRUE THEN 'attribution_unknown'
      WHEN NOT s.destination_active OR s.origin_ig_account_id<>p_destination_id OR NOT s.assigned THEN 'destination_not_allowed'
    END
  FROM source_facts s JOIN instagram_media_index i ON i.workspace_id=s.workspace_id AND i.source_id=s.id
  LEFT JOIN instagram_media_assets a ON a.workspace_id=i.workspace_id AND a.source_id=i.source_id
    AND a.parent_media_id=i.parent_media_id
  LEFT JOIN media_items m ON m.workspace_id=a.workspace_id AND m.source_id=a.source_id AND m.id=a.media_item_id
  WHERE s.provider='instagram_archive' AND (p_media_item_id IS NULL OR m.id=p_media_item_id)
), context AS (
  SELECT a.*,
    EXISTS (SELECT 1 FROM post_intents p WHERE p.workspace_id=p_workspace_id
      AND p.id=p_existing_intent_id AND p.media_item_id=a.media_item_id
      AND p.ig_account_id=p_destination_id AND p.origin='planned' AND NOT p.cancel_requested
      AND p.state NOT IN ('posted','skipped','rejected','expired','failed','cancelled')) AS planned_warning_override,
    EXISTS (SELECT 1 FROM post_intents p WHERE p.workspace_id=p_workspace_id
      AND p.media_item_id=a.media_item_id AND p.ig_account_id=p_destination_id
      AND p.state NOT IN ('posted','skipped','rejected','expired','failed','cancelled')
      AND (p_existing_intent_id IS NULL OR p.id<>p_existing_intent_id)) AS live_intent_conflict,
    ARRAY(SELECT DISTINCT l.kind FROM post_locks l WHERE l.workspace_id=p_workspace_id
      AND l.media_item_id=a.media_item_id AND (l.ig_account_id IS NULL OR l.ig_account_id=p_destination_id)
      AND (l.expires_at IS NULL OR l.expires_at>p_evaluated_at)
      AND l.kind IN ('reject','unsupported','hold','seasonal') ORDER BY l.kind) AS blocking_locks,
    ARRAY(SELECT DISTINCT l.kind FROM post_locks l WHERE l.workspace_id=p_workspace_id
      AND l.media_item_id=a.media_item_id AND (l.ig_account_id IS NULL OR l.ig_account_id=p_destination_id)
      AND (l.expires_at IS NULL OR l.expires_at>p_evaluated_at)
      AND l.kind IN ('skip','recent') ORDER BY l.kind) AS warning_locks
  FROM assets a
)
SELECT c.source_id,c.media_item_id,c.provider,
  c.base_reason IS NULL AND NOT c.live_intent_conflict AND cardinality(c.blocking_locks)=0 AND (cardinality(c.warning_locks)=0 OR c.planned_warning_override),
  c.base_reason IS NULL,
  COALESCE(c.base_reason,CASE WHEN c.live_intent_conflict THEN 'live_intent'
    WHEN cardinality(c.blocking_locks)>0 OR (cardinality(c.warning_locks)>0 AND NOT c.planned_warning_override) THEN 'locked' END),
  c.data_generation_id,c.observation_revision,c.policy_revision,c.source_use_epoch,c.effective_age,
  c.live_intent_conflict,c.blocking_locks,c.warning_locks
FROM context c
$$;
```

```sql
GRANT UPDATE (review_reason) ON meta_deletion_receipts TO svc_clock;
GRANT CREATE ON SCHEMA public TO svc_clock;
CREATE OR REPLACE FUNCTION fn_meta_receipt_cleanup_jobs(p_limit int)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE minted int;
BEGIN
 -- A purged workspace's cascade took every row a scope froze: nothing is left to clean.
 UPDATE meta_deletion_receipt_scopes s SET state = 'workspace_purged'
  WHERE s.state IN ('pending','cleaning','held')
    AND NOT EXISTS (SELECT 1 FROM workspaces w WHERE w.id = s.workspace_id);
 -- Match the progress door: only a transit-only review can complete without
 -- manual scope verification, and only after every scope is completed or purged.
 UPDATE meta_deletion_receipts r
    SET state = 'completed', review_reason = NULL, completed_at = now()
  WHERE (r.state = 'pending' AND r.review_reason IS NULL
         OR r.state = 'needs_review' AND r.review_reason = 'transit_unresolved')
    AND NOT EXISTS (SELECT 1 FROM meta_deletion_receipt_scopes s
                     WHERE s.receipt_id = r.id AND s.state NOT IN ('completed','workspace_purged'));
 -- Due work is a pending or cleaning scope, or a held scope whose frozen generations are not yet fenced.
 WITH due AS (
  SELECT s.receipt_id, s.workspace_id FROM meta_deletion_receipt_scopes s
   WHERE s.next_attempt_at <= now()
     AND (s.state IN ('pending','cleaning')
      OR (s.state = 'held' AND EXISTS (SELECT 1 FROM meta_deletion_receipt_generations x
           JOIN instagram_archive_generations g ON g.workspace_id = x.workspace_id
            AND g.source_id = x.source_id AND g.id = x.generation_id
          WHERE x.receipt_id = s.receipt_id AND x.workspace_id = s.workspace_id
            AND x.credential_id = s.credential_id AND x.authorization_id = s.authorization_id
            AND g.lifecycle IN ('active','superseded'))))
     AND EXISTS (SELECT 1 FROM workspaces w WHERE w.id = s.workspace_id)
     AND NOT EXISTS (SELECT 1 FROM jobs j
                      WHERE j.serialization_key = 'receipt:' || s.receipt_id || ':' || s.workspace_id
                        AND j.state IN ('ready','leased'))
   GROUP BY s.receipt_id, s.workspace_id
   ORDER BY min(s.next_attempt_at), s.receipt_id, s.workspace_id
   LIMIT LEAST(GREATEST(p_limit, 0), 25)
 )
 INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, deadline_at, payload)
 SELECT 'meta_deletion_cleanup', workspace_id, 'bulk', 'receipt:' || receipt_id || ':' || workspace_id, now(), 5,
  now() + interval '6 hours', jsonb_build_object('v', 1, 'receipt_id', receipt_id)
 FROM due;
 GET DIAGNOSTICS minted = ROW_COUNT;
 RETURN minted;
END $$;
COMMENT ON FUNCTION fn_meta_receipt_cleanup_jobs(integer) IS
  'The lifecycle sweep''s deletion leg: marks scopes of purged workspaces workspace_purged; completes a receipt once every scope is completed or purged only if it is pending without review or its sole review reason is transit_unresolved; other review reasons remain held. Mints at most LEAST(p_limit, 25) meta_deletion_cleanup jobs, one per (receipt, workspace) with due work and no ready or leased job on its key. SECURITY DEFINER owned by svc_clock; EXECUTE for svc_worker (148, #1644).';
ALTER FUNCTION fn_meta_receipt_cleanup_jobs(integer) OWNER TO svc_clock;
REVOKE ALL ON FUNCTION fn_meta_receipt_cleanup_jobs(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_meta_receipt_cleanup_jobs(integer) TO svc_worker;
REVOKE CREATE ON SCHEMA public FROM svc_clock;
```

```sql
CREATE INDEX ix_archive_tombstone_item_revision ON instagram_item_tombstones (workspace_id, source_id, media_item_id, observation_revision);
```
