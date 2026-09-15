-- Read-only: did 076 land as advertised? (the float, PR #1306)
SELECT '== clock', now();
SELECT '== the two float edges', from_state, to_state FROM post_intent_transitions
 WHERE (from_state, to_state) IN (('publishing','approved'), ('approved','review_required')) ORDER BY 1, 2;
SELECT '== matrix size', count(*) FROM post_intent_transitions;
SELECT '== reaper sweep without its approved leg', prosrc NOT LIKE '%state = ''approved'' AND entered_state_at%' AS approved_leg_gone
  FROM pg_proc WHERE proname = 'fn_reaper_sweep';
SELECT '== the listing door', p.proname, r.rolname AS owner, p.prosecdef AS security_definer
  FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner WHERE p.proname = 'fn_reaper_stale_approved';
SELECT '== its EXECUTE grantees', r.rolname, a.privilege_type
  FROM pg_proc p, aclexplode(p.proacl) a JOIN pg_roles r ON r.oid = a.grantee WHERE p.proname = 'fn_reaper_stale_approved' ORDER BY 2;
SELECT '== no door owner keeps CREATE', NOT EXISTS (SELECT 1 FROM pg_namespace n, aclexplode(n.nspacl) a JOIN pg_roles r ON r.oid = a.grantee
  WHERE n.nspname = 'public' AND r.rolname = 'svc_maintenance' AND a.privilege_type = 'CREATE');
SELECT '== the door lists (should be 0 stale approved, or the rows it would park)', count(*) FROM fn_reaper_stale_approved(interval '72 hours', 50);
SELECT '== approved stories right now: state, step, debit, age', left(id::text, 8), publish_step, cap_consumed_on,
       date_trunc('minute', now() - entered_state_at) AS in_state
  FROM post_intents WHERE state = 'approved' ORDER BY entered_state_at LIMIT 20;
