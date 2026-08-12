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
    execute,
    fetch_ledger,
    fetch_one,
    migration_files,
    psql_apply,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

MANIFEST = MIGRATIONS_DIR / "adoption_manifest.json"


def corpus_versions():
    return [m.version for m in discover_migrations(MIGRATIONS_DIR, LEGACY_LINEAGE_MAX)]


class TestReplayFromEmpty:
    def test_runner_replays_full_corpus_from_empty(self, scratch_db):
        psql_apply(scratch_db, [SETUP_SQL])

        report = apply_pending(scratch_db, MIGRATIONS_DIR, LEGACY_LINEAGE_MAX)

        versions = [m.version for m in report.applied]
        assert versions == corpus_versions()
        assert len(versions) >= 50, "corpus floor — a wrong dir reads empty"
        rows = fetch_ledger(scratch_db)
        assert all(r[4] == "applied" for r in rows)


class TestAdoptProductionShaped:
    def test_at_49_everything_enters_the_ledger(self, at49_db):
        """The world where 046–049 DID run by hand: adopt records them all
        (048's limbo probe reads clean on a fixture with no queue rows) and
        only 050 stays pending. The seven data-only floor files enter as
        declared assertions, reported distinctly."""
        report = adopt(at49_db, MIGRATIONS_DIR, MANIFEST, LEGACY_LINEAGE_MAX)

        recorded = sorted(m.version for m in report.adopted + report.asserted)
        assert recorded == list(range(1, 50))
        assert [m.version for m in report.asserted] == [18, 22, 24, 27, 36, 39, 44]
        assert [m.version for m in report.pending] == [50]

        after = apply_pending(at49_db, MIGRATIONS_DIR, LEGACY_LINEAGE_MAX)
        assert [m.version for m in after.applied] == [50]

    def test_at_45_the_tail_stays_pending_then_applies(self, scratch_db):
        """The world where 046–049 never ran: adopt records 001–045 and
        leaves the tail pending; a gated apply completes it. Same manifest,
        same command, no foreknowledge."""
        psql_apply(scratch_db, [SETUP_SQL] + migration_files(45))

        report = adopt(scratch_db, MIGRATIONS_DIR, MANIFEST, LEGACY_LINEAGE_MAX)

        recorded = sorted(m.version for m in report.adopted + report.asserted)
        assert recorded == list(range(1, 46))
        assert [m.version for m in report.pending] == [46, 47, 48, 49, 50]

        after = apply_pending(scratch_db, MIGRATIONS_DIR, LEGACY_LINEAGE_MAX)
        assert [m.version for m in after.applied] == [46, 47, 48, 49, 50]
        assert len(fetch_ledger(scratch_db)) == len(corpus_versions())

    def test_at_49_with_lingering_limbo_rows_048_reapplies(self, at49_db):
        """The self-healing case: an at-49 database has re-accumulated an
        aged stamped-processing row (crash after the original backfill).
        048's probe reads false, the reapply-safe marker keeps the chain
        coherent, and the gated apply parks the row as delivered."""
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

        report = adopt(at49_db, MIGRATIONS_DIR, MANIFEST, LEGACY_LINEAGE_MAX)

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

        apply_pending(at49_db, MIGRATIONS_DIR, LEGACY_LINEAGE_MAX)
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

    def test_deliberately_removed_constraint_fails_loudly_by_name(self, at49_db):
        # Target the REQUIRED FLOOR: a missing constraint the manifest
        # requires applied is a discrepancy adopt must refuse by name.
        # (Above the floor, a false probe is legitimately "pending" — that
        # asymmetry is the 45-or-49 design.)
        execute(
            at49_db,
            "ALTER TABLE api_tokens DROP CONSTRAINT unique_credential_per_account",
        )

        with pytest.raises(MigrationRunnerError, match="040"):
            adopt(at49_db, MIGRATIONS_DIR, MANIFEST, LEGACY_LINEAGE_MAX)

        assert fetch_ledger(at49_db) == []


class TestSchemaParity:
    def test_replayed_schema_equals_models_schema(self, scratch_db, second_scratch_db):
        """The §0.2 parity gate: what the runner builds from SQL equals what
        SQLAlchemy builds from the models — DB-only drift (types, missing
        constraints) fails a test instead of surfacing in production."""
        psql_apply(scratch_db, [SETUP_SQL])
        apply_pending(scratch_db, MIGRATIONS_DIR, LEGACY_LINEAGE_MAX)

        from sqlalchemy import create_engine

        import src.models  # noqa: F401 - registers every model on Base
        from src.config.database import Base

        engine = create_engine(second_scratch_db)
        Base.metadata.create_all(engine)
        engine.dispose()

        diffs = schema_diff(
            schema_signature(scratch_db), schema_signature(second_scratch_db)
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
    def test_runner_replay_keeps_legacy_self_stamps_coherent(self, scratch_db):
        """Until schema_version is archived at M.3, the legacy self-stamp
        convention holds: every corpus file that stamps itself does so on a
        runner replay too (psycopg2 executes the INSERTs like psql did)."""
        psql_apply(scratch_db, [SETUP_SQL])
        apply_pending(scratch_db, MIGRATIONS_DIR, LEGACY_LINEAGE_MAX)

        row = fetch_one(scratch_db, "SELECT max(version) FROM schema_version")
        assert row[0] == max(corpus_versions())
