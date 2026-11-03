"""Compare portable PostgreSQL catalog snapshots.

Only reference rows in ``public.post_intent_transitions`` are data in the
signature; their created_at timestamps are reported as runtime metadata.
Archive and runner relations, extension members and system objects
are excluded. Comments and outside-role grants are reported for review; raw
function bodies remain part of the comparison.
Memberships held by service roles or the database owner are compared regardless
of the granted role. Grantor provenance is reported separately; admin power and,
on PostgreSQL 16 and later, inheritance and SET ROLE are compared across every
grant of the same membership edge.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import libpq_connect
from scripts.sql_lexer import CODE, COMMENTS, segments
from src.config.db_url import DATABASE_URL_NAME

FORMAT_VERSION = 2
MEMBERSHIP_OPTIONS = ("admin_option", "inherit_option", "set_option")
FLOORS = {
    "tables": 44,
    "functions": 87,
    "policies": 115,
    "triggers": 53,
    "rls_tables": 44,
}
DIMENSIONS = (
    "schemas",
    "relations",
    "columns",
    "constraints",
    "indexes",
    "sequences",
    "functions",
    "triggers",
    "internal_triggers",
    "policies",
    "types",
    "views",
    "extensions",
    "roles",
    "settings",
    "memberships",
    "database",
    "default_acls",
    "event_triggers",
    "publications",
    "publication_tables",
    "publication_schemas",
    "statistics",
    "reference_rows",
    "inherits",
    "foreign_tables",
    "foreign_servers",
    "foreign_data_wrappers",
    "user_mappings",
    "rules",
)


def collapse(text):
    """Normalize deparser layout, preserving a missing value."""
    if text is None:
        return None
    return "".join(
        re.sub(r"\s+", " ", s.text) if s.kind == CODE else s.text
        for s in segments(text)
    ).strip()


def body_diagnostic(text):
    """Comment-free comparison aid; never a substitute for raw ``prosrc``."""
    return " ".join(
        "".join(" " if s.kind in COMMENTS else s.text for s in segments(text)).split()
    )


def normalize_database_properties(properties):
    """Use common PostgreSQL 15–17 locale keys without discarding their values."""
    normalized = dict(properties)
    if "daticulocale" in normalized:
        normalized["datlocale"] = normalized.pop("daticulocale")
    normalized.setdefault("daticurules", None)
    return normalized


def normalize_role(name, owner):
    return "<owner>" if name in (owner, "pg_database_owner") else name


def normalize_acl(entries, owner):
    """Drop grantor/MAINTAIN and separate grants to unrelated roles."""
    compared, outside = [], []
    for item in entries:
        if item["privilege_type"] == "MAINTAIN":
            continue
        role = normalize_role(item["grantee"], owner)
        entry = {
            "grantee": role,
            "privilege": item["privilege_type"],
            "grantable": item["is_grantable"],
        }
        bucket = (
            compared
            if role == "<owner>" or role == "PUBLIC" or role.startswith("svc_")
            else outside
        )
        bucket.append(entry)

    def key(x):
        return json.dumps(x, sort_keys=True)

    return sorted({key(x): x for x in compared}.values(), key=key), sorted(
        outside, key=key
    )


def normalize_membership(row, owner, version):
    """Compare explicit membership edges with their options; report creator
    grants and option metadata. Before PostgreSQL 16 a grant has no INHERIT or
    SET option to compare."""
    normalized = dict(row)
    for name in ("role", "member", "grantor"):
        normalized[name] = normalize_role(normalized[name], owner)
    options = dict(normalized) if version >= 160000 else None
    automatic = version >= 160000 and (
        normalized["member"] == "<owner>"
        and normalized["grantor"] == "<owner>"
        and normalized["role"].startswith("svc_")
        and normalized["admin_option"]
        and not normalized["inherit_option"]
        and not normalized["set_option"]
    )
    if version < 160000:
        normalized.pop("inherit_option", None)
        normalized.pop("set_option", None)
    return (None if automatic else normalized), options


def _user_schema(alias="n"):
    return f"{alias}.nspname !~ '^pg_' AND {alias}.nspname <> 'information_schema'"


def _member_free(catalog, alias):
    return f"NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_catalog.{catalog}'::regclass AND d.objid = {alias}.oid AND d.deptype = 'e')"


def _relation_scope(alias="c", namespace="n"):
    return f"{_user_schema(namespace)} AND {namespace}.nspname NOT IN ('archive', 'runner') AND {_member_free('pg_class', alias)}"


def extract(conn, *, read_only=False):
    """Read one repeatable catalog snapshot and roll the transaction back.

    ``read_only`` also asks PostgreSQL to prohibit writes in that transaction.
    The caller supplies an idle connection; no extraction commits anything.
    """
    conn.set_session(readonly=read_only, isolation_level="REPEATABLE READ")
    snapshot = {
        "format_version": FORMAT_VERSION,
        "catalog": {k: {} for k in DIMENSIONS},
        "reported": {
            "outside_roles": {},
            "comments": [],
            "membership_options": [],
            "membership_grants": [],
            "function_bodies": {},
            "archive_relations": [],
            "reference_metadata": {},
        },
    }
    catalog, reported = snapshot["catalog"], snapshot["reported"]
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SET LOCAL search_path = pg_catalog")
            cur.execute("SHOW server_version_num")
            version = int(cur.fetchone()["server_version_num"])
            reported["server_version_num"] = version
            cur.execute(
                "SELECT r.rolname FROM pg_database d JOIN pg_roles r ON r.oid = d.datdba WHERE d.datname = current_database()"
            )
            owner = cur.fetchone()["rolname"]

            def rows(sql):
                cur.execute(sql)
                return [dict(row) for row in cur.fetchall()]

            def acl(raw, kind, role, key):
                if raw == []:
                    return []
                cur.execute(
                    "SELECT CASE WHEN x.grantee = 0 THEN 'PUBLIC' ELSE r.rolname END AS grantee, x.privilege_type, x.is_grantable FROM aclexplode(coalesce(%s::aclitem[], acldefault(%s::\"char\", %s::oid))) x LEFT JOIN pg_roles r ON r.oid = x.grantee",
                    (raw, kind, role),
                )
                compared, outside = normalize_acl(
                    [dict(r) for r in cur.fetchall()], owner
                )
                if outside:
                    reported["outside_roles"][key] = outside
                return compared

            def put(dimension, row, *, acl_kind=None, deparse=()):
                key = row.pop("key")
                if "owner" in row:
                    row["owner"] = normalize_role(row["owner"], owner)
                if acl_kind is not None:
                    row["acl"] = acl(
                        row.pop("acl"),
                        acl_kind,
                        row.pop("owner_oid"),
                        f"{dimension}.{key}",
                    )
                for name in deparse:
                    row[name] = collapse(row[name])
                catalog[dimension][key] = row

            for row in rows(
                f"SELECT n.nspname AS key, r.rolname AS owner, n.nspowner AS owner_oid, n.nspacl::text[] AS acl FROM pg_namespace n JOIN pg_roles r ON r.oid=n.nspowner WHERE {_user_schema()} AND {_member_free('pg_namespace', 'n')}"
            ):
                put("schemas", row, acl_kind="n")
            relations = rows(
                f"SELECT c.oid, n.nspname || '.' || quote_ident(c.relname) AS key, c.relkind AS kind, c.relpersistence AS persistence, c.relrowsecurity AS rls_enabled, c.relforcerowsecurity AS rls_forced, c.reloptions AS options, c.relreplident AS replica_identity, c.relispartition AS is_partition, pg_get_expr(c.relpartbound,c.oid) AS partition_bound, CASE WHEN c.relkind='p' THEN pg_get_partkeydef(c.oid) END AS partition_key, ts.spcname AS tablespace, am.amname AS access_method, r.rolname AS owner, c.relowner AS owner_oid, c.relacl::text[] AS acl FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_roles r ON r.oid=c.relowner LEFT JOIN pg_tablespace ts ON ts.oid=c.reltablespace LEFT JOIN pg_am am ON am.oid=c.relam WHERE {_relation_scope()}"
            )
            relation_ids = []
            for row in relations:
                relation_ids.append(row.pop("oid"))
                if row["options"] is not None:
                    row["options"] = sorted(row["options"])
                put(
                    "relations",
                    row,
                    acl_kind="S" if row["kind"] == "S" else "r",
                    deparse=("partition_bound", "partition_key"),
                )
            allowed = ",".join(str(n) for n in relation_ids) or "0"
            relname = "n.nspname || '.' || quote_ident(c.relname)"
            for row in rows(
                f"SELECT {relname} || '.' || quote_ident(a.attname) AS key, row_number() OVER (PARTITION BY a.attrelid ORDER BY a.attnum) AS position, format_type(a.atttypid,a.atttypmod) AS type, a.attnotnull AS not_null, pg_get_expr(ad.adbin,ad.adrelid) AS default, a.attidentity AS identity, a.attgenerated AS generated, cn.nspname || '.' || quote_ident(co.collname) AS collation, a.attacl::text[] AS acl, c.relowner AS owner_oid, a.attstorage AS storage, a.attcompression AS compression, a.attoptions AS options, a.attfdwoptions AS foreign_options, a.attislocal AS is_local, a.attinhcount AS inheritance_count FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace LEFT JOIN pg_attrdef ad ON ad.adrelid=a.attrelid AND ad.adnum=a.attnum LEFT JOIN pg_collation co ON co.oid=a.attcollation LEFT JOIN pg_namespace cn ON cn.oid=co.collnamespace WHERE a.attrelid IN ({allowed}) AND a.attnum>0 AND NOT a.attisdropped"
            ):
                # Column ACL defaults are empty, unlike table ACL defaults.
                row["acl"] = row["acl"] or []
                put("columns", row, acl_kind="c", deparse=("default",))
            for row in rows(
                f"SELECT n.nspname || '.' || quote_ident(co.conname) || '@' || coalesce(c.relname,t.typname) AS key, co.contype AS kind, pg_get_constraintdef(co.oid,true) AS definition, co.condeferrable AS deferrable, co.condeferred AS deferred, co.convalidated AS validated, co.conislocal AS is_local, co.coninhcount AS inheritance_count, co.connoinherit AS no_inherit FROM pg_constraint co JOIN pg_namespace n ON n.oid=co.connamespace LEFT JOIN pg_class c ON c.oid=co.conrelid LEFT JOIN pg_type t ON t.oid=co.contypid WHERE (co.conrelid IN ({allowed}) OR (co.conrelid=0 AND {_user_schema()})) AND {_member_free('pg_constraint', 'co')}"
            ):
                put("constraints", row, deparse=("definition",))
            for row in rows(
                f"SELECT {relname} AS key, pg_get_indexdef(i.indexrelid) AS definition, (to_jsonb(i) - ARRAY['indexrelid','indrelid','indkey','indcollation','indclass','indexprs','indpred']) AS properties FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE i.indrelid IN ({allowed})"
            ):
                put("indexes", row, deparse=("definition",))
            for row in rows(
                f"SELECT {relname} AS key, format_type(s.seqtypid,NULL) AS type, s.seqstart AS start, s.seqincrement AS increment, s.seqmax AS maximum, s.seqmin AS minimum, s.seqcache AS cache, s.seqcycle AS cycle, (SELECT tn.nspname || '.' || quote_ident(tc.relname) || '.' || quote_ident(a.attname) FROM pg_depend d JOIN pg_class tc ON tc.oid=d.refobjid JOIN pg_namespace tn ON tn.oid=tc.relnamespace JOIN pg_attribute a ON a.attrelid=tc.oid AND a.attnum=d.refobjsubid WHERE d.classid='pg_class'::regclass AND d.objid=c.oid AND d.refclassid='pg_class'::regclass AND d.deptype IN ('a','i') LIMIT 1) AS owned_by FROM pg_sequence s JOIN pg_class c ON c.oid=s.seqrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.oid IN ({allowed})"
            ):
                put("sequences", row)
            for row in rows(
                f"SELECT p.oid, n.nspname || '.' || quote_ident(p.proname) || '(' || pg_get_function_identity_arguments(p.oid) || ')' AS key, r.rolname AS owner, p.proowner AS owner_oid, p.proacl::text[] AS acl, l.lanname AS language, p.prosrc AS prosrc, p.probin AS binary, p.prokind AS kind, p.provolatile AS volatility, p.proparallel AS parallel, p.prosecdef AS security_definer, p.proleakproof AS leakproof, p.proisstrict AS strict, p.proretset AS returns_set, p.procost AS cost, p.prorows AS rows, p.pronargs AS argument_count, p.pronargdefaults AS default_count, p.provariadic::regtype::text AS variadic_type, p.prosupport::regproc::text AS support, p.proconfig AS config, ARRAY(SELECT transform_type::regtype::text FROM unnest(p.protrftypes) transform_type) AS transform_types, pg_get_function_arguments(p.oid) AS arguments, pg_get_function_result(p.oid) AS result, CASE WHEN p.prokind <> 'a' THEN pg_get_functiondef(p.oid) END AS definition FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_roles r ON r.oid=p.proowner JOIN pg_language l ON l.oid=p.prolang WHERE {_user_schema()} AND {_member_free('pg_proc', 'p')}"
            ):
                oid = row.pop("oid")
                key = row["key"]
                reported["function_bodies"][key] = body_diagnostic(row["prosrc"])
                # pg_get_functiondef contains prosrc; compare raw prosrc separately,
                # and retain its normalized definition for SQL-standard bodies.
                row["definition"] = collapse(row["definition"])
                if row["config"] is not None:
                    row["config"] = sorted(row["config"])
                put("functions", row, acl_kind="f", deparse=("arguments", "result"))
                for aggregate in rows(
                    f"SELECT to_jsonb(a) - ARRAY['aggfnoid','aggtransfn','aggfinalfn','aggcombinefn','aggserialfn','aggdeserialfn','aggmtransfn','aggminvtransfn','aggmfinalfn','aggsortop','aggtranstype','aggmtranstype'] AS properties, a.aggtransfn::regproc::text AS transition_function, a.aggfinalfn::regproc::text AS final_function, a.aggcombinefn::regproc::text AS combine_function, a.aggserialfn::regproc::text AS serialize_function, a.aggdeserialfn::regproc::text AS deserialize_function, a.aggmtransfn::regproc::text AS moving_function, a.aggminvtransfn::regproc::text AS inverse_function, a.aggmfinalfn::regproc::text AS moving_final_function, a.aggsortop::regoperator::text AS sort_operator, a.aggtranstype::regtype::text AS transition_type, a.aggmtranstype::regtype::text AS moving_type FROM pg_aggregate a WHERE a.aggfnoid={oid}"
                ):
                    catalog["functions"][key]["aggregate"] = aggregate
            for row in rows(
                f"SELECT {relname} || '.' || quote_ident(t.tgname) AS key, pg_get_triggerdef(t.oid,true) AS definition, t.tgenabled AS enabled FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE t.tgrelid IN ({allowed}) AND NOT t.tgisinternal AND {_member_free('pg_trigger', 't')}"
            ):
                put("triggers", row, deparse=("definition",))
            for row in rows(
                f"SELECT {relname} || '@' || CASE WHEN co.conrelid<>0 THEN co.conrelid::regclass::text ELSE co.contypid::regtype::text END || '.' || quote_ident(co.conname) || ':' || t.tgtype || ':' || t.tgfoid::regprocedure::text AS key, t.tgenabled AS enabled, t.tgdeferrable AS deferrable, t.tginitdeferred AS initially_deferred, t.tgfoid::regprocedure::text AS function, t.tgtype AS kind, encode(t.tgargs,'hex') AS arguments, pg_get_expr(t.tgqual,t.tgrelid) AS condition FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_constraint co ON co.oid=t.tgconstraint WHERE t.tgrelid IN ({allowed}) AND t.tgisinternal AND {_member_free('pg_trigger', 't')}"
            ):
                put("internal_triggers", row, deparse=("condition",))
            for row in rows(
                f"SELECT {relname} || '.' || quote_ident(p.polname) AS key, p.polcmd AS command, p.polpermissive AS permissive, ARRAY(SELECT CASE WHEN role_oid=0 THEN 'PUBLIC' ELSE r.rolname END FROM unnest(p.polroles) role_oid LEFT JOIN pg_roles r ON r.oid=role_oid ORDER BY 1) AS roles, pg_get_expr(p.polqual,p.polrelid) AS using, pg_get_expr(p.polwithcheck,p.polrelid) AS check FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE p.polrelid IN ({allowed})"
            ):
                normalized = sorted(set(normalize_role(r, owner) for r in row["roles"]))
                row["roles"] = [
                    r
                    for r in normalized
                    if r in ("<owner>", "PUBLIC") or r.startswith("svc_")
                ]
                outside = [r for r in normalized if r not in row["roles"]]
                if outside:
                    reported["outside_roles"]["policies." + row["key"]] = outside
                put("policies", row, deparse=("using", "check"))
            for row in rows(
                f"SELECT t.oid, n.nspname || '.' || quote_ident(t.typname) AS key, r.rolname AS owner, t.typowner AS owner_oid, t.typacl::text[] AS acl, t.typtype AS kind, t.typcategory AS category, t.typispreferred AS preferred, t.typisdefined AS defined, t.typdelim AS delimiter, t.typlen AS length, t.typbyval AS by_value, t.typalign AS alignment, t.typstorage AS storage, t.typnotnull AS not_null, t.typndims AS dimensions, t.typtypmod AS modifier, t.typinput::regproc::text AS input, t.typoutput::regproc::text AS output, t.typreceive::regproc::text AS receive, t.typsend::regproc::text AS send, t.typmodin::regproc::text AS modifier_in, t.typmodout::regproc::text AS modifier_out, t.typanalyze::regproc::text AS analyze, t.typsubscript::regproc::text AS subscript, t.typelem::regtype::text AS element, t.typarray::regtype::text AS array_type, t.typbasetype::regtype::text AS base_type, pg_get_expr(t.typdefaultbin,0) AS default, cn.nspname || '.' || quote_ident(co.collname) AS collation, ARRAY(SELECT e.enumlabel FROM pg_enum e WHERE e.enumtypid=t.oid ORDER BY e.enumsortorder) AS enum_labels FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace JOIN pg_roles r ON r.oid=t.typowner LEFT JOIN pg_collation co ON co.oid=t.typcollation LEFT JOIN pg_namespace cn ON cn.oid=co.collnamespace WHERE {_user_schema()} AND {_member_free('pg_type', 't')} AND (t.typrelid=0 OR t.typrelid IN ({allowed})) AND NOT EXISTS (SELECT 1 FROM pg_type elem WHERE elem.oid=t.typelem AND elem.typrelid<>0 AND elem.typrelid NOT IN ({allowed})) AND NOT EXISTS (SELECT 1 FROM pg_type elem JOIN pg_depend d ON d.classid='pg_type'::regclass AND d.objid=elem.oid AND d.deptype='e' WHERE elem.oid=t.typelem)"
            ):
                oid, key = row.pop("oid"), row["key"]
                put("types", row, acl_kind="T", deparse=("default",))
                for range_row in rows(
                    f"SELECT rngsubtype::regtype::text AS subtype, rngmultitypid::regtype::text AS multirange, cn.nspname || '.' || quote_ident(co.collname) AS collation, onsp.nspname || '.' || quote_ident(op.opcname) AS operator_class, rngcanonical::regproc::text AS canonical, rngsubdiff::regproc::text AS subtype_difference FROM pg_range ra LEFT JOIN pg_collation co ON co.oid=ra.rngcollation LEFT JOIN pg_namespace cn ON cn.oid=co.collnamespace JOIN pg_opclass op ON op.oid=ra.rngsubopc JOIN pg_namespace onsp ON onsp.oid=op.opcnamespace WHERE ra.rngtypid={oid}"
                ):
                    catalog["types"][key]["range"] = range_row
            for row in rows(
                f"SELECT {relname} AS key, pg_get_viewdef(c.oid,true) AS definition FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.oid IN ({allowed}) AND c.relkind IN ('v','m')"
            ):
                put("views", row, deparse=("definition",))
            for row in rows(
                "SELECT e.extname AS key, r.rolname AS owner, n.nspname AS schema, e.extrelocatable AS relocatable, e.extversion AS version, ARRAY(SELECT unnest(e.extconfig)::regclass::text) AS configuration_tables, e.extcondition AS configuration_conditions FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace JOIN pg_roles r ON r.oid=e.extowner"
            ):
                put("extensions", row)
            role_rows = rows(
                "SELECT rolname AS key, rolsuper AS superuser, rolinherit AS inherit, rolcreaterole AS create_role, rolcreatedb AS create_database, rolcanlogin AS login, rolreplication AS replication, rolconnlimit AS connection_limit, rolvaliduntil::text AS valid_until, rolbypassrls AS bypass_rls, rolconfig AS config FROM pg_roles"
            )
            for row in role_rows:
                name = row["key"]
                if name == owner or name.startswith("svc_"):
                    row["key"] = normalize_role(name, owner)
                    put("roles", row)
                elif not name.startswith("pg_"):
                    reported["outside_roles"].setdefault("roles", []).append(name)
            for row in rows(
                "SELECT CASE WHEN s.setdatabase=0 THEN '*' ELSE '<database>' END AS database, CASE WHEN s.setrole=0 THEN 'PUBLIC' ELSE r.rolname END AS role, s.setconfig AS config FROM pg_db_role_setting s LEFT JOIN pg_roles r ON r.oid=s.setrole WHERE s.setdatabase=0 OR s.setdatabase=(SELECT oid FROM pg_database WHERE datname=current_database())"
            ):
                row["role"] = normalize_role(row["role"], owner)
                row["config"] = sorted(row["config"])
                row["key"] = row["database"] + "@" + row["role"]
                if row["role"] in ("<owner>", "PUBLIC") or row["role"].startswith(
                    "svc_"
                ):
                    put("settings", row)
                else:
                    reported["outside_roles"].setdefault("settings", {})[
                        row.pop("key")
                    ] = row
            options = ", m.inherit_option, m.set_option" if version >= 160000 else ""
            for row in rows(
                "SELECT r.rolname AS role, member.rolname AS member, grantor.rolname AS grantor, m.admin_option"
                + options
                + " FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.roleid JOIN pg_roles member ON member.oid=m.member JOIN pg_roles grantor ON grantor.oid=m.grantor"
            ):
                row, options_report = normalize_membership(row, owner, version)
                if options_report is not None:
                    reported["membership_options"].append(options_report)
                if row is None:
                    continue
                if row["member"] == "<owner>" or row["member"].startswith("svc_"):
                    reported["membership_grants"].append(dict(row))
                    row.pop("grantor")
                    key = row["role"] + "->" + row["member"]
                    existing = catalog["memberships"].get(key)
                    if existing is not None:
                        # PostgreSQL 16+ can retain grants from several grantors.
                        # Any grant carrying an option supplies it to this edge.
                        for option in MEMBERSHIP_OPTIONS:
                            if option in row:
                                row[option] |= existing[option]
                    catalog["memberships"][key] = row
                elif any(
                    r == "<owner>" or r.startswith("svc_")
                    for r in (row["role"], row["member"])
                ):
                    reported["outside_roles"].setdefault("memberships", []).append(row)
            for row in rows(
                "SELECT '<database>' AS key, r.rolname AS owner, d.datdba AS owner_oid, d.datacl::text[] AS acl, (to_jsonb(d) - ARRAY['oid','datname','datdba','datacl','datfrozenxid','datminmxid','dattablespace','dathasloginevt']) AS properties, t.spcname AS tablespace FROM pg_database d JOIN pg_roles r ON r.oid=d.datdba JOIN pg_tablespace t ON t.oid=d.dattablespace WHERE d.datname=current_database()"
            ):
                row["properties"] = normalize_database_properties(row["properties"])
                put("database", row, acl_kind="d")
            for row in rows(
                f"SELECT r.rolname AS role, coalesce(n.nspname,'*') AS schema, d.defaclobjtype AS kind, d.defaclrole AS owner_oid, d.defaclacl::text[] AS acl FROM pg_default_acl d JOIN pg_roles r ON r.oid=d.defaclrole LEFT JOIN pg_namespace n ON n.oid=d.defaclnamespace WHERE n.oid IS NULL OR ({_user_schema()})"
            ):
                row["role"] = normalize_role(row["role"], owner)
                row["key"] = f"{row['role']}@{row['schema']}:{row['kind']}"
                key = row["key"]
                put("default_acls", row, acl_kind=row["kind"])
                if row["role"] != "<owner>" and not row["role"].startswith("svc_"):
                    reported["outside_roles"].setdefault("default_acls", {})[key] = (
                        catalog["default_acls"].pop(key)
                    )
            for row in rows(
                f"SELECT e.evtname AS key, e.evtevent AS event, r.rolname AS owner, e.evtfoid::regprocedure::text AS function, e.evtenabled AS enabled, e.evttags AS tags FROM pg_event_trigger e JOIN pg_roles r ON r.oid=e.evtowner WHERE {_member_free('pg_event_trigger', 'e')}"
            ):
                if row["tags"] is not None:
                    row["tags"] = sorted(row["tags"])
                put("event_triggers", row)
            for row in rows(
                "SELECT p.pubname AS key, r.rolname AS owner, to_jsonb(p) - ARRAY['oid','pubname','pubowner'] AS properties FROM pg_publication p JOIN pg_roles r ON r.oid=p.pubowner"
            ):
                put("publications", row)
            for row in rows(
                f"SELECT p.pubname || '@' || {relname} AS key, pg_get_expr(pr.prqual,pr.prrelid) AS filter, ARRAY(SELECT a.attname FROM unnest(pr.prattrs) num JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=num ORDER BY a.attnum) AS columns FROM pg_publication_rel pr JOIN pg_publication p ON p.oid=pr.prpubid JOIN pg_class c ON c.oid=pr.prrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.oid IN ({allowed})"
            ):
                put("publication_tables", row, deparse=("filter",))
            for row in rows(
                f"SELECT p.pubname || '@' || n.nspname AS key FROM pg_publication_namespace pn JOIN pg_publication p ON p.oid=pn.pnpubid JOIN pg_namespace n ON n.oid=pn.pnnspid WHERE {_user_schema()}"
            ):
                put("publication_schemas", row)
            for row in rows(
                f"SELECT sn.nspname || '.' || quote_ident(s.stxname) AS key, r.rolname AS owner, pg_get_statisticsobjdef(s.oid) AS definition, s.stxstattarget AS target FROM pg_statistic_ext s JOIN pg_namespace sn ON sn.oid=s.stxnamespace JOIN pg_roles r ON r.oid=s.stxowner WHERE s.stxrelid IN ({allowed}) AND {_member_free('pg_statistic_ext', 's')}"
            ):
                put("statistics", row, deparse=("definition",))
            for row in rows(
                f"SELECT {relname} || '@' || pn.nspname || '.' || quote_ident(p.relname) AS key, i.inhseqno AS position, i.inhdetachpending AS detach_pending FROM pg_inherits i JOIN pg_class c ON c.oid=i.inhrelid JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_class p ON p.oid=i.inhparent JOIN pg_namespace pn ON pn.oid=p.relnamespace WHERE c.oid IN ({allowed})"
            ):
                put("inherits", row)
            for row in rows(
                f"SELECT {relname} AS key, s.srvname AS server, f.ftoptions AS options FROM pg_foreign_table f JOIN pg_class c ON c.oid=f.ftrelid JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_foreign_server s ON s.oid=f.ftserver WHERE c.oid IN ({allowed})"
            ):
                put("foreign_tables", row)
            for row in rows(
                f"SELECT f.fdwname AS key, r.rolname AS owner, f.fdwowner AS owner_oid, f.fdwacl::text[] AS acl, f.fdwhandler::regproc::text AS handler, f.fdwvalidator::regproc::text AS validator, f.fdwoptions AS options FROM pg_foreign_data_wrapper f JOIN pg_roles r ON r.oid=f.fdwowner WHERE {_member_free('pg_foreign_data_wrapper', 'f')}"
            ):
                put("foreign_data_wrappers", row, acl_kind="F")
            for row in rows(
                f"SELECT s.srvname AS key, r.rolname AS owner, s.srvowner AS owner_oid, s.srvacl::text[] AS acl, f.fdwname AS wrapper, s.srvtype AS type, s.srvversion AS version, s.srvoptions AS options FROM pg_foreign_server s JOIN pg_roles r ON r.oid=s.srvowner JOIN pg_foreign_data_wrapper f ON f.oid=s.srvfdw WHERE {_member_free('pg_foreign_server', 's')}"
            ):
                put("foreign_servers", row, acl_kind="s")
            # User mapping options may contain credentials and are never extracted.
            for row in rows(
                "SELECT s.srvname AS server, CASE WHEN m.umuser=0 THEN 'PUBLIC' ELSE r.rolname END AS role FROM pg_user_mappings m JOIN pg_foreign_server s ON s.oid=m.srvid LEFT JOIN pg_roles r ON r.oid=m.umuser"
            ):
                row["role"] = normalize_role(row["role"], owner)
                row["key"] = row["server"] + "@" + row["role"]
                put("user_mappings", row)
            for row in rows(
                f"SELECT {relname} || '.' || quote_ident(r.rulename) AS key, pg_get_ruledef(r.oid,true) AS definition, r.ev_enabled AS enabled FROM pg_rewrite r JOIN pg_class c ON c.oid=r.ev_class JOIN pg_namespace n ON n.oid=c.relnamespace WHERE r.ev_class IN ({allowed}) AND r.rulename <> '_RETURN' AND {_member_free('pg_rewrite', 'r')}"
            ):
                put("rules", row, deparse=("definition",))
            cur.execute(
                "SELECT to_regclass('public.post_intent_transitions') AS relation"
            )
            if cur.fetchone()["relation"]:
                for row in rows(
                    "SELECT to_jsonb(t) AS row FROM public.post_intent_transitions t"
                ):
                    value = row["row"]
                    created_at = value.pop("created_at", None)
                    key = json.dumps(value, sort_keys=True)
                    reported["reference_metadata"][key] = {"created_at": created_at}
                    catalog["reference_rows"][key] = value
            reported["archive_relations"] = [
                r["name"]
                for r in rows(
                    "SELECT n.nspname || '.' || quote_ident(c.relname) AS name FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='archive' ORDER BY 1"
                )
            ]
            # Descriptions are diagnostics, keyed by stable object identities.
            reported["comments"] = rows(
                "SELECT i.identity AS object, d.description FROM pg_description d "
                "CROSS JOIN LATERAL pg_identify_object(d.classoid,d.objoid,d.objsubid) i "
                "WHERE (i.schema !~ '^pg_' AND i.schema <> 'information_schema') "
                "OR (d.classoid='pg_namespace'::regclass AND d.objoid IN "
                "(SELECT n.oid FROM pg_namespace n WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema')) "
                "OR d.classoid IN ('pg_event_trigger'::regclass,'pg_publication'::regclass,'pg_extension'::regclass) ORDER BY 1"
            )
            reported["shared_comments"] = rows(
                "SELECT pg_describe_object(d.classoid,d.objoid,0) AS object, d.description "
                "FROM pg_shdescription d WHERE (d.classoid='pg_database'::regclass AND d.objoid IN "
                "(SELECT oid FROM pg_database WHERE datname=current_database())) "
                "OR (d.classoid='pg_authid'::regclass AND d.objoid IN "
                "(SELECT r.oid FROM pg_roles r WHERE left(r.rolname,4)='svc_' OR r.rolname=current_user)) ORDER BY 1"
            )
        snapshot["counts"] = counts(snapshot)
        return snapshot
    finally:
        conn.rollback()


def schema_signature(
    dsn, *, read_only=True, variable="the dsn passed to schema_signature"
):
    """The catalog snapshot of *dsn*'s database. A *dsn* libpq cannot read is
    refused by *variable*, where the caller got it, and never quoted
    (`scripts/libpq_connect.py`)."""
    conn = libpq_connect.connect(dsn, variable)
    try:
        return extract(conn, read_only=read_only)
    finally:
        conn.close()


def counts(snapshot):
    catalog = snapshot["catalog"]
    relations = catalog["relations"].values()
    return {
        "tables": sum(r["kind"] in ("r", "p") for r in relations),
        "functions": len(catalog["functions"]),
        "policies": len(catalog["policies"]),
        "triggers": len(catalog["triggers"]),
        "rls_tables": sum(
            r["kind"] in ("r", "p") and r["rls_enabled"]
            for r in catalog["relations"].values()
        ),
    }


def validate(snapshot):
    if snapshot.get("format_version") != FORMAT_VERSION:
        raise ValueError("unsupported or missing snapshot format_version")
    if set(snapshot.get("catalog", {})) != set(DIMENSIONS):
        raise ValueError("snapshot must contain every catalog dimension")
    actual = counts(snapshot)
    return [
        f"floor {name}: {actual[name]} < {floor}"
        for name, floor in FLOORS.items()
        if actual[name] < floor
    ]


def schema_diff(first, second):
    """Return every structural difference plus each side's count-floor failures.

    A membership option that one side's server predates (INHERIT and SET
    arrived in PostgreSQL 16) has nothing to compare against there."""
    diffs = []
    for label, snapshot in (("first", first), ("second", second)):
        diffs.extend(f"{label} {failure}" for failure in validate(snapshot))
    for dimension in DIMENSIONS:
        left, right = first["catalog"][dimension], second["catalog"][dimension]
        for key in sorted(left.keys() | right.keys()):
            if key not in left:
                diffs.append(f"{dimension} {key}: only in second schema")
            elif key not in right:
                diffs.append(f"{dimension} {key}: only in first schema")
            elif left[key] != right[key]:
                for field in sorted(left[key].keys() | right[key].keys()):
                    if (
                        dimension == "memberships"
                        and field in ("inherit_option", "set_option")
                        and not (field in left[key] and field in right[key])
                    ):
                        continue
                    a, b = left[key].get(field), right[key].get(field)
                    if a != b:
                        diffs.append(
                            f"{dimension} {key}.{field}: first={a!r} second={b!r}"
                        )
    return diffs


def print_comparison(first, second):
    diffs = schema_diff(first, second)
    for item in diffs:
        print(item)
    for label, snapshot in (("first", first), ("second", second)):
        print(f"{label} counts: {json.dumps(counts(snapshot), sort_keys=True)}")
        report = {
            k: v
            for k, v in snapshot.get("reported", {}).items()
            if k != "function_bodies"
        }
        print(f"{label} reported: {json.dumps(report, sort_keys=True)}")
    left = first.get("reported", {}).get("function_bodies", {})
    right = second.get("reported", {}).get("function_bodies", {})
    functions_a, functions_b = (
        first["catalog"]["functions"],
        second["catalog"]["functions"],
    )
    for key in sorted(functions_a.keys() & functions_b.keys()):
        if functions_a[key].get("prosrc") != functions_b[key].get("prosrc"):
            status = (
                "equal"
                if key in left and key in right and left[key] == right[key]
                else "different"
            )
            print(
                f"diagnostic function {key}: comment-free bodies are {status}; raw body difference gates"
            )
    outside_sides = [
        label
        for label, snapshot in (("first", first), ("second", second))
        if snapshot.get("reported", {}).get("outside_roles")
    ]
    scope_note = (
        f"; out-of-scope roles reported ({', '.join(outside_sides)})"
        if outside_sides
        else ""
    )
    print(f"{len(diffs)} difference(s){scope_note}")
    return 1 if diffs else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    extract_parser = commands.add_parser("extract")
    extract_parser.add_argument("--out", required=True, type=Path)
    extract_parser.add_argument("--read-only", action="store_true")
    extract_parser.add_argument(
        "--database-url", default=os.environ.get("DATABASE_URL")
    )
    diff_parser = commands.add_parser("diff")
    diff_parser.add_argument("first", type=Path)
    diff_parser.add_argument("second", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "extract":
            if not args.database_url:
                parser.error("set DATABASE_URL or pass --database-url")
            snapshot = schema_signature(
                args.database_url,
                read_only=args.read_only,
                variable=DATABASE_URL_NAME,
            )
            args.out.write_text(
                json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            failures = validate(snapshot)
            for failure in failures:
                print(failure, file=sys.stderr)
            return 1 if failures else 0
        return print_comparison(
            json.loads(args.first.read_text(encoding="utf-8")),
            json.loads(args.second.read_text(encoding="utf-8")),
        )
    except (ValueError, OSError, psycopg2.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
