"""Archive lifecycle cleanup, job kinds and receipt purge completion: the redaction door, the finalize
transit guard with the offboarding cascade over archive rows, and the two
job-minting doors, driven as the real runtime logins on a fresh lineage
database per test (the minting doors read the whole estate).
"""

import secrets
import uuid
from datetime import timedelta

import psycopg2
import psycopg2.errors
import pytest

from tests.scripts import test_meta_receipt_doors_gate as receipts
from tests.scripts.conftest import seed_workspace_chain
from tests.scripts.test_instagram_catalog_gate import seed_archive_evidence

pytestmark = [pytest.mark.integration]
db = receipts.db
run, owner, REFUSED, T0 = receipts.run, receipts.owner, receipts.REFUSED, receipts.T0


def evidence_world(db):
    """An archive with a positive row on every provenance axis: a scheduled
    intent captured from the generation, its unsettled transit artifact, the
    intent and operation provenance, and a permitted publish operation."""
    conn = psycopg2.connect(db["owner"])
    try:
        chain = seed_workspace_chain(conn, f"life-{uuid.uuid4().hex[:8]}")
        with conn.cursor() as cur:
            w = seed_archive_evidence(cur, chain)
            cur.execute(
                "SELECT authorization_id FROM instagram_archive_generations WHERE id = %s",
                (w["generation"],),
            )
            w["authorization"] = cur.fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    return w


def redact(db, w, generations, *, tenant="own", source=None):
    return run(
        db["worker"],
        "SELECT o_operation_rows, o_intent_rows, o_artifact_rows"
        " FROM fn_archive_redact_generation_records(%s, %s::uuid[])",
        (source or w["archive"], list(generations)),
        tenant=w["ws"] if tenant == "own" else tenant,
    )


def count(db, table, where, params):
    return owner(db, f"SELECT count(*) FROM {table} WHERE {where}", params)[0][0]


def settle_transit(db, w, *, destroyed):
    owner(
        db,
        "UPDATE instagram_artifacts SET outcome = 'settled', settled_at = now(),"
        " cleanup_state = %s WHERE artifact_ref = %s",
        ("destroyed" if destroyed else "pending", w["artifact"]),
    )


# --- archive lifecycle cleanup: the redaction door -------------------------------------------------


def test_redaction_refuses_by_name_until_nothing_can_use_the_records(db):
    w = evidence_world(db)
    g = [w["generation"]]
    for args in (
        dict(tenant=None),  # no tenant
        dict(generations=[]),  # an empty scope
        dict(),  # the generation is still active
    ):
        with pytest.raises(
            REFUSED, match="archive redaction refused: scope_unavailable"
        ):
            redact(db, w, args.pop("generations", g), **args)
    owner(
        db,
        "UPDATE instagram_archive_generations SET lifecycle = 'deletion_pending' WHERE id = %s",
        (w["generation"],),
    )
    for args in (
        dict(source=str(uuid.uuid4())),  # not this source's generation
        dict(generations=g + [str(uuid.uuid4())]),  # one id nobody owns
        dict(tenant=receipts.world(db)["ws"]),  # another tenant
    ):
        with pytest.raises(REFUSED, match="scope_unavailable"):
            redact(db, w, args.pop("generations", g), **args)

    for state in ("permitted", "ambiguous"):
        owner(
            db,
            "UPDATE provider_operations SET state = %s WHERE id = %s",
            (state, w["operation"]),
        )
        with pytest.raises(
            REFUSED, match="archive redaction refused: operation_unresolved"
        ):
            redact(db, w, g)
    owner(
        db,
        "UPDATE provider_operations SET state = 'succeeded' WHERE id = %s",
        (w["operation"],),
    )

    with pytest.raises(REFUSED, match="archive redaction refused: transit_unresolved"):
        redact(db, w, g)  # unsettled
    settle_transit(db, w, destroyed=False)
    with pytest.raises(REFUSED, match="transit_unresolved"):
        redact(db, w, g)  # settled, not yet destroyed
    settle_transit(db, w, destroyed=True)

    with pytest.raises(REFUSED, match="archive redaction refused: intent_live"):
        redact(db, w, g)  # the scheduled intent captured from the generation
    owner(
        db,
        "UPDATE post_intents SET state = 'cancelled' WHERE id = %s",
        (w["archive_intent"],),
    )

    # A live intent whose provenance links the set only by selection does not
    # hold redaction, and its row stays.
    g2, _ = receipts.grant(db, w)
    ((selected,),) = owner(
        db,
        "INSERT INTO post_intents (workspace_id, ig_account_id, media_item_id,"
        " provider_account_ref, approval_mode, schedule_slot_at)"
        " VALUES (%s, %s, %s, 'archive-account-2', 'manual', now() + interval '2 years')"
        " RETURNING id",
        (w["ws"], w["iga"], w["item"]),
    )
    owner(
        db,
        "INSERT INTO instagram_intent_provenance (workspace_id, intent_id, source_id,"
        " media_item_id, origin_ig_account_id, selection_generation_id,"
        " selection_observation_revision, capture_generation_id,"
        " capture_observation_revision, selected_asset_identity, capture_epoch,"
        " last_authorized_policy_revision)"
        " VALUES (%s, %s, %s, %s, %s, %s, 1, %s, 1, 'asset-fingerprint', 0, 1)",
        (w["ws"], selected, w["archive"], w["item"], w["iga"], w["generation"], g2),
    )

    # The runtime logins cannot delete these records themselves.
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
        run(
            db["worker"],
            "DELETE FROM instagram_operation_provenance WHERE workspace_id = %s",
            (w["ws"],),
            tenant=w["ws"],
        )

    assert redact(db, w, g) == [(1, 1, 1)], "operation, intent capture, artifact"
    ws = (w["ws"],)
    assert count(db, "instagram_operation_provenance", "workspace_id = %s", ws) == 0
    assert count(db, "instagram_artifacts", "workspace_id = %s", ws) == 0
    assert owner(
        db,
        "SELECT intent_id FROM instagram_intent_provenance WHERE workspace_id = %s",
        ws,
    ) == [(selected,)], "only the selection-only row stays"
    # The operation and the generation are other doors' records.
    assert count(db, "provider_operations", "id = %s", (w["operation"],)) == 1
    assert (
        count(db, "instagram_archive_generations", "id = %s", (w["generation"],)) == 1
    )


# --- archive lifecycle cleanup: the finalize transit guard and the offboarding cascade -------------


ARCHIVE_ROWS = (
    "media_sources",
    "media_source_destinations",
    "instagram_archive_generations",
    "instagram_source_policies",
    "instagram_media_index",
    "instagram_item_tombstones",
    "instagram_artifacts",
    "instagram_intent_provenance",
    "instagram_operation_provenance",
    "meta_identity_mappings",
    "provider_operations",
    "post_intents",
    "media_items",
    "oauth_credentials",
    "ig_accounts",
)


def test_finalize_refuses_an_owed_transit_upload_then_cascades_every_archive_row(db):
    w = evidence_world(db)
    who = receipts.subject()
    receipts.map_identity(db, w, who)
    r = receipts.receive(
        db, who, [receipts.fingerprint()], issued=T0 + timedelta(days=1)
    )
    ws = (w["ws"],)
    # Every inter-archive RESTRICT key holds a row too: the source's current
    # authorization and the intent's staged bundle on the transit artifact.
    owner(
        db,
        "UPDATE media_sources SET current_authorization_generation_id = %s WHERE id = %s",
        (w["generation"], w["archive"]),
    )
    owner(
        db,
        "UPDATE instagram_intent_provenance SET staged_generation_id = %s,"
        " staged_observation_revision = 1, transit_artifact_ref = %s,"
        " validation_fingerprint = 'validation' WHERE intent_id = %s",
        (w["generation"], w["artifact"], w["archive_intent"]),
    )
    assert all(count(db, t, "workspace_id = %s", ws) >= 1 for t in ARCHIVE_ROWS), (
        "positive control: every archive table holds a row of this workspace"
    )
    owner(db, "UPDATE post_intents SET state = 'cancelled' WHERE workspace_id = %s", ws)
    owner(
        db,
        "UPDATE workspaces SET state = 'offboarding', offboarding_at = now() - interval '2 days'"
        " WHERE id = %s",
        ws,
    )

    def finalize():
        return run(
            db["worker"], "SELECT fn_offboard_finalize(%s, interval '1 day')", ws
        )

    with pytest.raises(
        REFUSED, match="still holds archive transit cleanup obligations"
    ):
        finalize()  # unsettled
    settle_transit(db, w, destroyed=False)
    with pytest.raises(
        REFUSED, match="still holds archive transit cleanup obligations"
    ):
        finalize()  # settled, its asset not yet destroyed
    settle_transit(db, w, destroyed=True)

    finalize()
    assert count(db, "workspaces", "id = %s", ws) == 0
    survivors = {t: count(db, t, "workspace_id = %s", ws) for t in ARCHIVE_ROWS}
    assert survivors == dict.fromkeys(ARCHIVE_ROWS, 0), (
        "the cascade takes every archive row"
    )
    # The receipt is tenant-free: it outlives the workspace it scoped.
    assert count(db, "meta_deletion_receipts", "id = %s", (r["id"],)) == 1
    assert count(db, "meta_deletion_receipt_scopes", "workspace_id = %s", ws) == 1


# --- archive lifecycle job kinds: the deletion cleanup jobs ------------------------------------------


def cleanup(db, limit=25):
    return run(db["worker"], "SELECT fn_meta_receipt_cleanup_jobs(%s)", (limit,))[0][0]


def jobs(db, kind):
    return owner(
        db,
        "SELECT workspace_id, lane, serialization_key, max_attempts, payload,"
        " EXTRACT(EPOCH FROM deadline_at - run_at), state FROM jobs"
        " WHERE kind = %s ORDER BY workspace_id, created_at",
        (kind,),
    )


def settle_jobs(db, kind):
    owner(
        db,
        "UPDATE jobs SET state = 'succeeded' WHERE kind = %s AND state = 'ready'",
        (kind,),
    )


def test_cleanup_mints_one_job_per_due_receipt_workspace_and_never_a_second(db):
    r, first, second = receipts.two_workspace_receipt(db)
    assert cleanup(db, 1) == 1, "the limit bounds a call"
    assert cleanup(db) == 1
    assert jobs(db, "meta_deletion_cleanup") == sorted(
        (
            w["ws"],
            "bulk",
            f"receipt:{r['id']}:{w['ws']}",
            5,
            {"v": 1, "receipt_id": r["id"]},
            6 * 3600,
            "ready",
        )
        for w in (first, second)
    )
    assert cleanup(db) == 0, "a ready job on the key holds the next one back"
    settle_jobs(db, "meta_deletion_cleanup")
    assert cleanup(db) == 2, "still due and nothing ready: minted again"
    settle_jobs(db, "meta_deletion_cleanup")
    receipts.progress(db, r["id"], first, "cleaning", retry=3600)
    assert cleanup(db) == 1, "the reported scope is not due for an hour"
    assert owner(
        db,
        "SELECT serialization_key FROM jobs WHERE kind = 'meta_deletion_cleanup' AND state = 'ready'",
    ) == [(f"receipt:{r['id']}:{second['ws']}",)]
    settle_jobs(db, "meta_deletion_cleanup")
    # Completion is structural (Meta deletion receipt functions): the cleanup has deleted what each scope froze.
    for w in (first, second):
        receipts.set_lifecycle(db, w, "deleted")
    receipts.progress(db, r["id"], second, "completed")
    receipts.progress(db, r["id"], first, "completed")
    owner(
        db,
        "UPDATE meta_deletion_receipt_scopes SET next_attempt_at = now() - interval '1 hour'",
    )
    assert cleanup(db) == 0, "a completed scope is never due"


def test_cleanup_mints_at_most_twenty_five_and_the_limit_bounds_it(db):
    ((rid,),) = owner(
        db,
        "INSERT INTO meta_deletion_receipts (app_identity, request_fingerprint,"
        " fingerprint_key_version) VALUES ('instagram:1', %s, 1) RETURNING id",
        (secrets.token_hex(32),),
    )
    conn = psycopg2.connect(db["owner"])
    try:
        for i in range(26):
            chain = seed_workspace_chain(conn, f"cap-{i}-{uuid.uuid4().hex[:6]}")
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO meta_deletion_receipt_scopes (receipt_id, workspace_id,"
                    " ig_account_id, credential_id, authorization_id, disposition, state)"
                    " VALUES (%s, %s, %s, %s, %s, 'in_scope', 'pending')",
                    (
                        rid,
                        chain["ws"],
                        chain["iga"],
                        str(uuid.uuid4()),
                        str(uuid.uuid4()),
                    ),
                )
            conn.commit()
    finally:
        conn.close()
    assert cleanup(db, -5) == 0
    assert cleanup(db, 100) == 25, "never more than 25 in one call"
    assert cleanup(db, 100) == 1, "the 26th waits for the next"
    assert cleanup(db, 100) == 0


def test_cleanup_purges_scopes_of_deleted_workspaces_and_completes_their_receipts(db):
    def receipt_with(state, reason, scope_states):
        ((rid,),) = owner(
            db,
            "INSERT INTO meta_deletion_receipts (app_identity, request_fingerprint,"
            " fingerprint_key_version, state, review_reason) VALUES ('instagram:1', %s, 1, %s, %s)"
            " RETURNING id",
            (secrets.token_hex(32), state, reason),
        )
        for workspace, scope_state in scope_states:
            owner(
                db,
                "INSERT INTO meta_deletion_receipt_scopes (receipt_id, workspace_id,"
                " ig_account_id, credential_id, authorization_id, disposition, state)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    rid,
                    workspace,
                    str(uuid.uuid4()),
                    str(uuid.uuid4()),
                    str(uuid.uuid4()),
                    "held" if scope_state == "held" else "in_scope",
                    scope_state,
                ),
            )
        return rid

    live = receipts.world(db)["ws"]
    gone, gone_too = str(uuid.uuid4()), str(uuid.uuid4())  # no such workspace
    finished = receipt_with("pending", None, [(gone, "cleaning"), (live, "completed")])
    in_review = receipt_with("needs_review", "ordering_ambiguous", [(gone_too, "held")])
    waiting = receipt_with("pending", None, [(live, "pending")])

    assert cleanup(db) == 1, "only the live workspace's due scope"
    assert sorted(s[3] for s in receipts.scopes(db, finished)) == [
        "completed",
        "workspace_purged",
    ]
    assert owner(
        db,
        "SELECT state FROM meta_deletion_receipt_scopes WHERE workspace_id IN (%s, %s)",
        (gone, gone_too),
    ) == [("workspace_purged",), ("workspace_purged",)]
    assert receipts.receipt(db, finished)["state"] == "completed"
    assert receipts.receipt(db, finished)["completed"] is not None
    assert receipts.receipt(db, in_review)["state"] == "needs_review", (
        "a review is never resolved here"
    )
    assert receipts.receipt(db, waiting)["state"] == "pending"
    assert [j[0] for j in jobs(db, "meta_deletion_cleanup")] == [live]


def test_cleanup_completes_resolved_transit_review_after_workspace_purge(db):
    w = evidence_world(db)
    who = receipts.subject()
    receipts.map_identity(db, w, who)
    request = receipts.receive(
        db, who, [receipts.fingerprint()], issued=T0 + timedelta(days=1)
    )
    assert request["state"] == "pending"
    assert receipts.frozen(db, request["id"]) == {w["generation"]}
    assert (
        receipts.progress(
            db, request["id"], w, "cleaning", reason="transit_unresolved", retry=3600
        )
        == "needs_review"
    )
    assert receipts.status(db, request["capability"]) == [("in_progress",)]

    # The upload is now accounted for, but the still-live workspace and its
    # cleaning scope must not become a completed receipt just because the
    # sweep ran before the cleanup job's hour-later retry.
    settle_transit(db, w, destroyed=True)
    assert owner(
        db,
        "SELECT outcome, cleanup_state FROM instagram_artifacts WHERE artifact_ref = %s",
        (w["artifact"],),
    ) == [("settled", "destroyed")]
    assert cleanup(db, 0) == 0
    assert receipts.receipt(db, request["id"])["state"] == "needs_review"
    assert receipts.scopes(db, request["id"])[0][3] == "cleaning"

    owner(
        db,
        "UPDATE post_intents SET state = 'cancelled' WHERE workspace_id = %s",
        (w["ws"],),
    )
    owner(
        db,
        "UPDATE workspaces SET state = 'offboarding',"
        " offboarding_at = now() - interval '2 days' WHERE id = %s",
        (w["ws"],),
    )
    run(db["worker"], "SELECT fn_offboard_finalize(%s, interval '1 day')", (w["ws"],))
    assert count(db, "workspaces", "id = %s", (w["ws"],)) == 0
    assert receipts.receipt(db, request["id"])["state"] == "needs_review", (
        "the offboarding door leaves the durable receipt for the sweep"
    )
    assert cleanup(db, 0) == 0
    assert receipts.scopes(db, request["id"])[0][3] == "workspace_purged"
    facts = receipts.receipt(db, request["id"])
    assert (facts["state"], facts["reason"]) == ("completed", None)
    assert facts["completed"] is not None
    assert receipts.status(db, request["capability"]) == [("completed",)]
    assert cleanup(db, 0) == 0, "the completed receipt stays completed"


def test_cleanup_waits_for_every_scope_of_a_transit_review_receipt(db):
    request, first, second = receipts.two_workspace_receipt(db)
    assert (
        receipts.progress(
            db, request["id"], first, "cleaning", reason="transit_unresolved"
        )
        == "needs_review"
    )
    owner(
        db,
        "UPDATE post_intents SET state = 'cancelled' WHERE workspace_id = %s",
        (first["ws"],),
    )
    owner(
        db,
        "UPDATE workspaces SET state = 'offboarding',"
        " offboarding_at = now() - interval '2 days' WHERE id = %s",
        (first["ws"],),
    )
    run(
        db["worker"],
        "SELECT fn_offboard_finalize(%s, interval '1 day')",
        (first["ws"],),
    )
    assert cleanup(db, 0) == 0
    assert sorted(scope[3] for scope in receipts.scopes(db, request["id"])) == [
        "pending",
        "workspace_purged",
    ]
    assert receipts.receipt(db, request["id"])["state"] == "needs_review"
    assert receipts.status(db, request["capability"]) == [("in_progress",)]
    receipts.set_lifecycle(db, second, "deleted")
    assert receipts.progress(db, request["id"], second, "completed") == "completed"
    assert receipts.status(db, request["capability"]) == [("completed",)]


def test_cleanup_keeps_unverified_review_reasons_after_purge(db):
    w = receipts.world(db)
    who = receipts.subject()
    receipts.map_identity(db, w, who)
    ordering = receipts.receive(db, who, [receipts.fingerprint()], issued=T0)
    assert receipts.receipt(db, ordering["id"])["reason"] == "ordering_ambiguous"
    unknown_app = receipts.receive(
        db,
        receipts.subject(),
        [receipts.fingerprint()],
        app="facebook:legacy",
    )
    assert receipts.receipt(db, unknown_app["id"])["reason"] == "app_unrecognized"

    owner(
        db,
        "UPDATE post_intents SET state = 'cancelled' WHERE workspace_id = %s",
        (w["ws"],),
    )
    owner(
        db,
        "UPDATE workspaces SET state = 'offboarding',"
        " offboarding_at = now() - interval '2 days' WHERE id = %s",
        (w["ws"],),
    )
    run(db["worker"], "SELECT fn_offboard_finalize(%s, interval '1 day')", (w["ws"],))
    assert cleanup(db, 0) == 0
    assert receipts.scopes(db, ordering["id"])[0][3] == "workspace_purged"
    for request, reason in (
        (ordering, "ordering_ambiguous"),
        (unknown_app, "app_unrecognized"),
    ):
        facts = receipts.receipt(db, request["id"])
        assert (facts["state"], facts["reason"], facts["completed"]) == (
            "needs_review",
            reason,
            None,
        )
        assert receipts.status(db, request["capability"]) == [("in_progress",)]


def test_a_held_scope_is_due_until_its_generations_are_fenced(db):
    w = receipts.world(db)
    who = receipts.subject()
    receipts.map_identity(db, w, who)
    r = receipts.receive(db, who, [receipts.fingerprint()], issued=T0)  # held
    assert receipts.scopes(db, r["id"])[0][2:] == ("held", "held")
    assert cleanup(db) == 1, "its frozen generation is still active"
    settle_jobs(db, "meta_deletion_cleanup")
    assert cleanup(db) == 1
    settle_jobs(db, "meta_deletion_cleanup")
    owner(
        db,
        "UPDATE instagram_archive_generations SET lifecycle = 'review_hold' WHERE id = %s",
        (w["generation"],),
    )
    assert cleanup(db) == 0, "held for review: nothing left to fence"


# --- archive lifecycle job kinds: the reconciliation jobs --------------------------------------------


def converted(db, ws):
    owner(
        db,
        "UPDATE workspaces SET source_mix_mode = 'equal', source_mix_policy_version = 1,"
        " source_membership_policy = 'reset_equal', source_mix_revision = 1,"
        " source_mix_conversion_version = 1, source_mix_converted_at = now() WHERE id = %s",
        (ws,),
    )


def credentialed(db, *, convert=True, ref=None, state="active"):
    """A workspace whose account holds an ig_login credential and nothing else."""
    conn = psycopg2.connect(db["owner"])
    try:
        chain = seed_workspace_chain(conn, f"recon-{uuid.uuid4().hex[:8]}")
        with conn.cursor() as cur:
            if ref:
                cur.execute(
                    "UPDATE ig_accounts SET provider_account_ref = %s WHERE id = %s",
                    (ref, chain["iga"]),
                )
            cur.execute(
                "INSERT INTO oauth_credentials (workspace_id, ig_account_id, provider,"
                " encrypted_payload, state) VALUES (%s, %s, 'ig_login', 'ct', %s) RETURNING id",
                (chain["ws"], chain["iga"], state),
            )
            chain["credential"] = cur.fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    if convert:
        converted(db, chain["ws"])
    return chain


def archived(db, *, mapped):
    """A converted workspace with an archive source; its credential mapped or not."""
    w = receipts.world(db)
    converted(db, w["ws"])
    if mapped:
        receipts.map_identity(db, w, receipts.subject())
    return w


def reconcile(db, limit, run_id="synthetic-run", digest="a" * 64):
    return run(
        db["worker"],
        "SELECT fn_archive_reconcile_jobs(%s, %s, %s)",
        (limit, run_id, digest),
    )[0][0]


def test_reconciliation_mints_once_per_digest_for_unmapped_or_unsourced_accounts(db):
    eligible = [credentialed(db)["ws"] for _ in range(4)]
    eligible.append(archived(db, mapped=False)["ws"])  # a source, no current mapping
    unsourced = credentialed(db)
    receipts.map_identity(
        db, unsourced, receipts.subject(), authorization=str(uuid.uuid4())
    )
    eligible.append(unsourced["ws"])  # a current mapping, no source
    # Never: done already, not converted, a manual account, a revoked
    # credential, an offboarding workspace.
    archived(db, mapped=True)
    credentialed(db, convert=False)
    credentialed(db, ref="manual:someone")
    credentialed(db, state="revoked")
    leaving = credentialed(db)
    owner(
        db,
        "UPDATE workspaces SET state = 'offboarding', offboarding_at = now() WHERE id = %s",
        (leaving["ws"],),
    )

    assert reconcile(db, 10, run_id="not a run id!") == 0
    assert reconcile(db, 10, digest="A" * 64) == 0
    assert reconcile(db, 10) == 5, "at most five per call"
    assert reconcile(db, 10) == 1
    assert reconcile(db, 10) == 0, "one job per workspace per digest"
    rows = jobs(db, "reconcile_archive_accounts")
    assert [row[0] for row in rows] == sorted(eligible)
    for workspace, lane, key, attempts, payload, deadline, _ in rows:
        assert (lane, key, attempts, deadline) == (
            "bulk",
            f"archive-reconcile:{workspace}",
            5,
            6 * 3600,
        )
        assert payload == {"v": 1, "run_id": "synthetic-run", "run_digest": "a" * 64}
    settle_jobs(db, "reconcile_archive_accounts")
    assert reconcile(db, 10) == 0, "a finished job still counts for its digest"
    assert reconcile(db, 2, digest="b" * 64) == 2, "a new digest starts again, bounded"
