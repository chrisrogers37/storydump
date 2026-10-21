-- Functions owned by the NOLOGIN door roles. Taking ownership needs CREATE on
-- the schema, held only for the hand-off.

GRANT CREATE ON SCHEMA public TO svc_claim, svc_clock, svc_maintenance, svc_membership;

ALTER FUNCTION fn_claim_job(text, text, interval, integer) OWNER TO svc_claim;

ALTER FUNCTION fn_extend_leases(uuid[], interval) OWNER TO svc_claim;

ALTER FUNCTION fn_reconciler_sweep(integer, interval[], interval) OWNER TO svc_maintenance;

ALTER FUNCTION fn_reaper_sweep(integer, interval, interval) OWNER TO svc_maintenance;

ALTER FUNCTION fn_retention_batch(text, interval, integer) OWNER TO svc_maintenance;

ALTER FUNCTION fn_offboard_finalize(uuid, interval) OWNER TO svc_maintenance;

ALTER FUNCTION fn_auth_plane_sweep(interval, interval, interval, integer) OWNER TO svc_maintenance;

ALTER FUNCTION fn_invitation_accept(text, uuid, text, text, bigint, text) OWNER TO svc_membership;

ALTER FUNCTION fn_memberships_for_caller() OWNER TO svc_membership;

ALTER FUNCTION fn_resolve_binding(text, text) OWNER TO svc_membership;

ALTER FUNCTION fn_group_member_seen(text, text, uuid) OWNER TO svc_membership;

ALTER FUNCTION fn_member_remove(uuid, uuid, uuid) OWNER TO svc_membership;

ALTER FUNCTION fn_reaper_stale_approved(interval, integer) OWNER TO svc_maintenance;

ALTER FUNCTION fn_health_posting_freshness() OWNER TO svc_maintenance;

ALTER FUNCTION fn_health_publish_attempts() OWNER TO svc_maintenance;

ALTER FUNCTION fn_health_destinations() OWNER TO svc_maintenance;

ALTER FUNCTION fn_health_scheduling_lag() OWNER TO svc_maintenance;

ALTER FUNCTION fn_health_ready_lanes() OWNER TO svc_maintenance;

ALTER FUNCTION fn_health_outbox_pending() OWNER TO svc_maintenance;

ALTER FUNCTION fn_health_oldest_tenant_wait() OWNER TO svc_maintenance;

ALTER FUNCTION fn_health_oldest_tenant_wait_named() OWNER TO svc_maintenance;

ALTER FUNCTION fn_meta_accounts_for_ref(text) OWNER TO svc_maintenance;

ALTER FUNCTION fn_sender_sweep(text, integer, numeric, numeric, integer) OWNER TO svc_maintenance;

ALTER FUNCTION fn_prompts_pending(integer) OWNER TO svc_maintenance;

ALTER FUNCTION fn_settled_cards(text[], integer) OWNER TO svc_maintenance;

ALTER FUNCTION fn_stranded_sources(numeric, integer) OWNER TO svc_maintenance;

ALTER FUNCTION fn_clock_tick(integer, interval, jsonb) OWNER TO svc_clock;

ALTER FUNCTION fn_prompts_due(integer, interval) OWNER TO svc_maintenance;

ALTER FUNCTION fn_planned_misses(integer, interval) OWNER TO svc_maintenance;

ALTER FUNCTION fn_signup_admitted(text) OWNER TO svc_membership;

ALTER FUNCTION fn_identity_unlink(uuid, text) OWNER TO svc_membership;

ALTER FUNCTION fn_health_outbox_failures(integer) OWNER TO svc_maintenance;

ALTER FUNCTION fn_health_outbox_sent(integer) OWNER TO svc_maintenance;

ALTER FUNCTION fn_activation_funnel(timestamp with time zone, interval) OWNER TO svc_maintenance;

ALTER FUNCTION fn_activation_stages(timestamp with time zone) OWNER TO svc_maintenance;

ALTER FUNCTION fn_activation_stalled(timestamp with time zone, interval, integer) OWNER TO svc_maintenance;

ALTER FUNCTION fn_invitations_for(uuid, text, integer) OWNER TO svc_membership;

ALTER FUNCTION fn_invitation_reissue(uuid, text, text) OWNER TO svc_membership;

ALTER FUNCTION fn_waitlist(integer, text) OWNER TO svc_membership;

ALTER FUNCTION fn_signup_admit(text) OWNER TO svc_membership;

ALTER FUNCTION fn_signup_link(text, text) OWNER TO svc_membership;

ALTER FUNCTION fn_signup_claim(text) OWNER TO svc_membership;

ALTER FUNCTION trg_users_consume_admission() OWNER TO svc_membership;

ALTER FUNCTION fn_claim_job(text, text, interval, integer, boolean) OWNER TO svc_claim;

ALTER FUNCTION fn_clock_tick(integer, interval, jsonb, boolean) OWNER TO svc_clock;

ALTER FUNCTION fn_reaper_sweep(integer, interval, interval, boolean) OWNER TO svc_maintenance;

ALTER FUNCTION fn_mix_conversion_jobs(integer, text, text) OWNER TO svc_clock;

ALTER FUNCTION fn_selected_preparation_due(integer) OWNER TO svc_maintenance;

ALTER FUNCTION fn_meta_subject_mappings(text, text) OWNER TO svc_maintenance;

ALTER FUNCTION fn_meta_deletion_receive(text, text[], integer, text, timestamp with time zone, text, integer) OWNER TO svc_maintenance;

ALTER FUNCTION fn_meta_deletion_status(text) OWNER TO svc_maintenance;

ALTER FUNCTION fn_meta_receipt_scope(uuid) OWNER TO svc_maintenance;

ALTER FUNCTION fn_meta_receipt_progress(uuid, uuid, uuid, text, jsonb, text, integer) OWNER TO svc_maintenance;

ALTER FUNCTION fn_past_posts_redact_generation_records(uuid, uuid[]) OWNER TO svc_maintenance;

ALTER FUNCTION fn_meta_receipt_cleanup_jobs(integer) OWNER TO svc_clock;

ALTER FUNCTION fn_past_posts_reconcile_jobs(integer, text, text) OWNER TO svc_clock;

REVOKE CREATE ON SCHEMA public FROM svc_claim, svc_clock, svc_maintenance, svc_membership;
