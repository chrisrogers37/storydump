-- Migration 116: a planned story repeats. The rule rides on the story, and the ledger makes its
-- next occurrence when the last one ends (recurring schedules, phase 1; #485, plan PR #1687;
-- `07` §59). Statements appended to the advertised stream.
--
-- THE RULE. Four nullable columns on post_intents: repeat_unit ('day' or 'week'), repeat_every
-- (every N units), repeat_days (ISO weekdays, weekly only) and repeat_at (the local time of day).
-- All four or none, and only on a planned story (ck_intent_repeat). The unit list belongs to
-- ck_intent_repeat_unit alone, which vocabulary.REPEAT_UNITS is pinned to. There is no start
-- date: each occurrence's own date anchors the grid, and moving the waiting occurrence moves the
-- series.
--
-- THE WALL TIME. fn_wall_instant(local, zone) reads a local time as an instant: the earliest of
-- two readings that reads back as that wall time, which is the first occurrence of a repeated
-- hour, or NULL for a time the clocks skip. One-off planning reads a chosen time through it
-- (command_executors._INSTANT), and the grid reads every occurrence through it, so the two
-- cannot disagree. The zone is used as given: the caller decides what a decayed zone means.
--
-- THE GRID. fn_repeat_times(unit, every, days, at, anchor, zone, lo, hi) returns the rule's times
-- in [lo, hi). With hi NULL it returns them up to two local days past one period after lo, a
-- window that always holds the first time at or after lo. It is the one definition of "when next":
-- the trigger takes the first time it returns with hi NULL, and the calendar (phase 4) asks it for
-- a month.
-- - It is STABLE, because AT TIME ZONE reads the tz database.
-- - A decayed zone degrades to UTC through fn_safe_tz, as fn_next_slot's does.
-- - A time the clocks skip takes the offset in force before the jump.
-- - The zone and the anchor are computed once per call (MATERIALIZED), and days step as integers.
--
-- THE SUCCESSOR. tg_intent_repeat (AFTER UPDATE OF state) fires when a story with a rule enters a
-- terminal state other than rejected, since Reject means never. It inserts the next occurrence
-- in the same transaction, unless:
-- - the miss sweep recorded a reason other than late or paused;
-- - the workspace is offboarding;
-- - the account is no longer live (command_executors._LIVE_ACCOUNT, the one spelling).
-- After a late or paused miss, the next time is the rule's first after now, so an outage gives one
-- miss and no cascade. Otherwise it is the rule's first after the ended occurrence's time.
-- After a post it is also on a later local day than the post, in the story's zone: a series posts
-- at most once a local day, so neither a card answered just before the next time nor an account
-- moved to a zone further west posts the item twice in a day.
-- The INSERT names its columns, so every other column takes its default, a column a later
-- migration adds included: nothing decided about one occurrence carries over to the next. It
-- writes approval_mode as 'manual', as every insert has since 110. The trigger is total: any
-- failure records a repeat_failed audit row and a warning, and lets the ending stand. A series can
-- therefore never stall fn_reaper_sweep's other legs or the offboarding drain.
--
-- THE GRANT. fn_reaper_sweep runs as svc_maintenance (SECURITY DEFINER), and its cancel and
-- approval-timeout legs end stories, so svc_maintenance must be able to INSERT the successor.
-- Its policy p_maint_intents (058) already admits every row; only the privilege was missing
-- (057 grants it SELECT and UPDATE).
--
-- Adoption evidence (#997): the four columns, the two CHECKs, the two functions, the trigger and
-- the grant are catalog state this file alone creates. Every probe reads the catalogs by name, so
-- it reads false, without raising, on a database that has no post_intents at all.

-- runner:postcondition SELECT count(*) = 4 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'post_intents' AND column_name IN ('repeat_unit', 'repeat_every', 'repeat_days', 'repeat_at')
-- runner:postcondition SELECT count(*) = 2 FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid JOIN pg_namespace n ON n.oid = t.relnamespace WHERE n.nspname = 'public' AND t.relname = 'post_intents' AND c.conname IN ('ck_intent_repeat_unit', 'ck_intent_repeat')
-- runner:postcondition SELECT count(*) = 2 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.proname IN ('fn_repeat_times', 'fn_wall_instant') AND NOT p.prosecdef
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_trigger g JOIN pg_class t ON t.oid = g.tgrelid JOIN pg_namespace n ON n.oid = t.relnamespace WHERE n.nspname = 'public' AND t.relname = 'post_intents' AND g.tgname = 'tg_intent_repeat' AND NOT g.tgisinternal)
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace, aclexplode(c.relacl) a JOIN pg_roles g ON g.oid = a.grantee WHERE n.nspname = 'public' AND c.relname = 'post_intents' AND a.privilege_type = 'INSERT' AND g.rolname = 'svc_maintenance')

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
