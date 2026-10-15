"""``runner parity`` — the operator door onto the schema comparator:
two live databases compared DSN-vs-DSN (zero src imports preserved)."""

import psycopg2
import pytest

from scripts.migration_runner import main

pytestmark = pytest.mark.integration


class TestParitySubcommand:
    def test_equal_schemas_exit_zero(self, scratch_db, second_scratch_db):
        for dsn in (scratch_db, second_scratch_db):
            with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
                cur.execute("CREATE TABLE par_t (id INT)")
            conn.close()

        rc = main(
            ["--database-url", scratch_db, "parity", "--against", second_scratch_db]
        )

        assert rc == 0

    def test_differing_schemas_exit_nonzero(
        self, scratch_db, second_scratch_db, capsys
    ):
        with psycopg2.connect(scratch_db) as conn, conn.cursor() as cur:
            cur.execute("CREATE TABLE par_only_here (id INT)")
        conn.close()

        rc = main(
            ["--database-url", scratch_db, "parity", "--against", second_scratch_db]
        )

        assert rc == 1
        assert "par_only_here" in capsys.readouterr().out
