-- Migration 088: the intent ledger learns 'planned' (content schedule, phase 2; #1413, plan PR #1414).
--
-- A planned story is a `post_intents` row a person scheduled for a chosen time, beside the
-- cadence rows `plan_slot` mints. This file gives the ledger the shape to hold one; nothing
-- creates one yet (the verbs are a later phase).
--
-- THE COLUMNS. `origin` ('cadence' | 'planned', default 'cadence', so every existing row reads
-- as what it is) and `scheduled_by_user_id`. One ADD per statement: the tenancy gate refuses a
-- compound ALTER.
--
-- MANUAL BY CONSTRUCTION. `ck_intent_planned_manual`: a planned row is `approval_mode = 'manual'`.
-- The workspace's `approval_mode` and `auto_reapprove_returning` are stored settings nothing acts
-- on; this keeps a future auto path from ever applying to a planned row through its copy of them.
--
-- THE SLOT KEY, EXPAND HALF. `uq_intent_slot_cadence` is `uq_intent_slot` restricted to cadence
-- rows, so a planned row will never absorb a cadence slot's mint. The unconditional
-- `uq_intent_slot` STAYS: `plan_slot`'s conflict target now carries the predicate
-- (`scheduler.py`), and until every worker runs that spelling, a predicate-less
-- `ON CONFLICT (workspace_id, ig_account_id, schedule_slot_at)` must still find its arbiter.
-- Against the partial index alone it finds none and raises, so every cadence mint would fail.
-- Both spellings resolve while both indexes exist. Dropping the old key is a later file, after
-- this one has deployed and drained on every worker.
--
-- ONLY A PERSON APPROVES A PLANNED ROW. `trg_intent_planned_person` refuses
-- `awaiting_approval -> approved` on a planned row unless the actor is a person:
-- `app.actor_kind = 'user'` with an `app.actor_user_id`, what the approve paths set (the tap,
-- the web, a person-bound token). A service identity stamps `operator` and no user, and a system
-- path stamps `system`; both are refused. `origin` is fixed at birth, checked by the same
-- trigger: a rule keyed on a column that could be rewritten would be one UPDATE from not holding.
-- Every other edge, the pipeline's `publishing -> approved` wait among them, is untouched. The
-- trigger's WHEN is the rule's scope: a cadence row's update never runs the function at all.
--
-- Adoption evidence (#997): the two columns, the two CHECKs, the partial key and the trigger are
-- catalog state this file alone creates. Each probe reads false, without raising, on a database
-- that has no `post_intents` at all. The partial key is probed by its definition, never its name:
-- the contract half gives it the unconditional key's name, and a probe keyed on the name would
-- then read false below a true one, which `runner adopt` refuses as an incoherent chain.
--
-- runner:postcondition SELECT count(*) = 2 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'post_intents' AND column_name IN ('origin', 'scheduled_by_user_id')
-- runner:postcondition SELECT count(*) = 2 FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid JOIN pg_namespace n ON n.oid = t.relnamespace WHERE n.nspname = 'public' AND t.relname = 'post_intents' AND c.conname IN ('ck_intent_origin', 'ck_intent_planned_manual')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND tablename = 'post_intents' AND indexdef LIKE 'CREATE UNIQUE INDEX % (workspace_id, ig_account_id, schedule_slot_at) WHERE (origin = ''cadence''::text)')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_trigger g JOIN pg_class t ON t.oid = g.tgrelid JOIN pg_namespace n ON n.oid = t.relnamespace WHERE n.nspname = 'public' AND t.relname = 'post_intents' AND g.tgname = 'tg_intent_planned_person' AND NOT g.tgisinternal)

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
