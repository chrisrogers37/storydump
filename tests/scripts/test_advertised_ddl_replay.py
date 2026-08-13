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

    def test_no_door_owner_carries_create_on_public(
        self, owner_window_db, owner_actor, admin_conn
    ):
        """The stream's transient bracket closed behind itself: no system role
        that owns a SECURITY DEFINER door is left holding CREATE on public. A
        surviving grant would be standing privilege the door-model forbids."""
        _replay_as_window_actor(owner_window_db, owner_actor, admin_conn)

        for role in DOOR_OWNER_ROLES:
            row = fetch_one(
                owner_window_db,
                "SELECT has_schema_privilege(%s, 'public', 'CREATE')",
                (role,),
            )
            assert row[0] is False, (
                f"{role} still holds CREATE on public — the stream's transient"
                " door-install bracket did not close"
            )
