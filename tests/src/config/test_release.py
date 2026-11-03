"""The deployment's commit, as the API and the worker both name it."""

from __future__ import annotations

import pytest

from src.config import release


@pytest.mark.parametrize(
    "env,expected",
    [
        ({release.COMMIT_VAR: "abc1234def5678abc1234def5678abc1234def56"}, "abc1234"),
        ({release.COMMIT_VAR: "  abc1234  "}, "abc1234"),
        ({release.COMMIT_VAR: ""}, None),
        ({release.COMMIT_VAR: "   "}, None),
        ({}, None),
    ],
    ids=["full-sha", "padded", "empty", "blank", "unset"],
)
def test_the_short_commit_is_seven_characters_or_none(env, expected):
    assert release.short_commit(env) == expected


def test_it_reads_railways_commit_variable():
    # The API's /health and the worker's claimer name read the same variable,
    # so a split between them is a real difference in what each one runs.
    assert release.COMMIT_VAR == "RAILWAY_GIT_COMMIT_SHA"
