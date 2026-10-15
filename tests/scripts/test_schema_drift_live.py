"""Live schema drift: does a running database carry the schema the repo builds?

The expected side is a scratch database the schema builder stands up from the
code, through `src.models.target.ddl.build`, the call `make init-db` makes. The
other side is the database `SCHEMA_DRIFT_DSN` names. Both are read with
`schema_parity.schema_signature` and compared with `schema_diff`.

## Why it reads the live schema and never the ledger

`runner.schema_migrations` describes what the runner applied. It cannot
describe a change made around the runner, by hand or by any other tool, so a
database whose ledger is complete can still differ from the code. This compares
what the catalog holds, on both sides.

## Why this is not a pull-request gate

A deployed database lags `main` between a merge and its deploy, so a check
against it is legitimately red for every schema change in that window. Red would
be its normal state, and a gate whose normal state is red gets weakened by
whoever hits it first. It therefore SKIPS unless a DSN is supplied, and FAILS
when `REQUIRE_SCHEMA_DRIFT_CHECK=1` is set without one.

## Bounds: what this cannot see

- **`public` only.** `schema_parity` scopes every query to `public`, so every
  other schema, `archive` and the runner's ledger among them, is outside the
  comparison.
- **Structure, not data.** Tables, columns (type and nullability), CHECK
  constraints, uniqueness and foreign keys: the dimensions `schema_parity`
  compares, whose own docstring lists what it leaves out (column defaults,
  non-unique indexes and the rest).
- **The signature reads no function, trigger, row-level security setting,
  policy, grant, sequence or materialized view.** The builder creates every
  kind but the last on the expected side, and a change to any of them reports
  clean.
- **Views are compared by their columns only.** `information_schema.columns`
  includes views, so one gained or dropped is caught, while one whose
  definition changed behind an unchanged column list is not.
- **It never writes to the audited database.** Both sides are read with
  catalog queries; the expected side is built in a scratch database.
"""

import os

import pytest

from scripts.schema_parity import schema_diff, schema_signature
from tests.scripts.conftest import _scratch

#: Read-only connection string for the database to audit. Absent on pull
#: requests by design.
DSN_ENV = "SCHEMA_DRIFT_DSN"

#: Set where the audit must run, so a missing DSN FAILS instead of skipping.
#: Same reasoning as `REQUIRE_TEST_DATABASE` in `tests/conftest.py`: a job whose
#: only check silently skipped still exits 0 and shows a green tick, and "could
#: not ask" must never render as "asked and found nothing".
REQUIRE_ENV = "REQUIRE_SCHEMA_DRIFT_CHECK"


@pytest.fixture
def drift_dsn() -> str:
    """The DSN to audit, or skip — unless the caller demanded a real answer.

    A FIXTURE rather than a call in the test body, and requested FIRST, so the
    skip lands before the expected database is built. On a pull request
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
    pytest.skip(
        f"{DSN_ENV} not set — live drift is audited only where a DSN is supplied"
    )


@pytest.fixture
def built_db(drift_dsn, admin_conn):
    """A scratch database the schema builder stands up, as `make init-db` does:
    `build` on an empty database, connected as the role that owns it.

    It depends on `drift_dsn`, so the skip lands before anything is built. The
    builder creates the service roles it finds missing, and roles belong to the
    whole cluster, so the database comes with the suite's role bracket: the
    roles are absent before it and dropped after it, the database first.
    """
    from sqlalchemy import create_engine

    from src.models.target.ddl import build

    gen = _scratch(admin_conn, roles=[])
    dsn = next(gen)
    try:
        engine = create_engine(dsn)
        try:
            with engine.connect() as conn:
                build(conn)
        finally:
            engine.dispose()
        yield dsn
    finally:
        gen.close()


class TestLiveSchemaDrift:
    def test_live_public_schema_matches_the_built_schema(self, drift_dsn, built_db):
        """The schema the builder stands up equals the audited database's, on
        every dimension `schema_parity` compares."""
        expected = schema_signature(built_db)
        live = schema_signature(drift_dsn)

        # NON-VACUITY, both halves. An empty signature on either side makes the
        # diff below pass by comparing nothing — the failure mode a drift
        # monitor must never have, since it reports clean at exactly the moment
        # it has stopped looking.
        assert expected, "the expected schema is empty — the builder built nothing"
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
