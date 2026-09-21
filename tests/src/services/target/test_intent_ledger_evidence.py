"""`intent_ledger.EVIDENCE_MERGE` — the `last_error->'evidence'` MERGE.

Four writers stamp a key into `evidence`: the pipeline's two poison latches
and the reconciler's notify-attempt and customer-notified claims. The clause
is shared and the guards are not, so what a test can hold still is the
rendered clause — and it must MERGE. `evidence` carries `checks`,
`last_checked_at` and the trail, which is the operator's entire inheritance on
a parked intent; a `jsonb_build_object` rebuild would notify the customer by
destroying the evidence.
"""

from __future__ import annotations

import pytest

from src.services.target.intent_ledger import EVIDENCE_MERGE

PIPELINE_LATCH = (
    "last_error = COALESCE(last_error, CAST('{}' AS jsonb))"
    " || jsonb_build_object('evidence',"
    "      COALESCE(last_error->'evidence', CAST('{}' AS jsonb))"
    "      || jsonb_build_object('customer_notified', true))"
)
RECONCILER_ATTEMPT = (
    "last_error = COALESCE(last_error, CAST('{\"v\": 1}' AS jsonb))"
    " || jsonb_build_object('evidence',"
    "      COALESCE(last_error->'evidence', CAST('{}' AS jsonb))"
    "      || jsonb_build_object('notify_attempted_at', now()))"
)


@pytest.mark.unit
def test_the_pipelines_seedless_latch_renders_exactly():
    assert (
        EVIDENCE_MERGE.format(seed="{}", key="customer_notified", value="true")
        == PIPELINE_LATCH
    )


@pytest.mark.unit
def test_the_reconcilers_v1_seed_renders_exactly():
    assert (
        EVIDENCE_MERGE.format(seed='{"v": 1}', key="notify_attempted_at", value="now()")
        == RECONCILER_ATTEMPT
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "seed,key,value",
    [
        ("{}", "customer_notified", "true"),
        ('{"v": 1}', "customer_notified", "true"),
        ('{"v": 1}', "notify_attempted_at", "now()"),
    ],
    ids=["pipeline-latch", "reconciler-claim", "reconciler-attempt"],
)
def test_every_rendering_merges_rather_than_rebuilds(seed, key, value):
    rendered = EVIDENCE_MERGE.format(seed=seed, key=key, value=value)
    assert "COALESCE(last_error->'evidence'" in rendered, (
        "the existing evidence object is the left side of the ||"
    )
    assert rendered.count("||") == 2, "one merge into last_error, one into evidence"
    assert "SET" not in rendered and "WHERE" not in rendered, (
        "a clause, not a statement: the guards and RETURNING are the writer's"
    )


@pytest.mark.unit
def test_the_seed_is_what_an_absent_last_error_becomes():
    assert "CAST('{}' AS jsonb))" in EVIDENCE_MERGE.format(
        seed="{}", key="k", value="true"
    )
    assert "CAST('{\"v\": 1}' AS jsonb)" in EVIDENCE_MERGE.format(
        seed='{"v": 1}', key="k", value="true"
    )
