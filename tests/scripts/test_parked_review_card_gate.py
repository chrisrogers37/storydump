"""A story parked before it was served gets its review card: in every bound
chat that holds no card of it, beside the notice, under real tenant roles."""

import pytest
from sqlalchemy import text

from src.services.target import instagram_publish, refusal_evidence, source_policy
from tests.scripts import test_instagram_observations_gate as observations
from tests.scripts import test_instagram_preparation_gate as preparation
from tests.scripts import test_instagram_sources_gate as foundation
from tests.scripts.test_w6_sync_gate import _bind

past_posts_engine = foundation.past_posts_engine
origin = foundation.origin
pytestmark = pytest.mark.integration


def _outbox_rows(sync_conn, intent):
    """The story's outbox rows as (chat, kind, state, payload)."""
    with sync_conn.cursor() as cur:
        cur.execute(
            "SELECT binding_id::text, kind, state, payload FROM channel_outbox"
            " WHERE intent_id = %s ORDER BY kind, binding_id",
            (intent,),
        )
        rows = cur.fetchall()
    sync_conn.commit()
    return rows


async def _park_unsupported(past_posts_engine, origin, intent):
    selected, _ = preparation.service(past_posts_engine, origin, content=b"unsupported")
    c = instagram_publish.PastPostsPipeline(
        capability=foundation.capability(),
        fetch_selected=selected.fetch_selected,
        prepare_selected=selected.selected_prepare,
    )
    await c.selected_prepare(
        engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
    )


async def test_a_story_parked_before_it_was_served_gets_its_review_card(
    past_posts_engine, origin, sync_conn
):
    """A slot-time refusal parks the story from `scheduled`, before the serve
    leg wrote any card, so the park's courtesy writes one: its review card in
    every bound chat, beside the notice."""
    chats = [_bind(sync_conn, origin["ws"]) for _ in range(2)]
    src, item, intent = await preparation.selected(past_posts_engine, origin)
    await _park_unsupported(past_posts_engine, origin, intent)
    rows = _outbox_rows(sync_conn, intent)
    cards = [r for r in rows if r[1] == "approval_prompt"]
    assert sorted(r[0] for r in cards) == sorted(chats), "one card per bound chat"
    for _, _, state, payload in cards:
        # The sender renders it again at claim and sends it bare; its review
        # keyboard comes by the edit that follows (the sender gate's tests).
        assert state == "pending"
        assert payload["outcome_text"].startswith("👀 Needs review")
        assert "media" not in payload, "a review fetches no past-posts bytes"
    notices = [r for r in rows if r[1] == "notification"]
    assert sorted(r[0] for r in notices) == sorted(chats), "the notice goes too"


async def test_a_parked_storys_review_card_and_notice_say_why_it_was_refused(
    past_posts_engine, origin, sync_conn
):
    """The card the park writes carries its refusal's reason under its outcome
    line, and the notice says the same: both read the story's recorded refusal."""
    _bind(sync_conn, origin["ws"])
    src, item, intent = await preparation.selected(past_posts_engine, origin)
    await _park_unsupported(past_posts_engine, origin, intent)
    with sync_conn.cursor() as cur:
        cur.execute(
            "SELECT last_error -> 'error' -> 'refusal' FROM post_intents WHERE id = %s",
            (intent,),
        )
        (refusal,) = cur.fetchone()
    sync_conn.commit()
    sentence = refusal_evidence.plain(refusal)
    assert sentence, "the park records a refusal with something to say"
    rows = _outbox_rows(sync_conn, intent)
    (card,) = [r[3] for r in rows if r[1] == "approval_prompt"]
    assert card["outcome_text"].startswith("👀 Needs review")
    assert card["outcome_text"].endswith(f"\nRefused: {sentence}.")
    (notice,) = [r[3] for r in rows if r[1] == "notification"]
    assert f"Refused: {sentence}." in str(notice)
    assert "unavailable or changed" not in str(notice)


async def test_a_park_with_no_refusal_evidence_says_its_admission_reason(
    past_posts_engine, origin, sync_conn
):
    """A preparation that is not admitted for a reason it carries no evidence
    for (the chosen post was removed) parks the story, and the notice says that
    reason, never that no reason was recorded."""
    _bind(sync_conn, origin["ws"])
    src, item, intent = await preparation.selected(past_posts_engine, origin)
    selected, _ = preparation.service(past_posts_engine, origin)
    await selected.selected_prepare(
        engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
    )

    async def refuse(**kwargs):
        raise source_policy.MediaAdmissionPending("asset_unavailable")

    c = instagram_publish.PastPostsPipeline(
        capability=foundation.capability(),
        fetch_selected=selected.fetch_selected,
        prepare_selected=refuse,
    )
    await c.selected_prepare(
        engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
    )
    (notice,) = [
        r[3] for r in _outbox_rows(sync_conn, intent) if r[1] == "notification"
    ]
    assert "Refused: the chosen post is no longer available." in str(notice)


async def test_a_story_whose_item_was_removed_parks_with_give_up_alone(
    past_posts_engine, origin, sync_conn
):
    """A story whose item is removed still parks for review with its review
    card in every bound chat, and the keyboard its card carries is Give up
    alone: the item cannot post again, and giving the story up ends the
    review."""
    from src.services.target import prompts

    chats = [_bind(sync_conn, origin["ws"]) for _ in range(2)]
    src, item, intent = await preparation.selected(past_posts_engine, origin)
    selected, _ = preparation.service(past_posts_engine, origin)
    await selected.selected_prepare(
        engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
    )
    # A removed item fails suitability, so the story binds its selected asset
    # while the item is available; the item is removed, and its preparation
    # then reports the chosen post gone.
    async with observations.uow(past_posts_engine, origin) as conn:
        await conn.execute(
            text("UPDATE media_items SET state = 'removed' WHERE id = :id"),
            dict(id=item.media_item_id),
        )

    async def refuse(**kwargs):
        raise source_policy.MediaAdmissionPending("asset_unavailable")

    c = instagram_publish.PastPostsPipeline(
        capability=foundation.capability(),
        fetch_selected=selected.fetch_selected,
        prepare_selected=refuse,
    )
    await c.selected_prepare(
        engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
    )
    cards = [r for r in _outbox_rows(sync_conn, intent) if r[1] == "approval_prompt"]
    assert sorted(r[0] for r in cards) == sorted(chats), "one card per bound chat"
    for _, _, state, payload in cards:
        assert state == "pending"
        assert payload["outcome_text"].startswith("👀 Needs review")
    async with observations.uow(past_posts_engine, origin) as conn:
        keyboard = await prompts.card_keyboard_for(
            conn, workspace_id=str(origin["ws"]), intent_id=intent
        )
    assert keyboard == {
        "inline_keyboard": [
            [{"text": "🚫 Give up", "callback_data": f"v1:giveup:{intent}"}]
        ]
    }


@pytest.mark.parametrize(
    "state,ref,second",
    [("sent", "'41'", False), ("superseded", "NULL", True), ("failed", "NULL", True)],
)
async def test_a_chat_gets_a_second_card_only_if_the_first_never_reached_it(
    past_posts_engine, origin, sync_conn, state, ref, second
):
    """A chat holds the story's card once a row of it carries a message ref,
    or may still send one. A card retired or failed before it was sent left
    the chat nothing, so it is owed the review card too."""
    has_card, has_none = [_bind(sync_conn, origin["ws"]) for _ in range(2)]
    src, item, intent = await preparation.selected(past_posts_engine, origin)
    async with observations.uow(past_posts_engine, origin) as conn:
        for state_name in ("prompt_pending", "awaiting_approval"):
            await conn.execute(
                text("UPDATE post_intents SET state=:state WHERE id=:id"),
                dict(state=state_name, id=intent),
            )
    with sync_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO channel_outbox (workspace_id, binding_id, kind, intent_id,"
            " payload, state, attempts, external_message_ref)"
            ' VALUES (%s, %s, \'approval_prompt\', %s, \'{"v": 2, "text": "card"}\','
            f" %s, 1, {ref})",
            (origin["ws"], has_card, intent, state),
        )
    sync_conn.commit()
    await _park_unsupported(past_posts_engine, origin, intent)
    rows = _outbox_rows(sync_conn, intent)
    kinds = {
        chat: sorted((kind, state) for c, kind, state, _ in rows if c == chat)
        for chat in (has_card, has_none)
    }
    if second:
        assert kinds[has_card] == sorted(
            [
                ("approval_prompt", state),
                ("approval_prompt", "pending"),
                ("notification", "pending"),
            ]
        ), "a card that never reached the chat is no card: the review card is owed"
    else:
        assert kinds[has_card] == [
            ("approval_prompt", "sent"),
            ("notification", "pending"),
            ("prompt_supersede", "pending"),
        ], "the card it has is restated, and no second card is written"
    assert kinds[has_none] == [
        ("approval_prompt", "pending"),
        ("notification", "pending"),
    ]


async def test_a_changed_slide_parked_by_the_senders_last_try_says_why(
    past_posts_engine, origin, sync_conn
):
    """The card sender's fetch can spend a changed slide's last try. It latches
    the slide with the read's `slide_changed` evidence, and the sweep's park
    that meets the latch next records that evidence and says it, never that the
    chosen post is unavailable."""
    _bind(sync_conn, origin["ws"])
    src, item, intent = await preparation.selected(past_posts_engine, origin)
    # One try, so the sender's fetch spends the last one.
    changed, _ = preparation.service(
        past_posts_engine, origin, changed_child="456", truncated=True
    )
    with pytest.raises(source_policy.MediaAdmissionPending) as spent:
        await changed.fetch_selected(
            engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
        )
    evidence = spent.value.evidence
    assert spent.value.reason == "asset_unavailable"
    assert evidence["kind"] == "slide_changed"
    c = instagram_publish.PastPostsPipeline(
        capability=foundation.capability(),
        fetch_selected=changed.fetch_selected,
        prepare_selected=changed.selected_prepare,
    )
    await c.selected_prepare(
        engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
    )
    with sync_conn.cursor() as cur:
        cur.execute(
            "SELECT state, last_error -> 'error' -> 'refusal' FROM post_intents WHERE id = %s",
            (intent,),
        )
        state, refusal = cur.fetchone()
    sync_conn.commit()
    assert state == "review_required" and refusal == evidence
    sentence = refusal_evidence.plain(evidence)
    assert sentence.startswith("Instagram's slide list was incomplete")
    (notice,) = [
        r[3] for r in _outbox_rows(sync_conn, intent) if r[1] == "notification"
    ]
    assert f"Refused: {sentence}." in str(notice)
    assert "no longer available" not in str(notice)


async def test_the_publish_jobs_park_records_and_says_its_refusal(
    past_posts_engine, origin, sync_conn
):
    """A publish job whose preparation is refused with evidence parks the story
    with that evidence beside its reason, and its notice says it."""
    from src.services.target import publish_pipeline
    from src.services.target.meta_adapter import StubMetaAdapter
    from tests.scripts import test_instagram_publish_gate as publish_gate

    _bind(sync_conn, origin["ws"])
    src, item, intent, job, c, _ = await publish_gate.ready(past_posts_engine, origin)
    async with observations.uow(past_posts_engine, origin) as conn:
        await conn.execute(
            text("UPDATE post_intents SET state='approved' WHERE id=:id"),
            dict(id=intent),
        )
    changed = {"v": 1, "kind": "slide_changed", "children": 7, "complete": False}

    async def refuse(**kwargs):
        raise source_policy.MediaAdmissionPending("asset_unavailable", evidence=changed)

    async def forbidden_fetch(subject):
        pytest.fail("a refused past post fetches no media")

    c.prepare_capture = refuse
    assert (
        await publish_pipeline.run_publish_pipeline(
            job,
            engine=past_posts_engine,
            meta=StubMetaAdapter(),
            transit=publish_gate.Transit(),
            media_fetch=forbidden_fetch,
            past_posts=c,
        )
        == publish_pipeline.POISONED
    )
    with sync_conn.cursor() as cur:
        cur.execute(
            "SELECT state, last_error -> 'error' -> 'refusal' FROM post_intents WHERE id = %s",
            (intent,),
        )
        state, refusal = cur.fetchone()
    sync_conn.commit()
    assert state == "review_required" and refusal == changed
    (notice,) = [
        r[3] for r in _outbox_rows(sync_conn, intent) if r[1] == "notification"
    ]
    assert (
        "Refused: Instagram's slide list was incomplete (7 listed), so the chosen"
        " slide could not be confirmed." in str(notice)
    )
