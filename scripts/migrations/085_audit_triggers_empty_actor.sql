-- Migration 085: the audit triggers read an empty actor as unset (#1421).
--
-- THE GAP. trg_intent_audit and trg_governance_audit (055) refuse an
-- actor-less write by testing `current_setting('app.actor_kind', true) IS
-- NULL`, which reads NULL only on a connection that has never set the
-- setting. Once a transaction has run `set_config('app.actor_kind', …, true)`,
-- the session keeps the setting defined after that transaction ends, and a
-- later transaction on the same pooled connection reads it as '' (measured on
-- PostgreSQL 15 under #1402). On such a connection:
--   * an actor-less intent state change passed the trigger's test and was
--     refused only because its audit row then failed ck_audit_actor: an
--     accident of that table, with a message that does not name the rule;
--   * an actor-less write that moved only a governance table's machinery
--     columns (ig_accounts.next_slot_at / last_posted_at,
--     oauth_credentials.next_refresh_at / expires_at, among others) was not
--     refused at all.
--     Those writes exit before the audit INSERT, so nothing reached the CHECK.
--
-- THE FIX. Both tests become `NULLIF(current_setting('app.actor_kind', true),
-- '') IS NULL`, the idiom both bodies already use for app.actor_user_id and
-- app.channel: unset and empty are one case. Apart from that line and a
-- two-line `-- 085:` comment above it, the bodies are 055's, byte for byte.
--
-- CREATE OR REPLACE, not DROP + CREATE: 076's shape, not 084's. Six triggers
-- depend on these two functions, and a replace keeps them attached and keeps
-- the functions' owner and grants.
--
-- WHAT CHANGES AT RUNTIME. 055 already refused an actor-less write on a fresh
-- connection, so a path that writes without an actor already fails on the
-- first transaction each pooled connection serves, and every unit of work in
-- src/ names an actor. What changes is that the refusal no longer depends on
-- the connection's history.
--
-- Adoption evidence (#997): the new test is text this file alone puts in both
-- functions' source.
-- runner:postcondition SELECT count(*) = 2 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.proname IN ('trg_intent_audit', 'trg_governance_audit') AND position('NULLIF(current_setting(''app.actor_kind'', true), '''') IS NULL' IN p.prosrc) > 0

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
