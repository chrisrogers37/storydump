"""F.4 — the RLS runtime harness against the full F.2 schema (#751, `04` §F.4).

`test_rls_harness.py` proved the PREMISE on a one-table probe (owner bypasses,
login confined, the schema gate structurally blind, tenant context leaks
across connection reuse). This module is the other half: the DECLARED RUNTIME
LOGINS against the schema production will run — the full advertised stream,
which arm (b) of `advertised_ddl_replay` holds equal to the F.2 migration
files, replayed under the declared actors.

EXERCISED, not asserted-present: the policy census below is every row of
`pg_policies` (58), asserted equal to the live catalog at (table, cmd, roles)
grain — so a smuggled permissive policy on an already-covered table is a red
test — and every census row carries a DISPOSITION naming how this module
drives it (matrix read+write as both logins, a door's definer body, a direct
machinery probe) or disclosing that it is evaluated-but-not-effect-driven in
this world (the zero-row sweeps). Every zero/denial assertion is paired with
a positive control on its own axis.

Two schema truths asserted rather than assumed:
- ABSENT tenant context yields an EMPTY SET on the tenant tables (the
  policies read the GUC via ``NULLIF(current_setting(..., true), '')``) —
  deliberately different from the probe harness's 42704 — EXCEPT ``jobs``,
  whose policy is ``workspace_id = T OR workspace_id IS NULL`` by design:
  absent context sees exactly the system rows, and the matrix pins that.
- Governance mutations (workspaces, workspace_members, channel_bindings,
  ig_accounts, oauth_credentials) require the actor GUCs; write probes set
  them the way every runtime unit of work does.

Out of CI reach, stated rather than faked: the `04` F.4 clause "runtime
deployment env contains connection strings for exactly svc_ingress and
svc_worker" is a deployment property (M-phase ops gate), and #751's "does
any current prod path connect as an owner-equivalent role" needs prod
credentials the fleet does not hold.
"""

import hashlib
import uuid

import psycopg2
import psycopg2.errors
import pytest

from tests.scripts.conftest import (
    NOLOGIN_ROLES,
    SERVICE_ROLES,
    _scratch,
    as_user,
    replay_advertised_stream,
    seed_workspace_chain,
    set_test_passwords,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

WS_A_NAME = "f4-tenant-a"
WS_B_NAME = "f4-tenant-b"

LOGINS = ("svc_worker", "svc_ingress")
T = ("svc_ingress", "svc_worker")  # the tenant-policy TO-list, alphabetical

#: THE ORACLE — every policy in 058/060 at (policy, table, cmd, roles) grain,
#: with its disposition in this module. Asserted equal to pg_policies by
#: test_the_census_matches_the_catalog_exactly; dispositions are asserted
#: complete (every row has one) and honest (the disclosed set is pinned).
#: Disposition vocabulary:
#:   matrix        — read AND write driven as both logins via the isolation matrix
#:   matrix-read   — read-only policy driven via the matrix SELECT leg
#:   insert-pair   — INSERT policy driven by an own-succeeds/foreign-refused pair
#:   door:<fn>     — evaluated inside that SECURITY DEFINER body; "armed" doors
#:                   move rows under it, the rest evaluate it over this world
#:   machinery     — driven by a direct probe as the granted login
#:   auth          — driven by the auth-plane tests (ingress inserts + sweep)
POLICY_CENSUS = {
    ("p_tenant_workspaces", "workspaces", "ALL", T): "matrix",
    ("p_clock_ws", "workspaces", "SELECT", ("svc_clock",)): "door:fn_clock_tick",
    (
        "p_maint_ws",
        "workspaces",
        "ALL",
        ("svc_maintenance",),
    ): "door:fn_offboard_finalize",
    (
        "p_member_ws",
        "workspaces",
        "SELECT",
        ("svc_membership",),
    ): "door:fn_invitation_accept",
    ("p_tenant", "workspace_members", "ALL", T): "matrix",
    (
        "p_member_members",
        "workspace_members",
        "ALL",
        ("svc_membership",),
    ): "door:fn_invitation_accept",
    ("p_tenant", "workspace_invitations", "ALL", T): "matrix",
    (
        "p_maint_invites",
        "workspace_invitations",
        "ALL",
        ("svc_maintenance",),
    ): "door:fn_reaper_sweep",
    (
        "p_member_invites",
        "workspace_invitations",
        "ALL",
        ("svc_membership",),
    ): "door:fn_invitation_accept",
    ("p_user_plane", "users", "ALL", T): "matrix-userplane",
    ("p_user_plane", "user_identities", "ALL", T): "matrix-userplane",
    ("p_user_plane", "onboarding_sessions", "ALL", T): "matrix-userplane",
    (
        "p_maint_onboard",
        "onboarding_sessions",
        "ALL",
        ("svc_maintenance",),
    ): "door:fn_reaper_sweep",
    ("p_tenant", "channel_bindings", "ALL", T): "matrix",
    (
        "p_member_binds",
        "channel_bindings",
        "SELECT",
        ("svc_membership",),
    ): "door:fn_invitation_accept",
    ("p_tenant", "ig_accounts", "ALL", T): "matrix",
    ("p_clock_acct_s", "ig_accounts", "SELECT", ("svc_clock",)): "door:fn_clock_tick",
    ("p_clock_acct_u", "ig_accounts", "UPDATE", ("svc_clock",)): "door:fn_clock_tick",
    ("p_tenant", "provider_quarantine", "ALL", T): "matrix",
    (
        "p_claim_quar",
        "provider_quarantine",
        "SELECT",
        ("svc_claim",),
    ): "door:fn_claim_job",
    ("p_tenant", "media_sources", "ALL", T): "matrix",
    ("p_clock_src_s", "media_sources", "SELECT", ("svc_clock",)): "door:fn_clock_tick",
    ("p_clock_src_u", "media_sources", "UPDATE", ("svc_clock",)): "door:fn_clock_tick",
    ("p_tenant", "oauth_credentials", "ALL", T): "matrix",
    (
        "p_clock_cred_s",
        "oauth_credentials",
        "SELECT",
        ("svc_clock",),
    ): "door:fn_clock_tick",
    (
        "p_clock_cred_u",
        "oauth_credentials",
        "UPDATE",
        ("svc_clock",),
    ): "door:fn_clock_tick",
    ("p_tenant", "media_items", "ALL", T): "matrix",
    ("p_tenant", "post_locks", "ALL", T): "matrix",
    (
        "p_maint_locks",
        "post_locks",
        "ALL",
        ("svc_maintenance",),
    ): "door:fn_reaper_sweep",
    ("p_tenant", "category_post_case_mix", "ALL", T): "matrix",
    ("p_tenant", "post_intents", "ALL", T): "matrix",
    (
        "p_maint_intents",
        "post_intents",
        "ALL",
        ("svc_maintenance",),
    ): "door:fn_reconciler_sweep",
    (
        "p_transitions_read",
        "post_intent_transitions",
        "SELECT",
        (
            "svc_claim",
            "svc_clock",
            "svc_ingress",
            "svc_maintenance",
            "svc_membership",
            "svc_worker",
        ),
    ): "matrix-read",
    ("p_audit_ins", "audit_events", "INSERT", T): "insert-pair",
    ("p_audit_sel", "audit_events", "SELECT", T): "matrix-read",
    (
        "p_audit_sys",
        "audit_events",
        "INSERT",
        ("svc_claim", "svc_clock", "svc_maintenance", "svc_membership"),
    ): "door:fn_invitation_accept",
    (
        "p_audit_retention_sel",
        "audit_events",
        "SELECT",
        ("svc_maintenance",),
    ): "door:fn_retention_batch",
    (
        "p_audit_retention_del",
        "audit_events",
        "DELETE",
        ("svc_maintenance",),
    ): "door:fn_retention_batch",
    ("p_tenant", "daily_post_counts", "ALL", T): "matrix",
    (
        "p_maint_dpc",
        "daily_post_counts",
        "ALL",
        ("svc_maintenance",),
    ): "door:fn_retention_batch",
    ("p_jobs", "jobs", "ALL", T): "matrix",
    ("p_claim_jobs", "jobs", "ALL", ("svc_claim",)): "door:fn_claim_job",
    ("p_clock_jobs", "jobs", "ALL", ("svc_clock",)): "door:fn_clock_tick",
    ("p_maint_jobs", "jobs", "ALL", ("svc_maintenance",)): "door:fn_retention_batch",
    ("p_tenant", "channel_outbox", "ALL", T): "matrix",
    (
        "p_maint_outbox",
        "channel_outbox",
        "ALL",
        ("svc_maintenance",),
    ): "door:fn_retention_batch",
    (
        "p_member_outbox",
        "channel_outbox",
        "INSERT",
        ("svc_membership",),
    ): "door:fn_invitation_accept",
    ("p_tenant", "provider_operations", "ALL", T): "matrix",
    (
        "p_maint_ops",
        "provider_operations",
        "ALL",
        ("svc_maintenance",),
    ): "door:fn_retention_batch",
    ("p_dedup", "command_dedup", "ALL", ("svc_ingress",)): "machinery",
    (
        "p_maint_dedup",
        "command_dedup",
        "ALL",
        ("svc_maintenance",),
    ): "door:fn_auth_plane_sweep",
    ("p_rate", "rate_counters", "ALL", T): "machinery",
    (
        "p_maint_rate",
        "rate_counters",
        "ALL",
        ("svc_maintenance",),
    ): "door:fn_retention_batch",
    ("p_auth_ingress_sessions", "session_tokens", "ALL", ("svc_ingress",)): "auth",
    ("p_auth_sweep_sessions", "session_tokens", "ALL", ("svc_maintenance",)): "auth",
    ("p_auth_ingress_states", "oauth_states", "ALL", ("svc_ingress",)): "auth",
    ("p_auth_sweep_states", "oauth_states", "ALL", ("svc_maintenance",)): "auth",
    ("p_auth_ingress_svctok", "service_tokens", "ALL", ("svc_ingress",)): "auth",
}

#: The tenant-GUC tables (policies whose predicate reads app.tenant_id),
#: derived from the census so there is one oracle, not two.
GUC_TABLES = sorted(
    {
        table
        for (_, table, _, roles), _d in POLICY_CENSUS.items()
        if roles == T
        and table
        not in ("users", "user_identities", "onboarding_sessions", "rate_counters")
    }
    | {"audit_events"}
)

#: Tables whose ALL-policy rows the matrix WRITE leg drives (self-assign
#: UPDATE). audit_events is INSERT/SELECT-only for the logins by grant.
MATRIX_WRITE_TABLES = sorted(set(GUC_TABLES) - {"audit_events"})

#: Governance tables (055's tg_audit_* attach list): mutations need actors.
GOVERNANCE = {
    "workspaces",
    "workspace_members",
    "channel_bindings",
    "ig_accounts",
    "oauth_credentials",
}

#: Doors: name -> (permitted login, exercising call). One registry, so the
#: pairing cannot drift; the catalog census asserts the keyset and grants.
DOORS = {
    "fn_claim_job": (
        "svc_worker",
        "SELECT * FROM fn_claim_job('interactive', 'f4-runner', interval '2 minutes', 4)",
    ),
    "fn_extend_leases": (
        "svc_worker",
        "SELECT fn_extend_leases(ARRAY[]::uuid[], interval '2 minutes')",
    ),
    "fn_clock_tick": (
        "svc_worker",
        "SELECT * FROM fn_clock_tick(5, interval '5 minutes',"
        " '{\"reap_expired\": 1}'::jsonb)",
    ),
    "fn_reconciler_sweep": (
        "svc_worker",
        "SELECT * FROM fn_reconciler_sweep(5, ARRAY[interval '1 minute'], interval '1 day')",
    ),
    "fn_reaper_sweep": (
        "svc_worker",
        "SELECT * FROM fn_reaper_sweep(5, interval '1 day', interval '1 day')",
    ),
    "fn_retention_batch": (
        "svc_worker",
        "SELECT * FROM fn_retention_batch('jobs_ok', interval '90 days', 5)",
    ),
    "fn_offboard_finalize": (
        "svc_worker",
        "SELECT fn_offboard_finalize('00000000-0000-0000-0000-000000000000'::uuid, interval '1 day')",
    ),
    "fn_auth_plane_sweep": (
        "svc_worker",
        "SELECT fn_auth_plane_sweep(interval '1 day', interval '1 day', interval '1 day', 100)",
    ),
    "fn_invitation_accept": (
        "svc_ingress",
        "SELECT * FROM fn_invitation_accept('nope',"
        " '00000000-0000-0000-0000-000000000000'::uuid, 'telegram', NULL, 1, 'web')",
    ),
    # The tenth door (064, #1037): a user-plane READ, so its exercising call
    # carries no arguments — the caller is app.actor_user_id, read inside the
    # body, and an unclaimed session reads zero rows rather than anyone's.
    "fn_memberships_for_caller": (
        "svc_ingress",
        "SELECT * FROM fn_memberships_for_caller()",
    ),
}


def _exec(dsn, sql, params=None, tenant=None, actor=False, fetch=False):
    """One statement on a fresh connection. ``tenant`` sets the tenant GUC;
    ``actor`` additionally sets the actor GUCs the way every runtime unit of
    work does (governance triggers refuse anonymous mutations)."""
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            if tenant is not None:
                cur.execute("SET app.tenant_id = %s", (str(tenant),))
            if actor:
                cur.execute("SET app.actor_kind = 'user'")
                cur.execute("SET app.actor_user_id = %s", (str(uuid.uuid4()),))
            cur.execute(sql, params)
            if fetch:
                return cur.fetchall()
            return cur.rowcount
    finally:
        conn.close()


def _scalar(dsn, sql, params=None, tenant=None):
    return _exec(dsn, sql, params=params, tenant=tenant, fetch=True)[0][0]


def _external_ref(name: str) -> str:
    return "-100" + str(int(hashlib.sha256(name.encode()).hexdigest()[:8], 16))


def _seed_tenant(conn, name: str) -> dict:
    """One tenant's satellite set over the shared parent chain — every
    GUC-reading table populated. Seeded as the window actor (the table
    owner); owner bypass is the sanctioned seeding path the probe harness
    proved, and the SUBJECT here is the logins."""
    ids = seed_workspace_chain(conn, name)
    ws, iga, mi = ids["ws"], ids["iga"], ids["media"]
    ids["invite_hash"] = hashlib.sha256(f"invite-{name}".encode()).hexdigest()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO workspace_invitations"
            " (workspace_id, token_hash, delivery_channel, expires_at, role)"
            " VALUES (%s, %s, 'telegram', now() + interval '7 days', 'member')",
            (ws, ids["invite_hash"]),
        )
        cur.execute(
            "INSERT INTO channel_bindings (workspace_id, channel, external_ref)"
            " VALUES (%s, 'telegram_group', %s)",
            (ws, _external_ref(name)),
        )
        cur.execute(
            "INSERT INTO provider_quarantine"
            " (workspace_id, provider, scope_ref, quarantined_until)"
            " VALUES (%s, 'ig', %s, now() + interval '1 hour')",
            (ws, f"ig:quarantined-{name}"),
        )
        cur.execute(
            "INSERT INTO oauth_credentials"
            " (workspace_id, provider, encrypted_payload, media_source_id)"
            " VALUES (%s, 'gdrive', 'ct', %s)",
            (ws, ids["src"]),
        )
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
            "INSERT INTO jobs (workspace_id, kind, lane, serialization_key,"
            " payload, max_attempts) VALUES (%s, 'publish_pipeline',"
            " 'interactive', %s, '{\"v\": 1}', 3)",
            (ws, f"ig:{iga}"),
        )
        cur.execute("SELECT id FROM channel_bindings WHERE workspace_id = %s", (ws,))
        binding = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO channel_outbox (workspace_id, binding_id, kind, payload)"
            " VALUES (%s, %s, 'notification', '{\"v\": 1}')",
            (ws, binding),
        )
        cur.execute(
            "INSERT INTO provider_operations"
            " (workspace_id, intent_id, provider, op_kind, business_key, lease_token)"
            " VALUES (%s, %s, 'ig', 'publish', %s, %s)",
            (ws, ids["intent"], f"bk-{name}", str(uuid.uuid4())),
        )
    conn.commit()
    return ids


def _arm_world(conn, a: dict) -> None:
    """Rows that make the armed doors do observable work: a system job (also
    the p_jobs OR-IS-NULL pin), a slot-due ig_account for the clock, and a
    retention-eligible succeeded job (updated_at is writable at INSERT — the
    touch trigger is BEFORE UPDATE only)."""
    with conn.cursor() as cur:
        cur.execute("SET app.actor_kind = 'migration'")
        cur.execute(
            "INSERT INTO jobs (workspace_id, kind, lane, serialization_key,"
            " payload, max_attempts) VALUES (NULL, 'reap_expired', 'bulk',"
            " 'system:reap', '{\"v\": 1}', 3)"
        )
        cur.execute(
            "UPDATE ig_accounts SET next_slot_at = now() - interval '1 minute'"
            " WHERE id = %s",
            (a["iga"],),
        )
        # System row (NULL workspace) ON PURPOSE: the matrix write leg
        # self-assigns every tenant row, and the touch trigger would reset
        # this row's age — order-dependence measured, not theorized.
        cur.execute(
            "INSERT INTO jobs (workspace_id, kind, lane, serialization_key,"
            " payload, max_attempts, state, updated_at)"
            " VALUES (NULL, 'retention_sweep', 'bulk', %s, '{\"v\": 1}', 3,"
            " 'succeeded', now() - interval '365 days')",
            (f"old:{uuid.uuid4()}",),
        )
    conn.commit()


@pytest.fixture(scope="module")
def target(admin_conn, owner_actor):
    """The replayed full schema + passwords + two seeded tenants, once.

    Module-scoped (the per-test template idiom cannot hold a role-carrying
    template: grants to cluster roles make drop_service_roles destroy it —
    see conftest's hardened-drop notes). ``_scratch`` is driven directly the
    way ``roleless_db`` does; everything after the first ``next`` sits inside
    the try so a failed replay or seed cannot leak the scratch DB and roles
    into the session (the documented cascade class)."""
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    owner_window_db = next(gen)
    try:
        dsn = replay_advertised_stream(owner_window_db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        conn = psycopg2.connect(dsn)
        try:
            a = _seed_tenant(conn, WS_A_NAME)
            b = _seed_tenant(conn, WS_B_NAME)
            _arm_world(conn, a)
        finally:
            conn.close()
        yield {
            "worker": as_user(owner_window_db, "svc_worker"),
            "ingress": as_user(owner_window_db, "svc_ingress"),
            "owner_stream": dsn,
            "a": a,
            "b": b,
        }
    finally:
        gen.close()


def _login_dsn(target, login):
    return target["worker"] if login == "svc_worker" else target["ingress"]


class TestRuntimeTenantIsolationMatrix:
    """`04` F.4: absent/wrong ``app.tenant_id`` cannot read or mutate as the
    exact runtime logins — driven per GUC-reading table, per login, read AND
    write, each zero paired with a positive on the same axis."""

    @pytest.mark.parametrize("login", LOGINS)
    @pytest.mark.parametrize("table", GUC_TABLES)
    def test_select_matrix(self, target, table, login):
        dsn = _login_dsn(target, login)
        key = "id" if table == "workspaces" else "workspace_id"
        own = _scalar(dsn, f"SELECT count(*) FROM {table}", tenant=target["a"]["ws"])
        assert own >= 1, (
            f"{table}/{login}: positive control found no rows under the OWN"
            f" tenant — the zero assertions would be vacuous"
        )
        foreign = _scalar(
            dsn,
            f"SELECT count(*) FROM {table} WHERE {key} = %s",
            params=(str(target["b"]["ws"]),),
            tenant=target["a"]["ws"],
        )
        assert foreign == 0, f"{table}/{login}: tenant A can see tenant B's rows"
        absent = _scalar(dsn, f"SELECT count(*) FROM {table}")
        if table == "jobs":
            # p_jobs is `workspace_id = T OR workspace_id IS NULL` BY DESIGN:
            # absent context sees exactly the system rows — pinned, not
            # accidentally satisfied by a world with no system jobs.
            system = _scalar(
                target["owner_stream"],
                "SELECT count(*) FROM jobs WHERE workspace_id IS NULL",
            )
            assert system >= 1, "the armed world must carry a system job"
            assert absent == system, (
                "jobs under absent context must see exactly the system rows"
            )
            assert own > system, "own context must add tenant rows on top"
        else:
            assert absent == 0, (
                f"{table}/{login}: absent tenant context returned rows —"
                f" NULLIF-wrapped GUC must match nothing"
            )

    @pytest.mark.parametrize("login", LOGINS)
    @pytest.mark.parametrize("table", MATRIX_WRITE_TABLES)
    def test_update_matrix(self, target, table, login):
        """The write half, generically: a self-assign UPDATE exercises USING
        and WITH CHECK on every ALL-policy without table-specific payloads.
        Governance tables get the actor GUCs, as every runtime write does."""
        dsn = _login_dsn(target, login)
        key = "id" if table == "workspaces" else "workspace_id"
        actor = table in GOVERNANCE
        own = _exec(
            dsn,
            f"UPDATE {table} SET {key} = {key} WHERE {key} = %s",
            params=(str(target["a"]["ws"]),),
            tenant=target["a"]["ws"],
            actor=actor,
        )
        assert own >= 1, (
            f"{table}/{login}: the write positive touched no rows —"
            f" the foreign zero below would be vacuous"
        )
        foreign = _exec(
            dsn,
            f"UPDATE {table} SET {key} = {key} WHERE {key} = %s",
            params=(str(target["b"]["ws"]),),
            tenant=target["a"]["ws"],
            actor=actor,
        )
        assert foreign == 0, f"{table}/{login}: cross-tenant UPDATE moved rows"

    def test_cross_tenant_insert_is_refused_by_the_policy_not_a_missing_grant(
        self, target
    ):
        """The refusal must be RLS's (WITH CHECK), not a grant accident — the
        message is matched, and the same-verb positive proves the grant."""
        ok = _exec(
            target["worker"],
            "INSERT INTO category_post_case_mix (workspace_id, category, ratio)"
            " VALUES (%s, 'ins-own', 0.1)",
            params=(str(target["a"]["ws"]),),
            tenant=target["a"]["ws"],
        )
        assert ok == 1
        with pytest.raises(
            psycopg2.errors.InsufficientPrivilege,
            match="row-level security policy",
        ):
            _exec(
                target["worker"],
                "INSERT INTO category_post_case_mix (workspace_id, category, ratio)"
                " VALUES (%s, 'smuggled', 0.1)",
                params=(str(target["b"]["ws"]),),
                tenant=target["a"]["ws"],
            )

    def test_audit_insert_carries_the_tenant_or_is_refused(self, target):
        ok = _exec(
            target["worker"],
            "INSERT INTO audit_events (workspace_id, entity_kind, entity_id,"
            " to_state, actor_kind) VALUES (%s, 'workspace', %s, 'f4', 'user')",
            params=(str(target["a"]["ws"]), str(target["a"]["ws"])),
            tenant=target["a"]["ws"],
        )
        assert ok == 1
        with pytest.raises(
            psycopg2.errors.InsufficientPrivilege,
            match="row-level security policy",
        ):
            _exec(
                target["worker"],
                "INSERT INTO audit_events (workspace_id, entity_kind, entity_id,"
                " to_state, actor_kind) VALUES (%s, 'workspace', %s, 'f4', 'user')",
                params=(str(target["b"]["ws"]), str(target["b"]["ws"])),
                tenant=target["a"]["ws"],
            )

    def test_userplane_and_machinery_probes(self, target):
        """The `true`-predicate login policies, driven at their grants:
        user-plane reads as both logins; rate_counters as both; command_dedup
        as ingress only — svc_worker holds no grant there, which is asserted
        as the denial it is."""
        for login in LOGINS:
            dsn = _login_dsn(target, login)
            assert _scalar(dsn, "SELECT count(*) FROM users") >= 2
            assert (
                _exec(
                    dsn,
                    "INSERT INTO rate_counters (scope, key, window_start, count)"
                    " VALUES ('ws_admission', %s, date_trunc('hour', now()), 1)",
                    params=(f"f4:{login}",),
                )
                == 1
            )
        assert (
            _exec(
                target["ingress"],
                "INSERT INTO command_dedup"
                " (channel, principal, external_ref, fingerprint)"
                " VALUES ('web', 'f4-session', %s, 'fp')",
                params=(str(uuid.uuid4()),),
            )
            == 1
        )
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            _exec(
                target["worker"],
                "INSERT INTO command_dedup"
                " (channel, principal, external_ref, fingerprint)"
                " VALUES ('web', 'f4-worker', 'x', 'fp')",
            )

    def test_the_census_matches_the_catalog_exactly(self, target):
        """THE completeness gate, at (policy, table, cmd, roles) grain — a
        smuggled permissive policy on an already-covered table is a census
        diff, not a silent pass (the table-granular form could not see
        that)."""
        rows = _exec(
            target["owner_stream"],
            "SELECT policyname, tablename, cmd, roles FROM pg_policies"
            " WHERE schemaname = 'public'",
            fetch=True,
        )
        catalog = {(r[0], r[1], r[2], tuple(sorted(r[3]))) for r in rows}
        census = {(p, t, c, tuple(sorted(roles))) for (p, t, c, roles) in POLICY_CENSUS}
        assert catalog == census, (
            f"policy census drift: only-in-catalog={sorted(catalog - census)},"
            f" only-in-census={sorted(census - catalog)}"
        )
        assert len(POLICY_CENSUS) == 58

    def test_every_census_row_has_a_disposition_and_the_split_is_honest(self):
        by_kind = {}
        for row, disp in POLICY_CENSUS.items():
            by_kind.setdefault(disp.split(":")[0], []).append(row)
        assert set(by_kind) == {
            "matrix",
            "matrix-read",
            "matrix-userplane",
            "insert-pair",
            "door",
            "machinery",
            "auth",
        }
        # Exact split, so a re-tagged disposition is a visible diff:
        assert len(by_kind["matrix"]) == 16
        assert len(by_kind["door"]) == 29
        assert len(by_kind["auth"]) == 5
        # every door named in a disposition exists in the DOORS registry
        for row, disp in POLICY_CENSUS.items():
            if disp.startswith("door:"):
                assert disp.split(":", 1)[1] in DOORS, row


class TestAbsentContextAndConnectionReuse:
    """The real schema's absent-context shape is quiet (empty set) — asserted
    with its paired positive — and therefore the session-`SET` reuse leak the
    probe module proved reproduces here, on the production schema."""

    def test_unset_guc_is_empty_not_error_with_paired_positive(self, target):
        assert _scalar(target["worker"], "SELECT count(*) FROM media_items") == 0
        assert (
            _scalar(
                target["worker"],
                "SELECT count(*) FROM media_items",
                tenant=target["a"]["ws"],
            )
            >= 1
        )

    def test_session_set_leaks_on_the_real_schema_too(self, target):
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
    """system-role policies are exercised only through their doors. Armed
    doors move rows and assert the counts; the zero-world sweeps assert their
    exact empty shape (disclosed as evaluated-not-effect-driven)."""

    def test_fn_claim_job_claims_and_fn_extend_leases_extends(self, target):
        claimed = _exec(target["worker"], DOORS["fn_claim_job"][1], fetch=True)
        assert claimed, "expected the seeded ready job to be claimable"
        job_id = claimed[0][0]
        lease_token = _scalar(
            target["owner_stream"],
            "SELECT lease_token FROM jobs WHERE id = %s",
            params=(str(job_id),),
        )
        assert lease_token is not None, "a claimed job must carry its lease"
        extended = _scalar(
            target["worker"],
            "SELECT fn_extend_leases(ARRAY[%s]::uuid[], interval '2 minutes')",
            params=(str(lease_token),),
        )
        assert extended == 1

    def test_fn_claim_job_skips_quarantined_serialization_keys(self, target):
        """The quarantine exclusion, driven: a ready job whose key is
        quarantined is not served."""
        q_key = f"ig:quarantined-{WS_A_NAME}"
        _exec(
            target["owner_stream"],
            "INSERT INTO jobs (workspace_id, kind, lane, serialization_key,"
            " payload, max_attempts) VALUES (%s, 'publish_pipeline',"
            " 'interactive', %s, '{\"v\": 1}', 3)",
            params=(str(target["a"]["ws"]), q_key),
        )
        for _ in range(4):  # drain every claimable interactive job
            if not _exec(target["worker"], DOORS["fn_claim_job"][1], fetch=True):
                break
        held = _scalar(
            target["owner_stream"],
            "SELECT count(*) FROM jobs WHERE serialization_key = %s"
            " AND state = 'ready'",
            params=(q_key,),
        )
        assert held == 1, "the quarantined-key job must never be claimed"

    def test_fn_clock_tick_enqueues_for_the_armed_world(self, target):
        row = _exec(target["worker"], DOORS["fn_clock_tick"][1], fetch=True)
        assert len(row) == 1
        assert sum(row[0]) >= 1, (
            f"an armed world (due slot + recurring singleton) must make the"
            f" tick do observable work; got {row[0]}"
        )
        plan_jobs = _scalar(
            target["owner_stream"],
            "SELECT count(*) FROM jobs WHERE kind IN ('plan_slot', 'reap_expired')",
        )
        assert plan_jobs >= 2, "the tick's enqueues must be visible in jobs"

    def test_fn_retention_batch_reaps_the_aged_succeeded_job(self, target):
        world = _exec(
            target["owner_stream"],
            "SELECT kind, lane, state, now() - updated_at > interval '90 days'"
            " FROM jobs ORDER BY created_at",
            fetch=True,
        )
        reaped = _exec(target["worker"], DOORS["fn_retention_batch"][1], fetch=True)
        assert reaped == [(1,)], (
            f"exactly the year-old succeeded job should age out, got {reaped};"
            f" jobs world before sweep: {world}"
        )

    def test_fn_reconciler_sweep_finds_nothing_ambiguous_here(self, target):
        """Nothing in this world is ambiguous, asserted as the EXACT empty
        return (the door returns due rows; fetchall can never be None so
        is-not-None would assert nothing). Its effect path belongs to the
        intent-lifecycle world the L-phase suites own."""
        assert (
            _exec(target["worker"], DOORS["fn_reconciler_sweep"][1], fetch=True) == []
        )

    def test_fn_reaper_sweep_expires_the_overdue_intents(self, target):
        """The reaper IS armed by this world: both seeded intents sit at
        their slot with no activity, so the expiry leg moves exactly those
        two — asserted by count AND by the observable state change."""
        reaped = _exec(target["worker"], DOORS["fn_reaper_sweep"][1], fetch=True)
        assert reaped == [(2,)], f"expected the two overdue intents, got {reaped}"
        expired = _scalar(
            target["owner_stream"],
            "SELECT count(*) FROM post_intents WHERE state = 'expired'",
        )
        assert expired == 2, "the reaper's count must be visible as intent state"

    def test_fn_offboard_finalize_refuses_by_name(self, target):
        """Message-match is deliberate here (the module otherwise asserts
        SQLSTATEs): RaiseException alone cannot say WHICH guard fired, and
        the guard's identity is the assertion."""
        with pytest.raises(psycopg2.errors.RaiseException, match="not finalizable"):
            _exec(
                target["worker"],
                "SELECT fn_offboard_finalize(%s, interval '0 days')",
                params=(str(target["a"]["ws"]),),
                fetch=True,
            )

    def test_fn_invitation_accept_admits_a_member_as_ingress(self, target):
        new_user = _scalar(
            target["owner_stream"], "INSERT INTO users DEFAULT VALUES RETURNING id"
        )
        row = _exec(
            target["ingress"],
            "SELECT * FROM fn_invitation_accept(%s, %s, 'telegram', %s, %s, 'web')",
            params=(
                target["b"]["invite_hash"],
                str(new_user),
                f"proof:{new_user}",
                4242421,
            ),
            fetch=True,
        )
        assert len(row) == 1 and str(row[0][0]) == str(target["b"]["ws"])
        member = _scalar(
            target["owner_stream"],
            "SELECT count(*) FROM workspace_members WHERE workspace_id = %s"
            " AND user_id = %s",
            params=(str(target["b"]["ws"]), str(new_user)),
        )
        assert member == 1, "the accepted invitation must have admitted the member"

    def test_fn_memberships_for_caller_answers_for_the_claimed_caller_only(
        self, target
    ):
        """The tenth door, driven as ingress: tenant A's seeded owner claims
        itself and reads exactly A — B's membership is not in the answer —
        and a session that claimed nothing reads nothing, the fail-closed
        half the body's NULLIF on the unset GUC exists for."""
        conn = psycopg2.connect(target["ingress"])
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute("SET app.actor_user_id = %s", (str(target["a"]["user"]),))
                cur.execute(DOORS["fn_memberships_for_caller"][1])
                listed = [str(r[0]) for r in cur.fetchall()]
        finally:
            conn.close()
        assert listed == [str(target["a"]["ws"])], listed
        unclaimed = _exec(
            target["ingress"], DOORS["fn_memberships_for_caller"][1], fetch=True
        )
        assert unclaimed == [], "an unclaimed session must read nothing"

    @pytest.mark.parametrize("door", sorted(DOORS))
    def test_each_door_is_denied_to_the_other_login(self, target, door):
        """EXECUTE is per-signature, so the denial calls the real signature —
        a bare door() would 42883 and prove nothing (and does, if mistyped:
        UndefinedFunction is not InsufficientPrivilege)."""
        permitted, call = DOORS[door]
        other = target["ingress"] if permitted == "svc_worker" else target["worker"]
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            _exec(other, call)

    def test_the_catalog_agrees_ten_doors_and_these_grants(self, target):
        rows = _exec(
            target["owner_stream"],
            "SELECT p.proname, r.rolname FROM pg_proc p"
            " JOIN pg_namespace n ON n.oid = p.pronamespace"
            " JOIN pg_roles r ON r.oid = p.proowner"
            " WHERE n.nspname = 'public' AND p.prosecdef",
            fetch=True,
        )
        catalog = {r[0] for r in rows}
        owner_of = {r[0]: r[1] for r in rows}
        assert catalog == set(DOORS), (
            f"SECURITY DEFINER census drift: only-in-catalog="
            f"{sorted(catalog - set(DOORS))},"
            f" only-in-module={sorted(set(DOORS) - catalog)}"
        )
        for door, (permitted, _call) in DOORS.items():
            grants = _exec(
                target["owner_stream"],
                "SELECT grantee FROM information_schema.routine_privileges"
                " WHERE routine_schema = 'public' AND routine_name = %s"
                " AND privilege_type = 'EXECUTE'",
                params=(door,),
                fetch=True,
            )
            # subtract only THIS door's owner (its implicit EXECUTE row) —
            # a blanket four-role subtraction would hide a stray grant to a
            # different door-owner role.
            grantees = {g[0] for g in grants} - {owner_of[door]}
            assert grantees == {permitted}, (
                f"{door}: EXECUTE grantees {sorted(grantees)},"
                f" expected exactly {{{permitted}}}"
            )


class TestDirectPathsAreShut:
    """The grant matrix gives the logins no DELETE anywhere — asserted as a
    census over the grant catalog (complete by construction), with live
    probes proving the census reads a real world."""

    def test_no_login_holds_delete_anywhere(self, target):
        rows = _exec(
            target["owner_stream"],
            "SELECT DISTINCT table_name FROM information_schema.role_table_grants"
            " WHERE grantee IN %s AND privilege_type = 'DELETE'"
            " AND table_schema = 'public'",
            params=(tuple(LOGINS),),
            fetch=True,
        )
        assert rows == [], f"login DELETE grants exist: {sorted(r[0] for r in rows)}"

    @pytest.mark.parametrize("table", ["audit_events", "jobs", "media_items"])
    def test_direct_delete_is_denied_live(self, target, table):
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            _exec(
                target["worker"],
                f"DELETE FROM {table} WHERE workspace_id = %s",
                params=(str(target["a"]["ws"]),),
                tenant=target["a"]["ws"],
            )

    def test_set_role_fails_for_every_service_role(self, target, owner_actor):
        for role in sorted(set(SERVICE_ROLES) - {"svc_worker"}):
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
        # The bootstrap's two documented grants, exactly, and nothing else:
        # door-owner roles to the window actor (function-ownership machinery,
        # step0 L51) and the transient owner self-grant (L66, revoked by the
        # M.3 stand-down a test world never runs). The runtime logins appear
        # on neither side of any membership.
        assert set(rows) == {(r, "svc_migration") for r in NOLOGIN_ROLES} | {
            ("svc_migration", owner_actor)
        }, f"unexpected service-role memberships: {sorted(rows)}"


class TestAuthPlaneMaintenanceOnlyThroughTheSweep:
    """Auth-plane maintenance succeeds only through fn_auth_plane_sweep —
    direct DELETE fails as every login, and the sweep's bounds are proven on
    BOTH sides: the expired row goes, the live row on the SAME table stays."""

    def test_direct_delete_denied_to_both_logins(self, target):
        for table in ("session_tokens", "oauth_states", "service_tokens"):
            for login in LOGINS:
                with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                    _exec(_login_dsn(target, login), f"DELETE FROM {table}")

    def test_the_sweep_reaps_expired_only_and_never_service_tokens(self, target):
        user = target["a"]["user"]
        _exec(
            target["ingress"],
            "INSERT INTO session_tokens (token_hash, user_id, expires_at)"
            " VALUES ('f4-expired', %s, now() - interval '2 days'),"
            "        ('f4-live', %s, now() + interval '2 days')",
            params=(str(user), str(user)),
        )
        _exec(
            target["ingress"],
            "INSERT INTO service_tokens (name, token_hash, role)"
            " VALUES ('f4-survivor', 'f4-service-survivor', 'readonly')",
        )
        _exec(target["worker"], DOORS["fn_auth_plane_sweep"][1], fetch=True)
        remaining = _exec(
            target["owner_stream"],
            "SELECT token_hash FROM session_tokens"
            " WHERE token_hash IN ('f4-expired', 'f4-live')",
            fetch=True,
        )
        assert remaining == [("f4-live",)], (
            f"the sweep must reap exactly the expired session, got {remaining}"
            " — the same-table survivor is the overreach control"
        )
        assert (
            _scalar(
                target["owner_stream"],
                "SELECT count(*) FROM service_tokens"
                " WHERE token_hash = 'f4-service-survivor'",
            )
            == 1
        ), "service_tokens sits outside the sweep by design (060's asymmetry)"


class TestZeroNullGates:
    """Tenant keys are NOT NULL everywhere but the three sanctioned columns,
    the presence half is asserted (a dropped column would otherwise vanish
    from the census), and jobs' pairing CHECK is driven in both directions
    with its positive."""

    def test_workspace_id_presence_and_nullability(self, target):
        rows = _exec(
            target["owner_stream"],
            "SELECT c.relname, a.attnotnull FROM pg_attribute a"
            " JOIN pg_class c ON c.oid = a.attrelid"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'public' AND a.attname = 'workspace_id'"
            " AND c.relkind = 'r'",
            fetch=True,
        )
        present = {r[0] for r in rows}
        assert present >= set(GUC_TABLES) - {"workspaces"}, (
            f"tables missing a workspace_id column entirely:"
            f" {sorted(set(GUC_TABLES) - {'workspaces'} - present)}"
        )
        nullable = {r[0] for r in rows if not r[1]}
        assert nullable == {"jobs", "oauth_states", "service_tokens"}, (
            f"nullable workspace_id: {sorted(nullable)} — sanctioned NULLs are"
            f" jobs' system-kind pairing and the two auth-plane pre-tenant"
            f" columns (060)"
        )

    def test_jobs_pairing_check_refuses_both_mismatches(self, target):
        """The armed world's system job (NULL + reap_expired) is the standing
        positive for the second direction; both refusals name the pairing
        constraint so an unrelated CHECK cannot satisfy them."""
        assert (
            _scalar(
                target["owner_stream"],
                "SELECT count(*) FROM jobs WHERE workspace_id IS NULL"
                " AND kind = 'reap_expired'",
            )
            >= 1
        )
        with pytest.raises(
            psycopg2.errors.CheckViolation, match="ck_jobs_system_kinds"
        ):
            _exec(
                target["owner_stream"],
                "INSERT INTO jobs (workspace_id, kind, lane, serialization_key,"
                " payload, max_attempts) VALUES (NULL, 'publish_pipeline',"
                " 'interactive', 'k', '{\"v\":1}', 3)",
            )
        with pytest.raises(
            psycopg2.errors.CheckViolation, match="ck_jobs_system_kinds"
        ):
            _exec(
                target["owner_stream"],
                "INSERT INTO jobs (workspace_id, kind, lane, serialization_key,"
                " payload, max_attempts) VALUES (%s, 'reap_expired',"
                " 'bulk', 'k2', '{\"v\":1}', 3)",
                params=(str(target["a"]["ws"]),),
            )
