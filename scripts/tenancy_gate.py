"""Tenancy gate (F.2.0, #746) — the check that can see RLS.

`schema_parity` compares the runner-replayed schema against what
``Base.metadata.create_all`` builds from the models. That comparison cannot
carry RLS, because the models side cannot produce it: measured on this repo,
``create_all`` emits 24 foreign keys and **zero** policies, zero RLS-enabled
tables, zero triggers, zero functions. Adding policies to the parity signature
would not gate them — it would make parity permanently red.

So tenancy is an **invariant over the replayed schema alone**, not a
comparison: every workspace-keyed table is RLS-enabled and carries at least one
policy. Foreign keys, which both sides do emit, belong in the parity signature
instead and are added there rather than here.

Why this exists at all: `02` §7 prints a table's `ENABLE ROW LEVEL SECURITY`
and its policies in the same replay — tables are *born* tenant-scoped. Nothing
in this repository could observe that. A migration creating a workspace-keyed
table and omitting its policy passed every existing check, which made FC-1 an
assertion in a document rather than an enforced property.

Standalone stdlib + psycopg2, matching `migration_runner` and `schema_parity`:
the gate must be runnable in the same predeploy context, importing nothing from
``src``.
"""

from __future__ import annotations

import psycopg2

#: The column that marks a table as belonging to a tenant (`02` §1, FC-1).
TENANT_KEY = "workspace_id"

#: `workspaces` IS the tenant — `02` §7-DDL Class 1: "workspaces keys on id
#: (it IS the tenant)". It carries the tenant policy pair like any other
#: tenant-plane table, so it is tenant-keyed for this gate's purposes even
#: though it has no `workspace_id` column.
TENANT_ROOT = "workspaces"

#: Tables that legitimately carry no tenant key. Kept as an explicit, empty
#: allowlist rather than omitted: `02` §7-DDL Class 3 (user-plane) and Class 4
#: (machinery counters, admission dedup) have no workspace column BY DESIGN,
#: and they are recognised here by the absence of the key, not by being named.
#: An entry belongs here only for a table that HAS a tenant key and is
#: nonetheless exempt — none exists today, and adding one should require
#: explaining why in review.
TENANT_KEY_EXEMPT: set[str] = set()


def tenancy_signature(dsn: str) -> dict:
    """Per public table: is it tenant-keyed, is RLS on, how many policies.

    Deliberately three plain facts rather than the policy bodies. The gate's
    question is "was this table born tenant-scoped", which the counts answer;
    asserting policy *text* here would duplicate the plan and break on every
    legitimate rewording.
    """
    sig: dict[str, dict] = {}
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.relname,"
                "       c.relrowsecurity,"
                "       EXISTS (SELECT 1 FROM information_schema.columns col"
                "               WHERE col.table_schema = 'public'"
                "                 AND col.table_name = c.relname"
                "                 AND col.column_name = %s)"
                "  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE n.nspname = 'public' AND c.relkind = 'r'",
                (TENANT_KEY,),
            )
            for name, rls, has_key in cur.fetchall():
                sig[name] = {
                    "tenant_keyed": bool(has_key) or name == TENANT_ROOT,
                    "rls_enabled": bool(rls),
                    "policies": 0,
                }

            cur.execute(
                "SELECT tablename, count(*) FROM pg_policies"
                " WHERE schemaname = 'public' GROUP BY tablename"
            )
            for name, count in cur.fetchall():
                if name in sig:
                    sig[name]["policies"] = count
    finally:
        conn.close()
    return sig


def tenancy_violations(sig: dict) -> list[str]:
    """Tables that are tenant-keyed but not born tenant-scoped.

    Two separate failures, reported separately because they are different
    mistakes with the same symptom in a schema dump:

    * **RLS off** — the policies may exist and are simply not enforced.
    * **RLS on, no policy** — enforced, denying everything, which reads as
      "secured" to anyone checking `relrowsecurity` alone and fails closed in a
      way that looks like a bug elsewhere.
    """
    out: list[str] = []
    for table in sorted(sig):
        entry = sig[table]
        if not entry["tenant_keyed"] or table in TENANT_KEY_EXEMPT:
            continue
        if not entry["rls_enabled"]:
            out.append(
                f"{table}: tenant-keyed but RLS is not enabled — `02` §7 prints "
                f"ENABLE ROW LEVEL SECURITY in the same replay as the table"
            )
        elif not entry["policies"]:
            out.append(
                f"{table}: RLS enabled but carries no policy — every row is "
                f"denied to every login, which is not what 'born tenant-scoped' "
                f"means"
            )
    return out
