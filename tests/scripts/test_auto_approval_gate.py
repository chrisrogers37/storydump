"""Gate: the serve step approves a story automatically, or leaves it
to a person.

Driven against the replayed advertised stream, every sweep as `svc_worker`
with no tenant, exactly as the worker's prompt sweeper runs it
(`prompts.sweep_due_prompts`, called directly: the cadence fast path runs the
same sweep with ``limit=1`` and no ORDER BY, so nothing here assumes it picks
the story a test minted).

- **What approves, and how.** A story a rule approves ends `approved`, stamped
  with the rule, with exactly the `publish_pipeline` job a tap mints, an audit
  row naming the system, and its cards left `pending` for the sender to send
  settled. YOLO for a cadence and a planned story; the item's flag; the
  folder's position over the workspace's; Returning on an earlier API post; a
  dry run with no login.
- **What waits.** Each block in the decision's step 1, Returning on any
  earlier post but an API one, and a fault inside the decision: each leaves
  the story `awaiting_approval` with no rule and no job, and a fault leaves
  the rest of the sweep to go on.
- **The ledger is the authority.** The serve path's actor is the one
  `tg_intent_person_or_rule` (approval rules) requires; `test_intent_ledger_gate.py`
  covers the rule in depth.
- **The card's life.** A rule-approved story's card goes out once, settled,
  however far the story got before the sender claimed it; the settled sweep
  spares it for `SETTLED_CARD_GRACE` and then retires it; a later line
  replaces its outcome instead of stacking under it.

The world is module-scoped (one replay); each test seeds its own workspace,
and the sweeps are estate-wide by design, so each asserts on its own rows.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import text

from src.channels.telegram_transport import SendReceipt
from src.services.target import (
    auto_approval,
    commands,
    drive_credentials,
    google_drive_adapter,
    google_drive_oauth,
    intent_ledger,
    outbox,
    prompts,
    unit_of_work,
    vocabulary,
)
from src.services.target import media_adapters
from src.services.target.commands import Command
from src.services.target.work_loop import WorkerConfig
from src.services.target.oauth_states import ring
from src.worker import _publish_media_fetch
from src.utils.datetime_utils import utcnow
from tests.scripts.conftest import (
    _scratch,
    as_user,
    in_tenant,
    ingress_engine,
    build_world,
)
from tests.scripts.test_planned_serve_gate import (
    _checked,
    _exec,
    _owner_cursor,
    _seed_story,
    _serve,
    _worker,
    _workspace,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

#: A link the item carries: the person adds it by hand, so a story with one is
#: never approved automatically (the decision's step 1).
LINK = "https://example.com/the-link"

#: Every state an Instagram login can be in but `active` (`ck_credentials_state`).
#: None is a working login (`ig_credentials.usable_login_sql`), so each blocks a
#: rule as no login at all does.
DEAD_STATES = ("expired", "revoked")


@pytest.fixture(scope="module")
def world(admin_conn, owner_actor):
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        dsn = build_world(db, owner_actor, admin_conn)
        yield {"owner": dsn}
    finally:
        gen.close()


# --- seeding, as the migration actor ------------------------------------------


def _positioned(
    world,
    name,
    *,
    mode="auto",
    folder=None,
    api=True,
    dry_run=False,
):
    """`_workspace` (an owner, a folder, a push binding) at the position
    *mode*, its folder at *folder* (NULL: the workspace's), with API posting
    *api* and dry run *dry_run*."""
    w = _workspace(world, f"{name}-{uuid.uuid4().hex[:6]}")
    with _owner_cursor(world) as cur:
        cur.execute(
            "UPDATE workspaces SET approval_mode = %s, api_publishing_enabled = %s,"
            " dry_run_mode = %s WHERE id = %s",
            (mode, api, dry_run, w["ws"]),
        )
        cur.execute(
            "UPDATE media_sources SET approval_mode = %s WHERE id = %s",
            (folder, w["src"]),
        )
    return w


def _story(
    world,
    w,
    *,
    origin="cadence",
    login=True,
    login_state="active",
    auto_approve=False,
    link=None,
    cancel=False,
    state="scheduled",
):
    """One story due now on its own item and account (`_seed_story`): with an
    Instagram Login for the account in *login_state* unless *login* is False,
    the item's Auto-approve flag *auto_approve* (`media_items.auto_approve`,
    the decision's `Facts.flagged`), its link *link*, and a cancel pending
    when *cancel* (`_seed_story`'s *flagged*, which is `cancel_requested`)."""
    with _owner_cursor(world) as cur:
        story = _seed_story(cur, w, origin=origin, flagged=cancel, state=state)
        if login:
            cur.execute(
                "INSERT INTO oauth_credentials (workspace_id, ig_account_id,"
                " provider, encrypted_payload, state)"
                " VALUES (%s, %s, %s, 'ciphertext', %s)",
                (w["ws"], story["acct"], vocabulary.PROVIDER_IG_LOGIN, login_state),
            )
        cur.execute(
            "UPDATE media_items SET auto_approve = %s, link_url = %s WHERE id = %s",
            (auto_approve, link, story["media"]),
        )
    return story


#: A `posted` row as each path leaves it (`ck_posted_complete`): the columns
#: beyond `published_via` and the day's debit that each path's CHECK asks for.
_POSTED_BY = {
    "api": {"ig_container_id": "c-earlier", "publish_step": "effect_confirmed"},
    "dry_run": {"publish_step": "effect_confirmed"},
    "manual": {},
}


def _posted_before(world, w, story, via):
    """An earlier story of the same item on the same account, `posted` by
    *via* a day ago, inserted as the migration actor (history is the one
    thing born terminal)."""
    extra = _POSTED_BY[via]
    columns = "".join(f", {c}" for c in extra)
    values = ", %s" * len(extra)
    with _owner_cursor(world) as cur:
        cur.execute(
            "INSERT INTO post_intents (workspace_id, ig_account_id, media_item_id,"
            " provider_account_ref, approval_mode, schedule_slot_at, state,"
            f" published_via, cap_consumed_on{columns})"
            " VALUES (%s, %s, %s, %s, 'manual', now() - interval '1 day',"
            f" 'posted', %s, current_date{values}) RETURNING id",
            (w["ws"], story["acct"], story["media"], _ref(story), via, *extra.values()),
        )
        return str(cur.fetchone()[0])


def _ref(story):
    return f"acct-{story['acct_tag']}"


def _post(world, story):
    """`approved -> publishing -> posted` through the API, as the publish
    pipeline leaves it: the system's moves, every completeness CHECK met."""
    with _owner_cursor(world) as cur:
        cur.execute("SET LOCAL app.actor_kind = 'system'")
        cur.execute(
            "UPDATE post_intents SET state = 'publishing', cap_consumed_on = current_date,"
            " publish_step = 'publish_called', ig_container_id = 'c-1' WHERE id = %s",
            (story["intent"],),
        )
        cur.execute(
            "UPDATE post_intents SET state = 'posted', published_via = 'api',"
            " publish_step = 'effect_confirmed' WHERE id = %s",
            (story["intent"],),
        )


def _park(world, story):
    """`approved -> publishing -> review_required`, as the publish pipeline
    parks a story it could not finish: the system's moves."""
    with _owner_cursor(world) as cur:
        cur.execute("SET LOCAL app.actor_kind = 'system'")
        cur.execute(
            "UPDATE post_intents SET state = 'publishing', cap_consumed_on = current_date,"
            " publish_step = 'publish_called', ig_container_id = 'c-1' WHERE id = %s",
            (story["intent"],),
        )
        cur.execute(
            "UPDATE post_intents SET state = 'review_required' WHERE id = %s",
            (story["intent"],),
        )


# --- reads --------------------------------------------------------------------


def _row(world, story):
    ((state, rule),) = _exec(
        world,
        "SELECT state, approval_rule FROM post_intents WHERE id = %s",
        (story["intent"],),
        fetch=True,
    )
    return state, rule


def _jobs(world, story):
    return _exec(
        world,
        "SELECT kind, serialization_key, payload, deadline_at, workspace_id::text"
        "  FROM jobs WHERE payload->>'intent_id' = %s",
        (story["intent"],),
        fetch=True,
    )


def _approvals(world, story):
    """The story's `awaiting_approval -> approved` audit rows: who."""
    return _exec(
        world,
        "SELECT actor_kind, actor_user_id FROM audit_events"
        " WHERE entity_kind = 'post_intent' AND entity_id = %s"
        "   AND from_state = 'awaiting_approval' AND to_state = 'approved'",
        (story["intent"],),
        fetch=True,
    )


def _cards(world, story, kind="approval_prompt"):
    return _exec(
        world,
        "SELECT state, external_message_ref, payload FROM channel_outbox"
        " WHERE intent_id = %s AND kind = %s ORDER BY created_at",
        (story["intent"], kind),
        fetch=True,
    )


def _approved_by(world, w, story, rule, *, dry_run=False, cards=1):
    """*story* was approved automatically by *rule*: stamped, the one
    `publish_pipeline` job a tap mints (keyed on the account, no deadline,
    the dry-run decision in the payload), the system's audit row, and its
    cards left `pending` for the sender to send settled."""
    assert _row(world, story) == ("approved", rule)
    ((kind, key, payload, deadline, ws),) = _jobs(world, story)
    assert (kind, key, ws) == ("publish_pipeline", f"ig:{_ref(story)}", w["ws"])
    assert payload == {"v": 1, "intent_id": story["intent"], "dry_run": dry_run}
    assert deadline is None, "the pipeline's ceiling is its own (NO_DEADLINE)"
    assert _approvals(world, story) == [("system", None)]
    assert [state for state, _, _ in _cards(world, story)] == ["pending"] * cards


def _waits(world, story):
    """*story* was served and waits for a person: no rule, no job, no
    approval."""
    assert _row(world, story) == ("awaiting_approval", None)
    assert _jobs(world, story) == []
    assert _approvals(world, story) == []


# --- what approves ------------------------------------------------------------


class TestWhatApproves:
    def test_a_yolo_cadence_story_is_approved_at_serve(self, world):
        w = _positioned(world, "yolo")
        story = _story(world, w)
        counts = _serve(world)
        assert counts["approved"] >= 1, counts
        _approved_by(world, w, story, "auto")
        # The serve is idempotent: the next beat finds nothing to do for it.
        _serve(world)
        _approved_by(world, w, story, "auto")

    def test_a_planned_story_at_its_time_is_approved_the_same_way(self, world):
        w = _positioned(world, "yolo-planned")
        story = _story(world, w, origin="planned")
        _serve(world)
        _approved_by(world, w, story, "auto")

    def test_a_flagged_item_in_a_manual_workspace_is_approved_by_its_flag(self, world):
        w = _positioned(world, "flagged", mode="manual")
        story = _story(world, w, auto_approve=True)
        control = _story(world, w)
        _serve(world)
        _approved_by(world, w, story, "item")
        _waits(world, control)

    def test_a_disconnected_auto_folder_still_approves_but_cannot_fetch(self, world):
        """Disconnect preserves the folder's effective YOLO.

        Exercise the live credential door and the worker's real media-fetch
        wiring, with HTTP trapped locally. The full publish ladder is not
        driven here: the registry hands a paused Drive folder to its adapter,
        whose dead grant comes out as MediaCredentialDead (the redacted
        DriveCredentialDead), a StorydumpError that the fetch rung retries and
        eventually parks for review, rather than terminally failing as a
        missing file would.
        """
        w = _positioned(world, "disconnected-auto", mode="manual", folder="auto")
        story = _story(world, w)
        grant = google_drive_oauth.DriveGrant(
            "synthetic-access", "synthetic-refresh", None
        )
        encrypted = ring().encrypt(google_drive_oauth.encode_payload(grant))
        with _owner_cursor(world) as cur:
            cur.execute(
                "INSERT INTO oauth_credentials (workspace_id, provider,"
                " encrypted_payload, state) VALUES (%s, 'gdrive', %s, 'active')",
                (w["ws"], encrypted),
            )
        requests = []

        async def fetch(*, disconnected):
            async with ingress_engine(as_user(world["owner"], "svc_worker")) as engine:
                provider = drive_credentials.provider_from_engine(engine)
                if not disconnected:
                    assert (
                        await provider(w["src"], workspace_id=w["ws"])
                        == "synthetic-access"
                    )
                    return

                def http(request):
                    requests.append(request)
                    raise AssertionError("a revoked grant must never reach HTTP")

                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(http)
                ) as client:
                    adapter = google_drive_adapter.GoogleDriveAdapter(
                        token_provider=provider, client=client
                    )
                    with pytest.raises(media_adapters.MediaCredentialDead) as dead:
                        await _publish_media_fetch({"gdrive": adapter})(
                            {
                                "provider": "gdrive",
                                # disconnect_account paused the folder.
                                "source_state": "paused",
                                "source_config": {"folder_ref": "synthetic-folder"},
                                "source_id": w["src"],
                                "workspace_id": w["ws"],
                                "provider_file_ref": f"ref-{story['tag']}",
                                "media_kind": "image",
                            }
                        )
                    assert not isinstance(dead.value, media_adapters.MediaError)

        asyncio.run(fetch(disconnected=False))
        command = Command(
            kind="disconnect_account",
            workspace_id=w["ws"],
            actor_user_id=w["user"],
            channel="web",
            args={},
        )
        result = asyncio.run(
            in_tenant(
                as_user(world["owner"], "svc_ingress"),
                w["ws"],
                w["user"],
                lambda session: commands.execute(session, command),
            )
        )
        assert result.data["credential_revoked"] is True
        assert result.data["sources_paused"] == 1
        assert _exec(
            world,
            "SELECT s.state, s.approval_mode, COALESCE((s.config->>'removed')::boolean, false),"
            " c.state FROM media_sources s JOIN oauth_credentials c"
            " ON c.workspace_id = s.workspace_id AND c.provider = 'gdrive' WHERE s.id = %s",
            (w["src"],),
            fetch=True,
        ) == [("paused", "auto", False, "revoked")]
        _serve(world)
        _approved_by(world, w, story, "auto")
        asyncio.run(fetch(disconnected=True))
        assert requests == []
        assert _row(world, story) == ("approved", "auto")

    def test_a_folder_set_to_auto_in_a_manual_workspace_is_yolo(self, world):
        w = _positioned(world, "folder-auto", mode="manual", folder="auto")
        story = _story(world, w)
        _serve(world)
        _approved_by(world, w, story, "auto")

    @pytest.mark.parametrize(
        ("flagged", "rule"),
        [
            pytest.param(False, None, id="waits"),
            pytest.param(True, "item", id="unless-the-item-is-flagged"),
        ],
    )
    def test_a_folder_set_to_manual_in_a_yolo_workspace(self, world, flagged, rule):
        w = _positioned(world, "folder-manual", mode="auto", folder="manual")
        story = _story(world, w, auto_approve=flagged)
        _serve(world)
        if rule is None:
            _waits(world, story)
        else:
            _approved_by(world, w, story, rule)

    def test_returning_approves_an_item_posted_through_the_api_before(self, world):
        w = _positioned(world, "returning", mode="returning")
        story = _story(world, w)
        _posted_before(world, w, story, "api")
        never_posted = _story(world, w)
        _serve(world)
        _approved_by(world, w, story, "returning")
        _waits(world, never_posted)

    def test_a_dry_run_approves_with_no_login_and_says_so_in_its_job(self, world):
        w = _positioned(world, "dry-run", dry_run=True)
        story = _story(world, w, login=False)
        _serve(world)
        _approved_by(world, w, story, "auto", dry_run=True)


# --- what waits ---------------------------------------------------------------


class TestWhatWaits:
    def test_the_published_via_vocabulary_is_the_one_returning_reads(self, world):
        """The values the next test walks: every way a story is posted, and
        only `api` is an API post. Read from the schema, so a new value is a
        failure here before it is a silent approval."""
        assert _checked(world, "ck_intent_via") == tuple(sorted(_POSTED_BY))

    @pytest.mark.parametrize("via", ["manual", "dry_run"])
    def test_returning_waits_when_the_only_earlier_post_was_not_through_the_api(
        self, world, via
    ):
        w = _positioned(world, f"returning-{via}", mode="returning")
        story = _story(world, w)
        _posted_before(world, w, story, via)
        _serve(world)
        _waits(world, story)

    @pytest.mark.parametrize(
        "block",
        [
            pytest.param({"story": {"link": LINK}}, id="a-link-to-add"),
            pytest.param({"story": {"login": False}}, id="no-login-dry-run-off"),
            pytest.param({"workspace": {"api": False}}, id="api-posting-off"),
        ],
    )
    def test_a_block_leaves_a_yolo_story_to_a_person(self, world, block):
        """Each differs from the YOLO story above in that one fact alone. For
        a block of the story's own, a story beside it without the block is
        approved, so the block is what stops it."""
        w = _positioned(world, "blocked", **block.get("workspace", {}))
        story = _story(world, w, **block.get("story", {}))
        _serve(world)
        _waits(world, story)
        if not block.get("workspace"):
            control = _story(world, w)
            _serve(world)
            _approved_by(world, w, control, "auto")

    def test_the_login_state_vocabulary_is_the_one_a_working_login_reads(self, world):
        """The values the next test walks: every state a login can be in, and
        only `active` is a working one. Read from the schema, so a state the
        schema adds fails here until the next test walks it."""
        assert _checked(world, "ck_credentials_state") == tuple(
            sorted(("active", *DEAD_STATES))
        )

    @pytest.mark.parametrize("login_state", DEAD_STATES)
    def test_a_login_that_is_not_active_leaves_a_yolo_story_to_a_person(
        self, world, login_state
    ):
        """The serve step reads `approve`'s own precondition
        (`ig_credentials.usable_login_sql`), so a login in any state but
        `active` stops a rule as no login at all does: the story waits, and
        its card says why when it is sent. A story beside it whose login is
        active is approved, so the login's state is what stops it."""
        w = _positioned(world, f"login-{login_state}")
        story = _story(world, w, login_state=login_state)
        _serve(world)
        _waits(world, story)
        sent, _, _ = _send(world, w)
        (row,) = sent
        note = "🔒 YOLO is on, but Instagram isn't connected for"
        assert f"{note} @h_{story['tag']}." in row["payload"]["text"], row["payload"]
        control = _story(world, w)
        _serve(world)
        _approved_by(world, w, control, "auto")

    def test_a_cancel_pending_leaves_a_yolo_story_to_a_person(self, world):
        """A flagged story is never prompted (`fn_prompts_due`), but the
        advance door does not filter the flag (`fn_prompts_pending`): one
        flagged after its prompt is advanced, and must not be approved."""
        w = _positioned(world, "cancel")
        story = _story(world, w, cancel=True, state="prompt_pending")
        control = _story(world, w, state="prompt_pending")
        _serve(world)
        _waits(world, story)
        _approved_by(world, w, control, "auto", cards=0)

    @pytest.mark.parametrize("fault", ["python", "database"])
    def test_a_fault_in_the_decision_leaves_its_story_waiting_and_the_sweep_goes_on(
        self, world, monkeypatch, fault
    ):
        """The serve step's own savepoint and broad catch: whichever of
        the two stories the sweep decides first faults inside `facts`; it
        waits for a person, and the other, decided after it in the same
        sweep, is approved. A Python fault proves the catch is broad; a
        statement that fails proves the savepoint, since it aborts the
        transaction for everything after it unless that savepoint is rolled
        back."""
        w = _positioned(world, f"fault-{fault}")
        stories = [_story(world, w), _story(world, w)]
        ours = {s["intent"] for s in stories}
        real, seen = auto_approval.facts, []

        async def faulty(session, *, workspace_id, intent_id):
            if str(intent_id) in ours:
                seen.append(str(intent_id))
                if len(seen) == 1 and fault == "python":
                    raise RuntimeError("injected fault in facts")
                if len(seen) == 1:
                    await session.execute(text("SELECT 1 / 0"))
            return await real(session, workspace_id=workspace_id, intent_id=intent_id)

        monkeypatch.setattr(auto_approval, "facts", faulty)
        counts = _serve(world)
        assert len(seen) == 2, "both stories were decided in the one sweep"
        first, second = (next(s for s in stories if s["intent"] == i) for i in seen)
        _waits(world, first)
        _approved_by(world, w, second, "auto")
        assert counts["advanced"] >= 2 and counts["approved"] >= 1, counts


# --- the ledger is the authority ----------------------------------------------


class TestTheLedgerIsTheAuthority:
    def test_the_serve_paths_actor_is_the_one_the_trigger_requires(self, world):
        """A smoke check (`test_intent_ledger_gate.py` covers
        `tg_intent_person_or_rule` in depth): `transition(..., approval_rule=)`
        is refused under a person's actor, and so is a rule-less approval under
        the system's; the serve path's own shape, the system stamping a rule, is
        admitted."""
        w = _positioned(world, "authority")
        story = _story(world, w, state="awaiting_approval")

        async def approve(login, actor, rule, user=None):
            async with ingress_engine(as_user(world["owner"], login)) as engine:
                uow = unit_of_work.unit_of_work(
                    engine, w["ws"], actor_kind=actor, actor_user_id=user
                )
                async with uow.begin() as session:
                    await intent_ledger.transition(
                        session, story["intent"], "approved", approval_rule=rule
                    )

        with pytest.raises(intent_ledger.IntentTransitionRefused, match="system's"):
            asyncio.run(approve("svc_ingress", "user", "auto", w["user"]))
        with pytest.raises(
            intent_ledger.IntentTransitionRefused, match="only a person approves"
        ):
            asyncio.run(approve("svc_worker", "system", None))
        assert _row(world, story) == ("awaiting_approval", None)
        asyncio.run(approve("svc_worker", "system", "auto"))
        assert _row(world, story) == ("approved", "auto")


# --- the card's life ----------------------------------------------------------


def _send(world, w, *, during=None):
    """One tick of the binding's sender, as the worker's `deliver_outbox`
    runs it (`unit_of_work.poller_session_factory`, the worker's login, its
    pacing numbers), over a transport that records what it was handed and
    answers a receipt. *during* runs inside the send, after the claim has
    committed and before the settle."""
    cfg, sent = WorkerConfig(), []
    ref = str(10**9 + uuid.uuid4().int % 10**9)

    async def transport(row):
        sent.append(row)
        if during is not None:
            during()
        return SendReceipt(ref, sent_as="media")

    async def go():
        async with ingress_engine(as_user(world["owner"], "svc_worker")) as engine:
            poller = outbox.OutboxPoller(
                unit_of_work.poller_session_factory(engine, w["ws"]),
                binding_id=w["binding"],
                transport=transport,
                clock=utcnow,
                interval_seconds=0.05,
                chat_limit=cfg.chat_limit,
                chat_window_seconds=cfg.chat_window_seconds,
                global_limit=cfg.global_limit,
                global_window_seconds=cfg.global_window_seconds,
            )
            return await poller.tick()

    result = asyncio.run(go())
    return sent, ref, result


def _sweep_settled(world):
    """`prompts.sweep_settled_cards` as the reaper runs it: the worker's
    login, no tenant, the system actor."""
    return _worker(world, lambda s: prompts.sweep_settled_cards(s, limit=500))


def _line(world, w, story, word):
    """The outcome line naming YOLO, stamped as the card stamps it: the
    newest audit row that moved the story, in the workspace's zone."""
    ((at, tz),) = _exec(
        world,
        "SELECT e.created_at, w.tz FROM audit_events e"
        "  JOIN workspaces w ON w.id = e.workspace_id"
        " WHERE e.entity_id = %s AND e.from_state IS DISTINCT FROM e.to_state"
        " ORDER BY e.id DESC LIMIT 1",
        (story["intent"],),
        fetch=True,
    )
    return f"{word} by {vocabulary.RULE_LABELS['auto']} · {prompts.stamp(at, tz)}"


def _line_at_render(card):
    """The outcome line a settled card was sent with: its `outcome_text`, which
    the transport puts under the card's own caption."""
    return card["outcome_text"]


def _lost_answer():
    """A send whose answer never came back: the row is left `ambiguous`."""
    raise RuntimeError("the answer was lost")


def _age_ambiguous(world, story):
    """Past the resend backoff (`outbox.AMBIGUOUS_RESOLVE_AFTER_SECONDS`), the
    touch trigger held off so the backdated `updated_at` stands."""
    with _owner_cursor(world) as cur:
        cur.execute(
            "ALTER TABLE channel_outbox DISABLE TRIGGER tg_touch_channel_outbox"
        )
        cur.execute(
            "UPDATE channel_outbox SET updated_at = now() - interval '2 minutes'"
            " WHERE intent_id = %s AND kind = 'approval_prompt'",
            (story["intent"],),
        )
        cur.execute("ALTER TABLE channel_outbox ENABLE TRIGGER tg_touch_channel_outbox")


def _yolo_approved(world, name):
    """A YOLO workspace and one story the serve approved, its card pending."""
    w = _positioned(world, name)
    story = _story(world, w)
    _serve(world)
    _approved_by(world, w, story, "auto")
    return w, story


def _send_card_and_arm(world, w):
    """Two ticks: the card, which leaves with no keyboard, then the edit that
    gives it its story's."""
    _send(world, w)
    _send(world, w)


class TestTheCardOfAStoryARuleApproved:
    def test_a_story_that_posts_before_its_card_is_claimed_sends_one_settled_card(
        self, world
    ):
        w, story = _yolo_approved(world, "card-posted")
        _post(world, story)
        sent, ref, result = _send(world, w)
        assert result is not None and result["state"] == "sent", result
        (row,) = sent
        card = row["payload"]
        line = _line(world, w, story, "✅ Posted")
        assert card["outcome_text"] == line, card
        assert line not in card["caption"] and line not in card["text"], (
            "the card's own text stays its header; the line rides outcome_text"
        )
        assert "reply_markup" not in card, "a card leaves bare; an edit arms it"
        ((state, stored_ref, _),) = _cards(world, story)
        assert (state, stored_ref) == ("superseded", ref)
        ((_, _, arm),) = _cards(world, story, "prompt_supersede")
        assert arm["supersedes_ref"] == ref
        assert "outcome_text" not in arm, "its line is already on the card"
        armed, _, _ = _send(world, w)
        (edit,) = armed
        assert edit["payload"]["reply_markup"] == prompts.card_keyboard(
            story["intent"], state="posted", api_publishing_enabled=True
        ), "a settled card offers Configure alone"
        again, _, nothing = _send(world, w)
        assert (again, nothing) == ([], None), "the card goes out once"

    def test_the_settled_sweep_spares_its_pending_card_for_the_grace(self, world):
        """The control is a story a person approved, in a workspace that asks:
        its pending card is retired on the same beat, so the grace is the
        rule's, not every card's."""
        w, story = _yolo_approved(world, "card-grace")
        _post(world, story)
        asks = _positioned(world, "card-grace-person", mode="manual")
        person = _story(world, asks)
        _serve(world)
        _waits(world, person)
        with _owner_cursor(world) as cur:
            cur.execute("SET LOCAL app.actor_kind = 'user'")
            cur.execute("SET LOCAL app.actor_user_id = %s", (asks["user"],))
            cur.execute(
                "UPDATE post_intents SET state = 'approved' WHERE id = %s",
                (person["intent"],),
            )
        _post(world, person)
        assert _row(world, person) == ("posted", None), "the control's premise"

        _sweep_settled(world)
        assert [s for s, _, _ in _cards(world, story)] == ["pending"]
        assert [s for s, _, _ in _cards(world, person)] == ["superseded"]

        _exec(
            world,
            "UPDATE channel_outbox SET created_at = now() - %s"
            " WHERE intent_id = %s AND kind = 'approval_prompt'",
            (prompts.SETTLED_CARD_GRACE + timedelta(minutes=1), story["intent"]),
        )
        _sweep_settled(world)
        ((state, ref, payload),) = _cards(world, story)
        assert (state, ref) == ("superseded", None), "retired unsent, like the person's"
        assert payload["outcome_text"] == _line(world, w, story, "✅ Posted")
        assert _cards(world, story, "prompt_supersede") == [], "no ref, no edit"

    def test_a_settled_card_whose_answer_was_lost_is_spared_and_resent_settled(
        self, world
    ):
        """A lost answer leaves the card `ambiguous`, owed its resend after the
        backoff. A story that posts meanwhile does not cost it that resend: the
        settled sweep spares it within the grace, as it spares a pending card,
        and the resend goes out settled."""
        w, story = _yolo_approved(world, "card-lost")
        _, _, result = _send(world, w, during=_lost_answer)
        assert result["state"] == "ambiguous", result
        _post(world, story)
        _sweep_settled(world)
        assert [s for s, _, _ in _cards(world, story)] == ["ambiguous"], "spared"
        _age_ambiguous(world, story)
        sent, _, result = _send(world, w)
        (row,) = sent
        assert row["payload"]["outcome_text"] == _line(world, w, story, "✅ Posted")
        assert result["state"] == "sent"

    def test_a_card_sent_settled_whose_story_moves_on_gets_one_edit_that_replaces_its_line(
        self, world
    ):
        """Approved when the sender rendered it, posted by the time the send
        came back: one after-send edit, whose header is the card without its
        "Approved by YOLO" line, so the posted line replaces it."""
        w, story = _yolo_approved(world, "card-moved")
        sent, ref, _ = _send(world, w, during=lambda: _post(world, story))
        (row,) = sent
        card = row["payload"]
        approved = _line_at_render(card)
        assert approved.startswith("✅ Approved by YOLO · "), card
        ((state, stored_ref, _),) = _cards(world, story)
        assert (state, stored_ref) == ("superseded", ref)
        ((edit_state, _, edit),) = _cards(world, story, "prompt_supersede")
        assert edit_state == "pending"
        assert edit["supersedes_ref"] == ref and edit["sent_as"] == "media"
        assert edit["header"] == card["caption"]
        assert "Approved" not in edit["header"], edit["header"]
        assert edit["outcome_text"] == _line(world, w, story, "✅ Posted")

    def test_a_restate_by_ref_builds_its_edit_from_the_clean_header(self, world):
        w, story = _yolo_approved(world, "card-restate")
        _post(world, story)
        sent, ref, _ = _send(world, w)
        (row,) = sent
        card = row["payload"]
        queued = _worker(
            world,
            lambda s: outbox.restate_everywhere(
                s,
                workspace_id=w["ws"],
                intent_id=story["intent"],
                outcome_text="✅ Posted by YOLO · later",
            ),
            tenant=w["ws"],
        )
        assert queued == 1
        (_, _, arm), (_, _, edit) = _cards(world, story, "prompt_supersede")
        assert "outcome_text" not in arm, "the edit that arms the card"
        assert edit["supersedes_ref"] == ref
        assert edit["header"] == card["caption"], edit["header"]
        assert "Posted by YOLO" not in edit["header"], "the line is not stacked"
        assert edit["outcome_text"] == "✅ Posted by YOLO · later"


class TestTheSenderRendersEveryKeyboard:
    """Against the ledger: the sender claims a card's edit and sends
    it with the keyboard of its story as it is then, whatever its caller stored
    — a parked story's edit carries the three resolutions and Configure, a
    posted story's Configure alone."""

    def _restated(self, world, w, story, line):
        queued = _worker(
            world,
            lambda s: outbox.restate_everywhere(
                s, workspace_id=w["ws"], intent_id=story["intent"], outcome_text=line
            ),
            tenant=w["ws"],
        )
        assert queued == 1
        (*_, (_, _, edit)) = _cards(world, story, "prompt_supersede")
        assert "reply_markup" not in edit, "the caller stores no keyboard"

    def test_a_parked_storys_edit_goes_out_with_its_review_buttons(self, world):
        w, story = _yolo_approved(world, "kb-parked")
        _send_card_and_arm(world, w)
        _park(world, story)
        self._restated(world, w, story, "👀 Needs review")
        sent, _, _ = _send(world, w)
        (row,) = sent
        assert row["kind"] == "prompt_supersede"
        assert row["payload"]["reply_markup"] == prompts.card_keyboard(
            story["intent"], state="review_required", api_publishing_enabled=True
        )

    def test_a_posted_storys_edit_goes_out_with_configure_alone(self, world):
        w, story = _yolo_approved(world, "kb-posted")
        _send_card_and_arm(world, w)
        _post(world, story)
        self._restated(world, w, story, "✅ Posted by YOLO · later")
        sent, _, _ = _send(world, w)
        (row,) = sent
        assert row["payload"]["reply_markup"] == {
            "inline_keyboard": [
                [{"text": "⚙️ Configure", "callback_data": f"v1:cfg:{story['intent']}"}]
            ]
        }

    def test_a_cancelled_waiting_storys_edit_goes_out_with_configure_alone(self, world):
        """A member cancels a story still waiting for a
        tap. It stays `awaiting_approval` until the reaper ends it, and the
        cancel's edit is claimed first — so the edit must leave Configure
        alone, never the decision buttons: once the story is `cancelled`, no
        later edit reaches the card."""
        w = _positioned(world, "kb-cancel", mode="manual")
        story = _story(world, w)
        _serve(world)
        sent, _, _ = _send(world, w)
        assert [row["kind"] for row in sent] == ["approval_prompt"]
        armed, _, _ = _send(world, w)
        assert [row["kind"] for row in armed] == ["prompt_supersede"]
        cancel = Command(
            kind="cancel",
            workspace_id=w["ws"],
            actor_user_id=w["user"],
            channel="cli",
            args={"intent_id": story["intent"]},
        )
        asyncio.run(
            in_tenant(
                as_user(world["owner"], "svc_ingress"),
                w["ws"],
                w["user"],
                lambda session: commands.execute(session, cancel),
                channel="cli",
            )
        )
        sent, _, _ = _send(world, w)
        (edit,) = sent
        assert edit["kind"] == "prompt_supersede"
        assert "Cancelled" in edit["payload"]["outcome_text"]
        assert edit["payload"]["reply_markup"] == {
            "inline_keyboard": [
                [{"text": "⚙️ Configure", "callback_data": f"v1:cfg:{story['intent']}"}]
            ]
        }

    def test_a_removed_items_edit_goes_out_with_no_keyboard(self, world):
        w, story = _yolo_approved(world, "kb-removed")
        _send_card_and_arm(world, w)
        with _owner_cursor(world) as cur:
            cur.execute("SET LOCAL app.actor_kind = 'migration'")
            cur.execute(
                "UPDATE media_items SET state = 'removed' WHERE id ="
                " (SELECT media_item_id FROM post_intents WHERE id = %s)",
                (story["intent"],),
            )
        self._restated(world, w, story, "✅ Approved by YOLO · later")
        sent, _, _ = _send(world, w)
        (row,) = sent
        assert row["kind"] == "prompt_supersede"
        assert "reply_markup" not in row["payload"], "a removed item offers no lever"
