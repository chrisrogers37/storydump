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
import logging
from datetime import datetime, timedelta, timezone

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
            handle="exampleshop",
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
        assert payload["caption"].startswith("📸 @exampleshop")
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


class TestARendersAgainForAResend:
    """`rerender_prompt` (2026-09-12): a card that is sent again — a lost
    answer's resend, or a first send that waited — is rendered from the
    intent AS IT IS NOW: the workspace's current buttons, and nothing at all
    when the slot has already moved on."""

    class _Session:
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

    @pytest.mark.asyncio
    async def test_a_live_slot_renders_with_the_workspaces_current_buttons(self):
        s = self._Session(
            {
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
        )
        payload = await prompts.rerender_prompt(s, intent_id=INTENT)
        assert payload is not None
        tokens = [
            b.get("callback_data")
            for r in payload["reply_markup"]["inline_keyboard"]
            for b in r
        ]
        assert f"v1:post:{INTENT}" in tokens
        assert "WHERE i.id = :id" in s.sql[0][0]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", ["skipped", "expired", "approved", "posted"])
    async def test_a_slot_that_moved_on_renders_nothing(self, state):
        s = self._Session(
            {"id": INTENT, "state": state, "api_publishing_enabled": True}
        )
        assert await prompts.rerender_prompt(s, intent_id=INTENT) is None

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
            }
        )
        payload = await prompts.rerender_prompt(s, intent_id=INTENT)
        assert payload is not None and "v1:post:" not in str(payload)


class TestWaitingLine:
    """The approved card's line while its story waits for a slot that is
    hours away (plan 03, UX principle 1): the state's word and when it posts,
    in the workspace's zone. Structural review of #1306: the gate pinned only
    the prefix, so a wrong day or time passed."""

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
        Since 082 the prompt sweep's first card comes from `fn_prompts_due`,
        whose body spells the same columns and joins — a door cannot call the
        Python — so the fragment is pinned to the door's body verbatim, and the
        sweep's alias list to every column: a column added for `render_card`
        cannot reach the resend and not the sweep (#1349 re-verify). An
        applied file is immutable: a column change means a fix-forward door,
        and this pin moves to the new file with it — 089 re-created the door
        with the origin and the scheduler, so it reads 089."""
        import inspect

        from scripts.migration_runner import MIGRATIONS_DIR
        from src.services.target import prompts

        ddl = (MIGRATIONS_DIR / "089_planned_serve_and_misses.sql").read_text()
        body = ddl.split("CREATE FUNCTION fn_prompts_due(", 1)[1].split("$$;", 1)[0]
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

    def __init__(self, *, due=(), pending=(), misses=(), raced=(), changed=None):
        self.due, self.pending, self.misses = list(due), list(pending), list(misses)
        self.raced, self.changed = set(raced), dict(changed or {})
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
            return _SweepResult([], first=(params["id"],))
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
        aborts the transaction: without a savepoint the next statement — the
        next row's transition, or the hand-back of the caller's scope — raises
        `InFailedSqlTransaction`, and one raced row fails the whole sweep
        (#1349 re-verify)."""
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

        monkeypatch.setattr(prompts.intent_ledger, "transition", transition)
        counts = await prompts.sweep_due_prompts(session, limit=5, late_seconds=3600)
        assert counts == {"prompted": 0, "advanced": 1}
        assert calls == ["i-1", "i-2"], "the sweep goes on after a refusal"
        assert (session.savepoints, session.rolled_back) == (2, 1), (
            "each transition rides its own savepoint; the refused one rolls back"
        )
        last_sql, last_params = session.statements[-1]
        assert (
            "set_config('app.tenant_id'" in last_sql and "" in last_params.values()
        ), "the caller's scope is handed back after the refusal"

    async def test_an_intent_it_cannot_see_is_NOT_swallowed(self, monkeypatch):
        """#1423: `IntentNotVisible` is a caller's bug, not a refusal, so it
        escapes the savepoint that refusals ride. As a subclass of
        `IntentTransitionRefused` it would be swallowed right here, which is
        why the owner ruled it a distinct type."""
        from src.services.target import intent_ledger, prompts

        session = _SweepSession(pending=[{"id": "i-1", "workspace_id": "ws-1"}])

        async def transition(s, intent_id, to_state):
            raise intent_ledger.IntentNotVisible("matched no row")

        monkeypatch.setattr(prompts.intent_ledger, "transition", transition)
        with pytest.raises(intent_ledger.IntentNotVisible):
            await prompts.sweep_due_prompts(session, limit=5, late_seconds=3600)


SLOT = datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc)  # 14:00 in New York


class TestThePlannedCard:
    """A planned story's card says who scheduled it and for when (089), and
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

    def test_the_door_and_the_notice_share_one_vocabulary(self):
        """Every reason the miss door can return has words, and nothing else
        does: `account_removed` comes from the door and from a removal."""
        import re

        from scripts.migration_runner import MIGRATIONS_DIR

        ddl = (MIGRATIONS_DIR / "089_planned_serve_and_misses.sql").read_text()
        body = ddl.split("CREATE FUNCTION fn_planned_misses(", 1)[1].split("$$;", 1)[0]
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

        from scripts.migration_runner import MIGRATIONS_DIR

        ddl = (MIGRATIONS_DIR / "089_planned_serve_and_misses.sql").read_text()
        body = ddl.split("CREATE FUNCTION fn_planned_misses(", 1)[1].split("$$;", 1)[0]
        body = " ".join(body.split())
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

        async def say(session, row, *, reason, surface, by):
            told.append((row["id"], reason, session.savepoints, surface, by))
            return 1 if surface else outbox.UNDELIVERABLE

        async def bindings(session, workspace_id):
            looked_up.append(workspace_id)
            return ["b-1"] if workspace_id == "ws-1" else []

        async def display_name_for(session, *, user_id):
            named.append(user_id)
            return "Dana"

        monkeypatch.setattr(prompts, "say_not_served", say)
        monkeypatch.setattr(prompts, "push_bindings", bindings)
        monkeypatch.setattr(identity, "display_name_for", display_name_for)
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
        assert named == ["u-1"], "one name lookup per scheduler per sweep"
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
        async def say(session, row, *, reason, surface, by):
            if row["id"] == "i-1":
                raise RuntimeError("a zone, a lost binding")
            return 1

        async def bindings(session, workspace_id):
            return ["b-1"]

        async def no_name(session, user_id, names=None):
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

        async def display_name_for(s, *, user_id):
            named.append(user_id)
            return "Dana"

        monkeypatch.setattr(prompts, "say_not_served", say)
        monkeypatch.setattr(prompts, "push_bindings", bindings)
        monkeypatch.setattr(identity, "display_name_for", display_name_for)
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
        from src.services.target import identity

        seen = _serving(monkeypatch)

        async def display_name_for(s, *, user_id):
            assert user_id == "u-1"
            return "Dana"

        monkeypatch.setattr(identity, "display_name_for", display_name_for)
        session = _SweepSession(
            due=[_due_story(tz="America/New_York", scheduled_by_user_id="u-1")]
        )
        await prompts.sweep_due_prompts(session, limit=5, late_seconds=900)
        door = next(p for s, p in session.statements if "fn_prompts_due" in s)
        assert door == {"lim": 5, "late": 900.0}
        (card,) = seen["cards"]
        assert "🗓 Scheduled by Dana · 2026-10-01 14:00 America/New_York" in card["text"]


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
    """Each sweep locks a row as it reaches it, so both take rows that tie on
    workspace and due time in one total order, by id. The rows arrive here
    in the opposite order, so a sort that kept their order would show."""

    async def test_the_serve_leg_takes_tied_rows_by_id(self, monkeypatch):
        seen = _serving(monkeypatch)
        session = _SweepSession(due=[_due_story(id=i) for i in ("i-3", "i-2", "i-1")])
        await prompts.sweep_due_prompts(session, limit=5, late_seconds=900)
        locked = [p["id"] for s, p in session.statements if "FOR UPDATE" in s]
        assert locked == ["i-1", "i-2", "i-3"]
        assert seen["served"] == ["i-1", "i-2", "i-3"]

    async def test_the_miss_leg_takes_tied_rows_by_id(self, monkeypatch):
        async def say(session, row, *, reason, surface, by):
            return 1

        async def bindings(session, workspace_id):
            return []

        async def no_name(session, user_id, names=None):
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
