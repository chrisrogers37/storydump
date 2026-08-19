-- Migration: 055_intent_ledger_tables.sql
-- Description: F.2.4 — plan 02 §3's intent ledger. Five tables
--   (category_post_case_mix, post_intents, audit_events, daily_post_counts,
--   post_intent_transitions), the 27 seeded legal-edge rows, four trigger
--   functions (trg_intent_guard, trg_intent_audit, trg_intent_insert_guard,
--   trg_governance_audit) and the triggers that attach them.
--
--   A CONTIGUOUS PREFIX OF THE ADVERTISED STREAM, and that is the whole
--   contract this file is under. `04` §0.2 arm (b) diffs the concatenated
--   target lineage against the expanded 02+07 stream as an ORDERED PREFIX, so
--   this file is statements 45..76 of that stream in that order, continuing
--   054's 22..44, 053's 2..21 and 052's 0..1. Nothing here is authored — the
--   body is the plan's own SQL, extracted from the stream rather than
--   transcribed. Edit the plan and the manifest ratchet, never this file alone.
--
--   NUMBERED 055, NOT 054. 054 is F.2.3 (merged, #844). PR #840 also proposes a
--   054 for a LEGACY-lineage change and is blocked pending a lineage ruling; it
--   collides with the merged 054 regardless of this file and must renumber. If
--   that ruling lands it below the 051 move, this file is unaffected; if it
--   lands above, it takes the next free number after this one.
--
--   NO POLICIES, AND NO RLS — deliberately, per the #806 Fork 1 ruling (a) and
--   the ratified split (`documentation/planning/2026-08-14-f2-increment-split`,
--   §3): F.2.2-F.2.5 land tables, triggers and indexes, and RLS plus policies
--   for all of them land together in F.2.7. Split §4 records this file as
--   moving the lineage from 13 tables to 18 with RLS still at 0.
--
--   THIS INCREMENT CARRIES DATA, which the three before it did not. Statement
--   64 seeds post_intent_transitions with the 27 legal edges. They are not
--   fixture data: `trg_intent_guard` reads the table on every UPDATE, so an
--   unseeded schema rejects every transition while still replaying green —
--   the pass-5/R4 finding the seed exists to close. The advertised_ddl_replay
--   fixture asserts one legal and one illegal transition against these rows,
--   so the matrix text and the seeds cannot drift apart.
--
--   IT ALSO ATTACHES TRIGGERS TO EARLIER INCREMENTS' TABLES. The five
--   tg_audit_* governance triggers land on workspaces, workspace_members,
--   channel_bindings (053) and ig_accounts, oauth_credentials (054). That is
--   the stream's order, not a liberty taken here: audit_events must exist
--   before anything can write to it, which is why the reshape moved
--   audit_events into this increment (split §3).
--
--   DEPENDS ON 052, 053 AND 054. Every touch trigger calls
--   `trg_touch_updated_at` (052); every table references workspaces(id) (053);
--   post_intents carries composite FKs to ig_accounts and media_items and the
--   governance triggers attach to oauth_credentials and ig_accounts (054).
--
--   ABOVE THE LINEAGE BOUNDARY. 051 renames the legacy schema out of public;
--   these objects are created into the empty public it leaves behind. The
--   running application does not see them until the M.3 cutover.
--
-- Rollback: DROP TABLE IF EXISTS post_intent_transitions, daily_post_counts,
--   audit_events, post_intents, category_post_case_mix CASCADE;
--   DROP FUNCTION IF EXISTS trg_intent_guard(), trg_intent_audit(),
--   trg_intent_insert_guard(), trg_governance_audit();
--   The CASCADE drops the five tg_audit_* triggers from 053/054's tables with
--   their function; those tables themselves survive.
-- Created: 2026-08-19
-- Issue: #806
-- EVERY POSTCONDITION BELOW MUST STAY TRUE FOREVER, not merely at the end of
--   this file's run. `migration_runner.py` derives a file's permanent ADOPTION
--   PROBE from its postconditions when the adoption manifest carries no entry,
--   so each line answers two questions: "did this file do its job just now" and
--   "has this file ever been applied to this database". A postcondition
--   asserting the ABSENCE of state a LATER increment adds passes the first and
--   then fails the second. So every line below is scoped to an object 055
--   itself creates, and none asserts that something does not exist: F.2.7
--   enables RLS on these tables and attaches their policies, and that may not
--   retroactively un-apply this migration. The seed probe names ONE edge rather
--   than counting rows, because a later increment may legitimately add an edge
--   and a count would then read unapplied.
-- runner:postcondition SELECT count(*) = 5 FROM information_schema.tables WHERE table_schema = 'public' AND table_name IN ('category_post_case_mix','post_intents','audit_events','daily_post_counts','post_intent_transitions')
-- runner:postcondition SELECT count(*) = 4 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.proname IN ('trg_intent_guard','trg_intent_audit','trg_intent_insert_guard','trg_governance_audit')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM post_intent_transitions WHERE from_state = 'scheduled' AND to_state = 'prompt_pending')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = 'uq_publish_exclusive')
-- runner:postcondition SELECT count(*) = 5 FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND NOT t.tgisinternal AND t.tgname IN ('tg_audit_workspaces','tg_audit_workspace_members','tg_audit_oauth_credentials','tg_audit_ig_accounts','tg_audit_channel_bindings')

-- [§3 intent ledger: case mix, intents, audit, counts, transitions]
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

-- [§3 post_intents + audit_events + daily_post_counts]
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

CREATE INDEX ix_audit_retire ON audit_events (created_at);

-- the retention export's oldest-first
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

CREATE INDEX ix_dpc_retire ON daily_post_counts (local_date);

-- retention walk (§5 pattern; the
                                                               -- PK leads with workspace_id, so
                                                               -- age-ordered sweeps need this)
-- OUR product cadence cap — never Meta's (§8). Debit/refund SQL is in §4 (the transition owns it).

-- [§3 intent uniqueness keys]
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

-- [§3 reaper access-path indexes]
-- Reaper access paths (pass 5 — the H5 discipline applied to the reaper's own scans: post_intents
-- is kept forever (05 retention), so an unindexed working-state scan would re-walk the entire
-- ledger every sweep. Partial indexes bound each scan to the live working rows — a set that is
-- small at any instant regardless of ledger age):
CREATE INDEX ix_intents_reap_slot ON post_intents (schedule_slot_at)
  WHERE state IN ('scheduled','prompt_pending');

CREATE INDEX ix_intents_reap_age ON post_intents (entered_state_at)
  WHERE state IN ('awaiting_approval','approved');

-- [§4 transitions table + 27-row seed + guard/audit triggers]
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
