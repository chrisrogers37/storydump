"""Archive catalog contracts on PostgreSQL, including the real runtime roles."""

import uuid

import psycopg2
import pytest

from tests.scripts.conftest import as_user, seed_workspace_chain, set_test_passwords

pytestmark = [pytest.mark.integration]

ARCHIVE_TABLES = {
    "media_source_destinations",
    "instagram_archive_generations",
    "instagram_source_policies",
    "instagram_media_index",
    "instagram_media_assets",
    "instagram_item_tombstones",
    "instagram_intent_provenance",
    "instagram_artifacts",
    "instagram_operation_provenance",
}


def insert_asset(cur, archive, *, asset="slide-1", position=1, item=None):
    cur.execute(
        "INSERT INTO instagram_media_assets"
        " (workspace_id,source_id,parent_media_id,asset_media_id,data_generation_id,"
        " observation_revision,position,media_kind,media_item_id)"
        " VALUES (%s,%s,%s,%s,%s,1,%s,'image',%s)",
        (
            archive["ws"],
            archive["archive"],
            archive["parent"],
            asset,
            archive["generation"],
            position,
            item,
        ),
    )


def test_asset_link_requires_its_own_tenant_source_generation_and_bundle(
    sync_conn, archive
):
    other = seed_workspace_chain(sync_conn, "asset-other")
    with sync_conn.cursor() as cur:
        foreign = seed_archive(cur, other, parent="foreign-parent")
    sync_conn.commit()
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        with sync_conn.cursor() as cur:
            insert_asset(cur, archive, item=foreign["item"])
        sync_conn.commit()  # the item FK is deliberately deferred
    sync_conn.rollback()
    for wrong_ws, wrong_source in (
        (foreign["ws"], archive["archive"]),  # right source id, wrong tenant
        (archive["ws"], archive["src"]),  # right tenant, wrong source
    ):
        with pytest.raises(psycopg2.errors.ForeignKeyViolation):
            with sync_conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO instagram_media_assets"
                    " (workspace_id,source_id,parent_media_id,asset_media_id,"
                    " data_generation_id,observation_revision,position,media_kind)"
                    " VALUES (%s,%s,%s,'slide-1',%s,1,1,'image')",
                    (wrong_ws, wrong_source, archive["parent"], archive["generation"]),
                )
            sync_conn.commit()
        sync_conn.rollback()
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        with sync_conn.cursor() as cur:
            insert_asset(cur, archive)
            cur.execute(
                "UPDATE instagram_media_assets SET data_generation_id=%s"
                " WHERE workspace_id=%s AND source_id=%s",
                (foreign["generation"], archive["ws"], archive["archive"]),
            )
        sync_conn.commit()
    sync_conn.rollback()
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        with sync_conn.cursor() as cur:
            insert_asset(cur, archive)
            cur.execute(
                "UPDATE instagram_media_assets SET parent_media_id='other-post'"
                " WHERE workspace_id=%s AND source_id=%s",
                (archive["ws"], archive["archive"]),
            )
        sync_conn.commit()
    sync_conn.rollback()
    with sync_conn.cursor() as cur:
        insert_asset(cur, archive, item=archive["item"])
    sync_conn.commit()


def test_asset_rejects_item_from_another_generation_of_the_same_source(
    sync_conn, archive
):
    with sync_conn.cursor() as cur:
        cur.execute(
            "UPDATE instagram_archive_generations SET lifecycle='superseded'"
            " WHERE id=%s",
            (archive["generation"],),
        )
        cur.execute(
            "INSERT INTO instagram_archive_generations"
            " (workspace_id,source_id,origin_ig_account_id,credential_id,"
            " authorization_id,app_id,subject_id,authorized_at)"
            " VALUES (%s,%s,%s,%s,%s,'app','subject',now()) RETURNING id",
            (
                archive["ws"],
                archive["archive"],
                archive["iga"],
                archive["credential"],
                str(uuid.uuid4()),
            ),
        )
        later_generation = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO media_items"
            " (workspace_id,source_id,source_data_generation_id,content_hash,"
            " file_name,media_kind,provider_file_ref)"
            " VALUES (%s,%s,%s,'instagram:later','later.jpg','image','later') RETURNING id",
            (archive["ws"], archive["archive"], later_generation),
        )
        later_item = cur.fetchone()[0]
    sync_conn.commit()
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        with sync_conn.cursor() as cur:
            insert_asset(cur, archive, item=later_item)
        sync_conn.commit()
    sync_conn.rollback()


def test_asset_position_swap_is_deferred_and_duplicate_final_position_refused(
    sync_conn, archive
):
    with sync_conn.cursor() as cur:
        insert_asset(cur, archive, asset="slide-1", position=1)
        insert_asset(cur, archive, asset="slide-2", position=2)
    sync_conn.commit()
    with sync_conn.cursor() as cur:
        cur.execute(
            "UPDATE instagram_media_assets SET position=2 WHERE workspace_id=%s"
            " AND source_id=%s AND asset_media_id='slide-1'",
            (archive["ws"], archive["archive"]),
        )
        cur.execute(
            "SELECT count(*) FROM instagram_media_assets WHERE workspace_id=%s"
            " AND source_id=%s AND position=2",
            (archive["ws"], archive["archive"]),
        )
        assert cur.fetchone()[0] == 2  # the transient duplicate requires deferral
        cur.execute(
            "UPDATE instagram_media_assets SET position=1 WHERE workspace_id=%s"
            " AND source_id=%s AND asset_media_id='slide-2'",
            (archive["ws"], archive["archive"]),
        )
        cur.execute("SET CONSTRAINTS uq_archive_asset_position IMMEDIATE")
    sync_conn.commit()
    with pytest.raises(psycopg2.errors.UniqueViolation):
        with sync_conn.cursor() as cur:
            cur.execute(
                "UPDATE instagram_media_assets SET position=1"
                " WHERE workspace_id=%s AND source_id=%s",
                (archive["ws"], archive["archive"]),
            )
        sync_conn.commit()
    sync_conn.rollback()


def test_one_item_cannot_link_two_assets(sync_conn, archive):
    with sync_conn.cursor() as cur:
        insert_asset(cur, archive, asset="slide-1", position=1, item=archive["item"])
    sync_conn.commit()
    with pytest.raises(psycopg2.errors.UniqueViolation), sync_conn.cursor() as cur:
        insert_asset(cur, archive, asset="slide-2", position=2, item=archive["item"])
    sync_conn.rollback()


def test_unknown_asset_is_retained_only_as_excluded_unsupported_without_item(
    sync_conn, archive
):
    with sync_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO instagram_media_assets"
            " (workspace_id,source_id,parent_media_id,asset_media_id,"
            " data_generation_id,observation_revision,position,media_kind,"
            " availability,exclusion_reason)"
            " VALUES (%s,%s,%s,'unsupported-child',%s,1,2,'unknown',"
            " 'excluded','unsupported_media')",
            (
                archive["ws"],
                archive["archive"],
                archive["parent"],
                archive["generation"],
            ),
        )
        cur.execute(
            "SELECT media_kind,availability,exclusion_reason,media_item_id"
            " FROM instagram_media_assets WHERE workspace_id=%s AND asset_media_id='unsupported-child'",
            (archive["ws"],),
        )
        assert cur.fetchone() == ("unknown", "excluded", "unsupported_media", None)
    sync_conn.commit()
    for assignment, params in (
        ("availability='available'", ()),
        ("exclusion_reason='other'", ()),
        ("media_item_id=%s", (archive["item"],)),
    ):
        with pytest.raises(psycopg2.errors.CheckViolation), sync_conn.cursor() as cur:
            cur.execute(
                f"UPDATE instagram_media_assets SET {assignment}"
                " WHERE workspace_id=%s AND asset_media_id='unsupported-child'",
                (*params, archive["ws"]),
            )
        sync_conn.rollback()


def test_asset_bundle_cascades_and_generation_link_restricts(sync_conn, archive):
    with sync_conn.cursor() as cur:
        insert_asset(cur, archive)
    sync_conn.commit()
    with pytest.raises(psycopg2.errors.ForeignKeyViolation), sync_conn.cursor() as cur:
        cur.execute(
            "DELETE FROM instagram_archive_generations WHERE id=%s",
            (archive["generation"],),
        )
    sync_conn.rollback()
    with sync_conn.cursor() as cur:
        cur.execute(
            "DELETE FROM instagram_media_index WHERE workspace_id=%s AND source_id=%s",
            (archive["ws"], archive["archive"]),
        )
        cur.execute(
            "SELECT count(*) FROM instagram_media_assets WHERE workspace_id=%s",
            (archive["ws"],),
        )
        assert cur.fetchone()[0] == 0
    sync_conn.commit()
    # The generation still has the source and item references; the explicit
    # asset FK is catalogued as RESTRICT, independent of this cascade.
    with sync_conn.cursor() as cur:
        cur.execute(
            "SELECT confdeltype FROM pg_constraint WHERE conname='fk_archive_asset_generation'"
        )
        assert cur.fetchone()[0] == "r"


@pytest.mark.parametrize(
    "capture",
    [
        '{"generation_id":"wrong","observation_revision":1,"selected_asset_id":"slide-1","state":"supported"}',
        '{"generation_id":"GENERATION","observation_revision":2,"selected_asset_id":"slide-1","state":"supported"}',
        '{"generation_id":"GENERATION","observation_revision":1,"selected_asset_id":"slide-2","state":"supported"}',
        '{"generation_id":"GENERATION","observation_revision":1,"selected_asset_id":"slide-1","state":"wrong"}',
    ],
)
def test_asset_capture_is_bound_to_file_generation_revision_and_state(
    sync_conn, archive, capture
):
    with sync_conn.cursor() as cur:
        insert_asset(cur, archive)
    sync_conn.commit()
    capture = capture.replace("GENERATION", str(archive["generation"]))
    with pytest.raises(psycopg2.errors.CheckViolation), sync_conn.cursor() as cur:
        cur.execute(
            "UPDATE instagram_media_assets SET validation_capture=%s::jsonb"
            " WHERE workspace_id=%s AND source_id=%s",
            (capture, archive["ws"], archive["archive"]),
        )
    sync_conn.rollback()


def test_asset_revision_change_clears_validation_and_capture(sync_conn, archive):
    with sync_conn.cursor() as cur:
        insert_asset(cur, archive)
        cur.execute(
            "UPDATE instagram_media_assets SET validation_state='supported',"
            " validation_generation_id=data_generation_id,"
            " validation_observation_revision=observation_revision,"
            " validation_fingerprint='proof',validated_at=now(),"
            " validation_capture=jsonb_build_object('generation_id',data_generation_id::text,"
            " 'observation_revision',observation_revision,'selected_asset_id',asset_media_id,"
            " 'state','supported') WHERE workspace_id=%s",
            (archive["ws"],),
        )
    sync_conn.commit()
    with sync_conn.cursor() as cur:
        cur.execute(
            "UPDATE instagram_media_index SET observation_revision=2 WHERE workspace_id=%s",
            (archive["ws"],),
        )
        cur.execute(
            "UPDATE instagram_media_assets SET observation_revision=2 WHERE workspace_id=%s",
            (archive["ws"],),
        )
        cur.execute(
            "SELECT validation_state,validation_capture,validation_fingerprint"
            " FROM instagram_media_assets WHERE workspace_id=%s",
            (archive["ws"],),
        )
        assert cur.fetchone() == ("pending", None, None)
    sync_conn.commit()


@pytest.mark.parametrize("role", ["svc_worker", "svc_ingress"])
def test_asset_runtime_rls_hides_foreign_and_unset_tenants(
    world_db, sync_conn, admin_conn, archive, role
):
    other = seed_workspace_chain(sync_conn, "asset-rls-other")
    with sync_conn.cursor() as cur:
        foreign = seed_archive(cur, other, parent="foreign-parent")
        insert_asset(cur, archive)
        insert_asset(cur, foreign)
    sync_conn.commit()
    set_test_passwords(admin_conn)
    conn = psycopg2.connect(as_user(world_db, role))
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM instagram_media_assets")
            assert cur.fetchone()[0] == 0
            cur.execute("SET LOCAL app.tenant_id=%s", (str(archive["ws"]),))
            cur.execute("SELECT asset_media_id FROM instagram_media_assets")
            assert cur.fetchall() == [("slide-1",)]
            cur.execute(
                "UPDATE instagram_media_assets SET position=3 WHERE workspace_id=%s",
                (foreign["ws"],),
            )
            assert cur.rowcount == 0
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cur.execute("SET LOCAL app.tenant_id=%s", (str(foreign["ws"]),))
                cur.execute(
                    "INSERT INTO instagram_media_assets"
                    " (workspace_id,source_id,parent_media_id,asset_media_id,"
                    " data_generation_id,observation_revision,position,media_kind)"
                    " VALUES (%s,%s,%s,'new-slide',%s,1,2,'image')",
                    (
                        archive["ws"],
                        archive["archive"],
                        archive["parent"],
                        archive["generation"],
                    ),
                )
    finally:
        conn.close()


def test_policy_carousel_default_and_versions_one_two(sync_conn, archive):
    with sync_conn.cursor() as cur:
        cur.execute(
            "SELECT schema_version,carousel_slides FROM instagram_source_policies"
            " WHERE workspace_id=%s AND source_id=%s",
            (archive["ws"], archive["archive"]),
        )
        assert cur.fetchone() == (1, "all")
        cur.execute(
            "UPDATE instagram_source_policies SET schema_version=2,carousel_slides='first'"
            " WHERE workspace_id=%s AND source_id=%s",
            (archive["ws"], archive["archive"]),
        )
    sync_conn.commit()
    with pytest.raises(psycopg2.errors.CheckViolation), sync_conn.cursor() as cur:
        cur.execute(
            "UPDATE instagram_source_policies SET carousel_slides='middle'"
            " WHERE workspace_id=%s AND source_id=%s",
            (archive["ws"], archive["archive"]),
        )
    sync_conn.rollback()


def test_post_observation_groups_assets_without_changing_its_key(sync_conn, archive):
    with sync_conn.cursor() as cur:
        cur.execute(
            "SELECT slide_count,slides_complete,post_ref FROM instagram_media_index"
            " WHERE workspace_id=%s AND source_id=%s",
            (archive["ws"], archive["archive"]),
        )
        count, complete, post_ref = cur.fetchone()
        assert count is None and complete is True and post_ref is not None
        cur.execute(
            "UPDATE instagram_media_index SET slide_count=20,slides_complete=false"
            " WHERE workspace_id=%s AND source_id=%s",
            (archive["ws"], archive["archive"]),
        )
        cur.execute(
            "SELECT post_ref FROM instagram_media_index"
            " WHERE workspace_id=%s AND source_id=%s",
            (archive["ws"], archive["archive"]),
        )
        assert cur.fetchone()[0] == post_ref
    sync_conn.commit()
    with pytest.raises(psycopg2.errors.CheckViolation), sync_conn.cursor() as cur:
        cur.execute(
            "UPDATE instagram_media_index SET slide_count=21 WHERE workspace_id=%s",
            (archive["ws"],),
        )
    sync_conn.rollback()


def test_selected_preparation_door_uses_asset_identity_and_evidence(sync_conn, archive):
    with sync_conn.cursor() as cur:
        insert_asset(cur, archive, item=archive["item"])
        cur.execute(
            "UPDATE media_sources SET current_authorization_generation_id=%s"
            " WHERE workspace_id=%s AND id=%s",
            (archive["generation"], archive["ws"], archive["archive"]),
        )
        cur.execute(
            "INSERT INTO post_intents (workspace_id,ig_account_id,media_item_id,"
            " provider_account_ref,approval_mode,schedule_slot_at)"
            " VALUES (%s,%s,%s,'synthetic','manual',now()-interval '1 minute') RETURNING id",
            (archive["ws"], archive["iga"], archive["item"]),
        )
        intent = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO instagram_intent_provenance (workspace_id,intent_id,source_id,"
            " media_item_id,origin_ig_account_id,selection_generation_id,"
            " selection_observation_revision,capture_generation_id,"
            " capture_observation_revision,selected_asset_identity,"
            " validation_fingerprint,capture_epoch,last_authorized_policy_revision)"
            " VALUES (%s,%s,%s,%s,%s,%s,1,%s,1,'slide-1','proof',0,1)",
            (
                archive["ws"],
                intent,
                archive["archive"],
                archive["item"],
                archive["iga"],
                archive["generation"],
                archive["generation"],
            ),
        )
    sync_conn.commit()
    with sync_conn.cursor() as cur:
        cur.execute("SELECT intent_id FROM fn_selected_preparation_due(10)")
        assert (intent,) in cur.fetchall()
        cur.execute(
            "UPDATE instagram_media_assets SET validation_state='supported',"
            " validation_generation_id=data_generation_id,"
            " validation_observation_revision=observation_revision,"
            " validation_fingerprint='proof',validated_at=now()"
            " WHERE workspace_id=%s AND source_id=%s",
            (archive["ws"], archive["archive"]),
        )
    sync_conn.commit()
    with sync_conn.cursor() as cur:
        cur.execute("SELECT intent_id FROM fn_selected_preparation_due(10)")
        assert (intent,) not in cur.fetchall()
        cur.execute(
            "UPDATE instagram_intent_provenance SET selected_asset_identity='wrong-slide'"
            " WHERE workspace_id=%s AND intent_id=%s",
            (archive["ws"], intent),
        )
    sync_conn.commit()
    with sync_conn.cursor() as cur:
        cur.execute("SELECT intent_id FROM fn_selected_preparation_due(10)")
        assert (intent,) in cur.fetchall()


def test_archive_contracts_exist_before_any_consumer_is_enabled(sync_conn):
    with sync_conn.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        assert ARCHIVE_TABLES <= {r[0] for r in cur.fetchall()}
        cur.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = 'public' AND table_name = 'media_sources'"
        )
        assert {"origin_ig_account_id", "source_use_epoch"} <= {
            r[0] for r in cur.fetchall()
        }


def seed_archive(cur, chain, *, parent="parent-1"):
    ws, acct = chain["ws"], chain["iga"]
    cur.execute(
        "INSERT INTO media_sources (workspace_id, provider, config, origin_ig_account_id)"
        " VALUES (%s, 'instagram_archive', '{\"v\":1}', %s) RETURNING id",
        (ws, acct),
    )
    source = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO oauth_credentials"
        " (workspace_id, provider, ig_account_id, encrypted_payload)"
        " VALUES (%s, 'ig_login', %s, 'test-ciphertext') RETURNING id",
        (ws, acct),
    )
    credential = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO instagram_archive_generations"
        " (workspace_id, source_id, origin_ig_account_id, credential_id,"
        " authorization_id, app_id, subject_id, authorized_at)"
        " VALUES (%s, %s, %s, %s, %s, 'app', 'subject', now()) RETURNING id",
        (ws, source, acct, credential, str(uuid.uuid4())),
    )
    generation = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO media_source_destinations (workspace_id, source_id, ig_account_id)"
        " VALUES (%s, %s, %s)",
        (ws, source, acct),
    )
    cur.execute(
        "INSERT INTO instagram_source_policies (workspace_id, source_id) VALUES (%s, %s)",
        (ws, source),
    )
    cur.execute(
        "INSERT INTO media_items (workspace_id, source_id, source_data_generation_id,"
        " content_hash, file_name, media_kind, provider_file_ref)"
        " VALUES (%s, %s, %s, %s, 'archive.jpg', 'image', %s) RETURNING id",
        (ws, source, generation, f"instagram:{parent}", parent),
    )
    item = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO instagram_media_index (workspace_id, source_id, parent_media_id,"
        " data_generation_id, media_item_id, original_at)"
        " VALUES (%s, %s, %s, %s, %s, now() - interval '100 days')",
        (ws, source, parent, generation, item),
    )
    return {
        **chain,
        "archive": source,
        "generation": generation,
        "credential": credential,
        "item": item,
        "parent": parent,
    }


@pytest.fixture
def archive(sync_conn):
    chain = seed_workspace_chain(sync_conn, "catalog")
    with sync_conn.cursor() as cur:
        result = seed_archive(cur, chain)
    sync_conn.commit()
    return result


def test_origin_is_required_and_tenant_scoped(sync_conn):
    a = seed_workspace_chain(sync_conn, "ownership-a")
    b = seed_workspace_chain(sync_conn, "ownership-b")
    cases = [
        ("instagram_archive", None, psycopg2.errors.CheckViolation),
        ("gdrive", a["iga"], psycopg2.errors.CheckViolation),
        ("instagram_archive", b["iga"], psycopg2.errors.ForeignKeyViolation),
        ("instagram_archive", str(uuid.uuid4()), psycopg2.errors.ForeignKeyViolation),
    ]
    for provider, origin, refusal in cases:
        with pytest.raises(refusal), sync_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO media_sources (workspace_id, provider, config, origin_ig_account_id)"
                " VALUES (%s, %s, '{\"v\":1}', %s)",
                (a["ws"], provider, origin),
            )
        sync_conn.rollback()


def test_one_archive_per_origin_and_origin_cannot_be_deleted(sync_conn, archive):
    with pytest.raises(psycopg2.errors.UniqueViolation), sync_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO media_sources (workspace_id, provider, config, origin_ig_account_id)"
            " VALUES (%s, 'instagram_archive', '{\"v\":1}', %s)",
            (archive["ws"], archive["iga"]),
        )
    sync_conn.rollback()
    # The source FK is RESTRICT by itself, apart from any intent's reference.
    with sync_conn.cursor() as cur:
        cur.execute(
            "SELECT confdeltype FROM pg_constraint WHERE conname = 'fk_sources_origin'"
        )
        assert cur.fetchone()[0] == "r"


def test_assignments_keep_many_to_many_shape(sync_conn, archive):
    with sync_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ig_accounts (workspace_id, provider_account_ref)"
            " VALUES (%s, 'second-account') RETURNING id",
            (archive["ws"],),
        )
        account = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO media_source_destinations (workspace_id, source_id, ig_account_id)"
            " VALUES (%s, %s, %s), (%s, %s, %s)",
            (
                archive["ws"],
                archive["archive"],
                account,
                archive["ws"],
                archive["src"],
                account,
            ),
        )
    sync_conn.commit()
    with pytest.raises(psycopg2.errors.UniqueViolation), sync_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO media_source_destinations (workspace_id, source_id, ig_account_id)"
            " VALUES (%s, %s, %s)",
            (archive["ws"], archive["archive"], account),
        )
    sync_conn.rollback()


def test_index_cannot_link_a_drive_item_or_another_generation(sync_conn, archive):
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        with sync_conn.cursor() as cur:
            cur.execute(
                "UPDATE instagram_media_index SET media_item_id = %s"
                " WHERE workspace_id = %s AND source_id = %s",
                (archive["media"], archive["ws"], archive["archive"]),
            )
        sync_conn.commit()
    sync_conn.rollback()
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        with sync_conn.cursor() as cur:
            cur.execute(
                "UPDATE media_items SET source_data_generation_id = %s WHERE id = %s",
                (str(uuid.uuid4()), archive["item"]),
            )
        sync_conn.commit()
    sync_conn.rollback()


def test_generation_authorization_identity_cannot_be_rewritten(sync_conn, archive):
    with pytest.raises(psycopg2.errors.CheckViolation), sync_conn.cursor() as cur:
        cur.execute(
            "UPDATE instagram_archive_generations SET subject_id = 'different'"
            " WHERE id = %s",
            (archive["generation"],),
        )
    sync_conn.rollback()


@pytest.mark.parametrize("role", ["svc_worker", "svc_ingress"])
def test_runtime_roles_own_rows_and_deny_missing_or_foreign_tenant(
    world_db, sync_conn, admin_conn, archive, role
):
    other = seed_workspace_chain(sync_conn, "catalog-other")
    set_test_passwords(admin_conn)
    conn = psycopg2.connect(as_user(world_db, role))
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_user")
            assert cur.fetchone()[0] == role
            cur.execute("SELECT count(*) FROM instagram_media_index")
            assert cur.fetchone()[0] == 0
            cur.execute("SET LOCAL app.tenant_id = %s", (str(archive["ws"]),))
            cur.execute("SELECT count(*) FROM instagram_media_index")
            assert cur.fetchone()[0] == 1
            cur.execute(
                "UPDATE instagram_media_index SET observation_revision = observation_revision + 1"
                " WHERE workspace_id = %s",
                (archive["ws"],),
            )
            assert cur.rowcount == 1
            cur.execute("SET LOCAL app.tenant_id = %s", (str(other["ws"]),))
            cur.execute("SELECT count(*) FROM instagram_media_index")
            assert cur.fetchone()[0] == 0
            cur.execute(
                "UPDATE instagram_media_index SET observation_revision = observation_revision + 1"
                " WHERE workspace_id = %s",
                (archive["ws"],),
            )
            assert cur.rowcount == 0
    finally:
        conn.close()


def seed_archive_evidence(cur, chain):
    """Seed a positive row on every RLS/provenance axis."""
    archive = seed_archive(cur, chain)
    cur.execute(
        "INSERT INTO post_intents (workspace_id, ig_account_id, media_item_id,"
        " provider_account_ref, approval_mode, schedule_slot_at)"
        " VALUES (%s, %s, %s, 'archive-account', 'manual', now() + interval '1 year') RETURNING id",
        (archive["ws"], archive["iga"], archive["item"]),
    )
    intent = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO instagram_artifacts (workspace_id, source_id, data_generation_id,"
        " observation_revision, media_item_id, intent_id, kind, capture_epoch,"
        " policy_revision, selected_asset_identity, upload_attempt_id, admitted_at, outcome, validation_fingerprint)"
        " VALUES (%s, %s, %s, 1, %s, %s, 'transit', 0, 1, 'asset-fingerprint',"
        " %s, now(), 'unsettled', 'validation') RETURNING artifact_ref",
        (
            archive["ws"],
            archive["archive"],
            archive["generation"],
            archive["item"],
            intent,
            str(uuid.uuid4()),
        ),
    )
    artifact = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO instagram_intent_provenance (workspace_id, intent_id, source_id,"
        " media_item_id, origin_ig_account_id, selection_generation_id,"
        " selection_observation_revision, capture_generation_id, capture_observation_revision,"
        " selected_asset_identity, capture_epoch, last_authorized_policy_revision)"
        " VALUES (%s, %s, %s, %s, %s, %s, 1, %s, 1, 'asset-fingerprint', 0, 1)",
        (
            archive["ws"],
            intent,
            archive["archive"],
            archive["item"],
            archive["iga"],
            archive["generation"],
            archive["generation"],
        ),
    )
    cur.execute(
        "INSERT INTO instagram_item_tombstones (workspace_id, source_id, media_item_id,"
        " key_version, parent_hmac) VALUES (%s, %s, %s, 1, %s)",
        (archive["ws"], archive["archive"], archive["item"], "0" * 64),
    )
    cur.execute(
        "INSERT INTO provider_operations (workspace_id, intent_id, provider, op_kind,"
        " business_key, lease_token) VALUES (%s, %s, 'ig', 'publish', %s, %s) RETURNING id",
        (archive["ws"], intent, f"archive:{intent}", str(uuid.uuid4())),
    )
    operation = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO instagram_operation_provenance (workspace_id, operation_id, intent_id,"
        " source_id, media_item_id, origin_ig_account_id, data_generation_id,"
        " observation_revision, policy_revision, capture_epoch, selected_asset_identity,"
        " validation_fingerprint, transit_artifact_ref)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, 1, 1, 0, 'asset-fingerprint', 'validation', %s)",
        (
            archive["ws"],
            operation,
            intent,
            archive["archive"],
            archive["item"],
            archive["iga"],
            archive["generation"],
            artifact,
        ),
    )
    return {
        **archive,
        "archive_intent": intent,
        "artifact": artifact,
        "operation": operation,
    }


@pytest.fixture
def evidence(sync_conn):
    chain = seed_workspace_chain(sync_conn, "evidence")
    with sync_conn.cursor() as cur:
        result = seed_archive_evidence(cur, chain)
    sync_conn.commit()
    return result


@pytest.mark.parametrize(
    "table,assignments",
    [
        (
            "instagram_media_index",
            "validation_state = 'supported', validation_fingerprint = 'v', validated_at = now()",
        ),
        ("instagram_artifacts", "outcome = NULL"),
        (
            "instagram_intent_provenance",
            "staged_generation_id = capture_generation_id, transit_artifact_ref = :artifact, validation_fingerprint = 'v'",
        ),
    ],
)
def test_incomplete_evidence_cannot_pass_sql_null_checks(
    sync_conn, evidence, table, assignments
):
    assignments = assignments.replace(
        ":artifact", "'" + str(evidence["artifact"]) + "'"
    )
    with pytest.raises(psycopg2.errors.CheckViolation), sync_conn.cursor() as cur:
        cur.execute(
            f"UPDATE {table} SET {assignments} WHERE workspace_id = %s",
            (evidence["ws"],),
        )
    sync_conn.rollback()


@pytest.mark.parametrize(
    "table,assignment",
    [
        ("instagram_artifacts", "observation_revision = 2"),
        ("instagram_operation_provenance", "policy_revision = 2"),
    ],
)
def test_old_artifacts_and_issued_operations_cannot_be_relabelled(
    sync_conn, evidence, table, assignment
):
    with pytest.raises(psycopg2.errors.CheckViolation), sync_conn.cursor() as cur:
        cur.execute(
            f"UPDATE {table} SET {assignment} WHERE workspace_id = %s",
            (evidence["ws"],),
        )
    sync_conn.rollback()


def test_intent_capture_can_renew_without_rewriting_issued_operation(
    sync_conn, evidence
):
    with sync_conn.cursor() as cur:
        cur.execute(
            "UPDATE instagram_intent_provenance SET capture_observation_revision = 2,"
            " last_authorized_policy_revision = 2 WHERE workspace_id = %s",
            (evidence["ws"],),
        )
        cur.execute(
            "SELECT observation_revision, policy_revision FROM instagram_operation_provenance WHERE workspace_id = %s",
            (evidence["ws"],),
        )
        assert cur.fetchone() == (1, 1)
        cur.execute(
            "SELECT observation_revision FROM instagram_artifacts WHERE workspace_id = %s",
            (evidence["ws"],),
        )
        assert cur.fetchone()[0] == 1


def test_archive_projection_requires_generation_even_before_index_link(
    sync_conn, archive
):
    with pytest.raises(psycopg2.errors.CheckViolation), sync_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO media_items (workspace_id, source_id, content_hash, file_name,"
            " media_kind, provider_file_ref) VALUES (%s, %s, 'orphan', 'orphan.jpg', 'image', 'orphan')",
            (archive["ws"], archive["archive"]),
        )
    sync_conn.rollback()


def test_observation_revision_cannot_go_backwards(sync_conn, archive):
    with sync_conn.cursor() as cur:
        cur.execute(
            "UPDATE instagram_media_index SET observation_revision = 2 WHERE workspace_id = %s",
            (archive["ws"],),
        )
    sync_conn.commit()
    with pytest.raises(psycopg2.errors.CheckViolation), sync_conn.cursor() as cur:
        cur.execute(
            "UPDATE instagram_media_index SET observation_revision = 1 WHERE workspace_id = %s",
            (archive["ws"],),
        )
    sync_conn.rollback()


def test_staged_artifact_must_belong_to_the_same_intent(sync_conn, evidence):
    with sync_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO media_items (workspace_id, source_id, source_data_generation_id,"
            " content_hash, file_name, media_kind, provider_file_ref)"
            " VALUES (%s, %s, %s, 'other-artifact-item', 'other.jpg', 'image', 'other') RETURNING id",
            (evidence["ws"], evidence["archive"], evidence["generation"]),
        )
        other_item = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO post_intents (workspace_id, ig_account_id, media_item_id,"
            " provider_account_ref, approval_mode, schedule_slot_at)"
            " VALUES (%s, %s, %s, 'archive-account', 'manual', now() + interval '2 years') RETURNING id",
            (evidence["ws"], evidence["iga"], other_item),
        )
        other_intent = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO instagram_artifacts (workspace_id, source_id, data_generation_id,"
            " observation_revision, media_item_id, intent_id, kind, capture_epoch,"
            " policy_revision, selected_asset_identity, upload_attempt_id, admitted_at, outcome)"
            " VALUES (%s, %s, %s, 1, %s, %s, 'transit', 0, 1, 'asset', %s, now(), 'unsettled')"
            " RETURNING artifact_ref",
            (
                evidence["ws"],
                evidence["archive"],
                evidence["generation"],
                other_item,
                other_intent,
                str(uuid.uuid4()),
            ),
        )
        other_artifact = cur.fetchone()[0]
    sync_conn.commit()
    with pytest.raises(psycopg2.errors.ForeignKeyViolation), sync_conn.cursor() as cur:
        cur.execute(
            "UPDATE instagram_intent_provenance SET staged_generation_id = capture_generation_id,"
            " staged_observation_revision = 1, validation_fingerprint = 'v', transit_artifact_ref = %s"
            " WHERE workspace_id = %s AND intent_id = %s",
            (other_artifact, evidence["ws"], evidence["archive_intent"]),
        )
    sync_conn.rollback()


def test_index_persists_only_derived_attribution(sync_conn, archive):
    with sync_conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = 'public' AND table_name = 'instagram_media_index'"
        )
        columns = {row[0] for row in cur.fetchall()}
        assert "credited_handles" not in columns
        assert {"attribution", "attribution_detector_version"} <= columns
        cur.execute(
            "UPDATE instagram_media_index SET attribution = 'not_detected'"
            " WHERE workspace_id = %s AND source_id = %s",
            (archive["ws"], archive["archive"]),
        )
    sync_conn.commit()
    with pytest.raises(psycopg2.errors.CheckViolation), sync_conn.cursor() as cur:
        cur.execute(
            "UPDATE instagram_media_index SET attribution = 'no_credit_detected'"
            " WHERE workspace_id = %s AND source_id = %s",
            (archive["ws"], archive["archive"]),
        )
    sync_conn.rollback()


def evidence_artifact(cur, evidence, *, kind="transit", settled_at="NULL"):
    admission = "%s, now(), 'unsettled'" if kind == "transit" else "NULL, NULL, NULL"
    values = [
        evidence["ws"],
        evidence["archive"],
        evidence["generation"],
        evidence["item"],
        evidence["archive_intent"],
        kind,
    ]
    if kind == "transit":
        values.append(str(uuid.uuid4()))
    cur.execute(
        "INSERT INTO instagram_artifacts (workspace_id, source_id, data_generation_id, observation_revision, media_item_id, intent_id, kind, capture_epoch, policy_revision, selected_asset_identity, validation_fingerprint, upload_attempt_id, admitted_at, outcome, settled_at)"
        f" VALUES (%s, %s, %s, 1, %s, %s, %s, 0, 1, 'asset-fingerprint', 'validation', {admission}, {settled_at}) RETURNING artifact_ref",
        values,
    )
    return cur.fetchone()[0]


def link_evidence(
    cur,
    evidence,
    link,
    *,
    artifact=None,
    revision=1,
    asset="asset-fingerprint",
    fingerprint="validation",
    policy_revision=1,
    epoch=0,
):
    artifact = artifact or evidence["artifact"]
    if link == "intent":
        cur.execute(
            "UPDATE instagram_intent_provenance SET staged_generation_id = capture_generation_id, staged_observation_revision = %s, selected_asset_identity = %s, validation_fingerprint = %s, transit_artifact_ref = %s, last_authorized_policy_revision = %s, capture_epoch = %s WHERE workspace_id = %s AND intent_id = %s",
            (
                revision,
                asset,
                fingerprint,
                artifact,
                policy_revision,
                epoch,
                evidence["ws"],
                evidence["archive_intent"],
            ),
        )
    else:
        cur.execute(
            "INSERT INTO provider_operations (workspace_id, intent_id, provider, op_kind, business_key, lease_token) VALUES (%s, %s, 'ig', 'publish', %s, %s) RETURNING id",
            (
                evidence["ws"],
                evidence["archive_intent"],
                str(uuid.uuid4()),
                str(uuid.uuid4()),
            ),
        )
        operation = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO instagram_operation_provenance (workspace_id, operation_id, intent_id, source_id, media_item_id, origin_ig_account_id, data_generation_id, observation_revision, policy_revision, capture_epoch, selected_asset_identity, validation_fingerprint, transit_artifact_ref) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                evidence["ws"],
                operation,
                evidence["archive_intent"],
                evidence["archive"],
                evidence["item"],
                evidence["iga"],
                evidence["generation"],
                revision,
                policy_revision,
                epoch,
                asset,
                fingerprint,
                artifact,
            ),
        )


@pytest.mark.parametrize("link", ["intent", "operation"])
@pytest.mark.parametrize(
    "mismatch",
    [{"revision": 2}, {"asset": "other-asset"}, {"fingerprint": "other-validation"}],
    ids=["revision", "asset", "fingerprint"],
)
def test_artifact_links_require_exact_immutable_evidence(
    sync_conn, evidence, link, mismatch
):
    with pytest.raises(psycopg2.errors.ForeignKeyViolation), sync_conn.cursor() as cur:
        link_evidence(cur, evidence, link, **mismatch)
    sync_conn.rollback()


@pytest.mark.parametrize("link", ["intent", "operation"])
@pytest.mark.parametrize("kind", ["download", "validation", "temporary", "stage"])
def test_artifact_links_require_transit_admission(sync_conn, evidence, link, kind):
    with sync_conn.cursor() as cur:
        artifact = evidence_artifact(cur, evidence, kind=kind)
    sync_conn.commit()
    with pytest.raises(psycopg2.errors.ForeignKeyViolation), sync_conn.cursor() as cur:
        link_evidence(cur, evidence, link, artifact=artifact)
    sync_conn.rollback()


@pytest.mark.parametrize("link", ["intent", "operation"])
def test_matching_artifact_allows_renewed_policy_and_epoch(sync_conn, evidence, link):
    with sync_conn.cursor() as cur:
        link_evidence(cur, evidence, link, policy_revision=9, epoch=7)
        cur.execute(
            "SELECT policy_revision, capture_epoch FROM instagram_artifacts WHERE artifact_ref = %s",
            (evidence["artifact"],),
        )
        assert cur.fetchone() == (1, 0)
    sync_conn.commit()


@pytest.mark.parametrize("kind", ["download", "validation", "temporary", "stage"])
def test_non_transit_artifact_cannot_have_settlement_timestamp(
    sync_conn, evidence, kind
):
    with pytest.raises(psycopg2.errors.CheckViolation), sync_conn.cursor() as cur:
        evidence_artifact(cur, evidence, kind=kind, settled_at="now()")
    sync_conn.rollback()


@pytest.mark.parametrize(
    "missing",
    [
        "validation_generation_id",
        "validation_observation_revision",
        "validation_fingerprint",
        "validated_at",
    ],
)
def test_validated_observation_requires_every_evidence_field(
    sync_conn, archive, missing
):
    fields = {
        "validation_state": "'supported'",
        "validation_generation_id": "data_generation_id",
        "validation_observation_revision": "observation_revision",
        "validation_fingerprint": "'validation'",
        "validated_at": "now()",
    }
    fields[missing] = "NULL"
    with pytest.raises(psycopg2.errors.CheckViolation), sync_conn.cursor() as cur:
        cur.execute(
            "UPDATE instagram_media_index SET "
            + ", ".join(f"{k} = {v}" for k, v in fields.items())
            + " WHERE workspace_id = %s",
            (archive["ws"],),
        )
    sync_conn.rollback()


@pytest.mark.parametrize(
    "missing",
    [
        "staged_generation_id",
        "staged_observation_revision",
        "transit_artifact_ref",
        "validation_fingerprint",
    ],
)
def test_staged_bundle_requires_every_evidence_field(sync_conn, evidence, missing):
    with sync_conn.cursor() as cur:
        link_evidence(cur, evidence, "intent")
    sync_conn.commit()
    with pytest.raises(psycopg2.errors.CheckViolation), sync_conn.cursor() as cur:
        cur.execute(
            f"UPDATE instagram_intent_provenance SET {missing} = NULL WHERE workspace_id = %s",
            (evidence["ws"],),
        )
    sync_conn.rollback()


@pytest.mark.parametrize(
    "missing", ["intent_id", "upload_attempt_id", "admitted_at", "outcome"]
)
def test_transit_admission_requires_every_evidence_field(sync_conn, evidence, missing):
    with pytest.raises(psycopg2.errors.CheckViolation), sync_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO instagram_artifacts (workspace_id, source_id, data_generation_id, observation_revision, media_item_id, intent_id, kind, capture_epoch, policy_revision, selected_asset_identity, validation_fingerprint, upload_attempt_id, admitted_at, outcome)"
            " SELECT workspace_id, source_id, data_generation_id, observation_revision, media_item_id, "
            + ("NULL" if missing == "intent_id" else "intent_id")
            + ", kind, capture_epoch, policy_revision, selected_asset_identity, validation_fingerprint, "
            + ("NULL" if missing == "upload_attempt_id" else "gen_random_uuid()")
            + ", "
            + ("NULL" if missing == "admitted_at" else "admitted_at")
            + ", "
            + ("NULL" if missing == "outcome" else "outcome")
            + " FROM instagram_artifacts WHERE artifact_ref = %s",
            (evidence["artifact"],),
        )
    sync_conn.rollback()


@pytest.mark.parametrize("outcome", [None, "unsettled", "uncertain", "settled"])
@pytest.mark.parametrize("has_timestamp", [False, True])
def test_artifact_settlement_timestamp_matches_outcome_exactly(
    sync_conn, evidence, outcome, has_timestamp
):
    def insert():
        with sync_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ig_accounts(workspace_id,provider_account_ref) VALUES(%s,%s) RETURNING id",
                (evidence["ws"], "artifact-timestamp-" + uuid.uuid4().hex),
            )
            account = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO post_intents(workspace_id,ig_account_id,media_item_id,provider_account_ref,approval_mode,schedule_slot_at)"
                " SELECT workspace_id,%s::uuid,media_item_id,provider_account_ref,approval_mode,now()+interval '2 years'"
                " FROM post_intents WHERE id=%s RETURNING id",
                (account, evidence["archive_intent"]),
            )
            new_intent = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO instagram_artifacts (workspace_id, source_id, data_generation_id, observation_revision, media_item_id, intent_id, kind, capture_epoch, policy_revision, selected_asset_identity, validation_fingerprint, upload_attempt_id, admitted_at, outcome, settled_at)"
                " SELECT workspace_id, source_id, data_generation_id, observation_revision, media_item_id, %s::uuid, %s, capture_epoch, policy_revision, selected_asset_identity, validation_fingerprint, "
                + ("gen_random_uuid(), now()" if outcome is not None else "NULL, NULL")
                + ", %s, "
                + ("now()" if has_timestamp else "NULL")
                + " FROM instagram_artifacts WHERE artifact_ref = %s",
                (
                    new_intent,
                    "transit" if outcome is not None else "download",
                    outcome,
                    evidence["artifact"],
                ),
            )

    if has_timestamp == (outcome == "settled"):
        insert()
        sync_conn.commit()
    else:
        with pytest.raises(psycopg2.errors.CheckViolation):
            insert()
        sync_conn.rollback()
