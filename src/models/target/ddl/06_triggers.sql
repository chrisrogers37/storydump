-- Triggers.

CREATE TRIGGER tg_touch_users BEFORE UPDATE ON users
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

CREATE TRIGGER tg_touch_user_identities BEFORE UPDATE ON user_identities
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

CREATE TRIGGER tg_touch_workspaces BEFORE UPDATE ON workspaces
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

CREATE TRIGGER tg_touch_workspace_members BEFORE UPDATE ON workspace_members
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

CREATE CONSTRAINT TRIGGER ct_members_owner_exists
  AFTER UPDATE OR DELETE ON workspace_members
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
  WHEN (OLD.role = 'owner')
  EXECUTE FUNCTION trg_members_owner_exists();

CREATE CONSTRAINT TRIGGER ct_workspaces_owner_at_insert
  AFTER INSERT ON workspaces
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
  EXECUTE FUNCTION trg_workspaces_owner_at_insert();

CREATE TRIGGER tg_touch_workspace_invitations BEFORE UPDATE ON workspace_invitations
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

CREATE TRIGGER tg_touch_channel_bindings BEFORE UPDATE ON channel_bindings
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

CREATE TRIGGER tg_touch_onboarding_sessions BEFORE UPDATE ON onboarding_sessions
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

CREATE TRIGGER tg_touch_ig_accounts BEFORE UPDATE ON ig_accounts
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

CREATE TRIGGER tg_touch_provider_quarantine BEFORE UPDATE ON provider_quarantine
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

CREATE TRIGGER tg_touch_media_sources BEFORE UPDATE ON media_sources
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

CREATE TRIGGER tg_touch_oauth_credentials BEFORE UPDATE ON oauth_credentials
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

CREATE TRIGGER tg_touch_media_items BEFORE UPDATE ON media_items
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

CREATE TRIGGER tg_touch_post_locks BEFORE UPDATE ON post_locks
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

CREATE TRIGGER tg_touch_category_post_case_mix BEFORE UPDATE ON category_post_case_mix
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

CREATE TRIGGER tg_touch_post_intents BEFORE UPDATE ON post_intents
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

CREATE TRIGGER tg_touch_daily_post_counts BEFORE UPDATE ON daily_post_counts
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

CREATE TRIGGER tg_intent_guard BEFORE UPDATE ON post_intents
  FOR EACH ROW EXECUTE FUNCTION trg_intent_guard();

CREATE TRIGGER tg_intent_audit AFTER UPDATE ON post_intents
  FOR EACH ROW
  WHEN (OLD.state IS DISTINCT FROM NEW.state)
  EXECUTE FUNCTION trg_intent_audit();

CREATE TRIGGER tg_intent_insert_guard BEFORE INSERT ON post_intents
  FOR EACH ROW EXECUTE FUNCTION trg_intent_insert_guard();

CREATE TRIGGER tg_audit_workspaces        AFTER INSERT OR UPDATE OR DELETE ON workspaces
  FOR EACH ROW EXECUTE FUNCTION trg_governance_audit();

CREATE TRIGGER tg_audit_workspace_members AFTER INSERT OR UPDATE OR DELETE ON workspace_members
  FOR EACH ROW EXECUTE FUNCTION trg_governance_audit();

CREATE TRIGGER tg_audit_oauth_credentials AFTER INSERT OR UPDATE OR DELETE ON oauth_credentials
  FOR EACH ROW EXECUTE FUNCTION trg_governance_audit();

CREATE TRIGGER tg_audit_ig_accounts       AFTER INSERT OR UPDATE OR DELETE ON ig_accounts
  FOR EACH ROW EXECUTE FUNCTION trg_governance_audit();

CREATE TRIGGER tg_audit_channel_bindings  AFTER INSERT OR UPDATE OR DELETE ON channel_bindings
  FOR EACH ROW EXECUTE FUNCTION trg_governance_audit();

CREATE TRIGGER tg_touch_jobs BEFORE UPDATE ON jobs
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

CREATE TRIGGER tg_touch_provider_operations BEFORE UPDATE ON provider_operations
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

CREATE TRIGGER tg_touch_session_tokens BEFORE UPDATE ON session_tokens
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

CREATE TRIGGER tg_touch_oauth_states BEFORE UPDATE ON oauth_states
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

CREATE TRIGGER tg_touch_service_tokens BEFORE UPDATE ON service_tokens
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

CREATE TRIGGER tg_intent_no_self_transition BEFORE UPDATE OF state ON post_intents
  FOR EACH ROW WHEN (NEW.state IS NOT DISTINCT FROM OLD.state)
  EXECUTE FUNCTION trg_intent_no_self_transition();

CREATE TRIGGER tg_intent_person_or_rule BEFORE UPDATE OF state, origin, approval_rule ON post_intents
  FOR EACH ROW
  WHEN (NEW.origin IS DISTINCT FROM OLD.origin
        OR NEW.approval_rule IS DISTINCT FROM OLD.approval_rule
        OR (OLD.state = 'awaiting_approval' AND NEW.state = 'approved'))
  EXECUTE FUNCTION trg_intent_person_or_rule();

CREATE TRIGGER tg_intent_repeat AFTER UPDATE OF state ON post_intents
  FOR EACH ROW
  WHEN (NEW.repeat_unit IS NOT NULL AND OLD.state IS DISTINCT FROM NEW.state
        AND NEW.state IN ('posted','skipped','expired','failed','cancelled'))
  EXECUTE FUNCTION trg_intent_repeat();

CREATE TRIGGER tg_users_consume_admission AFTER INSERT ON users
  FOR EACH ROW WHEN (NEW.primary_email IS NOT NULL)
  EXECUTE FUNCTION trg_users_consume_admission();

CREATE TRIGGER tg_archive_generation_immutable BEFORE UPDATE ON instagram_archive_generations FOR EACH ROW EXECUTE FUNCTION trg_archive_generation_immutable();

CREATE TRIGGER tg_archive_artifact_immutable BEFORE UPDATE ON instagram_artifacts FOR EACH ROW EXECUTE FUNCTION trg_archive_artifact_immutable();

CREATE TRIGGER tg_archive_operation_immutable BEFORE UPDATE ON instagram_operation_provenance FOR EACH ROW EXECUTE FUNCTION trg_archive_operation_immutable();

CREATE TRIGGER tg_touch_media_source_destinations BEFORE UPDATE ON media_source_destinations FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

CREATE TRIGGER tg_touch_instagram_archive_generations BEFORE UPDATE ON instagram_archive_generations FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

CREATE TRIGGER tg_touch_instagram_source_policies BEFORE UPDATE ON instagram_source_policies FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

CREATE TRIGGER tg_touch_instagram_media_index BEFORE UPDATE ON instagram_media_index FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

CREATE TRIGGER tg_touch_instagram_artifacts BEFORE UPDATE ON instagram_artifacts FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

CREATE TRIGGER tg_touch_instagram_intent_provenance BEFORE UPDATE ON instagram_intent_provenance FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

CREATE TRIGGER tg_archive_media_generation BEFORE INSERT OR UPDATE OF source_id, source_data_generation_id, source_projection_redacted ON media_items FOR EACH ROW EXECUTE FUNCTION trg_archive_media_generation();

CREATE TRIGGER tg_archive_observation_revision BEFORE UPDATE ON instagram_media_index FOR EACH ROW EXECUTE FUNCTION trg_archive_observation_revision();

CREATE TRIGGER archive_validation_capture_reset BEFORE UPDATE ON instagram_media_index
FOR EACH ROW EXECUTE FUNCTION trg_archive_validation_capture_reset();

CREATE TRIGGER tg_touch_channel_outbox BEFORE UPDATE ON channel_outbox
FOR EACH ROW EXECUTE FUNCTION trg_touch_outbox_updated_at();

CREATE TRIGGER tg_meta_mapping_immutable BEFORE UPDATE ON meta_identity_mappings FOR EACH ROW EXECUTE FUNCTION trg_meta_mapping_immutable();

CREATE TRIGGER tg_touch_meta_deletion_receipts BEFORE UPDATE ON meta_deletion_receipts FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

CREATE TRIGGER tg_touch_meta_deletion_receipt_scopes BEFORE UPDATE ON meta_deletion_receipt_scopes FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

CREATE TRIGGER tg_instagram_media_assets_validation_reset BEFORE UPDATE ON instagram_media_assets
 FOR EACH ROW EXECUTE FUNCTION trg_instagram_media_assets_validation_reset();

CREATE TRIGGER tg_touch_instagram_media_assets BEFORE UPDATE ON instagram_media_assets
 FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();
