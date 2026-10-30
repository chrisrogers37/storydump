"""Prompt production: the card, designed first-principles: a tap
PUBLISHES (post-now is the user contract; internals may queue), and the
button set serves the next state, with the live loop preserved:
autopost/skip/posted/reject are the four callbacks production actually uses,
and none may be dropped silently.

The card: **Post now** (iff the workspace can publish via API) ·
**Posted myself** (the manual-mode path the matrix specifies at
awaiting→posted) · **Skip** · **Reject**, plus the zero-backend Instagram
deeplink button.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.services.target import (
    intent_ledger,
    prompt_rendering,
    prompts,
    refusal_evidence,
    vocabulary,
)
from tests.src.services.target.schema_code import function_body


def _card_input(**over):
    base = dict(
        intent_id="11111111-2222-3333-4444-555555555555",
        file_name="sunset.jpg",
        media_kind="image",
        schedule_slot_at="2026-08-21T18:30:00+00:00",
        tz="America/New_York",
    )
    base.update(over)
    return base


def _buttons(payload):
    return [b for row in payload["reply_markup"]["inline_keyboard"] for b in row]


class TestStringSlotWidths:
    """The defensive str branch must survive every fractional width PG emits.

    Postgres strips trailing fractional zeros in timestamptz text/jsonb
    rendering, so a microsecond value arrives at any width 0-6; at the
    repository's 3.10 floor a bare ``fromisoformat`` rejects widths 1, 2,
    4 and 5. The five-digit fraction ``.05024`` exercises that case.

    Bound, stated rather than implied: the call-site leg below has teeth
    ONLY at the 3.10 floor — on 3.11+ every width parses natively, so a
    green on a dev interpreter is not evidence for it. CI runs the floor.
    The helper leg asserts the canonicalized string itself and goes red
    under mutation on every interpreter.
    """

    WIDTHS = {
        0: "2026-08-21T17:00:00+00:00",
        1: "2026-08-21T17:00:00.5+00:00",
        2: "2026-08-21T17:00:00.05+00:00",
        3: "2026-08-21T17:00:00.050+00:00",
        4: "2026-08-21T17:00:00.0502+00:00",
        5: "2026-08-21T17:00:00.05024+00:00",
        6: "2026-08-21T17:00:00.050240+00:00",
    }

    @staticmethod
    def _canonical6(raw: str) -> str:
        """The width-6 form of this value (each width is its own instant —
        PG strips trailing zeros of one value, so one instant yields one
        stripped width; widths here are distinct values by construction)."""
        if "." not in raw:
            return raw
        head, tail = raw.split(".", 1)
        num = tail[
            : next((i for i, c in enumerate(tail) if not c.isdigit()), len(tail))
        ]
        rest = tail[len(num) :]
        return f"{head}.{num.ljust(6, '0')}{rest}"

    @pytest.mark.parametrize("width", sorted(WIDTHS))
    def test_every_width_renders_the_same_slot_as_the_datetime_form(self, width):
        raw = self.WIDTHS[width]
        via_str = prompts.render_card(
            _card_input(schedule_slot_at=raw), api_publishing_enabled=True
        )
        via_dt = prompts.render_card(
            _card_input(schedule_slot_at=datetime.fromisoformat(self._canonical6(raw))),
            api_publishing_enabled=True,
        )
        assert via_str["text"] == via_dt["text"], (
            f"width {width} parsed to a different rendered slot"
        )

    def test_canonical_fraction_pads_stripped_widths_and_passes_full_ones(self):
        for width, raw in self.WIDTHS.items():
            got = prompt_rendering._canonical_fraction(raw)
            if width in (0, 6):
                assert got == raw, f"width {width} must pass through untouched"
            else:
                assert got == self._canonical6(raw), (
                    f"width {width} not canonicalized: {got}"
                )
                assert len(got.split(".", 1)[1].split("+")[0]) == 6
            datetime.fromisoformat(got)


class TestCardRender:
    def test_api_enabled_card_carries_the_four_actions_plus_deeplink(self):
        payload = prompts.render_card(_card_input(), api_publishing_enabled=True)
        assert payload["v"] == 2 and payload["text"]
        labels = [b.get("text") for b in _buttons(payload)]
        assert any("Post now" in n for n in labels), "a tap publishes"
        assert any("Posted" in n for n in labels)
        assert any("Skip" in n for n in labels)
        assert any("Reject" in n for n in labels)
        assert any(b.get("url") for b in _buttons(payload)), "deeplink button stays"

    def test_api_disabled_card_has_no_post_now(self):
        payload = prompts.render_card(_card_input(), api_publishing_enabled=False)
        labels = [b.get("text", "") for b in _buttons(payload)]
        assert not any("Post now" in n for n in labels)
        assert any("Posted" in n for n in labels)

    def test_callback_tokens_are_versioned_and_within_telegrams_64_bytes(self):
        payload = prompts.render_card(_card_input(), api_publishing_enabled=True)
        datas = [b["callback_data"] for b in _buttons(payload) if "callback_data" in b]
        assert datas, "action buttons must carry callback data"
        for d in datas:
            assert d.startswith("v1:"), d
            assert d.endswith("11111111-2222-3333-4444-555555555555")
            assert len(d.encode()) <= 64, "Telegram refuses callback_data > 64 bytes"
        actions = {d.split(":")[1] for d in datas}
        assert actions == {"post", "posted", "skip", "reject", "cfg"}

    def test_the_text_names_the_media_and_the_slot_in_workspace_time(self):
        payload = prompts.render_card(_card_input(), api_publishing_enabled=True)
        assert "sunset.jpg" in payload["text"]
        assert "14:30" in payload["text"], "slot renders in the workspace tz, not UTC"

    def test_the_payload_is_json_serializable_for_the_outbox(self):
        payload = prompts.render_card(_card_input(), api_publishing_enabled=True)
        json.dumps(payload)


class TestPromptIntent:
    async def test_transitions_first_then_enqueues_one_card_per_active_binding(
        self, monkeypatch
    ):
        order = []

        async def fake_transition(session, intent_id, to_state):
            order.append(("transition", intent_id, to_state))

        async def fake_enqueue(session, **kwargs):
            order.append(("enqueue", kwargs))
            return f"ob-{len(order)}"

        monkeypatch.setattr(prompts.intent_ledger, "transition", fake_transition)
        monkeypatch.setattr(prompts.outbox, "enqueue", fake_enqueue)

        intent = _card_input()
        row = {
            "id": intent["intent_id"],
            "workspace_id": "ws-1",
            "file_name": "sunset.jpg",
            "media_kind": "image",
            "schedule_slot_at": intent["schedule_slot_at"],
            "tz": "America/New_York",
            "api_publishing_enabled": True,
        }
        bindings = [{"id": "b-1"}, {"id": "b-2"}]
        await prompts.prompt_intent(object(), row, bindings)

        assert order[0] == ("transition", intent["intent_id"], "prompt_pending")
        enqueues = [o for o in order if o[0] == "enqueue"]
        assert len(enqueues) == 2
        for _, kwargs in enqueues:
            assert kwargs["kind"] == "approval_prompt"
            assert kwargs["intent_id"] == intent["intent_id"]
            assert kwargs["workspace_id"] == "ws-1"
            assert "Post now" in str(kwargs["payload"])
        assert {k["binding_id"] for _, k in enqueues} == {"b-1", "b-2"}

    async def test_no_active_binding_still_transitions_and_enqueues_nothing(
        self, monkeypatch
    ):
        """The web queue is a surface: a workspace with no push binding
        still has somewhere to act, so the intent leaves `scheduled` and simply
        gets no card."""
        called = []

        async def fake_transition(session, intent_id, to_state):
            called.append(to_state)

        async def fake_enqueue(session, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("no binding, no card")

        monkeypatch.setattr(prompts.intent_ledger, "transition", fake_transition)
        monkeypatch.setattr(prompts.outbox, "enqueue", fake_enqueue)
        row = {"id": "i-1", "workspace_id": "ws-1", "api_publishing_enabled": False}
        await prompts.prompt_intent(object(), row, [])
        assert called == ["prompt_pending"]


class TestTheCardCarriesTheMedia:
    """The approval card is the PHOTO (or video) with the account and the slot
    as its caption — not a filename. The renderer names the media for the
    transport to fetch; the text stays as the fallback card when the file
    cannot be sent."""

    def _with_media(self, **over):
        fields = dict(
            handle="example.brand",
            workspace_id="ws-1",
            source_id="src-1",
            provider_file_ref="file-1",
            mime_type="image/jpeg",
        )
        fields.update(over)
        return _card_input(**fields)

    def test_the_payload_names_the_media_for_the_transport(self):
        payload = prompts.render_card(self._with_media(), api_publishing_enabled=False)
        assert payload["v"] == 2
        assert payload["media"] == {
            "workspace_id": "ws-1",
            "source_id": "src-1",
            "ref": "file-1",
            "kind": "image",
            "mime": "image/jpeg",
            "file_name": "sunset.jpg",
        }

    def test_the_caption_is_the_account_and_the_slot_in_workspace_time(self):
        payload = prompts.render_card(self._with_media(), api_publishing_enabled=False)
        assert payload["caption"].startswith("📸 @example.brand")
        assert "14:30" in payload["caption"]
        assert "sunset.jpg" not in payload["caption"], "the file name is not the story"

    def test_without_a_handle_the_caption_falls_back_to_the_file_name(self):
        payload = prompts.render_card(
            self._with_media(handle=None), api_publishing_enabled=False
        )
        assert payload["caption"].startswith("📸 sunset.jpg")

    def test_the_text_remains_the_fallback_card(self):
        payload = prompts.render_card(self._with_media(), api_publishing_enabled=False)
        assert "sunset.jpg" in payload["text"] and "14:30" in payload["text"]

    def test_a_trimmed_video_says_so_in_its_text_and_its_caption(self):
        payload = prompts.render_card(
            self._with_media(
                media_kind="video", mime_type="video/mp4", trimmed_to_seconds="60"
            ),
            api_publishing_enabled=False,
        )
        assert "✂️ Trimmed to 60 s" in payload["text"]
        assert "✂️ Trimmed to 60 s" in payload["caption"]

    def test_a_card_says_nothing_of_a_trim_it_did_not_have(self):
        payload = prompts.render_card(self._with_media(), api_publishing_enabled=False)
        assert "Trimmed" not in payload["text"] + payload["caption"]

    def test_an_intent_without_a_file_reference_renders_a_text_only_card(self):
        payload = prompts.render_card(_card_input(), api_publishing_enabled=False)
        assert "media" not in payload and "caption" not in payload

    def test_the_media_payload_is_json_serializable(self):
        json.dumps(prompts.render_card(self._with_media(), api_publishing_enabled=True))

    def test_a_caption_without_a_handle_is_bounded(self):
        payload = prompts.render_card(
            self._with_media(handle=None, file_name="x" * 900),
            api_publishing_enabled=False,
        )
        assert len(payload["caption"]) <= 1024


class TestTheOutcomeLine:
    """One formatter for the outcome line and the slot line:
    `%Y-%m-%d %H:%M` in the workspace tz, the state's word, the tapper."""

    def test_the_line_names_the_state_the_person_and_the_time_in_workspace_tz(self):
        from datetime import datetime, timezone

        at = datetime(2026, 9, 9, 18, 14, tzinfo=timezone.utc)
        line = prompts.outcome_line("approved", by="Sam", at=at, tz="America/New_York")
        assert line == "✅ Approved by Sam · 2026-09-09 14:14 America/New_York"

    def test_without_a_person_the_line_still_says_what_happened(self):
        from datetime import datetime, timezone

        at = datetime(2026, 9, 9, 18, 14, tzinfo=timezone.utc)
        assert prompts.outcome_line("expired", by=None, at=at, tz="UTC") == (
            "⌛ Expired — slot passed · 2026-09-09 18:14 UTC"
        )

    def test_an_act_that_ends_a_series_says_so_after_the_time(self):
        """The card's header still says how the story repeats, so the line
        under it says the series is over."""
        from datetime import datetime, timezone

        at = datetime(2026, 9, 9, 18, 14, tzinfo=timezone.utc)
        line = prompts.outcome_line(
            "cancelled", by="Sam", at=at, tz="UTC", series=prompts.SERIES_STOPPED
        )
        assert line == "🚫 Cancelled by Sam · 2026-09-09 18:14 UTC · the series stopped"
        assert prompts.outcome_line(
            "rejected", by="Sam", at=at, tz="UTC", series=prompts.SERIES_ENDED
        ).endswith(" · the series ended")

    @pytest.mark.parametrize(
        "state",
        [
            "approved",
            "publishing",
            "publishing_ambiguous",
            "posted",
            "skipped",
            "rejected",
            "expired",
            "cancelled",
            "failed",
            "review_required",
        ],
    )
    def test_every_state_after_awaiting_has_a_word(self, state):
        assert prompts.OUTCOME_WORDS[state]

    def test_a_zone_postgres_accepts_but_the_iana_database_does_not_degrades_to_utc(
        self,
    ):
        from datetime import datetime, timezone

        at = datetime(2026, 9, 9, 18, 14, tzinfo=timezone.utc)
        assert prompts.stamp(at, "PST") == "2026-09-09 18:14 UTC"
        assert prompts.outcome_line("skipped", by=None, at=at, tz="UTC+5").endswith(
            " UTC"
        )


def test_the_dry_run_outcome_word_names_what_did_not_happen():
    from datetime import datetime, timezone

    from src.services.target import prompts

    line = prompts.outcome_line(
        "dry_run",
        by=None,
        at=datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc),
        tz="UTC",
    )
    assert line.startswith("🧪 Dry run — not published")


def test_a_posted_dry_run_row_words_as_a_dry_run_everywhere():
    from src.services.target import prompts

    assert prompts.outcome_word("posted", "dry_run").startswith("🧪 Dry run")
    assert prompts.outcome_word("posted", "api") == "✅ Posted"
    assert prompts.outcome_word("skipped", "dry_run") == "⏭️ Skipped"


INTENT = "0b6e5f1a-2f4d-4c1e-9a3b-7d8e9f0a1b2c"
WORKSPACE = "8d1d3a3e-0000-4000-8000-000000000001"
#: A story's refusal as `last_error -> 'error' -> 'refusal'` holds it, and the
#: words `refusal_evidence.plain` gives it.
REFUSAL = {
    "v": 1,
    "kind": "local_validation",
    "check": "malformed_container",
    "measured": {},
}
REFUSED = refusal_evidence.plain(REFUSAL)


class TestTheCardKeyboard:
    """`card_keyboard`: the one spelling of an approval card's
    keyboard for its story as it is now. Configure is on every card but a
    rejected story's or a removed item's, which carry none — except a removed
    item's story in review, which carries Give up, and It posted above it once
    a publish call was made."""

    @staticmethod
    def _datas(kb):
        return [
            [b.get("callback_data") or b.get("url") for b in row]
            for row in kb["inline_keyboard"]
        ]

    def test_a_waiting_card_offers_the_decision_and_configure_beside_instagram(self):
        kb = prompts.card_keyboard(
            INTENT, state="awaiting_approval", api_publishing_enabled=True
        )
        assert self._datas(kb) == [
            [f"v1:post:{INTENT}", f"v1:posted:{INTENT}"],
            [f"v1:skip:{INTENT}", f"v1:reject:{INTENT}"],
            [prompts.INSTAGRAM_DEEPLINK_URL, f"v1:cfg:{INTENT}"],
        ]

    def test_a_workspace_that_posts_by_hand_offers_no_post_now(self):
        kb = prompts.card_keyboard(
            INTENT, state="awaiting_approval", api_publishing_enabled=False
        )
        assert self._datas(kb) == [
            [f"v1:posted:{INTENT}", f"v1:skip:{INTENT}"],
            [f"v1:reject:{INTENT}"],
            [prompts.INSTAGRAM_DEEPLINK_URL, f"v1:cfg:{INTENT}"],
        ]

    def test_a_parked_card_keeps_its_resolutions_then_configure(self):
        """A `review_required` card is the workspace's to resolve: the three
        resolutions a member may take, then Configure."""
        kb = prompts.card_keyboard(
            INTENT, state="review_required", api_publishing_enabled=False
        )
        assert self._datas(kb) == [
            [f"v1:itposted:{INTENT}", f"v1:notposted:{INTENT}"],
            [f"v1:giveup:{INTENT}"],
            [f"v1:cfg:{INTENT}"],
        ]
        # The retry button's label carries the member's verdict: it is the
        # answer to "is it on your story?", which is the review's question.
        rows = kb["inline_keyboard"]
        assert [b["text"] for b in rows[0]] == [
            "✅ It posted",
            "🔁 Not there — post again",
        ]
        assert rows[1][0]["text"] == "🚫 Give up"

    @pytest.mark.parametrize("api_publishing_enabled", [True, False])
    def test_a_removed_items_card_in_review_carries_give_up_alone(
        self, api_publishing_enabled
    ):
        """A removed item cannot post again, so its story in review, never
        asked of Instagram, offers the one resolution that ends it: no retry,
        no Configure, whichever way the workspace posts."""
        kb = prompts.card_keyboard(
            INTENT,
            state="review_required",
            api_publishing_enabled=api_publishing_enabled,
            item_state="removed",
        )
        assert kb == {
            "inline_keyboard": [
                [{"text": "🚫 Give up", "callback_data": f"v1:giveup:{INTENT}"}]
            ]
        }

    @pytest.mark.parametrize("api_publishing_enabled", [True, False])
    @pytest.mark.parametrize(
        "publish_step, cancelling, actions",
        [
            # The story may have posted before its item was removed.
            ("publish_called", False, ("itposted", "giveup")),
            # Before the call it cannot have, and the port refuses It posted.
            ("container_ready", False, ("giveup",)),
            # On its way to `cancelled`, where It posted is refused too.
            ("publish_called", True, ("giveup",)),
        ],
    )
    def test_a_removed_items_story_keeps_it_posted_once_a_publish_call_was_made(
        self, api_publishing_enabled, publish_step, cancelling, actions
    ):
        """It posted stays above Give up once a publish call was made and no
        cancel is pending, as the web's Queue offers it; a retry still cannot
        post the removed item again."""
        kb = prompts.card_keyboard(
            INTENT,
            state="review_required",
            api_publishing_enabled=api_publishing_enabled,
            item_state="removed",
            cancelling=cancelling,
            publish_step=publish_step,
        )
        assert self._datas(kb) == [[f"v1:{a}:{INTENT}"] for a in actions]

    @pytest.mark.parametrize(
        "state",
        [
            "approved",
            "publishing",
            "publishing_ambiguous",
            "posted",
            "skipped",
            "expired",
            "cancelled",
            "failed",
        ],
    )
    def test_any_other_state_keeps_configure_alone_for_the_cards_whole_life(
        self, state
    ):
        kb = prompts.card_keyboard(INTENT, state=state, api_publishing_enabled=True)
        assert self._datas(kb) == [[f"v1:cfg:{INTENT}"]]

    @pytest.mark.parametrize(
        "state", ["scheduled", "prompt_pending", "awaiting_approval"]
    )
    def test_a_cancelling_story_keeps_configure_alone(self, state):
        """A pending cancel puts the story on its way to `cancelled`: the
        cancel's edit leaves Configure alone, never the decision buttons,
        since no later edit would take them away."""
        kb = prompts.card_keyboard(
            INTENT, state=state, api_publishing_enabled=True, cancelling=True
        )
        assert self._datas(kb) == [[f"v1:cfg:{INTENT}"]]

    @pytest.mark.parametrize(
        "state, item_state",
        [
            ("rejected", "available"),
            ("awaiting_approval", "removed"),
            ("posted", "removed"),
            # A story given up ends `cancelled`, and its card keeps no keyboard
            # while its item stays removed.
            ("cancelled", "removed"),
            ("rejected", "removed"),
        ],
    )
    def test_a_rejected_story_or_a_removed_item_carries_none(self, state, item_state):
        assert (
            prompts.card_keyboard(
                INTENT, state=state, api_publishing_enabled=True, item_state=item_state
            )
            is None
        )

    def test_every_token_fits_and_parses_back(self):
        from src.services.target import callback_tokens

        for state in ("awaiting_approval", "review_required", "posted"):
            kb = prompts.card_keyboard(INTENT, state=state, api_publishing_enabled=True)
            for row in kb["inline_keyboard"]:
                for b in row:
                    if "callback_data" in b:
                        assert len(b["callback_data"].encode()) <= 64
                        assert (
                            callback_tokens.parse(b["callback_data"]).intent_id
                            == INTENT
                        )


class TestTheMenuKeyboard:
    """Configure's menu: the flag's toggle, the web's approval settings
    only on an https link Telegram will take, and Back."""

    @pytest.mark.parametrize(
        "on, label, action", [(False, "off", "aa1"), (True, "on", "aa0")]
    )
    def test_the_toggle_says_the_flag_and_turns_it_the_other_way(
        self, on, label, action
    ):
        kb = prompts.menu_keyboard(INTENT, on=on, settings_url=None)
        (toggle,) = kb["inline_keyboard"][0]
        assert toggle["text"] == f"🔁 Auto-approve this item: {label}"
        assert toggle["callback_data"] == f"v1:{action}:{INTENT}"
        assert kb["inline_keyboard"][-1] == [
            {"text": "‹ Back", "callback_data": f"v1:back:{INTENT}"}
        ]

    def test_the_settings_link_only_for_https(self):
        https = prompts.menu_keyboard(
            INTENT, on=False, settings_url="https://app.example/s"
        )
        assert [
            {"text": "⚙️ Approval settings ↗", "url": "https://app.example/s"}
        ] in https["inline_keyboard"]
        for url in ("http://localhost:3000/s", None):
            kb = prompts.menu_keyboard(INTENT, on=False, settings_url=url)
            assert len(kb["inline_keyboard"]) == 2, (
                "Telegram refuses a non-https URL button"
            )


class TestARendersAgainForAResend:
    """`rerender_prompt`: a card that is sent again — a lost
    answer's resend, or a first send that waited — is rendered from the
    intent AS IT IS NOW: the workspace's current buttons, and nothing at all
    when the slot has already moved on. A waiting card also says
    when a block stopped a rule from approving it, and a story a rule
    approved is sent settled."""

    #: The decision's facts for a story nothing approves automatically.
    MANUAL = {
        "provider_account_ref": "ref",
        "cancel_requested": False,
        "api_publishing_enabled": True,
        "dry_run_mode": False,
        "position": "manual",
        "auto_approve": False,
        "has_link": False,
        "has_login": True,
        "posted_via_api": False,
    }

    @staticmethod
    def _settled(state, rule, *, at=None, published_via=None, refusal=None):
        """A settlement row (`intent_ledger.settlement`'s read). *refusal* is what
        the story's `last_error -> 'error' -> 'refusal'` holds, NULL when it has
        none."""
        return {
            "state": state,
            "published_via": published_via,
            "entered_state_at": at,
            "approval_rule": rule,
            "actor_user_id": None,
            "created_at": at,
            "refusal": refusal,
        }

    class _Session:
        """Answers each read by what it asks: the decision's facts
        (`auto_approval.facts`), the settlement (`intent_ledger.settlement`),
        or the card row."""

        def __init__(self, row, *, facts=None, settled=None):
            self.row = row
            self.facts = facts
            self.settled = settled
            self.sql = []
            self.rolled_back = 0

        def begin_nested(self):
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def _sp():
                try:
                    yield self
                except Exception:
                    self.rolled_back += 1
                    raise

            return _sp()

        async def execute(self, statement, params=None):
            sql = str(statement)
            self.sql.append((sql, params))
            if "has_login" in sql:
                if isinstance(self.facts, Exception):
                    raise self.facts
                row = self.facts
            elif "audit_events" in sql:
                row = self.settled
            else:
                row = self.row

            class _M:
                def first(self_inner):
                    return row

            class _R:
                def mappings(self_inner):
                    return _M()

            return _R()

    LIVE = {
        "id": INTENT,
        "state": "awaiting_approval",
        "workspace_id": "ws",
        "schedule_slot_at": datetime(2026, 9, 12, 13, 0, tzinfo=timezone.utc),
        "file_name": "f.jpg",
        "media_kind": "image",
        "mime_type": "image/jpeg",
        "source_id": "src",
        "provider_file_ref": "ref",
        "handle": "brand",
        "tz": "America/New_York",
        "api_publishing_enabled": True,
    }

    @pytest.mark.asyncio
    async def test_a_live_slot_renders_with_the_workspaces_current_buttons(self):
        s = self._Session(self.LIVE, facts=self.MANUAL)
        payload = await prompts.rerender_prompt(s, intent_id=INTENT)
        assert payload is not None
        tokens = [
            b.get("callback_data")
            for r in payload["reply_markup"]["inline_keyboard"]
            for b in r
        ]
        assert f"v1:post:{INTENT}" in tokens
        assert "WHERE card.id = :id" in s.sql[0][0]
        assert "🔒" not in payload["text"], "a story nothing would approve has no note"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", ["skipped", "expired", "approved", "posted"])
    async def test_a_slot_that_moved_on_renders_nothing(self, state):
        s = self._Session(
            {**self.LIVE, "state": state},
            settled=self._settled(state, None),
        )
        assert await prompts.rerender_prompt(s, intent_id=INTENT) is None

    @pytest.mark.asyncio
    async def test_a_story_parked_before_its_card_went_out_gets_its_review_card(
        self,
    ):
        at = datetime(2026, 9, 12, 13, 2, tzinfo=timezone.utc)
        s = self._Session(
            {
                **self.LIVE,
                "state": "review_required",
                "source_provider": "instagram_past_posts",
            },
            settled=self._settled("review_required", None, at=at),
        )
        payload = await prompts.rerender_prompt(s, intent_id=INTENT)
        assert payload is not None, "a parked story still gets its card"
        assert "media" not in payload, "a review card asks for no past-posts bytes"
        assert payload["outcome_text"].startswith("👀 Needs review")
        tokens = [
            b.get("callback_data")
            for r in payload["reply_markup"]["inline_keyboard"]
            for b in r
        ]
        assert f"v1:giveup:{INTENT}" in tokens
        assert f"v1:post:{INTENT}" not in tokens
        assert not any("has_login" in sql for sql, _ in s.sql), (
            "a parked story asks no approval rule and no media admission"
        )

    @pytest.mark.asyncio
    async def test_a_parked_storys_card_says_why_it_was_refused(self):
        """The review card's line carries the refusal the park recorded, in
        words, on a second line under the state's line."""
        at = datetime(2026, 9, 12, 13, 2, tzinfo=timezone.utc)
        s = self._Session(
            {
                **self.LIVE,
                "state": "review_required",
                "source_provider": "instagram_past_posts",
            },
            settled=self._settled("review_required", None, at=at, refusal=REFUSAL),
        )
        payload = await prompts.rerender_prompt(s, intent_id=INTENT)
        assert payload["outcome_text"] == (
            f"👀 Needs review · 2026-09-12 09:02 America/New_York\nRefused: {REFUSED}."
        )

    @pytest.mark.asyncio
    async def test_a_story_a_route_parked_says_why_rather_than_refused(self):
        """A route into review that refused nothing (here a spent job budget)
        records its own reason, and the line opens it with "Why"."""
        at = datetime(2026, 9, 12, 13, 2, tzinfo=timezone.utc)
        spent = refusal_evidence.evidence("budget_spent", tries=5)
        s = self._Session(
            {
                **self.LIVE,
                "state": "review_required",
                "source_provider": "instagram_past_posts",
            },
            settled=self._settled("review_required", None, at=at, refusal=spent),
        )
        payload = await prompts.rerender_prompt(s, intent_id=INTENT)
        assert payload["outcome_text"] == (
            "👀 Needs review · 2026-09-12 09:02 America/New_York\nWhy: we hit a"
            " fault on our side 5 times trying to post this story."
        )

    @pytest.mark.asyncio
    async def test_a_parked_story_with_no_refusal_keeps_its_plain_line(self):
        """A story parked with nothing recorded (a refusal raised without
        evidence) says only which state it is in."""
        at = datetime(2026, 9, 12, 13, 2, tzinfo=timezone.utc)
        s = self._Session(
            {
                **self.LIVE,
                "state": "review_required",
                "source_provider": "instagram_past_posts",
            },
            settled=self._settled("review_required", None, at=at),
        )
        payload = await prompts.rerender_prompt(s, intent_id=INTENT)
        assert payload["outcome_text"] == (
            "👀 Needs review · 2026-09-12 09:02 America/New_York"
        )

    @pytest.mark.asyncio
    async def test_a_prompt_still_pending_is_live(self):
        s = self._Session(
            {
                "id": INTENT,
                "state": "prompt_pending",
                "workspace_id": "ws",
                "schedule_slot_at": datetime(2026, 9, 12, 13, 0, tzinfo=timezone.utc),
                "file_name": "f.jpg",
                "tz": "UTC",
                "api_publishing_enabled": False,
            },
            facts=self.MANUAL,
        )
        payload = await prompts.rerender_prompt(s, intent_id=INTENT)
        assert payload is not None and "v1:post:" not in str(payload)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "state, line",
        [
            ("approved", "✅ Approved by YOLO · 2026-09-12 09:01 America/New_York"),
            ("publishing", "🚀 Posting… by YOLO · 2026-09-12 09:01 America/New_York"),
            ("posted", "✅ Posted by YOLO · 2026-09-12 09:01 America/New_York"),
        ],
    )
    async def test_a_story_a_rule_approved_is_sent_settled(self, state, line):
        s = self._Session(
            {**self.LIVE, "state": state},
            settled=self._settled(
                state, "auto", at=datetime(2026, 9, 12, 13, 1, tzinfo=timezone.utc)
            ),
        )
        payload = await prompts.rerender_prompt(s, intent_id=INTENT)
        assert payload is not None
        # The line rides the outbox's own `outcome_text`; the card's text stays
        # its header, so a later line replaces this one instead of stacking.
        assert payload["outcome_text"] == line
        assert (
            payload["caption"] == "📸 @brand\nSlot: 2026-09-12 09:00 America/New_York"
        )
        assert line not in payload["text"]
        assert payload["reply_markup"] == {
            "inline_keyboard": [
                [{"text": "⚙️ Configure", "callback_data": f"v1:cfg:{INTENT}"}]
            ]
        }, "a settled card offers Configure alone"

    @pytest.mark.asyncio
    async def test_a_dry_run_says_so_and_names_the_rule(self):
        s = self._Session(
            {**self.LIVE, "state": "posted"},
            settled=self._settled(
                "posted",
                "item",
                at=datetime(2026, 9, 12, 13, 1, tzinfo=timezone.utc),
                published_via="dry_run",
            ),
        )
        payload = await prompts.rerender_prompt(s, intent_id=INTENT)
        assert payload["outcome_text"] == (
            "🧪 Dry run — not published by Auto-approve · 2026-09-12 09:01"
            " America/New_York"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("state, rule", [("failed", "auto"), ("cancelled", "auto")])
    async def test_a_rule_approved_story_that_ended_otherwise_renders_nothing(
        self, state, rule
    ):
        s = self._Session(
            {**self.LIVE, "state": state},
            settled=self._settled(state, rule),
        )
        assert await prompts.rerender_prompt(s, intent_id=INTENT) is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "facts, note",
        [
            (
                {"position": "auto", "api_publishing_enabled": False},
                "🔒 YOLO is set, but this workspace posts by hand.",
            ),
            (
                {"auto_approve": True, "has_login": False},
                "🔒 Auto-approve is on, but Instagram isn't connected for @brand.",
            ),
            (
                {"position": "returning", "posted_via_api": True, "has_link": True},
                "🔒 Returning items is on, but this item has a link to add — post"
                " it by hand.",
            ),
        ],
    )
    async def test_a_waiting_card_says_which_block_stopped_which_rule(
        self, facts, note
    ):
        s = self._Session(self.LIVE, facts={**self.MANUAL, **facts})
        payload = await prompts.rerender_prompt(s, intent_id=INTENT)
        slot = "Slot: 2026-09-12 09:00 America/New_York"
        assert f"{slot}\n{note}" in payload["text"]
        assert f"{slot}\n{note}" in payload["caption"]

    @pytest.mark.asyncio
    async def test_a_note_that_cannot_be_read_never_holds_the_card_back(self, caplog):
        """The note is advisory: its read rides a savepoint of its own, and a
        failure sends the card without it — a card that could not be rendered
        would hold the chat's queue behind it."""
        s = self._Session(self.LIVE, facts=RuntimeError("the facts read failed"))
        with caplog.at_level("WARNING", logger=prompts.__name__):
            payload = await prompts.rerender_prompt(s, intent_id=INTENT)
        assert payload is not None and "🔒" not in payload["text"]
        assert f"v1:post:{INTENT}" in str(payload["reply_markup"])
        assert s.rolled_back == 1
        assert any(
            "block note could not be read" in r.getMessage() for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_a_note_that_cannot_be_rendered_never_holds_the_card_back(
        self, caplog, monkeypatch
    ):
        """A block with no words in `_BLOCK_NOTES` (a reason added to `decide`
        alone) goes the failed read's way: the card is sent without its note,
        under the same warning. The savepoint holds the read alone, so the
        render's failure has nothing to undo."""
        monkeypatch.setattr(
            prompts.auto_approval,
            "decide",
            lambda found: prompts.auto_approval.Decision(None, "fourth", "auto"),
        )
        s = self._Session(self.LIVE, facts=self.MANUAL)
        with caplog.at_level("WARNING", logger=prompts.__name__):
            payload = await prompts.rerender_prompt(s, intent_id=INTENT)
        assert payload is not None and "🔒" not in payload["text"]
        assert f"v1:post:{INTENT}" in str(payload["reply_markup"])
        assert s.rolled_back == 0, "the savepoint holds the read alone"
        assert any(
            "block note could not be read" in r.getMessage() for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_no_note_without_a_rule_a_block_stopped(self):
        """API posting off, and nothing that would approve the story."""
        s = self._Session(
            self.LIVE, facts={**self.MANUAL, "api_publishing_enabled": False}
        )
        payload = await prompts.rerender_prompt(s, intent_id=INTENT)
        assert "🔒" not in payload["text"]


class TestTheLineNamesTheMechanism:
    """A line a rule caused names the rule where a person's name
    would stand ("✅ Posted by YOLO"), a dry run included. A failed, review,
    expired or cancelled line never names it, and a person's lines are
    unchanged."""

    AT = datetime(2026, 9, 9, 18, 14, tzinfo=timezone.utc)

    @pytest.mark.parametrize(
        "state, rule, line",
        [
            ("approved", "auto", "✅ Approved by YOLO"),
            ("publishing", "item", "🚀 Posting… by Auto-approve"),
            ("publishing_ambiguous", "auto", "🚀 Posting… (confirming) by YOLO"),
            ("posted", "returning", "✅ Posted by Returning items"),
            ("dry_run", "auto", "🧪 Dry run — not published by YOLO"),
        ],
    )
    def test_the_rule_names_the_lines_it_caused_when_no_person_moved_the_story(
        self, state, rule, line
    ):
        assert prompts.outcome_line(
            state, by=None, at=self.AT, tz="UTC", rule=rule
        ) == (f"{line} · 2026-09-09 18:14 UTC")

    def test_a_persons_name_wins_over_the_rule(self):
        """A person who moved a rule-approved story (an "it posted", a retry)
        is named on every surface: the card's restate, the settled sweep, a
        repeat tap's answer."""
        assert prompts.outcome_line(
            "posted", by="Dana", at=self.AT, tz="UTC", rule="auto"
        ) == ("✅ Posted by Dana · 2026-09-09 18:14 UTC")

    def test_a_posted_dry_run_reads_as_one(self):
        assert prompts.outcome_line(
            "posted",
            by=None,
            at=self.AT,
            tz="UTC",
            published_via="dry_run",
            rule="auto",
        ) == ("🧪 Dry run — not published by YOLO · 2026-09-09 18:14 UTC")

    @pytest.mark.parametrize(
        "state",
        ["failed", "review_required", "expired", "cancelled", "skipped", "rejected"],
    )
    def test_a_line_the_rule_did_not_cause_never_names_it(self, state):
        line = prompts.outcome_line(state, by="Dana", at=self.AT, tz="UTC", rule="auto")
        assert "YOLO" not in line and " by Dana · " in line
        assert "YOLO" not in prompts.outcome_line(
            state, by=None, at=self.AT, tz="UTC", rule="auto"
        )

    def test_a_persons_line_is_unchanged(self):
        assert prompts.outcome_line("approved", by="Dana", at=self.AT, tz="UTC") == (
            "✅ Approved by Dana · 2026-09-09 18:14 UTC"
        )

    def test_the_waiting_line_names_the_rule_too(self):
        from datetime import timedelta

        now = datetime(2026, 9, 14, 20, 0, tzinfo=timezone.utc)
        tz = "America/New_York"
        assert (
            prompts.waiting_line(
                now + timedelta(hours=2), tz=tz, now=now, rule="returning"
            )
            == "✅ Approved by Returning items · posts today 18:00"
        )
        assert (
            prompts.waiting_line(
                now + timedelta(hours=2), tz=tz, now=now, day_spent=True, rule="auto"
            )
            == "✅ Approved by YOLO · posts tomorrow"
        )


class _Answer:
    """What `session.execute(...)` answers a read of one row: its
    `.mappings().first()`."""

    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


class _ReadSession:
    """A session whose one read answers *row*, and which keeps the statement and
    the parameters it was asked with."""

    def __init__(self, row):
        self.row = row
        self.asked = []

    async def execute(self, statement, params=None):
        self.asked.append((str(statement), params))
        return _Answer(self.row)


def _settlement_row(state, refusal):
    """The settlement read's row, as the database hands it: the story's state, its
    last move, and the refusal its `last_error` holds."""
    return {
        "state": state,
        "published_via": None,
        "entered_state_at": datetime(2026, 9, 12, 13, 2, tzinfo=timezone.utc),
        "approval_rule": None,
        "actor_user_id": None,
        "created_at": None,
        "refusal": refusal,
    }


class TestTheSettlementCarriesTheRefusal:
    """`intent_ledger.settlement` hands on the refusal in the story's
    `last_error -> 'error' -> 'refusal'` only while the story is in review. A
    story that has left review keeps the refusal in its `last_error` and is no
    longer refused."""

    @staticmethod
    async def _read(row):
        session = _ReadSession(row)
        found = await intent_ledger.settlement(
            session, workspace_id=WORKSPACE, intent_id=INTENT
        )
        return found, session

    @pytest.mark.parametrize(
        "stored", [REFUSAL, json.dumps(REFUSAL)], ids=["decoded", "json-text"]
    )
    async def test_a_story_in_review_carries_its_refusal(self, stored):
        """The database hands a JSON value over decoded or as its text, by driver;
        either way the settlement carries the object."""
        found, _ = await self._read(_settlement_row("review_required", stored))
        assert found["refusal"] == REFUSAL

    async def test_a_story_in_review_with_no_refusal_carries_none(self):
        found, _ = await self._read(_settlement_row("review_required", None))
        assert found["state"] == "review_required"
        assert found["refusal"] is None

    @pytest.mark.parametrize(
        "state", [s for s in vocabulary.INTENT_STATES if s != "review_required"]
    )
    async def test_a_story_that_is_not_in_review_carries_no_refusal(self, state):
        found, _ = await self._read(_settlement_row(state, REFUSAL))
        assert found["state"] == state
        assert found["refusal"] is None

    async def test_a_story_the_session_cannot_see_has_no_refusal(self):
        found, _ = await self._read(None)
        assert found["state"] is None
        assert found["refusal"] is None

    async def test_the_read_selects_the_refusal_under_the_story_and_its_tenant(self):
        _, session = await self._read(_settlement_row("review_required", REFUSAL))
        ((sql, params),) = session.asked
        assert "i.last_error -> 'error' -> 'refusal' AS refusal" in sql
        assert "WHERE i.id = :i AND i.workspace_id = :ws" in sql
        assert params == {"i": INTENT, "ws": WORKSPACE}


class TestTheSettledLineSaysWhyAStoryWasRefused:
    """`settled_line` is the one spelling of a settled card's line. A story in
    review gains a second line, "Refused: …", in the words of its refusal
    (`refusal_evidence.plain`); every other line, and the line of a story in
    review whose refusal says nothing, is the state's line alone."""

    AT = datetime(2026, 9, 12, 13, 2, tzinfo=timezone.utc)
    IN_REVIEW = "👀 Needs review · 2026-09-12 13:02 UTC"

    @staticmethod
    def _settlement(state, *, rule=None, refusal=None):
        """What `intent_ledger.settlement` answers when nobody moved the story
        and it has no time of its own: the line asks no one's name."""
        return {
            "state": state,
            "published_via": None,
            "approval_rule": rule,
            "by_user_id": None,
            "at": None,
            "refusal": refusal,
        }

    async def _line(self, settled):
        return await prompts.settled_line(None, settled, tz="UTC", at=self.AT)

    async def test_a_story_in_review_says_why_on_a_second_line(self):
        line = await self._line(self._settlement("review_required", refusal=REFUSAL))
        assert line == f"{self.IN_REVIEW}\nRefused: {REFUSED}."

    @pytest.mark.parametrize(
        "refusal, reason",
        [
            (
                {
                    "v": 1,
                    "kind": "cdn_refused",
                    "statuses": [403, 404],
                    "host": "scontent-test.fbcdn.net",
                    "expiry": None,
                },
                "Instagram's file server refused the link twice (HTTP 403/404)",
            ),
            (
                {
                    "v": 1,
                    "kind": "slide_gone",
                    "children": 2,
                    "complete": True,
                    "slide_count": 4,
                },
                "the chosen slide is no longer in the post: Instagram listed"
                " 2 slide(s) of the 4 indexed",
            ),
            (
                {
                    "v": 1,
                    "kind": "container_dead",
                    "status_code": "ERROR",
                    "status": "Error: Media upload has failed with error code 2207026",
                },
                "Instagram could not process this story's media (ERROR): Error: Media"
                " upload has failed with error code 2207026",
            ),
        ],
        ids=["cdn-refused", "slide-gone", "container-dead"],
    )
    async def test_the_second_line_is_the_refusals_own_sentence(self, refusal, reason):
        line = await self._line(self._settlement("review_required", refusal=refusal))
        assert line.splitlines() == [self.IN_REVIEW, f"Refused: {reason}."]

    @pytest.mark.parametrize(
        "refusal",
        [None, "not a record"],
        ids=["none", "not-a-record"],
    )
    async def test_a_story_in_review_with_no_refusal_record_keeps_its_line(
        self, refusal
    ):
        """No record, or a value that is not one: nothing to add, and the line
        serves every story in review, past posts or not."""
        line = await self._line(self._settlement("review_required", refusal=refusal))
        assert line == self.IN_REVIEW

    @pytest.mark.parametrize(
        "refusal,reason",
        [
            ({}, "the fetch was refused (unknown) and no detail was recorded"),
            (
                {"v": 1, "kind": "unrecorded", "error": "MediaTerminalError"},
                "the fetch was refused (MediaTerminalError) and no detail was recorded",
            ),
            (
                {"v": 1, "kind": "no_such_kind"},
                "the fetch was refused (no_such_kind) and no detail was recorded",
            ),
        ],
        ids=["empty", "unrecorded", "unknown-kind"],
    )
    async def test_a_refusal_with_nothing_specific_still_says_what_it_holds(
        self, refusal, reason
    ):
        """A record with no detail still gets a line: what it holds, never a
        cause it cannot support."""
        line = await self._line(self._settlement("review_required", refusal=refusal))
        assert line.splitlines() == [self.IN_REVIEW, f"Refused: {reason}."]

    async def test_a_settlement_without_a_refusal_key_keeps_its_line(self):
        """A settlement built without the key (a caller's own) reads as no refusal."""
        settled = self._settlement("review_required")
        del settled["refusal"]
        assert await self._line(settled) == self.IN_REVIEW

    @pytest.mark.parametrize(
        "state, rule, line",
        [
            ("approved", "auto", "✅ Approved by YOLO · 2026-09-12 13:02 UTC"),
            ("approved", None, "✅ Approved · 2026-09-12 13:02 UTC"),
            ("posted", "auto", "✅ Posted by YOLO · 2026-09-12 13:02 UTC"),
        ],
    )
    async def test_a_story_that_is_not_in_review_keeps_its_line(
        self, state, rule, line
    ):
        """Even with a reason still on its row: a retry's, or one kept after
        "It posted"."""
        settled = self._settlement(state, rule=rule, refusal=REFUSAL)
        assert await self._line(settled) == line


class TestWaitingLine:
    """The approved card's line while its story waits for a slot that is
    hours away: the state's word and when it posts, in the workspace's zone.
    Each test pins the whole line, so a wrong day or time fails."""

    NOW = datetime(2026, 9, 14, 20, 0, tzinfo=timezone.utc)  # 16:00 in New York
    TZ = "America/New_York"

    def test_a_slot_later_today(self):
        from datetime import timedelta

        line = prompts.waiting_line(
            self.NOW + timedelta(hours=2), tz=self.TZ, now=self.NOW
        )
        assert line == "✅ Approved · posts today 18:00"

    def test_a_slot_tomorrow_morning(self):
        from datetime import timedelta

        slot = self.NOW + timedelta(hours=17)  # 09:00 tomorrow in New York
        assert (
            prompts.waiting_line(slot, tz=self.TZ, now=self.NOW)
            == "✅ Approved · posts tomorrow 09:00"
        )

    def test_a_slot_days_away_names_the_weekday(self):
        from datetime import timedelta
        from zoneinfo import ZoneInfo

        slot = self.NOW + timedelta(days=3)
        day = slot.astimezone(ZoneInfo(self.TZ)).strftime("%a")
        assert (
            prompts.waiting_line(slot, tz=self.TZ, now=self.NOW)
            == f"✅ Approved · posts {day} 16:00"
        )

    def test_the_days_cap_spent_promises_tomorrow_without_a_time(self):
        """The local cap is at its count: a slot later today cannot post, and
        the clock has not chosen tomorrow's time yet."""
        from datetime import timedelta

        slot = self.NOW + timedelta(hours=2)
        line = prompts.waiting_line(slot, tz=self.TZ, now=self.NOW, day_spent=True)
        assert line == "✅ Approved · posts tomorrow"

    def test_the_days_cap_spent_with_tomorrows_slot_keeps_its_time(self):
        from datetime import timedelta

        slot = self.NOW + timedelta(hours=17)
        line = prompts.waiting_line(slot, tz=self.TZ, now=self.NOW, day_spent=True)
        assert line == "✅ Approved · posts tomorrow 09:00"

    def test_an_unknown_zone_reads_as_utc(self):
        from datetime import timedelta

        line = prompts.waiting_line(
            self.NOW + timedelta(hours=2), tz="Mars/Olympus", now=self.NOW
        )
        assert line == "✅ Approved · posts today 22:00"


class TestTheDueDoorReadsTheCardSelect:
    def test_fn_prompts_due_carries_the_card_select_verbatim(self):
        """`_CARD_SELECT` is the one spelling of what a card is rendered from.
        The prompt sweep's first card comes from `fn_prompts_due`, whose body
        spells the same columns and joins — a door cannot call the Python — so
        the fragment is pinned to the door's body in the schema code verbatim,
        and the sweep's alias list to every column: a column added for
        `render_card` cannot reach the resend and not the sweep."""
        import inspect

        from src.services.target import prompts

        body = function_body("fn_prompts_due")
        assert " ".join(prompts._CARD_SELECT.split()) in " ".join(body.split())
        select_list = prompts._CARD_SELECT.split("SELECT", 1)[1].split("FROM", 1)[0]
        names = [c.strip().split(".")[-1] for c in select_list.split(",")]
        sweep = inspect.getsource(prompts.sweep_due_prompts)
        for name in names:
            assert f"o_{name} AS {name}" in sweep, f"the sweep does not read {name}"


class _SweepResult:
    def __init__(self, rows, first=None):
        self.rows, self.first_row = rows, first

    def mappings(self):
        return self

    def all(self):
        return self.rows

    def first(self):
        return self.first_row

    def __iter__(self):
        return iter(self.rows)

    def one(self):
        return ("", "system")  # the caller's scope, read by the first claim


class _SweepSession:
    """The prompt sweeper's session double: the three doors answer; the miss
    leg's guarded UPDATE returns its id unless the intent is in *raced*; the
    serve leg's locked re-read answers a due row as the door read it, but
    for what *changed* (intent id → the columns changed since the door read
    it); every statement is recorded, a savepoint is offered and its rollback
    counted."""

    def __init__(
        self,
        *,
        due=(),
        pending=(),
        misses=(),
        raced=(),
        changed=None,
        ended=None,
        waits=(),
        named=None,
        rules=None,
    ):
        self.due, self.pending, self.misses = list(due), list(pending), list(misses)
        self.raced, self.changed = set(raced), dict(changed or {})
        # what the miss UPDATE returns per intent, over a story with no rule,
        # and the intents whose next occurrence waits after their miss
        self.ended, self.waits = dict(ended or {}), set(waits)
        # what the card's one read (`_with_scheduler`) answers per intent
        self.named, self.rules = dict(named or {}), dict(rules or {})
        self.statements = []
        self.savepoints = self.rolled_back = 0

    def begin_nested(self):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _sp():
            self.savepoints += 1
            try:
                yield self
            except Exception:
                self.rolled_back += 1
                raise

        return _sp()

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append((sql, params))
        if "fn_prompts_due" in sql:
            return _SweepResult(self.due)
        if "fn_prompts_pending" in sql:
            return _SweepResult(self.pending)
        if "fn_planned_misses" in sql:
            return _SweepResult(self.misses)
        if sql.startswith("UPDATE post_intents") and params["id"] not in self.raced:
            returned = {
                "born_late": False,
                "repeating": False,
                "media_item_id": f"m-{params['id']}",
                "ig_account_id": f"a-{params['id']}",
                **self.ended.get(params["id"], {}),
            }
            return _SweepResult([], first=SimpleNamespace(**returned))
        if "END AS scheduled_by" in sql:
            return _SweepResult(
                [],
                first={
                    "repeat": self.rules.get(params["id"]),
                    "scheduled_by": self.named.get(params["id"]),
                },
            )
        if "AS waits" in sql:
            intent = params["media"].removeprefix("m-")
            return _SweepResult([], first={"waits": intent in self.waits})
        if sql.startswith("SELECT state, cancel_requested, schedule_slot_at"):
            (read,) = [r for r in self.due if str(r["id"]) == params["id"]]
            now = {
                "state": read["state"],
                "cancel_requested": False,
                "schedule_slot_at": read["schedule_slot_at"],
                **self.changed.get(params["id"], {}),
            }
            return _SweepResult([], first=now)
        return _SweepResult([])


class TestTheAdvancePhaseSurvivesARefusal:
    async def test_a_refused_transition_rolls_back_its_savepoint_and_the_sweep_goes_on(
        self, monkeypatch
    ):
        """`IntentTransitionRefused` is a Postgres check_violation, which
        aborts the transaction: without a savepoint the next statement —
        the next row's transition, or the hand-back of the caller's scope —
        raises `InFailedSqlTransaction`, and one raced row fails the whole
        sweep."""
        from src.services.target import intent_ledger, prompts

        pending = [
            {"id": "i-1", "workspace_id": "ws-1"},
            {"id": "i-2", "workspace_id": "ws-2"},
        ]
        session = _SweepSession(pending=pending)
        calls = []

        async def transition(s, intent_id, to_state):
            calls.append(intent_id)
            if intent_id == "i-1":
                raise intent_ledger.IntentTransitionRefused("raced by the fast path")

        served = []

        async def approve_if_due(s, *, workspace_id, intent_id):
            served.append(intent_id)
            return None

        monkeypatch.setattr(prompts.intent_ledger, "transition", transition)
        monkeypatch.setattr(prompts.auto_approval, "approve_if_due", approve_if_due)
        counts = await prompts.sweep_due_prompts(session, limit=5, late_seconds=3600)
        assert counts == {"prompted": 0, "advanced": 1, "approved": 0}
        assert calls == ["i-1", "i-2"], "the sweep goes on after a refusal"
        assert served == ["i-2"], "only a story the leg advanced is served its decision"
        assert (session.savepoints, session.rolled_back) == (3, 1), (
            "each transition rides its own savepoint, and so does the serve step;"
            " the refused one rolls back"
        )
        last_sql, last_params = session.statements[-1]
        assert (
            "set_config('app.tenant_id'" in last_sql and "" in last_params.values()
        ), "the caller's scope is handed back after the refusal"

    async def test_the_serve_step_approves_and_a_failure_there_rolls_back_alone(
        self, monkeypatch, caplog
    ):
        """The serve step rides a savepoint of its own after the
        advance. A story it approves is counted; any failure deciding or
        approving one rolls back only that savepoint, is logged naming the
        story, and leaves it waiting for a person while the sweep goes on."""
        from src.services.target import prompts

        session = _SweepSession(
            pending=[
                {"id": "i-1", "workspace_id": "ws-1"},
                {"id": "i-2", "workspace_id": "ws-2"},
            ]
        )

        async def transition(s, intent_id, to_state):
            return None

        async def approve_if_due(s, *, workspace_id, intent_id):
            if intent_id == "i-1":
                raise RuntimeError("the facts read failed")
            return "auto"

        monkeypatch.setattr(prompts.intent_ledger, "transition", transition)
        monkeypatch.setattr(prompts.auto_approval, "approve_if_due", approve_if_due)
        with caplog.at_level("WARNING", logger=prompts.__name__):
            counts = await prompts.sweep_due_prompts(
                session, limit=5, late_seconds=3600
            )
        assert counts == {"prompted": 0, "advanced": 2, "approved": 1}
        assert (session.savepoints, session.rolled_back) == (4, 1)
        assert any(
            "i-1" in r.getMessage() and "waits for a person" in r.getMessage()
            for r in caplog.records
        )

    async def test_an_intent_it_cannot_see_is_NOT_swallowed(self, monkeypatch):
        """`IntentNotVisible` is a caller's bug, not a refusal, so it
        escapes the savepoint that refusals ride. As a subclass of
        `IntentTransitionRefused` it would be swallowed right here, which is
        why it is a distinct type."""
        from src.services.target import intent_ledger, prompts

        session = _SweepSession(pending=[{"id": "i-1", "workspace_id": "ws-1"}])

        async def transition(s, intent_id, to_state):
            raise intent_ledger.IntentNotVisible("matched no row")

        monkeypatch.setattr(prompts.intent_ledger, "transition", transition)
        with pytest.raises(intent_ledger.IntentNotVisible):
            await prompts.sweep_due_prompts(session, limit=5, late_seconds=3600)


SLOT = datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc)  # 14:00 in New York


class TestThePlannedCard:
    """A planned story's card says who scheduled it and for when (`fn_planned_serve` / `fn_planned_misses`), and
    says "served late" when it is rendered a minute or more past that time —
    at the serve, or again at send time, whichever renders it."""

    def _planned(self, **over):
        base = dict(
            schedule_slot_at=SLOT,
            origin="planned",
            scheduled_by="Dana",
            source_id="src",
            provider_file_ref="ref",
            workspace_id="ws",
            handle="brand",
        )
        base.update(over)
        return _card_input(**base)

    def test_it_names_the_scheduler_and_the_time_in_text_and_caption(self):
        from datetime import timedelta

        card = prompts.render_card(
            self._planned(),
            api_publishing_enabled=True,
            now=SLOT + timedelta(seconds=5),
        )
        line = "🗓 Scheduled by Dana · 2026-10-01 14:00 America/New_York"
        assert card["text"].endswith(line), card["text"]
        assert card["caption"].endswith(line), card["caption"]

    def test_a_minute_past_its_time_the_card_says_served_late(self):
        from datetime import timedelta

        on_time = prompts.render_card(
            self._planned(),
            api_publishing_enabled=True,
            now=SLOT + prompts.SERVED_LATE_AFTER - timedelta(seconds=1),
        )
        late = prompts.render_card(
            self._planned(),
            api_publishing_enabled=True,
            now=SLOT + prompts.SERVED_LATE_AFTER,
        )
        assert "served late" not in on_time["text"]
        assert late["text"].endswith(" · served late"), late["text"]
        assert late["caption"].endswith(" · served late"), late["caption"]

    def test_a_repeating_card_says_its_rule_before_served_late(self):
        rule = {"unit": "week", "every": 1, "days": [1, 4], "at": "14:00"}
        card = prompts.render_card(
            self._planned(repeat=rule),
            api_publishing_enabled=True,
            now=SLOT + prompts.SERVED_LATE_AFTER,
        )
        line = (
            "🗓 Scheduled by Dana · 2026-10-01 14:00 America/New_York"
            " · repeats every week on Mon and Thu at 14:00 · served late"
        )
        assert card["text"].endswith(line), card["text"]
        assert card["caption"].endswith(line), card["caption"]

    def test_a_rule_names_its_zone_where_the_account_keeps_its_own(self):
        """The slot is stamped in the workspace's zone, and the rule's time is the
        account's, so the rule names the account's zone where the two differ."""
        rule = {"unit": "week", "every": 1, "days": [4], "at": "16:07"}
        own, same = (
            prompts.render_card(
                self._planned(repeat=rule, repeat_tz=zone),
                api_publishing_enabled=True,
                now=SLOT,
            )
            for zone in ("Europe/London", "America/New_York")
        )
        assert own["text"].endswith(
            " · repeats every week on Thu at 16:07 (Europe/London)"
        ), own["text"]
        assert same["text"].endswith(" · repeats every week on Thu at 16:07"), same[
            "text"
        ]

    def test_a_scheduler_who_is_gone_leaves_the_line_without_a_name(self):
        card = prompts.render_card(
            self._planned(scheduled_by=None), api_publishing_enabled=True, now=SLOT
        )
        assert card["text"].endswith("🗓 Scheduled · 2026-10-01 14:00 America/New_York")

    def test_a_cadence_card_is_unchanged(self):
        from datetime import timedelta

        cadence = prompts.render_card(
            _card_input(schedule_slot_at=SLOT, origin="cadence", scheduled_by="Dana"),
            api_publishing_enabled=True,
            now=SLOT + timedelta(hours=5),
        )
        assert (
            cadence["text"]
            == "📸 sunset.jpg (image)\nSlot: 2026-10-01 14:00 America/New_York"
        )


class TestTheCardReadsTheRule:
    """Neither the due door nor `_CARD_SELECT` carries who scheduled a story or
    its repeat rule, so `_with_scheduler`, which both card paths come through,
    reads both in one statement."""

    async def test_a_planned_row_gains_both_in_one_read_under_its_tenant(self):
        statements = []
        found = {
            "repeat": {"unit": "day", "every": 2, "days": None, "at": "09:00"},
            "repeat_tz": "Europe/London",
            "scheduled_by": "Dana",
        }

        class _Session:
            async def execute(self, statement, params=None):
                statements.append((str(statement), params))
                return _SweepResult([], first=found)

        row = await prompts._with_scheduler(
            _Session(), {"id": "i-1", "workspace_id": "ws-1", "origin": "planned"}
        )
        assert row == {
            "id": "i-1",
            "workspace_id": "ws-1",
            "origin": "planned",
            **found,
        }
        ((sql, params),) = statements
        assert "a.tz AS repeat_tz" in sql, "the account's own zone, none for none"
        assert (
            "FROM post_intents i JOIN ig_accounts a ON a.id = i.ig_account_id"
            " WHERE i.id = :id AND i.workspace_id = :ws"
        ) in sql
        assert params == {"id": "i-1", "ws": "ws-1"}

    async def test_a_cadence_row_reads_nothing(self):
        class _Session:
            async def execute(self, statement, params=None):
                raise AssertionError("a cadence story carries neither")

        row = await prompts._with_scheduler(
            _Session(), {"id": "i-1", "workspace_id": "ws-1", "origin": "cadence"}
        )
        assert "repeat" not in row and "scheduled_by" not in row


def _notice(**over):
    kw = dict(
        file_name="drop.jpg",
        handle="brand",
        slot=SLOT,
        tz="America/New_York",
        by="Dana",
        reason="late",
    )
    kw.update(over)
    return prompts.missed_notice(**kw)


class TestTheMissNotice:
    @pytest.mark.parametrize("reason", sorted(prompts.MISS_REASONS))
    def test_each_reason_says_what_happened_and_that_nothing_was_posted(self, reason):
        assert _notice(reason=reason) == (
            "🗓 Not served: drop.jpg for @brand, scheduled for 2026-10-01 14:00"
            f" America/New_York by Dana: {prompts.MISS_REASONS[reason]}."
            " Nothing was posted."
        )

    @pytest.mark.parametrize("repeats", [True, False])
    def test_a_repeating_storys_notice_says_what_became_of_its_series(self, repeats):
        assert _notice().endswith(" Nothing was posted.")
        assert _notice(repeats=repeats).endswith(
            f" Nothing was posted. {prompts.SERIES_WORDS[repeats]}"
        )

    def test_the_door_and_the_notice_share_one_vocabulary(self):
        """Every reason the miss door can return has words, and nothing else
        does: `account_removed` comes from the door and from a removal."""
        import re

        body = function_body("fn_planned_misses")
        returned = set(re.findall(r"(?:THEN|ELSE) '([a-z_]+)'", body))
        assert returned == set(prompts.MISS_REASONS), returned

    def test_no_handle_no_name_and_a_long_file_name_still_make_a_notice(self):
        said = _notice(file_name="x" * 5000, handle=None, by=None)
        bound = "x" * prompts.FILE_NAME_BOUND
        assert said.startswith(f"🗓 Not served: {bound} for one of this workspace's")
        assert "by " not in said and len(said) < 400

    def test_the_miss_door_carries_the_notice_select_verbatim(self):
        """`_NOTICE_SELECT` is the one spelling of the row a notice is written
        from: the removal reads it directly, and `fn_planned_misses` spells the
        same columns and joins (a door cannot call the Python). So its select
        list and its joins are pinned to the door's body verbatim, and the miss
        leg's alias list to every column: a column added for the notice cannot
        reach the removal and not the miss."""
        import inspect
        import re

        body = " ".join(function_body("fn_planned_misses").split())
        columns, joins = prompts._NOTICE_SELECT.split("FROM", 1)
        assert " ".join(columns.split()) in body
        assert " ".join(f"FROM{joins}".split()) in body
        leg = inspect.getsource(prompts.sweep_planned_misses)
        names = [
            c.strip().split(".")[-1] for c in columns.split("SELECT", 1)[1].split(",")
        ]
        for name in names:
            assert f"o_{name} AS {name}" in leg, f"the miss leg does not read {name}"
        read = {alias for _, alias in re.findall(r"o_(\w+) AS (\w+)", leg)}
        assert read - {"reason"} == set(names), (
            "the miss leg reads a column the removal's notice row does not carry"
        )


def _miss_row(intent, ws, reason="late"):
    return {
        "id": intent,
        "workspace_id": ws,
        "reason": reason,
        "schedule_slot_at": SLOT,
        "file_name": "drop.jpg",
        "handle": "brand",
        "tz": "America/New_York",
        "scheduled_by_user_id": "u-1",
    }


class TestTheMissLeg:
    async def test_each_miss_is_ended_with_its_reason_and_told_in_its_savepoint(
        self, monkeypatch
    ):
        from src.services.target import identity, outbox

        told, looked_up, named = [], [], []

        async def say(session, row, *, reason, surface, by, repeats=None):
            assert repeats is None, "none of these stories repeats"
            told.append((row["id"], reason, session.savepoints, surface, by))
            return 1 if surface else outbox.UNDELIVERABLE

        async def bindings(session, workspace_id):
            looked_up.append(workspace_id)
            return ["b-1"] if workspace_id == "ws-1" else []

        async def member_name_for(session, *, user_id, workspace_id):
            named.append((workspace_id, user_id))
            return "Dana"

        monkeypatch.setattr(prompts, "say_not_served", say)
        monkeypatch.setattr(prompts, "push_bindings", bindings)
        monkeypatch.setattr(identity, "member_name_for", member_name_for)
        session = _SweepSession(
            misses=[
                _miss_row("i-1", "ws-1", "item_locked"),
                _miss_row("i-2", "ws-2", "paused"),
                _miss_row("i-3", "ws-1", "late"),
                _miss_row("i-4", "ws-1", "late"),
            ],
            raced={"i-3"},
        )
        counts = await prompts.sweep_planned_misses(session, limit=7, late_seconds=900)
        assert counts == {"missed": 3, "unheard": 1}
        assert told == [
            ("i-1", "item_locked", 1, ["b-1"], "Dana"),
            ("i-4", "late", 3, ["b-1"], "Dana"),
            ("i-2", "paused", 4, [], "Dana"),
        ], (
            "told inside the savepoint that ended it; a row served or flagged"
            " since the door read it (i-3) is neither ended nor told"
        )
        assert looked_up == ["ws-1", "ws-2"], "one surface lookup per workspace"
        assert named == [("ws-1", "u-1"), ("ws-2", "u-1")], (
            "one name lookup per scheduler and workspace per sweep: whether they"
            " are still a member is the workspace's to say (#485)"
        )
        door = next(p for s, p in session.statements if "fn_planned_misses" in s)
        assert door == {"lim": 7, "late": 900.0}
        updates = [(s, p) for s, p in session.statements if s.startswith("UPDATE")]
        assert [p["id"] for _, p in updates] == ["i-1", "i-3", "i-4", "i-2"]
        sql, params = updates[0]
        for guard in (
            "state = 'expired'",
            "WHERE id = :id AND workspace_id = :ws",
            "state = 'scheduled'",
            "origin = 'planned'",
            "NOT cancel_requested",
            "schedule_slot_at = :slot",
        ):
            assert guard in sql, guard
        assert params["ws"] == "ws-1"
        assert params["slot"] == SLOT, "the time the door read"
        assert json.loads(params["e"]) == {
            "v": 1,
            "class": "planned_missed",
            "message": "item_locked",
        }
        last_sql, last_params = session.statements[-1]
        assert (
            "set_config('app.tenant_id'" in last_sql and "" in last_params.values()
        ), "the caller's scope is handed back"

    async def test_one_rows_fault_is_logged_and_the_sweep_goes_on(
        self, monkeypatch, caplog
    ):
        async def say(session, row, *, reason, surface, by, repeats=None):
            if row["id"] == "i-1":
                raise RuntimeError("a zone, a lost binding")
            return 1

        async def bindings(session, workspace_id):
            return ["b-1"]

        async def no_name(session, user_id, workspace_id, names=None):
            return None

        monkeypatch.setattr(prompts, "say_not_served", say)
        monkeypatch.setattr(prompts, "push_bindings", bindings)
        monkeypatch.setattr(prompts, "_scheduler_name", no_name)
        session = _SweepSession(
            misses=[_miss_row("i-1", "ws-1"), _miss_row("i-2", "ws-1")]
        )
        with caplog.at_level(logging.ERROR):
            counts = await prompts.sweep_planned_misses(
                session, limit=5, late_seconds=900
            )
        assert counts == {"missed": 1, "unheard": 0}
        assert session.rolled_back == 1, "the faulty row's savepoint is undone"
        assert "planned-miss sweep: intent i-1 skipped" in caplog.text


class TestTheMissLegAndARepeat:
    """A repeating story's miss: a notice says what became of its
    series, read from the ledger after the miss; a repeat's occurrence born
    after its time ends with no notice at all."""

    @pytest.fixture
    def told(self, monkeypatch):
        told = []

        async def say(session, row, *, reason, surface, by, repeats=None):
            told.append((row["id"], reason, repeats))
            return 1

        async def bindings(session, workspace_id):
            return ["b-1"]

        async def no_name(session, user_id, workspace_id, names=None):
            return None

        monkeypatch.setattr(prompts, "say_not_served", say)
        monkeypatch.setattr(prompts, "push_bindings", bindings)
        monkeypatch.setattr(prompts, "_scheduler_name", no_name)
        return told

    async def test_the_notice_says_whether_the_next_occurrence_now_waits(self, told):
        session = _SweepSession(
            misses=[_miss_row("i-1", "ws-1", "late"), _miss_row("i-2", "ws-1", "late")],
            ended={"i-1": {"repeating": True}, "i-2": {"repeating": True}},
            waits={"i-1"},
        )
        counts = await prompts.sweep_planned_misses(session, limit=5, late_seconds=900)
        assert counts == {"missed": 2, "unheard": 0}
        assert told == [("i-1", "late", True), ("i-2", "late", False)], (
            "by what the ledger holds after the miss, not by the reason"
        )
        reads = [p for sql, p in session.statements if "AS waits" in sql]
        assert reads == [
            {"ws": "ws-1", "media": "m-i-1", "acct": "a-i-1"},
            {"ws": "ws-1", "media": "m-i-2", "acct": "a-i-2"},
        ]

    async def test_a_story_without_a_rule_asks_nothing_about_a_series(self, told):
        session = _SweepSession(misses=[_miss_row("i-1", "ws-1")])
        await prompts.sweep_planned_misses(session, limit=5, late_seconds=900)
        assert told == [("i-1", "late", None)]
        assert not any("AS waits" in sql for sql, _ in session.statements)

    async def test_a_repeat_born_after_its_time_whose_series_goes_on_is_silent(
        self, told
    ):
        session = _SweepSession(
            misses=[_miss_row("i-1", "ws-1"), _miss_row("i-2", "ws-1")],
            ended={"i-1": {"born_late": True, "repeating": True}},
            waits={"i-1"},
        )
        counts = await prompts.sweep_planned_misses(session, limit=5, late_seconds=900)
        assert told == [("i-2", "late", None)], "nobody was waiting for i-1"
        assert counts == {"missed": 2, "unheard": 0}, "ended, and not counted unheard"

    async def test_a_repeat_born_after_its_time_whose_series_stops_says_so(self, told):
        """Born late, it had no waiting person; but a stop is news to the
        series' person, so it is said."""
        session = _SweepSession(
            misses=[_miss_row("i-1", "ws-1", "item_missing")],
            ended={"i-1": {"born_late": True, "repeating": True}},
        )
        await prompts.sweep_planned_misses(session, limit=5, late_seconds=900)
        assert told == [("i-1", "item_missing", False)]


class TestSayNotServed:
    async def test_it_writes_one_notice_per_binding_with_the_scheduler_named(
        self, monkeypatch
    ):
        from src.services.target import outbox

        written = []

        async def fanout(session, *, workspace_id, bindings, text, intent_id=None):
            written.append((workspace_id, list(bindings), text, intent_id))
            return len(bindings)

        monkeypatch.setattr(outbox, "fanout_notification", fanout)
        got = await prompts.say_not_served(
            None,
            _miss_row("i-1", "ws-1"),
            reason="late",
            surface=["b-1", "b-2"],
            by="Dana",
        )
        assert got == 2
        assert written == [("ws-1", ["b-1", "b-2"], _notice(), "i-1")]

    async def test_no_binding_is_the_undeliverable_verdict_not_a_zero(
        self, monkeypatch
    ):
        from src.services.target import outbox

        async def fanout(*a, **k):  # pragma: no cover - must not run
            raise AssertionError("nothing to write to")

        monkeypatch.setattr(outbox, "fanout_notification", fanout)
        got = await prompts.say_not_served(
            None, _miss_row("i-1", "ws-1"), reason="late", surface=[], by=None
        )
        assert got == outbox.UNDELIVERABLE


class TestSayRemovedBeforeServed:
    async def test_it_reads_the_notice_rows_and_tells_each_once(self, monkeypatch):
        """The removal's notice: the flagged stories read with `_NOTICE_SELECT`,
        its one workspace's surface looked up once, each story told once with
        the reason `account_removed` and its scheduler named."""
        from src.services.target import identity

        rows = [_miss_row("i-1", "ws-1"), _miss_row("i-2", "ws-1")]
        session = _SweepSession()

        async def execute(statement, params=None):
            session.statements.append((str(statement), params))
            return _SweepResult(rows)

        session.execute = execute
        told, looked_up, named = [], [], []

        async def say(s, row, *, reason, surface, by):
            told.append((row["id"], reason, surface, by))
            return len(surface)

        async def bindings(s, workspace_id):
            looked_up.append(workspace_id)
            return ["b-1"]

        async def member_name_for(s, *, user_id, workspace_id):
            assert workspace_id == "ws-1"
            named.append(user_id)
            return "Dana"

        monkeypatch.setattr(prompts, "say_not_served", say)
        monkeypatch.setattr(prompts, "push_bindings", bindings)
        monkeypatch.setattr(identity, "member_name_for", member_name_for)
        await prompts.say_removed_before_served(
            session, workspace_id="ws-1", intent_ids=["i-1", "i-2"]
        )
        ((sql, params),) = session.statements
        assert sql.startswith(prompts._NOTICE_SELECT)
        assert "i.workspace_id = :ws" in sql
        assert "i.id = ANY(CAST(:ids AS uuid[]))" in sql
        assert params == {"ws": "ws-1", "ids": ["i-1", "i-2"]}
        assert told == [
            ("i-1", "account_removed", ["b-1"], "Dana"),
            ("i-2", "account_removed", ["b-1"], "Dana"),
        ]
        assert (looked_up, named) == (["ws-1"], ["u-1"])


def _due_story(origin="planned", **over):
    """A row as `fn_prompts_due` answers it."""
    return {
        "id": f"i-{origin}",
        "state": "scheduled",
        "workspace_id": "ws-1",
        "schedule_slot_at": SLOT,
        "file_name": "drop.jpg",
        "media_kind": "image",
        "tz": "UTC",
        "api_publishing_enabled": True,
        "origin": origin,
        "scheduled_by_user_id": None,
        **over,
    }


def _serving(monkeypatch) -> dict:
    """The serve leg's seams, recorded: the stories it flipped, the cards it
    queued."""
    from src.services.target import intent_ledger, outbox

    seen = {"served": [], "cards": []}

    async def transition(s, intent_id, to_state):
        if to_state == "prompt_pending":
            seen["served"].append(intent_id)

    async def bindings(s, ws):
        return ["b-1"]

    async def enqueue(s, **kw):
        seen["cards"].append(kw["payload"])

    monkeypatch.setattr(intent_ledger, "transition", transition)
    monkeypatch.setattr(prompts, "push_bindings", bindings)
    monkeypatch.setattr(outbox, "enqueue", enqueue)
    return seen


class TestTheServeLegNamesTheScheduler:
    async def test_the_window_rides_to_the_door_and_the_card_names_who_scheduled_it(
        self, monkeypatch
    ):
        seen = _serving(monkeypatch)
        session = _SweepSession(
            due=[_due_story(tz="America/New_York", scheduled_by_user_id="u-1")],
            named={"i-planned": "Dana"},
            rules={
                "i-planned": {"unit": "day", "every": 1, "days": None, "at": "14:00"}
            },
        )
        await prompts.sweep_due_prompts(session, limit=5, late_seconds=900)
        door = next(p for s, p in session.statements if "fn_prompts_due" in s)
        assert door == {"lim": 5, "late": 900.0}
        (read,) = [p for s, p in session.statements if "END AS scheduled_by" in s]
        assert read == {"id": "i-planned", "ws": "ws-1"}, "one read, under its tenant"
        (card,) = seen["cards"]
        assert (
            "🗓 Scheduled by Dana · 2026-10-01 14:00 America/New_York"
            " · repeats every day at 14:00"
        ) in card["text"]


class TestTheServeLegServesAStoryAsTheDoorReadIt:
    """The door read takes no lock, and a story can change before the sweep
    reaches it, so the serve leg locks the row and reads it again: its state,
    its cancel flag and its time must be as the door read them. The race
    itself, two transactions and a lock, is the gate's
    (`test_schedule_verbs_gate.py::TestAWriteAtTheDueInstant`)."""

    @pytest.mark.parametrize("origin", ["planned", "cadence"])
    async def test_a_story_as_the_door_read_it_is_served(self, monkeypatch, origin):
        seen = _serving(monkeypatch)
        session = _SweepSession(due=[_due_story(origin)])
        counts = await prompts.sweep_due_prompts(session, limit=5, late_seconds=900)
        assert seen["served"] == [f"i-{origin}"] and counts["prompted"] == 1
        ((sql, reread),) = [(s, p) for s, p in session.statements if "FOR UPDATE" in s]
        # the tenant is the SQL's own: the gates run under the policies, which
        # would hide another workspace's row whether or not the SQL says so
        assert " WHERE id = :id AND workspace_id = :ws FOR UPDATE" in sql
        assert reread == {"id": f"i-{origin}", "ws": "ws-1"}

    @pytest.mark.parametrize("origin", ["planned", "cadence"])
    @pytest.mark.parametrize(
        "since",
        [
            {"schedule_slot_at": SLOT + timedelta(days=1)},
            {"cancel_requested": True},
            {"state": "prompt_pending"},
        ],
        ids=["moved", "flagged", "served_by_another_sweep"],
    )
    async def test_a_story_changed_since_the_door_read_it_waits_for_its_next(
        self, monkeypatch, caplog, origin, since
    ):
        seen = _serving(monkeypatch)
        caplog.set_level(logging.INFO, logger=prompts.logger.name)
        session = _SweepSession(
            due=[_due_story(origin)], changed={f"i-{origin}": since}
        )
        counts = await prompts.sweep_due_prompts(session, limit=5, late_seconds=900)
        assert seen == {"served": [], "cards": []} and counts["prompted"] == 0
        assert f"intent i-{origin} changed under the sweep" in caplog.text, (
            "never silent"
        )


class TestBothSweepsTakeTheirRowsInOneOrder:
    """Each sweep locks a row as it reaches it, so both take their rows in one
    total order: by due time, and rows that tie on workspace and due time by
    id. The rows arrive here out of that order, so a sort that kept their
    order would show."""

    async def test_the_serve_leg_takes_tied_rows_by_id(self, monkeypatch):
        seen = _serving(monkeypatch)
        session = _SweepSession(due=[_due_story(id=i) for i in ("i-3", "i-2", "i-1")])
        await prompts.sweep_due_prompts(session, limit=5, late_seconds=900)
        locked = [p["id"] for s, p in session.statements if "FOR UPDATE" in s]
        assert locked == ["i-1", "i-2", "i-3"]
        assert seen["served"] == ["i-1", "i-2", "i-3"]

    async def test_the_miss_leg_takes_tied_rows_by_id(self, monkeypatch):
        async def say(session, row, *, reason, surface, by, repeats=None):
            return 1

        async def bindings(session, workspace_id):
            return []

        async def no_name(session, user_id, workspace_id, names=None):
            return None

        monkeypatch.setattr(prompts, "say_not_served", say)
        monkeypatch.setattr(prompts, "push_bindings", bindings)
        monkeypatch.setattr(prompts, "_scheduler_name", no_name)
        session = _SweepSession(
            misses=[_miss_row(i, "ws-1") for i in ("i-3", "i-2", "i-1")]
        )
        await prompts.sweep_planned_misses(session, limit=5, late_seconds=900)
        ended = [p["id"] for s, p in session.statements if s.startswith("UPDATE")]
        assert ended == ["i-1", "i-2", "i-3"]

    async def test_the_serve_leg_takes_an_earlier_story_before_a_lower_id(
        self, monkeypatch
    ):
        seen = _serving(monkeypatch)
        later = SLOT + timedelta(hours=1)
        session = _SweepSession(
            due=[_due_story(id="i-1", schedule_slot_at=later), _due_story(id="i-2")]
        )
        await prompts.sweep_due_prompts(session, limit=5, late_seconds=900)
        assert seen["served"] == ["i-2", "i-1"]


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
                "provider": "gdrive",
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


def test_past_posts_card_media_binds_server_intent_but_drive_contract_is_unchanged():
    row = dict(
        workspace_id="ws",
        source_id="source",
        provider_file_ref="123",
        media_kind="image",
        file_name="synthetic",
        source_provider="instagram_past_posts",
        schedule_slot_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )
    past_posts = prompts._card_for(row, intent_id="server-intent")
    assert past_posts["media"]["intent_id"] == "server-intent"
    drive = prompts._card_for(
        {**row, "source_provider": "gdrive"}, intent_id="server-intent"
    )
    assert "intent_id" not in drive["media"]


#: The Instagram id a past-posts item is stored under, as its file reference. It is
#: distinctive, so a leak is found by a plain substring.
GRAPH_ID = "17841400000000771"
SLOT_WORDS = "2026-10-01 14:00 America/New_York"


def _stored(provider, **over):
    """The item of a story as `_CARD_WITH_PROVIDER` reads it. A past-posts item is
    stored under the neutral name (`instagram_catalog._project`), with the Graph
    id for its file reference."""
    past_posts = provider == "instagram_past_posts"
    return {
        "workspace_id": "ws-1",
        "source_id": "src-1",
        "provider_file_ref": GRAPH_ID if past_posts else "drive-file-ref",
        "file_name": vocabulary.PAST_POSTS_ITEM_NAME if past_posts else "sunset.jpg",
        "media_kind": "image",
        "mime_type": "image/jpeg",
        "source_provider": provider,
        "handle": None,
        "tz": "America/New_York",
        "schedule_slot_at": SLOT,
        **over,
    }


class TestAPastPostsItemOnTheTelegramSurfaces:
    """The stored name is what the card and the missed-story notice say, so an
    past-posts item is named for where it came from there, as on the web, and a
    card carries no Instagram id."""

    def test_the_card_names_the_item_and_carries_no_graph_id(self):
        card = prompts._card_for(_stored("instagram_past_posts"), intent_id=INTENT)
        assert card["text"] == f"📸 Instagram post (image)\nSlot: {SLOT_WORDS}"
        # With no account handle the caption falls back to the item's name.
        assert card["caption"] == f"📸 Instagram post\nSlot: {SLOT_WORDS}"
        # The block binds the server-selected intent and leaves out the file
        # reference, which for a past-posts item is the Graph id.
        assert card["media"] == {
            "workspace_id": "ws-1",
            "source_id": "src-1",
            "kind": "image",
            "mime": "image/jpeg",
            "file_name": "Instagram post",
            "intent_id": INTENT,
        }
        assert GRAPH_ID not in json.dumps(card)

    @pytest.mark.parametrize(
        "name", ["Instagram post, slide 3 of 10", "Instagram post, slide 3"]
    )
    def test_photo_caption_names_the_selected_slide_beside_the_account(self, name):
        card = prompts._card_for(
            _stored("instagram_past_posts", handle="example_handle", file_name=name),
            intent_id=INTENT,
        )
        assert card["caption"] == f"📸 @example_handle\n{name}\nSlot: {SLOT_WORDS}"
        assert GRAPH_ID not in json.dumps(card)

    def test_a_drive_card_beside_it_names_its_file_and_its_reference(self):
        """The positive control: the Drive card keeps its file's name and the
        reference its bytes are fetched by, and names no intent."""
        card = prompts._card_for(_stored("gdrive"), intent_id=INTENT)
        assert card["text"] == f"📸 sunset.jpg (image)\nSlot: {SLOT_WORDS}"
        assert card["caption"] == f"📸 sunset.jpg\nSlot: {SLOT_WORDS}"
        assert card["media"] == {
            "workspace_id": "ws-1",
            "source_id": "src-1",
            "ref": "drive-file-ref",
            "kind": "image",
            "mime": "image/jpeg",
            "file_name": "sunset.jpg",
        }

    async def test_the_missed_story_notice_names_the_item_as_a_post(self, monkeypatch):
        told = []

        async def fanout(session, **kwargs):
            told.append(kwargs["text"])
            return 1

        monkeypatch.setattr(prompts.outbox, "fanout_notification", fanout)
        for provider in ("instagram_past_posts", "gdrive"):
            name = _stored(provider)["file_name"]
            row = {**_miss_row("i-1", "ws-1"), "file_name": name}
            await prompts.say_not_served(
                object(), row, reason="late", surface=["b-1"], by="Dana"
            )
        notice, control = told
        words = (
            f"for @brand, scheduled for {SLOT_WORDS} by Dana: "
            f"{prompts.MISS_REASONS['late']}. Nothing was posted."
        )
        assert notice == f"🗓 Not served: Instagram post {words}"
        assert GRAPH_ID not in notice
        # The positive control: a Drive story's notice names its file.
        assert control == f"🗓 Not served: sunset.jpg {words}"


def test_past_posts_review_card_is_textual_and_never_requests_replacement_bytes():
    row = {
        **_card_input(
            workspace_id="synthetic-workspace",
            source_id="synthetic-source",
            provider_file_ref="synthetic-file",
        ),
        "state": "review_required",
        "source_provider": "instagram_past_posts",
    }
    card = prompts._card_for(row, intent_id="server-intent")
    assert "media" not in card and "caption" not in card and card["text"]


def test_drive_posted_settled_card_keeps_media_and_configure_alone():
    row = {
        **_card_input(
            workspace_id="synthetic-workspace",
            source_id="synthetic-source",
            provider_file_ref="synthetic-file",
        ),
        "state": "posted",
        "source_provider": "gdrive",
    }
    card = prompts._settled_card(row, intent_id=INTENT, line="synthetic posted outcome")
    assert "media" in card and "caption" in card
    assert card["outcome_text"] == "synthetic posted outcome"
    # A settled card keeps Configure alone.
    assert card["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "⚙️ Configure", "callback_data": f"v1:cfg:{INTENT}"}]
        ]
    }
    assert "intent_id" not in card["media"]
