"""`outbox.settle` on a 429: the row returns to `pending`
and the provider's `retry_after` becomes a DURABLE hold on the pacing rows —
never an in-task sleep, never a failed row, never an ambiguous one."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from contextlib import asynccontextmanager


from src.services.target import outbox


class _ClaimSession:
    @asynccontextmanager
    async def begin_nested(self):
        yield

    async def commit(self):
        pass


NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)


@pytest.fixture()
def floor(monkeypatch):
    seen = {"left": [], "ambiguous": [], "holds": []}

    async def recover_stranded(session, *, binding_id):
        return []

    async def resolve_aged_ambiguous(session, *, binding_id):
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

    async def mark_ambiguous(session, *, outbox_id, failure):
        seen["ambiguous"].append((outbox_id, failure))

    async def write_pacing_hold(session, **kw):
        seen["holds"].append(kw)
        return 3

    monkeypatch.setattr(outbox, "recover_stranded", recover_stranded)
    monkeypatch.setattr(outbox, "resolve_aged_ambiguous", resolve_aged_ambiguous)
    monkeypatch.setattr(outbox, "increment", increment)
    monkeypatch.setattr(outbox, "claim_next", claim_next)
    monkeypatch.setattr(outbox, "_leave_sending", _leave_sending)
    monkeypatch.setattr(outbox, "mark_ambiguous", mark_ambiguous)
    monkeypatch.setattr(outbox, "write_pacing_hold", write_pacing_hold)
    return seen


ROW = {"id": "row-1", "binding_id": "b-1", "kind": "notification", "attempts": 1}


def _warnings(caplog):
    """The WARNINGs the outbox logged, as text."""
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == outbox.__name__ and r.levelname == "WARNING"
    ]


class TestSettleOnAFloodLimit:
    async def test_the_row_goes_back_to_pending_and_both_holds_are_written(self, floor):
        result = await outbox.settle(
            _ClaimSession(),
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
        assert floor["left"] == [
            (
                "row-1",
                "pending",
                {"restore_attempt": True, "failure": ("rate_limited", None)},
            )
        ]
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
                _ClaimSession(),
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
            _ClaimSession(),
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
            _ClaimSession(),
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
            _ClaimSession(),
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
            _ClaimSession(), ROW, error=outbox.ChannelPaced("429", retry_after_s=3)
        )
        assert result["state"] == "paced"
        assert floor["left"] == [
            (
                "row-1",
                "pending",
                {"restore_attempt": True, "failure": ("rate_limited", None)},
            )
        ]
        assert floor["holds"] == [], "no budget was named, so nothing is held"


class TestDeliverOnAFloodLimit:
    async def test_deliver_reports_paced_not_failed(self, floor):
        async def transport(row):
            raise outbox.ChannelPaced("429", retry_after_s=4, scope="chat")

        result = await outbox.deliver(
            _ClaimSession(),
            binding_id="b-1",
            transport=transport,
            now=NOW,
            chat_limit=10,
            chat_window_seconds=60,
            global_limit=100,
            global_window_seconds=1,
        )
        assert result["state"] == "paced" and result["retry_after_s"] == 4.0
        assert floor["left"] == [
            (
                "row-1",
                "pending",
                {"restore_attempt": True, "failure": ("rate_limited", None)},
            )
        ]
        assert len(floor["holds"]) == 2


class TestTheRowKeepsItsAttemptOnAFlood:
    async def test_leave_sending_restores_the_claimed_attempt(self):
        """The ambiguity budget is spent by LOST sends only; a 429 gives the
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
    """An `approval_prompt` whose send keeps losing its
    answer is resent, but not forever — past MAX_PROMPT_RESENDS it fails. Every
    row that fails in `resolve_ambiguous` is said once."""

    class _Session:
        def __init__(
            self,
            kind,
            attempts,
            newer_edit=False,
            fresh=True,
            *,
            no_card_reached=True,
            code=None,
        ):
            # The columns `resolve_ambiguous` selects: the policy's four, the
            # row's tenant, binding and story (an invitation has none), and
            # its last failure's class and code.
            self.row = (
                kind,
                attempts,
                newer_edit,
                fresh,
                "ws",
                "b",
                None if kind == "invitation" else "i",
                "ambiguous",
                code,
            )
            # Whether the binding holds no card of the story, the answer
            # `chats_without_card` gives (patched below).
            self.no_card_reached = no_card_reached
            self.updates = []

        async def execute(self, stmt, params=None):
            sql = str(stmt)
            session = self
            self.sql = getattr(self, "sql", []) + [(sql, params)]

            class _R:
                rowcount = 1

                def first(self_inner):
                    return session.row

            if sql.lstrip().startswith("UPDATE"):
                self.updates.append(params["s"])
            return _R()

    @pytest.fixture(autouse=True)
    def enqueued(self, monkeypatch):
        """`outbox.enqueue`, recorded with the writes the session had seen
        when it was called; `outbox.chats_without_card` answers from the
        session."""
        seen = []

        async def enqueue(session, **kw):
            seen.append((list(session.updates), kw))
            return "ob-new"

        async def chats_without_card(session, *, workspace_id, intent_id):
            assert (workspace_id, intent_id) == ("ws", "i")
            return ["b"] if session.no_card_reached else []

        monkeypatch.setattr(outbox, "enqueue", enqueue)
        monkeypatch.setattr(outbox, "chats_without_card", chats_without_card)
        return seen

    async def test_under_the_cap_the_prompt_is_resent(self):
        s = self._Session("approval_prompt", outbox.MAX_PROMPT_RESENDS)
        assert await outbox.resolve_ambiguous(s, outbox_id="x") == "pending"

    async def test_past_the_cap_the_prompt_fails(self, enqueued, caplog):
        s = self._Session("approval_prompt", outbox.MAX_PROMPT_RESENDS + 1, code=502)
        with caplog.at_level("WARNING", logger=outbox.__name__):
            assert await outbox.resolve_ambiguous(s, outbox_id="x") == "failed"
        assert s.updates == ["failed"]
        (message,) = _warnings(caplog)
        for part in (
            "row x (approval_prompt)",
            "story i",
            "binding b",
            f"gave up after {outbox.MAX_PROMPT_RESENDS + 1} sends",
            "last failure ambiguous, code 502",
        ):
            assert part in message
        assert len(enqueued) == 1, "the chat is told once"
        after, call = enqueued[0]
        assert after == ["failed"], "after the row failed, in the same transaction"
        assert call == {
            "workspace_id": "ws",
            "binding_id": "b",
            "kind": "notification",
            "intent_id": "i",
            "payload": {"v": 1, "text": outbox.CARD_UNCONFIRMED_NOTICE},
        }

    async def test_the_give_up_tells_the_chat_only_when_no_card_reached_it(
        self, enqueued, caplog
    ):
        s = self._Session(
            "approval_prompt", outbox.MAX_PROMPT_RESENDS + 1, no_card_reached=False
        )
        with caplog.at_level("WARNING", logger=outbox.__name__):
            assert await outbox.resolve_ambiguous(s, outbox_id="x") == "failed"
        (message,) = _warnings(caplog)
        assert "gave up after" in message and "no card of the story" not in message
        assert enqueued == [], "another card of the story is in the chat"

    @pytest.mark.parametrize(
        "kind, attempts, fresh",
        [
            ("invitation", outbox.MAX_PROMPT_RESENDS + 1, True),
            ("notification", outbox.MAX_NOTIFICATION_RESENDS + 1, True),
            ("prompt_supersede", 2, False),
        ],
    )
    async def test_every_give_up_is_said_once(
        self, enqueued, caplog, kind, attempts, fresh
    ):
        s = self._Session(kind, attempts, fresh=fresh)
        with caplog.at_level("WARNING", logger=outbox.__name__):
            assert await outbox.resolve_ambiguous(s, outbox_id="x") == "failed"
        (message,) = _warnings(caplog)
        assert f"({kind})" in message and f"gave up after {attempts} sends" in message
        assert enqueued == [], "only a card that left the chat nothing is told of"

    async def test_a_lost_card_edit_is_resent_until_confirmed(self):
        """A `prompt_supersede`, the card's edit, is idempotent, so a lost
        answer never fails it: it goes back to `pending` however many attempts
        it has spent (`outbox.RESEND_KINDS` says why), where a card past
        `MAX_PROMPT_RESENDS` fails. The notification's single retry would
        leave a card without its buttons after one bad Telegram minute."""
        for attempts in (
            outbox.MAX_PROMPT_RESENDS,
            outbox.MAX_PROMPT_RESENDS + 1,
            10 * outbox.MAX_PROMPT_RESENDS,
        ):
            s = self._Session("prompt_supersede", attempts)
            assert await outbox.resolve_ambiguous(s, outbox_id="x") == "pending"
            assert s.updates == ["pending"], attempts

    async def test_a_lost_card_edit_past_its_horizon_fails(self):
        """An edit is resent until Telegram confirms it, but not past
        `EDIT_RESEND_HORIZON_SECONDS` from its creation: one that never
        answers fails, so it holds nothing open."""
        s = self._Session("prompt_supersede", 2, fresh=False)
        assert await outbox.resolve_ambiguous(s, outbox_id="x") == "failed"
        assert s.updates == ["failed"]
        (sql, params), *_ = s.sql
        assert "make_interval(secs => :horizon) AS fresh" in sql
        assert params["horizon"] == outbox.EDIT_RESEND_HORIZON_SECONDS

    async def test_a_lost_edit_with_a_newer_edit_of_the_same_message_is_superseded(
        self,
    ):
        """A resent edit carries ITS line; if a later `prompt_supersede` for
        the same message ref exists (the pipeline's "✅ Posted" after the tap's
        "✅ Approved"), resending the older one would overwrite the newer line
        for good — so it is retired instead (#1297)."""
        s = self._Session("prompt_supersede", 1, newer_edit=True)
        assert await outbox.resolve_ambiguous(s, outbox_id="x") == "superseded"
        assert s.updates == ["superseded"]
        assert "supersedes_ref" in s.sql[0][0] and "created_at >" in s.sql[0][0]

    async def test_a_notification_still_gets_one_retry(self):
        s = self._Session("notification", outbox.MAX_NOTIFICATION_RESENDS)
        assert await outbox.resolve_ambiguous(s, outbox_id="x") == "pending"
        s = self._Session("notification", outbox.MAX_NOTIFICATION_RESENDS + 1)
        assert await outbox.resolve_ambiguous(s, outbox_id="x") == "failed"


class TestARefusalIsDefinitive:
    """A provider's 4xx refusal (`ChannelRefused`) is not a lost answer:
    the message as shaped was never accepted, so the row fails outright
    instead of entering the ambiguity policy and being resent up to the
    cap (#1297)."""

    async def test_a_refused_send_fails_the_row_without_ambiguity(self, floor):
        result = await outbox.settle(
            _ClaimSession(),
            ROW,
            error=outbox.ChannelRefused(
                "editMessageReplyMarkup: 400 message to edit not found"
            ),
            now=NOW,
            chat_limit=20,
            chat_window_seconds=60,
            global_limit=30,
            global_window_seconds=1,
        )
        assert result["state"] == "failed" and result["external_message_ref"] is None
        assert floor["left"] == [("row-1", "failed", {"failure": ("refused", None)})]
        assert floor["ambiguous"] == [] and floor["holds"] == []


class TestALostAnswerIsSaidOnce:
    """`settle` of a lost answer logs one WARNING naming the row, its kind, the
    attempt, and the failure's class and code — never the provider's text — so
    a card or an edit sent again after one is visible in the log. For an edit
    that carried the card's media it also says the picture is given up."""

    async def test_it_names_the_row_the_attempt_and_the_failure_never_its_text(
        self, floor, caplog
    ):
        lost = outbox.ChannelSendError("sendMessage: transport failure", code=502)
        with caplog.at_level("WARNING", logger=outbox.__name__):
            result = await outbox.settle(_ClaimSession(), ROW, error=lost)
        assert result["state"] == "ambiguous"
        (message,) = _warnings(caplog)
        for part in ("row-1", "notification", "attempt 1", "ambiguous", "code 502"):
            assert part in message
        assert "transport failure" not in message, "the provider's text is not logged"
        assert "media" not in message

    async def test_an_edit_that_carried_the_cards_media_says_it_is_given_up(
        self, floor, caplog, monkeypatch
    ):
        recorded = []

        async def record(session, row, *, sent_as):
            recorded.append((row["id"], sent_as))

        monkeypatch.setattr(outbox, "_record_cards_media", record, raising=False)
        edit = {
            **ROW,
            "kind": "prompt_supersede",
            "intent_id": "i",
            "workspace_id": "ws",
            "payload": {"v": 1, "supersedes_ref": "77", "media": {"kind": "image"}},
        }
        lost = outbox.ChannelSendError("editMessageMedia: transport failure")
        with caplog.at_level("WARNING", logger=outbox.__name__):
            await outbox.settle(_ClaimSession(), edit, error=lost)
        (message,) = _warnings(caplog)
        assert "prompt_supersede" in message and "media is given up" in message
        assert recorded == [("row-1", "text")], "the card's record follows"

        # The same edit without a block gives nothing up.
        caplog.clear()
        bare = {**edit, "payload": {"v": 1, "supersedes_ref": "77"}}
        with caplog.at_level("WARNING", logger=outbox.__name__):
            await outbox.settle(_ClaimSession(), bare, error=lost)
        (message,) = _warnings(caplog)
        assert "prompt_supersede" in message and "given up" not in message
        assert recorded == [("row-1", "text")]


class TestALostAnswerIsResolvedOnALaterTick:
    """`settle` marks a live sender's lost answer `ambiguous`. The binding's
    sender applies the per-kind policy (`resolve_ambiguous`) to it on a later
    tick, after a backoff, and not only to rows a DEAD sender left `sending`
    (`recover_stranded`): otherwise a live sender's ambiguous row would sit
    until retention deleted it, and the resend policy would be unreachable for
    the common case (#1297)."""

    class _Session:
        def __init__(self, ids):
            self.ids = ids
            self.sql = []

        async def execute(self, statement, params=None):
            self.sql.append((str(statement), params))
            ids = self.ids

            class _R:
                def fetchall(self_inner):
                    return [(i,) for i in ids]

            return _R()

    async def test_aged_ambiguous_rows_go_through_the_policy_in_order(
        self, monkeypatch
    ):
        resolved = []

        async def resolve_ambiguous(session, *, outbox_id):
            resolved.append(outbox_id)
            return "pending"

        monkeypatch.setattr(outbox, "resolve_ambiguous", resolve_ambiguous)
        s = self._Session(["old-1", "old-2"])
        assert await outbox.resolve_aged_ambiguous(s, binding_id="b-1") == [
            "old-1",
            "old-2",
        ]
        assert resolved == ["old-1", "old-2"]
        sql, params = s.sql[0]
        assert "state = 'ambiguous'" in sql and "updated_at" in sql
        assert params["b"] == "b-1"
        assert params["age"] == outbox.AMBIGUOUS_RESOLVE_AFTER_SECONDS

    async def test_the_claim_resolves_the_aged_rows_after_the_stranded_ones(
        self, floor, monkeypatch
    ):
        order = []

        async def recover_stranded(session, *, binding_id):
            order.append("stranded")
            return []

        async def resolve_aged_ambiguous(session, *, binding_id):
            order.append(("aged", binding_id))
            return []

        async def claim_next(session, *, binding_id):
            order.append("claim")
            return None

        monkeypatch.setattr(outbox, "recover_stranded", recover_stranded)
        monkeypatch.setattr(outbox, "resolve_aged_ambiguous", resolve_aged_ambiguous)
        monkeypatch.setattr(outbox, "claim_next", claim_next)
        assert (
            await outbox.pace_and_claim(
                _ClaimSession(),
                binding_id="b-1",
                now=NOW,
                chat_limit=20,
                chat_window_seconds=60,
                global_limit=30,
                global_window_seconds=1,
            )
            is None
        )
        assert order == ["stranded", ("aged", "b-1"), "claim"]


@pytest.fixture(autouse=True)
def source_prefix_seam(monkeypatch):
    """Transport/ledger doubles do not impersonate the shared SQL authority."""
    from unittest.mock import AsyncMock

    monkeypatch.setattr(outbox, "_lock_binding_sources", AsyncMock())


class TestTheBackoffIsOneSchedule:
    """A lost answer waits `AMBIGUOUS_RESOLVE_AFTER_SECONDS` times its row's
    attempts, in two places that cannot share code: the sender's own
    resolution (`resolve_aged_ambiguous`) and the sender sweep's door
    (`fn_sender_sweep`), which mints a sender only for a row that is due. One
    schedule, or the sweep mints idle senders every pass, or none for a due
    row; the plain bound keeps `ix_outbox_ambiguous_age`'s range."""

    def test_the_sweep_door_and_the_sender_read_one_schedule(self):
        import inspect

        from tests.src.services.target.schema_code import function_body

        body = function_body("fn_sender_sweep")
        assert "o.updated_at <= now() - make_interval(secs => p_age)" in body
        assert "secs => p_age * GREATEST(o.attempts, 1)" in body
        source = inspect.getsource(outbox.resolve_aged_ambiguous)
        assert "updated_at <= now() - make_interval(secs => :age)" in source
        assert "* GREATEST(attempts, 1)" in source
