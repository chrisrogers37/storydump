"""The way in and the way back, as the dashboard sees them (#1127, `06` §1).

`offboard_workspace` and `restore_workspace` are owner-only commands with
executors (`command_executors.py`) that nothing could issue until the dashboard
offered them. Two facts the UI needs are pinned here rather than restated in
TypeScript: the restore deadline is `offboarding_at` plus the ONE grace
constant, and the workspace read carries that deadline so the screen never
computes it from a copied number.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest

from src.services.target import intent_ledger, offboarding, workspaces

OFFBOARDED_AT = datetime(2026, 9, 2, 15, 0, tzinfo=timezone.utc)


class TestRestorableUntil:
    def test_it_is_offboarding_at_plus_the_grace_window(self):
        assert offboarding.restorable_until(OFFBOARDED_AT) == OFFBOARDED_AT + timedelta(
            seconds=offboarding.GRACE_SECONDS_DEFAULT
        )

    def test_the_grace_window_is_thirty_days_and_this_is_its_one_home(self):
        """The finalizer, `restore_workspace` and the dashboard's deadline all
        read this constant. A second copy — in TypeScript, say — is one that
        can disagree about when the window closed."""
        assert offboarding.GRACE_SECONDS_DEFAULT == 30 * 24 * 3600
        assert offboarding.restorable_until(OFFBOARDED_AT) - OFFBOARDED_AT == timedelta(
            days=30
        )

    def test_a_workspace_that_is_not_offboarding_has_no_deadline(self):
        assert offboarding.restorable_until(None) is None


class _Result:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


class _Executor:
    def __init__(self, row):
        self._row = row

    async def execute(self, statement, params=None):
        return _Result(self._row)


class _DrainSession:
    """`drain`'s session double: its SELECT answers with *ids*, and a
    savepoint is offered."""

    def __init__(self, ids):
        self._ids = ids

    def begin_nested(self):
        @asynccontextmanager
        async def _savepoint():
            yield self

        return _savepoint()

    async def execute(self, statement, params=None):
        ids = self._ids

        class _Rows:
            def all(self):
                return [(i,) for i in ids]

        return _Rows()


class TestTheDrainDoesNotSwallowAnIntentItCannotSee:
    async def test_IntentNotVisible_escapes_the_drain(self, monkeypatch):
        """#1423: the drain catches a refusal and goes on to the next intent.
        `IntentNotVisible` is a caller's bug, not a refusal, and must fail the
        job instead; as a subclass of `IntentTransitionRefused` it would be
        swallowed here."""

        async def transition(session, intent_id, to_state):
            raise intent_ledger.IntentNotVisible("matched no row")

        monkeypatch.setattr(offboarding.intent_ledger, "transition", transition)
        with pytest.raises(intent_ledger.IntentNotVisible):
            await offboarding.drain(_DrainSession(["i-1"]), "ws-1", limit=5)


class TestTheDrainBound:
    """#1441: a run makes at most 64 cancels, and a refusal does not count
    (its savepoint rolls back and holds no subtransaction id)."""

    @pytest.fixture(autouse=True)
    def _transition(self, monkeypatch):
        refuse = set()

        async def transition(session, intent_id, to_state):
            if intent_id in refuse:
                raise intent_ledger.IntentTransitionRefused("illegal edge")

        monkeypatch.setattr(offboarding.intent_ledger, "transition", transition)
        return refuse

    async def test_exactly_64_leaves_nothing_more(self):
        ids = [f"i-{n}" for n in range(64)]
        out = await offboarding.drain(_DrainSession(ids), "ws-1", limit=500)
        assert (out["cancelled"], out["more"]) == (64, False)

    async def test_the_65th_is_left_for_the_next_run(self):
        ids = [f"i-{n}" for n in range(65)]
        out = await offboarding.drain(_DrainSession(ids), "ws-1", limit=500)
        assert (out["cancelled"], out["more"]) == (64, True)

    async def test_refusals_do_not_count(self, _transition):
        refused = [f"r-{n}" for n in range(10)]
        _transition.update(refused)
        ids = refused + [f"i-{n}" for n in range(64)]
        out = await offboarding.drain(_DrainSession(ids), "ws-1", limit=500)
        assert (out["cancelled"], out["refused"], out["more"]) == (64, 10, False)
        assert [r["intent_id"] for r in out["refusals"]] == refused

    async def test_a_read_that_fills_its_limit_says_more(self):
        ids = [f"i-{n}" for n in range(5)]
        out = await offboarding.drain(_DrainSession(ids), "ws-1", limit=5)
        assert (out["cancelled"], out["more"]) == (5, True)


class TestTheWorkspaceReadCarriesTheDeadline:
    async def test_an_offboarding_workspace_reports_when_it_can_last_be_restored(self):
        row = await workspaces.get_workspace(
            _Executor(
                {"id": "ws", "state": "offboarding", "offboarding_at": OFFBOARDED_AT}
            ),
            workspace_id="ws",
        )
        assert row["restorable_until"] == OFFBOARDED_AT + timedelta(days=30)

    async def test_an_active_workspace_reports_none_not_a_date(self):
        row = await workspaces.get_workspace(
            _Executor({"id": "ws", "state": "active", "offboarding_at": None}),
            workspace_id="ws",
        )
        assert row["restorable_until"] is None

    async def test_a_missing_workspace_is_still_none(self):
        assert (
            await workspaces.get_workspace(_Executor(None), workspace_id="ws") is None
        )
