-- Migration 106: the activation nudge — one email, once, to a person who stopped part-way through
-- setup, and the stage definition the funnel and the nudge now share (#1481; `07` §49). Statements
-- appended to the advertised stream.
--
-- BUILT OFF. Nothing here sends anything. The sweep that reads the new door is a worker kind the
-- registry parks unless TWO gates are both open: TARGET_ACTIVATION_NUDGE_ENABLED, and an email
-- provider. With either closed the clock is never asked to mint the kind. This file only makes the
-- kind legal and the door exist, so turning the nudge on later is a config change, not a migration.
--
-- ONE DEFINITION OF A STAGE, TWO READERS. 104's fn_activation_funnel computed each person's five
-- stage timestamps inline. The nudge needs the same timestamps per person rather than as counts, and
-- two copies of "what counts as connecting Instagram" would drift. So the per-person computation
-- moves into fn_activation_stages, unchanged, and both doors read it. fn_activation_funnel is
-- replaced with the same signature and the same result: the CTE that placed and counted people is
-- kept, only the stage timestamps now come from the shared function. fn_activation_stages is a
-- SECURITY INVOKER helper owned by svc_maintenance with EXECUTE revoked from PUBLIC: only the two
-- doors, which run as svc_maintenance, can call it, and it is not a door itself.
--
-- WHO GETS A NUDGE. fn_activation_stalled lists the people whose first missing stage is 3
-- (Instagram), 4 (a folder) or 5 (a first approval), who signed up at or after p_since, who have an
-- email, who have never been nudged, and who have been idle for p_stall. Stage 2 (no workspace) is
-- counted by the funnel and never emailed. Stage 5 counts only once a card was offered, an audit row
-- the intent trigger wrote moving a post_intent into awaiting_approval: a person who added a folder
-- and never got a card is the product's stall (no eligible media, a schedule window, a failed sync),
-- not theirs, and an auto-approve workspace never offers one at all. For stage 5 the idle clock
-- starts at the first card, since nobody can approve before one exists. The most recent stalls come
-- first, so a capped run spends its emails on the people most likely to come back.
--
-- ONCE PER PERSON, ON users. 104's stages are a person's, across the workspaces they own, so the
-- latch is a person's too: users.activation_nudge_at, nullable. The sweep sets it with a conditional
-- UPDATE ... RETURNING whose predicate repeats the door's (still NULL, still active, still has an
-- email), and enqueues the send_email job in the same transaction, so a person is nudged once even
-- if two sweeps overlap. users is the user plane (058 class 3): svc_worker already holds SELECT,
-- INSERT and UPDATE on it (057) under a row-open policy, so no grant is needed.
--
-- THE KIND IS A SYSTEM KIND, IN BOTH CONSTRAINTS. One sweep row covers every workspace, so it has no
-- tenant: the reap_expired shape. 065's warning holds: ck_jobs_system_kinds is a biconditional, and a
-- kind added to ck_jobs_kind alone would make the clock's whole tick abort on its first leg. Both
-- are widened here. It is not added to the reaper's list of kinds it may end past their deadline
-- (089): like retention_sweep, a late sweep is still claimed and runs late rather than stranding.
--
-- GRANTS FOLLOW 104. EXECUTE on the new door for svc_worker alone, never svc_ingress, so no API
-- route or CLI verb reaches it. Every definer pins search_path with pg_temp last (103). The CREATE
-- bracket is 062's: ALTER FUNCTION ... OWNER TO needs the incoming owner to hold CREATE on the
-- schema, and the steady state never leaves it there.
--
-- NO BEGIN/COMMIT: the runner owns one transaction per file, as for every target-lineage sibling.

-- runner:postcondition SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'users' AND column_name = 'activation_nudge_at' AND data_type = 'timestamp with time zone')
-- runner:postcondition SELECT pg_get_constraintdef(oid) LIKE '%activation_nudge_sweep%' FROM pg_constraint WHERE conname = 'ck_jobs_kind'
-- runner:postcondition SELECT pg_get_constraintdef(oid) LIKE '%activation_nudge_sweep%' FROM pg_constraint WHERE conname = 'ck_jobs_system_kinds'
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_roles r ON r.oid = p.proowner WHERE n.nspname = 'public' AND p.proname = 'fn_activation_stalled' AND p.prosecdef AND r.rolname = 'svc_maintenance')
-- runner:postcondition SELECT count(*) >= 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace, aclexplode(p.proacl) a JOIN pg_roles g ON g.oid = a.grantee WHERE n.nspname = 'public' AND p.proname = 'fn_activation_stalled' AND a.privilege_type = 'EXECUTE' AND g.rolname = 'svc_worker'
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_roles r ON r.oid = p.proowner WHERE n.nspname = 'public' AND p.proname = 'fn_activation_stages' AND NOT p.prosecdef AND r.rolname = 'svc_maintenance')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.proname = 'fn_activation_funnel' AND position('fn_activation_stages' in p.prosrc) > 0)

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
