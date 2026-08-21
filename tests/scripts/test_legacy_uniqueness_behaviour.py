"""Every legacy uniqueness rule must REFUSE its duplicate and PERMIT its
near-miss (#654 TD-100, second clause).

#654 asks for two things. The first — *"no test asserts migrated schema == ORM
schema"* — **is already served**: `scripts/schema_parity.py` diffs two live
schema signatures and `test_migration_gate.py::TestSchemaParity` runs it against
a runner-replayed database and a `create_all` one, in CI, with a real
PostgreSQL service and its own positive control. This file is the second clause:
*"…or that the 021/015/040 constraints behave as intended."*

## Why parity does not already cover it

**Parity proves the two sides AGREE. It cannot prove they are RIGHT.** The
comparator reads a uniqueness rule as a column set plus a partial predicate and
checks that the migration-built schema and the models-built schema carry the
same one. If both encode the *same wrong predicate*, parity passes and the
constraint is still wrong — a shared misunderstanding is invisible to a
comparison of the two things that share it.

That is not a hypothetical shape for this repo. Migration **021** exists because
`unique_permanent_lock_per_media` was previously wrong: an unconditional unique
index collided a permanent lock with an ordinary one on the same media. The fix
is a *predicate* (`WHERE locked_until IS NULL`), and a predicate is intent
written as SQL. Parity can only ever confirm two files spell it the same way.

## The two-sided shape, and why the second side is the load-bearing one

Each rule is proven twice:

- **REFUSES** — the duplicate it names raises `UniqueViolation`. This is the
  obvious half, and on its own it is satisfied by a rule that is far too broad.
- **PERMITS** — the near-miss, differing only in the discriminating column,
  **commits**. This is what catches a predicate that constrains too much, and
  for the partial indexes it is the whole point: 021's near-miss (a permanent
  lock *and* a temporary lock on one media item) is literally the bug 021 was
  written to fix, and 040's (two credentials differing only in `auth_method`) is
  literally what 040 widened the key to allow.

For the single-column rules the near-miss cannot fail on semantics, and it is
kept anyway as the **insert-path control** — without it, a `UniqueViolation`
that was really a malformed row would read as the rule working.

## The denominator comes from the catalog, never from a list here

A test that asserted "there are 11 uniqueness rules and here they are" would pin
today's schema and pass by restating it. Instead `enumerate_uniqueness_rules()`
reads `pg_indexes` on the live replayed schema, and the coverage test asserts
every rule it finds has a case. **A uniqueness rule added by a future migration
fails this suite until someone proves what it refuses and what it permits** —
there is no expected number to edit.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Callable

import psycopg2
import pytest
from psycopg2 import errors

pytestmark = [pytest.mark.integration]

#: Primary keys are excluded, and the exclusion is stated rather than silently
#: filtered: PK uniqueness is enforced by PostgreSQL itself with no predicate
#: and no composite subtlety, so a behaviour test over one would exercise the
#: database rather than this schema's intent. Everything else is in scope.
EXCLUDED_SUFFIX = "_pkey"


def table_of(conn, index: str) -> str:
    """The table a uniqueness rule guards, from the catalog rather than from a
    field on `Rule` — a hand-written table name is a second place to be wrong,
    and the whole design of this file is that the schema is the source."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tablename FROM pg_indexes"
            " WHERE schemaname = current_schema() AND indexname = %s",
            (index,),
        )
        row = cur.fetchone()
        assert row, f"{index} is not an index in this schema"
        return row[0]


def row_count(conn, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table}")
        return cur.fetchone()[0]


def enumerate_uniqueness_rules(conn) -> set:
    """Every non-PK uniqueness rule in the schema under test, from the catalog.

    Reads BOTH sources deliberately — a `UNIQUE` constraint and a bare
    `CREATE UNIQUE INDEX` are the same guarantee wearing different catalog
    rows, and this repo uses both (015/021 are indexes, 040 is a constraint).
    `pg_indexes` covers them together because a unique constraint is backed by
    an index.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT indexname FROM pg_indexes"
            " WHERE schemaname = current_schema() AND indexdef LIKE '%UNIQUE%'"
        )
        return {r[0] for r in cur.fetchall() if not r[0].endswith(EXCLUDED_SUFFIX)}


@dataclass(frozen=True)
class Rule:
    """One uniqueness rule, and the two writes that prove what it means."""

    index: str
    migration: str
    refuses: str  # prose — what the duplicate is
    permits: str  # prose — what the near-miss is, and why it must survive
    setup: Callable  # the first row(s); must commit
    duplicate: Callable  # must raise UniqueViolation
    near_miss: Callable  # must commit


def _x(conn, sql, *params):
    with conn.cursor() as cur:
        cur.execute(sql, params or None)


def _uuid():
    return str(uuid.uuid4())


CHAT_A = "eeee0000-0000-0000-0000-00000000000a"
CHAT_B = "eeee0000-0000-0000-0000-00000000000b"
USER_A = "eeee1111-0000-0000-0000-00000000000a"
MEDIA_A = "eeee2222-0000-0000-0000-00000000000a"
ACCT_A = "eeee3333-0000-0000-0000-00000000000a"


def _chat(conn, cid=CHAT_A, tg=-8001):
    _x(conn, "INSERT INTO chat_settings (id, telegram_chat_id) VALUES (%s,%s)", cid, tg)
    return cid


def _user(conn, uid=USER_A, tg=80001):
    _x(conn, "INSERT INTO users (id, telegram_user_id) VALUES (%s,%s)", uid, tg)
    return uid


def _account(conn, aid=ACCT_A, ref="ig-1"):
    _x(
        conn,
        "INSERT INTO instagram_accounts (id, display_name, instagram_account_id)"
        " VALUES (%s,'@a',%s)",
        aid,
        ref,
    )
    return aid


def _media(conn, mid=MEDIA_A, chat=CHAT_A, path="/p/a", ig_id=None):
    _x(
        conn,
        "INSERT INTO media_items (id, file_path, file_name, file_size, file_hash,"
        " source_type, chat_settings_id, instagram_media_id)"
        " VALUES (%s,%s,'n',1,%s,'google_drive',%s,%s)",
        mid,
        path,
        f"h-{mid}",
        chat,
        ig_id,
    )
    return mid


def _token(
    conn, service="google_drive", ttype="oauth_refresh", chat=None, acct=None, auth=None
):
    _x(
        conn,
        "INSERT INTO api_tokens (service_name, token_type, token_value, issued_at,"
        " chat_settings_id, instagram_account_id, auth_method)"
        " VALUES (%s,%s,'v',now(),%s,%s,%s)",
        service,
        ttype,
        chat,
        acct,
        auth,
    )


def _lock(conn, media=MEDIA_A, until=None):
    _x(
        conn,
        "INSERT INTO media_posting_locks (media_item_id, locked_until) VALUES (%s,%s)",
        media,
        until,
    )


# ---------------------------------------------------------------------------
# The rules. Ordered simple-key first, then composite, then the four PARTIAL
# indexes — which are the ones a predicate can get wrong and parity cannot see.
# ---------------------------------------------------------------------------

RULES: tuple[Rule, ...] = (
    Rule(
        index="users_telegram_user_id_key",
        migration="001",
        refuses="a second user with the same telegram_user_id",
        permits="a user with a different telegram_user_id (insert-path control)",
        setup=lambda c: _user(c, tg=80001),
        duplicate=lambda c: _user(c, uid=_uuid(), tg=80001),
        near_miss=lambda c: _user(c, uid=_uuid(), tg=80002),
    ),
    Rule(
        index="chat_settings_telegram_chat_id_key",
        migration="006",
        refuses="a second chat with the same telegram_chat_id",
        permits="a chat with a different telegram_chat_id (insert-path control)",
        setup=lambda c: _chat(c, tg=-8001),
        duplicate=lambda c: _chat(c, cid=_uuid(), tg=-8001),
        near_miss=lambda c: _chat(c, cid=_uuid(), tg=-8002),
    ),
    Rule(
        index="unique_instagram_account",
        migration="007",
        refuses="a second row for the same provider account id",
        permits="a different provider account id (insert-path control)",
        setup=lambda c: _account(c, ref="ig-1"),
        duplicate=lambda c: _account(c, aid=_uuid(), ref="ig-1"),
        near_miss=lambda c: _account(c, aid=_uuid(), ref="ig-2"),
    ),
    Rule(
        index="unique_active_onboarding",
        migration="023",
        refuses="a second concurrent onboarding session for one user",
        permits="a session for a different user (insert-path control)",
        setup=lambda c: _x(
            c,
            "INSERT INTO onboarding_sessions (user_id, expires_at)"
            " VALUES (%s, now() + interval '1 day')",
            _user(c),
        ),
        duplicate=lambda c: _x(
            c,
            "INSERT INTO onboarding_sessions (user_id, expires_at)"
            " VALUES (%s, now() + interval '1 day')",
            USER_A,
        ),
        near_miss=lambda c: _x(
            c,
            "INSERT INTO onboarding_sessions (user_id, expires_at)"
            " VALUES (%s, now() + interval '1 day')",
            _user(c, uid=_uuid(), tg=80099),
        ),
    ),
    Rule(
        index="unique_user_chat_membership",
        migration="023",
        refuses="the same user joined to the same chat twice",
        permits="THE SAME USER in a DIFFERENT chat — the key is composite, and"
        " a single-column key here would make a user joinable to one chat only",
        setup=lambda c: _x(
            c,
            "INSERT INTO user_chat_memberships (user_id, chat_settings_id,"
            " instance_role, joined_at) VALUES (%s,%s,'member',now())",
            _user(c),
            _chat(c),
        ),
        duplicate=lambda c: _x(
            c,
            "INSERT INTO user_chat_memberships (user_id, chat_settings_id,"
            " instance_role, joined_at) VALUES (%s,%s,'member',now())",
            USER_A,
            CHAT_A,
        ),
        near_miss=lambda c: _x(
            c,
            "INSERT INTO user_chat_memberships (user_id, chat_settings_id,"
            " instance_role, joined_at) VALUES (%s,%s,'member',now())",
            USER_A,
            _chat(c, cid=CHAT_B, tg=-8002),
        ),
    ),
    Rule(
        index="unique_file_path_per_tenant",
        migration="014",
        refuses="the same file_path indexed twice for one tenant",
        permits="THE SAME file_path under a DIFFERENT tenant — the whole point"
        " of the per-tenant key, and what a single-column key would forbid",
        setup=lambda c: _media(c, chat=_chat(c), path="/p/shared"),
        duplicate=lambda c: _media(c, mid=_uuid(), chat=CHAT_A, path="/p/shared"),
        near_miss=lambda c: _media(
            c, mid=_uuid(), chat=_chat(c, cid=CHAT_B, tg=-8002), path="/p/shared"
        ),
    ),
    Rule(
        index="unique_credential_per_account",
        migration="040",
        refuses="two credentials identical in service, type, account AND auth method",
        permits="two credentials differing ONLY in auth_method — this is exactly"
        " what 040 widened the key to allow, so a key that still refused it"
        " would mean 040 never took effect",
        setup=lambda c: _token(
            c, "instagram", "access_token", acct=_account(c), auth="instagram_login"
        ),
        duplicate=lambda c: _token(
            c, "instagram", "access_token", acct=ACCT_A, auth="instagram_login"
        ),
        near_miss=lambda c: _token(
            c, "instagram", "access_token", acct=ACCT_A, auth="facebook_login"
        ),
    ),
    # ----- the four PARTIAL indexes -------------------------------------
    Rule(
        index="unique_google_drive_token_per_chat",
        migration="015",
        refuses="a second google_drive token of the same type for one chat",
        permits="an INSTAGRAM token of the same type for the SAME chat — the"
        " predicate is scoped to google_drive, so a rule that refused this"
        " would be constraining rows it was never meant to see",
        setup=lambda c: _token(c, "google_drive", "oauth_refresh", chat=_chat(c)),
        duplicate=lambda c: _token(c, "google_drive", "oauth_refresh", chat=CHAT_A),
        near_miss=lambda c: _token(c, "instagram", "oauth_refresh", chat=CHAT_A),
    ),
    Rule(
        index="idx_media_items_file_path_legacy_unique",
        migration="044",
        refuses="two NULL-tenant rows sharing a file_path — which"
        " unique_file_path_per_tenant CANNOT catch, because NULLs are distinct"
        " in a plain UNIQUE and the pair never collides there",
        permits="the same file_path once a tenant is set — that row is outside"
        " the predicate and belongs to the per-tenant key instead",
        setup=lambda c: _media(c, chat=None, path="/p/orphan"),
        duplicate=lambda c: _media(c, mid=_uuid(), chat=None, path="/p/orphan"),
        near_miss=lambda c: _media(c, mid=_uuid(), chat=_chat(c), path="/p/orphan"),
    ),
    Rule(
        index="idx_media_items_instagram_media_id",
        migration="013",
        refuses="two rows claiming the same Instagram media id",
        permits="MANY rows with a NULL instagram_media_id — the reason the index"
        " is partial at all; unconditional it would allow exactly one"
        " un-backfilled media item in the whole table",
        setup=lambda c: _media(c, chat=_chat(c), path="/p/1", ig_id="ig-media-1"),
        duplicate=lambda c: _media(
            c, mid=_uuid(), chat=CHAT_A, path="/p/2", ig_id="ig-media-1"
        ),
        near_miss=lambda c: [
            _media(c, mid=_uuid(), chat=CHAT_A, path="/p/3", ig_id=None),
            _media(c, mid=_uuid(), chat=CHAT_A, path="/p/4", ig_id=None),
        ],
    ),
    Rule(
        index="unique_permanent_lock_per_media",
        migration="021",
        refuses="two PERMANENT locks (locked_until IS NULL) on one media item",
        permits="a permanent lock AND a temporary one on the SAME media item —"
        " this is the defect 021 was written to fix: the pre-021 unconditional"
        " index collided them, so a rule refusing this has regressed to it",
        setup=lambda c: _lock(c, media=_media(c, chat=_chat(c)), until=None),
        duplicate=lambda c: _lock(c, media=MEDIA_A, until=None),
        near_miss=lambda c: _x(
            c,
            "INSERT INTO media_posting_locks (media_item_id, locked_until)"
            " VALUES (%s, now() + interval '1 day')",
            MEDIA_A,
        ),
    ),
)

BY_INDEX = {r.index: r for r in RULES}


@pytest.fixture()
def conn(replayed_db):
    c = psycopg2.connect(replayed_db)
    c.autocommit = True
    yield c
    c.close()


class TestTheDenominatorComesFromTheCatalog:
    def test_every_uniqueness_rule_in_the_schema_has_a_behaviour_case(self, conn):
        """The anti-pinning half. A count assertion ("there are 11 rules")
        would restate today's schema and pass by running the code it guards;
        this reads the live catalog and demands a case for whatever it finds,
        so a rule a future migration adds fails here until someone states what
        it refuses and what it permits."""
        found = enumerate_uniqueness_rules(conn)
        uncovered = sorted(found - set(BY_INDEX))

        assert uncovered == [], (
            "uniqueness rules with no behaviour case: "
            + ", ".join(uncovered)
            + ". Add a Rule stating the duplicate it refuses AND the near-miss"
            " it permits — the second is what catches a rule that is too broad."
        )

    def test_no_case_names_a_rule_the_schema_does_not_have(self, conn):
        """The other direction. A case for a dropped index would keep passing
        against nothing at all — the tests would exercise inserts that collide
        with no rule and the suite would look healthy."""
        found = enumerate_uniqueness_rules(conn)
        stale = sorted(set(BY_INDEX) - found)

        assert stale == [], "cases naming rules absent from the schema: " + ", ".join(
            stale
        )


class TestEachRuleRefusesItsDuplicate:
    @pytest.mark.parametrize("index", sorted(BY_INDEX), ids=str)
    def test_the_duplicate_is_refused_BY_THIS_RULE(self, index, conn):
        """`pytest.raises(UniqueViolation)` alone proves only that *something*
        refused, and several of these tables carry more than one uniqueness
        rule — `media_items` carries three. A duplicate that tripped a
        neighbouring rule instead would pass a bare raises-check while saying
        nothing about the rule under test, and would keep passing if the rule
        were dropped entirely.

        PostgreSQL names the offending index in the error, so the assertion is
        on the NAME rather than on the exception type."""
        rule = BY_INDEX[index]
        rule.setup(conn)

        with pytest.raises(errors.UniqueViolation) as caught:
            rule.duplicate(conn)

        assert caught.value.diag.constraint_name == index, (
            f"the duplicate was refused by {caught.value.diag.constraint_name!r},"
            f" not by {index!r} — this test was passing for the wrong reason"
        )


class TestEachRulePermitsItsNearMiss:
    """The half that catches a rule constraining more than it should.

    A `refuses` test alone is satisfied by a rule that refuses everything.
    These are also what keep the `refuses` results honest: without them a
    `UniqueViolation` raised by a malformed row would read as the rule working.
    """

    @pytest.mark.parametrize("index", sorted(BY_INDEX), ids=str)
    def test_the_near_miss_is_permitted_AND_LANDS(self, index, conn):
        """Not-raising is only half the assertion, and it is the half that a
        near-miss doing NOTHING would also satisfy — a `lambda c: None` passes
        a bare no-raise check forever. So the row has to arrive: the guarded
        table's count must go up.

        The table is read from the catalog rather than declared on the Rule,
        for the same reason the denominator is: a hand-written name is a second
        place to be wrong."""
        rule = BY_INDEX[index]
        table = table_of(conn, index)
        rule.setup(conn)
        before = row_count(conn, table)

        rule.near_miss(conn)  # must not raise

        after = row_count(conn, table)
        assert after > before, (
            f"the near-miss for {index} raised nothing but wrote nothing either"
            f" — {table} still holds {after} rows, so 'permitted' was vacuous"
        )


class TestTheGateCatchesTheRegressionsItExistsFor:
    """The gate's own positive controls, and they are not invented shapes —
    each mutation below re-creates a defect this repo actually shipped and then
    fixed with a migration. If a mutant passes, the corresponding rule's
    behaviour test is decorative.

    The mutation is applied to the SCHEMA rather than to this file, because the
    schema is where the property lives: these re-run the real assertions
    against a database where the migration has been undone.
    """

    def test_undoing_021s_predicate_is_caught(self, conn):
        """Pre-021, `unique_permanent_lock_per_media` was unconditional and a
        permanent lock collided with an ordinary one on the same media. That is
        the bug 021 fixed. Parity cannot see it — the models would simply have
        to agree — so this rule's `permits` case is the only thing standing on
        it."""
        rule = BY_INDEX["unique_permanent_lock_per_media"]
        rule.setup(conn)
        _x(conn, "DROP INDEX unique_permanent_lock_per_media")
        _x(
            conn,
            "CREATE UNIQUE INDEX unique_permanent_lock_per_media"
            " ON media_posting_locks (media_item_id)",
        )

        with pytest.raises(errors.UniqueViolation) as caught:
            rule.near_miss(conn)

        assert caught.value.diag.constraint_name == "unique_permanent_lock_per_media"

    def test_undoing_040s_widening_is_caught(self, conn):
        """040 widened `unique_credential_per_account` to include
        `auth_method`, so one account can hold both an instagram-login and a
        facebook-login credential. Narrow it back and the near-miss collides."""
        rule = BY_INDEX["unique_credential_per_account"]
        rule.setup(conn)
        _x(conn, "ALTER TABLE api_tokens DROP CONSTRAINT unique_credential_per_account")
        _x(
            conn,
            "ALTER TABLE api_tokens ADD CONSTRAINT unique_credential_per_account"
            " UNIQUE (service_name, token_type, instagram_account_id)",
        )

        with pytest.raises(errors.UniqueViolation) as caught:
            rule.near_miss(conn)

        assert caught.value.diag.constraint_name == "unique_credential_per_account"

    def test_scoping_015s_predicate_away_is_caught(self, conn):
        """015's index is scoped to `service_name = 'google_drive'`. Drop the
        predicate and it starts constraining Instagram tokens it was never
        meant to see."""
        rule = BY_INDEX["unique_google_drive_token_per_chat"]
        rule.setup(conn)
        _x(conn, "DROP INDEX unique_google_drive_token_per_chat")
        _x(
            conn,
            "CREATE UNIQUE INDEX unique_google_drive_token_per_chat"
            " ON api_tokens (token_type, chat_settings_id)",
        )

        with pytest.raises(errors.UniqueViolation):
            rule.near_miss(conn)

    def test_a_new_uniqueness_rule_with_no_case_fails_coverage(self, conn):
        """The denominator is the catalog, so this is what makes a future
        migration's new rule land as a red test rather than as silence."""
        _x(conn, "CREATE UNIQUE INDEX uq_probe_new_rule ON users (telegram_username)")

        found = enumerate_uniqueness_rules(conn)

        assert "uq_probe_new_rule" in found - set(BY_INDEX), (
            "a uniqueness rule added to the schema was not surfaced as"
            " uncovered — the coverage test cannot see new rules"
        )

    def test_a_dropped_rule_fails_the_stale_case_check(self, conn):
        """And the other direction: a case whose rule is gone would otherwise
        keep 'passing' against inserts that now collide with nothing."""
        _x(conn, "DROP INDEX unique_permanent_lock_per_media")

        found = enumerate_uniqueness_rules(conn)

        assert "unique_permanent_lock_per_media" in set(BY_INDEX) - found
