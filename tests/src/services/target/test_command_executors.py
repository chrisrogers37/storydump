"""The intent executors read, then decide.

A row in any state other than `awaiting_approval` ANSWERS with its current
state and writes nothing — a repeat tap, a tap on a finished card, and the
operator-only `review_required` edge all end here, never at the guard. Only
a row that was `awaiting_approval` at read time flips, and every flip
supersedes the intent's cards in every binding with the outcome line."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from src.services.target import commands  # noqa: I001 — the port first: the registry cycle
from src.services.target import (
    command_executors,
    intent_ledger,
    jobs,
    oauth_states,
    workspaces,
)
from src.services.target.commands import Command
from tests.src.services.target.test_provisioning import _ScriptedExecutor

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
        return {
            **log["row"],
            "has_ig_credential": log.get("connected", True),
            "_suitability_context": None,
        }

    async def transition(session, intent_id, to_state):
        log["flips"].append((intent_id, to_state))

    async def enqueue(session, **kw):
        log["jobs"].append(kw)
        return "job-1"

    async def _settlement(session, *, workspace_id, intent_id):
        from datetime import datetime, timezone

        return {
            "state": log["row"]["state"],
            "by": log.get("by", "Sam"),
            "approval_rule": log.get("rule"),
            "at": datetime(2026, 9, 9, 14, 14, tzinfo=timezone.utc),
        }

    async def _supersede_everywhere(session, *, workspace_id, intent_id, outcome_text):
        log["supersedes"].append((workspace_id, intent_id, outcome_text))
        return 1

    async def _actor_name(session, user_id, **kw):
        return "Sam"

    # The review resolutions: the ledger's own resolve doors and
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


class TestARepeatTapNamesTheMechanism:
    """#1477: a tap on a card already decided answers with the card's own
    line, which names the rule that approved the story when no person moved
    it; a person who did (an "it posted", a retry) keeps their name."""

    @pytest.mark.parametrize(
        "by, rule, named",
        [(None, "auto", "YOLO"), ("Sam", "auto", "Sam"), ("Sam", None, "Sam")],
    )
    async def test_the_answer_names_the_rule_only_when_no_person_moved_it(
        self, world, by, rule, named
    ):
        world["row"]["state"] = "posted"
        world["by"], world["rule"] = by, rule
        result = await command_executors.approve(_Session(), _cmd("approve"))
        assert result.outcome == "answered"
        assert f"✅ Posted by {named} · " in result.data["outcome_text"]

    async def test_the_settlement_carries_the_rule_beside_the_person(self, monkeypatch):
        at = datetime(2026, 10, 12, 14, 1, tzinfo=timezone.utc)

        async def settlement(session, *, workspace_id, intent_id):
            return {
                "state": "posted",
                "published_via": "api",
                "approval_rule": "auto",
                "by_user_id": None,
                "at": at,
            }

        monkeypatch.setattr(intent_ledger, "settlement", settlement)
        found = await command_executors._settlement(
            _Session(), workspace_id="ws", intent_id="i"
        )
        assert found == {
            "state": "posted",
            "published_via": "api",
            "approval_rule": "auto",
            "by": None,
            "at": at,
        }


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
        assert result.data["settled_by"] == "Sam"
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
        assert "Sam" in world["supersedes"][0][2]

    async def test_skip_and_reject_supersede_with_their_own_line(self, world):
        await command_executors.skip(_Session(), _cmd("skip"))
        assert "Skipped" in world["supersedes"][-1][2]
        await command_executors.reject(_Session(), _cmd("reject"))
        assert "Rejected" in world["supersedes"][-1][2]
        assert "series" not in world["supersedes"][-1][2], "this story repeats not"

    async def test_a_repeating_storys_reject_says_its_series_ended(self, world):
        """Reject ends a series (the successor trigger leaves `rejected` out),
        while the card's header still says how it repeats (#485)."""
        world["row"]["repeat_unit"] = "day"
        await command_executors.reject(_Session(), _cmd("reject"))
        line = world["supersedes"][-1][2]
        assert "Rejected" in line and line.endswith(" · the series ended"), line

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
    """Settings › General: a dry run needs no Instagram token, and the tap's
    answer says what happens next."""

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
        actor_label="Sam",
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
    """`review_required` is resolved by the workspace, not the operator (the
    member is the human who can look at their own story). Three resolutions,
    each an audited edge: `retry` re-approves debit-neutral and
    mints the publish job; `posted` confirms what a publish call did; `cancel`
    gives up and keeps the debit. `failed` (a refund) stays the operator's."""

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

    async def test_retry_starts_a_fresh_float_with_zeroed_counters(self, parked):
        """The float: a story parked after six fetch waits must
        not re-park on its first refusal, and the next notice must not report
        a float that never ran — "post again" zeroes the class counters and
        the float's start, keeping only the retry count."""
        parked["op"] = {"id": "op-9", "state": "failed"}
        parked["row"]["attempts_by_step"] = {
            "v": 1,
            "retries": 1,
            "fetch_refusals": 28,
            "fetch_waits": 6,
            "container_gone": 1,
            "busy_waits": 2,
            "float_since": "2026-09-14T00:00:00+00:00",
        }
        out = await command_executors.resolve_review(_Session(), _review("retry"))
        assert out.outcome == "enqueued"
        (_intent_id, _ws, _acct, attempts) = parked["retries"][0]
        assert attempts == {"v": 1, "retries": 2}

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
            "Approved" in parked["restates"][0][2] and "Sam" in parked["restates"][0][2]
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
        """`cancel` on a `review_required` row takes the give-up edge outright:
        a flag alone would never be finished (the worker terminalizes at its
        checkpoints; a parked row has none), and the card would keep its
        buttons under a *Cancelling* badge."""
        out = await command_executors.cancel(_Session(), _cmd("cancel"))
        assert out.outcome == "executed" and out.data["state"] == "cancelled"
        assert parked["cancels"] == ["i1"] and parked["supersedes"] == []
        assert "Cancelled" in parked["restates"][0][2]

    @pytest.mark.parametrize("stop", [True, False], ids=["stop_repeat", "plain"])
    async def test_a_parked_series_give_up_says_whether_it_stopped(self, parked, stop):
        """The give-up's line says the series stopped, as the flag's does
        (#485). A plain give-up ends this story alone."""
        parked["row"]["repeat_unit"] = "day"
        args = {"intent_id": "i1", **({"stop_repeat": True} if stop else {})}
        command = Command(
            kind="cancel",
            workspace_id="ws",
            actor_user_id="u1",
            channel="telegram",
            args=args,
        )
        out = await command_executors.cancel(_Session(), command)
        assert out.data["state"] == "cancelled"
        line = parked["restates"][0][2]
        if stop:
            assert line.endswith(" · the series stopped"), line
        else:
            assert "series" not in line, line

    @pytest.mark.parametrize("via", ["resolve_review", "cancel"])
    async def test_a_give_up_with_a_transit_copy_mints_the_job_that_destroys_it(
        self, parked, via
    ):
        """The API holds no transit credentials; the worker's pipeline job
        meets the intent `cancelled` and destroys the copy (instead of the
        sweep, up to two days later)."""
        parked["row"]["transit_asset_ref"] = "ws/x/t1"
        if via == "cancel":
            await command_executors.cancel(_Session(), _cmd("cancel"))
        else:
            await command_executors.resolve_review(_Session(), _review("cancel"))
        assert [j["kind"] for j in parked["jobs"]] == ["publish_pipeline"]
        job = parked["jobs"][0]
        assert job["payload"]["intent_id"] == "i1"
        assert job["serialization_key"] == "ig:ref"
        assert job["workspace_id"] == "ws"

    async def test_a_give_up_without_a_transit_copy_mints_nothing(self, parked):
        """Parked before the upload, or a dry run: nothing on Cloudinary."""
        parked["row"]["transit_asset_ref"] = None
        await command_executors.resolve_review(_Session(), _review("cancel"))
        assert parked["jobs"] == []

    async def test_a_lost_give_up_race_mints_nothing(self, parked):
        parked["row"]["transit_asset_ref"] = "ws/x/t1"
        parked["cancel_ok"] = False
        with pytest.raises(commands.CommandRefused):
            await command_executors.resolve_review(_Session(), _review("cancel"))
        assert parked["jobs"] == []

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


class TestRemovingAnAccountGivesUpItsParkedReviews:
    """`disable_account` flags an account's live intents; a parked row has no
    worker checkpoint to honour the flag at, so the removal IS its give-up:
    the row is cancelled (debit retained), its lost publish op ends by
    verdict, and its card loses the review buttons by ref."""

    @pytest.fixture
    def removed(self, parked, monkeypatch):
        parked["order"] = []

        async def remove_account_archive(session, *, workspace_id, ig_account_id):
            parked["order"].append(("archive", workspace_id, ig_account_id))

        async def disable_destination(session, *, workspace_id, ig_account_id):
            parked["order"].append(("destination", workspace_id, ig_account_id))
            return {"credential_revoked": True}

        monkeypatch.setattr(
            command_executors.archive_lifecycle,
            "remove_account_archive",
            remove_account_archive,
        )

        async def rows(executor, sql, **params):
            return [
                {"id": "i1", "state": "review_required", "eff_tz": "UTC"},
                {"id": "i2", "state": "approved", "eff_tz": "UTC"},
            ]

        monkeypatch.setattr(
            command_executors.provisioning, "disable_destination", disable_destination
        )
        monkeypatch.setattr(command_executors.readers, "rows", rows)
        return parked

    async def test_a_parked_row_is_cancelled_its_op_ended_and_its_card_restated(
        self, removed
    ):
        command = Command(
            kind="disable_account",
            workspace_id="ws",
            actor_user_id="admin",
            channel="web",
            args={"ig_account_id": "a1"},
        )
        out = await command_executors.disable_account(_Session(), command)
        assert out.outcome == "executed"
        assert removed["order"] == [
            ("archive", "ws", "a1"),
            ("destination", "ws", "a1"),
        ], "the archive (and the source prefix it takes) goes before the disable"
        assert removed["cancels"] == ["i1"], "the parked row takes the give-up edge"
        assert removed["verdicts"] == [
            ("op-9", "failed", "given_up", "admin", "ambiguous")
        ]
        assert [r[1] for r in removed["restates"]] == ["i1"]
        assert "Account disabled" in removed["restates"][0][2]
        # The approved row keeps the flag path: superseded live, not restated.
        assert [s[1] for s in removed["supersedes"]] == ["i2"]

    async def test_a_row_a_member_resolved_meanwhile_keeps_their_line(self, removed):
        removed["cancel_ok"] = False
        command = Command(
            kind="disable_account",
            workspace_id="ws",
            actor_user_id="admin",
            channel="web",
            args={"ig_account_id": "a1"},
        )
        await command_executors.disable_account(_Session(), command)
        assert removed["verdicts"] == [] and removed["restates"] == []


class TestTheDriveLinkDoorMintsItsTablesPurpose:
    """The chat door reads `commands.CONNECT_PURPOSE_KIND` too, so it mints the
    purpose its web legs and callbacks gate by."""

    @pytest.fixture
    def needs(self, monkeypatch):
        from src.services.target import google_drive_oauth

        holder = {"purpose": None}

        async def connect_purpose(session, *, workspace_id):
            return holder["purpose"]

        async def issue_state(session, **kw):
            return "st"

        monkeypatch.setattr(google_drive_oauth, "connect_purpose", connect_purpose)
        monkeypatch.setattr(oauth_states, "issue_state", issue_state)
        return holder

    @staticmethod
    def _command(kind):
        return Command(
            kind=kind, workspace_id="ws-1", actor_user_id="u-1", channel="web"
        )

    @pytest.mark.parametrize(
        "purpose,kind", list(commands.CONNECT_PURPOSE_KIND.items())
    )
    async def test_each_command_mints_the_purpose_the_table_pairs_it_with(
        self, needs, purpose, kind
    ):
        needs["purpose"] = purpose
        result = await getattr(command_executors, kind)(object(), self._command(kind))
        assert result.data["purpose"] == purpose

    @pytest.mark.parametrize(
        "purpose,kind",
        [("connect", "reconnect_account"), ("reconnect", "connect_account")],
    )
    async def test_the_other_commands_purpose_is_refused_by_name(
        self, needs, purpose, kind
    ):
        needs["purpose"] = purpose
        with pytest.raises(commands.CommandRefused) as info:
            await getattr(command_executors, kind)(object(), self._command(kind))
        assert info.value.reason == "illegal_transition"


class TestTheDriveLinkMintsItsVerifierWithTheState:
    """`connect_account` / `reconnect_account` mint the state the Drive
    callback consumes, and that callback redeems a code only with the PKCE
    verifier minted beside it (RFC 7636). So the state carries one,
    and the result carries its S256 challenge for whoever composes the URL —
    never the verifier, which stays on the row."""

    @pytest.fixture
    def minted(self, monkeypatch):
        seen = {"purpose": "connect", "issued": {}}

        async def connect_purpose(session, *, workspace_id):
            return seen["purpose"]

        async def issue_state(session, **kw):
            seen["issued"] = kw
            return "st-drive"

        monkeypatch.setattr(
            command_executors.google_drive_oauth, "connect_purpose", connect_purpose
        )
        monkeypatch.setattr(oauth_states, "issue_state", issue_state)
        return seen

    @pytest.mark.parametrize(
        "kind,purpose",
        [("connect_account", "connect"), ("reconnect_account", "reconnect")],
    )
    async def test_the_state_carries_a_verifier_and_the_result_its_challenge(
        self, minted, kind, purpose
    ):
        minted["purpose"] = purpose
        command = Command(
            kind=kind, workspace_id="ws", actor_user_id="admin", channel="web", args={}
        )
        out = await getattr(command_executors, kind)(_Session(), command)
        issued = minted["issued"]
        verifier = issued["code_verifier"]
        assert issued["purpose"] == purpose and issued["reconnect_target"] == "ws"
        assert out.outcome == "executed"
        assert out.data["state"] == "st-drive"
        assert out.data["code_challenge"] == oauth_states.code_challenge(verifier)
        assert verifier not in out.data.values()


# --- the item's Auto-approve flag (#1477) ---------------------------------------

ITEM = "7a1c3d5e-0000-4000-8000-00000000000b"


def _flag(**args) -> Command:
    """`set_item_auto_approve` on ITEM, as a member's tap sends it."""
    return Command(
        kind="set_item_auto_approve",
        workspace_id="ws",
        actor_user_id="u1",
        channel="telegram",
        args={"media_item_id": ITEM, **args},
    )


def _item_found(state: str = "available") -> _ScriptedExecutor:
    """The item is the workspace's: the write returns it, then the audit lands."""
    return _ScriptedExecutor((1, {"id": ITEM, "state": state}), (1, None))


class TestTheItemsAutoApproveFlag:
    """A member's standing Approve, at the SQL seam: one UPDATE of the
    workspace's own item, then one audit row naming the flag set or cleared,
    and the refusals decided before and after it. What only PostgreSQL can
    say (the tenant's policy, the login's grant, the removed item's skip) is
    the gate's: `tests/scripts/test_set_item_auto_approve_gate.py`."""

    @pytest.mark.parametrize("on", [True, False])
    async def test_sets_and_clears_it_with_an_audit_row(self, on):
        ex = _item_found()
        result = await command_executors.set_item_auto_approve(
            ex, _flag(auto_approve=on)
        )

        assert result.outcome == "executed"
        assert result.data == {"media_item_id": ITEM, "auto_approve": on}
        (sql, params), (audit_sql, audit) = ex.statements
        assert "UPDATE media_items SET auto_approve = :on" in sql
        assert "WHERE id = :m AND workspace_id = :ws" in sql
        assert "AND state <> 'removed'" in sql
        assert "RETURNING state" in sql
        assert params == {"on": on, "m": ITEM, "ws": "ws"}
        assert "INSERT INTO audit_events" in audit_sql
        assert audit["ws"] == "ws"
        assert audit["entity_kind"] == "media_item"
        assert audit["item"] == ITEM
        # A flag is not a state: the row carries the item's own on both sides.
        assert audit["from_state"] == audit["to_state"] == "available"
        assert json.loads(audit["detail"]) == {
            "v": 1,
            "event": "auto_approve",
            "on": on,
        }

    async def test_another_workspaces_item_is_not_found_and_not_audited(self):
        # Neither the write nor the read after its miss finds the item.
        ex = _ScriptedExecutor((0, None), (0, None))
        with pytest.raises(commands.CommandRefused) as refused:
            await command_executors.set_item_auto_approve(ex, _flag(auto_approve=True))
        assert refused.value.reason == "not_found"
        assert refused.value.facts == {"missing": "item"}
        (_, _), (read, params) = ex.statements
        assert "SELECT state FROM media_items" in read
        assert "WHERE id = :m AND workspace_id = :ws" in read
        assert params == {"m": ITEM, "ws": "ws"}

    async def test_a_removed_item_is_refused_and_not_audited(self):
        # The write skips a removed item; the read after its miss finds it.
        ex = _ScriptedExecutor((0, None), (1, {"state": "removed"}))
        with pytest.raises(commands.CommandRefused) as refused:
            await command_executors.set_item_auto_approve(ex, _flag(auto_approve=True))
        # The item's own state in the way, named as `schedule_item` names it.
        assert refused.value.reason == "locked"
        assert refused.value.facts == {
            "in_the_way": ["item_removed"],
            "overridable": False,
        }
        assert len(ex.statements) == 2, "the write and the read: no audit row"

    @pytest.mark.parametrize("state", ["unsupported", "missing"])
    async def test_only_a_removed_item_is_refused(self, state):
        # Only a removed item's card loses Configure (the keyboard rule), so
        # an item that cannot post today still takes the flag.
        ex = _item_found(state)
        result = await command_executors.set_item_auto_approve(
            ex, _flag(auto_approve=True)
        )
        assert result.outcome == "executed"
        assert ex.statements[1][1]["from_state"] == state

    @pytest.mark.parametrize("value", [None, "true", "on", 1, 0, [], {}])
    async def test_anything_but_true_or_false_is_refused_by_name(self, value):
        ex = _ScriptedExecutor()
        with pytest.raises(commands.CommandRefused) as refused:
            await command_executors.set_item_auto_approve(ex, _flag(auto_approve=value))
        assert refused.value.reason == "invalid_args"
        assert "auto_approve" in str(refused.value)
        assert ex.statements == []

    async def test_no_auto_approve_at_all_is_refused_rather_than_read_as_off(self):
        ex = _ScriptedExecutor()
        with pytest.raises(commands.CommandRefused) as refused:
            await command_executors.set_item_auto_approve(ex, _flag())
        assert refused.value.reason == "invalid_args"
        assert "auto_approve" in str(refused.value)
        assert ex.statements == []

    @pytest.mark.parametrize("media_item_id", ["not-an-id", "", 42, None])
    async def test_an_item_id_that_is_not_one_is_refused_by_name(self, media_item_id):
        ex = _ScriptedExecutor()
        command = Command(
            kind="set_item_auto_approve",
            workspace_id="ws",
            actor_user_id="u1",
            channel="telegram",
            args={"media_item_id": media_item_id, "auto_approve": True},
        )
        with pytest.raises(commands.CommandRefused) as refused:
            await command_executors.set_item_auto_approve(ex, command)
        assert refused.value.reason == "invalid_args"
        assert "media_item_id" in str(refused.value)
        assert ex.statements == []


SOURCE = "7a1c3d5e-0000-4000-8000-00000000000c"


def _position(**args) -> Command:
    """`set_source_approval` on SOURCE, as an admin's web form sends it."""
    return Command(
        kind="set_source_approval",
        workspace_id="ws",
        actor_user_id="u1",
        channel="web",
        args={"source_id": SOURCE, **args},
    )


class TestTheFoldersApprovalPosition:
    """A folder's own position (#1477), at the SQL seam: the
    value checked by name, one UPDATE of the workspace's own source, then one
    audit row naming the change. What only PostgreSQL can say is the gate's:
    `tests/scripts/test_set_source_approval_gate.py`."""

    @pytest.mark.parametrize("mode", ["manual", "returning", "auto", None])
    async def test_sets_each_position_or_the_workspaces_with_an_audit_row(self, mode):
        ex = _ScriptedExecutor((1, {"state": "active", "was": "manual"}), (1, None))
        result = await command_executors.set_source_approval(
            ex, _position(approval_mode=mode)
        )

        assert result.outcome == "executed"
        assert result.data == {"source_id": SOURCE, "approval_mode": mode}
        (sql, params), (audit_sql, audit) = ex.statements
        assert "UPDATE media_sources s SET approval_mode = :m" in sql
        assert "s.id = :s AND s.workspace_id = :ws" in sql
        # A connected folder only: a removed one is not found.
        assert workspaces.CONNECTED_SQL in sql
        assert params == {"m": mode, "s": SOURCE, "ws": "ws"}
        assert "INSERT INTO audit_events" in audit_sql
        assert audit["entity_kind"] == "media_source"
        assert audit["source"] == SOURCE
        # A position is not a state: the source's own on both sides.
        assert audit["from_state"] == audit["to_state"] == "active"
        assert json.loads(audit["detail"]) == {
            "v": 1,
            "event": "approval_mode",
            "from": "manual",
            "to": mode,
        }

    async def test_another_workspaces_source_is_not_found_and_not_audited(self):
        ex = _ScriptedExecutor((0, None))
        with pytest.raises(commands.CommandRefused) as refused:
            await command_executors.set_source_approval(
                ex, _position(approval_mode="auto")
            )
        assert refused.value.reason == "not_found"
        assert refused.value.facts == {"missing": "source"}
        assert len(ex.statements) == 1

    @pytest.mark.parametrize("value", ["yolo", "Manual", "", True, 1, [], {}])
    async def test_a_value_outside_the_positions_is_refused_by_name(self, value):
        ex = _ScriptedExecutor()
        with pytest.raises(commands.CommandRefused) as refused:
            await command_executors.set_source_approval(
                ex, _position(approval_mode=value)
            )
        assert refused.value.reason == "invalid_args"
        assert "approval_mode" in str(refused.value)
        assert ex.statements == []

    async def test_no_approval_mode_at_all_is_refused_rather_than_cleared(self):
        ex = _ScriptedExecutor()
        with pytest.raises(commands.CommandRefused) as refused:
            await command_executors.set_source_approval(ex, _position())
        assert refused.value.reason == "invalid_args"
        assert "approval_mode" in str(refused.value)
        assert ex.statements == []

    @pytest.mark.parametrize("source_id", ["not-an-id", "", 42, None])
    async def test_a_source_id_that_is_not_one_is_refused_by_name(self, source_id):
        ex = _ScriptedExecutor()
        with pytest.raises(commands.CommandRefused) as refused:
            await command_executors.set_source_approval(
                ex, _position(source_id=source_id, approval_mode="auto")
            )
        assert refused.value.reason == "invalid_args"
        assert "source_id" in str(refused.value)
        assert ex.statements == []


@pytest.fixture(autouse=True)
def trusted_policy_seam(monkeypatch):
    """Keep the unit's fake SQL executor scoped to its own rule; real gates are
    unmocked."""
    from unittest.mock import AsyncMock
    from src.services.target import source_policy, source_state, versioned_mix
    from types import SimpleNamespace

    monkeypatch.setattr(
        source_policy,
        "suitability_context",
        AsyncMock(return_value=SimpleNamespace(binds=lambda: {})),
    )
    monkeypatch.setattr(source_policy, "intent_context", AsyncMock(return_value=None))
    monkeypatch.setattr(
        source_policy,
        "intent_suitability",
        AsyncMock(
            return_value={
                "suitable": True,
                "base_suitable": True,
                "live_intent_conflict": False,
                "blocking_locks": [],
                "warning_locks": [],
                "reason": None,
            }
        ),
    )
    monkeypatch.setattr(source_state, "lock_all_workspace_sources", AsyncMock())
    monkeypatch.setattr(
        versioned_mix,
        "state",
        AsyncMock(return_value={"converted": False}),
    )


@pytest.fixture(autouse=True)
def planning_verdict_seam(monkeypatch):
    from unittest.mock import AsyncMock
    from src.services.target import source_policy

    monkeypatch.setattr(
        source_policy, "suitability", AsyncMock(return_value=[{"base_suitable": True}])
    )
