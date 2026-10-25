"""The parity CLI compares complete owner-built catalogs and enforces floors."""

import psycopg2
import pytest

from scripts.migration_runner import main, parity
from tests.scripts.conftest import _scratch, build_world
from tests.unreadable_urls import (
    LIBPQ_UNREADABLE,
    OWNER_URL,
    READABLE,
    REFUSAL,
    assert_refused_by_name,
    assert_withheld,
    cases,
    forms,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def catalog_pair(admin_conn, owner_actor):
    """Share service roles until both owner-built databases have been dropped."""
    first = _scratch(admin_conn, owner=owner_actor, roles=[])
    first_db = next(first)
    try:
        first_owner = build_world(first_db, owner_actor, admin_conn)
        second = _scratch(admin_conn, owner=owner_actor, roles=None)
        second_db = next(second)
        try:
            second_owner = build_world(second_db, owner_actor, admin_conn)
            yield first_owner, second_owner
        finally:
            second.close()
    finally:
        first.close()


class TestParitySubcommand:
    def test_equal_schemas_exit_zero(self, catalog_pair, capsys):
        first, second = catalog_pair
        rc = main(["--database-url", first, "parity", "--against", second])

        assert rc == 0
        output = capsys.readouterr().out
        assert "0 difference(s)" in output
        assert "floor " not in output

    def test_differing_schemas_exit_nonzero(self, catalog_pair, capsys):
        first, second = catalog_pair
        conn = psycopg2.connect(first)
        try:
            with conn, conn.cursor() as cur:
                cur.execute("CREATE TABLE par_only_here (id INT)")
        finally:
            conn.close()

        rc = main(["--database-url", first, "parity", "--against", second])

        assert rc == 1
        output = capsys.readouterr().out
        assert "par_only_here" in output
        assert "floor " not in output

    @pytest.mark.parametrize("table_count", [0, 1])
    def test_matching_incomplete_catalogs_refused(
        self, scratch_db, second_scratch_db, table_count, capsys
    ):
        if table_count:
            for dsn in (scratch_db, second_scratch_db):
                conn = psycopg2.connect(dsn)
                try:
                    with conn, conn.cursor() as cur:
                        cur.execute("CREATE TABLE par_t (id INT)")
                finally:
                    conn.close()

        rc = main(
            ["--database-url", scratch_db, "parity", "--against", second_scratch_db]
        )

        assert rc == 1
        output = capsys.readouterr().out
        assert f"first floor tables: {table_count} < 44" in output
        assert f"second floor tables: {table_count} < 44" in output


class TestAnUnreadableUrlIsRefusedByItsName:
    """`parity` reads two catalogs, each through a URL libpq may not read. The
    refusal names which one, `DATABASE_URL (or --database-url)` or
    `--against`, and quotes neither, the way the writing verbs refuse theirs
    (`test_migration_runner.py`)."""

    @pytest.mark.parametrize("dsn", **cases(LIBPQ_UNREADABLE))
    def test_the_database_url_is_refused_by_name(self, dsn, capsys):
        """No database: the URL under test is the first one read."""
        rc = main(["--database-url", dsn, "parity", "--against", READABLE])

        out, err = capsys.readouterr()
        assert rc == 1
        assert f"error: {OWNER_URL} {REFUSAL}" in err
        assert_withheld(out, err)

        with pytest.raises(ValueError) as caught:
            parity(dsn, READABLE)
        assert_refused_by_name(caught.value, OWNER_URL)

    @pytest.mark.parametrize(
        "dsn", **cases(forms("invalid-escape-in-password", "raw-slash"))
    )
    def test_the_url_it_is_compared_against_is_refused_by_name(
        self, scratch_db, dsn, capsys
    ):
        """The first catalog is read from a real database, so the refusal is
        the second URL's."""
        rc = main(["--database-url", scratch_db, "parity", "--against", dsn])

        out, err = capsys.readouterr()
        assert rc == 1
        assert f"error: --against {REFUSAL}" in err
        assert_withheld(out, err)

        with pytest.raises(ValueError) as caught:
            parity(scratch_db, dsn)
        assert_refused_by_name(caught.value, "--against")
