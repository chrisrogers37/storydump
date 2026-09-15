SELECT '== clock', now();
SELECT '== cards of the Kermit story now: kind, state, ref, attempts, created, line';
SELECT o.kind, o.state, o.external_message_ref, o.attempts, to_char(o.created_at AT TIME ZONE 'UTC', 'HH24:MI:SS'),
       COALESCE(o.payload->>'supersedes_ref', '') AS edits_ref, left(o.payload->>'outcome_text', 48)
  FROM channel_outbox o WHERE o.intent_id::text LIKE 'b1c31076%' ORDER BY o.created_at;
SELECT '== the tap that healed it (tap admission spent?)', scope, key, window_start, count FROM rate_counters
 WHERE scope = 'ws_admission' AND window_start >= now() - interval '10 minutes' ORDER BY window_start DESC LIMIT 3;
