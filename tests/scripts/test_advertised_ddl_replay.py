"""Arm (a) of `advertised_ddl_replay` (#753 PR-B, plan §0.2) — the extracted
stream REPLAYED from empty UNDER THE DECLARED ACTORS.

Actor-coupled and DB-backed, so it lives apart from the pure-text extractor
suite (`test_advertised_ddl.py`) and is gated one-at-a-time on the shared
cluster (#758). The stream seeds nothing and creates the target schema into
the empty `public` the M.3 move leaves behind — replayed as `svc_migration`
after the step-0 bootstrap runs as the owner, exactly as M.3 step-3 does.

The four assertions §0.2 names, on the replayed database (behavior, not
syntax): one legal intent transition succeeds, one illegal transition raises,
`pg_policies` matches the normative lists, and the end state carries no
door-owner `CREATE` on `public` (the stream's transient bracket closed behind
itself).
"""

import psycopg2
import pytest

from scripts.advertised_ddl import (
    DEFAULT_DOCS,
    DEFAULT_MANIFEST,
    build_stream,
    load_manifest,
)
from scripts.tenancy_gate import (
    tenancy_signature,
    tenancy_violations,
    tenant_keyed_tables,
)
from tests.scripts.conftest import fetch_one, window_actor

pytestmark = [pytest.mark.integration, pytest.mark.slow]

#: The system roles that OWN the SECURITY DEFINER doors (`02` §7). The stream
#: may hold one of these a transient CREATE on public to install its doors;
#: the fourth assertion is that no such grant survives ("the transient bracket
#: closed behind itself"). svc_migration is excluded — it owns public in the
#: steady state by design, not as a door-owner bracket.
DOOR_OWNER_ROLES = ("svc_claim", "svc_clock", "svc_maintenance", "svc_membership")


def _replay_as_window_actor(owner_window_db, owner_actor, admin_conn):
    """Bootstrap as owner, then replay the advertised stream as svc_migration
    into the empty public — the M.3 step-3 shape. Returns the window DSN."""
    manifest = load_manifest(DEFAULT_MANIFEST)
    stream = build_stream(DEFAULT_DOCS, manifest)
    as_svc = window_actor(owner_window_db, owner_actor, admin_conn)
    conn = psycopg2.connect(as_svc)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(stream)
    finally:
        conn.close()
    return as_svc


def _seed_scheduled_intent(conn) -> str:
    """The full parent chain a post_intent requires, born in 'scheduled'.

    Run in ONE transaction and committed, because ``ct_workspaces_owner_at_insert``
    is a CONSTRAINT TRIGGER INITIALLY DEFERRED — a workspace and its owner
    member row must both exist by commit — so the chain is workspace + user +
    owner membership + media_source + ig_account + media_item + intent, all
    NOT-NULL / CHECK / composite-FK satisfied. ``app.actor_kind`` is set at
    session scope (survives the commit) because the §4 insert/transition guards
    and the governance audit triggers forbid anonymous writes. Returns the
    intent id; leaves the connection idle (committed) with the GUC still set."""
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute("SET app.actor_kind = 'migration'")
        cur.execute("INSERT INTO users DEFAULT VALUES RETURNING id")
        user = cur.fetchone()[0]
        cur.execute("INSERT INTO workspaces (name) VALUES ('arm-a') RETURNING id")
        ws = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO workspace_members (workspace_id, user_id, role)"
            " VALUES (%s, %s, 'owner')",
            (ws, user),
        )
        cur.execute(
            "INSERT INTO media_sources (workspace_id, provider, config)"
            " VALUES (%s, 'gdrive', '{\"v\": 1}') RETURNING id",
            (ws,),
        )
        src = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO ig_accounts (workspace_id, provider_account_ref)"
            " VALUES (%s, 'acct-ref') RETURNING id",
            (ws,),
        )
        iga = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO media_items"
            " (workspace_id, source_id, content_hash, file_name, media_kind,"
            "  provider_file_ref)"
            " VALUES (%s, %s, 'hash', 'f.jpg', 'image', 'file-ref') RETURNING id",
            (ws, src),
        )
        mi = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO post_intents"
            " (workspace_id, ig_account_id, media_item_id, provider_account_ref,"
            "  approval_mode, schedule_slot_at)"
            " VALUES (%s, %s, %s, 'acct-ref', 'manual', now()) RETURNING id",
            (ws, iga, mi),
        )
        intent = cur.fetchone()[0]
    conn.commit()
    return intent


class TestTheStreamReplays:
    def test_the_advertised_stream_installs_the_target_schema_from_empty(
        self, owner_window_db, owner_actor, admin_conn
    ):
        """The foundational proof: the extracted stream is valid, ordered SQL
        that the declared window actor can replay into an empty public. If
        this fails, the extractor or the plan's stream ordering is wrong —
        which is exactly what this arm exists to catch before M.3."""
        _replay_as_window_actor(owner_window_db, owner_actor, admin_conn)

        row = fetch_one(
            owner_window_db,
            "SELECT tableowner FROM pg_tables"
            " WHERE schemaname = 'public' AND tablename = 'post_intents'",
        )
        assert row is not None, "the stream did not create post_intents"
        assert row[0] == "svc_migration"

    def test_the_completed_target_schema_has_no_tenancy_violations(
        self, owner_window_db, owner_actor, admin_conn
    ):
        """#806's tenancy wiring, at the ONE place it is non-vacuous today.

        Fork 1's ruling required aiming `tenancy_gate` at the target lineage,
        and `test_lineage_lane.py` does that — at the migration FILES, which
        carry no table yet and will not carry all 26 until F.2.9. This replay
        carries the whole target schema NOW, so the invariant is checked at
        full strength immediately instead of accruing increment by increment.

        IT ALSO CHECKS A DIFFERENT CLAIM FROM ARM (b), which is why both exist
        rather than one being redundant. Arm (b) says the migrations reproduce
        the plan. This says THE PLAN IS CORRECT: a tenant-keyed table whose
        policy `02` simply forgot would leave arm (b) green — the migrations
        would faithfully reproduce the omission — and redden this.

        The count assertion is a POSITIVE CONTROL, not a fact about the
        schema. `tenancy_violations` returns `[]` just as readily for a schema
        it never saw, so without a floor on what was examined this would pass
        against an empty database. The floor is deliberately loose: the exact
        number is the plan's to change, and pinning it here would make a
        legitimate `02` edit fail in a file that is not about counting tables.
        """
        _replay_as_window_actor(owner_window_db, owner_actor, admin_conn)

        sig = tenancy_signature(owner_window_db)
        keyed = sorted(tenant_keyed_tables(sig))
        assert len(keyed) >= 15, (
            f"positive control: the replayed target schema carries only"
            f" {len(keyed)} tenant-keyed tables ({keyed}). Too few to be the"
            f" advertised stream's output — the assertion below would then be"
            f" passing by having nothing to examine"
        )

        assert tenancy_violations(sig) == []


class TestTheFourBehavioralAssertions:
    """§0.2: 'asserting behavior, not just syntax'. Each is a property of the
    REPLAYED database, not of the stream text."""

    def test_a_legal_intent_transition_succeeds(
        self, owner_window_db, owner_actor, admin_conn
    ):
        as_svc = _replay_as_window_actor(owner_window_db, owner_actor, admin_conn)
        conn = psycopg2.connect(as_svc)
        try:
            intent = _seed_scheduled_intent(conn)
            with conn.cursor() as cur:
                # scheduled → prompt_pending is a seeded edge (§4).
                cur.execute(
                    "UPDATE post_intents SET state = 'prompt_pending' WHERE id = %s",
                    (intent,),
                )
                conn.commit()
                cur.execute("SELECT state FROM post_intents WHERE id = %s", (intent,))
                assert cur.fetchone()[0] == "prompt_pending"
        finally:
            conn.close()

    def test_an_illegal_intent_transition_raises(
        self, owner_window_db, owner_actor, admin_conn
    ):
        as_svc = _replay_as_window_actor(owner_window_db, owner_actor, admin_conn)
        conn = psycopg2.connect(as_svc)
        try:
            intent = _seed_scheduled_intent(conn)
            with conn.cursor() as cur:
                # scheduled → posted is NOT a seeded edge — the guard trigger
                # raises with a check_violation errcode (its deliberate choice).
                # The seed and the guard are the authority; text cannot drift
                # from them (§4 comment).
                with pytest.raises(
                    psycopg2.errors.CheckViolation, match="illegal transition"
                ):
                    cur.execute(
                        "UPDATE post_intents SET state = 'posted' WHERE id = %s",
                        (intent,),
                    )
        finally:
            conn.close()

    def test_pg_policies_matches_the_normative_lists(
        self, owner_window_db, owner_actor, admin_conn
    ):
        """The two §7-DDL pattern lists the extractor GENERATES are present as
        real policies on the replayed database: p_tenant on every tenant-plane
        table, p_user_plane on every user-plane table. (The literal-only replay
        pass 6/R5 fixed left exactly these 15 absent; this proves the expansion
        installs.)"""
        _replay_as_window_actor(owner_window_db, owner_actor, admin_conn)
        manifest = load_manifest(DEFAULT_MANIFEST)

        for table in manifest.tenant_policy_tables:
            row = fetch_one(
                owner_window_db,
                "SELECT 1 FROM pg_policies WHERE schemaname = 'public'"
                " AND tablename = %s AND policyname = 'p_tenant'",
                (table,),
            )
            assert row is not None, f"p_tenant missing on {table}"
        for table in manifest.user_policy_tables:
            row = fetch_one(
                owner_window_db,
                "SELECT 1 FROM pg_policies WHERE schemaname = 'public'"
                " AND tablename = %s AND policyname = 'p_user_plane'",
                (table,),
            )
            assert row is not None, f"p_user_plane missing on {table}"

    def test_the_door_owner_create_bracket_was_exercised_then_closed(
        self, owner_window_db, owner_actor, admin_conn
    ):
        """The stream's transient bracket closed BEHIND ITSELF — proved as two
        halves, because the negative alone cannot tell 'granted then revoked'
        from 'never ran' (rajan, #784 review: with the stream stubbed out the
        pure has_schema_privilege=False assertion passed vacuously).

        EXERCISED: each door-owner system role owns the SECURITY DEFINER doors
        the stream created for it — which required the transient CREATE on
        public (`02` §7 / D40: PostgreSQL demands it for OWNER TO beyond
        membership). This half requires a real replay: on an empty or unrun
        public the role owns nothing and the assertion fails.
        CLOSED: no door-owner is left holding that CREATE."""
        _replay_as_window_actor(owner_window_db, owner_actor, admin_conn)

        for role in DOOR_OWNER_ROLES:
            owned = fetch_one(
                owner_window_db,
                "SELECT count(*) FROM pg_proc p"
                " JOIN pg_namespace n ON n.oid = p.pronamespace"
                " JOIN pg_roles r ON r.oid = p.proowner"
                " WHERE n.nspname = 'public' AND p.prosecdef AND r.rolname = %s",
                (role,),
            )
            assert owned[0] > 0, (
                f"{role} owns no SECURITY DEFINER door — the stream did not run"
                " (so there was no bracket to close), or the door-owner model"
                " changed"
            )
            priv = fetch_one(
                owner_window_db,
                "SELECT has_schema_privilege(%s, 'public', 'CREATE')",
                (role,),
            )
            assert priv[0] is False, (
                f"{role} still holds CREATE on public — the transient"
                " door-install bracket did not close"
            )
