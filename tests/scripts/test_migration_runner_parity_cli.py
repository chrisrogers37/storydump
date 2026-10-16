"""The parity CLI compares complete owner-built catalogs and enforces floors."""

import psycopg2
import pytest

from scripts.migration_runner import main
from tests.scripts.conftest import _scratch, build_world

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
