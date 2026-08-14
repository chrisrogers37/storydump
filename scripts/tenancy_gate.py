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

#: The ratified posture on owner-bypass (`02` §7-DDL, verbatim): "ENABLE without
#: FORCE, deliberately: svc_migration owns the tables and owner-bypass is what
#: lets the runner transform without blanket migration policies".
#:
#: So FORCE is NOT required — requiring it would fail every table built to spec.
#: What IS gated is CONSISTENCY: every tenant-keyed table agrees. A mixed estate
#: is the dangerous state, because it reads as uniform to anyone spot-checking
#: one table, and the plan's rationale only holds if the posture is universal.
#: Flip this to True if the posture is ever re-ratified; the gate follows.
FORCE_REQUIRED = False

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
                sig[name] = {
                    "tenant_keyed": bool(has_key) or name == TENANT_ROOT,
                    "rls_enabled": bool(rls),
                    "rls_forced": bool(forced),
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


def expected_tenancy(statements) -> dict:
    """The tenancy state a PREFIX of the advertised stream implies, in the same
    dict shape `tenancy_signature` reads off a live catalog.

    The two producers sit together deliberately: one derives from plan TEXT,
    the other observes a live CATALOG, and the whole value of comparing them is
    that they are different objects. A test that built both from the same source
    would be a tautology; this pair can disagree, and the disagreement is the
    finding — a statement the plan declares that the migration did not actually
    install shows up as a diff on one table rather than as a silent pass.

    WHY A PREFIX AND NOT A COMPLETE LINEAGE. Under #806 Fork 1 ruling (a) every
    F.2 increment is a contiguous segment of the stream, so the target lineage
    is always a positional prefix of it (that is what `f2_prefix_report` gates).
    The stream creates all 23 of `02`'s tables before the first ENABLE ROW LEVEL
    SECURITY, so "no tenant-keyed tables have landed yet" stops being true at
    F.2.2 and stays false until F.2.7 — a five-increment window in which a check
    written that way must either go red on schedule or be switched off. "The
    tenancy state matches what a prefix of this length implies" is true at EVERY
    increment instead, including the empty one, and it tightens by itself: at
    F.2.7 the implied state grows 23 RLS-enabled tables and 53 policies with no
    edit here. That is the alternative to bounding the check to a complete
    lineage, which buys green by going quiet for exactly the stretch the gate
    exists to cover.

    Note this is deliberately NOT `tenancy_violations` over a prefix. The
    invariant "every tenant-keyed table carries a policy" is FALSE mid-stream by
    the plan's own order, so asserting it early would be asserting the plan is
    wrong. What holds mid-stream is equality with the plan's own implication.

    Calibrated, not assumed: over the full 257-statement stream this derives 26
    tables and 19 tenant-keyed — the same counts a live replay of that stream
    observed (`test_advertised_ddl_replay`), with the other 7 carrying no
    workspace key by design (`02` §7-DDL Class 3/4).

    Takes normalized statements (`advertised_ddl.normalize_statements`) rather
    than raw SQL, so the caller owns the parse and this stays a pure function of
    a statement list. Dependency runs one way only — `advertised_ddl` declares
    itself stdlib-only, so the derivation lives here where the tenancy
    vocabulary and the signature shape already are, and that module never
    imports psycopg2 through the back door.
    """
    import re

    sig: dict[str, dict] = {}
    for stmt in statements:
        # Forward-only by construction: the stream builds, it never tears down.
        # A teardown would make everything below wrong in the quiet direction
        # (state derived as present that the replay has since dropped), so it
        # fails loudly rather than deriving an expectation that is not true.
        if re.match(r"DROP POLICY\b|ALTER TABLE .* DISABLE ROW LEVEL SECURITY", stmt):
            raise AssertionError(
                f"tenancy teardown in the advertised stream, which this "
                f"forward-only derivation cannot represent: {stmt[:120]}"
            )

        m = re.match(r"CREATE TABLE (?:IF NOT EXISTS )?(?:public\.)?(\w+) \((.*)", stmt)
        if m:
            name, body = m.group(1), m.group(2)
            sig[name] = {
                "tenant_keyed": bool(re.search(rf"\b{TENANT_KEY}\b", body))
                or name == TENANT_ROOT,
                "rls_enabled": False,
                "rls_forced": False,
                "policies": 0,
            }
            continue

        m = re.match(r"ALTER TABLE (?:public\.)?(\w+) ENABLE ROW LEVEL SECURITY", stmt)
        if m and m.group(1) in sig:
            sig[m.group(1)]["rls_enabled"] = True
            continue

        m = re.match(r"ALTER TABLE (?:public\.)?(\w+) FORCE ROW LEVEL SECURITY", stmt)
        if m and m.group(1) in sig:
            sig[m.group(1)]["rls_forced"] = True
            continue

        m = re.match(r"CREATE POLICY \S+ ON (?:public\.)?(\w+)", stmt)
        if m and m.group(1) in sig:
            sig[m.group(1)]["policies"] += 1

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

    # THIRD INVARIANT — owner-bypass posture (rajan, #750 review). Policies do
    # not apply to a table's OWNER unless FORCE is set, so ENABLE + policies +
    # no FORCE leaves the table readable wholesale by the owning role. The plan
    # accepts that deliberately and says why; what it cannot survive is a MIXED
    # estate, where some tables are owner-bypassable and some are not and
    # nothing says which. Checked as agreement with FORCE_REQUIRED so the gate
    # states the posture rather than inferring it from whatever landed first.
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
            f"{table}: owner-bypass posture deviates — {have}, but {want} "
            f"(`02` §7-DDL). A mixed estate is the failure: policies do not "
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
