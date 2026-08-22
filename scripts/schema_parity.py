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

What this gate DOES NOT compare, and why each omission is structural
--------------------------------------------------------------------

Stated because a scoped green tick whose scope is invisible gets read under
time pressure as "the schemas match". It means "the schemas match on the
dimensions below, by design". Measured against `main` (runner-replayed vs
``create_all``), the excluded dimensions hold **152 divergences** — every one
of them expected:

``schema_version`` (table)
    The legacy self-stamp table exists only on the migration-built side by
    design: models never declared it, and the runner ledger supersedes it in
    the ``runner`` schema, outside this comparison.

Column defaults — **65 diverging**
    SQLAlchemy ``default=`` is applied in **Python** and emits no DDL
    ``DEFAULT``; only ``server_default=`` would. So the migration side carries
    ``CURRENT_TIMESTAMP`` / ``uuid_generate_v4()`` where the models side
    carries nothing, on every stamped column in the schema::

        api_tokens.id          migrated: uuid_generate_v4()   orm: None
        api_tokens.created_at  migrated: CURRENT_TIMESTAMP    orm: None

    Both are correct: an ORM insert gets its value from Python, a raw SQL
    insert from the DDL. Comparing this axis would report all 65 forever.

Non-unique indexes — **87 diverging**
    Excluded because a non-unique index carries **no correctness guarantee**.
    It is a query-planning object; nothing about the data is different if it
    is absent, differently named, or differently shaped. Unique indexes ARE
    compared — uniqueness constrains data, which is why they sit above with
    the constraints rather than here.

    The 87 decompose into THREE categories, not two, and the decomposition is
    worth stating because both shorter summaries are wrong::

        54  (27 pairs)  same table + columns + predicate, different NAME
        18  ( 9 pairs)  same table + columns, DIFFERENT PREDICATE
        15              ORPHANS — present on one side, no counterpart at all

    **Naming (54).** The only group that is "same guarantee, different catalog
    rows" — the case this module already treats as equal for unique indexes.

    **Partial vs unconditional (18).** A deliberate divergence in SHAPE: the
    migrations hand-write partial indexes where ``index=True``, which takes no
    predicate, emits unconditional ones ::

        migrated:  ... (auth_method) WHERE (auth_method IS NOT NULL)
        orm:       ... (auth_method)

    Two valid indexes over one column, different planner behaviour, identical
    correctness.

    **Orphans (15).** A different situation, and worth telling apart from the
    above: not two shapes of one index but an index that is simply absent on
    one side. **All 15 are migration-only** (zero models-only), from three
    causes — 5 multi-column (``index=True`` is per-column and has no composite
    form), 5 single-column-with-a-predicate (same limitation), and 5 plain
    single-column indexes the models just do not declare.

    **The "no correctness guarantee" reason above covers orphans too, and this
    is stated rather than left to inherit:** an index that is absent on one
    side is a planner difference, not a data-integrity one — exactly as a
    differently-shaped or differently-named one is. Nothing about the data
    differs because a non-unique index is missing.

    Two summaries are false and are named as false so they are not re-derived:
    "all 87 are naming" (the first draft of #957) and "the 33 non-naming are
    all partial-vs-unconditional" (its correction). The measured answer is the
    three categories above.

Triggers — **0 diverging**
    Not compared, and currently identical anyway. Listed so a future
    divergence here is a known blind spot rather than a discovery.

**Do not "fix" these omissions by adding the dimensions.** Each would report
its full population as a failure on the first run, permanently, and the
natural next step is to weaken or delete the gate. If comparing one of them
ever becomes genuinely worth doing, it is a separate decision that must carry
these numbers with it (#957).
"""

import psycopg2

#: Tables the models deliberately do not declare, so parity must not read
#: their absence as drift. Both are database-side artifacts with no ORM
#: reader: ``schema_version`` is the legacy lineage ledger, and
#: ``posting_history_dedup_archive`` is the one-time forensic copy migration
#: 050 takes before reducing the duplicate groups (#695) — retained
#: indefinitely because for three of its rows a distinct ``instagram_story_id``
#: is the only surviving evidence that a story published more than once.
#: Declaring a model for either would assert the application manages it.
EXCLUDED_TABLES = {"schema_version", "posting_history_dedup_archive"}


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
