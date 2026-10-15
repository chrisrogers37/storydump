-- Row-level security: enabled on every table, and the policies.

ALTER TABLE users ENABLE ROW LEVEL SECURITY;

ALTER TABLE user_identities ENABLE ROW LEVEL SECURITY;

ALTER TABLE workspaces ENABLE ROW LEVEL SECURITY;

ALTER TABLE workspace_members ENABLE ROW LEVEL SECURITY;

ALTER TABLE workspace_invitations ENABLE ROW LEVEL SECURITY;

ALTER TABLE channel_bindings ENABLE ROW LEVEL SECURITY;

ALTER TABLE onboarding_sessions ENABLE ROW LEVEL SECURITY;

ALTER TABLE ig_accounts ENABLE ROW LEVEL SECURITY;

ALTER TABLE provider_quarantine ENABLE ROW LEVEL SECURITY;

ALTER TABLE media_sources ENABLE ROW LEVEL SECURITY;

ALTER TABLE oauth_credentials ENABLE ROW LEVEL SECURITY;

ALTER TABLE media_items ENABLE ROW LEVEL SECURITY;

ALTER TABLE post_locks ENABLE ROW LEVEL SECURITY;

ALTER TABLE category_post_case_mix ENABLE ROW LEVEL SECURITY;

ALTER TABLE post_intents ENABLE ROW LEVEL SECURITY;

ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY;

ALTER TABLE daily_post_counts ENABLE ROW LEVEL SECURITY;

ALTER TABLE post_intent_transitions ENABLE ROW LEVEL SECURITY;

ALTER TABLE jobs ENABLE ROW LEVEL SECURITY;

ALTER TABLE channel_outbox ENABLE ROW LEVEL SECURITY;

ALTER TABLE provider_operations ENABLE ROW LEVEL SECURITY;

ALTER TABLE command_dedup ENABLE ROW LEVEL SECURITY;

ALTER TABLE rate_counters ENABLE ROW LEVEL SECURITY;

ALTER TABLE session_tokens ENABLE ROW LEVEL SECURITY;

ALTER TABLE oauth_states ENABLE ROW LEVEL SECURITY;

ALTER TABLE service_tokens ENABLE ROW LEVEL SECURITY;

ALTER TABLE workspace_member_removals ENABLE ROW LEVEL SECURITY;

ALTER TABLE signup_admissions ENABLE ROW LEVEL SECURITY;

ALTER TABLE waitlist_entries ENABLE ROW LEVEL SECURITY;

ALTER TABLE media_source_destinations ENABLE ROW LEVEL SECURITY;

ALTER TABLE instagram_archive_generations ENABLE ROW LEVEL SECURITY;

ALTER TABLE instagram_source_policies ENABLE ROW LEVEL SECURITY;

ALTER TABLE instagram_media_index ENABLE ROW LEVEL SECURITY;

ALTER TABLE instagram_item_tombstones ENABLE ROW LEVEL SECURITY;

ALTER TABLE instagram_artifacts ENABLE ROW LEVEL SECURITY;

ALTER TABLE instagram_intent_provenance ENABLE ROW LEVEL SECURITY;

ALTER TABLE instagram_operation_provenance ENABLE ROW LEVEL SECURITY;

ALTER TABLE oauth_connection_results ENABLE ROW LEVEL SECURITY;

ALTER TABLE meta_identity_mappings ENABLE ROW LEVEL SECURITY;

ALTER TABLE meta_deletion_receipts ENABLE ROW LEVEL SECURITY;

ALTER TABLE meta_deletion_receipt_scopes ENABLE ROW LEVEL SECURITY;

ALTER TABLE meta_deletion_receipt_generations ENABLE ROW LEVEL SECURITY;

ALTER TABLE meta_deletion_receipt_capabilities ENABLE ROW LEVEL SECURITY;

ALTER TABLE instagram_media_assets ENABLE ROW LEVEL SECURITY;

CREATE POLICY p_tenant_workspaces ON workspaces FOR ALL TO svc_ingress, svc_worker
  USING      (id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_tenant ON workspace_members FOR ALL TO svc_ingress, svc_worker
  USING      (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_jobs ON jobs FOR ALL TO svc_ingress, svc_worker
  USING      (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
              OR workspace_id IS NULL)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
              OR workspace_id IS NULL);

CREATE POLICY p_user_plane ON users FOR ALL TO svc_ingress, svc_worker
  USING (true) WITH CHECK (true);

CREATE POLICY p_rate ON rate_counters FOR ALL TO svc_ingress, svc_worker
  USING (true) WITH CHECK (true);

CREATE POLICY p_dedup ON command_dedup FOR ALL TO svc_ingress
  USING (true) WITH CHECK (true);

CREATE POLICY p_transitions_read ON post_intent_transitions FOR SELECT
  TO svc_ingress, svc_worker, svc_claim, svc_clock, svc_maintenance, svc_membership
  USING (true);

CREATE POLICY p_audit_ins ON audit_events FOR INSERT TO svc_ingress, svc_worker
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_audit_sel ON audit_events FOR SELECT TO svc_ingress, svc_worker
  USING (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_audit_sys ON audit_events FOR INSERT
  TO svc_claim, svc_clock, svc_maintenance, svc_membership WITH CHECK (true);

CREATE POLICY p_audit_retention_sel ON audit_events FOR SELECT TO svc_maintenance USING (true);

CREATE POLICY p_audit_retention_del ON audit_events FOR DELETE TO svc_maintenance USING (true);

CREATE POLICY p_claim_jobs   ON jobs FOR ALL TO svc_claim USING (true) WITH CHECK (true);

CREATE POLICY p_claim_quar   ON provider_quarantine FOR SELECT TO svc_claim USING (true);

CREATE POLICY p_clock_ws     ON workspaces      FOR SELECT TO svc_clock USING (true);

CREATE POLICY p_clock_acct_s ON ig_accounts     FOR SELECT TO svc_clock USING (true);

CREATE POLICY p_clock_acct_u ON ig_accounts     FOR UPDATE TO svc_clock USING (true) WITH CHECK (true);

CREATE POLICY p_clock_src_s  ON media_sources   FOR SELECT TO svc_clock USING (true);

CREATE POLICY p_clock_src_u  ON media_sources   FOR UPDATE TO svc_clock USING (true) WITH CHECK (true);

CREATE POLICY p_clock_cred_s ON oauth_credentials FOR SELECT TO svc_clock USING (true);

CREATE POLICY p_clock_cred_u ON oauth_credentials FOR UPDATE TO svc_clock USING (true) WITH CHECK (true);

CREATE POLICY p_clock_jobs   ON jobs FOR ALL TO svc_clock USING (true) WITH CHECK (true);

CREATE POLICY p_maint_intents ON post_intents FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);

CREATE POLICY p_maint_locks   ON post_locks   FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);

CREATE POLICY p_maint_invites ON workspace_invitations FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);

CREATE POLICY p_maint_onboard ON onboarding_sessions   FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);

CREATE POLICY p_maint_jobs    ON jobs FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);

CREATE POLICY p_maint_outbox  ON channel_outbox      FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);

CREATE POLICY p_maint_ops     ON provider_operations FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);

CREATE POLICY p_maint_dpc     ON daily_post_counts   FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);

CREATE POLICY p_maint_rate    ON rate_counters       FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);

CREATE POLICY p_maint_ws      ON workspaces FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);

CREATE POLICY p_member_invites ON workspace_invitations FOR ALL TO svc_membership USING (true) WITH CHECK (true);

CREATE POLICY p_member_members ON workspace_members FOR ALL TO svc_membership USING (true) WITH CHECK (true);

CREATE POLICY p_member_binds   ON channel_bindings FOR SELECT TO svc_membership USING (true);

CREATE POLICY p_member_outbox  ON channel_outbox FOR INSERT TO svc_membership WITH CHECK (true);

CREATE POLICY p_member_ws      ON workspaces FOR SELECT TO svc_membership USING (true);

CREATE POLICY p_maint_dedup ON command_dedup FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);

CREATE POLICY p_tenant ON workspace_invitations FOR ALL TO svc_ingress, svc_worker
  USING      (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_tenant ON channel_bindings FOR ALL TO svc_ingress, svc_worker
  USING      (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_tenant ON ig_accounts FOR ALL TO svc_ingress, svc_worker
  USING      (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_tenant ON provider_quarantine FOR ALL TO svc_ingress, svc_worker
  USING      (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_tenant ON media_sources FOR ALL TO svc_ingress, svc_worker
  USING      (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_tenant ON oauth_credentials FOR ALL TO svc_ingress, svc_worker
  USING      (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_tenant ON media_items FOR ALL TO svc_ingress, svc_worker
  USING      (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_tenant ON post_locks FOR ALL TO svc_ingress, svc_worker
  USING      (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_tenant ON category_post_case_mix FOR ALL TO svc_ingress, svc_worker
  USING      (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_tenant ON post_intents FOR ALL TO svc_ingress, svc_worker
  USING      (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_tenant ON daily_post_counts FOR ALL TO svc_ingress, svc_worker
  USING      (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_tenant ON channel_outbox FOR ALL TO svc_ingress, svc_worker
  USING      (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_tenant ON provider_operations FOR ALL TO svc_ingress, svc_worker
  USING      (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_user_plane ON user_identities FOR ALL TO svc_ingress, svc_worker
  USING (true) WITH CHECK (true);

CREATE POLICY p_user_plane ON onboarding_sessions FOR ALL TO svc_ingress, svc_worker
  USING (true) WITH CHECK (true);

CREATE POLICY p_auth_ingress_sessions ON session_tokens FOR ALL TO svc_ingress
  USING (true) WITH CHECK (true);

CREATE POLICY p_auth_ingress_states   ON oauth_states   FOR ALL TO svc_ingress
  USING (true) WITH CHECK (true);

CREATE POLICY p_auth_ingress_svctok   ON service_tokens FOR ALL TO svc_ingress
  USING (true) WITH CHECK (true);

CREATE POLICY p_auth_sweep_sessions ON session_tokens FOR ALL TO svc_maintenance
  USING (true) WITH CHECK (true);

CREATE POLICY p_auth_sweep_states   ON oauth_states   FOR ALL TO svc_maintenance
  USING (true) WITH CHECK (true);

CREATE POLICY p_maint_accts ON ig_accounts FOR SELECT TO svc_maintenance USING (true);

CREATE POLICY p_maint_bindings ON channel_bindings FOR SELECT TO svc_maintenance USING (true);

CREATE POLICY p_maint_media ON media_items FOR SELECT TO svc_maintenance USING (true);

CREATE POLICY p_maint_sources ON media_sources FOR SELECT TO svc_maintenance USING (true);

CREATE POLICY p_maint_sources_rearm ON media_sources FOR UPDATE TO svc_maintenance
  USING (true) WITH CHECK (true);

CREATE POLICY p_member_removals ON workspace_member_removals FOR ALL TO svc_membership
  USING (true) WITH CHECK (true);

CREATE POLICY p_member_admissions ON signup_admissions FOR SELECT TO svc_membership USING (true);

CREATE POLICY p_member_users ON users FOR SELECT TO svc_membership USING (true);

CREATE POLICY p_member_identities ON user_identities FOR ALL TO svc_membership
  USING (true) WITH CHECK (true);

CREATE POLICY p_ingress_waitlist ON waitlist_entries FOR INSERT TO svc_ingress WITH CHECK (true);

CREATE POLICY p_maint_users ON users FOR SELECT TO svc_maintenance USING (true);

CREATE POLICY p_maint_members ON workspace_members FOR SELECT TO svc_maintenance USING (true);

CREATE POLICY p_member_admissions_ins ON signup_admissions FOR INSERT TO svc_membership
  WITH CHECK (true);

CREATE POLICY p_member_admissions_upd ON signup_admissions FOR UPDATE TO svc_membership
  USING (true) WITH CHECK (true);

CREATE POLICY p_member_waitlist ON waitlist_entries FOR SELECT TO svc_membership USING (true);

CREATE POLICY p_tenant ON media_source_destinations FOR ALL TO svc_ingress, svc_worker
  USING (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_tenant ON instagram_archive_generations FOR ALL TO svc_ingress, svc_worker
  USING (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_tenant ON instagram_source_policies FOR ALL TO svc_ingress, svc_worker
  USING (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_tenant ON instagram_media_index FOR ALL TO svc_ingress, svc_worker
  USING (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_tenant ON instagram_item_tombstones FOR ALL TO svc_ingress, svc_worker
  USING (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_tenant ON instagram_artifacts FOR ALL TO svc_ingress, svc_worker
  USING (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_tenant ON instagram_intent_provenance FOR ALL TO svc_ingress, svc_worker
  USING (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_tenant ON instagram_operation_provenance FOR ALL TO svc_ingress, svc_worker
  USING (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_source_read ON workspaces FOR SELECT TO svc_claim,svc_clock,svc_maintenance USING (true);

CREATE POLICY p_source_read_lock ON workspaces FOR UPDATE TO svc_claim,svc_clock,svc_maintenance USING (true) WITH CHECK (true);

CREATE POLICY p_source_read ON ig_accounts FOR SELECT TO svc_claim,svc_clock,svc_maintenance USING (true);

CREATE POLICY p_source_read_lock ON ig_accounts FOR UPDATE TO svc_claim,svc_clock,svc_maintenance USING (true) WITH CHECK (true);

CREATE POLICY p_source_read ON media_sources FOR SELECT TO svc_claim,svc_clock,svc_maintenance USING (true);

CREATE POLICY p_source_read_lock ON media_sources FOR UPDATE TO svc_claim,svc_clock,svc_maintenance USING (true) WITH CHECK (true);

CREATE POLICY p_source_read ON oauth_credentials FOR SELECT TO svc_claim,svc_clock,svc_maintenance USING (true);

CREATE POLICY p_source_read_lock ON oauth_credentials FOR UPDATE TO svc_claim,svc_clock,svc_maintenance USING (true) WITH CHECK (true);

CREATE POLICY p_source_read ON instagram_archive_generations FOR SELECT TO svc_claim,svc_clock,svc_maintenance USING (true);

CREATE POLICY p_source_read_lock ON instagram_archive_generations FOR UPDATE TO svc_claim,svc_clock,svc_maintenance USING (true) WITH CHECK (true);

CREATE POLICY p_source_read ON category_post_case_mix FOR SELECT TO svc_claim,svc_clock,svc_maintenance USING (true);

CREATE POLICY p_source_read ON media_source_destinations FOR SELECT TO svc_claim,svc_clock,svc_maintenance USING (true);

CREATE POLICY p_selected_preparation_read ON instagram_intent_provenance
 FOR SELECT TO svc_maintenance USING (true);

CREATE POLICY p_selected_preparation_read ON instagram_media_index
 FOR SELECT TO svc_maintenance USING (true);

CREATE POLICY p_ingress_connection_results ON oauth_connection_results FOR ALL TO svc_ingress
  USING (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_tenant ON meta_identity_mappings FOR ALL TO svc_ingress, svc_worker
  USING (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY p_maint_meta_mappings ON meta_identity_mappings FOR SELECT TO svc_maintenance USING (true);

CREATE POLICY p_clock_meta_mappings ON meta_identity_mappings FOR SELECT TO svc_clock USING (true);

CREATE POLICY p_maint_meta_receipts ON meta_deletion_receipts FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);

CREATE POLICY p_maint_meta_scopes ON meta_deletion_receipt_scopes FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);

CREATE POLICY p_maint_meta_generations ON meta_deletion_receipt_generations FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);

CREATE POLICY p_maint_meta_capabilities ON meta_deletion_receipt_capabilities FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);

CREATE POLICY p_clock_meta_receipts_s ON meta_deletion_receipts FOR SELECT TO svc_clock USING (true);

CREATE POLICY p_clock_meta_receipts_u ON meta_deletion_receipts FOR UPDATE TO svc_clock USING (true) WITH CHECK (true);

CREATE POLICY p_clock_meta_scopes_s ON meta_deletion_receipt_scopes FOR SELECT TO svc_clock USING (true);

CREATE POLICY p_clock_meta_scopes_u ON meta_deletion_receipt_scopes FOR UPDATE TO svc_clock USING (true) WITH CHECK (true);

CREATE POLICY p_clock_meta_generations ON meta_deletion_receipt_generations FOR SELECT TO svc_clock USING (true);

CREATE POLICY p_maint_redact_read ON instagram_operation_provenance FOR SELECT TO svc_maintenance USING (true);

CREATE POLICY p_maint_redact_delete ON instagram_operation_provenance FOR DELETE TO svc_maintenance USING (true);

CREATE POLICY p_maint_redact_delete ON instagram_intent_provenance FOR DELETE TO svc_maintenance USING (true);

CREATE POLICY p_maint_redact_read ON instagram_artifacts FOR SELECT TO svc_maintenance USING (true);

CREATE POLICY p_maint_redact_delete ON instagram_artifacts FOR DELETE TO svc_maintenance USING (true);

CREATE POLICY p_tenant ON instagram_media_assets FOR ALL TO svc_ingress,svc_worker
 USING (workspace_id=NULLIF(current_setting('app.tenant_id',true),'')::uuid)
 WITH CHECK (workspace_id=NULLIF(current_setting('app.tenant_id',true),'')::uuid);

CREATE POLICY p_selected_preparation_read ON instagram_media_assets
 FOR SELECT TO svc_maintenance USING (true);
