"""Migrations 142-143 (#1644): the Meta identity mapping and
deletion receipt doors, driven as the real runtime logins.

Each test gets a fresh lineage database: receipts are tenant-free and the doors
read across every workspace, so a shared world would let one test's episode
become another's prior episode. Seeding is the owner's (it bypasses RLS); every
door call is the API's or the worker's login, with the tenant claimed the way
the unit of work claims it, transaction-local.
"""

import secrets
import uuid
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.errors
import pytest

from tests.scripts.conftest import as_user, seed_workspace_chain, set_test_passwords
from tests.scripts.test_instagram_catalog_gate import (
    seed_archive,
    seed_archive_evidence,
)

pytestmark = [pytest.mark.integration]

#: A fixed past instant, so every ordering boundary is exact rather than "about now".
T0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
SKEW = 300
APP = "instagram:1"
REFUSED = psycopg2.errors.RaiseException


@pytest.fixture
def db(world_db, admin_conn):
    set_test_passwords(admin_conn)
    return {
        "owner": world_db,
        "ingress": as_user(world_db, "svc_ingress"),
        "worker": as_user(world_db, "svc_worker"),
    }


def run(dsn, sql, params=(), *, tenant=None):
    """One statement in one transaction as *dsn*'s login; the tenant, when
    given, is claimed transaction-local, as `unit_of_work.apply_gucs` does."""
    conn = psycopg2.connect(dsn)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT set_config('app.actor_kind', 'system', true)")
            if tenant is not None:
                cur.execute(
                    "SELECT set_config('app.tenant_id', %s, true)", (str(tenant),)
                )
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else cur.rowcount
    finally:
        conn.close()


def owner(db, sql, params=()):
    """As the table owner, the seeding path, with the migration actor."""
    conn = psycopg2.connect(db["owner"])
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET LOCAL app.actor_kind = 'migration'")
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else cur.rowcount
    finally:
        conn.close()


def world(db, *, evidence=False):
    """A workspace with an Instagram archive: source, credential, one active
    generation (its authorization returned), an item and its index row. With
    ``evidence``, also a scheduled intent captured from the generation and its
    unsettled transit upload (``w["artifact"]``)."""
    conn = psycopg2.connect(db["owner"])
    try:
        chain = seed_workspace_chain(conn, f"rcpt-{uuid.uuid4().hex[:8]}")
        with conn.cursor() as cur:
            w = (seed_archive_evidence if evidence else seed_archive)(cur, chain)
            cur.execute(
                "SELECT authorization_id FROM instagram_archive_generations WHERE id = %s",
                (w["generation"],),
            )
            w["authorization"] = cur.fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    return w


def grant(db, w, *, lifecycle="superseded"):
    """Another authorization's generation on the same archive source."""
    authorization = str(uuid.uuid4())
    ((generation,),) = owner(
        db,
        "INSERT INTO instagram_archive_generations (workspace_id, source_id,"
        " origin_ig_account_id, credential_id, authorization_id, app_id, subject_id,"
        " authorized_at, lifecycle) VALUES (%s, %s, %s, %s, %s, 'app', 'subject',"
        " now(), %s) RETURNING id",
        (w["ws"], w["archive"], w["iga"], w["credential"], authorization, lifecycle),
    )
    return generation, authorization


def map_identity(
    db,
    w,
    subject,
    *,
    authorized_at=T0,
    verified_at=None,
    authorization=None,
    app=APP,
    current=True,
):
    """One verified mapping on the archive's credential: an OAuth exchange,
    or a reconciling read when ``authorized_at`` is None."""
    owner(
        db,
        "INSERT INTO meta_identity_mappings (workspace_id, ig_account_id,"
        " credential_id, authorization_id, app_identity, subject_ref, account_ref,"
        " method, authorized_at, verified_at, is_current)"
        " VALUES (%s, %s, %s, %s, %s, %s, '100', %s, %s, %s, %s)",
        (
            w["ws"],
            w["iga"],
            w["credential"],
            authorization or w["authorization"],
            app,
            subject,
            "oauth_exchange" if authorized_at is not None else "reconciled_read",
            authorized_at,
            verified_at or authorized_at or T0,
            current,
        ),
    )


def subject():
    return str(10**14 + uuid.uuid4().int % 10**14)


def fingerprint():
    return secrets.token_hex(32)


def receive(db, subject, fingerprints, *, issued=None, app=APP, key_version=1):
    capability = secrets.token_hex(32)
    ((rid, replay, state),) = run(
        db["ingress"],
        "SELECT o_receipt_id, o_replay, o_state FROM fn_meta_deletion_receive("
        "%s, %s::text[], %s, %s, %s, %s, %s)",
        (app, list(fingerprints), key_version, subject, issued, capability, SKEW),
    )
    return {"id": rid, "replay": replay, "state": state, "capability": capability}


def receipt(db, rid):
    ((state, reason, issued, completed),) = owner(
        db,
        "SELECT state, review_reason, issued_at, completed_at"
        " FROM meta_deletion_receipts WHERE id = %s",
        (rid,),
    )
    return {"state": state, "reason": reason, "issued": issued, "completed": completed}


def scopes(db, rid):
    return owner(
        db,
        "SELECT workspace_id, authorization_id, disposition, state"
        " FROM meta_deletion_receipt_scopes WHERE receipt_id = %s"
        " ORDER BY workspace_id, authorization_id",
        (rid,),
    )


def frozen(db, rid):
    return {
        g
        for (g,) in owner(
            db,
            "SELECT generation_id FROM meta_deletion_receipt_generations"
            " WHERE receipt_id = %s",
            (rid,),
        )
    }


def status(db, capability):
    return run(
        db["ingress"], "SELECT o_state FROM fn_meta_deletion_status(%s)", (capability,)
    )


def lifecycles(db, w):
    """Every generation of the workspace's archives, by lifecycle."""
    return dict(
        owner(
            db,
            "SELECT id, lifecycle FROM instagram_archive_generations"
            " WHERE workspace_id = %s",
            (w["ws"],),
        )
    )


def set_lifecycle(db, w, lifecycle):
    """What the cleanup does to the world's generation: fence it, then delete it."""
    owner(
        db,
        "UPDATE instagram_archive_generations SET lifecycle = %s WHERE id = %s",
        (lifecycle, w["generation"]),
    )


def settle_transit(db, w, *, destroyed):
    owner(
        db,
        "UPDATE instagram_artifacts SET outcome = 'settled', settled_at = now(),"
        " cleanup_state = %s WHERE artifact_ref = %s",
        ("destroyed" if destroyed else "pending", w["artifact"]),
    )


# --- 142: the mappings and the deauthorize door ------------------------------


def test_subject_mappings_are_one_equality_read_across_tenants_without_a_tenant(db):
    first, second = world(db), world(db)
    who, stranger = subject(), subject()
    map_identity(db, first, who, verified_at=T0 + timedelta(days=2))
    map_identity(db, second, who, verified_at=T0 + timedelta(days=1))
    # The same raw subject under another app is another identity.
    other_app = world(db)
    map_identity(db, other_app, who, app="instagram:2")
    map_identity(db, world(db), stranger)

    rows = run(
        db["ingress"],
        "SELECT o_workspace_id, o_credential_id, o_authorization_id, o_is_current"
        " FROM fn_meta_subject_mappings(%s, %s)",
        (APP, who),
    )
    expected = sorted(
        [
            (first["ws"], first["credential"], first["authorization"], True),
            (second["ws"], second["credential"], second["authorization"], True),
        ]
    )
    assert rows == expected, "exactly the two workspaces' rows, workspace ascending"
    assert run(
        db["ingress"],
        "SELECT o_workspace_id FROM fn_meta_subject_mappings('instagram:2', %s)",
        (who,),
    ) == [(other_app["ws"],)]
    # The door is what reaches them: the API's login reads nothing directly
    # without a tenant, beside the rows the door just returned.
    assert run(db["ingress"], "SELECT count(*) FROM meta_identity_mappings") == [(0,)]


def test_a_mapping_is_evidence_only_its_current_flag_falls(db):
    w = world(db)
    map_identity(db, w, subject())
    flip = "UPDATE meta_identity_mappings SET is_current = %s WHERE workspace_id = %s"
    assert run(db["worker"], flip, (False, w["ws"]), tenant=w["ws"]) == 1
    with pytest.raises(psycopg2.errors.CheckViolation, match="immutable"):
        run(db["worker"], flip, (True, w["ws"]), tenant=w["ws"])
    # The logins' UPDATE grant is the flag alone, and they hold no DELETE.
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
        run(
            db["worker"],
            "UPDATE meta_identity_mappings SET subject_ref = '1' WHERE workspace_id = %s",
            (w["ws"],),
            tenant=w["ws"],
        )
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
        run(
            db["ingress"],
            "DELETE FROM meta_identity_mappings WHERE workspace_id = %s",
            (w["ws"],),
            tenant=w["ws"],
        )
    # Past the grants, the trigger holds the evidence even for the owner.
    with pytest.raises(psycopg2.errors.CheckViolation, match="immutable"):
        owner(
            db,
            "UPDATE meta_identity_mappings SET subject_ref = '1' WHERE workspace_id = %s",
            (w["ws"],),
        )


@pytest.mark.parametrize(
    "app,subject_ref,method,authorized,constraint",
    [
        ("facebook:legacy", "1", "oauth_exchange", True, "ck_meta_mapping_app"),
        ("instagram:1", "x1", "oauth_exchange", True, "ck_meta_mapping_refs"),
        ("instagram:1", "1", "oauth_exchange", False, "ck_meta_mapping_method"),
        ("instagram:1", "1", "reconciled_read", True, "ck_meta_mapping_method"),
    ],
)
def test_mapping_shape_is_checked(db, app, subject_ref, method, authorized, constraint):
    w = world(db)
    with pytest.raises(psycopg2.errors.CheckViolation, match=constraint):
        owner(
            db,
            "INSERT INTO meta_identity_mappings (workspace_id, ig_account_id,"
            " credential_id, authorization_id, app_identity, subject_ref,"
            " account_ref, method, authorized_at, verified_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, '100', %s, %s, now())",
            (
                w["ws"],
                w["iga"],
                w["credential"],
                str(uuid.uuid4()),
                app,
                subject_ref,
                method,
                T0 if authorized else None,
            ),
        )


def test_one_current_mapping_per_credential(db):
    w = world(db)
    map_identity(db, w, subject())
    with pytest.raises(
        psycopg2.errors.UniqueViolation, match="uq_meta_mapping_current"
    ):
        map_identity(db, w, subject(), authorization=str(uuid.uuid4()))
    map_identity(db, w, subject(), authorization=str(uuid.uuid4()), current=False)


# --- 143: receive, replay, classification ------------------------------------


def test_a_request_freezes_its_scope_and_a_replay_reuses_the_receipt(db):
    w = world(db)
    who, fp = subject(), fingerprint()
    map_identity(db, w, who)
    first = receive(db, who, [fp], issued=T0 + timedelta(days=1))
    assert (first["replay"], first["state"]) == (False, "pending")
    assert scopes(db, first["id"]) == [
        (w["ws"], w["authorization"], "in_scope", "pending")
    ]
    assert frozen(db, first["id"]) == {w["generation"]}

    again = receive(db, who, [fp], issued=T0 + timedelta(days=1))
    # Rotation: the current key's fingerprint is new, an older version matches.
    rotated = receive(db, who, [fingerprint(), fp], key_version=2)
    for replay in (again, rotated):
        assert (replay["id"], replay["replay"], replay["state"]) == (
            first["id"],
            True,
            "pending",
        )
    assert scopes(db, first["id"]) == [
        (w["ws"], w["authorization"], "in_scope", "pending")
    ]
    assert frozen(db, first["id"]) == {w["generation"]}
    assert owner(
        db,
        "SELECT count(*) FROM meta_deletion_receipts WHERE request_fingerprint = %s",
        (fp,),
    ) == [(1,)]
    # A capability per delivery, each naming the one receipt.
    assert owner(
        db,
        "SELECT count(*) FROM meta_deletion_receipt_capabilities WHERE receipt_id = %s",
        (first["id"],),
    ) == [(3,)]
    for delivery in (first, again, rotated):
        assert status(db, delivery["capability"]) == [("in_progress",)]
    assert status(db, secrets.token_hex(32)) == [], "an unknown capability is no row"

    other = receive(db, who, [fingerprint()], issued=T0 + timedelta(days=1))
    assert other["replay"] is False and other["id"] != first["id"], "a new episode"


def test_receipts_are_closed_to_the_runtime_logins(db):
    w = world(db)
    who = subject()
    map_identity(db, w, who)
    receive(db, who, [fingerprint()], issued=T0 + timedelta(days=1))
    for login in ("ingress", "worker"):
        for table in (
            "meta_deletion_receipts",
            "meta_deletion_receipt_scopes",
            "meta_deletion_receipt_generations",
            "meta_deletion_receipt_capabilities",
        ):
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                run(db[login], f"SELECT count(*) FROM {table}", tenant=w["ws"])


@pytest.mark.parametrize(
    "authorized,verified,issued,disposition",
    [
        # upper <= issued - skew: in scope, the boundary included
        (T0, None, T0 + timedelta(hours=1), "in_scope"),
        (T0, None, T0 + timedelta(seconds=SKEW), "in_scope"),
        # inside the skew on either side: held
        (T0, None, T0 + timedelta(seconds=SKEW - 1), "held"),
        (T0, None, T0 - timedelta(seconds=SKEW - 1), "held"),
        # lower >= issued + skew: excluded, the boundary included
        (T0, None, T0 - timedelta(seconds=SKEW), "excluded"),
        (T0, None, T0 - timedelta(days=1), "excluded"),
        # a reconciling read knows only when it was verified: never excluded
        (None, T0, T0 + timedelta(hours=1), "in_scope"),
        (None, T0, T0 - timedelta(days=1), "held"),
    ],
)
def test_each_mapping_is_classified_by_its_authorization_against_issued_at(
    db, authorized, verified, issued, disposition
):
    w = world(db)
    who = subject()
    map_identity(db, w, who, authorized_at=authorized, verified_at=verified)
    r = receive(db, who, [fingerprint()], issued=issued)
    facts = receipt(db, r["id"])
    if disposition == "excluded":
        assert scopes(db, r["id"]) == [] and frozen(db, r["id"]) == set()
        assert r["state"] == facts["state"] == "completed"
        assert facts["reason"] is None and facts["completed"] is not None
    elif disposition == "held":
        assert scopes(db, r["id"]) == [(w["ws"], w["authorization"], "held", "held")]
        assert frozen(db, r["id"]) == {w["generation"]}, "a held scope still freezes"
        assert (r["state"], facts["reason"]) == ("needs_review", "ordering_ambiguous")
    else:
        assert scopes(db, r["id"]) == [
            (w["ws"], w["authorization"], "in_scope", "pending")
        ]
        assert frozen(db, r["id"]) == {w["generation"]}
        assert (r["state"], facts["reason"]) == ("pending", None)


def test_without_order_evidence_only_the_accounts_first_episode_is_in_scope(db):
    w = world(db)
    who = subject()
    map_identity(db, w, who)
    # An issue time beyond the skew is no evidence: stored as absent.
    first = receive(
        db, who, [fingerprint()], issued=datetime.now(timezone.utc) + timedelta(days=1)
    )
    assert receipt(db, first["id"])["issued"] is None
    assert scopes(db, first["id"]) == [
        (w["ws"], w["authorization"], "in_scope", "pending")
    ]
    second = receive(db, who, [fingerprint()], issued=None)
    assert scopes(db, second["id"]) == [(w["ws"], w["authorization"], "held", "held")]
    assert (second["state"], receipt(db, second["id"])["reason"]) == (
        "needs_review",
        "ordering_ambiguous",
    )


def test_without_order_evidence_a_reauthorized_account_holds_every_grant(db):
    """Nothing says which of two grants an undated request reached, and a
    boundary never expands to a newer grant for want of evidence: both are held,
    the receipt needs review, no generation is frozen into an in-scope scope and
    nothing is fenced. The same request on an account with one grant is the
    positive control: it is in scope."""
    w = world(db)
    who = subject()
    map_identity(db, w, who, current=False)
    g2, a2 = grant(db, w)
    map_identity(db, w, who, authorization=a2, authorized_at=T0 + timedelta(days=1))
    r = receive(db, who, [fingerprint()], issued=None)
    assert scopes(db, r["id"]) == sorted(
        [
            (w["ws"], w["authorization"], "held", "held"),
            (w["ws"], a2, "held", "held"),
        ]
    )
    facts = receipt(db, r["id"])
    assert (r["state"], facts["state"], facts["reason"]) == (
        "needs_review",
        "needs_review",
        "ordering_ambiguous",
    )
    assert frozen(db, r["id"]) == {w["generation"], g2}, "a held scope still freezes"
    assert owner(
        db,
        "SELECT count(*) FROM meta_deletion_receipt_generations g"
        " JOIN meta_deletion_receipt_scopes s ON s.receipt_id = g.receipt_id"
        " AND s.workspace_id = g.workspace_id AND s.credential_id = g.credential_id"
        " AND s.authorization_id = g.authorization_id"
        " WHERE g.receipt_id = %s AND s.disposition = 'in_scope'",
        (r["id"],),
    ) == [(0,)], "no generation is frozen into a scope that is in scope"
    assert lifecycles(db, w) == {w["generation"]: "active", g2: "superseded"}, (
        "nothing is fenced"
    )

    single = world(db)
    only = subject()
    map_identity(db, single, only)
    c = receive(db, only, [fingerprint()], issued=None)
    assert scopes(db, c["id"]) == [
        (single["ws"], single["authorization"], "in_scope", "pending")
    ]
    assert frozen(db, c["id"]) == {single["generation"]}
    assert (c["state"], receipt(db, c["id"])["reason"]) == ("pending", None)


def test_a_frozen_scope_never_takes_a_newer_grant_or_a_deleted_generation(db):
    w = world(db)
    who = subject()
    map_identity(db, w, who, current=False)
    # The first grant's generation is superseded by a later grant (G2).
    owner(
        db,
        "UPDATE instagram_archive_generations SET lifecycle = 'superseded' WHERE id = %s",
        (w["generation"],),
    )
    g2, a2 = grant(db, w, lifecycle="active")
    map_identity(db, w, who, authorization=a2, authorized_at=T0 + timedelta(days=10))
    # An older grant whose generation is already deleted freezes nothing.
    g0, a0 = grant(db, w, lifecycle="deleted")
    map_identity(
        db,
        w,
        who,
        authorization=a0,
        authorized_at=T0 - timedelta(days=10),
        current=False,
    )

    r = receive(db, who, [fingerprint()], issued=T0 + timedelta(days=5))
    assert scopes(db, r["id"]) == sorted(
        [
            (w["ws"], w["authorization"], "in_scope", "pending"),
            (w["ws"], a0, "in_scope", "pending"),
        ]
    ), "G2's grant is newer than the request: excluded, no scope"
    assert frozen(db, r["id"]) == {w["generation"]}, "never G2, never a deleted G0"

    # A grant made after the receipt never joins it, even through a replay.
    g3, a3 = grant(db, w)
    map_identity(
        db,
        w,
        who,
        authorization=a3,
        authorized_at=T0 + timedelta(days=1),
        current=False,
    )
    (fp,) = owner(
        db,
        "SELECT request_fingerprint FROM meta_deletion_receipts WHERE id = %s",
        (r["id"],),
    )[0]
    assert receive(db, who, [fp])["replay"] is True
    assert frozen(db, r["id"]) == {w["generation"]}
    assert {g2, g3}.isdisjoint(frozen(db, r["id"]))


@pytest.mark.parametrize("app", ["facebook:legacy", "instagram:unconfigured"])
def test_an_unrecognized_app_needs_review_with_no_scope(db, app):
    w = world(db)
    who = subject()
    map_identity(db, w, who)  # the same subject under the configured app
    r = receive(db, who, [fingerprint()], issued=T0 + timedelta(days=1), app=app)
    assert scopes(db, r["id"]) == []
    assert (r["state"], receipt(db, r["id"])["reason"]) == (
        "needs_review",
        "app_unrecognized",
    )
    assert status(db, r["capability"]) == [("in_progress",)], (
        "the public status says no more"
    )


def test_a_subject_nobody_mapped_completes_with_an_empty_scope(db):
    map_identity(db, world(db), subject())
    r = receive(db, subject(), [fingerprint()], issued=T0 + timedelta(days=1))
    facts = receipt(db, r["id"])
    assert (r["state"], facts["reason"], scopes(db, r["id"])) == ("completed", None, [])
    assert facts["completed"] is not None
    assert status(db, r["capability"]) == [("completed",)]


@pytest.mark.parametrize(
    "app,fingerprints,key_version,who,capability,skew",
    [
        ("instagram:abc", None, 1, "1", None, SKEW),
        (APP, [], 1, "1", None, SKEW),
        (APP, ["A" * 64], 1, "1", None, SKEW),
        (APP, ["a" * 63], 1, "1", None, SKEW),
        (APP, ["a" * 64] * 17, 1, "1", None, SKEW),
        (APP, None, 0, "1", None, SKEW),
        (APP, None, 1, "x1", None, SKEW),
        (APP, None, 1, "1", "not-a-hash", SKEW),
        (APP, None, 1, "1", None, -1),
        (APP, None, 1, "1", None, 3601),
    ],
)
def test_the_receive_door_refuses_a_malformed_request_and_writes_nothing(
    db, app, fingerprints, key_version, who, capability, skew
):
    fps = [fingerprint()] if fingerprints is None else fingerprints
    with pytest.raises(REFUSED, match="meta receipt refused: invalid input"):
        run(
            db["ingress"],
            "SELECT * FROM fn_meta_deletion_receive(%s, %s::text[], %s, %s, NULL, %s, %s)",
            (app, fps, key_version, who, capability or secrets.token_hex(32), skew),
        )
    assert owner(db, "SELECT count(*) FROM meta_deletion_receipts") == [(0,)]
    # Positive control: the same call, well formed, is admitted.
    receive(db, "1", [fingerprint()])
    assert owner(db, "SELECT count(*) FROM meta_deletion_receipts") == [(1,)]


def test_a_null_fingerprint_inside_the_array_is_refused(db):
    with pytest.raises(REFUSED, match="invalid input"):
        run(
            db["ingress"],
            "SELECT * FROM fn_meta_deletion_receive(%s, ARRAY[%s, NULL]::text[], 1,"
            " '1', NULL, %s, %s)",
            (APP, fingerprint(), secrets.token_hex(32), SKEW),
        )


# --- 143: the worker's scope read and progress report ------------------------


def two_workspace_receipt(db):
    first, second = world(db), world(db)
    who = subject()
    for w in (first, second):
        map_identity(db, w, who)
    r = receive(db, who, [fingerprint()], issued=T0 + timedelta(days=1))
    return r, first, second


def scope_rows(db, rid, tenant):
    return run(
        db["worker"],
        "SELECT o_credential_id, o_authorization_id, o_disposition, o_state,"
        " o_source_id, o_generation_ids::text[] FROM fn_meta_receipt_scope(%s)",
        (rid,),
        tenant=tenant,
    )


def test_the_scope_read_is_bound_to_the_claimed_tenant(db):
    r, first, second = two_workspace_receipt(db)
    for w in (first, second):
        assert scope_rows(db, r["id"], w["ws"]) == [
            (
                w["credential"],
                w["authorization"],
                "in_scope",
                "pending",
                w["archive"],
                [w["generation"]],
            )
        ]
    assert scope_rows(db, r["id"], None) == [], "no tenant reads nothing"
    assert scope_rows(db, r["id"], world(db)["ws"]) == [], (
        "another tenant reads nothing"
    )


def progress(db, rid, w, state, *, tenant="own", results=None, reason=None, retry=60):
    return run(
        db["worker"],
        "SELECT fn_meta_receipt_progress(%s, %s, %s, %s, %s::jsonb, %s, %s)",
        (
            rid,
            w["credential"],
            w["authorization"],
            state,
            results if results is not None else '{"v": 1}',
            reason,
            retry,
        ),
        tenant=w["ws"] if tenant == "own" else tenant,
    )[0][0]


def due_in(db, rid, w):
    ((seconds,),) = owner(
        db,
        "SELECT EXTRACT(EPOCH FROM next_attempt_at - now())"
        " FROM meta_deletion_receipt_scopes WHERE receipt_id = %s AND workspace_id = %s",
        (rid, w["ws"]),
    )
    return float(seconds)


def test_progress_moves_a_scope_and_completes_the_receipt_from_the_database(db):
    r, first, second = two_workspace_receipt(db)
    assert progress(db, r["id"], first, "cleaning", retry=0) == "pending"
    assert 50 <= due_in(db, r["id"], first) <= 61, "the retry floor is 60 seconds"
    assert progress(db, r["id"], first, "cleaning", retry=10**6) == "pending"
    assert 3590 <= due_in(db, r["id"], first) <= 3601, "the retry ceiling is an hour"
    # Completion is structural: the cleanup has deleted what each scope froze.
    set_lifecycle(db, first, "deleted")
    set_lifecycle(db, second, "deleted")
    assert progress(db, r["id"], first, "completed") == "pending", "one scope remains"
    assert receipt(db, r["id"])["state"] == "pending"
    # pending -> completed directly, and the last scope completes the receipt.
    assert progress(db, r["id"], second, "completed") == "completed"
    facts = receipt(db, r["id"])
    assert facts["state"] == "completed" and facts["completed"] is not None
    assert status(db, r["capability"]) == [("completed",)]


def test_progress_refuses_every_move_it_does_not_name(db):
    r, first, second = two_workspace_receipt(db)
    with pytest.raises(REFUSED, match="scope_unavailable"):
        progress(db, r["id"], first, "cleaning", tenant=None)
    with pytest.raises(REFUSED, match="scope_unavailable"):
        progress(db, r["id"], first, "cleaning", tenant=second["ws"])
    for state, results, reason in (
        ("pending", None, None),
        ("held", None, None),
        ("cleaning", '["not", "an", "object"]', None),
        ("cleaning", '{"x": "%s"}' % ("y" * 4100), None),
        ("cleaning", None, "ordering_ambiguous"),
        ("completed", None, "transit_unresolved"),
    ):
        with pytest.raises(
            REFUSED, match="meta receipt progress refused: invalid input"
        ):
            progress(db, r["id"], first, state, results=results, reason=reason)
    set_lifecycle(db, first, "deleted")
    progress(db, r["id"], first, "completed")
    with pytest.raises(REFUSED, match="scope_completed"):
        progress(db, r["id"], first, "cleaning")
    owner(
        db,
        "UPDATE meta_deletion_receipt_scopes SET state = 'workspace_purged'"
        " WHERE receipt_id = %s AND workspace_id = %s",
        (r["id"], second["ws"]),
    )
    with pytest.raises(REFUSED, match="scope_workspace_purged"):
        progress(db, r["id"], second, "cleaning")


def test_a_held_scope_is_refused_and_keeps_the_receipt_in_review(db):
    w = world(db)
    who = subject()
    map_identity(db, w, who)
    r = receive(db, who, [fingerprint()], issued=T0)  # inside the skew: held
    with pytest.raises(REFUSED, match="scope_held"):
        progress(db, r["id"], w, "cleaning")


def test_completion_is_structural_generations_deleted_and_uploads_resolved(db):
    w = world(db, evidence=True)
    who = subject()
    map_identity(db, w, who)
    r = receive(db, who, [fingerprint()], issued=T0 + timedelta(days=1))
    pending = [(w["ws"], w["authorization"], "in_scope", "pending")]
    assert scopes(db, r["id"]) == pending

    # Never fenced, or fenced and not yet deleted: the generation is not deleted.
    for lifecycle in ("active", "deletion_pending"):
        set_lifecycle(db, w, lifecycle)
        with pytest.raises(REFUSED, match="generations_not_deleted"):
            progress(db, r["id"], w, "completed")
    # Deleted, but the admitted upload is unsettled, or settled and not destroyed.
    set_lifecycle(db, w, "deleted")
    with pytest.raises(
        REFUSED, match="meta receipt progress refused: transit_unresolved"
    ):
        progress(db, r["id"], w, "completed")
    settle_transit(db, w, destroyed=False)
    with pytest.raises(REFUSED, match="transit_unresolved"):
        progress(db, r["id"], w, "completed")
    # A refusal writes nothing.
    assert scopes(db, r["id"]) == pending
    assert receipt(db, r["id"])["state"] == "pending"
    # Reporting that it is still being cleaned is not completion: no such check.
    assert progress(db, r["id"], w, "cleaning") == "pending"

    # Positive control: every generation deleted and the upload destroyed.
    settle_transit(db, w, destroyed=True)
    assert progress(db, r["id"], w, "completed") == "completed"
    assert scopes(db, r["id"]) == [
        (w["ws"], w["authorization"], "in_scope", "completed")
    ]
    assert status(db, r["capability"]) == [("completed",)]


def test_a_scope_that_froze_nothing_completes(db):
    w = world(db)
    who = subject()
    map_identity(db, w, who)
    set_lifecycle(db, w, "deleted")  # already deleted: nothing is frozen
    r = receive(db, who, [fingerprint()], issued=T0 + timedelta(days=1))
    assert frozen(db, r["id"]) == set()
    assert progress(db, r["id"], w, "completed") == "completed"


def test_an_unresolved_transit_puts_the_receipt_in_review_until_its_evidence_completes(
    db,
):
    r, first, second = two_workspace_receipt(db)
    assert progress(db, r["id"], first, "cleaning", reason="transit_unresolved") == (
        "needs_review"
    )
    assert receipt(db, r["id"])["reason"] == "transit_unresolved"
    assert status(db, r["capability"]) == [("in_progress",)]
    for w in (first, second):
        set_lifecycle(db, w, "deleted")
    # The evidence holds for each scope in turn; the receipt completes with the last.
    assert progress(db, r["id"], first, "completed") == "needs_review", "one remains"
    assert receipt(db, r["id"])["state"] == "needs_review"
    assert progress(db, r["id"], second, "completed") == "completed"
    facts = receipt(db, r["id"])
    assert (facts["state"], facts["reason"]) == ("completed", None)
    assert facts["completed"] is not None
    assert status(db, r["capability"]) == [("completed",)]


def test_a_receipt_in_review_for_ordering_never_completes_from_a_report(db):
    first, second = world(db), world(db)
    who = subject()
    map_identity(db, first, who, authorized_at=T0 - timedelta(days=1))
    map_identity(db, second, who)  # authorized at the request's own instant: held
    r = receive(db, who, [fingerprint()], issued=T0)
    assert scopes(db, r["id"]) == sorted(
        [
            (first["ws"], first["authorization"], "in_scope", "pending"),
            (second["ws"], second["authorization"], "held", "held"),
        ]
    )
    assert (r["state"], receipt(db, r["id"])["reason"]) == (
        "needs_review",
        "ordering_ambiguous",
    )
    # The held scope's workspace is purged, so nothing but the reason keeps the
    # receipt in review once the in-scope scope completes.
    owner(
        db,
        "UPDATE meta_deletion_receipt_scopes SET state = 'workspace_purged'"
        " WHERE receipt_id = %s AND workspace_id = %s",
        (r["id"], second["ws"]),
    )
    set_lifecycle(db, first, "deleted")
    assert progress(db, r["id"], first, "completed") == "needs_review"
    facts = receipt(db, r["id"])
    assert (facts["state"], facts["reason"], facts["completed"]) == (
        "needs_review",
        "ordering_ambiguous",
        None,
    )
    assert status(db, r["capability"]) == [("in_progress",)]
