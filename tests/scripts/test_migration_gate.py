"""CI migration gate (plan §0.2) — the REAL corpus, replayed and adopted.

- Replay-from-empty: legacy setup + the legacy lineage through the runner.
- Adoption: production-shaped fixtures built by hand-psql (the way production
  actually got its schema), entered into the ledger by ``runner adopt`` at
  both ends of the live uncertainty (at-45 and at-49) — the design
  requirement that adopt works without knowing which world it is in.
- Tamper: a deliberately-removed floor constraint fails adoption loudly.
- Parity: the runner-replayed schema equals the models-built schema.

**Every replay here is bounded to ``LEGACY_LINEAGE_MAX`` (#746, F.2.1b), and
the bound is load-bearing rather than tidy.** From 051 the corpus holds two
lineages in one directory: the legacy schema this file guards, and the target
schema created into the empty ``public`` the 3c move leaves behind. An
unbounded replay runs the move, so ``public`` ends the run empty and every
assertion below — parity against the legacy models, the ``schema_version``
self-stamp, the adopt fixtures' queue rows — is asserted against a schema that
is no longer there. `test_lineage_lane.py` replays *across* the boundary and
asserts that emptiness on purpose; these tests replay up to it. The bound is
derived from the move file's own marker, so it follows a renumbering; nothing
here names a version.

**Every replay here also runs under the declared actors (#753, `04` §0.2):
the lineage seeds and replays as the database-owner-shaped actor — legacy
tables carry real legacy ownership — and window-actor arms act as
``svc_migration`` after the step-0 bootstrap. Where the printed window
sequence cannot succeed as declared (D1: no mechanism gives ``svc_migration``
ALTER rights on owner-owned legacy tables, and no GRANT can), the gate
asserts that failure BY NAME rather than hiding it — those arms flip to green
paths when D1 is ruled.**
"""

import pytest

from scripts.migration_runner import (
    MigrationRunnerError,
    adopt,
    apply_pending,
    discover_migrations,
)
from scripts.schema_parity import schema_diff, schema_signature
from src.utils.validators import MIGRATIONS_DIR
from tests.scripts.conftest import (
    LEGACY_LINEAGE_MAX,
    SETUP_SQL,
    as_user,
    execute,
    fetch_ledger,
    fetch_one,
    psql_apply,
    window_actor,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

MANIFEST = MIGRATIONS_DIR / "adoption_manifest.json"

# The D1 seam (#753): what the declared window actor's refusal looks like
# today. Chris's ruling flips THIS STRING's tests to green paths — one
# constant, not scattered regexes.
D1_REFUSAL = "permission denied|must be owner"


def corpus_versions():
    return [m.version for m in discover_migrations(MIGRATIONS_DIR, LEGACY_LINEAGE_MAX)]


class TestReplayFromEmpty:
    def test_runner_replays_legacy_lineage_from_empty_as_the_owner(
        self, owner_actor, owner_db
    ):
        """The lineage-seed semantic: setup + 001–050 as the non-superuser
        owner. This is also the retroactive-tightening battery for the whole
        legacy corpus — a file that silently required superuser fails here."""
        as_owner = as_user(owner_db, owner_actor)
        psql_apply(as_owner, [SETUP_SQL])

        report = apply_pending(as_owner, MIGRATIONS_DIR, LEGACY_LINEAGE_MAX)

        versions = [m.version for m in report.applied]
        assert versions == corpus_versions()
        assert len(versions) >= 50, "corpus floor — a wrong dir reads empty"
        rows = fetch_ledger(owner_db)
        assert all(r[4] == "applied" for r in rows)
        owners = fetch_one(
            owner_db,
            "SELECT array_agg(DISTINCT tableowner) FROM pg_tables"
            " WHERE schemaname = 'public'",
        )
        assert owners[0] == [owner_actor], (
            "legacy tables must carry real legacy ownership — a superuser-"
            "seeded schema tests an environment production does not have"
        )


class TestAdoptProductionShaped:
    def test_at_49_everything_enters_the_ledger_as_the_window_actor(
        self, at49_window_db, owner_actor, admin_conn
    ):
        """The world where 046–049 DID run by hand, entered by the DECLARED
        window actor: bootstrap as owner, adopt as ``svc_migration`` — the
        probes read through the bootstrap's grants, and the ledger is created
        by (and owned by) the window actor, the production first-contact
        shape. Only 050 stays pending; the seven data-only floor files enter
        as declared assertions, reported distinctly."""
        as_svc = window_actor(at49_window_db, owner_actor, admin_conn)

        report = adopt(as_svc, MIGRATIONS_DIR, MANIFEST, LEGACY_LINEAGE_MAX)

        recorded = sorted(m.version for m in report.adopted + report.asserted)
        assert recorded == list(range(1, 50))
        assert [m.version for m in report.asserted] == [18, 22, 24, 27, 36, 39, 44]
        assert [m.version for m in report.pending] == [50]

        # D1, asserted as current truth on the SAME adopted state (#753):
        # 050 ALTERs legacy tables, ALTER requires ownership or membership
        # in the owning role, and the printed bootstrap grants neither — the
        # printed window sequence fails at 3b as declared. R6-P0's class,
        # held in CI. THIS ASSERTION FLIPS to a green tail-apply when Chris
        # rules D1; D1_REFUSAL is the seam, deliberately loud.
        with pytest.raises(MigrationRunnerError, match=D1_REFUSAL):
            apply_pending(as_svc, MIGRATIONS_DIR, LEGACY_LINEAGE_MAX)

        owners = fetch_one(
            at49_window_db,
            "SELECT array_agg(DISTINCT tableowner) FROM pg_tables"
            " WHERE schemaname = 'public'",
        )
        assert owners[0] == [owner_actor], (
            "the production-shaped fixture must carry real legacy ownership"
            " — an admin-seeded template would fake every actor property"
            " downstream of it"
        )

    def test_at_49_the_owner_world_completes_the_tail(self, at49_db, owner_actor):
        """The D1-option-(a) world, kept green so 050's apply coverage never
        lapses while D1 is open: the owner actor runs first contact end to
        end (adopt + tail apply), owning the ledger it creates."""
        as_owner = as_user(at49_db, owner_actor)

        report = adopt(as_owner, MIGRATIONS_DIR, MANIFEST, LEGACY_LINEAGE_MAX)
        assert [m.version for m in report.pending] == [50]

        after = apply_pending(as_owner, MIGRATIONS_DIR, LEGACY_LINEAGE_MAX)
        assert [m.version for m in after.applied] == [50]

    def test_at_45_the_tail_stays_pending_then_applies(self, owner_actor, at45_db):
        """The world where 046–049 never ran: adopt records 001–045 and
        leaves the tail pending; a gated apply completes it. Same manifest,
        same command, no foreknowledge. Owner-world actors (the tail carries
        legacy-table DDL, which is D1's territory — the declared-actor
        refusal is asserted separately below)."""
        as_owner = as_user(at45_db, owner_actor)

        report = adopt(as_owner, MIGRATIONS_DIR, MANIFEST, LEGACY_LINEAGE_MAX)

        recorded = sorted(m.version for m in report.adopted + report.asserted)
        assert recorded == list(range(1, 46))
        assert [m.version for m in report.pending] == [46, 47, 48, 49, 50]

        after = apply_pending(as_owner, MIGRATIONS_DIR, LEGACY_LINEAGE_MAX)
        assert [m.version for m in after.applied] == [46, 47, 48, 49, 50]
        assert len(fetch_ledger(at45_db)) == len(corpus_versions())

    def test_at_45_the_printed_window_actor_refuses_the_tail_pending_d1(
        self, owner_actor, at45_window_db, admin_conn
    ):
        """D1's other end: at-45 the pending tail opens with 046's ALTER, so
        the declared window actor refuses at the first file. Same seam as the
        at-49 arm; flips with the ruling."""
        as_svc = window_actor(at45_window_db, owner_actor, admin_conn)
        adopt(as_svc, MIGRATIONS_DIR, MANIFEST, LEGACY_LINEAGE_MAX)

        with pytest.raises(MigrationRunnerError, match=D1_REFUSAL):
            apply_pending(as_svc, MIGRATIONS_DIR, LEGACY_LINEAGE_MAX)

    def test_adoption_probes_are_privilege_sensitive_so_the_grants_are_load_bearing(
        self, at49_window_db, owner_actor, admin_conn
    ):
        """Measured, not assumed (#753 plan) — and the measurement was
        sharper than the prediction: without the bootstrap's SELECT grant the
        first refusal is 048's probe ERRORING (it reads ``posting_queue``
        data, not catalogs, and a probe error is a hard failure by design) —
        before the floor check ever runs on the catalog probes' absent
        evidence. Either mechanism refuses and writes nothing; this is why
        bootstrap-before-adopt is sequencing, not ceremony."""
        as_svc = window_actor(at49_window_db, owner_actor, admin_conn)
        execute(
            as_user(at49_window_db, owner_actor),
            "REVOKE SELECT ON ALL TABLES IN SCHEMA public FROM svc_migration",
        )

        with pytest.raises(MigrationRunnerError, match="errored.*permission denied"):
            adopt(as_svc, MIGRATIONS_DIR, MANIFEST, LEGACY_LINEAGE_MAX)

        assert fetch_ledger(at49_window_db) == []

    def test_at_49_with_lingering_limbo_rows_048_reapplies(self, at49_db, owner_actor):
        """The self-healing case: an at-49 database has re-accumulated an
        aged stamped-processing row (crash after the original backfill).
        048's probe reads false, the reapply-safe marker keeps the chain
        coherent, and the gated apply parks the row as delivered.
        Owner-world actors (048's UPDATEs sit in D1's territory)."""
        as_owner = as_user(at49_db, owner_actor)
        row = fetch_one(
            at49_db,
            "INSERT INTO media_items (file_path, file_name, file_size, file_hash)"
            " VALUES ('/limbo.jpg', 'limbo.jpg', 1, 'deadbeef') RETURNING id",
        )
        execute(
            at49_db,
            "INSERT INTO posting_queue"
            " (media_item_id, status, telegram_message_id, scheduled_for)"
            " VALUES (%s, 'processing', 424242, now() - interval '3 days')",
            (row[0],),
        )

        report = adopt(as_owner, MIGRATIONS_DIR, MANIFEST, LEGACY_LINEAGE_MAX)

        assert 48 not in [m.version for m in report.adopted]
        assert [m.version for m in report.pending] == [48, 50]

        # A FRESH in-flight claim (mason's #749 repro): claim_for_processing
        # COMMITS at queue_repository.py:63, so nothing holds the row lock
        # while the handler works — the re-applied backfill must not touch it.
        # Only the bound on the UPDATE (matching the probe's >24h evidence)
        # protects this row.
        fresh = fetch_one(
            at49_db,
            "INSERT INTO media_items (file_path, file_name, file_size, file_hash)"
            " VALUES ('/fresh.jpg', 'fresh.jpg', 1, 'cafebabe') RETURNING id",
        )
        execute(
            at49_db,
            "INSERT INTO posting_queue"
            " (media_item_id, status, telegram_message_id, scheduled_for)"
            " VALUES (%s, 'processing', 555555, now())",
            (fresh[0],),
        )

        apply_pending(as_owner, MIGRATIONS_DIR, LEGACY_LINEAGE_MAX)
        parked = fetch_one(
            at49_db,
            "SELECT status FROM posting_queue WHERE telegram_message_id = 424242",
        )
        assert parked[0] == "delivered"
        live = fetch_one(
            at49_db,
            "SELECT status FROM posting_queue WHERE telegram_message_id = 555555",
        )
        assert live[0] == "processing", (
            "a re-applied 048 must not rewrite a fresh live claim — the"
            " UPDATE's row-set must match the probe's evidence (>24h)"
        )

    def test_deliberately_removed_constraint_fails_loudly_by_name(
        self, at49_db, owner_actor
    ):
        # Target the REQUIRED FLOOR: a missing constraint the manifest
        # requires applied is a discrepancy adopt must refuse by name.
        # (Above the floor, a false probe is legitimately "pending" — that
        # asymmetry is the 45-or-49 design.)
        as_owner = as_user(at49_db, owner_actor)
        execute(
            as_owner,
            "ALTER TABLE api_tokens DROP CONSTRAINT unique_credential_per_account",
        )

        with pytest.raises(MigrationRunnerError, match="040"):
            adopt(as_owner, MIGRATIONS_DIR, MANIFEST, LEGACY_LINEAGE_MAX)

        assert fetch_ledger(at49_db) == []


class TestSchemaParity:
    def test_replayed_schema_equals_models_schema(self, replayed_db, second_scratch_db):
        """The §0.2 parity gate: what the runner builds from SQL equals what
        SQLAlchemy builds from the models — DB-only drift (types, missing
        constraints) fails a test instead of surfacing in production. The
        replayed side is the owner-actor runner replay (session template);
        parity is actor-independent by construction (the comparator reads
        structure, not ownership)."""

        from sqlalchemy import create_engine

        import src.models  # noqa: F401 - registers every model on Base
        from src.config.database import Base

        engine = create_engine(second_scratch_db)
        Base.metadata.create_all(engine)
        engine.dispose()

        diffs = schema_diff(
            schema_signature(replayed_db), schema_signature(second_scratch_db)
        )
        assert diffs == [], "replayed vs models drift:\n" + "\n".join(diffs)

    def test_parity_comparator_can_fail(self):
        """A comparator that cannot fail proves nothing. Pure-dict check —
        the catalog-extraction path is exercised by the test above."""
        base = {
            "t": {
                "columns": {"id": ("integer", "NO")},
                "checks": {"check_posting_method": "CHECK (x)"},
                "uniques": {(("id",), "")},
            }
        }
        broken = {
            "t": {
                "columns": {"id": ("integer", "NO")},
                "checks": {},
                "uniques": {(("id",), "")},
            }
        }

        diffs = schema_diff(base, broken)

        assert any("check_posting_method" in d for d in diffs)


class TestLegacyStampParity:
    def test_runner_replay_keeps_legacy_self_stamps_coherent(self, replayed_db):
        """Until schema_version is archived at M.3, the legacy self-stamp
        convention holds: every corpus file that stamps itself does so on a
        runner replay too (psycopg2 executes the INSERTs like psql did)."""
        row = fetch_one(replayed_db, "SELECT max(version) FROM schema_version")
        assert row[0] == max(corpus_versions())
