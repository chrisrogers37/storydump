"""Live schema drift — does a running database carry what the repo declares?

#1195. Migration 066 merged 2026-08-31 and production's highest applied
migration was 65 for a day, and nothing anywhere noticed. This is the check
that would have noticed.

## Why the existing gates could not catch it

Both lineages already gate models against migrations, and both were green
through the whole window:

- `test_migration_gate.py::TestSchemaParity` — the legacy lineage.
- `test_lineage_lane.py` — the target lineage, whose parity assertion this
  test deliberately mirrors so the two cannot drift apart in method.

They compare the repo against itself. 066's migration and its model agreed
perfectly; what disagreed was **production**, which no test had ever been
pointed at. Repo-internal parity is necessary and is not the same property.

## Why it reads LIVE SCHEMA and never the ledger

`runner.schema_migrations` read entirely healthy while this was wrong: rows
1-65, no holes, zero checksum mismatches. It was also blind, in a way that
cannot be fixed by reading it more carefully — #840's objects
(`uq_posting_history_queue_item_id`, `posting_history_dedup_archive`) are in
production with no ledger row at all, applied outside the runner. **A ledger
describes what the runner did; it cannot describe what a human did around it.**
So the comparison here is against `information_schema`, and a ledger-reading
variant of this check would be theatre.

## Why this is not a pull-request gate

Production lags `main` by design between merge and deploy, so prod-versus-repo
is legitimately red for every migration PR from merge until deploy. Red would
be its normal state, and a gate whose normal state is red gets weakened by
whoever hits it first. It therefore SKIPS unless a target DSN is supplied, and
only the scheduled `schema-drift` workflow supplies one. The blocking version
of this belongs at deploy time — `railway.toml`'s dormant `preDeployCommand` —
which is #1195's other half and an operator decision.

## Bounds — what this cannot see

- **`public` only.** `schema_parity` scopes every query to `nspname='public'`,
  so the target schema is covered and `legacy` is not. The #840 objects quoted
  above live in `legacy` and this check would NOT report them; they are cited
  as why ledger-reading fails, not as something this catches.
- **Structure, not data.** Columns (type + nullability), CHECK constraints,
  uniqueness and foreign keys — the dimensions `schema_parity` compares, whose
  own docstring enumerates the deliberate omissions (column defaults, index
  shape, and the rest).
- **It never writes**, to production or anywhere. Both sides are read with
  `information_schema` queries; the expected side is built in a scratch
  database.
"""

import os

import pytest

from scripts.advertised_ddl import target_lineage_files
from scripts.schema_parity import schema_diff, schema_signature
from src.utils.validators import MIGRATIONS_DIR
from tests.scripts.conftest import psql_apply

#: Read-only connection string for the database to audit. Absent on pull
#: requests by design; supplied only by the scheduled workflow.
DSN_ENV = "SCHEMA_DRIFT_DSN"

#: Set by the scheduled workflow so a missing DSN FAILS instead of skipping.
#: Same reasoning as `REQUIRE_TEST_DATABASE` in `tests/conftest.py`: a job whose
#: only check silently skipped still exits 0 and shows a green tick, and "could
#: not ask" must never render as "asked and found nothing".
REQUIRE_ENV = "REQUIRE_SCHEMA_DRIFT_CHECK"


@pytest.fixture
def drift_dsn() -> str:
    """The DSN to audit, or skip — unless the caller demanded a real answer.

    A FIXTURE rather than a call in the test body, and requested FIRST, so the
    skip lands before `second_scratch_db` builds anything. On a pull request
    this check is meant to cost nothing, and a scratch database created only to
    be thrown away is not nothing.
    """
    dsn = os.environ.get(DSN_ENV, "").strip()
    if dsn:
        return dsn
    if os.environ.get(REQUIRE_ENV) == "1":
        raise AssertionError(
            f"{REQUIRE_ENV}=1 but {DSN_ENV} is empty — the drift check could not"
            " run. This is NOT a clean schema result; it is an unwired monitor."
        )
    pytest.skip(f"{DSN_ENV} not set — live drift is audited on a schedule only")


class TestLiveSchemaDrift:
    def test_live_public_schema_matches_the_target_models(
        self, drift_dsn, second_scratch_db
    ):
        """The repo's declared target schema equals the audited database's.

        The expected side is built exactly as `test_lineage_lane.py` builds it
        — the lineage head's shared functions, then `create_all` — rather than
        by replaying the whole corpus, because the lane test already pins that
        replay and the models to each other. Reusing its recipe keeps this a
        third comparison against the same two proven sides, not a fourth
        opinion about what the schema should be.
        """
        # Function prerequisite only — it supplies shared functions and creates
        # no relations. This test does not depend on that being asserted
        # elsewhere: anything it did create would land on the expected side and
        # be reported below as a table present here and absent there, which is
        # the honest reading of an unexpected relation anyway.
        psql_apply(second_scratch_db, [target_lineage_files(MIGRATIONS_DIR)[0]])

        from sqlalchemy import create_engine

        from src.models.target import TargetBase

        engine = create_engine(second_scratch_db)
        TargetBase.metadata.create_all(engine)
        engine.dispose()

        expected = schema_signature(second_scratch_db)
        live = schema_signature(drift_dsn)

        # NON-VACUITY, both halves. An empty signature on either side makes the
        # diff below pass by comparing nothing — the failure mode a drift
        # monitor must never have, since it reports clean at exactly the moment
        # it has stopped looking.
        assert expected, "the expected schema is empty — `create_all` built nothing"
        assert live, (
            "the audited database's public schema is empty — this is a wrong"
            " DSN or a wrong schema, not a clean result"
        )

        diffs = schema_diff(expected, live)
        assert diffs == [], (
            "live schema drift (first = declared by this repo, second = live in"
            " the audited database):\n"
            + "\n".join(diffs)
            + "\n\nA column present in the first and absent in the second is an"
            " UNAPPLIED MIGRATION. The reverse is an object applied outside the"
            " repo. See #1195."
        )
