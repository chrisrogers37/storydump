-- Migration 090: the outbox records why a delivery failed, and the estate's delivery failures get a
-- door (#1482; `07` §33). Statements appended to the advertised stream.
--
-- THE GAP. `settle` sorts a failed send by type: a 429 goes back to `pending`, a gone destination and
-- a refused message end `failed`, anything else is `ambiguous`. Then it writes the state and nothing
-- else, so a burst of failures leaves no cause in the database. Production's 21 failed rows of
-- 2026-09-12 (all card edits, 16 of them in one hour) cannot say today whether the chat was gone,
-- the message refused or the token dead. And nothing counted failures at all, so nothing could alert
-- on them.
--
-- THREE NULLABLE COLUMNS, one fact each: the class of the row's LAST failed attempt
-- (`last_failure_class`, a closed list that `vocabulary.py` copies), the provider's code for it
-- (`last_error_code`: Telegram's `error_code`, NULL when no answer came back) and when
-- (`last_failed_at`). They describe the last failure, not how the row ended: a later success does
-- not clear them, and `state` still says how it ended. `credential_dead` is a dead token's 401, which
-- the outbox has filed as a lost response. This file records that and changes no state; whether such
-- a row should fail at once is #1493. Nullable with no default, so ADD COLUMN touches the catalog
-- only; the CHECK validates NULLs; code that predates this file never names the columns.
--
-- THE INDEX serves the failures door's one predicate, `last_failed_at >= now() - window`. It is
-- partial on the rows that have ever failed, so it grows with the failures rather than the table.
--
-- TWO DOORS, 081's shape: owned by svc_maintenance (USING (true) and SELECT on channel_outbox,
-- 057/058), EXECUTE for svc_ingress (the /health/delivery route) and svc_worker, the window clamped
-- to [60 s, 24 h], counts only (/health/delivery is unauthenticated).
--   * `fn_health_outbox_failures(p_window_seconds)`: the rows whose last failure falls in the window,
--     by class and code, and how many of them ended `failed` or sit `ambiguous` (the alerting count;
--     a 429 is a deferral, reported as context and never alerted on).
--   * `fn_health_outbox_sent(p_window_seconds)`: the rows sent in the same window, so an hour with no
--     failures and no traffic cannot read as an hour that delivered (#1120).
--
-- The CREATE bracket is 062's (081 restates why).
--
-- DEPLOY ORDER: the predeploy applies this file before the code that writes the columns starts, and a
-- worker still draining never names them. Additive: rolling the code back leaves them NULL.
--
-- Adoption evidence + post-apply verification: one probe per structural thing this file creates,
-- each reading the catalogs by name, so on a database that predates the target tables it reads
-- false rather than raising (a `regclass` cast would raise).
-- runner:postcondition SELECT count(*) = 3 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'channel_outbox' AND column_name IN ('last_failure_class', 'last_error_code', 'last_failed_at')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid JOIN pg_namespace n ON n.oid = t.relnamespace WHERE c.conname = 'ck_outbox_failure_class' AND n.nspname = 'public' AND t.relname = 'channel_outbox')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = 'ix_outbox_last_failed')
-- runner:postcondition SELECT count(*) = 2 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_roles r ON r.oid = p.proowner WHERE n.nspname = 'public' AND p.proname IN ('fn_health_outbox_failures', 'fn_health_outbox_sent') AND r.rolname = 'svc_maintenance' AND p.prosecdef
-- runner:postcondition SELECT count(*) >= 4 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace, aclexplode(p.proacl) a JOIN pg_roles g ON g.oid = a.grantee WHERE n.nspname = 'public' AND p.proname IN ('fn_health_outbox_failures', 'fn_health_outbox_sent') AND a.privilege_type = 'EXECUTE' AND g.rolname IN ('svc_ingress', 'svc_worker')

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
  'Outbox rows whose last failure falls in the window, every tenant''s included, by class and code; o_alerting_rows ended failed or sit ambiguous. Counts only. Window clamped to [60 s, 24 h]. Owned by svc_maintenance; EXECUTE for svc_ingress and svc_worker (090, #1482).';

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
  'Outbox rows sent in the window, every tenant''s included: the traffic the failure count is read against. Window clamped to [60 s, 24 h]. Owned by svc_maintenance; EXECUTE for svc_ingress and svc_worker (090, #1482).';

ALTER FUNCTION fn_health_outbox_sent(p_window_seconds integer) OWNER TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_health_outbox_sent(p_window_seconds integer) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_health_outbox_sent(p_window_seconds integer) TO svc_ingress, svc_worker;

REVOKE CREATE ON SCHEMA public FROM svc_maintenance;
