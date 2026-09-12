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

    # The review resolutions (2026-09-12): the ledger's own resolve doors and
    # the restate-by-ref that reaches a card the tap already superseded.
    log.update({"retries": [], "posted": [], "cancels": [], "restates": []})

    async def resolve_retry(
        session, *, intent_id, workspace_id, ig_account_id, attempts_by_step
    ):
        log["retries"].append(
            (intent_id, workspace_id, ig_account_id, attempts_by_step)
        )
        return log.get("retry_ok", True)

    async def resolve_posted(session, *, intent_id):
        log["posted"].append(intent_id)
        return log.get("posted_ok", True)

    async def resolve_cancel(session, *, intent_id):
        log["cancels"].append(intent_id)
        return log.get("cancel_ok", True)

    async def _restate_everywhere(session, *, workspace_id, intent_id, outcome_text):
        log["restates"].append((workspace_id, intent_id, outcome_text))
        return 1

    # The latest `publish` op of the intent (None = Instagram was never asked)
    # and the human-verdict door that terminalizes it.
    log.update({"op": None, "verdicts": []})

    async def _latest_publish_op(session, intent_id):
        return log["op"]

    async def resolve_by_human(
        session, *, op_id, outcome, verdict, actor_user_id, from_state
    ):
        log["verdicts"].append((op_id, outcome, verdict, actor_user_id, from_state))

    monkeypatch.setattr(command_executors, "_latest_publish_op", _latest_publish_op)
    monkeypatch.setattr(
        command_executors.provider_ops, "resolve_by_human", resolve_by_human
    )

    monkeypatch.setattr(command_executors.publish_cap, "resolve_retry", resolve_retry)
    monkeypatch.setattr(command_executors.publish_cap, "resolve_posted", resolve_posted)
    monkeypatch.setattr(command_executors.publish_cap, "resolve_cancel", resolve_cancel)
    monkeypatch.setattr(command_executors, "_restate_everywhere", _restate_everywhere)
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

    async def test_a_review_card_does_not_take_approve(self, world):
        """`review_required → approved` is the review card's own edge
        (`resolve_review`, resolution `retry`); an `approve` on it — a stale
        approval card, a replayed tap — answers with the state, never flips."""
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


def _review(resolution: str, **extra) -> Command:
    return Command(
        kind="resolve_review",
        workspace_id="ws",
        actor_user_id="u1",
        channel="telegram",
        args={"intent_id": "i1", "resolution": resolution, **extra},
        actor_label="Chris",
    )


NOT_POSTED = {"verdict": "not_posted"}


@pytest.fixture
def parked(world):
    """A `review_required` row as the pipeline parks it after a publish call
    whose answer never settled: the container exists, the day is debited."""
    world["row"].update(
        {
            "state": "review_required",
            "publish_step": "publish_called",
            "ig_container_id": "c-1",
            "attempts_by_step": {"v": 1},
            "dry_run_mode": False,
            "is_paused": False,
        }
    )
    # The publish call's answer was lost: the reconciler's ladder ran out and
    # parked the intent with the op still `ambiguous` — the one origin where
    # "it posted" may be the truth.
    world["op"] = {"id": "op-9", "state": "ambiguous"}
    return world


class TestTheReviewCardIsTheTenantsToResolve:
    """`review_required` is resolved by the workspace, not the operator
    (ruling 2026-09-12, first principles for many tenants: the member is the
    human who can look at their own story). Three resolutions, each an audited
    edge of `02` §4: `retry` re-approves debit-neutral and mints the publish
    job; `posted` confirms what a publish call did; `cancel` gives up and
    keeps the debit. `failed` (a refund) stays the operator's."""

    async def test_retry_on_a_lost_answer_needs_the_members_verdict(self, parked):
        """The publish op is `ambiguous`: Instagram MAY have posted. A plain
        retry would re-permit a second publish call — the rail's one
        forbidden thing — so it is refused by name until the member says
        the story is not there."""
        with pytest.raises(commands.CommandRefused) as exc:
            await command_executors.resolve_review(_Session(), _review("retry"))
        assert exc.value.reason == "may_have_posted"
        assert (
            parked["retries"] == []
            and parked["jobs"] == []
            and parked["verdicts"] == []
        )

    async def test_retry_with_the_verdict_terminalizes_the_op_then_reapproves(
        self, parked
    ):
        out = await command_executors.resolve_review(
            _Session(), _review("retry", **NOT_POSTED)
        )
        assert out.outcome == "enqueued"
        assert parked["verdicts"] == [
            ("op-9", "failed", "not_posted", "u1", "ambiguous")
        ], "the human verdict ends the ambiguous op BEFORE a new generation exists"
        assert len(parked["retries"]) == 1 and len(parked["jobs"]) == 1

    async def test_retry_after_a_definitive_failure_needs_no_verdict(self, parked):
        parked["op"] = {"id": "op-9", "state": "failed"}
        out = await command_executors.resolve_review(_Session(), _review("retry"))
        assert out.outcome == "enqueued" and parked["verdicts"] == []
        parked["op"] = None  # poisoned before any publish call
        out = await command_executors.resolve_review(_Session(), _review("retry"))
        assert out.outcome == "enqueued" and parked["verdicts"] == []

    async def test_retry_reapproves_debit_neutral_and_mints_the_publish_job(
        self, parked
    ):
        parked["op"] = {"id": "op-9", "state": "failed"}
        out = await command_executors.resolve_review(_Session(), _review("retry"))
        assert out.outcome == "enqueued" and out.data["state"] == "approved"
        (intent_id, ws, acct, attempts) = parked["retries"][0]
        assert (intent_id, ws, acct) == ("i1", "ws", "a1")
        assert attempts["v"] == 1 and attempts["retries"] == 1, (
            "the retry is counted on the row, the version key kept"
        )
        assert parked["flips"] == [], "the ledger door flips, not the guard path"
        job = parked["jobs"][0]
        assert job["kind"] == "publish_pipeline"
        assert job["serialization_key"] == "ig:ref"
        assert job["deadline_seconds"] == jobs.NO_DEADLINE
        assert job["payload"] == {"v": 1, "intent_id": "i1", "dry_run": False}
        # The card the approve tap already superseded is reached BY REF.
        assert parked["supersedes"] == []
        assert len(parked["restates"]) == 1
        assert (
            "Approved" in parked["restates"][0][2]
            and "Chris" in parked["restates"][0][2]
        )

    async def test_a_second_retry_counts_up(self, parked):
        parked["row"]["attempts_by_step"] = {"v": 1, "retries": 2}
        await command_executors.resolve_review(
            _Session(), _review("retry", **NOT_POSTED)
        )
        assert parked["retries"][0][3]["retries"] == 3

    async def test_retry_keeps_approves_preconditions(self, parked):
        parked["row"]["api_publishing_enabled"] = False
        with pytest.raises(commands.CommandRefused) as exc:
            await command_executors.resolve_review(
                _Session(), _review("retry", **NOT_POSTED)
            )
        assert exc.value.reason == "manual_mode"
        parked["row"]["api_publishing_enabled"] = True
        parked["connected"] = False
        with pytest.raises(commands.CommandRefused) as exc:
            await command_executors.resolve_review(
                _Session(), _review("retry", **NOT_POSTED)
            )
        assert exc.value.reason == "not_connected"
        assert parked["retries"] == [] and parked["jobs"] == []
        assert parked["verdicts"] == [], "a refused retry leaves the op as it was"

    async def test_a_dry_run_workspace_retries_as_a_dry_run(self, parked):
        parked["connected"] = False
        parked["row"]["dry_run_mode"] = True
        out = await command_executors.resolve_review(
            _Session(), _review("retry", **NOT_POSTED)
        )
        assert out.data["dry_run"] is True
        assert parked["jobs"][0]["payload"]["dry_run"] is True

    async def test_a_lost_retry_race_is_an_illegal_transition(self, parked):
        parked["retry_ok"] = False
        with pytest.raises(commands.CommandRefused) as exc:
            await command_executors.resolve_review(
                _Session(), _review("retry", **NOT_POSTED)
            )
        assert exc.value.reason == "illegal_transition"
        assert parked["jobs"] == [] and parked["restates"] == []

    async def test_posted_confirms_the_publish_call_with_every_posted_effect(
        self, parked
    ):
        session = _Session()
        out = await command_executors.resolve_review(session, _review("posted"))
        assert out.outcome == "executed" and out.data["state"] == "posted"
        assert out.data["published_via"] == "api"
        assert parked["posted"] == ["i1"]
        sql = " ".join(s for s, _ in session.statements)
        assert "times_posted = times_posted + 1" in sql
        assert "'recent'" in sql and "last_posted_at = now()" in sql
        assert "Posted" in parked["restates"][0][2]
        assert parked["verdicts"] == [
            ("op-9", "succeeded", "posted", "u1", "ambiguous")
        ], "the human verdict ends the ambiguous op so it can retire"

    async def test_posted_needs_a_publish_call_to_confirm(self, parked):
        """Poison before the publish rung means Instagram was never asked:
        there is nothing to confirm, and the member is told which two levers
        remain."""
        parked["row"]["publish_step"] = "container_ready"
        parked["op"] = None
        with pytest.raises(commands.CommandRefused) as exc:
            await command_executors.resolve_review(_Session(), _review("posted"))
        assert exc.value.reason == "nothing_to_confirm"
        assert parked["posted"] == [] and parked["restates"] == []

    async def test_posted_is_refused_when_instagram_answered_no(self, parked):
        """`publish_step` stays `publish_called` after a publish Meta
        definitively refused (the permit resolved `failed`); the step alone
        would let a member confirm a story that never landed."""
        parked["op"] = {"id": "op-9", "state": "failed"}
        with pytest.raises(commands.CommandRefused) as exc:
            await command_executors.resolve_review(_Session(), _review("posted"))
        assert exc.value.reason == "nothing_to_confirm"
        assert parked["posted"] == [] and parked["verdicts"] == []

    async def test_cancel_gives_up_and_keeps_the_debit(self, parked):
        out = await command_executors.resolve_review(_Session(), _review("cancel"))
        assert out.outcome == "executed" and out.data["state"] == "cancelled"
        assert parked["cancels"] == ["i1"]
        assert "Cancelled" in parked["restates"][0][2]
        assert parked["verdicts"] == [
            ("op-9", "failed", "given_up", "u1", "ambiguous")
        ], "giving up ends the ambiguous op too — nothing may stay un-retirable"

    async def test_the_plain_cancel_command_on_a_parked_row_is_the_give_up(
        self, parked
    ):
        """`cancel` used to flag a `review_required` row and nothing ever
        finished it (the worker terminalizes at its checkpoints; a parked row
        has none) — the card kept its buttons under a *Cancelling* badge.
        It now takes the give-up edge outright."""
        out = await command_executors.cancel(_Session(), _cmd("cancel"))
        assert out.outcome == "executed" and out.data["state"] == "cancelled"
        assert parked["cancels"] == ["i1"] and parked["supersedes"] == []
        assert "Cancelled" in parked["restates"][0][2]

    async def test_cancel_is_honoured_even_when_a_cancel_was_already_asked_for(
        self, parked
    ):
        parked["row"]["cancel_requested"] = True
        out = await command_executors.resolve_review(_Session(), _review("cancel"))
        assert out.data["state"] == "cancelled"
        with pytest.raises(commands.CommandRefused) as exc:
            await command_executors.resolve_review(_Session(), _review("retry"))
        assert exc.value.reason == "cancelling"

    @pytest.mark.parametrize(
        "state", ["approved", "publishing", "posted", "awaiting_approval"]
    )
    async def test_any_other_state_answers_and_writes_nothing(self, parked, state):
        parked["row"]["state"] = state
        out = await command_executors.resolve_review(_Session(), _review("retry"))
        assert out.outcome == "answered" and out.data["state"] == state
        assert parked["retries"] == [] and parked["jobs"] == []

    async def test_an_unknown_resolution_is_refused_before_any_read(self, parked):
        with pytest.raises(commands.CommandRefused) as exc:
            await command_executors.resolve_review(_Session(), _review("failed"))
        assert exc.value.reason == "invalid_args"
        assert (
            parked["retries"] == []
            and parked["posted"] == []
            and parked["cancels"] == []
        )

    def test_the_floor_is_the_members_like_every_other_intent_command(self):
        assert commands.ROLE_FLOOR["resolve_review"] == "member"
        assert commands.REGISTRY["resolve_review"] is command_executors.resolve_review
        assert {"nothing_to_confirm", "may_have_posted"} <= set(commands.REASONS)
