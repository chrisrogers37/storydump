"""133 (`07` §73, #1740) as an upgrade: the file applied by the runner to a
database at 129 that already holds admissions and the accounts at them.

Every other gate meets 133 inside the replayed advertised stream, on an empty
database. Here the stream is replayed only through §72 (129) as the declared
migration actor, `svc_migration` after the owner's step-0 bootstrap, and what a
database at 129 can hold is seeded. Then the runner applies the real
`133_signup_admission_consumption.sql`, byte for byte, as that same actor: one
transaction, with the runner's lock bound, the file's postconditions and its
ledger row.

- The backfill stamps every unconsumed admission an account holds, a disabled
  account's included, and touches nothing else.
- A failure after the backfill rolls the whole file back, ledger row included,
  and the real file then applies.
- A fault in the trigger's write fails the account's creation, rather than
  letting the account through with its admission unused.
"""

from __future__ import annotations

import asyncio
import hashlib

import psycopg2
import pytest
from sqlalchemy.exc import DBAPIError

from scripts.advertised_ddl import (
    DEFAULT_DOCS,
    DEFAULT_MANIFEST,
    build_stream,
    load_manifest,
    normalize_statements,
)
from scripts.migration_runner import (
    MIGRATIONS_DIR,
    MigrationRunnerError,
    apply_pending,
)
from src.services.target import identity, sessions
from tests.scripts.conftest import (
    as_user,
    execute,
    fetch_all,
    fetch_one,
    in_user_plane,
    window_actor,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

MIGRATION = MIGRATIONS_DIR / "133_signup_admission_consumption.sql"

#: When the admissions seeded as claimed were claimed, before 133.
CLAIMED_BEFORE = "2026-09-01 12:00:00+00"

#: The links seeded on admissions, by address. Only their hashes are stored.
TOKENS = {
    email: sessions.new_token()
    for email in (
        "held-linked@example.com",
        "held-disabled@example.com",
        "unused-linked@example.com",
        "vacated@example.com",
    )
}


def _replay_to_129(db, owner_actor, admin_conn):
    """The advertised stream through §72 (129), replayed as the declared actor:
    the schema 133 upgrades. `build_stream` emits the blocks in file order, so
    everything before §73's block is 129's database, whatever a later increment
    appends after it. Returns the actor's DSN."""
    stream = build_stream(DEFAULT_DOCS, load_manifest(DEFAULT_MANIFEST))
    cut = stream.index("\n\n-- [§73 ")
    assert stream.rfind("\n\n-- [§", 0, cut) == stream.index("\n\n-- [§72 "), (
        "the block just before §73's is §72's"
    )
    actor = window_actor(db, owner_actor, admin_conn)
    conn = psycopg2.connect(actor)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(stream[:cut])
    finally:
        conn.close()
    return actor


@pytest.fixture
def at_129(owner_window_db, owner_actor, admin_conn):
    actor = _replay_to_129(owner_window_db, owner_actor, admin_conn)
    return {"actor": actor, "ingress": as_user(owner_window_db, "svc_ingress")}


def _apply(world, tmp_path, text=None):
    """The runner's apply, as the declared actor, over a directory that holds
    133 alone: the real file, or *text* in its place under the same name."""
    directory = tmp_path / "migrations"
    directory.mkdir(exist_ok=True)
    target = directory / MIGRATION.name
    if text is None:
        target.write_bytes(MIGRATION.read_bytes())
    else:
        target.write_text(text)
    return apply_pending(world["actor"], directory)


def _seed(world):
    """What a database at 129 can hold, as its owner:

    - admissions held by an account created at the address before 133, which
      used them up without recording it: one with a link (its account's
      address in another case), one never emailed, and one whose account is
      disabled;
    - an admission held by an account that moved to its address, which the
      backfill cannot tell from one created there;
    - admissions claimed through their link, one of them also held;
    - admissions nobody used, one emailed and one not;
    - an admission used by an account that has since moved to another address.

    Each held account signs in as Google subject `sub-<address>`."""
    conn = psycopg2.connect(world["actor"])
    try:
        with conn.cursor() as cur:

            def admit(email, *, claimed=False):
                token = TOKENS.get(email)
                cur.execute(
                    "INSERT INTO signup_admissions"
                    " (email, note, claim_token_hash, emailed_at, claimed_at)"
                    " VALUES (%s, %s, %s, %s, %s)",
                    (
                        email,
                        f"note for {email}",
                        None if token is None else sessions.token_hash(token),
                        None if token is None else "2026-10-01 09:00:00+00",
                        CLAIMED_BEFORE if claimed else None,
                    ),
                )

            def account(email, *, sub, state="active"):
                cur.execute(
                    "INSERT INTO users (primary_email, state) VALUES (%s, %s)"
                    " RETURNING id",
                    (email, state),
                )
                (user,) = cur.fetchone()
                cur.execute(
                    "INSERT INTO user_identities (user_id, provider, external_id)"
                    " VALUES (%s, 'google', %s)",
                    (user, sub),
                )
                return user

            for email in (
                "held-linked@example.com",
                "held-unlinked@example.com",
                "held-disabled@example.com",
                "held-moved-in@example.com",
                "unused-linked@example.com",
                "unused-unlinked@example.com",
                "vacated@example.com",
            ):
                admit(email)
            for email in ("claimed-held@example.com", "claimed-elsewhere@example.com"):
                admit(email, claimed=True)
            account("Held-Linked@Example.com", sub="sub-held-linked@example.com")
            account("held-unlinked@example.com", sub="sub-held-unlinked@example.com")
            account(
                "held-disabled@example.com",
                sub="sub-held-disabled@example.com",
                state="disabled",
            )
            mover = account("mover@example.com", sub="sub-held-moved-in@example.com")
            cur.execute(
                "UPDATE users SET primary_email = 'held-moved-in@example.com'"
                " WHERE id = %s",
                (mover,),
            )
            account("claimed-held@example.com", sub="sub-claimed-held@example.com")
            moved = account("vacated@example.com", sub="sub-vacated@example.com")
            cur.execute(
                "UPDATE users SET primary_email = 'vacated-now@example.com'"
                " WHERE id = %s",
                (moved,),
            )
        conn.commit()
    finally:
        conn.close()


HELD = (
    "held-linked@example.com",
    "held-unlinked@example.com",
    "held-disabled@example.com",
    "held-moved-in@example.com",
)
LEFT = (
    "claimed-held@example.com",
    "claimed-elsewhere@example.com",
    "unused-linked@example.com",
    "unused-unlinked@example.com",
    "vacated@example.com",
)


def _admissions(world):
    return {
        row["email"]: row
        for row in fetch_all(
            world["actor"],
            "SELECT email, admitted_at, note, emailed_at, claimed_at, claim_token_hash"
            " FROM signup_admissions",
        )
    }


def _applied_at(world):
    """When the runner applied 133, which is its transaction's `now()`: the
    ledger row is written in the file's own transaction."""
    rows = fetch_all(
        world["actor"],
        "SELECT applied_at, checksum FROM runner.schema_migrations WHERE version = 133",
    )
    return rows[0] if rows else None


def _installed(world):
    """Whether 133's function and trigger are in the catalog."""
    return fetch_one(
        world["actor"],
        "SELECT EXISTS (SELECT 1 FROM pg_proc"
        "                WHERE proname = 'trg_users_consume_admission'),"
        "       EXISTS (SELECT 1 FROM pg_trigger"
        "                WHERE tgname = 'tg_users_consume_admission')",
    )


def _in_user_plane(world, fn):
    return asyncio.run(in_user_plane(world["ingress"], fn))


def _sign_in(world, sub, email):
    """Google subject *sub* through the sign-in upsert as `svc_ingress`."""
    return _in_user_plane(
        world,
        lambda c: identity.upsert_google_identity(
            c, sub=sub, email=email, display_name=None
        ),
    )


def _ask(world, sql, *params):
    return fetch_one(world["ingress"], sql, params)[0]


class TestTheUpgrade:
    def test_the_backfill_stamps_the_held_admissions_once_and_nothing_else(
        self, at_129, tmp_path
    ):
        """Held, at 129, means used: each held admission is stamped with the
        file's own `now()` and its link cleared, whatever case the account's
        address is in and whether or not the account is disabled. An account
        that moved to the address counts as well, since nothing at 129 tells
        it from one created there: stricter than the trigger, which uses an
        admission up only at an account's creation. Its other columns stay as
        they were. An admission claimed before 133 keeps its
        claim's time, and one nobody used stays open.

        The vacated admission stays open too, and that is the limit the
        backfill has, not a repair: before 133 nothing recorded which account
        used an admission at its address, so once that account moved away the
        admission reads exactly as an unused one does. #1740 leaves that
        history to the owner."""
        _seed(at_129)
        before = _admissions(at_129)
        assert [m.version for m in _apply(at_129, tmp_path).applied] == [133]

        after = _admissions(at_129)
        stamped = _applied_at(at_129)["applied_at"]
        for email in HELD:
            assert after[email] == {
                **before[email],
                "claimed_at": stamped,
                "claim_token_hash": None,
            }, email
        for email in LEFT:
            assert after[email] == before[email], email
        assert set(after) == set(HELD) | set(LEFT)

    def test_after_the_upgrade_a_holders_move_keeps_its_admission_used(
        self, at_129, tmp_path
    ):
        """The point of the backfill: an account that held its address at 129
        moves on, and its admission admits no one, through the address or the
        link. A new account at an open admission is then used up by the
        trigger itself."""
        _seed(at_129)
        _apply(at_129, tmp_path)
        address = "held-linked@example.com"
        used = _admissions(at_129)[address]
        user = _sign_in(at_129, f"sub-{address}", "held-linked-now@example.com")
        assert fetch_one(
            at_129["actor"], "SELECT primary_email FROM users WHERE id = %s", (user,)
        ) == ("held-linked-now@example.com",)
        assert _admissions(at_129)[address] == used
        assert _ask(at_129, "SELECT fn_signup_admitted(%s)", address) is False
        assert (
            _ask(
                at_129,
                "SELECT fn_signup_claim(%s)",
                sessions.token_hash(TOKENS[address]),
            )
            is False
        )

        fresh = "unused-linked@example.com"
        _sign_in(at_129, "sub-fresh", fresh)
        assert _admissions(at_129)[fresh]["claimed_at"] is not None
        assert _admissions(at_129)[fresh]["claim_token_hash"] is None


#: A statement after the backfill that fails, having first checked that the
#: backfill ran in the same transaction.
FAILING_LAST_STATEMENT = """
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM signup_admissions
                  WHERE email = 'held-linked@example.com' AND claimed_at = now()) THEN
    RAISE EXCEPTION 'the backfill had not run';
  END IF;
  RAISE EXCEPTION 'induced after the backfill';
END $$;
"""

#: A postcondition that reads false only once the backfill has stamped a row.
FAILING_POSTCONDITION = (
    "-- runner:postcondition SELECT NOT EXISTS"
    " (SELECT 1 FROM signup_admissions WHERE claimed_at = now())\n"
)

#: Each fault: how the scratch copy is made from the real file, and the
#: refusal it must end in (an error in the injected SQL itself would not).
FAULTS = {
    "a_last_statement": (
        lambda sql: sql + FAILING_LAST_STATEMENT,
        "induced after the backfill",
    ),
    "a_postcondition": (
        lambda sql: FAILING_POSTCONDITION + sql,
        r"postcondition returned \(False,\)",
    ),
}


class TestTheFileIsOneTransaction:
    def test_the_lock_on_users_is_the_files_first_statement(self):
        """The runner retries a lock wait by running the whole file again, so
        a file takes its contended lock before any other work. 133's is
        on `users`, in the mode CREATE TRIGGER takes anyway: taken first, it is
        held from the file's first statement to its commit, and no work comes
        before it for a retry to repeat."""
        assert normalize_statements(MIGRATION.read_text())[0] == (
            "LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE"
        )

    @pytest.mark.parametrize("fault", sorted(FAULTS))
    def test_a_failure_after_the_backfill_takes_the_whole_file_back(
        self, at_129, tmp_path, fault
    ):
        """A scratch copy of 133 that fails after its backfill, applied by the
        real runner: the function, the trigger, the CREATE bracket, the
        backfill and the ledger row go together. The runner's transaction is
        not bypassed and its lock bound is not touched. The real file then
        applies."""
        _seed(at_129)
        before = _admissions(at_129)
        copy, refusal = FAULTS[fault]
        with pytest.raises(MigrationRunnerError, match=refusal) as refused:
            _apply(at_129, tmp_path, copy(MIGRATION.read_text()))
        assert "133" in str(refused.value)

        assert _installed(at_129) == (False, False)
        assert _admissions(at_129) == before
        assert _applied_at(at_129) is None
        assert fetch_one(
            at_129["actor"],
            "SELECT has_schema_privilege('svc_membership', 'public', 'CREATE')",
        ) == (False,)

        assert [m.version for m in _apply(at_129, tmp_path).applied] == [133]
        assert _installed(at_129) == (True, True)
        assert _applied_at(at_129)["checksum"] == (
            hashlib.sha256(MIGRATION.read_bytes()).hexdigest()
        )
        assert all(
            _admissions(at_129)[email]["claimed_at"] is not None for email in HELD
        )


class TestTheTriggersFaultIsTheAccounts:
    def test_a_fault_in_the_stamp_fails_the_users_creation(self, at_129, tmp_path):
        """In this scratch database only, the stamp is made to fail: a CHECK
        that refuses a stamped admission. The account created at an admitted
        address then fails with it, as `svc_ingress` sees it, and nothing is
        left behind. The function never swallows an error. An account at an
        address with no admission writes nothing there, and is created as
        before."""
        _apply(at_129, tmp_path)
        address = "stamp-fault@example.com"
        execute(
            at_129["actor"],
            "INSERT INTO signup_admissions (email) VALUES (%s)",
            (address,),
        )
        execute(
            at_129["actor"],
            "ALTER TABLE signup_admissions ADD CONSTRAINT ck_injected_fault"
            " CHECK (claimed_at IS NULL) NOT VALID",
        )
        before = _admissions(at_129)

        with pytest.raises(DBAPIError, match="ck_injected_fault"):
            _sign_in(at_129, "sub-stamp-fault", address)
        assert fetch_one(
            at_129["actor"],
            "SELECT count(*) FROM users WHERE primary_email = %s",
            (address,),
        ) == (0,)
        assert fetch_one(
            at_129["actor"],
            "SELECT count(*) FROM user_identities WHERE external_id = 'sub-stamp-fault'",
        ) == (0,)
        assert _admissions(at_129) == before

        _in_user_plane(
            at_129,
            lambda c: identity.upsert_google_identity(
                c,
                sub="sub-no-admission",
                email="no-admission@example.com",
                display_name=None,
                signup_open=True,
            ),
        )
        assert fetch_one(
            at_129["actor"],
            "SELECT count(*) FROM users WHERE primary_email = 'no-admission@example.com'",
        ) == (1,)
