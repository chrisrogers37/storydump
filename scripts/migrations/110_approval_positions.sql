-- Migration 110: one approval rule for every story, and the shape auto-approval needs (#1477;
-- plan `documentation/planning/2026-10-11-auto-approval/01_ledger.md`; `07` §53). Statements
-- appended to the advertised stream. Nothing reads the new columns yet: the serve step that
-- acts on them is phase 02, so this file changes no behaviour except the one the trigger adds.
--
-- THE RECORD. `post_intents.approval_rule` is the rule that approved a story: NULL when a person
-- approved it (or nothing has yet), else 'auto' (YOLO), 'item' (the item's flag) or 'returning'
-- (posted through the API before). It is a new column, not a reading of `approval_mode`: that
-- column is the workspace's mode copied at mint, terminal rows are frozen (055), and a story a
-- person approved under a saved 'auto' would read as automatic forever. `approval_mode` stops
-- being copied (every insert writes 'manual') and stays as history.
--
-- A WAITING STORY IS ALWAYS ONE A PERSON DECIDES. `ck_intent_waiting_unruled`: a rule appears
-- only with the approval, so no story is born carrying one, whoever inserts it.
--
-- ONE APPROVAL RULE FOR EVERY STORY. `tg_intent_person_or_rule` replaces 088's planned-only
-- `tg_intent_planned_person`: `awaiting_approval -> approved` is a person's (`app.actor_kind =
-- 'user'` with an `app.actor_user_id`, rule left NULL) or the system's stamping the rule in the
-- same UPDATE; `approval_rule` changes on no other update; `origin` stays fixed at birth. Before
-- this file a cadence row had no such guard: only Python kept a non-person from approving it.
-- 088's `ck_intent_planned_manual` goes: a planned story may now be approved automatically
-- (owner, 2026-10-11), and the column it pinned is history. Every other edge into `approved`
-- (the review retry, the float's step back) is untouched: the trigger's WHEN is the rule's scope.
-- Its name sorts after `tg_intent_guard` and `tg_intent_no_self_transition`, as 088's did:
-- BEFORE row triggers fire in name order, and 061 holds the guard first.
--
-- POSITIONS. `workspaces.approval_mode` is 'manual' (ask every time), 'returning' or 'auto'
-- (YOLO); `media_sources.approval_mode` is the same, NULL meaning the workspace's. A value nothing
-- ever honored is not consent, so every workspace starts at 'manual' (F4). The governance audit
-- refuses an anonymous write to `workspaces`, so the reset names the migration as its actor and
-- clears it again (SET LOCAL, so neither outlives this file's transaction).
-- `auto_reapprove_returning` stays until 02's contract half drops it: code running before this
-- file still reads it.
--
-- LOCK ORDER. The small tables come first and `post_intents` last, so the busiest table is held
-- only for its own statements and the commit, not across the others' lock waits (the runner
-- bounds each wait and retries the whole file).
--
-- THE FLAG. `media_items.auto_approve`, false until a member sets it.
--
-- "POSTED THROUGH THE API BEFORE". `ix_intents_api_posted` serves the one EXISTS phase 02 asks
-- per serve; no index held an item's posted history (`uq_intent_live_subject` is live states
-- only).
--
-- Adoption: 088's postconditions assert the trigger and the CHECK this file drops, so the
-- adoption manifest carries an explicit probe for 088 asserting what of it survives.

-- runner:postcondition SELECT count(*) = 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'post_intents' AND column_name = 'approval_rule'
-- runner:postcondition SELECT count(*) = 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'media_sources' AND column_name = 'approval_mode'
-- runner:postcondition SELECT count(*) = 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'media_items' AND column_name = 'auto_approve'
-- runner:postcondition SELECT count(*) = 3 FROM pg_constraint WHERE conname IN ('ck_intent_approval_rule', 'ck_intent_waiting_unruled', 'ck_source_approval_mode')
-- runner:postcondition SELECT COALESCE((SELECT pg_get_constraintdef(oid) LIKE '%returning%' FROM pg_constraint WHERE conname = 'ck_ws_approval_mode'), false)
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND tablename = 'post_intents' AND indexname = 'ix_intents_api_posted')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_trigger g JOIN pg_class t ON t.oid = g.tgrelid JOIN pg_namespace n ON n.oid = t.relnamespace WHERE n.nspname = 'public' AND t.relname = 'post_intents' AND g.tgname = 'tg_intent_person_or_rule' AND NOT g.tgisinternal)

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
