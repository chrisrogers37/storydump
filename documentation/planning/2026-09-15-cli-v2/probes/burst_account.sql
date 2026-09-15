-- Read-only: every card tapped in the burst must end posted, skipped, or visibly waiting — nothing swallowed.
\set since '''2026-09-15 14:50+00'''
SELECT '== clock', now();
SELECT '== taps since 14:50 (transitions out of awaiting_approval, by outcome and actor)', a.to_state, a.actor_kind, count(*)
  FROM audit_events a WHERE a.entity_kind = 'post_intent' AND a.created_at >= :since AND a.from_state = 'awaiting_approval'
 GROUP BY 1, 2 ORDER BY 1, 2;
SELECT '== every intent touched since 14:50, by its state NOW', i.state, count(*)
  FROM post_intents i WHERE i.id IN (SELECT entity_id FROM audit_events WHERE entity_kind = 'post_intent' AND created_at >= :since)
 GROUP BY 1 ORDER BY 2 DESC;
SELECT '== still awaiting approval (never tapped, or the tap never arrived)', count(*) FROM post_intents WHERE state = 'awaiting_approval';
SELECT '== approved right now (would be waiting or floating)', left(id::text, 8), publish_step, cap_consumed_on,
       to_char(entered_state_at AT TIME ZONE 'UTC', 'HH24:MI:SS') FROM post_intents WHERE state = 'approved';
SELECT '== publishing / ambiguous / review right now', state, count(*) FROM post_intents
 WHERE state IN ('publishing', 'publishing_ambiguous', 'review_required') GROUP BY 1;
SELECT '== publish jobs minted since 14:50, by state', j.state, count(*) FROM jobs j
 WHERE j.kind = 'publish_pipeline' AND j.created_at >= :since GROUP BY 1 ORDER BY 2 DESC;
SELECT '== publish jobs not finished (any age)', left(j.id::text, 8), j.state, j.attempts,
       to_char(j.run_at AT TIME ZONE 'UTC', 'HH24:MI:SS'), left(j.payload->>'intent_id', 8)
  FROM jobs j WHERE j.kind = 'publish_pipeline' AND j.state NOT IN ('succeeded', 'cancelled', 'failed');
SELECT '== failed since 14:50, with the reason', left(id::text, 8), left(last_error::text, 220)
  FROM post_intents WHERE state = 'failed' AND entered_state_at >= :since;
SELECT '== outbox rows since 14:50 (card edits, notices), by kind and state', kind, state, count(*)
  FROM channel_outbox WHERE created_at >= :since GROUP BY 1, 2 ORDER BY 1, 2;
SELECT '== card lines of the posted ones (sample)', left(intent_id::text, 8), payload->>'outcome_text'
  FROM channel_outbox WHERE kind = 'approval_prompt' AND intent_id IN
       (SELECT id FROM post_intents WHERE state = 'posted' AND entered_state_at >= :since) ORDER BY updated_at DESC LIMIT 5;
SELECT '== full timeline since 14:50', to_char(a.created_at AT TIME ZONE 'UTC', 'HH24:MI:SS'), left(a.entity_id::text, 8),
       a.from_state, a.to_state, a.actor_kind
  FROM audit_events a WHERE a.entity_kind = 'post_intent' AND a.created_at >= :since AND a.from_state IS DISTINCT FROM a.to_state
 ORDER BY a.created_at;
