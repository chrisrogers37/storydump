-- The float, read from the production ledger after the deploy (plan 03's third checklist item).
-- Read-only. Run: railway run --service storydump -- sh -c 'psql "$TARGET_DATABASE_URL" -At -F " | " -f /dev/stdin' < this.sql | sed -E "s#postgres(ql)?://[^ ]+#postgres://<redacted>#g"
\set since '''now() - interval ''3 hours'''''

SELECT '== clock', now();

SELECT '== floating now (approved with progress): intent, step, counters, next run';
SELECT left(i.id::text, 8), i.publish_step, i.attempts_by_step::text,
       to_char(j.run_at AT TIME ZONE 'UTC', 'HH24:MI:SS') AS next_run, j.state AS job_state, j.attempts
  FROM post_intents i
  LEFT JOIN jobs j ON j.kind = 'publish_pipeline' AND j.payload->>'intent_id' = i.id::text
   AND j.state IN ('ready', 'leased')
 WHERE i.state = 'approved' AND i.cap_consumed_on IS NOT NULL
 ORDER BY j.run_at NULLS LAST;

SELECT '== float waits in the window: class, rung, seconds, when';
SELECT to_char(a.created_at AT TIME ZONE 'UTC', 'HH24:MI:SS'), left(a.entity_id::text, 8),
       a.detail->>'class' AS class, a.detail->>'rung' AS rung, a.detail->>'seconds' AS seconds,
       a.detail->'counters' AS counters
  FROM audit_events a
 WHERE a.entity_kind = 'post_intent' AND a.detail->>'event' = 'float_wait'
   AND a.created_at > now() - interval '3 hours'
 ORDER BY a.created_at;

SELECT '== container permits in the window: variant, outcome, error (a refused first url, a fresh one accepted)';
SELECT to_char(o.created_at AT TIME ZONE 'UTC', 'HH24:MI:SS'), left(o.intent_id::text, 8), o.generation,
       o.state, o.response_ref->>'url_variant' AS variant, o.response_ref->>'error' AS error,
       o.response_ref->'meta'->>'subcode' AS subcode, o.response_ref->>'elapsed_ms' AS ms
  FROM provider_operations o
 WHERE o.op_kind = 'container_create' AND o.created_at > now() - interval '3 hours'
 ORDER BY o.created_at;

SELECT '== the round shape: refusals per intent and whether it posted';
SELECT left(o.intent_id::text, 8), i.state,
       count(*) FILTER (WHERE o.state = 'failed') AS refused,
       count(*) FILTER (WHERE o.state = 'succeeded') AS accepted,
       max((o.response_ref->>'url_variant')::int) AS max_variant,
       i.attempts_by_step::text
  FROM provider_operations o JOIN post_intents i ON i.id = o.intent_id
 WHERE o.op_kind = 'container_create' AND o.created_at > now() - interval '3 hours'
 GROUP BY o.intent_id, i.state, i.attempts_by_step
 ORDER BY min(o.created_at);

SELECT '== siblings posting past a waiter: posted stories whose account had a floating story at the time';
SELECT to_char(p.created_at AT TIME ZONE 'UTC', 'HH24:MI:SS') AS posted_at, left(p.entity_id::text, 8) AS posted,
       left(w.entity_id::text, 8) AS was_waiting, w.detail->>'class' AS its_wait
  FROM audit_events p
  JOIN post_intents pi ON pi.id = p.entity_id
  JOIN audit_events w ON w.detail->>'event' = 'float_wait' AND w.entity_id <> p.entity_id
   AND w.created_at < p.created_at AND (w.detail->>'next_run_at')::timestamptz > p.created_at
  JOIN post_intents wi ON wi.id = w.entity_id AND wi.ig_account_id = pi.ig_account_id
 WHERE p.to_state = 'posted' AND p.created_at > now() - interval '3 hours'
 ORDER BY p.created_at;

SELECT '== review cards raised in the window (expected: none unless a real outage)';
SELECT to_char(a.created_at AT TIME ZONE 'UTC', 'HH24:MI:SS'), left(a.entity_id::text, 8), a.from_state,
       left(i.last_error::text, 160)
  FROM audit_events a JOIN post_intents i ON i.id = a.entity_id
 WHERE a.to_state = 'review_required' AND a.created_at > now() - interval '3 hours'
 ORDER BY a.created_at;

SELECT '== the window''s outcomes';
SELECT i.state, count(*) FROM post_intents i
 WHERE i.entered_state_at > now() - interval '3 hours' GROUP BY i.state ORDER BY 2 DESC;
