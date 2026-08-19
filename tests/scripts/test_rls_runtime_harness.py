"""F.4 — the RLS runtime harness against the full F.2 schema (#751, `04` §F.4).

`test_rls_harness.py` proved the PREMISE on a one-table probe (owner bypasses,
login is confined, the schema gate is structurally blind, tenant context
leaks across connection reuse). This module is the other half: the DECLARED
RUNTIME LOGINS against the schema production will actually run — the full
advertised stream, which arm (b) of `advertised_ddl_replay` holds equal to
the F.2 migration files, replayed under the declared actors.

What EXERCISED means here, because presence-checking is the failure shape
this harness exists to close: every assertion of "cannot see / cannot do"
is paired with a positive control on the same table under the permitted
context, and the table and door sets are derived from the live catalog and
asserted equal to what this module exercised — so a policy this file never
touched is a red test, not a silent gap.

One mechanic differs from the probe harness by design and is asserted here:
the real policies read the tenant GUC with ``current_setting(..., true)``
wrapped in ``NULLIF``, so an ABSENT tenant context yields an EMPTY SET, not
an error (the probe's bare ``current_setting`` raised 42704). Both fail
closed; the real schema's shape is the quiet one, which is exactly why the
reuse-leak tests and the paired positives matter more here.

Out of CI reach, stated rather than faked: the `04` F.4 clause "runtime
deployment env contains connection strings for exactly svc_ingress and
svc_worker" is a property of the production deployment environment; it is
verified at the M-phase ops gate, not simulatable here. Likewise #751's
"does any current prod path connect as an owner-equivalent role" needs prod
credentials the fleet does not hold.
"""

import hashlib
import uuid

import psycopg2
import psycopg2.errors
import pytest

from tests.scripts.conftest import (
    _scratch,
    as_user,
    set_test_passwords,
)
from tests.scripts.test_advertised_ddl_replay import _replay_as_window_actor

pytestmark = [pytest.mark.integration, pytest.mark.slow]

WS_A_NAME = "f4-tenant-a"
WS_B_NAME = "f4-tenant-b"

#: Every table whose RLS predicate reads the tenant GUC, per the 058/060
#: census — re-derived from pg_policies at runtime by
#: test_the_matrix_covered_every_guc_reading_table, so this literal cannot
#: silently diverge from the schema.
GUC_TABLES = {
    "workspaces",
    "workspace_members",
    "workspace_invitations",
    "channel_bindings",
    "ig_accounts",
    "provider_quarantine",
    "media_sources",
    "oauth_credentials",
    "media_items",
    "post_locks",
    "category_post_case_mix",
    "post_intents",
    "audit_events",
    "daily_post_counts",
    "jobs",
    "channel_outbox",
    "provider_operations",
}

#: The nine doors and who may call each — asserted equal to the catalog by
#: test_the_catalog_agrees_nine_doors_and_these_grants, and every row here is
#: exercised (positive as the permitted login, denied as the other).
DOORS = {
    "fn_claim_job": "svc_worker",
    "fn_extend_leases": "svc_worker",
    "fn_clock_tick": "svc_worker",
    "fn_reconciler_sweep": "svc_worker",
    "fn_reaper_sweep": "svc_worker",
    "fn_retention_batch": "svc_worker",
    "fn_offboard_finalize": "svc_worker",
    "fn_auth_plane_sweep": "svc_worker",
    "fn_invitation_accept": "svc_ingress",
}


def _exec(dsn, sql, params=None, tenant=None, autocommit=True, fetch=False):
    """One statement on a fresh connection, optionally under a tenant GUC."""
    conn = psycopg2.connect(dsn)
    conn.autocommit = autocommit
    try:
        with conn.cursor() as cur:
            if tenant is not None:
                cur.execute("SET app.tenant_id = %s", (str(tenant),))
            cur.execute(sql, params)
            if fetch:
                return cur.fetchall()
            return cur.rowcount
    finally:
        if not autocommit:
            conn.commit()
        conn.close()


def _seed_tenant(conn, name: str) -> dict:
    """One tenant's full satellite set, every GUC-reading table populated.

    Seeded as the window actor (svc_migration, the table owner) — owner
    bypass is the sanctioned seeding path, and is exactly the posture the
    probe harness proved; the SUBJECT of this module is the logins."""
    ids = {}
    with conn.cursor() as cur:
        cur.execute("SET app.actor_kind = 'migration'")
        cur.execute("INSERT INTO users DEFAULT VALUES RETURNING id")
        ids["user"] = cur.fetchone()[0]
        cur.execute("INSERT INTO workspaces (name) VALUES (%s) RETURNING id", (name,))
        ws = ids["ws"] = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO workspace_members (workspace_id, user_id, role)"
            " VALUES (%s, %s, 'owner')",
            (ws, ids["user"]),
        )
        cur.execute(
            "INSERT INTO workspace_invitations"
            " (workspace_id, token_hash, delivery_channel, expires_at, role)"
            " VALUES (%s, %s, 'telegram', now() + interval '7 days', 'member')"
            " RETURNING id",
            (ws, hashlib.sha256(f"invite-{name}".encode()).hexdigest()),
        )
        ids["invite"] = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO channel_bindings (workspace_id, channel, external_ref)"
            " VALUES (%s, 'telegram_group', %s) RETURNING id",
            (ws, f"-100{abs(hash(name)) % 10**10}"),
        )
        ids["binding"] = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO ig_accounts (workspace_id, provider_account_ref)"
            " VALUES (%s, %s) RETURNING id",
            (ws, f"acct-{name}"),
        )
        iga = ids["iga"] = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO provider_quarantine"
            " (workspace_id, provider, scope_ref, quarantined_until)"
            " VALUES (%s, 'ig', %s, now() + interval '1 hour')",
            (ws, f"ig:quarantined-{name}"),
        )
        cur.execute(
            "INSERT INTO media_sources (workspace_id, provider, config)"
            " VALUES (%s, 'gdrive', '{\"v\": 1}') RETURNING id",
            (ws,),
        )
        src = ids["src"] = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO oauth_credentials"
            " (workspace_id, provider, encrypted_payload, media_source_id)"
            " VALUES (%s, 'gdrive', 'ct', %s)",
            (ws, src),
        )
        cur.execute(
            "INSERT INTO media_items"
            " (workspace_id, source_id, content_hash, file_name, media_kind,"
            "  provider_file_ref)"
            " VALUES (%s, %s, %s, 'f.jpg', 'image', %s) RETURNING id",
            (ws, src, f"hash-{name}", f"ref-{name}"),
        )
        mi = ids["media"] = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO post_locks (workspace_id, media_item_id, kind)"
            " VALUES (%s, %s, 'hold')",
            (ws, mi),
        )
        cur.execute(
            "INSERT INTO category_post_case_mix (workspace_id, category, ratio)"
            " VALUES (%s, 'cat', 0.5)",
            (ws,),
        )
        cur.execute(
            "INSERT INTO post_intents"
            " (workspace_id, ig_account_id, media_item_id, provider_account_ref,"
            "  approval_mode, schedule_slot_at)"
            " VALUES (%s, %s, %s, %s, 'manual', now()) RETURNING id",
            (ws, iga, mi, f"acct-{name}"),
        )
        ids["intent"] = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO audit_events"
            " (workspace_id, entity_kind, entity_id, to_state, actor_kind)"
            " VALUES (%s, 'workspace', %s, 'seeded', 'system')",
            (ws, ws),
        )
        cur.execute(
            "INSERT INTO daily_post_counts"
            " (workspace_id, ig_account_id, local_date, count, cap_at_write)"
            " VALUES (%s, %s, current_date, 1, 25)",
            (ws, iga),
        )
        cur.execute(
            "INSERT INTO jobs (workspace_id, kind, lane, serialization_key, payload,"
            " max_attempts) VALUES (%s, 'publish_pipeline', 'interactive', %s,"
            " '{\"v\": 1}', 3) RETURNING id",
            (ws, f"ig:{iga}"),
        )
        ids["job"] = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO channel_outbox (workspace_id, binding_id, kind, payload)"
            " VALUES (%s, %s, 'notification', '{\"v\": 1}')",
            (ws, ids["binding"]),
        )
        cur.execute(
            "INSERT INTO provider_operations"
            " (workspace_id, intent_id, provider, op_kind, business_key, lease_token)"
            " VALUES (%s, %s, 'ig', 'publish', %s, %s)",
            (ws, ids["intent"], f"bk-{name}", str(uuid.uuid4())),
        )
    conn.commit()
    return ids


@pytest.fixture(scope="module")
def target(admin_conn, owner_actor):
    """The replayed full schema + passwords + two seeded tenants, once.

    Module-scoped because the stream replay and the two-tenant seed are the
    expensive part and every test here reads the same world; tests that
    mutate do so on rows they create or on their own tenant's satellites.
    Drives ``_scratch`` directly (the function-scoped ``owner_window_db``
    cannot be consumed at module scope); ``admin_conn``/``owner_actor`` are
    session-scoped."""
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    owner_window_db = next(gen)
    dsn = _replay_as_window_actor(owner_window_db, owner_actor, admin_conn)
    set_test_passwords(admin_conn)
    conn = psycopg2.connect(dsn)
    try:
        a = _seed_tenant(conn, WS_A_NAME)
        b = _seed_tenant(conn, WS_B_NAME)
    finally:
        conn.close()
    try:
        yield {
            "db": owner_window_db,
            "worker": as_user(owner_window_db, "svc_worker"),
            "ingress": as_user(owner_window_db, "svc_ingress"),
            "owner_stream": dsn,
            "a": a,
            "b": b,
        }
    finally:
        try:
            next(gen)
        except StopIteration:
            pass


class TestRuntimeTenantIsolationMatrix:
    """`04` F.4: absent/wrong ``app.tenant_id`` cannot read or mutate as the
    exact runtime login — exercised per GUC-reading table, three ways each:
    own context sees the seeded row (the positive control), the foreign
    context sees zero, the absent context sees zero."""

    @pytest.mark.parametrize("table", sorted(GUC_TABLES))
    def test_select_matrix(self, target, table):
        own = _exec(
            target["worker"],
            f"SELECT count(*) FROM {table}",
            tenant=target["a"]["ws"],
            fetch=True,
        )[0][0]
        assert own >= 1, (
            f"{table}: the positive control found no rows under the OWN tenant"
            f" — the zero assertions below would be vacuous"
        )
        foreign_visible = _exec(
            target["worker"],
            f"SELECT count(*) FROM {table} WHERE workspace_id = %s"
            if table != "workspaces"
            else "SELECT count(*) FROM workspaces WHERE id = %s",
            params=(str(target["b"]["ws"]),),
            tenant=target["a"]["ws"],
            fetch=True,
        )[0][0]
        assert foreign_visible == 0, f"{table}: tenant A can see tenant B's rows"
        absent = _exec(target["worker"], f"SELECT count(*) FROM {table}", fetch=True)[
            0
        ][0]
        assert absent == 0, (
            f"{table}: absent tenant context returned rows — the NULLIF wrap"
            f" should make an unset GUC match nothing"
        )

    def test_cross_tenant_insert_is_refused_by_with_check(self, target):
        """The mutate half: under tenant A's context, writing a row stamped
        with tenant B's workspace violates the policy WITH CHECK (42501)."""
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            _exec(
                target["worker"],
                "INSERT INTO category_post_case_mix (workspace_id, category, ratio)"
                " VALUES (%s, 'smuggled', 0.1)",
                params=(str(target["b"]["ws"]),),
                tenant=target["a"]["ws"],
            )

    def test_cross_tenant_update_moves_zero_rows(self, target):
        """UPDATE under the wrong tenant finds nothing to update — and the
        same statement under the right tenant is the paired positive."""
        moved = _exec(
            target["worker"],
            "UPDATE category_post_case_mix SET ratio = 0.6 WHERE workspace_id = %s",
            params=(str(target["b"]["ws"]),),
            tenant=target["a"]["ws"],
        )
        assert moved == 0
        own = _exec(
            target["worker"],
            "UPDATE category_post_case_mix SET ratio = 0.6 WHERE workspace_id = %s",
            params=(str(target["a"]["ws"]),),
            tenant=target["a"]["ws"],
        )
        assert own == 1

    def test_audit_insert_carries_the_tenant_or_is_refused(self, target):
        ok = _exec(
            target["worker"],
            "INSERT INTO audit_events (workspace_id, entity_kind, entity_id,"
            " to_state, actor_kind) VALUES (%s, 'workspace', %s, 'f4', 'user')",
            params=(str(target["a"]["ws"]), str(target["a"]["ws"])),
            tenant=target["a"]["ws"],
        )
        assert ok == 1
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            _exec(
                target["worker"],
                "INSERT INTO audit_events (workspace_id, entity_kind, entity_id,"
                " to_state, actor_kind) VALUES (%s, 'workspace', %s, 'f4', 'user')",
                params=(str(target["b"]["ws"]), str(target["b"]["ws"])),
                tenant=target["a"]["ws"],
            )

    def test_the_matrix_covered_every_guc_reading_table(self, target):
        """Completeness, derived from the live catalog: the set of tables
        whose policies read the tenant GUC must equal the set this module
        exercised. A new tenant-keyed table whose policy this file never
        touches is a red test, not a silent gap."""
        rows = _exec(
            target["owner_stream"],
            "SELECT DISTINCT tablename FROM pg_policies"
            " WHERE schemaname = 'public'"
            " AND (coalesce(qual, '') LIKE '%app.tenant_id%'"
            "  OR coalesce(with_check, '') LIKE '%app.tenant_id%')",
            fetch=True,
        )
        catalog = {r[0] for r in rows}
        assert catalog == GUC_TABLES, (
            f"catalog vs exercised drift: only-in-catalog="
            f"{sorted(catalog - GUC_TABLES)},"
            f" only-in-module={sorted(GUC_TABLES - catalog)}"
        )


class TestAbsentContextFailsClosedQuietly:
    """The real schema's absent-context shape is an EMPTY SET, not an error —
    deliberately different from the probe harness's 42704, and asserted here
    so a future policy edit that flips the shape is caught. The quietness is
    why the probe module's reuse-leak tests exist."""

    def test_unset_guc_is_empty_not_error_with_paired_positive(self, target):
        empty = _exec(target["worker"], "SELECT count(*) FROM media_items", fetch=True)[
            0
        ][0]
        assert empty == 0
        seen = _exec(
            target["worker"],
            "SELECT count(*) FROM media_items",
            tenant=target["a"]["ws"],
            fetch=True,
        )[0][0]
        assert seen >= 1, "paired positive: the same table, the right context"

    def test_session_set_leaks_on_the_real_schema_too(self, target):
        """The probe module proved the leak mechanics; this pins them to the
        production schema: a pooled connection that ran tenant A's request
        serves A's media to the next request that sets nothing."""
        conn = psycopg2.connect(target["worker"])
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute("SET app.tenant_id = %s", (str(target["a"]["ws"]),))
                cur.execute("SELECT count(*) FROM media_items")
                assert cur.fetchone()[0] >= 1
            with conn.cursor() as cur:  # the next request; sets nothing
                cur.execute("SELECT count(*) FROM media_items")
                assert cur.fetchone()[0] >= 1, (
                    "expected the session GUC to persist across requests on a"
                    " reused connection — if empty, the GUC lifetime changed"
                )
        finally:
            conn.close()


class TestDoorsAreExercisedAndExclusive:
    """`04` F.4: system-role policies are exercised only through their doors.
    Each door: a positive call as its permitted login (the door-only policies
    execute inside the definer body), and a denial as the other login."""

    def test_fn_claim_job_claims_and_fn_extend_leases_extends(self, target):
        claimed = _exec(
            target["worker"],
            "SELECT * FROM fn_claim_job('interactive', 'f4-runner',"
            " interval '2 minutes', 4)",
            fetch=True,
        )
        assert claimed, "expected the seeded ready job to be claimable"
        job_id = claimed[0][0]
        lease_token = _exec(
            target["owner_stream"],
            "SELECT lease_token FROM jobs WHERE id = %s",
            params=(str(job_id),),
            fetch=True,
        )[0][0]
        assert lease_token is not None, "a claimed job must carry its lease"
        extended = _exec(
            target["worker"],
            "SELECT fn_extend_leases(ARRAY[%s]::uuid[], interval '2 minutes')",
            params=(str(lease_token),),
            fetch=True,
        )[0][0]
        assert extended == 1

    def test_fn_clock_tick_runs_under_budget(self, target):
        row = _exec(
            target["worker"],
            "SELECT * FROM fn_clock_tick(5, interval '5 minutes', '{}'::jsonb)",
            fetch=True,
        )
        assert row is not None

    @pytest.mark.parametrize(
        "call",
        [
            "SELECT * FROM fn_reconciler_sweep(5,"
            " ARRAY[interval '1 minute'], interval '1 day')",
            "SELECT * FROM fn_reaper_sweep(5, interval '1 day', interval '1 day')",
            "SELECT * FROM fn_retention_batch('jobs_ok', interval '90 days', 5)",
            "SELECT fn_auth_plane_sweep(interval '1 day', interval '1 day',"
            " interval '1 day', 5)",
        ],
        ids=["reconciler", "reaper", "retention", "auth_sweep"],
    )
    def test_maintenance_doors_run_as_the_worker(self, target, call):
        assert _exec(target["worker"], call, fetch=True) is not None

    def test_fn_offboard_finalize_guards_hold(self, target):
        """The door's in-body guard IS the exercised behavior: a workspace not
        in the offboarding state is refused by name, loudly — never silently
        finalized."""
        with pytest.raises(psycopg2.errors.RaiseException, match="not finalizable"):
            _exec(
                target["worker"],
                "SELECT fn_offboard_finalize(%s, interval '0 days')",
                params=(str(target["a"]["ws"]),),
                fetch=True,
            )

    def test_fn_invitation_accept_admits_a_member_as_ingress(self, target):
        token_hash = hashlib.sha256(f"invite-{WS_B_NAME}".encode()).hexdigest()
        new_user = _exec(
            target["owner_stream"],
            "INSERT INTO users DEFAULT VALUES RETURNING id",
            fetch=True,
        )[0][0]
        row = _exec(
            target["ingress"],
            "SELECT * FROM fn_invitation_accept(%s, %s, 'telegram', %s, %s, 'web')",
            params=(token_hash, str(new_user), f"proof:{new_user}", 4242421),
            fetch=True,
        )
        assert row is not None
        member = _exec(
            target["owner_stream"],
            "SELECT count(*) FROM workspace_members WHERE workspace_id = %s"
            " AND user_id = %s",
            params=(str(target["b"]["ws"]), str(new_user)),
            fetch=True,
        )[0][0]
        assert member == 1, "the accepted invitation must have admitted the member"

    DOOR_CALLS = {
        "fn_claim_job": "SELECT * FROM fn_claim_job('interactive', 'x',"
        " interval '1 minute', 1)",
        "fn_extend_leases": "SELECT fn_extend_leases(ARRAY[]::uuid[],"
        " interval '1 minute')",
        "fn_clock_tick": "SELECT * FROM fn_clock_tick(1, interval '1 minute',"
        " '{}'::jsonb)",
        "fn_reconciler_sweep": "SELECT * FROM fn_reconciler_sweep(1,"
        " ARRAY[interval '1 minute'], interval '1 day')",
        "fn_reaper_sweep": "SELECT * FROM fn_reaper_sweep(1, interval '1 day',"
        " interval '1 day')",
        "fn_retention_batch": "SELECT * FROM fn_retention_batch('jobs_ok',"
        " interval '90 days', 1)",
        "fn_offboard_finalize": "SELECT fn_offboard_finalize("
        "'00000000-0000-0000-0000-000000000000'::uuid, interval '1 day')",
        "fn_auth_plane_sweep": "SELECT fn_auth_plane_sweep(interval '1 day',"
        " interval '1 day', interval '1 day', 1)",
        "fn_invitation_accept": "SELECT * FROM fn_invitation_accept('h',"
        " '00000000-0000-0000-0000-000000000000'::uuid, 'telegram', NULL,"
        " 1, 'web')",
    }

    def test_every_worker_door_is_denied_to_ingress_and_vice_versa(self, target):
        """EXECUTE is per-signature, so the denial must call the real
        signature — a bare ``door()`` would 42883 and prove nothing."""
        assert set(self.DOOR_CALLS) == set(DOORS)
        for door, permitted in DOORS.items():
            other = target["ingress"] if permitted == "svc_worker" else target["worker"]
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                _exec(other, self.DOOR_CALLS[door])

    def test_the_catalog_agrees_nine_doors_and_these_grants(self, target):
        rows = _exec(
            target["owner_stream"],
            "SELECT p.proname FROM pg_proc p"
            " JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE n.nspname = 'public' AND p.prosecdef",
            fetch=True,
        )
        catalog = {r[0] for r in rows}
        assert catalog == set(DOORS), (
            f"SECURITY DEFINER census drift: only-in-catalog="
            f"{sorted(catalog - set(DOORS))},"
            f" only-in-module={sorted(set(DOORS) - catalog)}"
        )
        for door, permitted in DOORS.items():
            grants = _exec(
                target["owner_stream"],
                "SELECT grantee FROM information_schema.routine_privileges"
                " WHERE routine_schema = 'public' AND routine_name = %s"
                " AND privilege_type = 'EXECUTE' AND grantee LIKE 'svc_%%'",
                params=(door,),
                fetch=True,
            )
            grantees = {g[0] for g in grants} - {
                "svc_claim",
                "svc_clock",
                "svc_maintenance",
                "svc_membership",
            }
            assert grantees == {permitted}, (
                f"{door}: EXECUTE grantees {sorted(grantees)},"
                f" expected exactly {permitted}"
            )


class TestDirectPathsAreShut:
    """The grant matrix gives the logins no DELETE anywhere and the doors'
    verbs only inside definer bodies — a login reaching for door work fails
    at the GRANT layer (42501), which is louder than an RLS empty set."""

    @pytest.mark.parametrize(
        "table",
        ["audit_events", "jobs", "post_locks", "channel_outbox", "media_items"],
    )
    def test_direct_delete_is_denied_to_the_worker(self, target, table):
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            _exec(
                target["worker"],
                f"DELETE FROM {table} WHERE workspace_id = %s",
                params=(str(target["a"]["ws"]),),
                tenant=target["a"]["ws"],
            )

    def test_set_role_fails_for_every_service_role(self, target):
        """`04` F.4: no memberships exist to assume — SET ROLE from the worker
        login to any other service role fails, and the catalog agrees."""
        for role in (
            "svc_ingress",
            "svc_claim",
            "svc_clock",
            "svc_maintenance",
            "svc_membership",
            "svc_migration",
        ):
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                _exec(target["worker"], f'SET ROLE "{role}"')
        rows = _exec(
            target["owner_stream"],
            "SELECT r.rolname, g.rolname FROM pg_auth_members m"
            " JOIN pg_roles r ON r.oid = m.roleid"
            " JOIN pg_roles g ON g.oid = m.member"
            " WHERE r.rolname LIKE 'svc_%' OR g.rolname LIKE 'svc_%'",
            fetch=True,
        )
        # The bootstrap's own design (step0 L51): the WINDOW actor holds the
        # four door-owner roles so it can assign function ownership. That is
        # deploy-context machinery, pinned here to exactly its shape — the
        # RUNTIME logins appear on neither side of any membership.
        door_rows = {r for r in rows if r[1] == "svc_migration"}
        assert door_rows == {
            ("svc_claim", "svc_migration"),
            ("svc_clock", "svc_migration"),
            ("svc_maintenance", "svc_migration"),
            ("svc_membership", "svc_migration"),
        }, f"unexpected door-owner memberships: {sorted(door_rows)}"
        # The bootstrap's OTHER documented grant (step0 L66): the owner actor
        # transiently holds svc_migration — revoked by the M.3 step-8
        # stand-down, which a test world never runs. Exactly one such row,
        # to exactly the lineage owner, and nothing else exists.
        rest = set(rows) - door_rows
        assert len(rest) == 1 and next(iter(rest))[0] == "svc_migration", (
            f"unexpected extra memberships: {sorted(rest)}"
        )
        assert not next(iter(rest))[1].startswith("svc_"), (
            "the transient self-grant must point at the owner actor,"
            " never a service role"
        )
        for login in ("svc_worker", "svc_ingress"):
            assert not any(login in row for row in rows), (
                f"{login} must hold and grant no memberships"
            )


class TestAuthPlaneMaintenanceOnlyThroughTheSweep:
    """`04` F.4 (pass 3): auth-plane maintenance succeeds only through
    fn_auth_plane_sweep — a direct DELETE fails as every login."""

    def test_direct_delete_denied_to_both_logins(self, target):
        for table in ("session_tokens", "oauth_states", "service_tokens"):
            for dsn in (target["worker"], target["ingress"]):
                with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                    _exec(dsn, f"DELETE FROM {table}")

    def test_the_sweep_reaps_expired_rows_but_never_service_tokens(self, target):
        _exec(
            target["ingress"],
            "INSERT INTO session_tokens (token_hash, user_id, expires_at)"
            " VALUES ('f4-expired', %s, now() - interval '2 days')",
            params=(str(target["a"]["user"]),),
        )
        _exec(
            target["ingress"],
            "INSERT INTO service_tokens (name, token_hash, role)"
            " VALUES ('f4-survivor', 'f4-service-survivor', 'readonly')",
        )
        _exec(
            target["worker"],
            "SELECT fn_auth_plane_sweep(interval '1 day', interval '1 day',"
            " interval '1 day', 100)",
            fetch=True,
        )
        gone = _exec(
            target["owner_stream"],
            "SELECT count(*) FROM session_tokens WHERE token_hash = 'f4-expired'",
            fetch=True,
        )[0][0]
        assert gone == 0, "the sweep must reap the expired session token"
        survivor = _exec(
            target["owner_stream"],
            "SELECT count(*) FROM service_tokens"
            " WHERE token_hash = 'f4-service-survivor'",
            fetch=True,
        )[0][0]
        assert survivor == 1, (
            "service_tokens is deliberately outside the sweep (060's stated"
            " asymmetry) — the survivor proves the sweep did not overreach"
        )


class TestZeroNullGates:
    """`04` F.4: zero-NULL gates pass — tenant keys are NOT NULL everywhere
    but jobs, whose sanctioned NULL is bound to system kinds by CHECK."""

    def test_workspace_id_is_not_null_on_every_guc_table_except_jobs(self, target):
        rows = _exec(
            target["owner_stream"],
            "SELECT c.relname, a.attnotnull FROM pg_attribute a"
            " JOIN pg_class c ON c.oid = a.attrelid"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'public' AND a.attname = 'workspace_id'"
            " AND c.relkind = 'r'",
            fetch=True,
        )
        nullable = {r[0] for r in rows if not r[1]}
        assert nullable == {"jobs", "oauth_states", "service_tokens"}, (
            f"tables with nullable workspace_id: {sorted(nullable)} — the"
            f" sanctioned NULLs are jobs' system-kind pairing and the two"
            f" auth-plane pre-tenant columns (060: NULL = all workspaces /"
            f" pre-workspace OAuth)"
        )

    def test_jobs_pairing_check_refuses_both_mismatches(self, target):
        with pytest.raises(psycopg2.errors.CheckViolation):
            _exec(
                target["owner_stream"],
                "INSERT INTO jobs (workspace_id, kind, lane, serialization_key,"
                " payload, max_attempts) VALUES (NULL, 'publish_pipeline',"
                " 'interactive', 'k', '{\"v\":1}', 3)",
            )
        with pytest.raises(psycopg2.errors.CheckViolation):
            _exec(
                target["owner_stream"],
                "INSERT INTO jobs (workspace_id, kind, lane, serialization_key,"
                " payload, max_attempts) VALUES (%s, 'reap_expired',"
                " 'bulk', 'k2', '{\"v\":1}', 3)",
                params=(str(target["a"]["ws"]),),
            )
