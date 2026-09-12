"""W3 — prompt production: the card, designed first-principles under two
rulings (#790, Chris 2026-08-21): a tap PUBLISHES (Fork 1 — post-now is the
user contract; internals may queue), and parity is out (the button set serves
the next state, with the live loop preserved: autopost/skip/posted/reject are
the four callbacks production actually uses, and none may be dropped
silently).

The ruled card: **Post now** (iff the workspace can publish via API) ·
**Posted myself** (the manual-mode path the matrix specifies at
awaiting→posted) · **Skip** · **Reject**, plus the zero-backend Instagram
deeplink button legacy carried. Divergences from legacy are enumerated in the
PR body, never ridden.
"""

import json
from datetime import datetime

import pytest

from src.services.target import prompts


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
    4 and 5 (#969). Width 5 is the incident's real shape (``.05024``).

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
            got = prompts._canonical_fraction(raw)
            if width in (0, 6):
                assert got == raw, f"width {width} must pass through untouched"
            else:
                assert got == self._canonical6(raw), (
                    f"width {width} not canonicalized: {got}"
                )
                assert len(got.split(".", 1)[1].split("+")[0]) == 6
            datetime.fromisoformat(got)


class TestCardRender:
    def test_api_enabled_card_carries_the_ruled_four_plus_deeplink(self):
        payload = prompts.render_card(_card_input(), api_publishing_enabled=True)
        assert payload["v"] == 2 and payload["text"]
        labels = [b.get("text") for b in _buttons(payload)]
        assert any("Post now" in n for n in labels), "Fork 1: a tap publishes"
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
        assert actions == {"post", "posted", "skip", "reject"}

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
        """The web queue is a surface (#1033): a workspace with no push binding
        still has somewhere to act, so the intent leaves `scheduled` and simply
        gets no card. Before #1033 this case stayed `scheduled` and was reaped —
        on exactly the Google-only workspaces the product exists for."""
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
    """Legacy parity (owner, 2026-09-08): the approval card is the PHOTO (or
    video) with the account and the slot as its caption — not a filename. The
    renderer names the media for the transport to fetch; the text stays as the
    fallback card when the file cannot be sent."""

    def _with_media(self, **over):
        fields = dict(
            handle="gatortails",
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
        assert payload["caption"].startswith("📸 @gatortails")
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
    """One formatter for the outcome line and the slot line (phase 1 step 5):
    `%Y-%m-%d %H:%M` in the workspace tz, the state's word, the tapper."""

    def test_the_line_names_the_state_the_person_and_the_time_in_workspace_tz(self):
        from datetime import datetime, timezone

        at = datetime(2026, 9, 9, 18, 14, tzinfo=timezone.utc)
        line = prompts.outcome_line(
            "approved", by="Chris", at=at, tz="America/New_York"
        )
        assert line == "✅ Approved by Chris · 2026-09-09 14:14 America/New_York"

    def test_without_a_person_the_line_still_says_what_happened(self):
        from datetime import datetime, timezone

        at = datetime(2026, 9, 9, 18, 14, tzinfo=timezone.utc)
        assert prompts.outcome_line("expired", by=None, at=at, tz="UTC") == (
            "⌛ Expired — slot passed · 2026-09-09 18:14 UTC"
        )

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


class TestTheReviewKeyboard:
    """A `review_required` card is the workspace's to resolve (2026-09-12): it
    offers the three resolutions a member may take — post again, it posted,
    give up — as callback buttons minted by the one token function."""

    def test_it_offers_the_three_resolutions_in_two_rows(self):
        kb = prompts.review_keyboard(INTENT)
        rows = kb["inline_keyboard"]
        assert [[b["callback_data"] for b in row] for row in rows] == [
            [f"v1:itposted:{INTENT}", f"v1:notposted:{INTENT}"],
            [f"v1:giveup:{INTENT}"],
        ]
        # The retry button's label carries the member's verdict: it is the
        # answer to "is it on your story?", which is the review's question.
        assert [b["text"] for b in rows[0]] == [
            "✅ It posted",
            "🔁 Not there — post again",
        ]
        assert rows[1][0]["text"] == "🚫 Give up"

    def test_every_button_parses_back_to_its_action(self):
        from src.services.target import callback_tokens

        for row in prompts.review_keyboard(INTENT)["inline_keyboard"]:
            for button in row:
                tap = callback_tokens.parse(button["callback_data"])
                assert tap is not None and tap.intent_id == INTENT
                assert tap.action in prompts.REVIEW_ACTIONS

    def test_it_is_json_serializable_for_the_outbox(self):
        import json

        json.dumps(prompts.review_keyboard(INTENT))
