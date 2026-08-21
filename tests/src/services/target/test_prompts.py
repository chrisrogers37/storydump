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


class TestCardRender:
    def test_api_enabled_card_carries_the_ruled_four_plus_deeplink(self):
        payload = prompts.render_card(_card_input(), api_publishing_enabled=True)
        assert payload["v"] == 1 and payload["text"]
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
        ids = await prompts.prompt_intent(object(), row, bindings)

        assert order[0] == ("transition", intent["intent_id"], "prompt_pending")
        enqueues = [o for o in order if o[0] == "enqueue"]
        assert len(enqueues) == 2 and len(ids) == 2
        for _, kwargs in enqueues:
            assert kwargs["kind"] == "approval_prompt"
            assert kwargs["intent_id"] == intent["intent_id"]
            assert kwargs["workspace_id"] == "ws-1"
            assert "Post now" in str(kwargs["payload"])
        assert {k["binding_id"] for _, k in enqueues} == {"b-1", "b-2"}

    async def test_no_active_binding_means_no_prompt_and_no_transition(
        self, monkeypatch
    ):
        called = []

        async def fake_transition(session, intent_id, to_state):
            called.append(to_state)

        monkeypatch.setattr(prompts.intent_ledger, "transition", fake_transition)
        row = {"id": "i-1", "workspace_id": "ws-1", "api_publishing_enabled": False}
        ids = await prompts.prompt_intent(object(), row, [])
        assert ids == [] and called == [], (
            "a promptless transition would strand the intent in prompt_pending"
            " with no card anywhere — the no-surface case stays 'scheduled'"
        )
