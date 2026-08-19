"""#905 — an id prefix is matched literally, not as a LIKE pattern.

Both id-prefix lookups built `cast(col, String).like(f"{prefix}%")` from a
caller-supplied prefix. `%` and `_` are LIKE metacharacters, so such a prefix
was interpreted as a **pattern**: `_` matches any single character, `%` matches
any run, and the match set silently widened.

**No test noticed, and no test could have**, because every existing case passes
a plain hex prefix — a string in which no metacharacter appears. The defect is
only visible when the *caller's* input carries one, which is exactly the input
nobody was writing.

Real database: LIKE semantics are the database's, and a mocked query would
assert that `.like()` was called with something — the shape of test that cannot
fail on this.

**The discriminator is deliberately chosen so the two behaviours disagree in
opposite directions.** A UUID contains only hex digits and dashes, so no real
id can contain `%` or `_` literally. A wildcard-bearing prefix therefore
matches MORE than one row under the defect and ZERO rows under the fix — never
the same answer, so the test cannot pass for the wrong reason.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from src.repositories.chat_settings_repository import ChatSettingsRepository
from src.repositories.instagram_account_repository import InstagramAccountRepository

#: Two ids differing at the LAST character of the 8-hex prefix, so a `_` in
#: that position matches both under LIKE and neither literally.
ID_A = "aaaaaaaa-1111-4111-8111-111111111111"
ID_B = "aaaaaaab-2222-4222-8222-222222222222"


@pytest.fixture(autouse=True)
def _route_repos_to_test_db(setup_test_database, monkeypatch):
    if setup_test_database is None:
        pytest.skip("Database not available - skipping integration test")

    import src.config.database as db_module

    monkeypatch.setattr(
        db_module,
        "SessionLocal",
        sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=setup_test_database,
            expire_on_commit=False,
        ),
    )
    yield


@pytest.fixture
def two_accounts():
    """Two real rows with CONTROLLED ids, cleaned up afterwards."""
    import random

    settings = ChatSettingsRepository().get_or_create(-random.randint(10**11, 10**12))
    repo = InstagramAccountRepository()
    try:
        for account_id, name in ((ID_A, "Account A"), (ID_B, "Account B")):
            repo.db.execute(
                text(
                    "INSERT INTO instagram_accounts"
                    " (id, display_name, instagram_account_id, is_active)"
                    " VALUES (CAST(:i AS uuid), :n, :m, true)"
                    " ON CONFLICT (id) DO NOTHING"
                ),
                {"i": account_id, "n": name, "m": str(uuid.uuid4().int)[:15]},
            )
        repo.db.commit()
        yield repo
    finally:
        cleanup = InstagramAccountRepository()
        try:
            cleanup.db.execute(
                text(
                    "DELETE FROM instagram_accounts WHERE id IN"
                    " (CAST(:a AS uuid), CAST(:b AS uuid))"
                ),
                {"a": ID_A, "b": ID_B},
            )
            cleanup.db.execute(
                text("DELETE FROM chat_settings WHERE id = CAST(:i AS uuid)"),
                {"i": str(settings.id)},
            )
            cleanup.db.commit()
        finally:
            cleanup.close()
            repo.close()


@pytest.mark.integration
class TestAnIdPrefixIsMatchedLiterally:
    def test_a_plain_prefix_still_resolves_its_own_row(self, two_accounts):
        """Positive control. Without it, a lookup that returned None for
        everything would satisfy every wildcard assertion below."""
        found = two_accounts.get_by_id_prefix("aaaaaaaa")
        assert found is not None and str(found.id) == ID_A
        other = two_accounts.get_by_id_prefix("aaaaaaab")
        assert other is not None and str(other.id) == ID_B

    def test_an_underscore_is_a_literal_not_a_single_character_wildcard(
        self, two_accounts
    ):
        """`aaaaaaa_` matches BOTH rows under LIKE and NEITHER literally.

        This is the case #905 names: the prefix widens the match set, and the
        caller receives whichever row the database happened to order first —
        a row it did not ask for.
        """
        found = two_accounts.get_by_id_prefix("aaaaaaa_")
        assert found is None, (
            f"'aaaaaaa_' resolved {getattr(found, 'id', None)} — `_` was"
            " interpreted as a single-character wildcard, so the prefix"
            " matched a row the caller did not name (#905)"
        )

    def test_a_percent_is_a_literal_not_a_run_wildcard(self, two_accounts):
        """`a%` matches every row beginning with `a` under LIKE — here, both."""
        found = two_accounts.get_by_id_prefix("a%")
        assert found is None, (
            f"'a%' resolved {getattr(found, 'id', None)} — `%` was interpreted"
            " as a wildcard, so a two-character prefix reached rows whose ids"
            " share only their first character (#905)"
        )

    def test_a_bare_percent_does_not_match_everything(self, two_accounts):
        """The widest form of the same defect: one metacharacter reaching the
        entire table, which is what makes this worth fixing ahead of a caller
        that is not behind an ownership gate."""
        assert two_accounts.get_by_id_prefix("%") is None

    def test_the_escape_character_is_also_a_literal(self, two_accounts):
        """A backslash is the trap in the *fix* rather than the defect: an
        escaping implementation that forgot to escape its own escape character
        would pass the two tests above and fail here. This expression has no
        escape character to forget, and this pins that."""
        assert two_accounts.get_by_id_prefix("\\") is None
        assert two_accounts.get_by_id_prefix("aaaaaaaa\\") is None

    def test_an_empty_prefix_still_matches_as_it_always_did(self, two_accounts):
        """Stated rather than changed. An empty prefix matched everything
        before and still does — this fix closes a metacharacter hole, not a
        missing-input check, and quietly turning empty into "match nothing"
        would be a behaviour change hiding inside a security fix.
        """
        assert two_accounts.get_by_id_prefix("") is not None


@pytest.mark.integration
class TestTheOtherPrefixLookupIsFixedToo:
    """`queue_repository.get_by_id_prefix` is the same expression, and it is
    the one reachable today — its caller passes `SYSTEM_SCOPE` deliberately
    and checks ownership afterwards. The issue named only the account lookup;
    a fix that patched that one alone would leave the live instance."""

    def test_neither_repository_builds_a_like_pattern_from_its_input(self):
        """Structural, because this is the property that must not regress.

        An escaping fix would still read `.like(...)` and could lose its
        escaping in a later edit without the operator changing. Asserting the
        pattern language is absent is what makes the fix survive editing.
        """
        import inspect

        from src.repositories import instagram_account_repository, queue_repository

        for module in (instagram_account_repository, queue_repository):
            source = inspect.getsource(module)
            assert 'like(f"' not in source, (
                f"{module.__name__} builds a LIKE pattern from an f-string —"
                " a caller-supplied metacharacter is interpreted again (#905)"
            )
            assert "id_prefix_matches(" in source, (
                f"{module.__name__} no longer routes through the one helper"
            )
