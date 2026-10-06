"""`workspaces.change_settings` — the boundary a `settings_change` crosses.

Unit tier, no database. Each TTL setting takes an inclusive range
(`workspaces.SETTINGS_RANGES`): a value outside it is refused by name before
anything is written, a value at either end is written as given, and NULL still
means the deployment's default.
"""

from __future__ import annotations

import pytest

from src.services.target.workspaces import InvalidWorkspaceArgs, change_settings

WS = "33333333-3333-3333-3333-333333333333"

#: `(setting, minimum, maximum)` — the inclusive range each TTL setting takes.
TTL_RANGES = [
    ("repost_ttl_days", 1, 365),
    ("skip_ttl_days", 1, 365),
    ("approval_ttl_minutes", 1, 365 * 24 * 60),
]

#: Each setting one step outside either end of its range, as
#: `(setting, value, minimum, maximum)`.
TTL_OUTSIDE = [
    (key, value, low, high)
    for key, low, high in TTL_RANGES
    for value in (low - 1, high + 1)
]

#: Each setting at either end of its range, as `(setting, value)`.
TTL_EDGES = [(key, value) for key, low, high in TTL_RANGES for value in (low, high)]


class _Recorder:
    """Records each statement; a settings UPDATE reads no result."""

    def __init__(self):
        self.statements = []

    async def execute(self, statement, params=None):
        self.statements.append((str(statement), params))


@pytest.fixture
def executor():
    return _Recorder()


class TestTheTtlSettingsAreBounded:
    @pytest.mark.parametrize("key, value, low, high", TTL_OUTSIDE)
    async def test_a_value_outside_its_range_is_refused_and_nothing_is_written(
        self, executor, key, value, low, high
    ):
        with pytest.raises(InvalidWorkspaceArgs) as info:
            await change_settings(executor, workspace_id=WS, changes={key: value})
        assert str(info.value) == f"{key} must be {low} to {high}"
        assert executor.statements == []

    @pytest.mark.parametrize("key, value", TTL_EDGES)
    async def test_a_value_at_either_end_of_its_range_is_written_as_given(
        self, executor, key, value
    ):
        cleaned = await change_settings(executor, workspace_id=WS, changes={key: value})
        assert cleaned == {key: value}
        assert executor.statements == [
            (
                f"UPDATE workspaces SET {key} = :{key} WHERE id = :ws",
                {key: value, "ws": WS},
            )
        ]

    async def test_null_still_means_the_deployment_default(self, executor):
        nulls = {key: None for key, _, _ in TTL_RANGES}
        cleaned = await change_settings(executor, workspace_id=WS, changes=nulls)
        assert cleaned == nulls
        ((_, params),) = executor.statements
        assert params == {**nulls, "ws": WS}


class TestAnOrdinarySettingsWriteIsUnchanged:
    async def test_every_setting_at_an_ordinary_value_is_written_as_given(
        self, executor
    ):
        changes = {
            "tz": "Europe/London",
            "posts_per_day": 3,
            "posting_hours_start": 14,
            "posting_hours_end": 2,
            "approval_mode": "manual",
            "auto_reapprove_returning": False,
            "approval_ttl_minutes": 1440,
            "dry_run_mode": True,
            "repost_ttl_days": 30,
            "skip_ttl_days": 45,
            "caption_style": "simple",
            "api_publishing_enabled": False,
        }
        cleaned = await change_settings(executor, workspace_id=WS, changes=changes)
        assert cleaned == changes
        ((sql, params),) = executor.statements
        assert sql.startswith("UPDATE workspaces SET ")
        assert sql.endswith(" WHERE id = :ws")
        assert params == {**changes, "ws": WS}
