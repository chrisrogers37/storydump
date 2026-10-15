"""The 10,000-post archives through the real sync composition (opt-in).

`RUN_ONBOARDING_10K=1` selects it; see `conftest.py`. The same drive, spies and
checks as the default sizes (`tests/scripts/test_instagram_onboarding_budgets.py`),
against the manifest's 10,000 rows. What is particular to this size is the
listing ceiling: the scan stops at 10,000 posts exactly, so `partial_coverage`
is true although nothing was skipped (the manifest predeclares it, with its
reasons), and the destination's state still reads `ready` or `zero_eligible`.
"""

import pytest

from tests.scripts import test_instagram_onboarding_budgets as budgets

pytestmark = [pytest.mark.integration, pytest.mark.slow]

origin = budgets.origin
worker = budgets.worker


@pytest.mark.parametrize("kind", ["useful", "excluded", "carousel"])
async def test_the_ten_thousand_post_archive_stays_within_the_budgets(
    kind, worker, origin, sync_conn, monkeypatch
):
    report = await budgets.measure(
        kind,
        10_000,
        "account",
        worker=worker,
        origin=origin,
        sync_conn=sync_conn,
        monkeypatch=monkeypatch,
    )
    # The coverage-ceiling flag stays truthful at the ceiling.
    assert report["final"]["partial_coverage"] is True
    if kind in ("useful", "carousel"):
        # The first card does not wait for the archive: its requests are the
        # indexing that reaches the first candidate and the selected asset's own
        # fetch, inside ceil(0.1N/25)+16 Graph and four CDN, with the
        # candidate landing before a quarter of the parents are observed.
        ceiling = budgets.fx.expected(kind, 10_000)["first_card"]
        card = report["first_card"]
        assert card["graph"] <= ceiling["max_graph"]
        assert card["cdn"] <= ceiling["max_cdn"]
        assert card["observed"] <= ceiling["max_observed"]
