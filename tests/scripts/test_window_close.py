"""M.3 steps 3g and 8 as the deploy cannot run them and the owner can (the
legacy tear-out, phase 04; forks F6, F7, F8 (a)).

Two files carry `-- runner:manual`: 079 drops `legacy` behind an in-file
precondition (every snapshot present, every count equal to its source), 080
closes the window (the subject-identity guard, the `window_ddl` door dropped,
`CREATE ON DATABASE` revoked from `svc_migration`) and keeps every membership
a door file's `OWNER TO svc_*` needs. This file is the gate on both, on phase
03's world — the owner actor, production's shape — and it is the only success
arm of the stand-down in CI (`test_window_bootstrap.py` guards the abandon
variant).

Three layers: the MECHANICAL GUARD (a plain `apply` leaves 079 and 080 owed
and applies nothing else — the deploy cannot drop `legacy`); the DROP as the
owner actor, with its two refusals; the STAND-DOWN as the owner actor, with
its refusal, the gate's own lines answered as 080's comments say they answer,
and F8's positive control — a door file's statement still lands afterwards.
"""

from __future__ import annotations

import re

import pytest
from scripts.migration_runner import (
    MIGRATIONS_DIR,
    MigrationRunnerError,
    apply_manual,
    apply_pending,
    status,
)
from tests.scripts.conftest import (
    NOLOGIN_ROLES,
    as_user,
    execute,
    fetch_all,
    fetch_ledger,
    fetch_one,
    run_bootstrap,
)
from tests.scripts.legacy_inventory import HAND_MADE, LEGACY_TABLES
from tests.scripts.test_legacy_snapshots import (
    SNAPSHOT_VERSION,
    _count,
    _seed,
    _snapshots,
    _stand_up_to_077,
)

DROP_VERSION = 79
STAND_DOWN_VERSION = 80
DATE = "20260917"


def _world_through_078(admin_conn, owner_actor, owner_window_db) -> str:
    """Phase 03's owner world, with 078 applied by a plain `apply` — the
    state production is in after phase 03's deploy."""
    as_owner = as_user(owner_window_db, owner_actor)
    run_bootstrap(admin_conn, as_owner)
    _stand_up_to_077(as_owner)
    _seed(as_owner)
    apply_pending(as_owner, MIGRATIONS_DIR)
    return as_owner


def _schema_present(dsn: str, name: str) -> bool:
    return fetch_one(
        dsn, "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = %s)", (name,)
    )[0]


def _pg_major(dsn: str) -> int:
    return int(fetch_one(dsn, "SHOW server_version_num")[0]) // 10000


#: What a door file's `ALTER … OWNER TO svc_*` needs of its executor on the
#: receiving role: SET on PG16+ (production is 17; the creator auto-grant
#: carries none, so the chain through svc_migration is what supplies it),
#: plain membership on 15 (CI's cluster).
def _owner_to_privilege(dsn: str) -> str:
    return "SET" if _pg_major(dsn) >= 16 else "MEMBER"


#: 076's idiom, verbatim: every door file brackets the schema half itself.
#: The membership half is what 080 keeps — this is F8's positive control.
def _door_hand_off(dsn: str, role: str) -> None:
    execute(
        dsn,
        f"GRANT CREATE ON SCHEMA public TO {role};"
        f" ALTER FUNCTION fn_reaper_stale_approved(interval, int) OWNER TO {role};"
        f" REVOKE CREATE ON SCHEMA public FROM {role}",
    )


def _memberships_of(dsn: str, member: str) -> set:
    rows = fetch_all(
        dsn,
        "SELECT r.rolname FROM pg_auth_members m"
        " JOIN pg_roles r ON r.oid = m.roleid JOIN pg_roles g ON g.oid = m.member"
        " WHERE g.rolname = %s",
        (member,),
    )
    return {row["rolname"] for row in rows}


class TestTheTwoFiles:
    """Unit pins on the files themselves, no database: both gated, both outside
    the advertised stream, both one transaction; 079's inventory is
    LEGACY_TABLES written out, with 078's date."""

    def _file(self, version):
        from scripts.migration_runner import discover_migrations

        [m] = [m for m in discover_migrations(MIGRATIONS_DIR) if m.version == version]
        return m

    @pytest.mark.parametrize("version", [DROP_VERSION, STAND_DOWN_VERSION])
    def test_it_is_gated_unadvertised_and_wrapped(self, version):
        m = self._file(version)
        assert m.manual is True
        assert m.unadvertised is True
        assert m.execution_mode == "wrapped"
        assert m.postconditions, "a gated file carries its adoption evidence"

    def test_079_names_every_table_of_the_inventory_and_078s_date(self):
        sql = self._file(DROP_VERSION).sql
        block = sql[sql.index("ARRAY[") : sql.index("] LOOP")]
        assert tuple(re.findall(r"'([a-z_]+)'", block)) == LEGACY_TABLES
        assert set(re.findall(r"_pre_cutover_(\d{8})", sql)) == {DATE}
        body = "\n".join(line for line in sql.splitlines() if not line.startswith("--"))
        assert body.count("DROP SCHEMA legacy CASCADE") == 1

    def test_080_carries_the_identity_guard_and_no_membership_revoke(self):
        sql = self._file(STAND_DOWN_VERSION).sql
        body = "\n".join(line for line in sql.splitlines() if not line.startswith("--"))
        assert "success variant refused" in body
        assert "DROP SCHEMA IF EXISTS window_ddl CASCADE" in body
        assert "REVOKE CREATE ON DATABASE" in body
        # F8 (a): the ONE revoke is the database privilege; no membership goes
        assert re.findall(r"\bREVOKE\b[^;']*", body) == [
            "REVOKE CREATE ON DATABASE %I FROM svc_migration"
        ]


@pytest.mark.integration
@pytest.mark.slow
class TestTheDeployCannotDropLegacy:
    """F6's whole point, mechanically: `apply` — what every predeploy runs —
    leaves the two manual files owed, applies nothing else, and exits as a
    success. The drop needs a hand on the other door."""

    def test_apply_leaves_079_and_080_owed_and_applies_nothing_else(
        self, admin_conn, owner_actor, owner_window_db
    ):
        as_owner = _world_through_078(admin_conn, owner_actor, owner_window_db)
        report = apply_pending(as_owner, MIGRATIONS_DIR)
        assert report.applied == []
        assert [m.version for m in report.owed] == [DROP_VERSION, STAND_DOWN_VERSION]
        assert [row[0] for row in fetch_ledger(as_owner)][-1] == SNAPSHOT_VERSION
        assert _schema_present(as_owner, "legacy")
        assert len(_snapshots(as_owner)) == len(LEGACY_TABLES)

    def test_status_reports_them_as_owed_and_pending_as_empty(
        self, admin_conn, owner_actor, owner_window_db
    ):
        as_owner = _world_through_078(admin_conn, owner_actor, owner_window_db)
        report = status(as_owner, MIGRATIONS_DIR)
        assert report.pending == []
        assert [m.version for m in report.owed] == [DROP_VERSION, STAND_DOWN_VERSION]
        assert report.discrepancies == []


@pytest.mark.integration
@pytest.mark.slow
class TestTheDropAsTheOwnerActor:
    def test_079_drops_legacy_and_keeps_every_snapshot(
        self, admin_conn, owner_actor, owner_window_db
    ):
        as_owner = _world_through_078(admin_conn, owner_actor, owner_window_db)
        before = {
            t: _count(as_owner, "archive", f"{t}_pre_cutover_{DATE}")
            for t in LEGACY_TABLES
        }
        assert before["schema_version"] == 48 and before[HAND_MADE[0]] == 1

        report = apply_manual(as_owner, MIGRATIONS_DIR, DROP_VERSION)

        assert [m.version for m in report.applied] == [DROP_VERSION]
        assert not _schema_present(as_owner, "legacy")
        assert _snapshots(as_owner).keys() == {
            f"{t}_pre_cutover_{DATE}" for t in LEGACY_TABLES
        }
        assert set(_snapshots(as_owner).values()) == {"svc_maintenance"}
        for t in LEGACY_TABLES:
            assert _count(as_owner, "archive", f"{t}_pre_cutover_{DATE}") == before[t]
        rows = fetch_ledger(as_owner)
        assert rows[-1][0] == DROP_VERSION and rows[-1][4] == "applied"
        # uuid-ossp rode into `legacy` (051's note) and went with it; the
        # target needs no extension for gen_random_uuid()
        assert (
            fetch_one(
                as_owner,
                "SELECT count(*) FROM pg_extension WHERE extname = 'uuid-ossp'",
            )[0]
            == 0
        )
        assert fetch_one(as_owner, "SELECT gen_random_uuid() IS NOT NULL")[0] is True
        # the target is intact: its marker table and its doors
        assert fetch_one(as_owner, "SELECT to_regclass('public.jobs') IS NOT NULL")[0]

    def test_079_refuses_when_a_snapshot_is_missing_and_leaves_legacy_intact(
        self, admin_conn, owner_actor, owner_window_db
    ):
        """A refusal is the guard working: an inventory error must abort the
        drop whole, never leave `legacy` half-gone."""
        as_owner = _world_through_078(admin_conn, owner_actor, owner_window_db)
        execute(as_owner, f"DROP TABLE archive.users_pre_cutover_{DATE}")

        with pytest.raises(MigrationRunnerError, match="079") as exc:
            apply_manual(as_owner, MIGRATIONS_DIR, DROP_VERSION)
        # LOAD-BEARING: without the snapshot check the count query errors on
        # the missing relation — still a refusal naming 079 — so the MESSAGE is
        # what proves the guard fired, not the raise.
        assert "no snapshot for legacy.users" in str(exc.value)
        assert _schema_present(as_owner, "legacy")
        assert len(
            fetch_all(
                as_owner,
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n"
                " ON n.oid = c.relnamespace WHERE n.nspname = 'legacy'"
                " AND c.relkind = 'r'",
            )
        ) == len(LEGACY_TABLES)
        assert [row[0] for row in fetch_ledger(as_owner)][-1] == SNAPSHOT_VERSION

    @pytest.mark.parametrize(
        "writer, names",
        [
            (
                # one more row than the snapshot holds
                lambda dsn: _seed(dsn),
                ("holds 2 rows but its snapshot holds 1",),
            ),
            (
                # one fewer
                lambda dsn: execute(dsn, f"DELETE FROM legacy.{HAND_MADE[0]}"),
                ("holds 0 rows but its snapshot holds 1",),
            ),
            (
                # the same count, different content: an update in place
                lambda dsn: execute(
                    dsn, f"UPDATE legacy.{HAND_MADE[0]} SET status = 'changed'"
                ),
                ("differs from its snapshot in 1 row",),
            ),
            (
                # the same count, a column the snapshot never had
                lambda dsn: execute(
                    dsn, f"ALTER TABLE legacy.{HAND_MADE[0]} ADD COLUMN later int"
                ),
                ("differs from its snapshot",),
            ),
        ],
        ids=["insert", "delete", "update-in-place", "column-added"],
    )
    def test_079_refuses_when_a_table_no_longer_matches_its_snapshot(
        self, admin_conn, owner_actor, owner_window_db, writer, names
    ):
        """A writer since 078 — in either direction, or in place — is data the
        drop would destroy; 079 compares every count AND every row's hash
        in-file and refuses, naming the table."""
        as_owner = _world_through_078(admin_conn, owner_actor, owner_window_db)
        writer(as_owner)

        with pytest.raises(MigrationRunnerError, match="079") as exc:
            apply_manual(as_owner, MIGRATIONS_DIR, DROP_VERSION)
        assert HAND_MADE[0] in str(exc.value)
        for name in names:
            assert name in str(exc.value)
        assert _schema_present(as_owner, "legacy")
        assert [row[0] for row in fetch_ledger(as_owner)][-1] == SNAPSHOT_VERSION

    def test_079_refuses_a_relation_in_legacy_that_is_not_in_the_inventory(
        self, admin_conn, owner_actor, owner_window_db
    ):
        """A table added to `legacy` after 078 has no snapshot and would go
        with the schema unseen; the drop refuses on the count of relations."""
        as_owner = _world_through_078(admin_conn, owner_actor, owner_window_db)
        execute(as_owner, "CREATE TABLE legacy.stray (id int)")

        with pytest.raises(MigrationRunnerError, match="079") as exc:
            apply_manual(as_owner, MIGRATIONS_DIR, DROP_VERSION)
        assert "17 relations" in str(exc.value)
        assert _schema_present(as_owner, "legacy")


@pytest.mark.integration
@pytest.mark.slow
class TestTheStandDownAsTheOwnerActor:
    def test_080_refuses_while_legacy_is_present(
        self, admin_conn, owner_actor, owner_window_db
    ):
        """The subject-identity guard (the R8 mirror): run early, the stand-down
        would certify a half-done window. It refuses, and touches nothing."""
        as_owner = _world_through_078(admin_conn, owner_actor, owner_window_db)
        assert _schema_present(as_owner, "window_ddl")

        with pytest.raises(MigrationRunnerError, match="080") as exc:
            apply_manual(as_owner, MIGRATIONS_DIR, STAND_DOWN_VERSION)
        # LOAD-BEARING: without the guard the door drops and postcondition
        # one fails — still a refusal naming 080 — so the MESSAGE proves it.
        assert "success variant refused" in str(exc.value)
        assert _schema_present(as_owner, "window_ddl")
        assert (
            fetch_one(
                as_owner,
                "SELECT has_database_privilege('svc_migration', current_database(), 'CREATE')",
            )[0]
            is True
        )
        assert [row[0] for row in fetch_ledger(as_owner)][-1] == SNAPSHOT_VERSION

    def test_080_refuses_when_legacy_reappears_after_079(
        self, admin_conn, owner_actor, owner_window_db
    ):
        """The other half-done shape: 079 recorded, but a `legacy` schema is
        back — a half-restored database. The legacy clause alone must refuse
        it (the ledger clause is satisfied here)."""
        as_owner = _world_through_078(admin_conn, owner_actor, owner_window_db)
        apply_manual(as_owner, MIGRATIONS_DIR, DROP_VERSION)
        execute(as_owner, "CREATE SCHEMA legacy")

        with pytest.raises(MigrationRunnerError, match="080") as exc:
            apply_manual(as_owner, MIGRATIONS_DIR, STAND_DOWN_VERSION)
        assert "legacy schema present: t" in str(exc.value)
        assert "079 recorded: t" in str(exc.value)
        assert _schema_present(as_owner, "window_ddl")

    def test_080_refuses_when_legacy_is_gone_but_079_was_never_recorded(
        self, admin_conn, owner_actor, owner_window_db
    ):
        """`public.jobs` present and `legacy` absent is also the shape of a
        database that never held a legacy schema — and of one where the drop
        was done by hand. The ledger row is what says 3g happened here."""
        as_owner = _world_through_078(admin_conn, owner_actor, owner_window_db)
        execute(as_owner, "DROP SCHEMA legacy CASCADE")

        with pytest.raises(MigrationRunnerError, match="080") as exc:
            apply_manual(as_owner, MIGRATIONS_DIR, STAND_DOWN_VERSION)
        assert "079 recorded: f" in str(exc.value)
        assert _schema_present(as_owner, "window_ddl")

    def test_080_closes_the_window_and_the_gate_answers_as_printed(
        self, admin_conn, owner_actor, owner_window_db
    ):
        """Every gate line 080 prints as a comment, run as printed, with the
        answer the comment gives — F8 (a)'s shape, not D40's design fact."""
        as_owner = _world_through_078(admin_conn, owner_actor, owner_window_db)
        apply_manual(as_owner, MIGRATIONS_DIR, DROP_VERSION)

        report = apply_manual(as_owner, MIGRATIONS_DIR, STAND_DOWN_VERSION)

        assert [m.version for m in report.applied] == [STAND_DOWN_VERSION]
        assert [row[0] for row in fetch_ledger(as_owner)][-1] == STAND_DOWN_VERSION
        q = lambda sql: fetch_one(as_owner, sql)[0]  # noqa: E731 — the gate's lines
        # identity: the target present, legacy gone, the door gone
        assert q(
            "SELECT to_regclass('public.jobs') IS NOT NULL"
            " AND NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'legacy')"
            " AND NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'window_ddl')"
        )
        # the window's CREATE on the database is revoked
        assert q(
            "SELECT NOT has_database_privilege('svc_migration', current_database(), 'CREATE')"
        )
        # F8 (a): svc_migration KEEPS exactly the four door-owner memberships
        assert _memberships_of(as_owner, "svc_migration") == set(NOLOGIN_ROLES)
        # and no other service role is a member of anything
        assert q(
            "SELECT count(*) = 0 FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.member"
            " WHERE g.rolname LIKE 'svc\\_%' AND g.rolname <> 'svc_migration'"
        )
        # the owner login keeps its membership of svc_migration — the chain
        # that gives it SET on the door owners (PG16+: the creator auto-grant
        # carries no SET), which is what a door file's OWNER TO needs
        assert q("SELECT pg_has_role(current_user, 'svc_migration', 'MEMBER')")
        assert q(
            "SELECT pg_has_role(current_user, 'svc_maintenance',"
            f" '{_owner_to_privilege(as_owner)}')"
        )
        # every roleid-side svc_% row is the creator's auto-grant (ADMIN, 16+),
        # the owner's explicit svc_migration membership, or svc_migration's
        # four — an owner membership of any OTHER service role, admin or not,
        # is a grant nobody made and fails here (on 15 the auto-grant clause
        # matches nothing; the rest is exact)
        assert q(
            "SELECT count(*) = 0 FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid"
            " WHERE r.rolname LIKE 'svc\\_%'"
            "   AND NOT (m.member = current_user::regrole"
            "            AND (r.rolname = 'svc_migration' OR m.admin_option))"
            "   AND NOT (m.member = 'svc_migration'::regrole"
            "            AND r.rolname IN ('svc_claim','svc_clock','svc_maintenance','svc_membership'))"
        )
        # no service role may CREATE in public
        assert q(
            "SELECT bool_and(NOT has_schema_privilege(r, 'public', 'CREATE'))"
            " FROM unnest(ARRAY['svc_claim','svc_clock','svc_maintenance','svc_membership','svc_ingress','svc_worker']) r"
        )
        # `public` is the owner login's (production's measured shape; D40's
        # design fact — svc_migration — is the F.4 increment's, not this file's)
        assert q(
            "SELECT nspowner::regrole::text = current_user::text"
            " FROM pg_namespace WHERE nspname = 'public'"
        )
        # no SECURITY DEFINER door anywhere but the target's own schema
        assert q(
            "SELECT count(*) = 0 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE p.prosecdef AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'public')"
        )
        # and the snapshots survived the whole window
        assert len(_snapshots(as_owner)) == len(LEGACY_TABLES)

    def test_a_door_file_statement_still_lands_after_the_stand_down(
        self, admin_conn, owner_actor, owner_window_db
    ):
        """F8's positive control: the statement 076 ran — a SECURITY DEFINER
        door handed to a service role — still succeeds as the owner after
        080, because the memberships it needs were kept. The full stand-down
        would have broken every later door file's deploy."""
        as_owner = _world_through_078(admin_conn, owner_actor, owner_window_db)
        apply_manual(as_owner, MIGRATIONS_DIR, DROP_VERSION)
        apply_manual(as_owner, MIGRATIONS_DIR, STAND_DOWN_VERSION)

        _door_hand_off(as_owner, "svc_clock")
        _door_hand_off(as_owner, "svc_maintenance")
        assert (
            fetch_one(
                as_owner,
                "SELECT r.rolname FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner"
                " WHERE p.proname = 'fn_reaper_stale_approved'",
            )[0]
            == "svc_maintenance"
        )

    def test_a_second_apply_after_the_window_is_a_no_op_with_nothing_owed(
        self, admin_conn, owner_actor, owner_window_db
    ):
        as_owner = _world_through_078(admin_conn, owner_actor, owner_window_db)
        apply_manual(as_owner, MIGRATIONS_DIR, DROP_VERSION)
        apply_manual(as_owner, MIGRATIONS_DIR, STAND_DOWN_VERSION)

        report = apply_pending(as_owner, MIGRATIONS_DIR)
        assert report.applied == [] and report.owed == []
        after = status(as_owner, MIGRATIONS_DIR)
        assert after.pending == [] and after.owed == []
        assert after.applied[-1][0].version == STAND_DOWN_VERSION
