"""The intent executors read, then decide (F2 (a), phase 1 step 6).

A row in any state other than `awaiting_approval` ANSWERS with its current
state and writes nothing — a repeat tap, a tap on a finished card, and the
operator-only `review_required` edge all end here, never at the guard. Only
a row that was `awaiting_approval` at read time flips, and every flip
supersedes the intent's cards in every binding with the outcome line."""

from __future__ import annotations

import pytest

from src.services.target import commands  # noqa: I001 — the port first: the registry cycle
from src.services.target import command_executors, intent_ledger, jobs
from src.services.target.commands import Command

ROW = {
    "id": "i1",
    "workspace_id": "ws",
    "state": "awaiting_approval",
    "cancel_requested": False,
    "media_item_id": "m1",
    "ig_account_id": "a1",
    "provider_account_ref": "ref",
    "handle": "brand",
    "api_publishing_enabled": True,
    "repost_ttl_days": 30,
    "skip_ttl_days": 7,
    "eff_ppd": 3,
    "eff_tz": "UTC",
}


def _cmd(kind: str) -> Command:
    return Command(
        kind=kind,
        workspace_id="ws",
        actor_user_id="u1",
        channel="telegram",
        args={"intent_id": "i1"},
    )


class _Session:
    """Only the executors' side statements land here; the decision is made
    on the row `_intent_row` returned."""

    def __init__(self):
        self.statements = []

    async def execute(self, statement, params=None):
        self.statements.append((str(statement), params))

        class _R:
            rowcount = 1

            def first(self):
                return None

            def fetchone(self):
                return None

        return _R()


@pytest.fixture
def world(monkeypatch):
    """Every seam the executors reach through, recorded."""
    log = {"flips": [], "jobs": [], "supersedes": [], "row": dict(ROW)}

    async def _intent_row(session, command):
        # The publish precondition rides the row (#1286): a usable token.
        return {**log["row"], "has_ig_credential": log.get("connected", True)}

    async def transition(session, intent_id, to_state):
        log["flips"].append((intent_id, to_state))

    async def enqueue(session, **kw):
        log["jobs"].append(kw)
        return "job-1"

    async def _settlement(session, *, workspace_id, intent_id):
        from datetime import datetime, timezone

        return {
            "state": log["row"]["state"],
            "by": "Chris",
            "at": datetime(2026, 9, 9, 14, 14, tzinfo=timezone.utc),
        }

    async def _supersede_everywhere(session, *, workspace_id, intent_id, outcome_text):
        log["supersedes"].append((workspace_id, intent_id, outcome_text))
        return 1

    async def _actor_name(session, user_id, **kw):
        return "Chris"

    monkeypatch.setattr(command_executors, "_intent_row", _intent_row)
    monkeypatch.setattr(intent_ledger, "transition", transition)
    monkeypatch.setattr(jobs, "enqueue", enqueue)
    monkeypatch.setattr(command_executors, "_settlement", _settlement)
    monkeypatch.setattr(
        command_executors, "_supersede_everywhere", _supersede_everywhere
    )
    monkeypatch.setattr(command_executors, "_actor_name", _actor_name)
    return log


class TestAnAlreadyDecidedCardAnswers:
    @pytest.mark.parametrize(
        "state",
        [
            "approved",
            "publishing",
            "posted",
            "skipped",
            "rejected",
            "expired",
            "cancelled",
            "failed",
            "review_required",
            "scheduled",
            "prompt_pending",
        ],
    )
    @pytest.mark.parametrize("kind", ["approve", "skip", "reject"])
    async def test_any_state_but_awaiting_approval_answers_and_writes_nothing(
        self, world, state, kind
    ):
        world["row"]["state"] = state
        result = await getattr(command_executors, kind)(_Session(), _cmd(kind))
        assert result.outcome == "answered"
        assert result.data["state"] == state
        assert result.data["settled_by"] == "Chris"
        assert world["flips"] == [] and world["jobs"] == []

    async def test_an_answered_card_is_superseded_so_a_stale_card_heals(self, world):
        world["row"]["state"] = "posted"
        await command_executors.skip(_Session(), _cmd("skip"))
        assert len(world["supersedes"]) == 1
        (ws, intent, outcome) = world["supersedes"][0]
        assert (ws, intent) == ("ws", "i1") and "Posted" in outcome

    async def test_a_card_in_transit_answers_without_touching_its_buttons(self, world):
        """`approved`/`publishing`/`review_required` are not final: the card
        keeps its buttons (a tap answers) and the settled-card sweep writes the
        terminal line when the intent ends."""
        world["row"]["state"] = "publishing"
        result = await command_executors.skip(_Session(), _cmd("skip"))
        assert result.outcome == "answered" and world["supersedes"] == []

    async def test_the_operator_edge_is_not_reachable_from_a_members_tap(self, world):
        """`review_required → approved` is seeded as the operator's edge; a
        member's `approve` must answer, not take it."""
        world["row"]["state"] = "review_required"
        result = await command_executors.approve(_Session(), _cmd("approve"))
        assert result.outcome == "answered" and world["flips"] == []


class TestAnAwaitingCardFlipsOnceAndSupersedesEverywhere:
    async def test_approve_flips_enqueues_and_supersedes(self, world):
        result = await command_executors.approve(_Session(), _cmd("approve"))
        assert result.outcome == "enqueued"
        assert world["flips"] == [("i1", "approved")]
        assert world["jobs"][0]["kind"] == "publish_pipeline"
        assert len(world["supersedes"]) == 1
        assert "Approved" in world["supersedes"][0][2]
        assert "Chris" in world["supersedes"][0][2]

    async def test_skip_and_reject_supersede_with_their_own_line(self, world):
        await command_executors.skip(_Session(), _cmd("skip"))
        assert "Skipped" in world["supersedes"][-1][2]
        await command_executors.reject(_Session(), _cmd("reject"))
        assert "Rejected" in world["supersedes"][-1][2]

    async def test_a_cancelling_card_is_refused_by_name(self, world):
        world["row"]["cancel_requested"] = True
        with pytest.raises(commands.CommandRefused) as info:
            await command_executors.approve(_Session(), _cmd("approve"))
        assert info.value.reason == "cancelling"
        assert world["flips"] == []

    async def test_manual_mode_refuses_post_before_any_write(self, world):
        world["row"]["api_publishing_enabled"] = False
        with pytest.raises(commands.CommandRefused) as info:
            await command_executors.approve(_Session(), _cmd("approve"))
        assert info.value.reason == "manual_mode" and world["flips"] == []

    async def test_a_disconnected_account_refuses_post_before_any_write(self, world):
        world["connected"] = False
        with pytest.raises(commands.CommandRefused) as info:
            await command_executors.approve(_Session(), _cmd("approve"))
        assert info.value.reason == "not_connected" and world["flips"] == []


class TestDryRunAndPauseAtApprove:
    """Settings › General (owner, 2026-09-10): a dry run needs no Instagram
    token, and the tap's answer says what happens next."""

    @pytest.mark.asyncio
    async def test_a_dry_run_approve_needs_no_credential_and_says_so(self, world):
        world["connected"] = False
        world["row"]["dry_run_mode"] = True
        out = await command_executors.approve(_Session(), _cmd("approve"))
        assert out.outcome == "enqueued" and out.data["dry_run"] is True
        assert world["jobs"][0]["payload"]["dry_run"] is True, (
            "the decision travels with the job, not the live flag"
        )

    @pytest.mark.asyncio
    async def test_a_paused_workspace_still_approves_and_says_it_waits(self, world):
        world["row"]["is_paused"] = True
        out = await command_executors.approve(_Session(), _cmd("approve"))
        assert out.outcome == "enqueued" and out.data["paused"] is True
