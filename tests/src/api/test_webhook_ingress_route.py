"""W4 ingress route — the refusal ladder, and the seam's negative (#942).

The load-bearing test here is not that an unwired route returns 503. It is that
an unwired route **never reaches `admit`**. Admission is irreversible, so a
route that admitted and then refused would destroy the command while reporting
a refusal, and the status code alone cannot tell those two apart.
"""

from __future__ import annotations

import pytest

from src.api.app import app
from src.api.routes import webhooks
from src.api.routes.webhooks import SECRET_HEADER
from src.config.settings import settings
from src.services.target.webhook_ingress import AdmissionConflict, DeliveryReplayed

SECRET = "test-secret-value"
URL = "/webhooks/telegram"


class FakeConn:
    """Minimal async connection: records commits and when it was released."""

    def __init__(self) -> None:
        self.commits = 0
        self.released = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.released = True
        return False

    async def commit(self):
        self.commits += 1


@pytest.fixture
def armed(monkeypatch):
    """The secret configured — i.e. the deployment deliberately armed."""
    monkeypatch.setattr(
        settings, "TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN", SECRET, raising=False
    )


@pytest.fixture
def client():
    """Bound to the module singleton, because these tests wire the seam on
    `app.state` — the factory-built client in conftest is a different app."""
    from fastapi.testclient import TestClient

    return TestClient(app)


@pytest.fixture(autouse=True)
def unwired():
    """Every test starts at the real default: no dispatcher wired.

    `app` is a module singleton, so the seam is process state whether it lives
    on `app.state` or in a module global. The reset is a discipline either way.
    """
    app.state.ingress = None
    yield
    app.state.ingress = None


@pytest.fixture
def spy(monkeypatch):
    """Wire a runtime and record every call the route makes through the seam."""
    seen = {"admit": [], "dispatch": [], "conn": FakeConn()}

    async def fake_admit(conn, **kw):
        seen["admit"].append((conn, kw))
        return {"admitted": True}

    async def fake_dispatch(conn, payload):
        seen["dispatch"].append((conn, payload))

    monkeypatch.setattr(webhooks, "admit", fake_admit)
    app.state.ingress = webhooks.IngressRuntime(
        connect=lambda: seen["conn"], dispatch=fake_dispatch
    )
    return seen


def _post(client, body=None, *, secret=SECRET, raw=None):
    headers = {SECRET_HEADER: secret} if secret is not None else {}
    if raw is not None:
        return client.post(
            URL, content=raw, headers={**headers, "Content-Type": "application/json"}
        )
    return client.post(URL, json=body, headers=headers)


# --- the dormancy property -------------------------------------------------


def test_an_unset_secret_refuses_every_delivery(client, monkeypatch):
    """Absence of config refuses, it does not accept. The direction is the point."""
    monkeypatch.setattr(
        settings, "TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN", None, raising=False
    )
    assert _post(client, {"update_id": 1}).status_code == 403


def test_a_wrong_secret_is_refused(client, armed):
    assert _post(client, {"update_id": 1}, secret="wrong").status_code == 403


def test_a_missing_header_is_refused(client, armed):
    assert _post(client, {"update_id": 1}, secret=None).status_code == 403


def test_the_secret_is_checked_before_the_body_is_parsed(client, armed):
    """A bad secret with an unparseable body must still be 403, not 400.

    Refusal order is a security property: the cheapest, least informative
    refusal has to come first, or a prober learns about the body handling of a
    route it cannot authenticate to.
    """
    assert _post(client, secret="wrong", raw=b"{not json").status_code == 403


# --- the refusal ladder ----------------------------------------------------


def test_a_non_json_body_is_rejected(client, armed):
    assert _post(client, raw=b"{not json").status_code == 400


def test_a_json_scalar_body_is_rejected(client, armed):
    assert _post(client, raw=b'"a string"').status_code == 400


def test_a_body_without_update_id_is_rejected(client, armed):
    assert _post(client, {"message": {"text": "hi"}}).status_code == 400


def test_a_non_integer_update_id_is_rejected(client, armed):
    assert _post(client, {"update_id": "17"}).status_code == 400


# --- THE SEAM: the negative that matters ----------------------------------


def test_an_unwired_route_refuses_with_503(client, armed):
    assert _post(client, {"update_id": 42}).status_code == 503


def test_an_unwired_route_NEVER_REACHES_ADMIT(client, armed, monkeypatch):
    """The load-bearing negative.

    Admission is irreversible: once the `command_dedup` key is committed, a
    redelivery of the same update is a replay and is correctly never executed.
    So an unwired route that admitted would destroy the command while returning
    a refusal, and the 503 above cannot distinguish that from this.
    """
    called = []

    async def exploding_admit(conn, **kw):
        called.append(kw)
        raise AssertionError("admit must not be reached with no dispatcher wired")

    monkeypatch.setattr(webhooks, "admit", exploding_admit)
    assert _post(client, {"update_id": 42}).status_code == 503
    assert called == [], "a delivery was admitted that could not be dispatched"


# --- the wired path -------------------------------------------------------


def test_a_wired_route_admits_and_dispatches(client, armed, spy):
    r = _post(client, {"update_id": 7, "message": {"text": "hi"}})
    assert r.status_code == 200
    assert r.json()["status"] == "admitted"
    assert len(spy["admit"]) == 1
    assert len(spy["dispatch"]) == 1


def test_admission_keys_on_the_update_id_and_carries_no_tenant(client, armed, spy):
    """The reason this route can precede #854: admission takes no tenant."""
    _post(client, {"update_id": 99})
    _conn, kw = spy["admit"][0]
    assert kw["channel"] == "telegram"
    assert kw["external_ref"] == "99"
    assert "workspace_id" not in kw and "tenant_id" not in kw


def test_admission_and_dispatch_share_one_connection(client, armed, spy):
    """So #854 can make admission and effect a single transaction."""
    _post(client, {"update_id": 5})
    assert spy["admit"][0][0] is spy["dispatch"][0][0]


def test_the_commit_follows_dispatch(client, armed, spy):
    """Committing before dispatch would leave a key with no effect."""
    _post(client, {"update_id": 5})
    assert spy["conn"].commits == 1


# --- the two named admission outcomes ------------------------------------
#
# Kept as two tests rather than one parametrized table: `DeliveryReplayed` and
# `AdmissionConflict` are separate classes in L.8's design precisely because
# they are different events, and a shared test name would merge the one
# distinction the module exists to draw.


def test_a_replay_is_acknowledged_and_neither_dispatched_nor_committed(
    client, armed, spy, monkeypatch
):
    """200 replayed callbacks yield one command — the L.8 clause."""

    async def replaying_admit(conn, **kw):
        raise DeliveryReplayed("seen")

    monkeypatch.setattr(webhooks, "admit", replaying_admit)
    r = _post(client, {"update_id": 7})
    assert r.status_code == 200
    assert r.json() == {"status": "replayed"}
    assert spy["dispatch"] == [], "a replay must not re-execute"
    assert spy["conn"].commits == 0, "nothing was inserted, so nothing to commit"


def test_a_conflict_is_rejected_never_swallowed_as_a_replay(
    client, armed, spy, monkeypatch
):
    """Same key, different content: a caller bug or an attack, never a dedup."""

    async def conflicting_admit(conn, **kw):
        raise AdmissionConflict("reused key, new body")

    monkeypatch.setattr(webhooks, "admit", conflicting_admit)
    r = _post(client, {"update_id": 7})
    assert r.status_code == 409
    assert r.json() == {"detail": "admission conflict"}
    assert spy["dispatch"] == []
    assert spy["conn"].commits == 0


# --- the acknowledgement: a handled /start is answered, after commit ----------
#
# Nothing sent the reply text the handlers already carried (#1224 review left
# the bot silent). The owner's verdict on 2026-09-05: a successful link should
# say so in the chat. Refusals stay silent — the router's existence-oracle
# rule — and the send is best-effort AFTER the delivery is committed, so a
# provider hiccup can neither roll back the link nor make Telegram redeliver.

START_UPDATE = {
    "update_id": 7,
    "message": {"text": "/start link-abc", "chat": {"id": 555, "type": "private"}},
}


@pytest.fixture
def replying(monkeypatch):
    """A wired runtime whose dispatch answers `outcome`, plus a reply recorder
    that notes how many commits had happened by the time it was called."""
    from src.services.target.start_router import StartResult

    seen = {"admit": [], "replies": [], "conn": FakeConn(), "result": None}

    async def fake_admit(conn, **kw):
        seen["admit"].append(kw)
        return {"admitted": True}

    async def fake_dispatch(conn, payload):
        return seen["result"]

    async def fake_reply(chat_id, text):
        seen["replies"].append((chat_id, text, seen["conn"].commits))

    monkeypatch.setattr(webhooks, "admit", fake_admit)
    app.state.ingress = webhooks.IngressRuntime(
        connect=lambda: seen["conn"], dispatch=fake_dispatch, reply=fake_reply
    )
    seen["StartResult"] = StartResult
    return seen


def test_a_handled_start_is_acknowledged_in_the_chat_after_commit(
    client, armed, replying
):
    replying["result"] = replying["StartResult"](
        outcome="linked",
        handled=True,
        reply="Your Telegram account is now linked to Storydump.",
    )
    resp = _post(client, START_UPDATE)
    assert resp.status_code == 200 and resp.json()["status"] == "admitted"
    assert replying["replies"] == [
        ("555", "Your Telegram account is now linked to Storydump.", 1)
    ], "sent to the tapping chat, once, and only after the delivery committed"


def test_a_refused_start_stays_silent(client, armed, replying):
    replying["result"] = replying["StartResult"](outcome="state_refused", handled=False)
    assert _post(client, START_UPDATE).status_code == 200
    assert replying["replies"] == []


def test_a_failed_acknowledgement_does_not_fail_the_delivery(
    client, armed, replying, monkeypatch
):
    replying["result"] = replying["StartResult"](
        outcome="linked", handled=True, reply="Linked."
    )

    async def exploding_reply(chat_id, text):
        raise RuntimeError("telegram is down")

    app.state.ingress = webhooks.IngressRuntime(
        connect=lambda: replying["conn"],
        dispatch=app.state.ingress.dispatch,
        reply=exploding_reply,
    )
    resp = _post(client, START_UPDATE)
    assert resp.status_code == 200 and resp.json()["status"] == "admitted"
    assert replying["conn"].commits == 1, "the link stays committed"


def test_a_runtime_without_a_sender_is_silent_by_construction(client, armed, replying):
    replying["result"] = replying["StartResult"](
        outcome="linked", handled=True, reply="Linked."
    )
    app.state.ingress = webhooks.IngressRuntime(
        connect=lambda: replying["conn"], dispatch=app.state.ingress.dispatch
    )
    assert _post(client, START_UPDATE).status_code == 200
    assert replying["replies"] == []


class TestTheAcknowledgementIsWiredFromTheBotToken:
    """`create_app` arms the reply sender from `TARGET_TELEGRAM_BOT_TOKEN` —
    the worker's variable and transport — and leaves the door silent without it."""

    def _app(self, env):
        from types import SimpleNamespace

        from src.api.app import create_app

        return create_app(engine=SimpleNamespace(connect=lambda: None), env=env)

    def test_with_the_token_the_runtime_can_reply(self):
        built = self._app({"TARGET_TELEGRAM_BOT_TOKEN": "123:abc"})
        assert built.state.ingress is not None and built.state.ingress.reply is not None

    def test_without_the_token_the_door_is_silent(self):
        built = self._app({})
        assert built.state.ingress is not None and built.state.ingress.reply is None


# --- the tap is answered through the REAL transport --------------------------


def test_a_tap_is_answered_through_the_real_transport(client, armed, monkeypatch):
    """The route's answer runs the transport's own `answer_callback` — wired
    exactly as `app.py` wires it, over a scripted Bot API — so a signature
    drift between the two (positional vs keyword `show_alert`) fails HERE,
    not silently in production behind the best-effort `except` (structural
    review of #1271). It is the route's ONLY Telegram call (2026-09-12)."""
    import json as _json

    import httpx

    from src.channels.telegram_transport import TelegramTransport
    from src.services.target.telegram_dispatch import TapResult

    calls = []

    def bot(request):
        calls.append(
            (str(request.url).rsplit("/", 1)[-1], _json.loads(request.content))
        )
        return httpx.Response(200, json={"ok": True, "result": True})

    transport = TelegramTransport(
        "8675309:AAtest", client=httpx.AsyncClient(transport=httpx.MockTransport(bot))
    )

    async def fake_admit(conn, **kw):
        return {"admitted": True}

    async def fake_dispatch(conn, payload):
        return TapResult(
            outcome="executed",
            handled=True,
            callback_query_id="q1",
            chat_ref="-100",
            message_ref="555",
            answer_text="⏭️ Skipped for 7 days",
            show_alert=True,
        )

    monkeypatch.setattr(webhooks, "admit", fake_admit)
    conn = FakeConn()
    app.state.ingress = webhooks.IngressRuntime(
        connect=lambda: conn,
        dispatch=fake_dispatch,
        reply=transport.send_text,
        answer_callback=transport.answer_callback,
    )
    before = app.state.tap_metrics.snapshot()["taps_total"]
    response = _post(
        client,
        {
            "update_id": 77,
            "callback_query": {
                "id": "q1",
                "data": "v1:skip:x",
                "message": {"message_id": 555, "chat": {"id": -100}},
            },
        },
    )
    assert response.status_code == 200 and response.json()["status"] == "admitted"
    assert conn.commits == 1, "the answer follows the commit"
    # One answer and nothing else from the route: the card's keyboard goes
    # with the paced supersede edit (one Telegram message per tap per
    # binding, 2026-09-12), never an unpaced strip from here.
    assert [c[0] for c in calls] == ["answerCallbackQuery"]
    assert calls[0][1] == {
        "callback_query_id": "q1",
        "text": "⏭️ Skipped for 7 days",
        "show_alert": True,
    }
    after = app.state.tap_metrics.snapshot()
    assert after["taps_total"] == before + 1 and after["answer_failed"] == 0


def test_a_replayed_tap_is_toasted_after_the_connection_is_released(
    client, armed, monkeypatch
):
    """Telegram redelivered a tap the route already handled: the redelivered
    query still has a spinner, so it gets a toast — sent AFTER the pool
    connection is released, never across a provider call (the discipline the
    sender's split exists for)."""
    import json as _json

    import httpx

    from src.channels.telegram_transport import TelegramTransport

    conn = FakeConn()
    seen = {}

    def bot(request):
        seen["released_at_call"] = conn.released
        seen["json"] = _json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": True})

    transport = TelegramTransport(
        "8675309:AAtest", client=httpx.AsyncClient(transport=httpx.MockTransport(bot))
    )

    async def replayed_admit(conn_, **kw):
        raise DeliveryReplayed("update 77 was admitted before")

    async def never_dispatch(
        conn_, payload
    ):  # pragma: no cover — a replay is not dispatched
        raise AssertionError("a replayed delivery must not be dispatched")

    monkeypatch.setattr(webhooks, "admit", replayed_admit)
    app.state.ingress = webhooks.IngressRuntime(
        connect=lambda: conn,
        dispatch=never_dispatch,
        answer_callback=transport.answer_callback,
    )
    response = _post(
        client,
        {
            "update_id": 77,
            "callback_query": {
                "id": "q-again",
                "data": "v1:skip:x",
                "message": {"message_id": 555, "chat": {"id": -100}},
            },
        },
    )
    assert response.status_code == 200 and response.json() == {"status": "replayed"}
    assert seen["released_at_call"] is True, "the toast must not hold the pool slot"
    assert seen["json"]["callback_query_id"] == "q-again"
    assert conn.commits == 0


# --- the boundary (phase 2 of the 2026-09-09 tap plan, step 2) ---------------


def _tap_update(update_id=41, *, with_message=True):
    cq = {
        "id": "q-41",
        "from": {"id": 7, "first_name": "Ada"},
        "data": "v1:skip:5f1b2c3d-0000-4000-8000-000000000001",
    }
    if with_message:
        cq["message"] = {"message_id": 555, "chat": {"id": -100, "type": "supergroup"}}
    return {"update_id": update_id, "callback_query": cq}


class _SaturatedConnect:
    """`engine.connect()` whose checkout times out: the pool is full."""

    async def __aenter__(self):
        from sqlalchemy.exc import TimeoutError as PoolTimeout

        raise PoolTimeout("QueuePool limit of size 10 overflow 0 reached")

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def saturated(monkeypatch):
    """A wired runtime whose pool is saturated; records what the route answers."""
    seen = {"admit": [], "answers": [], "dispatch": []}

    async def fake_admit(conn, **kw):
        seen["admit"].append(kw)

    async def fake_dispatch(conn, payload):
        seen["dispatch"].append(payload)

    async def answer(qid, text, alert):
        seen["answers"].append((qid, text, alert))
        return True

    monkeypatch.setattr(webhooks, "admit", fake_admit)
    app.state.tap_metrics = webhooks.TapMetrics()
    app.state.ingress = webhooks.IngressRuntime(
        connect=lambda: _SaturatedConnect(),
        dispatch=fake_dispatch,
        answer_callback=answer,
    )
    return seen


class TestTheSaturationBoundary:
    def test_a_stale_token_is_still_answered_busy(self, client, armed, saturated):
        """A malformed or older-card token is a real spinner; unsaturated it
        would hear `older_card`, saturated it hears busy — never nothing."""
        update = _tap_update()
        update["callback_query"]["data"] = "v0:garbage"
        r = _post(client, update)
        assert r.status_code == 200 and r.json()["outcome"] == "busy"
        assert saturated["answers"] == [("q-41", webhooks.BUSY_TEXT, False)]

    def test_an_answer_that_did_not_land_is_counted(self, client, armed, saturated):
        async def not_landed(qid, text, alert):
            return False

        app.state.ingress = webhooks.IngressRuntime(
            connect=lambda: _SaturatedConnect(),
            dispatch=app.state.ingress.dispatch,
            answer_callback=not_landed,
        )
        r = _post(client, _tap_update())
        assert r.status_code == 200 and r.json()["outcome"] == "busy"
        assert app.state.tap_metrics.snapshot()["answer_failed"] == 1

    def test_a_tap_is_answered_busy_and_not_admitted(self, client, armed, saturated):
        r = _post(client, _tap_update())
        assert r.status_code == 200
        assert r.json() == {"status": "refused", "outcome": "busy"}
        assert saturated["admit"] == [] and saturated["dispatch"] == []
        assert saturated["answers"] == [("q-41", webhooks.BUSY_TEXT, False)]
        assert app.state.tap_metrics.snapshot()["taps"] == {"busy": 1}

    def test_a_message_is_refused_503_before_admission(self, client, armed, saturated):
        r = _post(
            client, {"update_id": 42, "message": {"text": "/start", "chat": {"id": 1}}}
        )
        assert r.status_code == 503
        assert saturated["admit"] == [] and saturated["answers"] == []

    def test_an_unanswerable_busy_tap_still_consumes_the_delivery(
        self, client, armed, saturated, monkeypatch
    ):
        async def broken(qid, text, alert):
            raise RuntimeError("telegram down")

        app.state.ingress = webhooks.IngressRuntime(
            connect=lambda: _SaturatedConnect(),
            dispatch=app.state.ingress.dispatch,
            answer_callback=broken,
        )
        r = _post(client, _tap_update())
        assert r.status_code == 200 and r.json()["outcome"] == "busy"
        assert app.state.tap_metrics.snapshot()["answer_failed"] == 1


class TestADatabaseFaultIsA503WithNothingConsumed:
    def test_a_fault_at_admit_is_503_and_never_committed(
        self, client, armed, spy, monkeypatch
    ):
        from sqlalchemy.exc import OperationalError

        async def failing_admit(conn, **kw):
            raise OperationalError("INSERT", {}, Exception("connection lost"))

        monkeypatch.setattr(webhooks, "admit", failing_admit)
        r = _post(client, _tap_update())
        assert r.status_code == 503
        assert spy["conn"].commits == 0 and spy["conn"].released is True
        assert spy["dispatch"] == []

    def test_a_fault_at_commit_is_503(self, client, armed, spy, monkeypatch):
        from sqlalchemy.exc import OperationalError

        async def failing_commit():
            raise OperationalError("COMMIT", {}, Exception("connection lost"))

        spy["conn"].commit = failing_commit
        r = _post(client, _tap_update())
        assert r.status_code == 503 and spy["conn"].released is True


def test_an_admitted_tap_names_its_outcome_in_the_body(client, armed, monkeypatch):
    seen = FakeConn()

    async def fake_admit(conn, **kw):
        return {"admitted": True}

    async def dispatch(conn, payload):
        from src.services.target.telegram_dispatch import TapResult

        return TapResult(
            outcome="answered",
            handled=True,
            callback_query_id="q-41",
            chat_ref="-100",
            message_ref="555",
            answer_text="Already skipped.",
            show_alert=False,
        )

    monkeypatch.setattr(webhooks, "admit", fake_admit)
    app.state.tap_metrics = webhooks.TapMetrics()
    app.state.ingress = webhooks.IngressRuntime(connect=lambda: seen, dispatch=dispatch)
    r = _post(client, _tap_update())
    assert r.status_code == 200
    assert r.json() == {"status": "admitted", "outcome": "answered"}


# --- the answer rides behind the 200 (#1284) -------------------------------


@pytest.mark.asyncio
async def test_the_answer_and_strip_run_after_the_response_has_gone_out(
    armed, monkeypatch
):
    """#1284: the tap's answer is a Telegram round trip of its own; run inside
    the request it held this delivery until Telegram replied. It is a
    background task — the 200's body is sent BEFORE the transport is spoken
    to. Proven at the ASGI level: the order of `http.response.body` against
    the answer call. (The card's keyboard goes with the paced supersede edit,
    not from the route — 2026-09-12.)"""
    import json as _json

    from src.services.target.telegram_dispatch import TapResult

    order: list = []

    async def fake_admit(conn, **kw):
        return {"admitted": True}

    async def fake_dispatch(conn, payload):
        return TapResult(
            outcome="executed",
            handled=True,
            callback_query_id="q1",
            chat_ref="-100",
            message_ref="555",
            answer_text="⏭️ Skipped for 7 days",
            show_alert=True,
        )

    async def answer(callback_query_id, text, show_alert):
        order.append("answered")
        return True

    monkeypatch.setattr(webhooks, "admit", fake_admit)
    app.state.ingress = webhooks.IngressRuntime(
        connect=lambda: FakeConn(),
        dispatch=fake_dispatch,
        answer_callback=answer,
    )
    body = _json.dumps(
        {
            "update_id": 91,
            "callback_query": {
                "id": "q1",
                "data": "v1:skip:x",
                "message": {"message_id": 555, "chat": {"id": -100}},
            },
        }
    ).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": URL,
        "raw_path": URL.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (SECRET_HEADER.lower().encode(), SECRET.encode()),
        ],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }
    sent_body = {"given": False}

    async def receive():
        if not sent_body["given"]:
            sent_body["given"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    messages: list = []

    async def send(message):
        messages.append(message["type"])
        if message["type"] == "http.response.body" and not message.get("more_body"):
            order.append("body-sent")

    await app(scope, receive, send)

    assert "http.response.start" in messages
    assert order == ["body-sent", "answered"], order


@pytest.mark.asyncio
async def test_the_busy_answer_runs_after_the_refusal_has_gone_out(armed, monkeypatch):
    """The saturated path is where a request waiting on Telegram is dearest:
    its busy toast is a background task too (#1284, adversarial review of
    #1290)."""
    import json as _json

    from sqlalchemy.exc import TimeoutError as PoolTimeout

    order: list = []

    class _Saturated:
        async def __aenter__(self):
            raise PoolTimeout("pool saturated")

        async def __aexit__(self, *exc):
            return False

    async def answer(callback_query_id, text, show_alert):
        order.append(("answered", text))
        return True

    app.state.ingress = webhooks.IngressRuntime(
        connect=lambda: _Saturated(), dispatch=None, answer_callback=answer
    )
    body = _json.dumps(
        {
            "update_id": 92,
            "callback_query": {
                "id": "q2",
                "data": "v1:skip:x",
                "message": {"message_id": 556, "chat": {"id": -100}},
            },
        }
    ).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": URL,
        "raw_path": URL.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (SECRET_HEADER.lower().encode(), SECRET.encode()),
        ],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }
    given = {"body": False}

    async def receive():
        if not given["body"]:
            given["body"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body" and not message.get("more_body"):
            order.append(("body-sent", None))

    before = app.state.tap_metrics.snapshot()["taps_total"]
    await app(scope, receive, send)

    assert order == [("body-sent", None), ("answered", webhooks.BUSY_TEXT)], order
    assert app.state.tap_metrics.snapshot()["taps_total"] == before + 1
