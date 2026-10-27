-- A stored verdict that a past post's file is unsupported counts only while
-- the rules that refused it are the ones the validator runs now. Both overloads
-- of fn_source_asset_suitability take the current matrix version as their last
-- parameter, p_matrix_version, which defaults to NULL. A stored unsupported
-- verdict, in the validation_state column and in the validation capture, counts
-- only when the capture's matrix_version equals the version the caller passes.
-- An older or unstamped verdict leaves the slide a candidate again,
-- validation_pending in strict mode, so its story's preparation fetches and
-- judges the file again. A call that passes no version (NULL), as a call
-- written before the parameter existed does, honours every stored verdict, as
-- before.
-- The catalog's own unsupported_media exclusion is unchanged. The signatures
-- change, so both functions are dropped and made again, the core before the
-- wrapper that calls it, with the grants schema code gives them.
-- The runner wraps this file and its ledger row in one transaction.
-- runner:postcondition SELECT to_regprocedure('public.fn_source_asset_suitability(uuid,uuid,integer,timestamptz,boolean,uuid,uuid,uuid,text)') IS NOT NULL AND to_regprocedure('public.fn_source_asset_suitability(uuid,uuid,integer,timestamptz,boolean,uuid,uuid,uuid,boolean,text)') IS NOT NULL
-- runner:postcondition SELECT to_regprocedure('public.fn_source_asset_suitability(uuid,uuid,integer,timestamptz,boolean,uuid,uuid,uuid)') IS NULL AND to_regprocedure('public.fn_source_asset_suitability(uuid,uuid,integer,timestamptz,boolean,uuid,uuid,uuid,boolean)') IS NULL

SET LOCAL search_path = public, pg_temp;

DROP FUNCTION fn_source_asset_suitability(uuid, uuid, integer, timestamp with time zone, boolean, uuid, uuid, uuid);
DROP FUNCTION fn_source_asset_suitability(uuid, uuid, integer, timestamp with time zone, boolean, uuid, uuid, uuid, boolean);

CREATE FUNCTION fn_source_asset_suitability(
  p_workspace_id uuid, p_destination_id uuid, p_resolved_repost_ttl_days integer,
  p_evaluated_at timestamptz, p_past_posts_enabled boolean,
  p_source_id uuid, p_media_item_id uuid,
  p_existing_intent_id uuid, p_require_validation boolean,
  p_matrix_version text DEFAULT NULL)
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
    p.eligibility, p.ranking, p.destination_scope, p.carousel_slides,
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
  LEFT JOIN instagram_past_posts_generations g ON g.workspace_id=s.workspace_id AND g.source_id=s.id AND g.id=s.current_authorization_generation_id
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
      WHEN p_past_posts_enabled IS NOT TRUE THEN 'past_posts_not_activated'
      WHEN p_evaluated_at IS NULL OR p_resolved_repost_ttl_days IS NULL THEN 'context_unavailable'
      WHEN s.state<>'active' OR COALESCE((s.config->>'removed')::boolean,false) THEN 'source_unavailable'
      WHEN COALESCE(s.ratio>0,false) IS NOT TRUE THEN 'source_off'
      WHEN s.origin_state IS DISTINCT FROM 'active' OR s.origin_ref LIKE 'manual:%' THEN 'origin_unavailable'
      WHEN s.generation_id IS NULL OR s.lifecycle<>'active' THEN 'generation_unavailable'
      WHEN s.credential_state IS DISTINCT FROM 'active' OR s.credential_provider IS DISTINCT FROM 'ig_login'
        OR s.credential_expires_at<=p_evaluated_at THEN 'credential_unavailable'
      WHEN (s.schema_version IS DISTINCT FROM 1 AND s.schema_version IS DISTINCT FROM 2)
        OR s.carousel_slides NOT IN ('all','first') OR s.carousel_slides IS NULL
        OR s.eligibility IS DISTINCT FROM 'supported'
        OR s.ranking IS DISTINCT FROM 'none' OR s.destination_scope IS DISTINCT FROM 'origin_only' THEN 'unsupported_source_policy'
      WHEN i.data_generation_id IS DISTINCT FROM s.generation_id THEN 'awaiting_reobservation'
      WHEN i.exclusion_reason='origin_identity_conflict' OR m.source_projection_redacted IS TRUE THEN 'identity_conflict_or_deleted'
      WHEN i.availability='excluded' AND i.exclusion_reason='unsupported_media' THEN 'unsupported'
      WHEN i.availability IS DISTINCT FROM 'available' THEN 'unavailable'
      WHEN a.exclusion_reason='origin_identity_conflict' THEN 'identity_conflict_or_deleted'
      WHEN a.availability='excluded' AND a.exclusion_reason='unsupported_media' THEN 'unsupported'
      WHEN a.availability IS NOT NULL AND a.availability<>'available' THEN 'unavailable'
      WHEN m.id IS NOT NULL AND m.state<>'available' THEN 'unavailable'
      WHEN s.carousel_slides='first' AND a.position>1 THEN 'slide_not_first'
      WHEN (a.validation_state='unsupported' AND (p_matrix_version IS NULL OR a.validation_capture->>'matrix_version'=p_matrix_version)) OR a.exclusion_reason='unsupported_media' THEN 'unsupported'
      WHEN a.validation_capture->>'generation_id'=s.generation_id::text
        AND a.validation_capture->>'observation_revision'=a.observation_revision::text
        AND a.validation_capture->>'selected_asset_id'=a.asset_media_id
        AND a.validation_capture->>'state'='unsupported'
        AND (p_matrix_version IS NULL OR a.validation_capture->>'matrix_version'=p_matrix_version) THEN 'unsupported'
      WHEN a.validation_capture->>'generation_id'=s.generation_id::text
        AND a.validation_capture->>'observation_revision'=a.observation_revision::text
        AND a.validation_capture->>'selected_asset_id'=a.asset_media_id
        AND a.validation_capture->>'state'='validation_pending'
        AND a.validation_capture->>'reason' IN ('helper_unavailable','asset_unavailable')
        THEN a.validation_capture->>'reason'
      WHEN m.id IS NULL OR m.source_data_generation_id IS DISTINCT FROM a.data_generation_id THEN 'validation_pending'
      WHEN p_require_validation IS DISTINCT FROM false AND (
        a.validation_state IS DISTINCT FROM 'supported'
        OR a.validation_generation_id IS DISTINCT FROM a.data_generation_id
        OR a.validation_observation_revision IS DISTINCT FROM a.observation_revision
        OR a.validation_fingerprint IS NULL OR a.validated_at IS NULL) THEN 'validation_pending'
      WHEN EXISTS (SELECT 1 FROM instagram_intent_provenance b WHERE b.workspace_id=p_workspace_id
        AND b.intent_id=p_existing_intent_id AND (b.source_id IS DISTINCT FROM s.id
          OR b.media_item_id IS DISTINCT FROM m.id OR b.origin_ig_account_id IS DISTINCT FROM s.origin_ig_account_id
          OR b.selected_asset_identity IS DISTINCT FROM a.asset_media_id)) THEN 'selected_asset_changed'
      WHEN p_require_validation IS DISTINCT FROM false AND EXISTS (SELECT 1 FROM instagram_intent_provenance b
        WHERE b.workspace_id=p_workspace_id AND b.intent_id=p_existing_intent_id
        AND (b.capture_generation_id IS DISTINCT FROM a.data_generation_id
          OR b.capture_observation_revision IS DISTINCT FROM a.observation_revision
          OR b.validation_fingerprint IS DISTINCT FROM a.validation_fingerprint)) THEN 'past_posts_revalidation_required'
      WHEN p_evaluated_at-i.original_at < s.effective_age*interval '1 day' THEN 'too_recent'
      WHEN i.attribution='credited' AND s.include_credited IS NOT TRUE THEN 'credited_excluded'
      WHEN i.attribution='unknown' AND s.include_unknown IS NOT TRUE THEN 'attribution_unknown'
      WHEN NOT s.destination_active OR s.origin_ig_account_id<>p_destination_id OR NOT s.assigned THEN 'destination_not_allowed'
    END
  FROM source_facts s JOIN instagram_media_index i ON i.workspace_id=s.workspace_id AND i.source_id=s.id
  LEFT JOIN instagram_media_assets a ON a.workspace_id=i.workspace_id AND a.source_id=i.source_id
    AND a.parent_media_id=i.parent_media_id
  LEFT JOIN media_items m ON m.workspace_id=a.workspace_id AND m.source_id=a.source_id AND m.id=a.media_item_id
  WHERE s.provider='instagram_past_posts' AND (p_media_item_id IS NULL OR m.id=p_media_item_id)
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

CREATE FUNCTION fn_source_asset_suitability(
  p_workspace_id uuid, p_destination_id uuid, p_resolved_repost_ttl_days integer,
  p_evaluated_at timestamptz, p_past_posts_enabled boolean,
  p_source_id uuid DEFAULT NULL, p_media_item_id uuid DEFAULT NULL,
  p_existing_intent_id uuid DEFAULT NULL, p_matrix_version text DEFAULT NULL)
RETURNS TABLE (source_id uuid, media_item_id uuid, provider text,
  suitable boolean, base_suitable boolean, reason text,
  data_generation_id uuid, observation_revision bigint, policy_revision bigint,
  source_use_epoch bigint, effective_minimum_age_days bigint,
  live_intent_conflict boolean, blocking_locks text[], warning_locks text[])
LANGUAGE sql STABLE SECURITY INVOKER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT * FROM fn_source_asset_suitability(p_workspace_id,p_destination_id,
  p_resolved_repost_ttl_days,p_evaluated_at,p_past_posts_enabled,p_source_id,
  p_media_item_id,p_existing_intent_id,true,p_matrix_version)
$$;

REVOKE ALL ON FUNCTION fn_source_asset_suitability(uuid, uuid, integer, timestamp with time zone, boolean, uuid, uuid, uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_source_asset_suitability(uuid, uuid, integer, timestamp with time zone, boolean, uuid, uuid, uuid, text) TO svc_ingress, svc_worker;
REVOKE ALL ON FUNCTION fn_source_asset_suitability(uuid, uuid, integer, timestamp with time zone, boolean, uuid, uuid, uuid, boolean, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_source_asset_suitability(uuid, uuid, integer, timestamp with time zone, boolean, uuid, uuid, uuid, boolean, text) TO svc_ingress, svc_worker;
