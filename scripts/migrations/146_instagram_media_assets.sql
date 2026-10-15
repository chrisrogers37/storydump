-- 146: one catalog asset per postable Instagram file (#1644, phase 6).
-- runner:postcondition SELECT count(*) = 1 FROM pg_tables WHERE schemaname='public' AND tablename='instagram_media_assets'
-- runner:postcondition SELECT count(*) = 3 FROM information_schema.columns WHERE table_schema='public' AND table_name='instagram_media_index' AND column_name IN ('slide_count','slides_complete','post_ref')
-- runner:postcondition SELECT count(*) = 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='instagram_source_policies' AND column_name='carousel_slides'
-- runner:postcondition SELECT count(*) >= 2 FROM pg_policies WHERE schemaname='public' AND tablename='instagram_media_assets'
-- runner:postcondition SELECT COALESCE(position('instagram_media_assets' in pg_get_functiondef(to_regprocedure('public.fn_selected_preparation_due(integer)'))) > 0, false)
-- runner:postcondition SELECT count(*) = 1 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid WHERE c.relname='instagram_media_assets' AND t.tgname='tg_instagram_media_assets_validation_reset' AND NOT t.tgisinternal

-- [§89 Carousel assets and selected preparation discovery]
ALTER TABLE instagram_source_policies ADD COLUMN carousel_slides TEXT NOT NULL DEFAULT 'all';
ALTER TABLE instagram_source_policies ADD CONSTRAINT ck_archive_policy_carousel_slides CHECK (carousel_slides IN ('all','first'));

ALTER TABLE instagram_media_index ADD COLUMN slide_count INTEGER NULL;
ALTER TABLE instagram_media_index ADD COLUMN slides_complete BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE instagram_media_index ADD COLUMN post_ref UUID NOT NULL DEFAULT gen_random_uuid();
ALTER TABLE instagram_media_index ADD CONSTRAINT ck_archive_slide_count CHECK (slide_count IS NULL OR slide_count BETWEEN 0 AND 20);
ALTER TABLE instagram_media_index ADD CONSTRAINT uq_archive_index_bundle UNIQUE (workspace_id,source_id,parent_media_id,data_generation_id,observation_revision);

CREATE TABLE instagram_media_assets (
 workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
 source_id UUID NOT NULL,
 parent_media_id TEXT NOT NULL,
 asset_media_id TEXT NOT NULL,
 data_generation_id UUID NOT NULL,
 observation_revision BIGINT NOT NULL,
 position INTEGER NOT NULL,
 media_kind TEXT NOT NULL,
 availability TEXT NOT NULL DEFAULT 'available',
 exclusion_reason TEXT NULL,
 media_item_id UUID NULL,
 validation_state TEXT NOT NULL DEFAULT 'pending',
 validation_generation_id UUID NULL,
 validation_observation_revision BIGINT NULL,
 validation_fingerprint TEXT NULL,
 validated_at TIMESTAMPTZ NULL,
 validation_capture JSONB NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY (workspace_id,source_id,asset_media_id),
 CONSTRAINT uq_archive_asset_position UNIQUE (workspace_id,source_id,parent_media_id,position) DEFERRABLE INITIALLY DEFERRED,
 CONSTRAINT uq_archive_asset_item UNIQUE (workspace_id,source_id,media_item_id),
 CONSTRAINT ck_archive_asset_revision CHECK (observation_revision > 0 AND position > 0),
 CONSTRAINT ck_archive_asset_kind CHECK (media_kind IN ('image','video') OR (media_kind='unknown' AND availability='excluded' AND exclusion_reason='unsupported_media' AND media_item_id IS NULL)),
 CONSTRAINT ck_archive_asset_availability CHECK (availability IN ('available','unavailable','excluded')),
 CONSTRAINT ck_archive_asset_validation CHECK (validation_state IN ('pending','supported','unsupported')),
 CONSTRAINT ck_archive_asset_validation_evidence CHECK ((validation_state = 'pending' AND validation_generation_id IS NULL AND validation_observation_revision IS NULL AND validation_fingerprint IS NULL AND validated_at IS NULL) OR (validation_state <> 'pending' AND validation_generation_id IS NOT NULL AND validation_observation_revision IS NOT NULL AND validation_generation_id = data_generation_id AND validation_observation_revision = observation_revision AND validation_fingerprint IS NOT NULL AND validated_at IS NOT NULL)),
 CONSTRAINT ck_archive_asset_validation_capture CHECK (validation_capture IS NULL OR ((jsonb_typeof(validation_capture)='object' AND validation_capture->>'generation_id'=data_generation_id::text AND (validation_capture->>'observation_revision')::bigint=observation_revision AND validation_capture->>'selected_asset_id'=asset_media_id AND validation_capture->>'state' IN ('supported','unsupported','validation_pending')) IS TRUE)),
 CONSTRAINT fk_archive_asset_bundle FOREIGN KEY (workspace_id,source_id,parent_media_id,data_generation_id,observation_revision) REFERENCES instagram_media_index(workspace_id,source_id,parent_media_id,data_generation_id,observation_revision) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED,
 CONSTRAINT fk_archive_asset_generation FOREIGN KEY (workspace_id,source_id,data_generation_id) REFERENCES instagram_archive_generations(workspace_id,source_id,id) ON DELETE RESTRICT,
 CONSTRAINT fk_archive_asset_item FOREIGN KEY (workspace_id,source_id,data_generation_id,media_item_id) REFERENCES media_items(workspace_id,source_id,source_data_generation_id,id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX ix_archive_asset_bundle ON instagram_media_assets (workspace_id,source_id,parent_media_id,position);
CREATE INDEX ix_archive_asset_suitability ON instagram_media_assets (workspace_id,source_id,data_generation_id,media_item_id) WHERE availability='available' AND validation_state='supported';

CREATE FUNCTION trg_instagram_media_assets_validation_reset() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF (NEW.data_generation_id,NEW.observation_revision,NEW.asset_media_id)
 IS DISTINCT FROM (OLD.data_generation_id,OLD.observation_revision,OLD.asset_media_id) THEN
  NEW.validation_capture := NULL;
  NEW.validation_state := 'pending';
  NEW.validation_generation_id := NULL;
  NEW.validation_observation_revision := NULL;
  NEW.validation_fingerprint := NULL;
  NEW.validated_at := NULL;
 END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION trg_instagram_media_assets_validation_reset() FROM PUBLIC;
CREATE TRIGGER tg_instagram_media_assets_validation_reset BEFORE UPDATE ON instagram_media_assets
 FOR EACH ROW EXECUTE FUNCTION trg_instagram_media_assets_validation_reset();
CREATE TRIGGER tg_touch_instagram_media_assets BEFORE UPDATE ON instagram_media_assets
 FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

ALTER TABLE instagram_media_assets ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_tenant ON instagram_media_assets FOR ALL TO svc_ingress,svc_worker
 USING (workspace_id=NULLIF(current_setting('app.tenant_id',true),'')::uuid)
 WITH CHECK (workspace_id=NULLIF(current_setting('app.tenant_id',true),'')::uuid);
GRANT SELECT,INSERT,UPDATE,DELETE ON instagram_media_assets TO svc_ingress,svc_worker;
GRANT SELECT (workspace_id,source_id,media_item_id,asset_media_id,data_generation_id,
 observation_revision,validation_fingerprint,validation_state,
 validation_generation_id,validation_observation_revision,validated_at)
 ON instagram_media_assets TO svc_maintenance;
CREATE POLICY p_selected_preparation_read ON instagram_media_assets
 FOR SELECT TO svc_maintenance USING (true);

CREATE OR REPLACE FUNCTION fn_selected_preparation_due(p_limit integer)
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
 JOIN instagram_media_assets a ON a.workspace_id=p.workspace_id
   AND a.source_id=p.source_id AND a.media_item_id=p.media_item_id
 JOIN instagram_archive_generations g ON g.workspace_id=s.workspace_id
   AND g.source_id=s.id AND g.id=s.current_authorization_generation_id
 WHERE p.workspace_id=i.workspace_id AND p.intent_id=i.id
 AND p.source_id=s.id AND p.media_item_id=m.id
 AND p.origin_ig_account_id=s.origin_ig_account_id
 AND g.lifecycle='active' AND a.data_generation_id=g.id
 AND m.source_data_generation_id=a.data_generation_id
 AND p.selected_asset_identity=a.asset_media_id
 AND p.capture_generation_id=a.data_generation_id
 AND p.capture_observation_revision=a.observation_revision
 AND p.validation_fingerprint=a.validation_fingerprint
 AND a.validation_state='supported'
 AND a.validation_generation_id=a.data_generation_id
 AND a.validation_observation_revision=a.observation_revision
 AND a.validation_fingerprint IS NOT NULL AND a.validated_at IS NOT NULL)
 AND ((i.state IN ('scheduled','prompt_pending') AND i.schedule_slot_at<=now())
 OR (i.state='awaiting_approval' AND EXISTS(SELECT 1 FROM channel_outbox o
 WHERE o.workspace_id=i.workspace_id AND o.intent_id=i.id AND o.kind='approval_prompt'
 AND o.state IN ('pending','ambiguous'))))
 ORDER BY i.schedule_slot_at,i.id LIMIT p_limit
$$;
GRANT CREATE ON SCHEMA public TO svc_maintenance;
ALTER FUNCTION fn_selected_preparation_due(integer) OWNER TO svc_maintenance;
REVOKE CREATE ON SCHEMA public FROM svc_maintenance;
REVOKE ALL ON FUNCTION fn_selected_preparation_due(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_selected_preparation_due(integer) TO svc_worker;
