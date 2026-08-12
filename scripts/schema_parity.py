"""Schema-parity comparator (plan §0.2 gate): does one database's public
schema equal another's?

Lives beside the runner (same standalone constraints — stdlib + psycopg2,
zero ``src`` imports) so it serves two callers: the CI gate compares a
runner-replayed database against a models-built one, and an operator can ask
the same question of production via ``runner parity --against <dsn>``.

Compares what the drift class actually breaks on — tables, columns
(type + nullability), CHECK constraints (by name and normalized definition,
because migrations drop and re-add them by name), and uniqueness semantics
(as column sets + partial predicates, sourced from BOTH unique constraints
and unique indexes: inline SQL produces constraints while SQLAlchemy's
``unique=True, index=True`` produces a unique index — same guarantee,
different catalog rows, deliberately treated as equal).

Exclusion: ``schema_version`` — the legacy self-stamp table exists only on
the migration-built side by design (models never declared it; the runner
ledger supersedes it and lives in the ``runner`` schema, outside this
comparison).
"""

import psycopg2

EXCLUDED_TABLES = {"schema_version"}


def schema_signature(dsn: str) -> dict:
    sig = {}
    conn = psycopg2.connect(dsn)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name, data_type, is_nullable"
            " FROM information_schema.columns WHERE table_schema = 'public'"
        )
        for table, column, data_type, nullable in cur.fetchall():
            if table in EXCLUDED_TABLES:
                continue
            entry = sig.setdefault(
                table, {"columns": {}, "checks": {}, "uniques": set(), "fks": set()}
            )
            entry["columns"][column] = (data_type, nullable)

        cur.execute(
            "SELECT c.conrelid::regclass::text, c.conname,"
            "       pg_get_constraintdef(c.oid)"
            " FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace"
            " WHERE n.nspname = 'public' AND c.contype = 'c'"
        )
        for table, name, definition in cur.fetchall():
            if table in sig:
                sig[table]["checks"][name] = " ".join(definition.split())

        # Uniqueness semantics: unique constraints AND unique indexes,
        # keyed on (columns, predicate) — never on the historical name.
        cur.execute(
            "SELECT t.relname,"
            "       array_agg(a.attname ORDER BY a.attname),"
            "       coalesce(pg_get_expr(ix.indpred, ix.indrelid), '')"
            " FROM pg_index ix"
            " JOIN pg_class i ON i.oid = ix.indexrelid"
            " JOIN pg_class t ON t.oid = ix.indrelid"
            " JOIN pg_namespace n ON n.oid = t.relnamespace"
            " JOIN pg_attribute a ON a.attrelid = t.oid"
            "  AND a.attnum = ANY(ix.indkey)"
            " WHERE n.nspname = 'public' AND ix.indisunique"
            "  AND NOT ix.indisprimary"
            " GROUP BY t.relname, ix.indexrelid, ix.indpred, ix.indrelid"
        )
        for table, columns, predicate in cur.fetchall():
            if table in sig:
                sig[table]["uniques"].add((tuple(columns), " ".join(predicate.split())))
        # Foreign keys. Added F.2.0: both sides emit them (measured —
        # create_all produced 24), so unlike RLS they are comparable, and a
        # composite FK dropped on one side is exactly the drift this gate is
        # for. Compared by definition text rather than name: the name is
        # historical, the referential shape is the contract.
        cur.execute(
            "SELECT c.conrelid::regclass::text, pg_get_constraintdef(c.oid)"
            " FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace"
            " WHERE n.nspname = 'public' AND c.contype = 'f'"
        )
        for table, definition in cur.fetchall():
            if table in sig:
                sig[table]["fks"].add(" ".join(definition.split()))
    conn.close()
    return sig


def schema_diff(replayed: dict, models: dict) -> list:
    """Human-readable differences; empty means parity."""
    diffs = []
    for table in sorted(set(replayed) - set(models)):
        diffs.append(f"table {table} exists only in the first schema")
    for table in sorted(set(models) - set(replayed)):
        diffs.append(f"table {table} exists only in the second schema")
    for table in sorted(set(replayed) & set(models)):
        r, m = replayed[table], models[table]
        for column in sorted(set(r["columns"]) | set(m["columns"])):
            rv, mv = r["columns"].get(column), m["columns"].get(column)
            if rv != mv:
                diffs.append(f"column {table}.{column}: first={rv} second={mv}")
        for name in sorted(set(r["checks"]) | set(m["checks"])):
            rv, mv = r["checks"].get(name), m["checks"].get(name)
            if rv != mv:
                diffs.append(f"check {table}.{name}: first={rv} second={mv}")
        for unique in sorted(r["uniques"] - m["uniques"]):
            diffs.append(f"unique {table}{unique}: only in first schema")
        for unique in sorted(m["uniques"] - r["uniques"]):
            diffs.append(f"unique {table}{unique}: only in second schema")
        for fk in sorted(r.get("fks", set()) - m.get("fks", set())):
            diffs.append(f"fk {table}: only in first schema — {fk}")
        for fk in sorted(m.get("fks", set()) - r.get("fks", set())):
            diffs.append(f"fk {table}: only in second schema — {fk}")
    return diffs
