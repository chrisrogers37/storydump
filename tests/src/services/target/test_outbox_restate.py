"""The restate-by-ref door carries the keyboard the state offers, and has an
everywhere form for the review resolutions (2026-09-12).

`restate_cards` reaches a card the tap already superseded (by ref, any settled
state). A `review_required` card needs BUTTONS — the member's three
resolutions — so the supersede row it queues can carry a `reply_markup`; the
transport sends that instead of the empty keyboard. `restate_everywhere` is
the executor's door: every active Telegram binding in one statement, the
shape `supersede_everywhere` set for the tap (#1286).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from src.services.target import outbox

KEYBOARD = {
    "inline_keyboard": [[{"text": "🔁 Post again", "callback_data": "v1:retry:i"}]]
}


class TestTheSupersedePayload:
    def test_without_a_keyboard_the_payload_is_as_before(self):
        body = outbox._supersede_payload(
            "77", {"caption": "h", "sent_as": "media"}, "line"
        )
        assert body == {
            "v": 1,
            "supersedes_ref": "77",
            "outcome_text": "line",
            "header": "h",
            "sent_as": "media",
        }

    def test_a_keyboard_rides_the_payload(self):
        body = outbox._supersede_payload(
            "77", {"text": "h"}, "line", reply_markup=KEYBOARD
        )
        assert body["reply_markup"] == KEYBOARD
        json.dumps(body)


class _Session:
    """Records statements; the restate UPDATE returns the rows given."""

    def __init__(self, rows):
        self.rows = rows
        self.sql = []

    async def execute(self, statement, params=None):
        self.sql.append((str(statement), params))
        rows = self.rows

        class _R:
            def fetchall(self_inner):
                return rows

            def first(self_inner):
                return rows[0] if rows else None

        return _R()


class TestRestateCards:
    @pytest.fixture
    def queued(self, monkeypatch):
        seen = []

        async def enqueue(session, **kw):
            seen.append(kw)
            return "ob-new"

        monkeypatch.setattr(outbox, "enqueue", enqueue)
        return seen

    async def test_the_keyboard_reaches_the_queued_edit(self, queued):
        s = _Session([("77", {"text": "h", "sent_as": "text"})])
        n = await outbox.restate_cards(
            s,
            workspace_id="ws",
            binding_id="b",
            intent_id="i",
            outcome_text="👀 Needs review",
            reply_markup=KEYBOARD,
        )
        assert n == 1
        assert queued[0]["kind"] == "prompt_supersede"
        assert queued[0]["payload"]["reply_markup"] == KEYBOARD
        assert queued[0]["payload"]["outcome_text"] == "👀 Needs review"

    async def test_without_a_keyboard_none_is_queued(self, queued):
        s = _Session([("77", {"text": "h"})])
        await outbox.restate_cards(
            s, workspace_id="ws", binding_id="b", intent_id="i", outcome_text="x"
        )
        assert "reply_markup" not in queued[0]["payload"]


class TestRestateEverywhere:
    async def test_one_statement_addresses_every_binding_by_ref(self):
        s = _Session([(3,)])
        n = await outbox.restate_everywhere(
            s, workspace_id="ws", intent_id="i", outcome_text="✅ Posted by Chris"
        )
        assert n == 3 and len(s.sql) == 1, "one round trip (#1286)"
        sql, params = s.sql[0]
        assert "channel_bindings" in sql and "state = 'active'" in sql
        assert "kind = 'approval_prompt'" in sql
        assert "('sent', 'superseded', 'ambiguous')" in sql, "by ref, any settled state"
        assert "external_message_ref IS NOT NULL" in sql
        assert "'prompt_supersede'" in sql
        assert params == {"ws": "ws", "i": "i", "o": "✅ Posted by Chris", "kb": None}

    async def test_a_keyboard_is_bound_as_json(self):
        s = _Session([(1,)])
        await outbox.restate_everywhere(
            s, workspace_id="ws", intent_id="i", outcome_text="x", reply_markup=KEYBOARD
        )
        assert json.loads(s.sql[0][1]["kb"]) == KEYBOARD


class TestACardLandingOnAParkedIntentKeepsTheReviewButtons:
    """`_edit_sent_card` (R6 after a send): a card whose intent moved while it
    was in flight is edited at once. If the intent is `review_required`, the
    edit carries the review keyboard; for any other state it carries none."""

    @pytest.fixture
    def seams(self, monkeypatch):
        from src.services.target import identity, intent_ledger

        seen = {"state": "review_required", "queued": []}

        async def settlement(session, *, workspace_id, intent_id):
            return {"state": seen["state"], "by_user_id": None, "at": None}

        async def display_name_for(session, *, user_id):
            return None

        async def enqueue(session, **kw):
            seen["queued"].append(kw)
            return "ob-new"

        monkeypatch.setattr(intent_ledger, "settlement", settlement)
        monkeypatch.setattr(identity, "display_name_for", display_name_for)
        monkeypatch.setattr(outbox, "enqueue", enqueue)
        return seen

    class _Receipt(str):
        sent_as = "text"

    async def _land(self, seams):
        session = _Session([("UTC",)])
        row = {
            "id": "ob-1",
            "kind": "approval_prompt",
            "intent_id": "i",
            "workspace_id": "ws",
            "binding_id": "b",
            "payload": {"v": 2, "text": "📸 f.jpg"},
        }
        assert await outbox._edit_sent_card(
            session, row, self._Receipt("777"), force=False
        )
        return seams["queued"][0]["payload"]

    async def test_a_parked_intent_gets_the_three_buttons(self, seams):
        payload = await self._land(seams)
        assert "Needs review" in payload["outcome_text"]
        tokens = [
            b["callback_data"]
            for r in payload["reply_markup"]["inline_keyboard"]
            for b in r
        ]
        assert tokens == ["v1:itposted:i", "v1:notposted:i", "v1:giveup:i"]

    async def test_any_other_state_gets_none(self, seams):
        seams["state"] = "posted"
        payload = await self._land(seams)
        assert "Posted" in payload["outcome_text"] and "reply_markup" not in payload


class TestACardIsRenderedAgainAtClaimTime:
    """`_claim_current` (2026-09-12): every claimed `approval_prompt` with an
    intent is rendered again before the send; a slot that moved on is retired
    unsent and the next row claimed; a live one goes out as the fresh card,
    written through the one `sending` fence."""

    def test_the_rule(self):
        assert outbox._needs_refresh({"kind": "approval_prompt", "intent_id": "i"})
        assert not outbox._needs_refresh({"kind": "notification", "intent_id": "i"})
        assert not outbox._needs_refresh({"kind": "approval_prompt", "intent_id": None})

    @pytest.fixture
    def claims(self, monkeypatch):
        from src.services.target import prompts

        seen = {"claimed": [], "left": [], "rendered": {}}

        async def recover_stranded(session, *, binding_id):
            return []

        async def resolve_aged_ambiguous(session, *, binding_id):
            return []

        async def claim_next(session, *, binding_id):
            return seen["claimed"].pop(0) if seen["claimed"] else None

        async def _leave_sending(session, outbox_id, to_state, **extra):
            seen["left"].append((outbox_id, to_state, extra))

        async def rerender_prompt(session, *, intent_id):
            return seen["rendered"].get(intent_id)

        async def increment(session, **kw):
            return 1

        monkeypatch.setattr(outbox, "recover_stranded", recover_stranded)
        monkeypatch.setattr(outbox, "resolve_aged_ambiguous", resolve_aged_ambiguous)
        monkeypatch.setattr(outbox, "claim_next", claim_next)
        monkeypatch.setattr(outbox, "_leave_sending", _leave_sending)
        monkeypatch.setattr(prompts, "rerender_prompt", rerender_prompt)
        monkeypatch.setattr(outbox, "increment", increment)
        return seen

    async def _claim(self):
        return await outbox.pace_and_claim(
            object(),
            binding_id="b",
            now=datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc),
            chat_limit=20,
            chat_window_seconds=60,
            global_limit=30,
            global_window_seconds=1,
        )

    async def test_a_stale_card_is_retired_and_the_next_row_is_claimed(self, claims):
        claims["claimed"] = [
            {
                "id": "ob-1",
                "kind": "approval_prompt",
                "intent_id": "gone",
                "attempts": 2,
                "payload": {},
            },
            {
                "id": "ob-2",
                "kind": "notification",
                "intent_id": None,
                "attempts": 1,
                "payload": {},
            },
        ]
        row = await self._claim()
        assert row is not None and row["id"] == "ob-2"
        assert claims["left"] == [("ob-1", "superseded", {})]

    async def test_a_live_card_goes_out_as_the_fresh_card(self, claims):
        fresh = {"v": 2, "text": "new", "reply_markup": {"inline_keyboard": []}}
        claims["claimed"] = [
            {
                "id": "ob-1",
                "kind": "approval_prompt",
                "intent_id": "live",
                "attempts": 1,
                "payload": {"v": 2, "text": "old"},
            },
        ]
        claims["rendered"] = {"live": fresh}
        row = await self._claim()
        assert row is not None and row["payload"] == fresh
        assert claims["left"] == [("ob-1", "sending", {"payload": fresh})], (
            "the record is rewritten through the one fence, in the claim transaction"
        )
