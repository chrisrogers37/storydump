SELECT '== the 13 posted: served (card minted), tapped (approved), posted — in tap order';
WITH t AS (
  SELECT a.entity_id AS id,
         min(a.created_at) FILTER (WHERE a.to_state = 'awaiting_approval') AS served,
         min(a.created_at) FILTER (WHERE a.from_state = 'awaiting_approval' AND a.to_state = 'approved') AS tapped,
         min(a.created_at) FILTER (WHERE a.to_state = 'posted') AS posted
    FROM audit_events a WHERE a.entity_kind = 'post_intent' AND a.created_at >= '2026-09-15 00:00+00' GROUP BY 1)
SELECT left(id::text, 8), to_char(served AT TIME ZONE 'UTC', 'MM-DD HH24:MI'), to_char(tapped AT TIME ZONE 'UTC', 'HH24:MI:SS'),
       to_char(posted AT TIME ZONE 'UTC', 'HH24:MI:SS'),
       rank() OVER (ORDER BY served) AS served_rank, rank() OVER (ORDER BY tapped) AS tap_rank, rank() OVER (ORDER BY posted) AS post_rank
  FROM t WHERE posted IS NOT NULL AND tapped >= '2026-09-15 14:50+00' ORDER BY tapped;
SELECT '== the account''s count row today', d.local_date, d.count, d.cap_at_write FROM daily_post_counts d
  JOIN ig_accounts a ON a.id = d.ig_account_id WHERE d.local_date >= current_date - 1 ORDER BY 1;
SELECT '== the floated story''s card now', payload->>'outcome_text' FROM channel_outbox
 WHERE kind = 'approval_prompt' AND intent_id::text LIKE '0395b173%';
