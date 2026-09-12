"""`outbox.settle` on a 429 (phase 3a step 2): the row returns to `pending`
and the provider's `retry_after` becomes a DURABLE hold on the pacing rows —
never an in-task sleep, never a failed row, never an ambiguous one."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.services.target import outbox

NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)


@pytest.fixture()
def floor(monkeypatch):
    seen = {"left": [], "ambiguous": [], "holds": []}

    async def recover_stranded(session, *, binding_id):
        return []

    async def increment(session, **kw):
        return 1

    async def claim_next(session, *, binding_id):
        return {
            "id": "row-1",
            "binding_id": binding_id,
            "kind": "notification",
            "attempts": 1,
            "payload": {"v": 1, "text": "hi"},
        }

    async def _leave_sending(session, outbox_id, to_state, **extra):
        seen["left"].append((outbox_id, to_state, extra))

    async def mark_ambiguous(session, *, outbox_id):
        seen["ambiguous"].append(outbox_id)

    async def write_pacing_hold(session, **kw):
        seen["holds"].append(kw)
        return 3

    monkeypatch.setattr(outbox, "recover_stranded", recover_stranded)
    monkeypatch.setattr(outbox, "increment", increment)
    monkeypatch.setattr(outbox, "claim_next", claim_next)
    monkeypatch.setattr(outbox, "_leave_sending", _leave_sending)
    monkeypatch.setattr(outbox, "mark_ambiguous", mark_ambiguous)
    monkeypatch.setattr(outbox, "write_pacing_hold", write_pacing_hold)
    return seen


ROW = {"id": "row-1", "binding_id": "b-1", "kind": "notification", "attempts": 1}


class TestSettleOnAFloodLimit:
    async def test_the_row_goes_back_to_pending_and_both_holds_are_written(self, floor):
        result = await outbox.settle(
            object(),
            ROW,
            error=outbox.ChannelPaced("429", retry_after_s=7, scope="chat"),
            now=NOW,
            chat_limit=20,
            chat_window_seconds=60,
            global_limit=30,
            global_window_seconds=1,
        )
        assert result["state"] == "paced" and result["retry_after_s"] == 7.0
        assert result["external_message_ref"] is None
        assert floor["left"] == [("row-1", "pending", {"restore_attempt": True})]
        assert floor["ambiguous"] == [], "a 429 is not a lost response"
        scopes = {(h["scope"], h["key"]) for h in floor["holds"]}
        assert scopes == {("tg_global", ""), ("tg_chat", "b-1")}
        by_scope = {h["scope"]: h for h in floor["holds"]}
        # A chat-scoped 429 is the chat's limit: the chat row takes the whole
        # retry_after, the fleet's global row only a short brake.
        assert (
            by_scope["tg_global"]["seconds"] == outbox.CHAT_SCOPED_GLOBAL_BRAKE_SECONDS
        )
        assert by_scope["tg_global"]["limit"] == 30
        assert (
            by_scope["tg_chat"]["seconds"] == 7 and by_scope["tg_chat"]["limit"] == 20
        )
        assert by_scope["tg_chat"]["window_seconds"] == 60

    async def test_the_holds_survive_a_row_superseded_in_flight(
        self, floor, monkeypatch
    ):
        """`_leave_sending` is fenced when a tap retired the card between the
        claim and the 429 — the poller commits what came before. The holds
        must be in that set, or the next tick calls straight into the flood."""

        async def fenced(session, outbox_id, to_state, **extra):
            raise outbox.OutboxFenced("superseded in flight")

        monkeypatch.setattr(outbox, "_leave_sending", fenced)
        with pytest.raises(outbox.OutboxFenced):
            await outbox.settle(
                object(),
                ROW,
                error=outbox.ChannelPaced("429", retry_after_s=7, scope="chat"),
                now=NOW,
                chat_limit=20,
                chat_window_seconds=60,
                global_limit=30,
                global_window_seconds=1,
            )
        assert {h["scope"] for h in floor["holds"]} == {"tg_global", "tg_chat"}

    async def test_a_global_flood_writes_only_the_global_hold(self, floor):
        await outbox.settle(
            object(),
            ROW,
            error=outbox.ChannelPaced("429", retry_after_s=20, scope="global"),
            now=NOW,
            chat_limit=20,
            chat_window_seconds=60,
            global_limit=30,
            global_window_seconds=1,
        )
        assert [h["scope"] for h in floor["holds"]] == ["tg_global"]
        assert floor["holds"][0]["seconds"] == 20, (
            "a bot-wide 429 holds the fleet whole"
        )

    async def test_the_global_hold_is_bounded_the_chat_hold_takes_it_whole(self, floor):
        long = outbox.MAX_GLOBAL_HOLD_SECONDS * 10
        await outbox.settle(
            object(),
            ROW,
            error=outbox.ChannelPaced("429", retry_after_s=long, scope="chat"),
            now=NOW,
            chat_limit=20,
            chat_window_seconds=60,
            global_limit=30,
            global_window_seconds=1,
        )
        by_scope = {h["scope"]: h for h in floor["holds"]}
        assert (
            by_scope["tg_global"]["seconds"] == outbox.CHAT_SCOPED_GLOBAL_BRAKE_SECONDS
        )
        assert by_scope["tg_chat"]["seconds"] == long
        floor["holds"].clear()
        await outbox.settle(
            object(),
            ROW,
            error=outbox.ChannelPaced("429", retry_after_s=long, scope="global"),
            now=NOW,
            chat_limit=20,
            chat_window_seconds=60,
            global_limit=30,
            global_window_seconds=1,
        )
        assert floor["holds"][0]["seconds"] == outbox.MAX_GLOBAL_HOLD_SECONDS, (
            "one 429 must not spend the fleet's global row for ten minutes"
        )

    async def test_without_a_clock_the_row_still_returns_to_pending(self, floor):
        result = await outbox.settle(
            object(), ROW, error=outbox.ChannelPaced("429", retry_after_s=3)
        )
        assert result["state"] == "paced"
        assert floor["left"] == [("row-1", "pending", {"restore_attempt": True})]
        assert floor["holds"] == [], "no budget was named, so nothing is held"


class TestDeliverOnAFloodLimit:
    async def test_deliver_reports_paced_not_failed(self, floor):
        async def transport(row):
            raise outbox.ChannelPaced("429", retry_after_s=4, scope="chat")

        result = await outbox.deliver(
            object(),
            binding_id="b-1",
            transport=transport,
            now=NOW,
            chat_limit=10,
            chat_window_seconds=60,
            global_limit=100,
            global_window_seconds=1,
        )
        assert result["state"] == "paced" and result["retry_after_s"] == 4.0
        assert floor["left"] == [("row-1", "pending", {"restore_attempt": True})]
        assert len(floor["holds"]) == 2


class TestTheRowKeepsItsAttemptOnAFlood:
    async def test_leave_sending_restores_the_claimed_attempt(self):
        """R8's ambiguity budget is spent by LOST sends only; a 429 gives the
        claim's attempt back — asserted on the statement itself."""
        from src.services.target import outbox as real

        class _S:
            def __init__(self):
                self.sql = None

            async def execute(self, stmt, params=None):
                self.sql = " ".join(str(stmt).split())

                class _R:
                    rowcount = 1

                return _R()

        s = _S()
        await real._leave_sending(s, "row-1", "pending", restore_attempt=True)
        assert "attempts = GREATEST(attempts - 1, 0)" in s.sql
        assert "state = 'sending'" in s.sql, "still the one CAS"
        s2 = _S()
        await real._leave_sending(s2, "row-1", "failed")
        assert "GREATEST" not in s2.sql


class TestThePromptResendIsCapped:
    """Phase 3a step 3: an `approval_prompt` whose send keeps losing its
    answer is resent, but not forever — past MAX_PROMPT_RESENDS it fails."""

    class _Session:
        def __init__(self, kind, attempts):
            self.row = (kind, attempts)
            self.updates = []

        async def execute(self, stmt, params=None):
            sql = str(stmt)
            session = self

            class _R:
                rowcount = 1

                def first(self_inner):
                    return session.row

            if sql.lstrip().startswith("UPDATE"):
                self.updates.append(params["s"])
            return _R()

    async def test_under_the_cap_the_prompt_is_resent(self):
        s = self._Session("approval_prompt", outbox.MAX_PROMPT_RESENDS)
        assert await outbox.resolve_ambiguous(s, outbox_id="x") == "pending"

    async def test_past_the_cap_the_prompt_fails(self):
        s = self._Session("approval_prompt", outbox.MAX_PROMPT_RESENDS + 1)
        assert await outbox.resolve_ambiguous(s, outbox_id="x") == "failed"
        assert s.updates == ["failed"]

    async def test_a_lost_card_edit_is_resent_under_the_same_cap(self):
        """A `prompt_supersede` is the card's outcome edit — the one path
        that removes its buttons now that the route no longer strips (#1297).
        The edit is idempotent ("not modified" is success), so a lost answer
        costs one paced call to resend; the notification's single retry would
        leave a card live with buttons after one bad Telegram minute."""
        s = self._Session("prompt_supersede", outbox.MAX_PROMPT_RESENDS)
        assert await outbox.resolve_ambiguous(s, outbox_id="x") == "pending"
        s = self._Session("prompt_supersede", outbox.MAX_PROMPT_RESENDS + 1)
        assert await outbox.resolve_ambiguous(s, outbox_id="x") == "failed"

    async def test_a_notification_still_gets_one_retry(self):
        s = self._Session("notification", outbox.MAX_NOTIFICATION_RESENDS)
        assert await outbox.resolve_ambiguous(s, outbox_id="x") == "pending"
        s = self._Session("notification", outbox.MAX_NOTIFICATION_RESENDS + 1)
        assert await outbox.resolve_ambiguous(s, outbox_id="x") == "failed"
