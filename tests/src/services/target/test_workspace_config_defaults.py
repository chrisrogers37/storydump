"""The config payload carries the fallbacks the dashboard would otherwise retype.

`repost_ttl_days`, `skip_ttl_days` and `approval_ttl_minutes` are the columns
`053` declares NULL on purpose, so "the deployment's fallback applies" stays
distinguishable from "the owner chose this number" (`config/defaults.py`).
The settings cards need the fallback to SHOW it — and before #1366 they got it
by writing `?? 30` and `?? 45` into TSX, a second copy that drifted the moment
the worker's own copy did (#1365: the card said 30 while a worker publish
locked for 7).

This is the same argument, and the same answer, as `restorable_until` (#1127):
the server computes it "so the dashboard never derives it from a copied
number". `landing/src/lib/settings-defaults.test.ts` is the other half — it
asserts the cards read these and hold no literal of their own.
"""

from __future__ import annotations

import asyncio

from src.config.defaults import DEFAULT_REPOST_TTL_DAYS, DEFAULT_SKIP_TTL_DAYS
from src.services.target import workspaces


class _Mappings:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _Result:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return _Mappings(self._row)


class _Executor:
    """One row back, whatever is asked — this is about what `get_workspace`
    ADDS to the row, not about the query."""

    def __init__(self, row):
        self._row = row

    async def execute(self, *_args, **_kwargs):
        return _Result(self._row)


def _get(row):
    return asyncio.run(workspaces.get_workspace(_Executor(row), workspace_id="ws-1"))


class TestTheConfigCarriesItsFallbacks:
    def test_the_payload_names_the_ttl_fallbacks(self):
        got = _get({"id": "ws-1", "repost_ttl_days": None, "skip_ttl_days": None})
        assert got["defaults"] == {
            "repost_ttl_days": DEFAULT_REPOST_TTL_DAYS,
            "skip_ttl_days": DEFAULT_SKIP_TTL_DAYS,
        }

    def test_they_are_the_fallbacks_and_not_the_row(self):
        """The defaults describe the DEPLOYMENT, not this workspace.

        A workspace that HAS set both still carries them, because the card
        needs to say what leaving the field alone would mean — and because a
        payload whose shape depends on the row is one the client must branch
        on."""
        got = _get({"id": "ws-1", "repost_ttl_days": 3, "skip_ttl_days": 4})
        assert got["repost_ttl_days"] == 3 and got["skip_ttl_days"] == 4
        assert got["defaults"]["repost_ttl_days"] == DEFAULT_REPOST_TTL_DAYS
        assert got["defaults"]["skip_ttl_days"] == DEFAULT_SKIP_TTL_DAYS

    def test_a_missing_workspace_is_still_None(self):
        assert _get(None) is None
