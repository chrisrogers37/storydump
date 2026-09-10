"""The posting aggregate's Python contract (#1268).

**Scope, stated because the gap matters.** These drive the module against a
fake executor: they pin the SHAPE of the answer — the null rule, the coercion,
and the predicate the two posting aggregates are filtered on — and they do **not
execute a single statement against PostgreSQL.** The behaviour of the SQL is
covered by the endpoint's live answer and by nothing here.

That is a deliberate line rather than an omission. `tests/scripts/`'s real-
database harness serializes every suite on one cluster-wide advisory lock, and
adding a monitoring aggregate to that queue costs every other suite wall-clock
to prove a `count(*)`. What it would buy is small; what these buy is the one
property a reader cannot check by looking — see the last test.
"""

from __future__ import annotations

from src.services.target.posting_health import (
    destinations,
    posting_freshness,
    publish_attempts,
)


class FakeExecutor:
    """Captures the statement and answers with one canned row.

    Deliberately not a mock: the statement text is the thing under test in
    `TestTheBackfillExclusion`, so it has to be recorded rather than discarded.
    """

    def __init__(self, row):
        self.row = row
        self.statements: list[str] = []

    async def execute(self, statement):
        self.statements.append(str(statement))
        return self

    def mappings(self):
        return self

    def one(self):
        return self.row


class TestNothingEverPostedAnswersNULLRatherThanZERO:
    """`0` is the most reassuring value the age fields have — "a post landed
    just now" — and it is what a `coalesce` would return for a product that has
    never posted once. The whole defect class this instrument exists for is a
    reading that fails toward good news."""

    async def test_a_never_posted_estate_returns_a_null_age(self):
        out = await posting_freshness(
            FakeExecutor(
                {
                    "posted_ever": 0,
                    "last_post_age_seconds": None,
                    "intents_ever": 0,
                    "oldest_intent_age_seconds": None,
                }
            )
        )
        assert out["posted_ever"] == 0
        assert out["last_post_age_seconds"] is None
        assert out["oldest_intent_age_seconds"] is None

    async def test_a_real_age_is_coerced_to_a_whole_number_of_seconds(self):
        """Postgres `EXTRACT(EPOCH ...)` returns a `Decimal`; the poller's
        strictness check rejects anything that is not an `int`, so a float
        reaching the wire would classify the endpoint as unreachable."""
        out = await posting_freshness(
            FakeExecutor(
                {
                    "posted_ever": 3,
                    "last_post_age_seconds": 4321.987,
                    "intents_ever": 9,
                    "oldest_intent_age_seconds": 99999.5,
                }
            )
        )
        assert out == {
            "posted_ever": 3,
            "last_post_age_seconds": 4321,
            "intents_ever": 9,
            "oldest_intent_age_seconds": 99999,
        }
        assert all(isinstance(v, int) for v in out.values())


class TestTheOtherTwoReads:
    async def test_the_cap_ledger_is_returned_as_whole_counts(self):
        out = await publish_attempts(
            FakeExecutor({"debited_total": 37, "ledger_days": 9})
        )
        assert out == {"debited_total": 37, "ledger_days": 9}

    async def test_an_empty_ledger_is_zero_rather_than_null(self):
        """`sum()` over no rows is `NULL` in SQL, and a `None` here would be
        rejected by the poller as a malformed count. The `coalesce` in the
        statement is what stops that, and this is what notices if it goes."""
        out = await publish_attempts(
            FakeExecutor({"debited_total": 0, "ledger_days": 0})
        )
        assert out == {"debited_total": 0, "ledger_days": 0}

    async def test_the_destination_count_is_an_int(self):
        out = await destinations(
            FakeExecutor(
                {
                    "accounts_active": 2,
                    "oldest_active_destination_age_seconds": 604800.75,
                }
            )
        )
        assert out == {
            "accounts_active": 2,
            "oldest_active_destination_age_seconds": 604800,
        }

    async def test_an_estate_with_no_destinations_ages_to_NULL(self):
        """`max()` over no rows is NULL, and it must stay NULL rather than
        coalesce to 0 — a zero here would be the *freshest possible* rung and
        would say a destination was connected this instant."""
        out = await destinations(
            FakeExecutor(
                {
                    "accounts_active": 0,
                    "oldest_active_destination_age_seconds": None,
                }
            )
        )
        assert out["accounts_active"] == 0
        assert out["oldest_active_destination_age_seconds"] is None

    async def test_the_destination_read_is_ONE_statement_not_two(self):
        """The age rides the count's existing scan — same table, same filter.
        A second `execute` here would be a third round trip for a column."""
        fake = FakeExecutor(
            {"accounts_active": 1, "oldest_active_destination_age_seconds": 10}
        )
        await destinations(fake)
        assert len(fake.statements) == 1


class TestTheBackfillExclusion:
    """The one property in this module a reader cannot check by looking.

    `ck_posted_complete` EXEMPTS `published_via = 'legacy_backfill'` from every
    evidence requirement, so it is the one `posted` row the database accepts
    with no proof that anything reached Instagram. (The M.3 transform that would
    have produced them was cancelled — FC-7 §6, greenfield — so the filter
    guards what the schema still permits, not a migration that is coming.) Any
    bulk insert of such rows carries a fresh `entered_state_at` and would read
    as *a post just landed*, announcing a recovery nobody observed and then
    going quiet for a full threshold.

    Both posting aggregates must carry the filter. Testing only the count would
    pass while the AGE went unfiltered, which is the half that manufactures the
    false recovery.
    """

    @staticmethod
    async def _statement():
        fake = FakeExecutor(
            {
                "posted_ever": 0,
                "last_post_age_seconds": None,
                "intents_ever": 0,
                "oldest_intent_age_seconds": None,
            }
        )
        await posting_freshness(fake)
        return " ".join(fake.statements[0].split())

    async def test_both_posting_aggregates_exclude_backfilled_rows(self):
        sql = await self._statement()
        predicate = (
            "state = 'posted' AND published_via NOT IN ('legacy_backfill', 'dry_run')"
        )
        # Once for `posted_ever`, once for `last_post_age_seconds`. A single
        # occurrence means one of the two is reading the unfiltered population.
        assert sql.count(predicate) == 2

    async def test_no_posting_aggregate_is_filtered_on_state_alone(self):
        """The mutation this guards against is dropping the second conjunct,
        which leaves a `state = 'posted'` filter that looks entirely correct."""
        sql = await self._statement()
        assert "state = 'posted' AND" in sql
        assert "FILTER (WHERE state = 'posted')" not in sql

    async def test_the_intent_anchor_is_deliberately_NOT_filtered(self):
        """`oldest_intent_age_seconds` counts every intent ever created,
        backfilled rows included, and that is correct: it measures how long the
        estate has had something to post. Excluding them could only make the
        grace clock start LATER, which is the direction that suppresses alerts.
        """
        sql = await self._statement()
        assert (
            "max(EXTRACT(EPOCH FROM now() - created_at)) "
            "AS oldest_intent_age_seconds" in sql
        )
        assert "count(*) AS intents_ever" in sql
