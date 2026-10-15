"""Tenancy gate (#746) — the check that can see RLS.

`schema_parity` compares the runner-replayed schema against what
``Base.metadata.create_all`` builds from the models. That comparison cannot
carry RLS, because the models side cannot produce it: ``create_all`` emits
foreign keys and **zero** policies, zero RLS-enabled tables, zero triggers,
zero functions. Adding policies to the parity signature would not gate them —
it would make parity permanently red.

So tenancy is an **invariant over the replayed schema alone**, not a comparison
against the models: every workspace-keyed table is RLS-enabled and carries at
least one policy. Foreign keys, which both sides do emit, belong in the parity
signature instead and are added there rather than here.

Why this exists at all: a table's `ENABLE ROW LEVEL SECURITY` and its policies
belong in the same replay — tables are *born* tenant-scoped. Without this gate,
a migration creating a workspace-keyed table and omitting its policy would
pass every other check, and tenant isolation would be an assertion rather
than an enforced property.

Standalone stdlib + psycopg2, matching `migration_runner` and `schema_parity`:
the gate must be runnable in the same predeploy context, importing nothing from
``src``.
"""

from __future__ import annotations

import psycopg2

#: The column that marks a table as belonging to a tenant.
TENANT_KEY = "workspace_id"

#: `workspaces` IS the tenant: it keys on id. It carries the tenant policy pair
#: like any other tenant-plane table, so it is tenant-keyed for this gate's
#: purposes even though it has no `workspace_id` column.
TENANT_ROOT = "workspaces"

#: The posture on owner-bypass: ENABLE without FORCE, deliberately —
#: svc_migration owns the tables and owner-bypass is what lets the runner
#: transform without blanket migration policies.
#:
#: So FORCE is NOT required — requiring it would fail every table built to spec.
#: What IS gated is CONSISTENCY: every tenant-keyed table agrees. A mixed estate
#: is the dangerous state, because it reads as uniform to anyone spot-checking
#: one table, and the posture's rationale only holds if it is universal.
#: Flip this to True if the posture ever changes; the gate follows.
FORCE_REQUIRED = False

#: Tables that legitimately carry no tenant key. Kept as an explicit, empty
#: allowlist rather than omitted: user-plane tables and machinery tables
#: (counters, admission dedup) have no workspace column BY DESIGN,
#: and they are recognised here by the absence of the key, not by being named.
#: An entry belongs here only for a table that HAS a tenant key and is
#: nonetheless exempt — none exists, and an entry must say why it is
#: exempt.
TENANT_KEY_EXEMPT: set[str] = set()


def _tenancy_entry(
    *,
    tenant_keyed: bool,
    rls_enabled: bool = False,
    rls_forced: bool = False,
    policies: int = 0,
) -> dict:
    """The one spelling of a signature entry: the four facts the gate reads
    for a table, which `tenancy_signature` reads off a live catalog."""
    return {
        "tenant_keyed": tenant_keyed,
        "rls_enabled": rls_enabled,
        "rls_forced": rls_forced,
        "policies": policies,
    }


def tenancy_signature(dsn: str) -> dict:
    """Per public table: is it tenant-keyed, is RLS on, how many policies.

    Deliberately three plain facts rather than the policy bodies. The gate's
    question is "was this table born tenant-scoped", which the counts answer;
    asserting policy *text* here would duplicate the migrations and break on
    every legitimate rewording.
    """
    sig: dict[str, dict] = {}
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.relname,"
                "       c.relrowsecurity,"
                "       c.relforcerowsecurity,"
                "       EXISTS (SELECT 1 FROM information_schema.columns col"
                "               WHERE col.table_schema = 'public'"
                "                 AND col.table_name = c.relname"
                "                 AND col.column_name = %s)"
                "  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE n.nspname = 'public' AND c.relkind = 'r'",
                (TENANT_KEY,),
            )
            for name, rls, forced, has_key in cur.fetchall():
                sig[name] = _tenancy_entry(
                    tenant_keyed=bool(has_key) or name == TENANT_ROOT,
                    rls_enabled=bool(rls),
                    rls_forced=bool(forced),
                )

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


def tenant_keyed_tables(sig: dict) -> set[str]:
    """The tables this gate demands tenancy of — one definition, because the
    callers that DISCLOSE coverage must scope it the same way the gate scopes
    enforcement. Held apart from `tenancy_violations` so a disclosure can ask
    "is there anything here to check yet" without re-deriving the answer and
    drifting from it: a private copy that forgot `TENANT_KEY_EXEMPT` would go
    red naming a table the gate is deliberately silent about.
    """
    return {
        t for t, e in sig.items() if e["tenant_keyed"] and t not in TENANT_KEY_EXEMPT
    }


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
    keyed = tenant_keyed_tables(sig)

    # THIRD INVARIANT — owner-bypass posture (#750). Policies do
    # not apply to a table's OWNER unless FORCE is set, so ENABLE + policies +
    # no FORCE leaves the table readable wholesale by the owning role. The
    # posture accepts that deliberately (`FORCE_REQUIRED` says why); what it
    # cannot survive is a MIXED estate, where some tables are owner-bypassable
    # and some are not and nothing says which. Checked as agreement with
    # FORCE_REQUIRED so the gate states the posture rather than inferring it
    # from whatever landed first.
    forced = {t for t in keyed if sig[t].get("rls_forced")}
    deviating = (keyed - forced) if FORCE_REQUIRED else forced
    for table in sorted(deviating):
        want = (
            "FORCE is required"
            if FORCE_REQUIRED
            else "the posture is ENABLE without FORCE"
        )
        have = "not forced" if FORCE_REQUIRED else "FORCE is set"
        out.append(
            f"{table}: owner-bypass posture deviates — {have}, but {want}. "
            f"A mixed estate is the failure: policies do not "
            f"apply to the table OWNER unless FORCE is set, so half the tables "
            f"being owner-readable while the other half are not is invisible to "
            f"anyone spot-checking one of them"
        )

    for table in sorted(sig):
        entry = sig[table]
        if not entry["tenant_keyed"] or table in TENANT_KEY_EXEMPT:
            continue
        if not entry["rls_enabled"]:
            out.append(
                f"{table}: tenant-keyed but RLS is not enabled — ENABLE ROW LEVEL "
                f"SECURITY belongs in the same replay as the table"
            )
        elif not entry["policies"]:
            out.append(
                f"{table}: RLS enabled but carries no policy — every row is "
                f"denied to every login, which is not what 'born tenant-scoped' "
                f"means"
            )
    return out
