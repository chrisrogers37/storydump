"""Portable catalog snapshots detect structural drift without comparing runtime data."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import psycopg2
import pytest
from psycopg2.extensions import parse_dsn
from sqlalchemy import create_engine

from scripts import schema_consistency, schema_parity as parity
from scripts.migration_runner import adopt


def healthy_snapshot():
    catalog = {dimension: {} for dimension in parity.DIMENSIONS}
    catalog["relations"] = {
        f"public.table_{n}": {"kind": "r", "rls_enabled": True} for n in range(44)
    }
    for dimension, count in (("functions", 85), ("policies", 115), ("triggers", 53)):
        catalog[dimension] = {f"public.object_{n}": {} for n in range(count)}
    return {"format_version": parity.FORMAT_VERSION, "catalog": catalog, "reported": {}}


@pytest.fixture
def snapshot_json(tmp_path):
    file = tmp_path / "catalog.json"
    file.write_text(json.dumps(healthy_snapshot()), encoding="utf-8")
    return file


def test_diff_cli_accepts_equal_fixture_json(snapshot_json, capsys):
    assert parity.main(["diff", str(snapshot_json), str(snapshot_json)]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "0 difference(s)"


@pytest.mark.parametrize("side", ["first", "second"])
def test_diff_cli_labels_out_of_scope_role_reports(tmp_path, side, capsys):
    snapshots = {label: healthy_snapshot() for label in ("first", "second")}
    snapshots[side]["reported"]["outside_roles"] = {"roles": ["support_operator"]}
    files = []
    for label, snapshot in snapshots.items():
        file = tmp_path / f"{label}.json"
        file.write_text(json.dumps(snapshot), encoding="utf-8")
        files.append(str(file))
    assert parity.main(["diff", *files]) == 0
    summary = capsys.readouterr().out.splitlines()[-1]
    assert "0 difference(s)" in summary
    assert "out-of-scope roles reported" in summary
    assert side in summary


@pytest.mark.parametrize("dimension", parity.DIMENSIONS)
def test_every_catalog_dimension_gates(dimension):
    first = healthy_snapshot()
    second = copy.deepcopy(first)
    second["catalog"][dimension]["public.extra"] = (
        {"kind": "r", "rls_enabled": True}
        if dimension == "relations"
        else {"value": True}
    )
    assert any(
        dimension in difference for difference in parity.schema_diff(first, second)
    )


@pytest.mark.parametrize("name", parity.FLOORS)
def test_floor_is_recomputed_and_fails_on_either_side(name):
    first, second = healthy_snapshot(), healthy_snapshot()
    second["counts"] = dict(parity.FLOORS)
    if name in ("tables", "rls_tables"):
        second["catalog"]["relations"]["public.table_0"]["rls_enabled"] = False
        if name == "tables":
            second["catalog"]["relations"].pop("public.table_0")
    else:
        second["catalog"][name].pop("public.object_0")
    assert any(f"second floor {name}" in d for d in parity.schema_diff(first, second))
    assert any(f"first floor {name}" in d for d in parity.schema_diff(second, first))


def test_reports_do_not_gate():
    first, second = healthy_snapshot(), healthy_snapshot()
    second["reported"] = {
        "comments": ["description"],
        "outside_roles": {"role": "review"},
        "membership_options": [False],
        "reference_metadata": ["timestamp"],
        "archive_relations": ["archive.snapshot"],
    }
    assert parity.schema_diff(first, second) == []


def test_owner_and_acl_normalization():
    assert parity.normalize_role("owner_a", "owner_a") == "<owner>"
    assert parity.normalize_role("pg_database_owner", "owner_a") == "<owner>"
    assert parity.normalize_role("svc_worker", "owner_a") == "svc_worker"
    entries = [
        {
            "grantee": "owner_a",
            "privilege_type": "SELECT",
            "is_grantable": True,
            "grantor": "unrelated",
        },
        {
            "grantee": "pg_database_owner",
            "privilege_type": "SELECT",
            "is_grantable": True,
        },
        {"grantee": "PUBLIC", "privilege_type": "EXECUTE", "is_grantable": False},
        {"grantee": "svc_worker", "privilege_type": "MAINTAIN", "is_grantable": False},
        {"grantee": "review_role", "privilege_type": "UPDATE", "is_grantable": False},
    ]
    compared, outside = parity.normalize_acl(entries, "owner_a")
    assert len(compared) == 2
    assert {e["grantee"] for e in compared} == {"<owner>", "PUBLIC"}
    assert outside == [
        {"grantee": "review_role", "privilege": "UPDATE", "grantable": False}
    ]
    assert all("grantor" not in row for row in compared)


@pytest.fixture
def database_properties_by_version():
    common = {"datlocprovider": "i", "datconnlimit": -1, "encoding": 6}
    return {
        150000: {**common, "daticulocale": "en-US"},
        160000: {**common, "daticulocale": "en-US", "daticurules": None},
        170000: {**common, "datlocale": "en-US", "daticurules": None},
    }


@pytest.mark.parametrize("locale", [None, "en-US"])
def test_database_locale_catalog_shapes_compare_equal(
    database_properties_by_version, locale
):
    snapshots = []
    for properties in database_properties_by_version.values():
        locale_key = "daticulocale" if "daticulocale" in properties else "datlocale"
        properties[locale_key] = locale
        original = copy.deepcopy(properties)
        snapshot = healthy_snapshot()
        snapshot["catalog"]["database"]["<database>"] = {
            "properties": parity.normalize_database_properties(properties)
        }
        assert properties == original
        snapshots.append(snapshot)
    assert parity.schema_diff(snapshots[0], snapshots[1]) == []
    assert parity.schema_diff(snapshots[0], snapshots[2]) == []
    assert snapshots[0]["catalog"]["database"]["<database>"]["properties"] == {
        "datlocprovider": "i",
        "datconnlimit": -1,
        "encoding": 6,
        "datlocale": locale,
        "daticurules": None,
    }


@pytest.mark.parametrize(
    ("property_name", "value"),
    [
        ("datlocale", "fr-FR"),
        ("daticurules", "&a < b"),
        ("datlocprovider", "b"),
        ("datconnlimit", 3),
        ("future_property", "preserved"),
    ],
)
def test_database_properties_still_gate(
    database_properties_by_version, property_name, value
):
    first, second = healthy_snapshot(), healthy_snapshot()
    first["catalog"]["database"]["<database>"] = {
        "properties": parity.normalize_database_properties(
            database_properties_by_version[150000]
        )
    }
    changed = {**database_properties_by_version[170000], property_name: value}
    second["catalog"]["database"]["<database>"] = {
        "properties": parity.normalize_database_properties(changed)
    }
    assert parity.schema_diff(first, second) == [
        "database <database>.properties: "
        f"first={first['catalog']['database']['<database>']['properties']!r} "
        f"second={second['catalog']['database']['<database>']['properties']!r}"
    ]


def test_creator_membership_and_options_normalization():
    row = {
        "role": "svc_worker",
        "member": "owner_a",
        "grantor": "owner_a",
        "admin_option": True,
        "inherit_option": False,
        "set_option": False,
    }
    compared, report = parity.normalize_membership(row, "owner_a", 170000)
    assert compared is None
    assert report["member"] == "<owner>"
    explicit = {**row, "admin_option": False}
    first, first_report = parity.normalize_membership(explicit, "owner_a", 170000)
    second, second_report = parity.normalize_membership(
        {**explicit, "inherit_option": True, "set_option": True}, "owner_a", 170000
    )
    assert (first["inherit_option"], first["set_option"]) == (False, False)
    assert (second["inherit_option"], second["set_option"]) == (True, True)
    assert first_report != second_report
    assert (
        parity.normalize_membership(
            {**row, "grantor": "svc_migration"}, "owner_a", 170000
        )[0]
        is not None
    )
    old = {
        k: v for k, v in explicit.items() if k not in ("inherit_option", "set_option")
    }
    assert parity.normalize_membership(old, "owner_a", 150000) == (
        {
            "role": "svc_worker",
            "member": "<owner>",
            "grantor": "<owner>",
            "admin_option": False,
        },
        None,
    )


def test_deparsed_layout_preserves_literal_and_identifier_whitespace():
    assert parity.collapse("  SELECT\n  1  ") == "SELECT 1"
    for literal in ("'a  b'", '"a  b"', "$$a  b$$", "E'a  b'", "U&'a  b'"):
        assert parity.collapse(f" SELECT\n {literal}  ") == f"SELECT {literal}"
    assert parity.collapse("DEFAULT 'a  b'") != parity.collapse("DEFAULT 'a b'")
    assert parity.collapse(None) is None


def test_raw_function_body_gates_when_diagnostic_bodies_match(capsys):
    first, second = healthy_snapshot(), healthy_snapshot()
    first["catalog"]["functions"]["public.object_0"] = {"prosrc": "SELECT 1 -- note\n"}
    second["catalog"]["functions"]["public.object_0"] = {
        "prosrc": "SELECT   1 /* other */"
    }
    for snapshot in (first, second):
        snapshot["reported"]["function_bodies"] = {
            "public.object_0": parity.body_diagnostic(
                snapshot["catalog"]["functions"]["public.object_0"]["prosrc"]
            )
        }
    assert parity.print_comparison(first, second) == 1
    assert (
        "comment-free bodies are equal; raw body difference gates"
        in capsys.readouterr().out
    )
    assert (
        parity.body_diagnostic("SELECT '-- literal', 1 /* note */")
        == "SELECT '-- literal', 1"
    )


class FakeCursor:
    def __init__(self, version):
        self.version = version
        self.statements = []
        self.result = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, statement, parameters=None):
        self.statements.append(statement)
        if statement == "SHOW server_version_num":
            self.result = [{"server_version_num": str(self.version)}]
        elif statement.startswith("SELECT r.rolname FROM pg_database"):
            self.result = [{"rolname": "owner_a"}]
        elif statement.startswith("SELECT '<database>' AS key"):
            locale_key = "datlocale" if self.version >= 170000 else "daticulocale"
            properties = {locale_key: "en-US", "datconnlimit": -1}
            if self.version >= 160000:
                properties["daticurules"] = None
            self.result = [
                {
                    "key": "<database>",
                    "owner": "owner_a",
                    "owner_oid": 10,
                    "acl": [],
                    "properties": properties,
                    "tablespace": "pg_default",
                }
            ]
        elif statement.startswith("SELECT to_regclass"):
            self.result = [{"relation": None}]
        else:
            self.result = []

    def fetchall(self):
        return self.result

    def fetchone(self):
        return self.result[0]


@pytest.mark.parametrize(
    ("role", "member", "grantor", "compared"),
    [
        ("pg_execute_server_program", "svc_worker", "owner_a", True),
        ("pg_read_all_data", "owner_a", "external_grantor", True),
        ("external_privilege", "svc_worker", "external_grantor", True),
        ("svc_clock", "svc_worker", "external_grantor", True),
        ("svc_worker", "external_member", "owner_a", False),
    ],
)
def test_extract_membership_scope_depends_on_member(role, member, grantor, compared):
    class MembershipCursor(FakeCursor):
        def execute(self, statement, parameters=None):
            super().execute(statement, parameters)
            if "FROM pg_auth_members" in statement:
                self.result = [
                    {
                        "role": role,
                        "member": member,
                        "grantor": grantor,
                        "admin_option": False,
                        "inherit_option": True,
                        "set_option": True,
                    }
                ]

    cursor = MembershipCursor(170000)
    conn = SimpleNamespace(
        set_session=lambda **kwargs: None,
        cursor=lambda **kwargs: cursor,
        rollback=lambda: None,
    )
    snapshot = parity.extract(conn)
    normalized_member = parity.normalize_role(member, "owner_a")
    normalized_grantor = parity.normalize_role(grantor, "owner_a")
    key = f"{role}->{normalized_member}"
    expected = {
        "role": role,
        "member": normalized_member,
        "grantor": normalized_grantor,
        "admin_option": False,
        "inherit_option": True,
        "set_option": True,
    }
    if compared:
        assert snapshot["reported"]["membership_grants"] == [expected]
        assert snapshot["catalog"]["memberships"][key] == {
            k: v for k, v in expected.items() if k != "grantor"
        }
        assert "memberships" not in snapshot["reported"]["outside_roles"]
    else:
        assert snapshot["catalog"]["memberships"] == {}
        assert snapshot["reported"]["outside_roles"]["memberships"] == [expected]


def membership_snapshot(grants, version=170000):
    class MembershipCursor(FakeCursor):
        def execute(self, statement, parameters=None):
            super().execute(statement, parameters)
            if "FROM pg_auth_members" in statement:
                self.result = copy.deepcopy(grants)

    cursor = MembershipCursor(version)
    conn = SimpleNamespace(
        set_session=lambda **kwargs: None,
        cursor=lambda **kwargs: cursor,
        rollback=lambda: None,
    )
    extracted = parity.extract(conn)
    snapshot = healthy_snapshot()
    snapshot["catalog"]["memberships"] = extracted["catalog"]["memberships"]
    snapshot["reported"] = extracted["reported"]
    return snapshot


def membership_grant(grantor, admin=False):
    return {
        "role": "svc_clock",
        "member": "svc_worker",
        "grantor": grantor,
        "admin_option": admin,
        "inherit_option": True,
        "set_option": True,
    }


@pytest.mark.parametrize("version", [150000, 170000])
def test_membership_grantor_is_reported_without_gating(version, capsys):
    first = membership_snapshot([membership_grant("provider_owner_a")], version)
    second = membership_snapshot([membership_grant("provider_owner_b")], version)
    assert parity.print_comparison(first, second) == 0
    output = capsys.readouterr().out
    assert "membership_grants" in output
    assert "provider_owner_a" in output and "provider_owner_b" in output
    assert (
        first["reported"]["membership_grants"]
        != second["reported"]["membership_grants"]
    )


@pytest.mark.parametrize("grant_order", [(False, True), (True, False)])
def test_multiple_membership_grantors_preserve_effective_admin_option(grant_order):
    first = membership_snapshot([membership_grant("provider_owner_a")])
    grants = [
        membership_grant(f"provider_owner_{n}", admin)
        for n, admin in enumerate(grant_order)
    ]
    second = membership_snapshot(grants)
    assert second["catalog"]["memberships"] == {
        "svc_clock->svc_worker": {
            "role": "svc_clock",
            "member": "svc_worker",
            "admin_option": True,
            "inherit_option": True,
            "set_option": True,
        }
    }
    assert len(second["reported"]["membership_grants"]) == 2
    assert any(
        "admin_option" in difference for difference in parity.schema_diff(first, second)
    )


@pytest.mark.parametrize("option", ["inherit_option", "set_option"])
def test_membership_inherit_and_set_options_gate(option):
    first = membership_snapshot([membership_grant("provider_owner_a")])
    second = membership_snapshot(
        [{**membership_grant("provider_owner_a"), option: False}]
    )
    assert parity.schema_diff(first, second) == [
        f"memberships svc_clock->svc_worker.{option}: first=True second=False"
    ]


@pytest.mark.parametrize("first_inherits", [True, False])
def test_multiple_membership_grantors_preserve_effective_inherit_and_set(
    first_inherits,
):
    # One grant supplies inheritance and the other SET ROLE, so an option
    # merged from the wrong field loses one of them.
    grants = [
        {
            **membership_grant(f"provider_owner_{n}"),
            "inherit_option": inherits,
            "set_option": not inherits,
        }
        for n, inherits in enumerate((first_inherits, not first_inherits))
    ]
    snapshot = membership_snapshot(grants)
    assert snapshot["catalog"]["memberships"] == {
        "svc_clock->svc_worker": {
            "role": "svc_clock",
            "member": "svc_worker",
            "admin_option": False,
            "inherit_option": True,
            "set_option": True,
        }
    }


def test_memberships_before_postgresql_16_carry_admin_option_only():
    snapshot = membership_snapshot([membership_grant("provider_owner_a")], 150000)
    assert snapshot["catalog"]["memberships"] == {
        "svc_clock->svc_worker": {
            "role": "svc_clock",
            "member": "svc_worker",
            "admin_option": False,
        }
    }


def test_membership_options_one_server_lacks_do_not_gate():
    """A PostgreSQL 15 edge has no INHERIT or SET option to compare, so against
    a 16+ snapshot the edge compares its admin option alone."""
    before_16 = membership_snapshot([membership_grant("provider_owner_a")], 150000)
    since_16 = membership_snapshot([membership_grant("provider_owner_a")], 170000)
    assert parity.schema_diff(before_16, since_16) == []
    administered = membership_snapshot(
        [membership_grant("provider_owner_a", True)], 170000
    )
    assert parity.schema_diff(before_16, administered) == [
        "memberships svc_clock->svc_worker.admin_option: first=False second=True"
    ]


def test_redundant_membership_grantors_do_not_gate():
    first = membership_snapshot([membership_grant("provider_owner_a", True)])
    second = membership_snapshot(
        [
            membership_grant("provider_owner_a", True),
            membership_grant("provider_owner_b", False),
        ]
    )
    assert parity.schema_diff(first, second) == []
    assert len(second["reported"]["membership_grants"]) == 2


@pytest.mark.parametrize("version", [150000, 160000, 170000])
def test_extract_cli_reads_one_readonly_transaction_and_rolls_back(
    version, tmp_path, monkeypatch
):
    cursor = FakeCursor(version)
    settings, events = [], []
    conn = SimpleNamespace(
        set_session=lambda **kwargs: settings.append(kwargs),
        cursor=lambda **kwargs: cursor,
        rollback=lambda: events.append("rollback"),
        close=lambda: events.append("close"),
    )
    monkeypatch.setattr(parity.psycopg2, "connect", lambda dsn: conn)
    output = tmp_path / "empty.json"
    assert (
        parity.main(
            ["extract", "--out", str(output), "--read-only", "--database-url", "fake"]
        )
        == 1
    )
    snapshot = json.loads(output.read_text())
    assert set(snapshot["catalog"]) == set(parity.DIMENSIONS)
    assert snapshot["catalog"]["database"]["<database>"] == {
        "owner": "<owner>",
        "acl": [],
        "properties": {
            "datlocale": "en-US",
            "daticurules": None,
            "datconnlimit": -1,
        },
        "tablespace": "pg_default",
    }
    assert settings == [{"readonly": True, "isolation_level": "REPEATABLE READ"}]
    assert events == ["rollback", "close"]
    assert cursor.statements[0] == "SET LOCAL search_path = pg_catalog"
    memberships = next(q for q in cursor.statements if "FROM pg_auth_members" in q)
    assert ("m.inherit_option" in memberships) is (version >= 160000)


def test_invalid_snapshot_fails_cli(tmp_path, capsys):
    file = tmp_path / "invalid.json"
    file.write_text("{}")
    assert parity.main(["diff", str(file), str(file)]) == 1
    assert "format_version" in capsys.readouterr().err


def test_format_1_snapshot_fails_cli(tmp_path, capsys):
    """Format 1 recorded memberships without INHERIT or SET, so its PostgreSQL
    16+ edges cannot be compared with a current snapshot's."""
    snapshot = healthy_snapshot()
    snapshot["format_version"] = 1
    file = tmp_path / "format-1.json"
    file.write_text(json.dumps(snapshot))
    assert parity.main(["diff", str(file), str(file)]) == 1
    assert "format_version" in capsys.readouterr().err


def test_base_pin_requires_full_sha(tmp_path):
    pin = tmp_path / "pin"
    pin.write_text("main\n")
    with pytest.raises(ValueError, match="full commit SHA"):
        schema_consistency.base_revision(tmp_path, pin)


def test_base_migrations_are_immutable(tmp_path):
    base, current = tmp_path / "base", tmp_path / "current"
    base.mkdir()
    current.mkdir()
    for directory in (base, current):
        (directory / "000_initial.sql").write_text("")
    for directory in (base, current):
        (directory / "001_change.sql").write_text("SELECT 1;")
    (current / "002_change.sql").write_text("SELECT 2;")
    assert schema_consistency.check_migration_history(base, current) == [2]
    (current / "001_change.sql").write_text("SELECT 3;")
    with pytest.raises(ValueError, match="removed or changed"):
        schema_consistency.check_migration_history(base, current)


def build_database(dsn):
    from src.models.target.ddl import build

    engine = create_engine("postgresql+psycopg2://", connect_args=parse_dsn(dsn))
    try:
        with engine.connect() as conn:
            build(conn)
    finally:
        engine.dispose()
    migrations = Path(__file__).resolve().parents[2] / "scripts/migrations"
    adopt(dsn, migrations, migrations / "adoption_manifest.json")


@pytest.fixture
def catalog_db(admin_conn):
    from tests.scripts.conftest import _scratch

    bracket = _scratch(admin_conn, roles=[])
    dsn = next(bracket)
    try:
        build_database(dsn)
        yield dsn
    finally:
        bracket.close()


def execute(dsn, statement):
    with psycopg2.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(statement)


@pytest.mark.integration
def test_two_real_builds_match_including_reference_rows(catalog_db, second_scratch_db):
    build_database(second_scratch_db)
    first, second = (
        parity.schema_signature(catalog_db),
        parity.schema_signature(second_scratch_db),
    )
    assert parity.schema_diff(first, second) == []
    assert parity.counts(first) == parity.FLOORS
    assert first["catalog"]["reference_rows"]
    assert all(
        "created_at" not in row for row in first["catalog"]["reference_rows"].values()
    )
    assert all(
        "created_at" in row for row in first["reported"]["reference_metadata"].values()
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    ("statement", "dimension"),
    [
        ("ALTER TABLE public.workspaces ADD COLUMN parity_probe text", "columns"),
        (
            "CREATE FUNCTION public.parity_probe() RETURNS integer LANGUAGE sql AS 'SELECT 1'",
            "functions",
        ),
        (
            "DO $$DECLARE t text; p text; BEGIN SELECT tablename, policyname INTO t,p FROM pg_policies WHERE schemaname='public' LIMIT 1; EXECUTE format('DROP POLICY %I ON public.%I',p,t); END$$",
            "policies",
        ),
        ("REVOKE SELECT ON public.workspaces FROM svc_worker", "relations"),
    ],
)
def test_catalog_mutations_are_reported(catalog_db, statement, dimension):
    before = parity.schema_signature(catalog_db)
    execute(catalog_db, statement)
    after = parity.schema_signature(catalog_db)
    assert any(d.startswith(dimension + " ") for d in parity.schema_diff(before, after))


@pytest.mark.integration
def test_changed_raw_body_and_reference_transition_gate(catalog_db):
    execute(
        catalog_db,
        "CREATE FUNCTION public.parity_probe() RETURNS integer LANGUAGE sql AS 'SELECT 1'",
    )
    before = parity.schema_signature(catalog_db)
    execute(
        catalog_db,
        "CREATE OR REPLACE FUNCTION public.parity_probe() RETURNS integer LANGUAGE sql AS 'SELECT 2'; DELETE FROM public.post_intent_transitions WHERE (from_state,to_state)=(SELECT from_state,to_state FROM public.post_intent_transitions LIMIT 1)",
    )
    differences = parity.schema_diff(before, parity.schema_signature(catalog_db))
    assert any(
        d.startswith("functions public.parity_probe().prosrc:") for d in differences
    )
    assert any(d.startswith("reference_rows ") for d in differences)


@pytest.mark.integration
def test_dense_column_order_and_omitted_objects(catalog_db):
    execute(
        catalog_db,
        "CREATE TABLE public.parity_dense(a int, gap int, c int); ALTER TABLE public.parity_dense DROP COLUMN gap",
    )
    before = parity.schema_signature(catalog_db)
    assert before["catalog"]["columns"]["public.parity_dense.c"]["position"] == 2
    execute(
        catalog_db,
        "ALTER TABLE public.workspaces ADD COLUMN parity_probe text; ALTER TABLE public.workspaces DROP COLUMN parity_probe; CREATE TABLE archive.parity_snapshot(id int); CREATE TABLE runner.parity_ledger(id int); COMMENT ON TABLE public.workspaces IS 'catalog diagnostic'",
    )
    after = parity.schema_signature(catalog_db)
    assert parity.schema_diff(before, after) == []
    assert "archive.parity_snapshot" in after["reported"]["archive_relations"]
    assert any(
        c["description"] == "catalog diagnostic" for c in after["reported"]["comments"]
    )
    assert all("uuid_generate_v4" not in key for key in after["catalog"]["functions"])


def test_runner_parity_uses_complete_readonly_signatures(monkeypatch, capsys):
    from scripts import migration_runner

    calls = []

    def signature(dsn, *, read_only):
        calls.append((dsn, read_only))
        return healthy_snapshot()

    monkeypatch.setattr(parity, "schema_signature", signature)
    assert (
        migration_runner.main(
            ["--database-url", "live", "parity", "--against", "built"]
        )
        == 0
    )
    assert calls == [("live", True), ("built", True)]
    assert "0 difference(s)" in capsys.readouterr().out


@pytest.mark.integration
def test_internal_constraint_trigger_state_gates(catalog_db):
    before = parity.schema_signature(catalog_db)
    execute(
        catalog_db,
        "DO $$DECLARE t text; p text; BEGIN SELECT c.relname,tr.tgname INTO t,p FROM pg_trigger tr JOIN pg_class c ON c.oid=tr.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE tr.tgisinternal AND n.nspname='public' LIMIT 1; EXECUTE format('ALTER TABLE public.%I DISABLE TRIGGER %I',t,p); END$$",
    )
    assert any(
        d.startswith("internal_triggers ")
        for d in parity.schema_diff(before, parity.schema_signature(catalog_db))
    )


@pytest.mark.integration
def test_database_owner_extracts_every_dimension_without_superuser(
    admin_conn, owner_actor
):
    from tests.scripts.conftest import _scratch, as_user

    bracket = _scratch(admin_conn, owner=owner_actor, roles=[])
    administrator_dsn = next(bracket)
    owner_dsn = as_user(administrator_dsn, owner_actor)
    try:
        build_database(owner_dsn)
        from psycopg2 import sql

        execute(
            administrator_dsn,
            sql.SQL(
                "CREATE FOREIGN DATA WRAPPER parity_wrapper NO HANDLER; GRANT USAGE ON FOREIGN DATA WRAPPER parity_wrapper TO {}"
            ).format(sql.Identifier(owner_actor)),
        )
        execute(
            owner_dsn,
            "CREATE SERVER parity_server FOREIGN DATA WRAPPER parity_wrapper; CREATE USER MAPPING FOR CURRENT_USER SERVER parity_server OPTIONS (password 'example-secret')",
        )
        expected = parity.schema_signature(administrator_dsn)
        actual = parity.schema_signature(owner_dsn)
        assert actual["catalog"]["roles"]["<owner>"]["superuser"] is False
        assert parity.counts(actual) == parity.FLOORS
        assert parity.schema_diff(expected, actual) == []
        assert actual["catalog"]["user_mappings"] == {
            "parity_server@<owner>": {"server": "parity_server", "role": "<owner>"}
        }
        assert "example-secret" not in json.dumps(actual)
    finally:
        bracket.close()


def test_consistency_requires_independent_clean_clusters():
    class Admin:
        def __init__(self, identifier, roles=()):
            self.identifier, self.roles = identifier, roles
            self.statements = []

        def cursor(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, query):
            self.statements.append(query)

        def fetchone(self):
            return (self.identifier,)

        def fetchall(self):
            return [(name,) for name in self.roles]

    with pytest.raises(ValueError, match="independent PostgreSQL clusters"):
        schema_consistency.require_isolated_clusters([Admin(1), Admin(1)])
    with pytest.raises(ValueError, match="without svc_\\* roles"):
        schema_consistency.require_isolated_clusters(
            [Admin(1), Admin(2, ["svc_worker"])]
        )
    clean = [Admin(1), Admin(2)]
    schema_consistency.require_isolated_clusters(clean)
    assert all(all(q.startswith("SELECT ") for q in a.statements) for a in clean)


@pytest.mark.integration
def test_role_properties_and_membership_edges_gate(catalog_db):
    before = parity.schema_signature(catalog_db)
    execute(
        catalog_db,
        "ALTER ROLE svc_worker CONNECTION LIMIT 1; GRANT svc_clock TO svc_worker",
    )
    differences = parity.schema_diff(before, parity.schema_signature(catalog_db))
    assert any(d.startswith("roles svc_worker.connection_limit:") for d in differences)
    assert any(d.startswith("memberships svc_clock->svc_worker") for d in differences)


@pytest.mark.integration
@pytest.mark.parametrize("member", ["svc_worker", "CURRENT_USER"])
@pytest.mark.parametrize("role", ["pg_execute_server_program", "neon_superuser"])
def test_provider_and_predefined_memberships_held_by_scoped_roles_gate(
    catalog_db, member, role
):
    if role == "neon_superuser":
        execute(catalog_db, "CREATE ROLE neon_superuser")
    before = parity.schema_signature(catalog_db)
    execute(catalog_db, f"GRANT {role} TO {member}")
    try:
        after = parity.schema_signature(catalog_db)
        normalized_member = "<owner>" if member == "CURRENT_USER" else member
        expected = f"memberships {role}->{normalized_member}:"
        assert any(d.startswith(expected) for d in parity.schema_diff(before, after))
    finally:
        execute(catalog_db, f"REVOKE {role} FROM {member}")
        if role == "neon_superuser":
            execute(catalog_db, "DROP ROLE neon_superuser")


@pytest.mark.integration
def test_membership_grantor_change_is_reported_on_real_postgres(catalog_db):
    execute(
        catalog_db,
        "CREATE ROLE parity_grantor_a; CREATE ROLE parity_grantor_b; GRANT svc_clock TO parity_grantor_a, parity_grantor_b WITH ADMIN OPTION",
    )
    try:
        execute(
            catalog_db,
            "SET ROLE parity_grantor_a; GRANT svc_clock TO svc_worker; RESET ROLE",
        )
        first = parity.schema_signature(catalog_db)
        execute(
            catalog_db,
            "SET ROLE parity_grantor_a; REVOKE svc_clock FROM svc_worker; RESET ROLE; SET ROLE parity_grantor_b; GRANT svc_clock TO svc_worker; RESET ROLE",
        )
        second = parity.schema_signature(catalog_db)
        assert parity.schema_diff(first, second) == []
        assert (
            first["reported"]["membership_grants"]
            != second["reported"]["membership_grants"]
        )
        execute(
            catalog_db,
            "SET ROLE parity_grantor_a; GRANT svc_clock TO svc_worker WITH ADMIN OPTION; RESET ROLE",
        )
        administered = parity.schema_signature(catalog_db)
        assert administered["catalog"]["memberships"]["svc_clock->svc_worker"][
            "admin_option"
        ]
        assert any(
            "svc_clock->svc_worker.admin_option" in difference
            for difference in parity.schema_diff(second, administered)
        )
    finally:
        execute(
            catalog_db,
            "REVOKE svc_clock FROM parity_grantor_a, parity_grantor_b CASCADE; DROP ROLE parity_grantor_a; DROP ROLE parity_grantor_b",
        )


@pytest.mark.integration
def test_membership_inherit_and_set_options_gate_on_real_postgres(catalog_db):
    with psycopg2.connect(catalog_db) as conn, conn.cursor() as cur:
        cur.execute("SHOW server_version_num")
        version = int(cur.fetchone()[0])
    if version < 160000:
        execute(catalog_db, "GRANT svc_clock TO svc_worker")
        try:
            # No per-grant options before 16: the edge carries admin only.
            granted = parity.schema_signature(catalog_db)
            assert granted["catalog"]["memberships"]["svc_clock->svc_worker"] == {
                "role": "svc_clock",
                "member": "svc_worker",
                "admin_option": False,
            }
        finally:
            execute(catalog_db, "REVOKE svc_clock FROM svc_worker")
        return
    execute(
        catalog_db,
        "CREATE ROLE parity_grantor_a; CREATE ROLE parity_grantor_b; GRANT svc_clock TO parity_grantor_a, parity_grantor_b WITH ADMIN OPTION",
    )
    try:
        execute(
            catalog_db,
            "SET ROLE parity_grantor_a; GRANT svc_clock TO svc_worker WITH INHERIT TRUE, SET TRUE; RESET ROLE",
        )
        granted = parity.schema_signature(catalog_db)
        execute(
            catalog_db,
            "SET ROLE parity_grantor_a; GRANT svc_clock TO svc_worker WITH INHERIT FALSE, SET FALSE; RESET ROLE",
        )
        restricted = parity.schema_signature(catalog_db)
        assert parity.schema_diff(granted, restricted) == [
            "memberships svc_clock->svc_worker.inherit_option: first=True second=False",
            "memberships svc_clock->svc_worker.set_option: first=True second=False",
        ]
        # Each grantor supplies one option; PostgreSQL holds the edge to both.
        execute(
            catalog_db,
            "SET ROLE parity_grantor_a; GRANT svc_clock TO svc_worker WITH INHERIT TRUE, SET FALSE; RESET ROLE; SET ROLE parity_grantor_b; GRANT svc_clock TO svc_worker WITH INHERIT FALSE, SET TRUE; RESET ROLE",
        )
        with psycopg2.connect(catalog_db) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT pg_has_role('svc_worker', 'svc_clock', 'USAGE'), pg_has_role('svc_worker', 'svc_clock', 'SET')"
            )
            assert cur.fetchone() == (True, True)
        combined = parity.schema_signature(catalog_db)
        assert combined["catalog"]["memberships"]["svc_clock->svc_worker"] == {
            "role": "svc_clock",
            "member": "svc_worker",
            "admin_option": False,
            "inherit_option": True,
            "set_option": True,
        }
        assert parity.schema_diff(granted, combined) == []
    finally:
        execute(
            catalog_db,
            "REVOKE svc_clock FROM parity_grantor_a, parity_grantor_b CASCADE; DROP ROLE parity_grantor_a; DROP ROLE parity_grantor_b",
        )


@pytest.mark.integration
def test_database_role_settings_gate_with_portable_names(catalog_db):
    from psycopg2 import sql

    before = parity.schema_signature(catalog_db)
    database = parse_dsn(catalog_db)["dbname"]
    execute(
        catalog_db,
        sql.SQL(
            "ALTER ROLE svc_worker IN DATABASE {} SET search_path TO public; ALTER DATABASE {} SET row_security=on"
        ).format(sql.Identifier(database), sql.Identifier(database)),
    )
    after = parity.schema_signature(catalog_db)
    assert after["catalog"]["settings"]["<database>@svc_worker"]["config"] == [
        "search_path=public"
    ]
    assert after["catalog"]["settings"]["<database>@PUBLIC"]["config"] == [
        "row_security=on"
    ]
    assert any(d.startswith("settings ") for d in parity.schema_diff(before, after))
    assert database not in json.dumps(after["catalog"]["settings"])


@pytest.mark.integration
def test_duplicate_foreign_key_names_keep_each_internal_trigger(catalog_db):
    execute(
        catalog_db,
        "CREATE TABLE public.parity_parent(id int PRIMARY KEY); CREATE TABLE public.parity_child_a(parent int CONSTRAINT shared_parent REFERENCES public.parity_parent(id)); CREATE TABLE public.parity_child_b(parent int CONSTRAINT shared_parent REFERENCES public.parity_parent(id))",
    )
    before = parity.schema_signature(catalog_db)
    triggers = {
        key: value
        for key, value in before["catalog"]["internal_triggers"].items()
        if key.startswith("public.parity_parent@")
    }
    assert len(triggers) == 4
    execute(
        catalog_db,
        "DO $$DECLARE name text; BEGIN SELECT t.tgname INTO name FROM pg_trigger t JOIN pg_constraint c ON c.oid=t.tgconstraint WHERE t.tgrelid='public.parity_parent'::regclass AND c.conrelid='public.parity_child_a'::regclass LIMIT 1; EXECUTE format('ALTER TABLE public.parity_parent DISABLE TRIGGER %I',name); END$$",
    )
    after = parity.schema_signature(catalog_db)
    assert (
        len(
            [
                d
                for d in parity.schema_diff(before, after)
                if d.startswith(
                    "internal_triggers public.parity_parent@public.parity_child_a."
                )
            ]
        )
        == 1
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    ("namespace", "definition"),
    [
        ("archive", "CREATE DOMAIN archive.parity_example AS text"),
        ("runner", "CREATE TYPE runner.parity_example AS ENUM ('example')"),
    ],
)
def test_standalone_types_in_relation_excluded_schemas_still_gate(
    catalog_db, namespace, definition
):
    before = parity.schema_signature(catalog_db)
    execute(catalog_db, definition)
    assert any(
        d.startswith(f"types {namespace}.parity_example:")
        for d in parity.schema_diff(before, parity.schema_signature(catalog_db))
    )
