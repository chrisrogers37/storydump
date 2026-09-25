SELECT '== clock', now();
SELECT '== intents minted or moved after 15:00 UTC: id, media, state, slot, entered, created';
SELECT left(i.id::text, 8), left(i.media_item_id::text, 8), i.state,
       to_char(i.schedule_slot_at AT TIME ZONE 'UTC', 'HH24:MI:SS') AS slot_utc,
       to_char(i.entered_state_at AT TIME ZONE 'UTC', 'HH24:MI:SS') AS entered,
       to_char(i.created_at AT TIME ZONE 'UTC', 'HH24:MI:SS') AS created, i.approval_mode, i.publish_step
  FROM post_intents i WHERE i.created_at >= '2026-09-15 15:00+00' OR i.entered_state_at >= '2026-09-15 15:00+00'
 ORDER BY i.created_at;
SELECT '== every intent on the same media items as those (the whole history of the item)';
SELECT left(i.id::text, 8), left(i.media_item_id::text, 8), i.state,
       to_char(i.schedule_slot_at AT TIME ZONE 'UTC', 'MM-DD HH24:MI') AS slot_utc,
       to_char(i.created_at AT TIME ZONE 'UTC', 'MM-DD HH24:MI:SS') AS created,
       to_char(i.entered_state_at AT TIME ZONE 'UTC', 'MM-DD HH24:MI:SS') AS entered
  FROM post_intents i WHERE i.media_item_id IN (SELECT media_item_id FROM post_intents WHERE created_at >= '2026-09-15 15:00+00')
 ORDER BY i.media_item_id, i.created_at;
SELECT '== the media items: times_posted, last_posted_at';
SELECT left(m.id::text, 8), m.file_name, m.times_posted, to_char(m.last_posted_at AT TIME ZONE 'UTC', 'MM-DD HH24:MI:SS')
  FROM media_items m WHERE m.id IN (SELECT media_item_id FROM post_intents WHERE created_at >= '2026-09-15 15:00+00');
SELECT '== post_locks on those items';
SELECT left(l.media_item_id::text, 8), l.reason, to_char(l.created_at AT TIME ZONE 'UTC', 'MM-DD HH24:MI:SS'), to_char(l.expires_at AT TIME ZONE 'UTC', 'MM-DD HH24:MI')
  FROM post_locks l WHERE l.media_item_id IN (SELECT media_item_id FROM post_intents WHERE created_at >= '2026-09-15 15:00+00');
SELECT '== audit timeline after 15:00 UTC';
SELECT to_char(a.created_at AT TIME ZONE 'UTC', 'HH24:MI:SS'), left(a.entity_id::text, 8), a.from_state, a.to_state, a.actor_kind, left(a.detail::text, 120)
  FROM audit_events a WHERE a.entity_kind = 'post_intent' AND a.created_at >= '2026-09-15 15:00+00' ORDER BY a.created_at;
SELECT '== plan_slot / publish jobs after 15:00 UTC';
SELECT to_char(j.created_at AT TIME ZONE 'UTC', 'HH24:MI:SS'), j.kind, j.state, left(j.payload::text, 140)
  FROM jobs j WHERE j.created_at >= '2026-09-15 15:00+00' AND j.kind IN ('plan_slot', 'publish_pipeline') ORDER BY j.created_at;
SELECT '== cards after 15:00 UTC: intent, kind, state, created, line';
SELECT left(o.intent_id::text, 8), o.kind, o.state, to_char(o.created_at AT TIME ZONE 'UTC', 'HH24:MI:SS'), o.external_message_ref, left(o.payload->>'outcome_text', 60)
  FROM channel_outbox o WHERE o.created_at >= '2026-09-15 15:00+00' ORDER BY o.created_at;
SELECT '== account cursor now', to_char(a.next_slot_at AT TIME ZONE 'UTC', 'HH24:MI:SS'), COALESCE(a.tz, w.tz) FROM ig_accounts a JOIN workspaces w ON w.id = a.workspace_id;
