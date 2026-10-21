"""The deletion cleanup's pure edges; the database behaviour is
`tests/scripts/test_past_posts_deletion_cleanup_gate.py`'s."""

import uuid
from contextlib import asynccontextmanager

import pytest

from src.services.target import past_posts_deletion, past_posts_transit_cleanup
from src.services.target.transit import TransitDestroyRefused

WS, RECEIPT = str(uuid.uuid4()), str(uuid.uuid4())


def test_a_job_names_its_workspace_and_receipt_or_is_refused_by_name():
    job = {"workspace_id": WS, "payload": {"v": 1, "receipt_id": RECEIPT.upper()}}
    assert past_posts_deletion._subject(job) == (WS, RECEIPT)
    for payload in (
        None,
        {"v": 2, "receipt_id": RECEIPT},
        {"v": 1},
        {"v": 1, "receipt_id": "not-a-uuid"},
        "not an object",
    ):
        with pytest.raises(past_posts_deletion.DeletionCleanupRefused) as refused:
            past_posts_deletion._subject({"workspace_id": WS, "payload": payload})
        assert refused.value.reason == "payload_invalid"
    with pytest.raises(past_posts_deletion.DeletionCleanupRefused):
        past_posts_deletion._subject({"workspace_id": None, "payload": job["payload"]})


@pytest.mark.parametrize(
    "message,outcome",
    [
        ("past-posts redaction refused: operation_unresolved", "operation_unresolved"),
        ("past-posts redaction refused: transit_unresolved", "transit_unresolved"),
        ("past-posts redaction refused: intent_live", "intent_live"),
        ("past-posts redaction refused: scope_unavailable", None),
        ("past-posts redaction refused: intent_lively", None),
        ("meta receipt progress refused: scope_held", None),
    ],
)
def test_only_the_doors_three_waiting_refusals_become_outcomes(message, outcome):
    found = past_posts_deletion._REDACTION_REFUSAL.search(message)
    assert (found.group(1) if found else None) == outcome


class _Died(BaseException):
    pass


async def test_a_provider_error_answers_nothing_but_a_process_death_propagates():
    async def answers(ref, *, resource_type):
        return ("answered", ref, resource_type)

    async def refuses(ref, *, resource_type):
        raise TransitDestroyRefused("neither ok nor not found")

    async def dies(ref, *, resource_type):
        raise _Died()

    ask = past_posts_transit_cleanup._ask
    assert await ask(answers, "ref", "image", "lookup") == ("answered", "ref", "image")
    assert (
        await ask(refuses, "ref", "image", "destroy")
        is past_posts_transit_cleanup._UNANSWERED
    )
    with pytest.raises(_Died):
        await ask(dies, "ref", "image", "verification")


# --- D, the recount: what the completion predicate reads ----------------------

NOTHING_LEFT = dict.fromkeys(past_posts_deletion._COUNTED, 0)
JOB = {"id": "job-1", "lease_token": "lease-1"}


class _Recount:
    """`_report` over a scripted recount: the statement answers *remaining* for
    every source part, the transit drain's `owed` answers *owed* per part, and
    each progress report is kept."""

    def __init__(self, monkeypatch, remaining, owed=((0, 0),)):
        self.reports, self._owed = [], iter(owed)
        self._remaining = remaining

        @asynccontextmanager
        async def unit(engine, workspace_id):
            yield "conn"

        async def nothing(*args, **kwargs):
            return None

        async def recount(conn, sql, **params):
            assert sql is past_posts_deletion._REMAINING
            return self._remaining

        async def owed_by_part(conn, **kwargs):
            return next(self._owed)

        async def progress(conn, receipt, scope, state, results, reason, retry):
            self.reports.append((state, results, reason, retry))
            return state

        monkeypatch.setattr(past_posts_deletion, "_uow", unit)
        monkeypatch.setattr(past_posts_deletion, "_progress", progress)
        monkeypatch.setattr(past_posts_deletion.readers, "row", recount)
        monkeypatch.setattr(
            past_posts_deletion.source_state, "lock_all_workspace_sources", nothing
        )
        monkeypatch.setattr(past_posts_deletion.jobs, "assert_lease", nothing)
        monkeypatch.setattr(past_posts_transit_cleanup, "owed", owed_by_part)
        self.parts = tuple(
            (f"source-{n}", frozenset({f"g{n}"})) for n in range(len(owed))
        )

    async def run(self, blocker=None):
        scope = past_posts_deletion._Scope(
            "credential", "authorization", "in_scope", "cleaning", self.parts
        )
        return await past_posts_deletion._report(
            "engine", WS, RECEIPT, scope, [], blocker, JOB
        )


def test_the_recount_names_the_eleven_sql_categories_and_the_transit_pair_is_the_rest():
    assert set(past_posts_deletion._COUNTED) | {"transit", "transit_overdue"} == set(
        past_posts_deletion.CATEGORIES
    )
    assert len(past_posts_deletion._COUNTED) == 11


async def test_a_scope_completes_only_when_every_category_reads_zero(monkeypatch):
    recount = _Recount(monkeypatch, dict(NOTHING_LEFT))
    assert await recount.run() == "completed"
    ((state, results, reason, retry),) = recount.reports
    assert (state, reason, retry) == (
        "completed",
        None,
        past_posts_deletion.RETRY_AFTER_SECONDS,
    )
    assert results["outcome"] == "completed"
    assert all(results[c] == 0 for c in past_posts_deletion.CATEGORIES)


@pytest.mark.parametrize("category", past_posts_deletion._COUNTED)
async def test_one_category_still_owed_keeps_the_scope_cleaning(monkeypatch, category):
    recount = _Recount(monkeypatch, {**NOTHING_LEFT, category: 1})
    assert await recount.run(blocker="intent_live") == "intent_live"
    ((state, results, reason, retry),) = recount.reports
    assert (state, reason, results[category]) == ("cleaning", None, 1)
    assert retry == past_posts_deletion.RETRY_AFTER_SECONDS


@pytest.mark.parametrize("category", past_posts_deletion._COUNTED)
async def test_a_category_the_recount_lacks_fails_it_and_is_not_read_as_zero(
    monkeypatch, category
):
    """A statement that lost an alias must not report 'nothing left' for it, which
    is what completes a scope and tells the data subject their data is gone.
    Mutation that reddens: read a missing alias as 0 (`.get(category, 0)`)."""
    remaining = {c: n for c, n in NOTHING_LEFT.items() if c != category}
    recount = _Recount(monkeypatch, remaining)
    with pytest.raises(KeyError, match=category):
        await recount.run()
    assert recount.reports == [], "nothing was reported to the receipt"


async def test_transit_still_owed_counts_across_the_parts_and_sums_them(monkeypatch):
    recount = _Recount(monkeypatch, dict(NOTHING_LEFT), owed=((2, 0), (1, 0)))
    assert await recount.run() == "cleaning"
    ((state, results, reason, retry),) = recount.reports
    assert (state, results["transit"], results["transit_overdue"]) == ("cleaning", 3, 0)
    assert (reason, retry) == (None, past_posts_deletion.RETRY_AFTER_SECONDS)


async def test_an_overdue_upload_in_any_part_sends_the_receipt_to_review_slowly(
    monkeypatch,
):
    """The overdue part is not the last one: review follows the scope's total,
    not whatever the last part reported."""
    recount = _Recount(monkeypatch, dict(NOTHING_LEFT), owed=((3, 2), (1, 0)))
    assert await recount.run() == "cleaning"
    ((state, results, reason, retry),) = recount.reports
    assert (state, results["transit"], results["transit_overdue"]) == ("cleaning", 4, 2)
    assert (reason, retry) == (
        "transit_unresolved",
        past_posts_deletion.REVIEW_RETRY_AFTER_SECONDS,
    )
