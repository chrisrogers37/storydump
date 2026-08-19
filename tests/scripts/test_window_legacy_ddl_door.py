"""#787 — the window legacy-DDL definer door, and the three ruling conditions.

**What the ruling settled.** `svc_migration` cannot `ALTER` the owner-owned
legacy tables and no `GRANT` can produce that right — `ALTER` requires table
ownership or membership in the owning role and is not grantable. Chris ratified
option **(d)**: a `SECURITY DEFINER` function owned by the database-owner actor,
so the elevation is scoped to STATEMENTS rather than to a ROLE. The artifact is
`scripts/window/step0_legacy_ddl_door.sql`; this module is its gate.

**Why it is here and not in F.4.** `04` §F.4 specifies definer-door confinement
("a door touches only the tables its body names") as part of its harness, and
that is the right permanent home. It cannot be that home today:
`test_rls_harness.py`'s own docstring names door confinement as *deliberately
not covered* because F.2's tables do not exist yet, so there is nothing for it
to attach to. The window door exists NOW, at step 0, against the LEGACY tables —
a different subject from F.4's runtime doors on the target schema. So this lands
ALONGSIDE F.4 rather than inside it, and F.4 absorbs the shape when its own half
lands. Adding a legacy-schema case to a harness whose fixtures are target-schema
would have coupled the window's gate to F.2's schedule.

**Every expectation here was measured on the real server before it was
encoded** (PostgreSQL 15, the pinned CI major), and each negative carries a
positive control — a check that has never been seen to fail is not evidence.

The three conditions, each with the demonstration that makes it load-bearing:

1. ``REVOKE EXECUTE … FROM PUBLIC`` is the ENTIRE access control. A new definer
   function has ``proacl IS NULL``, which is EXECUTE to PUBLIC. Tested directly
   rather than assumed: a role holding no grant of any kind is refused on the
   shipped door, and the SAME role performs owner DDL through an otherwise
   identical door that skipped the revoke.
2. Static body only. The bound is the fixed statement list and nothing else, so
   the census asserts every door in ``window_ddl`` is parameterless AND free of
   dynamic SQL — with a positive control proving the detector fires.
3. The door survives an aborted window. That is a stand-down obligation rather
   than a code property; what is asserted here is the residue's SHAPE — one
   schema, enumerable, removed by one statement.
"""

import re

import psycopg2
import pytest

from tests.scripts.conftest import (
    TEST_ACTOR_PASSWORD,
    _scratch,
    as_user,
    execute,
    fetch_one,
    run_bootstrap,
    set_test_passwords,
)

pytestmark = [pytest.mark.integration]


def _has_dynamic_sql(src: str) -> bool:
    """plpgsql's dynamic route is ``EXECUTE``; ``format()`` is how the string it
    runs gets built. Matched on the BODY only (``prosrc``), so the ``EXECUTE`` in
    a GRANT — a privilege name, not a statement — cannot false-positive.
    """
    return bool(re.search(r"\bEXECUTE\b", src, re.IGNORECASE))


NOPRIV = "probe_nopriv_787"  # test-local role, dropped by the fixture bracket

# The minimal legacy shape the door names: the 004/008-era orphaned unique on
# api_tokens, and chat_settings.caption_style in its VARCHAR(20) divergence.
# Built by hand as the owner actor, the way production history was built.
LEGACY_SHAPE = """
CREATE TABLE public.api_tokens (
  id            integer PRIMARY KEY,
  service_name  text,
  token_type    text,
  CONSTRAINT api_tokens_service_name_token_type_key UNIQUE (service_name, token_type)
);
CREATE TABLE public.chat_settings (
  id            integer PRIMARY KEY,
  caption_style VARCHAR(20)
);
CREATE TABLE public.untouched_by_the_door (id integer PRIMARY KEY);
"""

DOOR = "window_ddl.fn_050_chain_reconciliation()"


@pytest.fixture()
def door_db(admin_conn, owner_actor):
    """An owner-owned database carrying the legacy shape, the real step-0
    bootstrap, and the real door artifact — in the order the runbook prints.

    The role bracket carries the no-privilege probe role so teardown drops it in
    `drop_service_roles`'s documented order (database first, roles second).
    """
    extra = [NOPRIV]
    gen = _scratch(admin_conn, owner=owner_actor, roles=extra)
    dsn = next(gen)
    try:
        as_owner = as_user(dsn, owner_actor)
        execute(as_owner, LEGACY_SHAPE)
        # The real step-0 stand-up, both files, through the production door:
        # `run_bootstrap` applies the bootstrap AND its companion. Applying the
        # door by hand here would test a path the window does not use.
        run_bootstrap(admin_conn, as_owner)
        set_test_passwords(admin_conn)
        with admin_conn.cursor() as cur:
            # The suite's actor password, so this role reaches the database
            # through `as_user` like every other one.
            cur.execute(
                f'CREATE ROLE "{NOPRIV}" LOGIN PASSWORD %s', (TEST_ACTOR_PASSWORD,)
            )
        yield dsn, as_owner
    finally:
        gen.close()


def _doors(dsn: str):
    """Every SECURITY DEFINER function in `window_ddl`, with the columns the
    static-body census reads. Multi-row, so it does not go through `fetch_one`
    — and it is a CENSUS by construction rather than a lookup: the invariant has
    to hold for the door added next, not only for the one shipped today."""
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT p.proname, p.pronargs, p.prosrc FROM pg_proc p"
            " JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE n.nspname = 'window_ddl' AND p.prosecdef"
        )
        rows = cur.fetchall()
    conn.close()
    return rows


class TestTheDoorDoesItsJob:
    def test_the_window_actor_performs_owner_ddl_through_the_door(self, door_db):
        """The mechanism, end to end: svc_migration holds NO membership and no
        ownership, and both of 050's ALTERs land."""
        dsn, _as_owner = door_db
        as_svc = as_user(dsn, "svc_migration")

        execute(as_svc, f"SELECT {DOOR}")

        assert fetch_one(
            dsn,
            "SELECT NOT EXISTS (SELECT 1 FROM pg_constraint"
            " WHERE conname = 'api_tokens_service_name_token_type_key')",
        )[0], "050's first postcondition — the orphaned unique is gone"
        assert (
            fetch_one(
                dsn,
                "SELECT data_type FROM information_schema.columns"
                " WHERE table_schema = 'public' AND table_name = 'chat_settings'"
                " AND column_name = 'caption_style'",
            )[0]
            == "text"
        ), "050's second postcondition — caption_style is TEXT"

    def test_the_window_actor_still_cannot_alter_the_tables_directly(self, door_db):
        """The door is the ONLY path. If the direct statement worked, the door
        would be proving nothing about privilege."""
        dsn, _ = door_db
        as_svc = as_user(dsn, "svc_migration")

        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            execute(
                as_svc,
                "ALTER TABLE public.chat_settings ALTER COLUMN caption_style TYPE TEXT",
            )

    def test_the_door_is_confined_to_the_tables_its_body_names(self, door_db):
        """`04` F.4's confinement property, on the window door: the same role,
        immediately after a successful call, on a table the body does not
        name."""
        dsn, _ = door_db
        as_svc = as_user(dsn, "svc_migration")
        execute(as_svc, f"SELECT {DOOR}")

        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            execute(
                as_svc,
                "ALTER TABLE public.untouched_by_the_door ADD COLUMN hijacked integer",
            )

    def test_the_door_is_owned_by_the_owner_actor_and_is_security_definer(
        self, door_db, owner_actor
    ):
        """SECURITY DEFINER runs the body as the function's OWNER — applying the
        artifact as any other role produces a door that confers nothing."""
        dsn, _ = door_db
        row = fetch_one(
            dsn,
            "SELECT r.rolname, p.prosecdef FROM pg_proc p"
            " JOIN pg_namespace n ON n.oid = p.pronamespace"
            " JOIN pg_roles r ON r.oid = p.proowner"
            " WHERE n.nspname = 'window_ddl' AND p.proname ="
            " 'fn_050_chain_reconciliation'",
        )
        assert row == (owner_actor, True)


class TestConditionOneTheRevokeIsTheEntireAccessControl:
    """Ruling condition 1. Tested directly rather than assumed."""

    def test_a_role_with_no_grant_of_any_kind_is_refused(self, door_db):
        dsn, _ = door_db
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            execute(as_user(dsn, NOPRIV), f"SELECT {DOOR}")

    def test_the_shipped_door_carries_a_non_null_acl(self, door_db):
        """`proacl IS NULL` IS the defect — it means EXECUTE to PUBLIC. The
        catalog must show an explicit ACL, and PUBLIC must not be in it."""
        dsn, _ = door_db
        acl = fetch_one(
            dsn,
            "SELECT coalesce(array_to_string(p.proacl, ','), '<NULL>')"
            " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE n.nspname = 'window_ddl'"
            "   AND p.proname = 'fn_050_chain_reconciliation'",
        )[0]
        assert acl != "<NULL>", (
            "proacl IS NULL means EXECUTE to PUBLIC — the revoke did not run"
        )
        # PUBLIC is the aclitem with an EMPTY grantee, so it is exactly the
        # entry that starts with "=" — not a substring search, which would also
        # match every named grantee's own "=X/" separator.
        public_items = [item for item in acl.split(",") if item.startswith("=")]
        assert public_items == [], f"PUBLIC holds EXECUTE on the window door: {acl}"
        assert "svc_migration=X/" in acl, f"the window actor's grant is missing: {acl}"

    def test_positive_control_an_unrevoked_door_hands_owner_ddl_to_anyone(
        self, door_db
    ):
        """THE demonstration that makes the revoke load-bearing rather than
        hygiene. Identical door, revoke omitted, same no-privilege role — and it
        performs owner DDL on a table it has no rights to."""
        dsn, as_owner = door_db
        execute(
            as_owner,
            "CREATE FUNCTION window_ddl.fn_probe_unrevoked() RETURNS void"
            " LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog"
            " AS $fn$ BEGIN"
            "   ALTER TABLE public.untouched_by_the_door ADD COLUMN hijacked integer;"
            " END $fn$",
        )
        execute(as_owner, f'GRANT USAGE ON SCHEMA window_ddl TO "{NOPRIV}"')

        execute(as_user(dsn, NOPRIV), "SELECT window_ddl.fn_probe_unrevoked()")

        assert fetch_one(
            dsn,
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns"
            " WHERE table_name = 'untouched_by_the_door'"
            "   AND column_name = 'hijacked')",
        )[0], (
            "the unrevoked door did NOT confer owner DDL — this control is"
            " supposed to demonstrate that it does, so the refusal in the test"
            " above may be passing for the wrong reason"
        )


class TestConditionTwoStaticBodyOnly:
    """Ruling condition 2, gated rather than intended.

    The bound rests on ONE property — the body is a fixed statement list. Two
    independent invariants hold it, because either alone leaves a route open: a
    parameterless door has nothing to inject INTO, and a door free of dynamic
    SQL cannot execute a string it built itself.
    """

    def test_every_window_door_is_parameterless_and_free_of_dynamic_sql(self, door_db):
        """A CENSUS, not a spot-check: the invariant has to hold for the door
        added next, not only for the one shipped today."""
        dsn, _ = door_db
        doors = _doors(dsn)

        assert doors, "no window door found — the artifact did not apply"
        for name, nargs, src in doors:
            assert nargs == 0, (
                f"{name} takes {nargs} argument(s): a parameterised window door"
                " is the widening path — the caller chooses what runs as the"
                " database owner"
            )
            assert not _has_dynamic_sql(src), (
                f"{name} contains dynamic SQL. One EXECUTE turns a bounded door"
                " into a general-purpose DDL executor running as the owner —"
                " see the positive control below"
            )

    def test_positive_control_the_detector_fires_on_a_dynamic_door(self, door_db):
        """A detector that has never fired proves nothing. Install the exact
        widening edit and assert the census catches it — then assert it really
        was dangerous, by driving the hijack the ruling demonstrated."""
        dsn, as_owner = door_db
        execute(
            as_owner,
            "CREATE FUNCTION window_ddl.fn_probe_dynamic(p_sql text) RETURNS void"
            " LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog"
            " AS $fn$ BEGIN EXECUTE p_sql; END $fn$",
        )

        offenders = [
            name
            for name, nargs, src in _doors(dsn)
            if nargs != 0 or _has_dynamic_sql(src)
        ]
        assert offenders == ["fn_probe_dynamic"], (
            f"the census did not flag the dynamic door: {offenders}"
        )

        # And the flag is not pedantry: the same edit lets a role that holds no
        # rights alter a table the author never named.
        execute(as_owner, f'GRANT USAGE ON SCHEMA window_ddl TO "{NOPRIV}"')
        execute(
            as_owner,
            f"GRANT EXECUTE ON FUNCTION window_ddl.fn_probe_dynamic(text)"
            f' TO "{NOPRIV}"',
        )
        execute(
            as_user(dsn, NOPRIV),
            "SELECT window_ddl.fn_probe_dynamic('ALTER TABLE"
            " public.untouched_by_the_door ADD COLUMN hijacked integer')",
        )
        assert fetch_one(
            dsn,
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns"
            " WHERE table_name = 'untouched_by_the_door'"
            "   AND column_name = 'hijacked')",
        )[0], "the dynamic door did not widen — the control proves nothing"


class TestConditionThreeTheResidueShape:
    """Ruling condition 3, conceded rather than solved: the door survives an
    aborted window. The implementation choice recorded in `04` is to DROP it in
    both stand-down variants and to accept that an abort reaching neither leaves
    it standing. What a test can hold is the residue's SHAPE — which is what
    makes that choice affordable.
    """

    def test_the_residue_is_one_schema_and_one_statement_removes_it(self, door_db):
        dsn, as_owner = door_db

        before = fetch_one(
            dsn,
            "SELECT count(*) FROM pg_proc p JOIN pg_namespace n"
            " ON n.oid = p.pronamespace WHERE p.prosecdef AND n.nspname = 'window_ddl'",
        )[0]
        assert before >= 1

        execute(as_owner, "DROP SCHEMA window_ddl CASCADE")

        assert (
            fetch_one(
                dsn,
                "SELECT count(*) FROM pg_namespace WHERE nspname = 'window_ddl'",
            )[0]
            == 0
        )
        assert (
            fetch_one(
                dsn,
                "SELECT count(*) FROM pg_proc p JOIN pg_namespace n"
                " ON n.oid = p.pronamespace WHERE p.prosecdef"
                "   AND n.nspname NOT IN ('pg_catalog', 'information_schema')",
            )[0]
            == 0
        ), "a definer door survived the schema drop — the residue is not bounded"

    def test_the_door_lives_outside_public_so_the_runtime_census_stays_clean(
        self, door_db
    ):
        """`public`'s definer-door census (test_rls_runtime_harness) is a drift
        detector. A window door landing in `public` would have to be excepted
        there, and an excepted drift detector detects less."""
        dsn, _ = door_db
        assert (
            fetch_one(
                dsn,
                "SELECT count(*) FROM pg_proc p JOIN pg_namespace n"
                " ON n.oid = p.pronamespace"
                " WHERE p.prosecdef AND n.nspname = 'public'",
            )[0]
            == 0
        )
