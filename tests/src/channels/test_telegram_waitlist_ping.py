"""The admin's Telegram message for each waitlist signup (`waitlist_ping`).

Sent through the real transport over `httpx.MockTransport`, so what Telegram
would receive and what each of its answers does are the real ones; the
network and the waits are not.
"""

import asyncio
import json
import logging
import types
from datetime import datetime, timezone

import httpx
import pytest

from src.channels import telegram_waitlist_ping as waitlist_ping
from src.channels.telegram_transport import TelegramTransport
from src.services.target.egress import EgressPolicy

TOKEN = "8675309:AAtestSECRETtokenVALUExyz"
CHAT = "424242"
OP = "00000000-0000-4000-8000-000000000001"
OPS = frozenset({OP})


def _to(*chats):
    """A recipients read that answers *chats*."""

    async def read():
        return list(chats)

    return read


OK = {"ok": True, "result": {"message_id": 1}}


def _ping(*answers, chats=(CHAT,)):
    """A ping whose transport answers each request with the next of *answers*
    (a dict body, or an exception to raise), the last one from then on;
    returns it and the sent bodies."""
    sent = []
    queue = list(answers)

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(answer.get("error_code", 200), json=answer)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    bot = TelegramTransport(TOKEN, client=client)
    return waitlist_ping.WaitlistPing(bot.send_text, _to(*chats)), sent


@pytest.fixture(autouse=True)
def no_waits(monkeypatch):
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    # The module's own waits only; the semaphore stays the real one.
    fake = types.SimpleNamespace(sleep=sleep, Semaphore=asyncio.Semaphore)
    monkeypatch.setattr(waitlist_ping, "asyncio", fake)
    return waits


def _logged(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


def test_the_message_names_the_address_and_the_time_in_utc():
    at = datetime(2026, 10, 4, 21, 1, 7, 500000, tzinfo=timezone.utc)
    assert waitlist_ping.message("a@example.com", at) == (
        "New waitlist signup!\n\nEmail: a@example.com\nTime: 2026-10-04T21:01:07Z"
    )


async def test_one_send_to_the_chat_as_plain_text(no_waits):
    ping, sent = _ping(OK)
    await ping("*bold*@example.com")
    assert len(sent) == 1
    assert sent[0]["chat_id"] == CHAT
    assert "parse_mode" not in sent[0]
    assert "Email: *bold*@example.com" in sent[0]["text"]
    assert no_waits == []


def _paced(retry_after):
    return {
        "ok": False,
        "error_code": 429,
        "description": "Too Many Requests",
        "parameters": {"retry_after": retry_after},
    }


async def test_a_429_waits_what_telegram_names_then_sends_again(no_waits):
    ping, sent = _ping(_paced(7), OK)
    await ping("a@example.com")
    assert len(sent) == 2
    assert no_waits == [7.0]


async def test_a_long_retry_after_is_capped(no_waits):
    ping, _ = _ping(_paced(3600), OK)
    await ping("a@example.com")
    assert no_waits == [waitlist_ping.MAX_PACED_WAIT_SECONDS]


async def test_429s_past_the_last_send_are_logged(no_waits, caplog):
    ping, sent = _ping(_paced(1))
    with caplog.at_level(logging.ERROR, logger=waitlist_ping.__name__):
        await ping("a@example.com")
    assert len(sent) == waitlist_ping.ATTEMPTS
    assert "waitlist ping: not sent after 3 tries" in _logged(caplog)


async def test_a_lost_answer_is_left_to_the_egress_floor_and_logged(caplog):
    lost = httpx.ConnectError(
        f"no route to https://api.telegram.org/bot{TOKEN}/sendMessage"
    )
    ping, sent = _ping(lost)
    with caplog.at_level(logging.ERROR, logger=waitlist_ping.__name__):
        await ping("a@example.com")
    # The floor's own attempts, and no more on top of them.
    assert len(sent) == EgressPolicy().max_attempts
    assert "waitlist ping: not sent" in _logged(caplog)
    assert TOKEN not in _logged(caplog)


async def test_past_the_queue_a_ping_is_dropped_and_logged(caplog):
    release = asyncio.Event()
    sent = []

    async def send(chat_id, text):
        sent.append(text)
        await release.wait()
        return "1"

    ping = waitlist_ping.WaitlistPing(send, _to(CHAT))
    first = asyncio.ensure_future(ping("first@example.com"))
    await asyncio.sleep(0)
    queued = [
        asyncio.ensure_future(ping(f"q{i}@example.com"))
        for i in range(waitlist_ping.MAX_WAITING)
    ]
    await asyncio.sleep(0)
    with caplog.at_level(logging.ERROR, logger=waitlist_ping.__name__):
        await ping("dropped@example.com")
    assert "waitlist ping: dropped, 20 already waiting" in _logged(caplog)
    release.set()
    await asyncio.gather(first, *queued)
    assert len(sent) == 1 + waitlist_ping.MAX_WAITING
    assert not any("dropped@" in text for text in sent)


async def test_past_the_queue_an_alert_still_waits_its_turn_and_is_sent():
    release = asyncio.Event()
    sent = []

    async def send(chat_id, text):
        sent.append(text)
        await release.wait()
        return "1"

    ping = waitlist_ping.WaitlistPing(send, _to(CHAT))
    first = asyncio.ensure_future(ping("first@example.com"))
    await asyncio.sleep(0)
    queued = [
        asyncio.ensure_future(ping(f"q{i}@example.com"))
        for i in range(waitlist_ping.MAX_WAITING)
    ]
    await asyncio.sleep(0)
    alert = asyncio.ensure_future(ping.send("the ceiling is full", alert=True))
    release.set()
    await asyncio.gather(first, *queued, alert)
    assert sent[-1] == "the ceiling is full"
    assert len(sent) == 2 + waitlist_ping.MAX_WAITING


async def test_a_ping_cancelled_in_the_queue_frees_its_place():
    release = asyncio.Event()
    sent = []

    async def send(chat_id, text):
        sent.append(text)
        await release.wait()
        return "1"

    ping = waitlist_ping.WaitlistPing(send, _to(CHAT))
    first = asyncio.ensure_future(ping("first@example.com"))
    await asyncio.sleep(0)
    queued = [
        asyncio.ensure_future(ping(f"q{i}@example.com"))
        for i in range(waitlist_ping.MAX_WAITING)
    ]
    await asyncio.sleep(0)
    for task in queued:
        task.cancel()
    await asyncio.gather(*queued, return_exceptions=True)
    release.set()
    await first
    assert ping._waiting == 0
    await ping("later@example.com")
    assert any("later@example.com" in text for text in sent)
    assert ping._waiting == 0


async def test_pings_go_out_one_at_a_time():
    active, peak = 0, 0

    async def send(chat_id, text):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return "1"

    ping = waitlist_ping.WaitlistPing(send, _to(CHAT))
    await asyncio.gather(*(ping(f"p{i}@example.com") for i in range(5)))
    assert peak == 1


@pytest.mark.parametrize(
    "answer, reason",
    [
        (
            {
                "ok": False,
                "error_code": 400,
                "description": "Bad Request: chat not found",
            },
            "chat not found",
        ),
        (
            {"ok": False, "error_code": 401, "description": "Unauthorized"},
            "Unauthorized",
        ),
        (
            {
                "ok": False,
                "error_code": 403,
                "description": "Forbidden: bot was blocked by the user",
            },
            "bot was blocked",
        ),
        (
            {
                "ok": False,
                "error_code": 400,
                "description": "Bad Request: message is too long",
            },
            "message is too long",
        ),
    ],
)
async def test_a_refusal_that_will_not_change_is_logged_with_its_reason_once(
    answer, reason, no_waits, caplog
):
    ping, sent = _ping(answer)
    with caplog.at_level(logging.ERROR, logger=waitlist_ping.__name__):
        await ping("a@example.com")
    assert len(sent) == 1
    assert no_waits == []
    logged = _logged(caplog)
    assert "waitlist ping: not sent" in logged
    assert reason in logged
    assert TOKEN not in logged and CHAT not in logged


async def test_an_unexpected_failure_is_logged_and_never_raised(caplog):
    async def broken(chat_id, text):
        raise RuntimeError("a bug")

    with caplog.at_level(logging.ERROR, logger=waitlist_ping.__name__):
        await waitlist_ping.WaitlistPing(broken, _to(CHAT))("a@example.com")
    assert "NO MESSAGE WAS SENT" in _logged(caplog)


async def test_each_linked_operator_gets_it_and_a_refusal_stops_no_one(caplog):
    gone = {
        "ok": False,
        "error_code": 403,
        "description": "Forbidden: bot was blocked by the user",
    }
    ping, sent = _ping(gone, OK, chats=("111", "222"))
    with caplog.at_level(logging.ERROR, logger=waitlist_ping.__name__):
        await ping("a@example.com")
    assert [body["chat_id"] for body in sent] == ["111", "222"]
    assert "bot was blocked" in _logged(caplog)


class _Bot:
    async def send_text(self, chat_id, text):
        return "1"


@pytest.mark.parametrize(
    "bot, operators, missing",
    [
        (_Bot(), frozenset(), "OPS_USER_IDS"),
        (None, OPS, "TARGET_TELEGRAM_BOT_TOKEN"),
        (None, frozenset(), "TARGET_TELEGRAM_BOT_TOKEN, OPS_USER_IDS"),
    ],
)
def test_without_a_bot_or_an_operator_it_is_off_and_says_which(
    bot, operators, missing, caplog
):
    with caplog.at_level(logging.WARNING, logger=waitlist_ping.__name__):
        assert waitlist_ping.from_settings(bot, operators, _never) is None
    assert f"waitlist ping: off, not set: {missing}" in _logged(caplog)


async def _never():
    raise AssertionError("an off ping reads no one")


async def test_with_both_it_reads_its_operators_anew_for_each_ping():
    asked, sent = [], []
    linked = [[], [CHAT]]

    class Bot:
        async def send_text(self, chat_id, text):
            sent.append(chat_id)
            return "1"

    async def recipients():
        asked.append(True)
        return linked.pop(0)

    ping = waitlist_ping.from_settings(Bot(), OPS, recipients)
    await ping("a@example.com")
    await ping("b@example.com")
    assert len(asked) == 2
    assert sent == [CHAT]


async def test_no_linked_operator_sends_nothing_and_says_why(caplog):
    sent = []

    async def send(chat_id, text):
        sent.append(chat_id)
        return "1"

    with caplog.at_level(logging.WARNING, logger=waitlist_ping.__name__):
        await waitlist_ping.WaitlistPing(send, _to())("a@example.com")
    assert sent == []
    assert "no one in OPS_USER_IDS has linked Telegram" in _logged(caplog)


async def test_a_failed_read_is_logged_never_raised(caplog):
    async def unreadable():
        raise OSError("connection lost")

    async def send(chat_id, text):
        raise AssertionError("nothing to send to")

    ping = waitlist_ping.WaitlistPing(send, unreadable)
    with caplog.at_level(logging.ERROR, logger=waitlist_ping.__name__):
        await ping("a@example.com")
    assert "the operators could not be read" in _logged(caplog)
    assert "linked Telegram" not in _logged(caplog)
    assert ping._waiting == 0


@pytest.mark.parametrize(
    "env, operators, wired",
    [
        ({"TARGET_TELEGRAM_BOT_TOKEN": TOKEN}, OP, True),
        ({"TARGET_TELEGRAM_BOT_TOKEN": TOKEN}, "", False),
        ({}, OP, False),
    ],
)
def test_the_app_wires_the_ping_from_its_settings(env, operators, wired, monkeypatch):
    from src.api.app import create_app
    from src.config.settings import settings

    monkeypatch.setattr(settings, "OPS_USER_IDS", operators)
    app = create_app(env=env)
    assert isinstance(app.state.waitlist_ping, waitlist_ping.WaitlistPing) is wired


async def test_an_unexpected_failure_for_one_operator_still_reaches_the_next(caplog):
    sent = []

    async def send(chat_id, text):
        if chat_id == "111":
            raise RuntimeError("a bug")
        sent.append(chat_id)
        return "1"

    with caplog.at_level(logging.ERROR, logger=waitlist_ping.__name__):
        await waitlist_ping.WaitlistPing(send, _to("111", "222"))("a@example.com")
    assert sent == ["222"]
    assert "NO MESSAGE WAS SENT to one operator" in _logged(caplog)


async def test_a_slow_read_never_holds_the_queue_and_an_empty_one_frees_its_place():
    gate, sent = asyncio.Event(), []

    async def send(chat_id, text):
        sent.append(text)
        return "1"

    async def slow():
        await gate.wait()
        return []

    stuck = waitlist_ping.WaitlistPing(send, slow)
    reading = asyncio.ensure_future(stuck("slow@example.com"))
    await asyncio.sleep(0)
    assert stuck._waiting == 1
    # Another ping on the same queue is sent while the first still reads.
    stuck._recipients = _to(CHAT)
    await stuck("quick@example.com")
    assert any("quick@example.com" in text for text in sent)
    gate.set()
    await reading
    assert stuck._waiting == 0


async def test_a_ping_cancelled_while_reading_frees_its_place():
    async def never():
        await asyncio.Event().wait()

    ping = waitlist_ping.WaitlistPing(_Bot().send_text, never)
    task = asyncio.ensure_future(ping("a@example.com"))
    await asyncio.sleep(0)
    assert ping._waiting == 1
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert ping._waiting == 0
