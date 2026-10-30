"""The restate-by-ref door, and its everywhere form for the review
resolutions; the review card a parked story gets in a chat that holds no
card of it; the sender's claim of a card and of its edit.

`restate_cards` reaches a card the tap already superseded (by ref, any settled
state). `restate_everywhere` is the executor's door: every active Telegram
binding in one statement, the shape `supersede_everywhere` set for the tap
(#1286). Neither carries a keyboard: the sender gives every edit of an
approval card its story's keyboard as it is when the row is claimed (#1477),
and sends the card itself with none — a parked story's edit keeps the member's
three resolutions because the story is `review_required`.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

from src.services.target import outbox
from src.services.target.outbox import OutboxPaced

PARKED = "00000000-0000-4000-8000-0000000000aa"


def _facts(state="awaiting_approval", *, item_state="available", cancel=False):
    """A card's story as `prompts.card_facts` reads it."""
    return {
        "state": state,
        "cancel_requested": cancel,
        "media_item_id": "m-1",
        "item_state": item_state,
        "auto_approve": False,
        "api_publishing_enabled": True,
    }


def _claimed(kind, intent_id, payload, **over):
    """A row as `claim_next` hands it to the sender, less its `binding_id`:
    outbox row `ob-1` of workspace `ws`, on its first attempt unless *over*
    says otherwise."""
    base = {
        "id": "ob-1",
        "kind": kind,
        "intent_id": intent_id,
        "workspace_id": "ws",
        "attempts": 1,
        "payload": payload,
    }
    base.update(over)
    return base


class _ClaimSession:
    @asynccontextmanager
    async def begin_nested(self):
        yield

    async def commit(self):
        pass


class TestTheSupersedePayload:
    def test_it_names_the_card_and_carries_no_keyboard(self):
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


class _Session:
    """Records statements and savepoints; the restate UPDATE returns the rows
    given."""

    def __init__(self, rows):
        self.rows = rows
        self.sql = []
        self.savepoints = []

    @asynccontextmanager
    async def begin_nested(self):
        """A savepoint: released on a clean exit; on a raise it is rolled back
        and the error let through."""
        self.savepoints.append("savepoint")
        try:
            yield
        except BaseException:
            self.savepoints.append("rolled back")
            raise
        else:
            self.savepoints.append("released")

    async def execute(self, statement, params=None):
        self.sql.append((str(statement), params))
        rows = self.rows

        class _R:
            # A write that changed its row, as `_leave_sending`'s CAS reads.
            rowcount = 1

            def fetchall(self_inner):
                return rows

            def first(self_inner):
                return rows[0] if rows else None

            fetchone = first

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

    async def test_each_ref_gets_one_edit_with_the_line_and_no_keyboard(self, queued):
        s = _Session([("77", {"text": "h", "sent_as": "text"})])
        n = await outbox.restate_cards(
            s,
            workspace_id="ws",
            binding_id="b",
            intent_id="i",
            outcome_text="👀 Needs review",
        )
        assert n == 1
        assert queued[0]["kind"] == "prompt_supersede"
        assert queued[0]["payload"]["outcome_text"] == "👀 Needs review"
        assert "reply_markup" not in queued[0]["payload"]


class TestRestateEverywhere:
    async def test_one_statement_addresses_every_binding_by_ref(self):
        s = _Session([(3,)])
        n = await outbox.restate_everywhere(
            s, workspace_id="ws", intent_id="i", outcome_text="✅ Posted by Sam"
        )
        assert n == 3 and len(s.sql) == 1, "one round trip (#1286)"
        sql, params = s.sql[0]
        assert "channel_bindings" in sql and "state = 'active'" in sql
        assert "kind = 'approval_prompt'" in sql
        assert "('sent', 'superseded', 'ambiguous')" in sql, "by ref, any settled state"
        assert "external_message_ref IS NOT NULL" in sql
        assert "'prompt_supersede'" in sql
        assert params == {"ws": "ws", "i": "i", "o": "✅ Posted by Sam"}
        assert "reply_markup" not in sql


CARD = {"v": 2, "text": "📸 card", "outcome_text": "👀 Needs review"}

#: The `media` block of a card whose story has a picture
#: (`prompt_rendering.render_card`).
MEDIA = {
    "workspace_id": "ws",
    "source_id": "src",
    "ref": "file-1",
    "kind": "image",
    "mime": "image/jpeg",
    "file_name": "f.jpg",
}


class TestTheChatsWithoutACard:
    """`chats_without_card`: the push bindings holding no card of a story,
    in one statement."""

    async def test_a_card_counts_once_it_reached_the_chat_or_may_still(self):
        s = _Session([("b1",), ("b2",)])
        chats = await outbox.chats_without_card(s, workspace_id="ws", intent_id="i")
        assert chats == ["b1", "b2"] and len(s.sql) == 1, "one round trip"
        sql, params = s.sql[0]
        assert "FROM channel_bindings b" in sql and "NOT EXISTS" in sql
        assert "kind = 'approval_prompt'" in sql
        assert "external_message_ref IS NOT NULL" in sql
        assert "('pending', 'sending', 'ambiguous')" in sql
        assert params == {"ws": "ws", "i": "i"}


class TestThePromptForAParkedStory:
    """`prompts.prompt_review`, the park's courtesy: the review card goes to
    every chat that holds no card of the story, and is rendered only when one
    does. Every route into review gives it: the parks and the publish job's
    poison through `tell_review`, the reconciler's park on its own."""

    @pytest.fixture
    def doors(self, monkeypatch):
        from src.services.target import prompts

        seen = {"chats": ["b1", "b2"], "rendered": 0, "written": []}

        async def chats_without_card(session, *, workspace_id, intent_id):
            return seen["chats"]

        async def rerender_prompt(session, *, intent_id):
            seen["rendered"] += 1
            return CARD

        async def enqueue(session, **kw):
            seen["written"].append(kw)
            return "ob-new"

        monkeypatch.setattr(outbox, "chats_without_card", chats_without_card)
        monkeypatch.setattr(prompts, "rerender_prompt", rerender_prompt)
        monkeypatch.setattr(outbox, "enqueue", enqueue)
        return prompts, seen

    async def test_each_chat_without_a_card_gets_the_review_card(self, doors):
        prompts, seen = doors
        n = await prompts.prompt_review(_Session([]), workspace_id="ws", intent_id="i")
        assert n == 2 and seen["rendered"] == 1, "rendered once"
        assert [
            (w["binding_id"], w["kind"], w["payload"]) for w in seen["written"]
        ] == [
            ("b1", "approval_prompt", CARD),
            ("b2", "approval_prompt", CARD),
        ]
        assert {w["intent_id"] for w in seen["written"]} == {"i"}

    async def test_no_chat_without_a_card_renders_and_writes_nothing(self, doors):
        prompts, seen = doors
        seen["chats"] = []
        n = await prompts.prompt_review(_Session([]), workspace_id="ws", intent_id="i")
        assert n == 0
        assert seen["rendered"] == 0 and seen["written"] == []

    async def test_the_park_gives_its_cards_after_the_restate_and_notice(
        self, monkeypatch
    ):
        from src.services.target import prompts, publish_pipeline

        order = []

        async def restate_and_notify(session, **kw):
            order.append(("courtesy", kw["state"]))
            return 1

        async def prompt_review(session, *, workspace_id, intent_id):
            order.append(("cards", intent_id))
            return 1

        monkeypatch.setattr(publish_pipeline, "_restate_and_notify", restate_and_notify)
        monkeypatch.setattr(prompts, "prompt_review", prompt_review)
        await publish_pipeline.tell_review(
            _Session([]), workspace_id="ws", intent_id="i", tz="UTC", notice="n"
        )
        assert order == [("courtesy", "review_required"), ("cards", "i")]

    async def test_the_publish_jobs_poison_gives_the_parked_story_the_same_courtesy(
        self, monkeypatch
    ):
        """A job whose tries are spent parks its story through `tell_review`,
        at the run's own time: the restate and notice, then the review card for
        the chats that hold none, then the latch, all before the job is
        finalized."""
        from src.services.target import prompts, publish_pipeline

        now = datetime(2026, 9, 12, 13, 2, tzinfo=timezone.utc)
        job = {
            "id": "job-1",
            "lease_token": "tok",
            "workspace_id": "ws",
            "attempts": 5,
            "max_attempts": 5,
        }
        story = {
            "id": "i1",
            "eff_tz": "Europe/Paris",
            "attempts_by_step": {"v": 1, "float_since": "2026-09-12T12:28:00+00:00"},
        }
        ctx = publish_pipeline._Ctx(job, story, [])
        seen = []

        class Session(_Session):
            async def execute(self, statement, params=None):
                sql = str(statement)
                if "customer_notified" in sql:
                    seen.append(("latch",))
                elif "state = 'review_required'" in sql:
                    seen.append(("flip",))
                return await super().execute(statement, params)

        @asynccontextmanager
        async def leased_tx(uow, job):
            yield Session([("i1",)])

        async def restate_and_notify(session, **kw):
            seen.append(("courtesy", kw["state"], kw["at"], kw["tz"], kw["notice"]))
            return 1

        async def prompt_review(session, *, workspace_id, intent_id):
            seen.append(("cards", workspace_id, intent_id))
            return 1

        async def finalize_job(session, job_id, token, state):
            seen.append(("finalize", state))

        monkeypatch.setattr(publish_pipeline, "_leased_tx", leased_tx)
        monkeypatch.setattr(publish_pipeline, "_restate_and_notify", restate_and_notify)
        monkeypatch.setattr(prompts, "prompt_review", prompt_review)
        monkeypatch.setattr(publish_pipeline, "finalize_job", finalize_job)

        outcome = await publish_pipeline._retry_or_poison(
            object(),
            ctx,
            [60],
            lambda: now,
            error={"v": 1, "error": {"type": "MetaRetryableError", "code": 4}},
        )
        assert outcome == publish_pipeline.POISONED
        notice = publish_pipeline._poison_notice(ctx, now)
        assert "after 34 minutes of trying" in notice, (
            "the notice reads the run's clock"
        )
        assert seen == [
            ("flip",),
            ("courtesy", "review_required", now, "Europe/Paris", notice),
            ("cards", "ws", "i1"),
            ("latch",),
            ("finalize", "review_required"),
        ]

    async def test_the_reconcilers_park_gives_its_cards_after_the_restate_and_no_notice(
        self, monkeypatch
    ):
        """The reconciler's park restates every card of the story by ref, then
        gives the review card to the chats that hold none, with ids as text.
        It writes no notice: that comes after the notify window."""
        from uuid import UUID

        from src.services.target import prompts, reconciler

        workspace, story = UUID(int=1), UUID(int=2)
        seen = []

        async def restate_everywhere(session, *, workspace_id, intent_id, outcome_text):
            seen.append(("restate", workspace_id, intent_id))
            return 1

        async def prompt_review(session, *, workspace_id, intent_id):
            seen.append(("cards", workspace_id, intent_id))
            return 1

        async def fanout_notification(session, **kw):
            seen.append(("notice", kw))

        monkeypatch.setattr(outbox, "restate_everywhere", restate_everywhere)
        monkeypatch.setattr(prompts, "prompt_review", prompt_review)
        monkeypatch.setattr(outbox, "fanout_notification", fanout_notification)

        conn = _Session([("UTC",)])
        await reconciler._park_review_required(
            conn, intent_id=story, workspace_id=workspace, checks=4
        )
        assert len(conn.sql) == 1, "the park's flip is the only write of its own"
        flip, params = conn.sql[0]
        assert "state = 'review_required'" in flip and params["intent"] == str(story)
        assert seen == [
            ("restate", str(workspace), str(story)),
            ("cards", str(workspace), str(story)),
        ]

    async def test_a_review_card_that_cannot_be_written_leaves_the_park_standing(
        self, monkeypatch, caplog
    ):
        """`prompt_review` writes in a savepoint of its own, for every park: a
        card that cannot be written is rolled back there and logged with its
        cause, and nothing is raised, so the caller's park stands."""
        import logging

        from src.services.target import prompts

        async def chats_without_card(session, *, workspace_id, intent_id):
            raise RuntimeError("card read failed")

        monkeypatch.setattr(outbox, "chats_without_card", chats_without_card)
        session = _Session([])
        with caplog.at_level(logging.ERROR, logger=prompts.__name__):
            written = await prompts.prompt_review(
                session, workspace_id="ws", intent_id="i1"
            )
        assert written == 0
        assert session.savepoints == ["savepoint", "rolled back"]
        [record] = [r for r in caplog.records if r.name == prompts.__name__]
        assert "i1" in record.getMessage() and record.exc_info is not None


class TestTheReviewRestateSaysWhy:
    """`publish_pipeline._restate_and_notify`, the restate a poison and every
    park send: a story in review has its cards restated with its line and,
    under it, why it was refused, from the refusal its `last_error` records
    (`intent_ledger.settlement`), as the card rendered again says."""

    AT = datetime(2026, 9, 12, 13, 2, tzinfo=timezone.utc)
    IN_REVIEW = "👀 Needs review · 2026-09-12 13:02 UTC"
    DEAD = {
        "v": 1,
        "kind": "container_dead",
        "status_code": "ERROR",
        "status": "Error: Media upload has failed with error code 2207026",
    }

    @pytest.fixture
    def doors(self, monkeypatch):
        from src.services.target import intent_ledger, prompts, publish_pipeline

        seen = {
            "refusal": None,
            "bindings": ["b1", "b2"],
            "reads": [],
            "lines": [],
            "notices": [],
        }

        async def settlement(session, *, workspace_id, intent_id):
            seen["reads"].append((workspace_id, intent_id))
            return {"state": "review_required", "refusal": seen["refusal"]}

        async def push_bindings(session, workspace_id):
            return seen["bindings"]

        async def restate_cards(
            session, *, workspace_id, binding_id, intent_id, outcome_text
        ):
            seen["lines"].append((binding_id, outcome_text))
            return 1

        async def fanout_notification(
            session, *, workspace_id, bindings, text, intent_id
        ):
            seen["notices"].append(text)

        monkeypatch.setattr(intent_ledger, "settlement", settlement)
        monkeypatch.setattr(prompts, "push_bindings", push_bindings)
        monkeypatch.setattr(outbox, "restate_cards", restate_cards)
        monkeypatch.setattr(outbox, "fanout_notification", fanout_notification)
        return publish_pipeline, seen

    async def _restate(self, publish_pipeline, state):
        return await publish_pipeline._restate_and_notify(
            None,
            workspace_id="ws",
            intent_id="i",
            state=state,
            at=self.AT,
            tz="UTC",
            notice="n",
        )

    async def test_a_dead_containers_cards_say_why_it_was_refused(self, doors):
        publish_pipeline, seen = doors
        seen["refusal"] = self.DEAD
        assert await self._restate(publish_pipeline, "review_required") == 2
        line = (
            f"{self.IN_REVIEW}\nRefused: Instagram could not process this story's"
            " media (ERROR): Error: Media upload has failed with error code 2207026."
        )
        assert seen["lines"] == [("b1", line), ("b2", line)]
        assert seen["reads"] == [("ws", "i")], "one read, under the story's tenant"
        assert seen["notices"] == ["n"]

    async def test_a_story_in_review_whose_refusal_says_nothing_keeps_its_line(
        self, doors
    ):
        publish_pipeline, seen = doors
        await self._restate(publish_pipeline, "review_required")
        assert seen["lines"] == [("b1", self.IN_REVIEW), ("b2", self.IN_REVIEW)]

    async def test_any_other_states_line_reads_no_refusal(self, doors):
        from src.services.target import prompts

        publish_pipeline, seen = doors
        seen["refusal"] = self.DEAD
        await self._restate(publish_pipeline, "posted")
        line = prompts.outcome_line("posted", by=None, at=self.AT, tz="UTC")
        assert seen["reads"] == [] and seen["lines"][0] == ("b1", line)

    async def test_a_workspace_no_chat_can_hear_reads_no_refusal(self, doors):
        """No push binding: no card to restate, so the refusal is not read."""
        publish_pipeline, seen = doors
        seen["refusal"], seen["bindings"] = self.DEAD, []
        assert await self._restate(publish_pipeline, "review_required") == 0
        assert seen["reads"] == [] and seen["lines"] == []


class TestACardLandingOnAMovedIntentIsEditedAtOnce:
    """`_edit_sent_card` (after a send): a card whose intent moved while it
    was in flight is edited at once. The edit picks no keyboard: the sender
    renders it when it claims the edit, from the story as it is then
    (#1477), so a parked story's card keeps its review buttons because it is
    `review_required`."""

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

    @pytest.mark.parametrize(
        "state, word", [("review_required", "Needs review"), ("posted", "Posted")]
    )
    async def test_the_edit_carries_its_line_and_no_keyboard_of_its_own(
        self, seams, state, word
    ):
        seams["state"] = state
        payload = await self._land(seams)
        assert word in payload["outcome_text"]
        assert "reply_markup" not in payload, "the sender renders it at claim"


class TestACardIsRenderedAgainAtClaimTime:
    """`_claim_current`: every claimed `approval_prompt` with an
    intent is rendered again before the send; a slot that moved on is retired
    unsent and the next row claimed; a live one goes out as the fresh card,
    written through the one `sending` fence. After the pacing, a card leaves
    with no keyboard and every edit of one gets its story's (#1477)."""

    def test_the_rule(self):
        assert outbox._needs_refresh({"kind": "approval_prompt", "intent_id": "i"})
        assert not outbox._needs_refresh({"kind": "prompt_supersede", "intent_id": "i"})
        assert not outbox._needs_refresh({"kind": "notification", "intent_id": "i"})
        assert not outbox._needs_refresh({"kind": "approval_prompt", "intent_id": None})

    def test_the_keyboards_rule(self):
        keyed = outbox._takes_the_storys_keyboard
        assert keyed({"kind": "approval_prompt", "intent_id": "i"})
        assert keyed({"kind": "prompt_supersede", "intent_id": "i"})
        assert not keyed({"kind": "notification", "intent_id": "i"})
        # An invitation's edit carries no intent, and is left as it was.
        assert not keyed({"kind": "prompt_supersede", "intent_id": None})

    @pytest.fixture
    def claims(self, monkeypatch):
        from src.services.target import prompts

        seen = {
            "claimed": [],
            "left": [],
            "rendered": {},
            "facts": {},
            "keyed": [],
            "paced": False,
            "cards": {},
            "asked": [],
        }

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
            return None if seen["paced"] else 1

        async def card_facts(session, *, workspace_id, intent_id):
            seen["keyed"].append(intent_id)
            return seen["facts"].get(intent_id)

        async def _card_of(session, row, *, ref):
            seen["asked"].append(ref)
            return seen["cards"].get(ref)

        # The real `card_keyboard_for` and `keyboard_of` run over the story's
        # facts: what the sender sends is `card_keyboard`'s, per state.
        monkeypatch.setattr(prompts, "card_facts", card_facts)
        monkeypatch.setattr(outbox, "_card_of", _card_of, raising=False)
        monkeypatch.setattr(outbox, "recover_stranded", recover_stranded)
        monkeypatch.setattr(outbox, "resolve_aged_ambiguous", resolve_aged_ambiguous)
        monkeypatch.setattr(outbox, "claim_next", claim_next)
        monkeypatch.setattr(outbox, "_leave_sending", _leave_sending)
        monkeypatch.setattr(prompts, "rerender_prompt", rerender_prompt)
        monkeypatch.setattr(outbox, "increment", increment)
        return seen

    async def _claim(self):
        return await outbox.pace_and_claim(
            _ClaimSession(),
            binding_id="b",
            now=datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc),
            chat_limit=20,
            chat_window_seconds=60,
            global_limit=30,
            global_window_seconds=1,
        )

    async def test_a_stale_card_is_retired_and_the_next_row_is_claimed(self, claims):
        claims["claimed"] = [
            _claimed("approval_prompt", "gone", {}, attempts=2),
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

    async def test_a_live_card_goes_out_as_the_fresh_card_without_a_keyboard(
        self, claims
    ):
        """A card leaves with no keyboard, whatever its render carries and
        whatever its story is: the keyboard reaches the chat only as the edit
        of a message whose id the ledger holds, so a copy a lost answer leaves
        behind has no buttons. The card never asks for its story's keyboard,
        and its record is the card as sent."""
        fresh = {"v": 2, "text": "new", "reply_markup": {"inline_keyboard": []}}
        claims["claimed"] = [
            _claimed("approval_prompt", "live", {"v": 2, "text": "old"}),
        ]
        claims["rendered"] = {"live": fresh}
        claims["facts"] = {"live": _facts()}
        row = await self._claim()
        sent = {"v": 2, "text": "new"}
        assert row is not None and row["payload"] == sent
        assert claims["left"] == [("ob-1", "sending", {"payload": sent})], (
            "the record is rewritten through the one fence, in the claim transaction"
        )
        assert claims["keyed"] == [], "a card does not read its story's keyboard"

    async def test_a_cards_edit_gets_the_storys_keyboard_as_it_is_now(self, claims):
        """#1477: a `prompt_supersede` of an approval card leaves with the
        keyboard of its story as it is when the edit is claimed. This test
        claims a parked story's edit: the three resolutions, then Configure.
        A rejected story's edit, which drops its caller's keyboard, is
        `test_a_rejected_cards_edit_carries_no_keyboard`. The seven cases of
        `test_every_states_edit_leaves_with_its_storys_keyboard` are an
        approved, a posted, a skipped, a parked, a cancelling and a rejected
        story's edit, and a removed item's."""
        claims["claimed"] = [
            _claimed(
                "prompt_supersede",
                PARKED,
                {"v": 1, "supersedes_ref": "77", "outcome_text": "👀"},
            ),
        ]
        claims["facts"] = {PARKED: _facts("review_required")}
        row = await self._claim()
        assert [
            [b["callback_data"] for b in r]
            for r in row["payload"]["reply_markup"]["inline_keyboard"]
        ] == [
            [f"v1:itposted:{PARKED}", f"v1:notposted:{PARKED}"],
            [f"v1:giveup:{PARKED}"],
            [f"v1:cfg:{PARKED}"],
        ]

    async def test_a_rejected_cards_edit_carries_no_keyboard(self, claims):
        claims["claimed"] = [
            _claimed(
                "prompt_supersede",
                "rejected",
                {
                    "v": 1,
                    "supersedes_ref": "77",
                    "reply_markup": {"inline_keyboard": [[{"text": "stale"}]]},
                },
            ),
        ]
        claims["facts"] = {"rejected": _facts("rejected")}
        row = await self._claim()
        assert "reply_markup" not in row["payload"], "no caller's keyboard survives"

    @pytest.mark.parametrize("kind", ["approval_prompt", "prompt_supersede"])
    async def test_a_story_the_session_cannot_see_goes_without_a_keyboard(
        self, claims, kind
    ):
        """`card_facts` reads nothing when the session cannot see the story:
        `card_keyboard_for` answers None, and an edit leaves with no keyboard.
        A raise here would roll the claim back at every tick and stall the
        chat's queue behind the row. A card never asks: it leaves with no
        keyboard whatever the story's facts are, and its record is written as
        it was sent. (`_claim_current` reads a card's story first
        (`_CARD_SELECT`) and retires one it cannot see as `superseded`, so only
        an outbox row whose workspace is not its story's, which no writer
        makes, brings a card here.)"""
        stale = {"inline_keyboard": [[{"text": "stale"}]]}
        claims["claimed"] = [
            _claimed(
                kind, "unseen", {"v": 1, "supersedes_ref": "77", "reply_markup": stale}
            ),
        ]
        claims["rendered"] = {"unseen": {"v": 2, "text": "new", "reply_markup": stale}}
        claims["facts"] = {}
        row = await self._claim()
        assert "reply_markup" not in row["payload"]
        assert claims["keyed"] == (["unseen"] if kind == "prompt_supersede" else []), (
            "an edit reads its story, and reads nothing; a card never asks"
        )
        assert claims["left"] == (
            [("ob-1", "sending", {"payload": row["payload"]})]
            if kind == "approval_prompt"
            else []
        ), "a card's record is written as it was sent; an edit's is not"

    async def test_a_cards_edit_is_not_written_back(self, claims):
        """Only the transport reads an edit's keyboard, from the claimed row; a
        resent edit is claimed, and given one, again."""
        claims["claimed"] = [
            _claimed("prompt_supersede", "posted", {"v": 1, "supersedes_ref": "77"}),
        ]
        claims["facts"] = {"posted": _facts("posted")}
        row = await self._claim()
        assert row["payload"]["reply_markup"] == {
            "inline_keyboard": [
                [{"text": "⚙️ Configure", "callback_data": "v1:cfg:posted"}]
            ]
        }
        assert claims["left"] == []

    @pytest.mark.parametrize("kind", ["approval_prompt", "prompt_supersede"])
    async def test_a_paced_attempt_reads_and_writes_no_keyboard(self, claims, kind):
        """The keyboard comes after the pacing, which it never changes: a chat
        whose budget is spent polls on, and each refused attempt does no
        keyboard work before its rollback."""
        claims["claimed"] = [
            _claimed(kind, "live", {"v": 1, "supersedes_ref": "77"}),
        ]
        claims["rendered"] = {"live": {"v": 2, "text": "new"}}
        claims["facts"] = {"live": _facts()}
        claims["paced"] = True
        with pytest.raises(OutboxPaced):
            await self._claim()
        assert claims["keyed"] == [] and claims["left"] == []

    @pytest.mark.parametrize(
        "facts, tokens",
        [
            (_facts("approved"), ["cfg"]),
            (_facts("posted"), ["cfg"]),
            (_facts("skipped"), ["cfg"]),
            (_facts("review_required"), ["itposted", "notposted", "giveup", "cfg"]),
            # A pending cancel: on its way to `cancelled`, never the decision
            # buttons back.
            (_facts("awaiting_approval", cancel=True), ["cfg"]),
            (_facts("rejected"), None),
            (_facts("posted", item_state="removed"), None),
        ],
    )
    async def test_every_states_edit_leaves_with_its_storys_keyboard(
        self, claims, facts, tokens
    ):
        """#1477, through the real keyboard read: an edit claimed for an
        approved, posted or skipped story carries Configure alone, a parked
        one the three resolutions and Configure, a cancelling one Configure
        alone; a rejected story's or a removed item's carries none."""
        claims["claimed"] = [
            _claimed(
                "prompt_supersede",
                "i",
                {"v": 1, "supersedes_ref": "77", "outcome_text": "x"},
            ),
        ]
        claims["facts"] = {"i": facts}
        row = await self._claim()
        keyboard = row["payload"].get("reply_markup")
        if tokens is None:
            assert keyboard is None
        else:
            assert [
                b["callback_data"].split(":")[1]
                for r in keyboard["inline_keyboard"]
                for b in r
            ] == tokens

    async def test_an_invitations_edit_is_left_as_it_was(self, claims):
        claims["claimed"] = [
            _claimed("prompt_supersede", None, {"v": 1, "supersedes_ref": "77"}),
        ]
        row = await self._claim()
        assert row["payload"] == {"v": 1, "supersedes_ref": "77"}
        assert claims["keyed"] == [] and claims["left"] == []

    async def test_a_cards_edit_carries_the_cards_media_while_it_is_unconfirmed(
        self, claims
    ):
        """The card's record is the one truth about its media, as the story's
        facts are about its keyboard: an edit of a card whose record holds a
        `media` block and does not say `media` is given that block in memory
        (the transport sends it as the media edit), with the card's `sent_as`.
        Nothing of it is written to the edit row."""
        claims["claimed"] = [
            _claimed("prompt_supersede", "i", {"v": 1, "supersedes_ref": "77"}),
        ]
        claims["facts"] = {"i": _facts()}
        claims["cards"] = {
            "77": {"text": "t", "caption": "c", "media": MEDIA, "sent_as": "text"}
        }
        row = await self._claim()
        assert row["payload"]["media"] == MEDIA
        assert row["payload"]["sent_as"] == "text"
        assert claims["asked"] == ["77"]
        assert claims["left"] == [], "an edit's record is not written back"

    async def test_once_the_media_is_confirmed_the_edit_carries_none(self, claims):
        """The card's record says `media` once an edit put the picture on the
        message: the edits after it are caption edits, as the card says."""
        claims["claimed"] = [
            _claimed(
                "prompt_supersede",
                "i",
                {
                    "v": 1,
                    "supersedes_ref": "77",
                    "outcome_text": "x",
                    "sent_as": "text",
                },
            ),
        ]
        claims["facts"] = {"i": _facts()}
        claims["cards"] = {"77": {"text": "t", "media": MEDIA, "sent_as": "media"}}
        row = await self._claim()
        assert "media" not in row["payload"]
        assert row["payload"]["sent_as"] == "media"

    async def test_a_card_whose_media_was_dropped_gives_its_edit_none(self, claims):
        """A card whose upload was given up (refused, unfetchable, or its
        answer lost) is text in its record, with no block: whatever its edit
        row carried gives way to the record, so no edit uploads again."""
        claims["claimed"] = [
            _claimed(
                "prompt_supersede",
                "i",
                {"v": 1, "supersedes_ref": "77", "sent_as": "media", "media": MEDIA},
            ),
        ]
        claims["facts"] = {"i": _facts()}
        claims["cards"] = {"77": {"text": "t", "sent_as": "text"}}
        row = await self._claim()
        assert "media" not in row["payload"]
        assert row["payload"]["sent_as"] == "text"

    async def test_a_settled_cards_media_edit_carries_the_line_the_card_shows(
        self, claims
    ):
        """The media edit rewrites the whole message, so it carries the line
        the card shows: the edit's own when it has one, else the card's."""
        claims["claimed"] = [
            _claimed("prompt_supersede", "i", {"v": 1, "supersedes_ref": "77"}),
            _claimed(
                "prompt_supersede",
                "i",
                {"v": 1, "supersedes_ref": "77", "outcome_text": "✅ Posted"},
            ),
        ]
        claims["facts"] = {"i": _facts()}
        claims["cards"] = {
            "77": {
                "text": "t",
                "media": MEDIA,
                "sent_as": "text",
                "outcome_text": "✅ Approved",
            }
        }
        without = await self._claim()
        assert without["payload"]["outcome_text"] == "✅ Approved"
        own = await self._claim()
        assert own["payload"]["outcome_text"] == "✅ Posted"

    async def test_a_card_never_asks_for_its_own_record(self, claims):
        """Only an edit of a card reads the card's record: the card itself has
        none before it is sent, and an invitation's edit, which names no story,
        is left as it was."""
        claims["claimed"] = [
            _claimed("approval_prompt", "live", {"v": 2, "text": "old"}),
            _claimed("prompt_supersede", "live", {"v": 1, "supersedes_ref": "77"}),
            _claimed("prompt_supersede", None, {"v": 1, "supersedes_ref": "78"}),
        ]
        claims["rendered"] = {"live": {"v": 2, "text": "new"}}
        claims["facts"] = {"live": _facts()}
        await self._claim()
        assert claims["asked"] == []
        await self._claim()
        assert claims["asked"] == ["77"], "the edit of a card asks for it"
        await self._claim()
        assert claims["asked"] == ["77"], "an intent-less edit asks for nothing"


class TestAConfirmedEditRecordsHowTheCardStands:
    """`settle` of an edit of a card: the card's own record says how its
    message now stands (`_record_cards_media`), in the write after the edit's
    own. A confirmed edit says it by its receipt; an edit that carried the
    card's media and lost its answer, or was refused, gives the upload up."""

    EDIT = {
        "id": "ob-9",
        "kind": "prompt_supersede",
        "intent_id": "i",
        "workspace_id": "ws",
        "binding_id": "b",
        "attempts": 1,
        "payload": {"v": 1, "supersedes_ref": "77", "sent_as": "text"},
    }
    CARRYING = {**EDIT, "payload": {**EDIT["payload"], "media": MEDIA}}
    NOTIFICATION = {
        "id": "ob-8",
        "kind": "notification",
        "intent_id": None,
        "workspace_id": "ws",
        "binding_id": "b",
        "attempts": 1,
        "payload": {"v": 1, "text": "n"},
    }

    @staticmethod
    def _receipt(sent_as):
        from src.channels.telegram_transport import SendReceipt

        return SendReceipt("77", sent_as=sent_as)

    @staticmethod
    def _lost():
        from src.channels.telegram_transport import TelegramSendError

        return TelegramSendError("editMessageMedia: transport failure")

    @staticmethod
    def _refused():
        from src.channels.telegram_transport import TelegramRefused

        return TelegramRefused(
            "editMessageText: 400 Bad Request: message to edit not found", code=400
        )

    @staticmethod
    def _auth_dead():
        from src.channels.telegram_transport import TelegramAuthDead

        return TelegramAuthDead("editMessageMedia: 401 Unauthorized", code=401)

    @staticmethod
    def _recorded(session):
        """The card's write, the session's last statement, whitespace folded."""
        sql, params = session.sql[-1]
        return " ".join(sql.split()), params

    @pytest.mark.parametrize("how", ["media", "text"])
    async def test_a_confirmed_edit_records_how_the_message_stands(self, how):
        s = _Session([])
        await outbox.settle(s, self.EDIT, receipt=self._receipt(how))
        assert len(s.sql) == 2, "the edit's own write, then the card's"
        sql, params = self._recorded(s)
        assert sql.startswith("UPDATE channel_outbox SET payload = CASE")
        assert "payload || jsonb_build_object('sent_as', 'media')" in sql
        assert "(payload - 'media') || jsonb_build_object('sent_as', 'text')" in sql
        assert sql.endswith("kind = 'approval_prompt' AND external_message_ref = :ref")
        assert params == {"how": how, "ws": "ws", "b": "b", "i": "i", "ref": "77"}

    async def test_only_an_edit_that_says_how_the_message_stands_records_it(self):
        said = _Session([])
        await outbox.settle(said, self.EDIT, receipt=self._receipt("media"))
        assert len(said.sql) == 2, "positive control: this one records"
        for row, how in ((self.EDIT, "edit"), (self.NOTIFICATION, "text")):
            s = _Session([])
            await outbox.settle(s, row, receipt=self._receipt(how))
            assert len(s.sql) == 1, (row["kind"], how)

    async def test_a_lost_answer_on_the_media_edit_gives_the_upload_up(self):
        s = _Session([])
        result = await outbox.settle(s, self.CARRYING, error=self._lost())
        assert result["state"] == "ambiguous"
        assert len(s.sql) == 2, "the ambiguous CAS, then the card's record"
        assert s.sql[0][1]["s"] == "ambiguous"
        sql, params = self._recorded(s)
        assert "(payload - 'media') || jsonb_build_object('sent_as', 'text')" in sql
        assert params == {"how": "text", "ws": "ws", "b": "b", "i": "i", "ref": "77"}

    async def test_only_an_edit_that_carried_the_media_gives_it_up_on_a_lost_answer(
        self,
    ):
        carrying = _Session([])
        await outbox.settle(carrying, self.CARRYING, error=self._lost())
        assert len(carrying.sql) == 2, "positive control: this one records"
        for row in (self.EDIT, self.NOTIFICATION):
            s = _Session([])
            await outbox.settle(s, row, error=self._lost())
            assert len(s.sql) == 1, row["kind"]

    async def test_a_refused_media_edit_gives_the_upload_up(self):
        s = _Session([])
        result = await outbox.settle(s, self.CARRYING, error=self._refused())
        assert result["state"] == "failed"
        assert len(s.sql) == 2, "the failed CAS, then the card's record"
        assert s.sql[0][1]["s"] == "failed"
        sql, params = self._recorded(s)
        assert "(payload - 'media') || jsonb_build_object('sent_as', 'text')" in sql
        assert params == {"how": "text", "ws": "ws", "b": "b", "i": "i", "ref": "77"}

    async def test_a_dead_credential_keeps_the_media_the_card_is_owed(self):
        refused = _Session([])
        await outbox.settle(refused, self.CARRYING, error=self._refused())
        assert len(refused.sql) == 2, "positive control: a refusal records"
        s = _Session([])
        result = await outbox.settle(s, self.CARRYING, error=self._auth_dead())
        assert result["state"] == "failed"
        assert len(s.sql) == 1, "the failed CAS alone: the card keeps its block"


@pytest.fixture(autouse=True)
def source_prefix_seam(monkeypatch):
    """Transport/ledger doubles do not impersonate the shared SQL authority."""
    from unittest.mock import AsyncMock

    monkeypatch.setattr(outbox, "_lock_binding_sources", AsyncMock())


async def test_pending_media_card_is_deferred_without_superseding(monkeypatch):
    from src.services.target import prompts, source_policy
    from unittest.mock import ANY, AsyncMock
    from contextlib import asynccontextmanager

    record = []
    row = {
        "id": "row",
        "kind": "approval_prompt",
        "intent_id": "intent",
        "attempts": 1,
        "payload": {},
    }
    monkeypatch.setattr(outbox, "recover_stranded", AsyncMock(return_value=[]))
    monkeypatch.setattr(outbox, "resolve_aged_ambiguous", AsyncMock(return_value=[]))
    monkeypatch.setattr(outbox, "claim_next", AsyncMock(side_effect=[row, None]))
    monkeypatch.setattr(
        prompts,
        "rerender_prompt",
        AsyncMock(
            side_effect=source_policy.MediaAdmissionPending("validation_pending")
        ),
    )
    leave = AsyncMock()
    monkeypatch.setattr(outbox, "_leave_sending", leave)

    class PendingSession(_ClaimSession):
        @asynccontextmanager
        async def begin_nested(self):
            try:
                yield
            except source_policy.MediaAdmissionPending:
                record.append("claim_rollback")
                raise

        async def commit(self):
            record.append("commit")

    touch = AsyncMock()
    monkeypatch.setattr(outbox, "_touch_media_deferred", touch)

    @asynccontextmanager
    async def sessions():
        yield PendingSession()

    transport = AsyncMock()
    poller = outbox.OutboxPoller(
        sessions,
        binding_id="binding",
        transport=transport,
        clock=lambda: datetime.now(timezone.utc),
        interval_seconds=1,
        chat_limit=10,
        chat_window_seconds=60,
        global_limit=10,
        global_window_seconds=60,
    )
    assert await poller.tick() is None
    assert poller.deferred == 1 and poller.consecutive_failures == 0
    assert record == ["claim_rollback", "commit"]
    touch.assert_awaited_once_with(ANY, outbox_id="row")
    leave.assert_not_awaited()
    transport.assert_not_awaited()
