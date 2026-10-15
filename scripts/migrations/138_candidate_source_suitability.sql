-- One suitability authority: metadata candidates and strict byte readiness.
-- runner:postcondition SELECT NOT prosecdef FROM pg_proc WHERE oid='public.fn_source_asset_suitability(uuid,uuid,integer,timestamptz,boolean,uuid,uuid,uuid,boolean)'::regprocedure
CREATE FUNCTION fn_source_asset_suitability(
  p_workspace_id uuid, p_destination_id uuid, p_resolved_repost_ttl_days integer,
  p_evaluated_at timestamptz, p_archive_enabled boolean,
  p_source_id uuid, p_media_item_id uuid,
  p_existing_intent_id uuid, p_require_validation boolean)
RETURNS TABLE (source_id uuid, media_item_id uuid, provider text,
  suitable boolean, base_suitable boolean, reason text,
  data_generation_id uuid, observation_revision bigint, policy_revision bigint,
  source_use_epoch bigint, effective_minimum_age_days bigint,
  live_intent_conflict boolean, blocking_locks text[], warning_locks text[])
LANGUAGE sql STABLE SECURITY INVOKER
SET search_path = pg_catalog, public, pg_temp AS $$
WITH source_facts AS (
  SELECT s.*, x.ratio, p.schema_version, p.revision AS policy_revision,
    p.minimum_age_days, p.include_credited, p.include_unknown,
    p.eligibility, p.ranking, p.destination_scope,
    g.id AS generation_id, g.lifecycle, c.state AS credential_state,
    c.expires_at AS credential_expires_at, c.provider AS credential_provider,
    a.state AS origin_state, a.provider_account_ref AS origin_ref,
    EXISTS (SELECT 1 FROM ig_accounts d WHERE d.workspace_id=s.workspace_id
      AND d.id=p_destination_id AND (d.state='active' OR (s.provider='gdrive' AND p_require_validation IS FALSE AND d.state='reauth_required'))) AS destination_active,
    EXISTS (SELECT 1 FROM media_source_destinations d WHERE d.workspace_id=s.workspace_id
      AND d.source_id=s.id AND d.ig_account_id=p_destination_id AND d.state='active') AS assigned,
    GREATEST(p.minimum_age_days::bigint,p_resolved_repost_ttl_days::bigint+1) AS effective_age
  FROM media_sources s
  LEFT JOIN category_post_case_mix x ON x.workspace_id=s.workspace_id AND x.source_id=s.id AND x.effective_to IS NULL
  LEFT JOIN instagram_source_policies p ON p.workspace_id=s.workspace_id AND p.source_id=s.id
  LEFT JOIN instagram_archive_generations g ON g.workspace_id=s.workspace_id AND g.source_id=s.id AND g.id=s.current_authorization_generation_id
  LEFT JOIN oauth_credentials c ON c.workspace_id=g.workspace_id AND c.ig_account_id=g.origin_ig_account_id AND c.id=g.credential_id
  LEFT JOIN ig_accounts a ON a.workspace_id=s.workspace_id AND a.id=s.origin_ig_account_id
  WHERE s.workspace_id=p_workspace_id AND (p_source_id IS NULL OR s.id=p_source_id)
), assets AS (
  SELECT s.id AS source_id, m.id AS media_item_id, s.provider, s.source_use_epoch,
    NULL::uuid AS data_generation_id, NULL::bigint AS observation_revision,
    NULL::bigint AS policy_revision, NULL::bigint AS effective_age,
    CASE
      WHEN p_evaluated_at IS NULL OR p_resolved_repost_ttl_days IS NULL THEN 'context_unavailable'
      WHEN s.state='error' OR COALESCE((s.config->>'removed')::boolean,false) THEN 'source_unavailable'
      WHEN s.ratio=0 THEN 'source_off'
      WHEN NOT s.destination_active THEN 'destination_not_allowed'
      WHEN m.state<>'available' THEN 'unavailable'
    END AS base_reason
  FROM source_facts s JOIN media_items m ON m.workspace_id=s.workspace_id AND m.source_id=s.id
  WHERE s.provider='gdrive' AND (p_media_item_id IS NULL OR m.id=p_media_item_id)
  UNION ALL
  SELECT s.id, m.id, s.provider, s.source_use_epoch,
    i.data_generation_id, i.observation_revision, s.policy_revision, s.effective_age,
    CASE
      WHEN p_archive_enabled IS NOT TRUE THEN 'archive_not_activated'
      WHEN p_evaluated_at IS NULL OR p_resolved_repost_ttl_days IS NULL THEN 'context_unavailable'
      WHEN s.state<>'active' OR COALESCE((s.config->>'removed')::boolean,false) THEN 'source_unavailable'
      WHEN COALESCE(s.ratio>0,false) IS NOT TRUE THEN 'source_off'
      WHEN s.origin_state IS DISTINCT FROM 'active' OR s.origin_ref LIKE 'manual:%' THEN 'origin_unavailable'
      WHEN s.generation_id IS NULL OR s.lifecycle<>'active' THEN 'generation_unavailable'
      WHEN s.credential_state IS DISTINCT FROM 'active' OR s.credential_provider IS DISTINCT FROM 'ig_login'
        OR s.credential_expires_at<=p_evaluated_at THEN 'credential_unavailable'
      WHEN s.schema_version IS DISTINCT FROM 1 OR s.eligibility IS DISTINCT FROM 'supported'
        OR s.ranking IS DISTINCT FROM 'none' OR s.destination_scope IS DISTINCT FROM 'origin_only' THEN 'unsupported_source_policy'
      WHEN i.data_generation_id IS DISTINCT FROM s.generation_id THEN 'awaiting_reobservation'
      WHEN i.exclusion_reason='origin_identity_conflict' OR m.source_projection_redacted IS TRUE THEN 'identity_conflict_or_deleted'
      WHEN i.availability='excluded' AND i.exclusion_reason='unsupported_media' THEN 'unsupported'
      WHEN i.availability IS DISTINCT FROM 'available' OR (m.id IS NOT NULL AND m.state<>'available') THEN 'unavailable'
      WHEN i.validation_state='unsupported' OR i.exclusion_reason='unsupported_media' THEN 'unsupported'
      WHEN i.validation_capture->>'generation_id'=s.generation_id::text
        AND i.validation_capture->>'observation_revision'=i.observation_revision::text
        AND i.validation_capture->>'selected_asset_id'=COALESCE(i.first_child_id,i.parent_media_id)
        AND i.validation_capture->>'state'='unsupported' THEN 'unsupported'
      WHEN i.validation_capture->>'generation_id'=s.generation_id::text
        AND i.validation_capture->>'observation_revision'=i.observation_revision::text
        AND i.validation_capture->>'selected_asset_id'=COALESCE(i.first_child_id,i.parent_media_id)
        AND i.validation_capture->>'state'='validation_pending'
        AND i.validation_capture->>'reason' IN ('helper_unavailable','asset_unavailable')
        THEN i.validation_capture->>'reason'
      WHEN m.id IS NULL OR m.source_data_generation_id IS DISTINCT FROM i.data_generation_id THEN 'validation_pending'
      WHEN p_require_validation IS DISTINCT FROM false AND (
        i.validation_state IS DISTINCT FROM 'supported'
        OR i.validation_generation_id IS DISTINCT FROM i.data_generation_id
        OR i.validation_observation_revision IS DISTINCT FROM i.observation_revision
        OR i.validation_fingerprint IS NULL OR i.validated_at IS NULL) THEN 'validation_pending'
      WHEN EXISTS (SELECT 1 FROM instagram_intent_provenance b WHERE b.workspace_id=p_workspace_id
        AND b.intent_id=p_existing_intent_id AND (b.source_id IS DISTINCT FROM s.id
          OR b.media_item_id IS DISTINCT FROM m.id OR b.origin_ig_account_id IS DISTINCT FROM s.origin_ig_account_id
          OR b.selected_asset_identity IS DISTINCT FROM COALESCE(i.first_child_id,i.parent_media_id))) THEN 'selected_asset_changed'
      WHEN p_require_validation IS DISTINCT FROM false AND EXISTS (SELECT 1 FROM instagram_intent_provenance b
        WHERE b.workspace_id=p_workspace_id AND b.intent_id=p_existing_intent_id
        AND (b.capture_generation_id IS DISTINCT FROM i.data_generation_id
          OR b.capture_observation_revision IS DISTINCT FROM i.observation_revision
          OR b.validation_fingerprint IS DISTINCT FROM i.validation_fingerprint)) THEN 'archive_revalidation_required'
      WHEN p_evaluated_at-i.original_at < s.effective_age*interval '1 day' THEN 'too_recent'
      WHEN i.attribution='credited' AND s.include_credited IS NOT TRUE THEN 'credited_excluded'
      WHEN i.attribution='unknown' AND s.include_unknown IS NOT TRUE THEN 'attribution_unknown'
      WHEN NOT s.destination_active OR s.origin_ig_account_id<>p_destination_id OR NOT s.assigned THEN 'destination_not_allowed'
    END
  FROM source_facts s JOIN instagram_media_index i ON i.workspace_id=s.workspace_id AND i.source_id=s.id
  LEFT JOIN media_items m ON m.workspace_id=i.workspace_id AND m.source_id=i.source_id AND m.id=i.media_item_id
  WHERE s.provider='instagram_archive' AND (p_media_item_id IS NULL OR m.id=p_media_item_id)
), context AS (
  SELECT a.*,
    EXISTS (SELECT 1 FROM post_intents p WHERE p.workspace_id=p_workspace_id
      AND p.id=p_existing_intent_id AND p.media_item_id=a.media_item_id
      AND p.ig_account_id=p_destination_id AND p.origin='planned' AND NOT p.cancel_requested
      AND p.state NOT IN ('posted','skipped','rejected','expired','failed','cancelled')) AS planned_warning_override,
    EXISTS (SELECT 1 FROM post_intents p WHERE p.workspace_id=p_workspace_id
      AND p.media_item_id=a.media_item_id AND p.ig_account_id=p_destination_id
      AND p.state NOT IN ('posted','skipped','rejected','expired','failed','cancelled')
      AND (p_existing_intent_id IS NULL OR p.id<>p_existing_intent_id)) AS live_intent_conflict,
    ARRAY(SELECT DISTINCT l.kind FROM post_locks l WHERE l.workspace_id=p_workspace_id
      AND l.media_item_id=a.media_item_id AND (l.ig_account_id IS NULL OR l.ig_account_id=p_destination_id)
      AND (l.expires_at IS NULL OR l.expires_at>p_evaluated_at)
      AND l.kind IN ('reject','unsupported','hold','seasonal') ORDER BY l.kind) AS blocking_locks,
    ARRAY(SELECT DISTINCT l.kind FROM post_locks l WHERE l.workspace_id=p_workspace_id
      AND l.media_item_id=a.media_item_id AND (l.ig_account_id IS NULL OR l.ig_account_id=p_destination_id)
      AND (l.expires_at IS NULL OR l.expires_at>p_evaluated_at)
      AND l.kind IN ('skip','recent') ORDER BY l.kind) AS warning_locks
  FROM assets a
)
SELECT c.source_id,c.media_item_id,c.provider,
  c.base_reason IS NULL AND NOT c.live_intent_conflict AND cardinality(c.blocking_locks)=0 AND (cardinality(c.warning_locks)=0 OR c.planned_warning_override),
  c.base_reason IS NULL,
  COALESCE(c.base_reason,CASE WHEN c.live_intent_conflict THEN 'live_intent'
    WHEN cardinality(c.blocking_locks)>0 OR (cardinality(c.warning_locks)>0 AND NOT c.planned_warning_override) THEN 'locked' END),
  c.data_generation_id,c.observation_revision,c.policy_revision,c.source_use_epoch,c.effective_age,
  c.live_intent_conflict,c.blocking_locks,c.warning_locks
FROM context c
$$;

REVOKE ALL ON FUNCTION fn_source_asset_suitability(uuid,uuid,integer,timestamptz,boolean,uuid,uuid,uuid,boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_source_asset_suitability(uuid,uuid,integer,timestamptz,boolean,uuid,uuid,uuid,boolean) TO svc_ingress,svc_worker;

CREATE OR REPLACE FUNCTION fn_source_asset_suitability(
  p_workspace_id uuid, p_destination_id uuid, p_resolved_repost_ttl_days integer,
  p_evaluated_at timestamptz, p_archive_enabled boolean,
  p_source_id uuid DEFAULT NULL, p_media_item_id uuid DEFAULT NULL,
  p_existing_intent_id uuid DEFAULT NULL)
RETURNS TABLE (source_id uuid, media_item_id uuid, provider text,
  suitable boolean, base_suitable boolean, reason text,
  data_generation_id uuid, observation_revision bigint, policy_revision bigint,
  source_use_epoch bigint, effective_minimum_age_days bigint,
  live_intent_conflict boolean, blocking_locks text[], warning_locks text[])
LANGUAGE sql STABLE SECURITY INVOKER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT * FROM fn_source_asset_suitability(p_workspace_id,p_destination_id,
  p_resolved_repost_ttl_days,p_evaluated_at,p_archive_enabled,p_source_id,
  p_media_item_id,p_existing_intent_id,true)
$$;


-- Read-only selected preparation discovery. Not a policy/admission authority.
-- Matched byte/binding proof needs no preparation work; this is discovery only.
-- Epoch/policy authorization is freshly checked by callback/send admission.
GRANT SELECT (workspace_id,intent_id,source_id,media_item_id,origin_ig_account_id,
 selected_asset_identity,capture_generation_id,capture_observation_revision,
 validation_fingerprint) ON instagram_intent_provenance TO svc_maintenance;
CREATE POLICY p_selected_preparation_read ON instagram_intent_provenance
 FOR SELECT TO svc_maintenance USING (true);
GRANT SELECT (workspace_id,source_id,media_item_id,data_generation_id,
 observation_revision,first_child_id,parent_media_id,validation_fingerprint,
 validation_state,validation_generation_id,validation_observation_revision,
 validated_at) ON instagram_media_index TO svc_maintenance;
CREATE POLICY p_selected_preparation_read ON instagram_media_index
 FOR SELECT TO svc_maintenance USING (true);
CREATE FUNCTION fn_selected_preparation_due(p_limit integer)
RETURNS TABLE(intent_id uuid,workspace_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT i.id,i.workspace_id FROM post_intents i JOIN workspaces w ON w.id=i.workspace_id
 JOIN media_items m ON m.workspace_id=i.workspace_id AND m.id=i.media_item_id
 JOIN media_sources s ON s.workspace_id=m.workspace_id AND s.id=m.source_id
 WHERE s.provider='instagram_archive'
 AND w.state='active' AND NOT w.is_paused AND NOT i.cancel_requested
 AND NOT EXISTS (
 SELECT 1 FROM instagram_intent_provenance p
 JOIN instagram_media_index x ON x.workspace_id=p.workspace_id
   AND x.source_id=p.source_id AND x.media_item_id=p.media_item_id
 JOIN instagram_archive_generations g ON g.workspace_id=s.workspace_id
   AND g.source_id=s.id AND g.id=s.current_authorization_generation_id
 WHERE p.workspace_id=i.workspace_id AND p.intent_id=i.id
 AND p.source_id=s.id AND p.media_item_id=m.id
 AND p.origin_ig_account_id=s.origin_ig_account_id
 AND g.lifecycle='active' AND x.data_generation_id=g.id
 AND m.source_data_generation_id=x.data_generation_id
 AND p.selected_asset_identity=COALESCE(x.first_child_id,x.parent_media_id)
 AND p.capture_generation_id=x.data_generation_id
 AND p.capture_observation_revision=x.observation_revision
 AND p.validation_fingerprint=x.validation_fingerprint
 AND x.validation_state='supported'
 AND x.validation_generation_id=x.data_generation_id
 AND x.validation_observation_revision=x.observation_revision
 AND x.validation_fingerprint IS NOT NULL AND x.validated_at IS NOT NULL)
 AND ((i.state IN ('scheduled','prompt_pending') AND i.schedule_slot_at<=now())
 OR (i.state='awaiting_approval' AND EXISTS(SELECT 1 FROM channel_outbox o
 WHERE o.workspace_id=i.workspace_id AND o.intent_id=i.id AND o.kind='approval_prompt'
 AND o.state IN ('pending','ambiguous'))))
 ORDER BY i.schedule_slot_at,i.id LIMIT p_limit
$$;
GRANT CREATE ON SCHEMA public TO svc_maintenance;
ALTER FUNCTION fn_selected_preparation_due(integer) OWNER TO svc_maintenance;
REVOKE ALL ON FUNCTION fn_selected_preparation_due(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_selected_preparation_due(integer) TO svc_worker;

REVOKE CREATE ON SCHEMA public FROM svc_maintenance;

-- Pending approval deferrals must use wall time, even in older transactions.
CREATE FUNCTION trg_touch_outbox_updated_at() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 NEW.updated_at := clock_timestamp();
 RETURN NEW;
END;
$$;
DROP TRIGGER tg_touch_channel_outbox ON channel_outbox;
CREATE TRIGGER tg_touch_channel_outbox BEFORE UPDATE ON channel_outbox
FOR EACH ROW EXECUTE FUNCTION trg_touch_outbox_updated_at();
