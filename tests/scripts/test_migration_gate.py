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
``svc_migration`` after the step-0 bootstrap.**

**Green here now means the window sequence WORKS, which is a change (#787).**
It previously meant something weaker and easy to misread: two of these arms
passed by asserting the printed sequence FAILED under its own declared actor,
because no mechanism gave ``svc_migration`` ALTER on the owner-owned legacy
tables and no GRANT could produce one. That was the honest way to hold a known
refusal, but a CI badge cannot distinguish *"the window works"* from *"the
window's failure is pinned where we put it."* Chris's ruling on #787 supplied
the mechanism — a SECURITY DEFINER door owned by the database-owner actor,
stood up at step 0 — so both arms now assert the apply SUCCEEDS, and a
regression in the door surfaces as a red gate rather than as a green one.
"""

import re

import psycopg2
import pytest

from scripts.migration_runner import (
    MigrationRunnerError,
    _load_manifest,
    adopt,
    apply_pending,
    discover_migrations,
)
from scripts.schema_parity import schema_diff, schema_signature
from src.utils.validators import MIGRATIONS_DIR
from tests.scripts.conftest import (
    LEGACY_LINEAGE_MAX,
    LEGACY_STANDUP,
    as_user,
    execute,
    fetch_ledger,
    fetch_one,
    psql_apply,
    window_actor,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

MANIFEST = MIGRATIONS_DIR / "adoption_manifest.json"


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
        psql_apply(as_owner, LEGACY_STANDUP)

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

        # STEP 3b, GREEN, AS THE DECLARED ACTOR (#787). This assertion used to
        # be `pytest.raises(... "must be owner")`. 050's two ALTERs now run
        # inside the step-0 definer door, owned by the database-owner actor, so
        # svc_migration completes 3b holding neither ownership nor membership.
        after = apply_pending(as_svc, MIGRATIONS_DIR, LEGACY_LINEAGE_MAX)
        assert [m.version for m in after.applied] == [50]

        # ...and the file did its WORK, not merely its exit status: the door
        # could have been a no-op and the apply would still be green.
        assert fetch_one(
            at49_window_db,
            "SELECT NOT EXISTS (SELECT 1 FROM pg_constraint"
            " WHERE conname = 'api_tokens_service_name_token_type_key')",
        )[0]
        assert (
            fetch_one(
                at49_window_db,
                "SELECT data_type FROM information_schema.columns"
                " WHERE table_schema = 'public' AND table_name = 'chat_settings'"
                " AND column_name = 'caption_style'",
            )[0]
            == "text"
        )

        # The elevation was scoped to the door's two statements and did not
        # leak to the caller — the property the whole ruling turns on. Asserted
        # on a legacy table the door's body does not name, as the same actor,
        # immediately after the successful call.
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            execute(
                as_svc,
                "ALTER TABLE public.posting_queue"
                " DROP CONSTRAINT IF EXISTS check_status",
            )

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

    def test_at_45_the_gated_apply_makes_the_world_window_ready_then_3b_is_green(
        self, owner_actor, at45_window_db, admin_conn
    ):
        """D1's other end (#787), and the shape of the answer is the finding.

        At-45 the pending tail is 046–050, and 046/047/049 carry their own
        legacy `ALTER TABLE`s. **The ruling's door covers 050 and only 050** —
        050 is the window's one legacy-DDL step (`04`: "3b 050"), and 046–049
        are pre-window work. So a database that arrives at the window still at
        45 is not window-ready: the plan's own gated apply, run by the owner
        actor at first contact, clears the pre-050 tail, and the WINDOW step is
        then green under the declared actor through the door.

        **Bound, stated rather than implied:** this arm no longer asserts that
        the window actor can apply an arbitrary pending tail. It cannot, by
        design — every legacy-DDL file that a door does not cover is another
        owner-privileged surface, and building three of them for migrations the
        printed window never runs would buy coverage of a world the plan does
        not execute at the price of three more standing doors.
        """
        as_svc = window_actor(at45_window_db, owner_actor, admin_conn)
        as_owner = as_user(at45_window_db, owner_actor)
        adopt(as_svc, MIGRATIONS_DIR, MANIFEST, LEGACY_LINEAGE_MAX)

        # The window actor is still refused on the PRE-050 tail, and that is
        # correct rather than a gap — it is what makes the gated apply a real
        # step instead of a formality.
        with pytest.raises(MigrationRunnerError, match="must be owner"):
            apply_pending(as_svc, MIGRATIONS_DIR, LEGACY_LINEAGE_MAX)

        # The plan's pre-window gated apply, as the owner actor, bounded BELOW
        # the window's own step so the assertion that follows is a real apply
        # and not an empty report. The bound is derived from the lineage max,
        # never a literal — nothing in this file names a version.
        gated = apply_pending(as_owner, MIGRATIONS_DIR, LEGACY_LINEAGE_MAX - 1)
        assert [m.version for m in gated.applied] == [46, 47, 48, 49]

        # STEP 3b, GREEN, AS THE DECLARED ACTOR — the same flip as the at-49
        # arm, reached from the other end of the uncertainty.
        after = apply_pending(as_svc, MIGRATIONS_DIR, LEGACY_LINEAGE_MAX)
        assert [m.version for m in after.applied] == [50]
        assert len(fetch_ledger(at45_window_db)) == len(corpus_versions())

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


class TestTheDerivedAdoptionProbesReadBothWays:
    """#997 — a file above the floor is adopted on evidence derived from its own
    ``runner:postcondition`` lines, so those lines must answer correctly in BOTH
    worlds, and a probe that can only ever answer one way is not evidence.

    The predicates are READ FROM THE FILE rather than restated here — one
    predicate, one home, the same rule that lets adoption derive them at all. A
    copy in this test could pass while the file the runner actually reads says
    something else.

    The false direction is the production-safety one and it is about RAISING,
    not about being wrong. Adopt treats a probe that errors as a hard failure,
    never as false, so a probe built on ``has_table_privilege`` naming a role
    that does not exist yet takes down first contact rather than reporting
    ``not applied``. Catalog reads answer ``false`` on the same database. The
    assertion below is therefore that these return false *without raising*.
    """

    @staticmethod
    def _probes(version: int) -> tuple:
        migration = next(
            m for m in discover_migrations(MIGRATIONS_DIR) if m.version == version
        )
        assert migration.postconditions, (
            f"migration {version:03d} carries no runner:postcondition lines, so"
            " adopt has nothing to derive adoption evidence from (#997)"
        )
        return migration.postconditions

    # The TRUE direction is already gated and is deliberately not duplicated
    # here. `test_lineage_lane.py::run_lane` replays the corpus through ONE
    # unbounded `apply_pending`, which crosses the schema move and applies 062;
    # `_run_postconditions` raises unless every line returns exactly true, so a
    # probe that could not read true where its own file had just applied fails
    # that lane rather than this file. A copy here would replay the same
    # lineage a second time to assert the same thing.
    #
    # What the lane CANNOT say is what a probe does on a database the file has
    # NOT reached, because it never asks one there. That is the direction below,
    # and it is the one production meets first.

    @pytest.mark.parametrize("version", [62])
    def test_probes_read_false_without_raising_before_the_target_lineage(
        self, version, at49_db, owner_actor
    ):
        """The false direction, which is the one that reaches production first.

        `at49_db` is the shape production is in: legacy lineage only, no target
        tables, and none of the seven service roles — no migration creates
        those, so a database that has only ever run migrations cannot have
        them.
        """
        as_owner = as_user(at49_db, owner_actor)
        for probe in self._probes(version):
            row = fetch_one(as_owner, probe)  # must not raise
            assert row[0] is False, (
                f"{version:03d} probe read true on a database predating it: {probe}"
            )


class TestEveryMigrationCarriesAdoptionEvidence:
    """#997 — the check whose absence let two files land without any.

    `_load_manifest` already refuses a file in its window carrying neither a
    manifest entry nor `runner:postcondition` lines. Nothing asked it that
    question about the corpus at HEAD: every `adopt` call in this file is
    bounded to ``LEGACY_LINEAGE_MAX``, so 051 onward were never in a window,
    while production's first contact is UNBOUNDED — the CLI passes no
    ``max_version``. Two files went green for a day on exactly that gap.

    No database and no fixture, which is the point: the cheapest possible gate
    on the thing that actually broke, run against the real manifest and the
    real corpus rather than a fixture pair.
    """

    #: EMPTY, and it must stay that way until a file genuinely has no evidence
    #: to carry. 063's exemption was spent when the open question it named — is
    #: a comment on `fn_clock_tick` warranted on its own merits? — was answered
    #: yes: the function is SECURITY DEFINER, runs the five scheduled legs that
    #: produce due work, and carried no comment while a single nullable timestamp
    #: column did. 063 now
    #: probes that comment's PRESENCE (never its text, which would be `prosrc`
    #: form-matching wearing a different hat).
    PARKED = frozenset()

    def test_the_unbounded_corpus_pairs_with_the_manifest(self):
        corpus = discover_migrations(MIGRATIONS_DIR)
        unpaired = set()
        try:
            _load_manifest(MANIFEST, corpus, corpus)
        except MigrationRunnerError as exc:
            unpaired = {int(v) for v in re.findall(r"\b(\d{3})\b", str(exc))}
            assert unpaired, f"could not parse the unpaired set from: {exc}"

        missing = unpaired - self.PARKED
        assert not missing, (
            "migration(s) "
            + ", ".join(f"{v:03d}" for v in sorted(missing))
            + " carry no adoption evidence, so `runner adopt` fails before it"
            " opens a connection — the #997 defect, again"
        )
        retired = self.PARKED - unpaired
        assert not retired, (
            "migration(s) "
            + ", ".join(f"{v:03d}" for v in sorted(retired))
            + " now carry adoption evidence, so the PARKED exemption above is"
            " spent — DELETE the entry rather than debugging this line. This"
            " assertion exists so the exemption cannot outlive its cause in"
            " silence (#997)"
        )
