-- Read-only, before the burst.
SELECT '== now', now();
SELECT '== open cards (awaiting approval)', count(*) FROM post_intents WHERE state = 'awaiting_approval';
SELECT '== account: cap/day, tz, next slot, posted today, count row today',
       COALESCE(a.posts_per_day, w.posts_per_day) AS ppd, COALESCE(a.tz, w.tz) AS tz,
       to_char(a.next_slot_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI') AS next_slot_utc,
       (SELECT count(*) FROM post_intents p WHERE p.ig_account_id = a.id AND p.state = 'posted'
          AND (p.entered_state_at AT TIME ZONE COALESCE(a.tz, w.tz))::date = (now() AT TIME ZONE COALESCE(a.tz, w.tz))::date) AS posted_today,
       (SELECT d.count || '/' || d.cap_at_write FROM daily_post_counts d WHERE d.ig_account_id = a.id
          AND d.local_date = (now() AT TIME ZONE COALESCE(a.tz, w.tz))::date) AS bucket_today
  FROM ig_accounts a JOIN workspaces w ON w.id = a.workspace_id;
SELECT '== states right now', state, count(*) FROM post_intents GROUP BY state ORDER BY 2 DESC;
SELECT '== anything mid-flight (publishing, or approved carrying a debit)', count(*) FROM post_intents
 WHERE state = 'publishing' OR (state = 'approved' AND cap_consumed_on IS NOT NULL);
SELECT '== workspace paused?', is_paused FROM workspaces LIMIT 3;
