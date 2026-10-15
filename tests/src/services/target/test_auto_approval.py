"""The serve step's decision (#1477): `auto_approval.decide` over its truth
table, the one read it decides from, and the approval it takes.

`decide` is pure, so the order is pinned here: the first match wins, and
step 1 (a cancel, API posting off, no login without a dry run, a link) stops
every rule. The database gate (`tests/scripts/test_auto_approval_gate.py`)
proves the same against the ledger's trigger.
"""

from __future__ import annotations

import itertools

import pytest

from src.services.target import (
    auto_approval,
    prompt_rendering,
    telegram_dispatch,
    vocabulary,
)
from src.services.target.auto_approval import Decision, Facts, decide

#: A story every rule would approve, and nothing blocks.
EVERYTHING = Facts(
    provider_account_ref="17841400000000000",
    cancel_requested=False,
    api_publishing_enabled=True,
    dry_run=False,
    has_login=True,
    has_link=False,
    flagged=True,
    position="auto",
    posted_via_api=True,
)

#: A story nothing approves: a person decides it.
NOTHING = EVERYTHING._replace(flagged=False, position="manual", posted_via_api=False)


class TestTheOrder:
    def test_the_flag_comes_first(self):
        assert decide(EVERYTHING) == Decision("item")

    def test_the_flag_wins_over_a_folder_set_to_manual(self):
        # The position is already resolved: a folder's own `manual` over a
        # YOLO workspace reaches decide as `manual`.
        assert decide(NOTHING._replace(flagged=True)) == Decision("item")

    def test_auto_is_yolo(self):
        assert decide(NOTHING._replace(position="auto")) == Decision("auto")

    def test_returning_needs_a_post_through_the_api(self):
        returning = NOTHING._replace(position="returning")
        assert decide(returning._replace(posted_via_api=True)) == Decision("returning")
        assert decide(returning) == Decision(None)

    def test_manual_asks_a_person(self):
        assert decide(NOTHING) == Decision(None)
        assert decide(NOTHING._replace(posted_via_api=True)) == Decision(None)


class TestStepOneStopsEveryRule:
    @pytest.mark.parametrize(
        "change, blocked_by",
        [
            ({"api_publishing_enabled": False}, "api_off"),
            ({"has_login": False}, "no_login"),
            ({"has_link": True}, "link"),
        ],
    )
    def test_each_block_says_what_the_rule_would_have_been(self, change, blocked_by):
        assert decide(EVERYTHING._replace(**change)) == Decision(
            None, blocked_by, "item"
        )

    def test_a_block_without_a_rule_is_not_reported(self):
        """A block is named only when it stopped a rule: a story nothing would
        approve has no block to report, so its card has no note."""
        assert decide(NOTHING._replace(api_publishing_enabled=False)) == Decision(None)

    def test_a_cancel_blocks_without_a_note(self):
        assert decide(EVERYTHING._replace(cancel_requested=True)) == Decision(None)

    def test_the_blocks_are_read_in_order(self):
        every_block = EVERYTHING._replace(
            api_publishing_enabled=False, has_login=False, has_link=True
        )
        assert decide(every_block).blocked_by == "api_off"
        assert decide(every_block._replace(api_publishing_enabled=True)).blocked_by == (
            "no_login"
        )

    def test_a_dry_run_needs_no_login(self):
        assert decide(EVERYTHING._replace(has_login=False, dry_run=True)) == Decision(
            "item"
        )

    def test_every_rule_it_gives_is_a_rule_the_ledger_takes(self):
        given = {
            decide(NOTHING._replace(flagged=True)).rule,
            decide(NOTHING._replace(position="auto")).rule,
            decide(NOTHING._replace(position="returning", posted_via_api=True)).rule,
        }
        assert given == set(vocabulary.APPROVAL_RULES)

    def test_every_block_it_can_name_has_a_note_and_a_toggle_tail(self):
        """`decide` over every fact it reads, each yes-or-no fact both ways and
        each position: every block it can name has a waiting card's note
        (`prompt_rendering._BLOCK_NOTES`) and the toggle's tail
        (`telegram_dispatch._HELD_BACK`), and nothing else does. A block
        missing from either map goes unsaid there: the card goes without its
        note, and the toggle's answer without what holds the flag back. The
        census pins the facts that are not yes-or-no: the position, which the
        test walks, and the account's ref, which `decide` does not read, held
        fixed. A fact of any other kind fails it until the census names it,
        which should come with walking that fact too."""
        yes_or_no = [k for k, v in EVERYTHING._asdict().items() if isinstance(v, bool)]
        census = set(Facts._fields) - set(yes_or_no)
        assert census == {"provider_account_ref", "position"}, census
        every_story = (
            EVERYTHING._replace(position=position, **dict(zip(yes_or_no, values)))
            for position in vocabulary.APPROVAL_POSITIONS
            for values in itertools.product((False, True), repeat=len(yes_or_no))
        )
        named = {decide(f).blocked_by for f in every_story} - {None}
        assert named == set(prompt_rendering._BLOCK_NOTES), named
        assert named == set(telegram_dispatch._HELD_BACK), named


class _Session:
    """Answers the facts read with *row*, and records it."""

    def __init__(self, row):
        self.row = row
        self.sql = []

    async def execute(self, statement, params=None):
        self.sql.append((str(statement), params))
        row = self.row

        class _M:
            def first(self_inner):
                return row

        class _R:
            def mappings(self_inner):
                return _M()

        return _R()


ROW = {
    "provider_account_ref": "17841400000000000",
    "cancel_requested": False,
    "api_publishing_enabled": True,
    "dry_run_mode": False,
    "position": "returning",
    "auto_approve": False,
    "has_link": False,
    "has_login": True,
    "posted_via_api": True,
}


class TestTheFactsRead:
    async def test_it_maps_the_row(self):
        found = await auto_approval.facts(
            _Session(ROW), workspace_id="ws", intent_id="i"
        )
        assert found == Facts(
            provider_account_ref="17841400000000000",
            cancel_requested=False,
            api_publishing_enabled=True,
            dry_run=False,
            has_login=True,
            has_link=False,
            flagged=False,
            position="returning",
            posted_via_api=True,
        )

    async def test_every_table_is_bound_on_the_workspace(self):
        session = _Session(ROW)
        await auto_approval.facts(session, workspace_id="ws", intent_id="i")
        sql, params = session.sql[0]
        assert params["ws"] == "ws" and params["id"] == "i"
        for binding in (
            "i.workspace_id = :ws",
            "w.id = :ws",
            "m.workspace_id = :ws",
            "s.workspace_id = :ws",
            "c.workspace_id = :ws",
            "p.workspace_id = :ws",
        ):
            assert binding in sql, binding
        # The probe repeats `ix_intents_api_posted`'s predicate (110), so the
        # planner can use the partial index.
        assert "p.state = 'posted' AND p.published_via = 'api'" in sql
        assert "_CARD_SELECT" not in sql and "fn_prompts_due" not in sql

    async def test_a_story_it_cannot_see_has_no_facts(self):
        assert (
            await auto_approval.facts(_Session(None), workspace_id="ws", intent_id="i")
            is None
        )


class TestTheApproval:
    async def test_a_rule_approves_with_its_stamp_and_mints_the_one_job(
        self, monkeypatch
    ):
        moves, mints = [], []

        async def transition(session, intent_id, to_state, *, approval_rule=None):
            moves.append((intent_id, to_state, approval_rule))

        async def mint_publish_job(session, **kwargs):
            mints.append(kwargs)

        monkeypatch.setattr(auto_approval.intent_ledger, "transition", transition)
        monkeypatch.setattr(auto_approval.jobs, "mint_publish_job", mint_publish_job)
        rule = await auto_approval.approve_if_due(
            _Session(ROW), workspace_id="ws", intent_id="i"
        )
        assert rule == "returning"
        assert moves == [("i", "approved", "returning")]
        assert mints == [
            {
                "workspace_id": "ws",
                "intent_id": "i",
                "provider_account_ref": "17841400000000000",
                "dry_run": False,
            }
        ]

    @pytest.mark.parametrize(
        "row", [None, {**ROW, "posted_via_api": False}, {**ROW, "has_link": True}]
    )
    async def test_no_rule_writes_nothing(self, monkeypatch, row):
        async def refuse(*args, **kwargs):
            raise AssertionError("nothing is written when no rule approves")

        monkeypatch.setattr(auto_approval.intent_ledger, "transition", refuse)
        monkeypatch.setattr(auto_approval.jobs, "mint_publish_job", refuse)
        assert (
            await auto_approval.approve_if_due(
                _Session(row), workspace_id="ws", intent_id="i"
            )
            is None
        )


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
