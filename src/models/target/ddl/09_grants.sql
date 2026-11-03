-- Table, column and function privileges.

GRANT SELECT, INSERT, UPDATE ON users TO svc_ingress, svc_worker;

GRANT SELECT ON users TO svc_maintenance;

GRANT SELECT, INSERT, UPDATE ON user_identities TO svc_ingress, svc_worker;

GRANT SELECT, DELETE ON user_identities TO svc_membership;

GRANT SELECT, INSERT, UPDATE ON workspaces TO svc_ingress, svc_worker;

GRANT SELECT ON workspaces TO svc_clock, svc_membership;

GRANT SELECT, DELETE ON workspaces TO svc_maintenance;

GRANT SELECT, INSERT, UPDATE ON workspace_members TO svc_ingress, svc_worker;

GRANT SELECT, INSERT, DELETE ON workspace_members TO svc_membership;

GRANT SELECT ON workspace_members TO svc_maintenance;

GRANT SELECT, INSERT, UPDATE ON workspace_invitations TO svc_ingress, svc_worker;

GRANT SELECT, UPDATE ON workspace_invitations TO svc_maintenance, svc_membership;

GRANT SELECT, INSERT, UPDATE ON channel_bindings TO svc_ingress, svc_worker;

GRANT SELECT ON channel_bindings TO svc_membership, svc_maintenance;

GRANT SELECT, INSERT, UPDATE ON onboarding_sessions TO svc_ingress, svc_worker;

GRANT SELECT, DELETE ON onboarding_sessions TO svc_maintenance;

GRANT SELECT, INSERT, UPDATE ON ig_accounts TO svc_ingress, svc_worker;

GRANT SELECT ON ig_accounts TO svc_clock, svc_maintenance;

GRANT SELECT, INSERT, UPDATE ON provider_quarantine TO svc_ingress, svc_worker;

GRANT SELECT ON provider_quarantine TO svc_claim;

GRANT SELECT, INSERT, UPDATE ON media_sources TO svc_ingress, svc_worker;

GRANT SELECT ON media_sources TO svc_clock, svc_maintenance;

GRANT SELECT, INSERT, UPDATE ON oauth_credentials TO svc_ingress, svc_worker;

GRANT SELECT, INSERT, UPDATE ON media_items TO svc_ingress, svc_worker;

GRANT SELECT ON media_items TO svc_maintenance;

GRANT SELECT, INSERT, UPDATE ON post_locks TO svc_ingress, svc_worker;

GRANT SELECT, DELETE ON post_locks TO svc_maintenance;

GRANT SELECT, INSERT, UPDATE ON category_post_case_mix TO svc_ingress, svc_worker;

GRANT SELECT, INSERT, UPDATE ON post_intents TO svc_ingress, svc_worker, svc_maintenance;

GRANT SELECT, INSERT ON audit_events TO svc_ingress, svc_worker;

GRANT INSERT ON audit_events TO svc_claim, svc_clock, svc_membership;

GRANT SELECT, INSERT, DELETE ON audit_events TO svc_maintenance;

GRANT SELECT, INSERT, UPDATE ON daily_post_counts TO svc_ingress, svc_worker;

GRANT SELECT, DELETE ON daily_post_counts TO svc_maintenance;

GRANT SELECT ON post_intent_transitions TO svc_ingress, svc_worker, svc_claim, svc_clock, svc_maintenance, svc_membership;

GRANT SELECT, INSERT, UPDATE ON jobs TO svc_ingress, svc_worker;

GRANT SELECT, UPDATE ON jobs TO svc_claim;

GRANT SELECT, INSERT ON jobs TO svc_clock;

GRANT SELECT, INSERT, UPDATE, DELETE ON jobs TO svc_maintenance;

GRANT SELECT, INSERT, UPDATE ON channel_outbox TO svc_ingress, svc_worker;

GRANT SELECT, DELETE ON channel_outbox TO svc_maintenance;

GRANT INSERT ON channel_outbox TO svc_membership;

GRANT SELECT, INSERT, UPDATE ON provider_operations TO svc_ingress, svc_worker;

GRANT SELECT, DELETE ON provider_operations TO svc_maintenance;

GRANT SELECT, INSERT ON command_dedup TO svc_ingress;

GRANT SELECT, DELETE ON command_dedup TO svc_maintenance;

GRANT SELECT, INSERT, UPDATE ON rate_counters TO svc_ingress, svc_worker;

GRANT SELECT, DELETE ON rate_counters TO svc_maintenance;

GRANT SELECT, INSERT, UPDATE ON session_tokens TO svc_ingress;

GRANT SELECT, DELETE ON session_tokens TO svc_maintenance;

GRANT SELECT, INSERT, UPDATE ON oauth_states TO svc_ingress;

GRANT SELECT, DELETE ON oauth_states TO svc_maintenance;

GRANT SELECT, INSERT, UPDATE ON service_tokens TO svc_ingress;

GRANT SELECT, INSERT, UPDATE ON workspace_member_removals TO svc_membership;

GRANT SELECT, INSERT, UPDATE ON signup_admissions TO svc_membership;

GRANT INSERT ON waitlist_entries TO svc_ingress;

GRANT SELECT ON waitlist_entries TO svc_membership;

GRANT SELECT, INSERT, UPDATE ON media_source_destinations TO svc_ingress, svc_worker;

GRANT SELECT, INSERT, UPDATE ON instagram_past_posts_generations TO svc_ingress, svc_worker;

GRANT SELECT, INSERT, UPDATE ON instagram_source_policies TO svc_ingress, svc_worker;

GRANT SELECT, INSERT, UPDATE, DELETE ON instagram_media_index TO svc_ingress, svc_worker;

GRANT SELECT, INSERT, UPDATE ON instagram_item_tombstones TO svc_ingress, svc_worker;

GRANT SELECT, INSERT, UPDATE ON instagram_artifacts TO svc_ingress, svc_worker;

GRANT SELECT, DELETE ON instagram_artifacts TO svc_maintenance;

GRANT SELECT, INSERT, UPDATE ON instagram_intent_provenance TO svc_ingress, svc_worker;

GRANT SELECT, DELETE ON instagram_intent_provenance TO svc_maintenance;

GRANT SELECT, INSERT, UPDATE ON instagram_operation_provenance TO svc_ingress, svc_worker;

GRANT SELECT, DELETE ON instagram_operation_provenance TO svc_maintenance;

GRANT SELECT, INSERT ON oauth_connection_results TO svc_ingress;

GRANT SELECT, INSERT ON meta_identity_mappings TO svc_ingress, svc_worker;

GRANT SELECT ON meta_identity_mappings TO svc_maintenance, svc_clock;

GRANT SELECT, INSERT, UPDATE ON meta_deletion_receipts TO svc_maintenance;

GRANT SELECT ON meta_deletion_receipts TO svc_clock;

GRANT SELECT, INSERT, UPDATE ON meta_deletion_receipt_scopes TO svc_maintenance;

GRANT SELECT ON meta_deletion_receipt_scopes TO svc_clock;

GRANT SELECT, INSERT ON meta_deletion_receipt_generations TO svc_maintenance;

GRANT SELECT ON meta_deletion_receipt_generations TO svc_clock;

GRANT SELECT, INSERT ON meta_deletion_receipt_capabilities TO svc_maintenance;

GRANT SELECT, INSERT, UPDATE, DELETE ON instagram_media_assets TO svc_ingress, svc_worker;

GRANT SELECT (id, state, primary_email) ON users TO svc_membership;

GRANT SELECT (id, source_mix_policy_version, source_mix_conversion_version, source_mix_revision, source_mix_converted_at, source_mix_mode, source_membership_policy) ON workspaces TO svc_claim;

GRANT UPDATE (id) ON workspaces TO svc_claim;

GRANT SELECT (id, source_mix_policy_version, source_mix_conversion_version, source_mix_revision, source_mix_converted_at, source_mix_mode, source_membership_policy) ON workspaces TO svc_clock;

GRANT UPDATE (id) ON workspaces TO svc_clock;

GRANT SELECT (id, source_mix_policy_version, source_mix_conversion_version, source_mix_revision, source_mix_converted_at, source_mix_mode, source_membership_policy) ON workspaces TO svc_maintenance;

GRANT UPDATE (id) ON workspaces TO svc_maintenance;

GRANT UPDATE (next_slot_at, last_reauth_prompt_at, id) ON ig_accounts TO svc_clock;

GRANT SELECT (id, workspace_id, state, provider_account_ref) ON ig_accounts TO svc_claim;

GRANT UPDATE (id) ON ig_accounts TO svc_claim;

GRANT SELECT (id, workspace_id, state, provider_account_ref) ON ig_accounts TO svc_clock;

GRANT SELECT (id, workspace_id, state, provider_account_ref) ON ig_accounts TO svc_maintenance;

GRANT UPDATE (id) ON ig_accounts TO svc_maintenance;

GRANT UPDATE (next_sync_at, id) ON media_sources TO svc_clock;

GRANT SELECT (next_sync_at, id, workspace_id, provider, state, config, origin_ig_account_id, current_authorization_generation_id, source_use_epoch) ON media_sources TO svc_clock;

GRANT UPDATE (next_sync_at, id) ON media_sources TO svc_maintenance;

GRANT SELECT (next_sync_at, id, workspace_id, provider, state, config, origin_ig_account_id, current_authorization_generation_id, source_use_epoch) ON media_sources TO svc_maintenance;

GRANT SELECT (next_sync_at, id, workspace_id, provider, state, config, origin_ig_account_id, current_authorization_generation_id, source_use_epoch) ON media_sources TO svc_claim;

GRANT UPDATE (id) ON media_sources TO svc_claim;

GRANT SELECT (id, workspace_id, ig_account_id, media_source_id, provider, expires_at, next_refresh_at, state) ON oauth_credentials TO svc_clock;

GRANT UPDATE (id, next_refresh_at) ON oauth_credentials TO svc_clock;

GRANT SELECT (id, workspace_id, ig_account_id, provider, expires_at, state) ON oauth_credentials TO svc_claim;

GRANT UPDATE (id) ON oauth_credentials TO svc_claim;

GRANT SELECT (id, workspace_id, ig_account_id, provider, expires_at, state) ON oauth_credentials TO svc_maintenance;

GRANT UPDATE (id) ON oauth_credentials TO svc_maintenance;

GRANT SELECT (workspace_id, source_id, ratio, effective_to) ON category_post_case_mix TO svc_claim;

GRANT SELECT (workspace_id, source_id, ratio, effective_to) ON category_post_case_mix TO svc_clock;

GRANT SELECT (workspace_id, source_id, ratio, effective_to) ON category_post_case_mix TO svc_maintenance;

GRANT UPDATE (state, locked_by, lease_token, locked_until) ON jobs TO svc_clock;

GRANT SELECT (workspace_id, source_id, ig_account_id, state) ON media_source_destinations TO svc_claim;

GRANT SELECT (workspace_id, source_id, ig_account_id, state) ON media_source_destinations TO svc_clock;

GRANT SELECT (workspace_id, source_id, ig_account_id, state) ON media_source_destinations TO svc_maintenance;

GRANT SELECT (id, workspace_id, source_id, origin_ig_account_id, credential_id, lifecycle) ON instagram_past_posts_generations TO svc_claim;

GRANT UPDATE (id) ON instagram_past_posts_generations TO svc_claim;

GRANT SELECT (id, workspace_id, source_id, origin_ig_account_id, credential_id, lifecycle) ON instagram_past_posts_generations TO svc_clock;

GRANT UPDATE (id) ON instagram_past_posts_generations TO svc_clock;

GRANT SELECT (id, workspace_id, source_id, origin_ig_account_id, credential_id, lifecycle, authorization_id) ON instagram_past_posts_generations TO svc_maintenance;

GRANT UPDATE (id) ON instagram_past_posts_generations TO svc_maintenance;

GRANT SELECT (workspace_id, source_id, media_item_id, data_generation_id, observation_revision, first_child_id, parent_media_id, validation_fingerprint, validation_state, validation_generation_id, validation_observation_revision, validated_at) ON instagram_media_index TO svc_maintenance;

GRANT SELECT (workspace_id, intent_id, source_id, media_item_id, origin_ig_account_id, selected_asset_identity, capture_generation_id, capture_observation_revision, validation_fingerprint) ON instagram_intent_provenance TO svc_maintenance;

GRANT UPDATE (is_current) ON meta_identity_mappings TO svc_ingress;

GRANT UPDATE (is_current) ON meta_identity_mappings TO svc_worker;

GRANT UPDATE (state, completed_at, updated_at, review_reason) ON meta_deletion_receipts TO svc_clock;

GRANT UPDATE (state, next_attempt_at, updated_at) ON meta_deletion_receipt_scopes TO svc_clock;

GRANT SELECT (workspace_id, source_id, media_item_id, asset_media_id, data_generation_id, observation_revision, validation_fingerprint, validation_state, validation_generation_id, validation_observation_revision, validated_at) ON instagram_media_assets TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_claim_job(text, text, interval, integer) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_claim_job(text, text, interval, integer) TO svc_worker;

REVOKE ALL ON FUNCTION fn_extend_leases(uuid[], interval) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_extend_leases(uuid[], interval) TO svc_worker;

REVOKE ALL ON FUNCTION fn_reconciler_sweep(integer, interval[], interval) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_reconciler_sweep(integer, interval[], interval) TO svc_worker;

REVOKE ALL ON FUNCTION fn_reaper_sweep(integer, interval, interval) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_reaper_sweep(integer, interval, interval) TO svc_worker;

REVOKE ALL ON FUNCTION fn_retention_batch(text, interval, integer) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_retention_batch(text, interval, integer) TO svc_worker;

REVOKE ALL ON FUNCTION fn_offboard_finalize(uuid, interval) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_offboard_finalize(uuid, interval) TO svc_worker;

REVOKE ALL ON FUNCTION fn_auth_plane_sweep(interval, interval, interval, integer) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_auth_plane_sweep(interval, interval, interval, integer) TO svc_worker;

REVOKE ALL ON FUNCTION fn_invitation_accept(text, uuid, text, text, bigint, text) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_invitation_accept(text, uuid, text, text, bigint, text) TO svc_ingress;

REVOKE ALL ON FUNCTION fn_memberships_for_caller() FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_memberships_for_caller() TO svc_ingress;

REVOKE ALL ON FUNCTION fn_resolve_binding(text, text) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_resolve_binding(text, text) TO svc_ingress;

REVOKE ALL ON FUNCTION fn_group_member_seen(text, text, uuid) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_group_member_seen(text, text, uuid) TO svc_ingress;

REVOKE ALL ON FUNCTION fn_member_remove(uuid, uuid, uuid) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_member_remove(uuid, uuid, uuid) TO svc_ingress;

REVOKE ALL ON FUNCTION fn_reaper_stale_approved(interval, integer) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_reaper_stale_approved(interval, integer) TO svc_worker;

REVOKE ALL ON FUNCTION fn_reaper_parks_late(uuid, uuid) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_reaper_parks_late(uuid, uuid) TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_reaper_late_past_posts(interval, integer) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_reaper_late_past_posts(interval, integer) TO svc_worker;

REVOKE ALL ON FUNCTION fn_health_posting_freshness() FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_health_posting_freshness() TO svc_ingress;

REVOKE ALL ON FUNCTION fn_health_publish_attempts() FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_health_publish_attempts() TO svc_ingress;

REVOKE ALL ON FUNCTION fn_health_destinations() FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_health_destinations() TO svc_ingress;

REVOKE ALL ON FUNCTION fn_health_scheduling_lag() FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_health_scheduling_lag() TO svc_ingress;

REVOKE ALL ON FUNCTION fn_health_ready_lanes() FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_health_ready_lanes() TO svc_ingress, svc_worker;

REVOKE ALL ON FUNCTION fn_health_outbox_pending() FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_health_outbox_pending() TO svc_ingress, svc_worker;

REVOKE ALL ON FUNCTION fn_health_oldest_tenant_wait() FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_health_oldest_tenant_wait() TO svc_ingress, svc_worker;

REVOKE ALL ON FUNCTION fn_health_oldest_tenant_wait_named() FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_health_oldest_tenant_wait_named() TO svc_worker;

REVOKE ALL ON FUNCTION fn_meta_accounts_for_ref(text) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_meta_accounts_for_ref(text) TO svc_ingress;

REVOKE ALL ON FUNCTION fn_sender_sweep(text, integer, numeric, numeric, integer) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_sender_sweep(text, integer, numeric, numeric, integer) TO svc_worker;

REVOKE ALL ON FUNCTION fn_prompts_pending(integer) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_prompts_pending(integer) TO svc_worker;

REVOKE ALL ON FUNCTION fn_settled_cards(text[], integer) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_settled_cards(text[], integer) TO svc_worker;

REVOKE ALL ON FUNCTION fn_stranded_sources(numeric, integer) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_stranded_sources(numeric, integer) TO svc_worker;

REVOKE ALL ON FUNCTION fn_clock_tick(integer, interval, jsonb) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_clock_tick(integer, interval, jsonb) TO svc_worker;

REVOKE ALL ON FUNCTION fn_prompts_due(integer, interval) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_prompts_due(integer, interval) TO svc_worker;

REVOKE ALL ON FUNCTION fn_planned_misses(integer, interval) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_planned_misses(integer, interval) TO svc_worker;

REVOKE ALL ON FUNCTION fn_signup_admitted(text) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_signup_admitted(text) TO svc_ingress;

REVOKE ALL ON FUNCTION fn_identity_unlink(uuid, text) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_identity_unlink(uuid, text) TO svc_ingress;

REVOKE ALL ON FUNCTION fn_health_outbox_failures(integer) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_health_outbox_failures(integer) TO svc_ingress, svc_worker;

REVOKE ALL ON FUNCTION fn_health_outbox_sent(integer) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_health_outbox_sent(integer) TO svc_ingress, svc_worker;

REVOKE ALL ON FUNCTION fn_activation_funnel(timestamp with time zone, interval) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_activation_funnel(timestamp with time zone, interval) TO svc_worker;

REVOKE ALL ON FUNCTION fn_activation_stages(timestamp with time zone) FROM PUBLIC;

REVOKE ALL ON FUNCTION fn_activation_stalled(timestamp with time zone, interval, integer) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_activation_stalled(timestamp with time zone, interval, integer) TO svc_worker;

REVOKE ALL ON FUNCTION fn_invitations_for(uuid, text, integer) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_invitations_for(uuid, text, integer) TO svc_ingress;

REVOKE ALL ON FUNCTION fn_invitation_reissue(uuid, text, text) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_invitation_reissue(uuid, text, text) TO svc_ingress;

REVOKE ALL ON FUNCTION fn_waitlist(integer, text) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_waitlist(integer, text) TO svc_ingress;

REVOKE ALL ON FUNCTION fn_signup_admit(text) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_signup_admit(text) TO svc_ingress;

REVOKE ALL ON FUNCTION fn_signup_link(text, text) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_signup_link(text, text) TO svc_worker;

REVOKE ALL ON FUNCTION fn_signup_claim(text) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_signup_claim(text) TO svc_ingress;

REVOKE ALL ON FUNCTION trg_users_consume_admission() FROM PUBLIC;

REVOKE ALL ON FUNCTION fn_source_asset_suitability(uuid, uuid, integer, timestamp with time zone, boolean, uuid, uuid, uuid) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_source_asset_suitability(uuid, uuid, integer, timestamp with time zone, boolean, uuid, uuid, uuid) TO svc_ingress, svc_worker;

REVOKE ALL ON FUNCTION fn_source_read_authority(uuid, uuid, boolean) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_source_read_authority(uuid, uuid, boolean) TO svc_ingress, svc_worker, svc_claim, svc_clock, svc_maintenance;

REVOKE ALL ON FUNCTION fn_lock_source_read_prefix(uuid) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_lock_source_read_prefix(uuid) TO svc_claim, svc_clock, svc_maintenance;

REVOKE ALL ON FUNCTION fn_source_read_capture(uuid, uuid, boolean) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_source_read_capture(uuid, uuid, boolean) TO svc_claim, svc_clock, svc_maintenance;

REVOKE ALL ON FUNCTION fn_source_job_read_reason(uuid, jsonb, boolean) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_source_job_read_reason(uuid, jsonb, boolean) TO svc_claim, svc_clock, svc_maintenance;

REVOKE ALL ON FUNCTION fn_claim_job(text, text, interval, integer, boolean) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_claim_job(text, text, interval, integer, boolean) TO svc_worker;

REVOKE ALL ON FUNCTION fn_clock_tick(integer, interval, jsonb, boolean) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_clock_tick(integer, interval, jsonb, boolean) TO svc_worker;

REVOKE ALL ON FUNCTION fn_reaper_sweep(integer, interval, interval, boolean, boolean, interval) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_reaper_sweep(integer, interval, interval, boolean, boolean, interval) TO svc_worker;

REVOKE ALL ON FUNCTION trg_past_posts_validation_capture_reset() FROM PUBLIC;

REVOKE ALL ON FUNCTION fn_mix_conversion_jobs(integer, text, text) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_mix_conversion_jobs(integer, text, text) TO svc_worker;

REVOKE ALL ON FUNCTION fn_source_asset_suitability(uuid, uuid, integer, timestamp with time zone, boolean, uuid, uuid, uuid, boolean) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_source_asset_suitability(uuid, uuid, integer, timestamp with time zone, boolean, uuid, uuid, uuid, boolean) TO svc_ingress, svc_worker;

REVOKE ALL ON FUNCTION fn_selected_preparation_due(integer) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_selected_preparation_due(integer) TO svc_worker;

REVOKE ALL ON FUNCTION trg_meta_mapping_immutable() FROM PUBLIC;

REVOKE ALL ON FUNCTION fn_meta_subject_mappings(text, text) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_meta_subject_mappings(text, text) TO svc_ingress;

REVOKE ALL ON FUNCTION fn_meta_deletion_receive(text, text[], integer, text, timestamp with time zone, text, integer) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_meta_deletion_receive(text, text[], integer, text, timestamp with time zone, text, integer) TO svc_ingress;

REVOKE ALL ON FUNCTION fn_meta_deletion_status(text) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_meta_deletion_status(text) TO svc_ingress;

REVOKE ALL ON FUNCTION fn_meta_receipt_scope(uuid) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_meta_receipt_scope(uuid) TO svc_worker;

REVOKE ALL ON FUNCTION fn_meta_receipt_progress(uuid, uuid, uuid, text, jsonb, text, integer) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_meta_receipt_progress(uuid, uuid, uuid, text, jsonb, text, integer) TO svc_worker;

REVOKE ALL ON FUNCTION fn_past_posts_redact_generation_records(uuid, uuid[]) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_past_posts_redact_generation_records(uuid, uuid[]) TO svc_worker;

REVOKE ALL ON FUNCTION fn_meta_receipt_cleanup_jobs(integer) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_meta_receipt_cleanup_jobs(integer) TO svc_worker;

REVOKE ALL ON FUNCTION fn_past_posts_reconcile_jobs(integer, text, text) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_past_posts_reconcile_jobs(integer, text, text) TO svc_worker;

REVOKE ALL ON FUNCTION trg_instagram_media_assets_validation_reset() FROM PUBLIC;
