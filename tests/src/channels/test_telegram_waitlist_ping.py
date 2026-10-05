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
OPS = frozenset({"00000000-0000-4000-8000-000000000001"})
OK = {"ok": True, "result": {"message_id": 1}}


def _ping(*answers):
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
    return waitlist_ping.WaitlistPing(bot.send_text, OPS), sent


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
    await ping("*bold*@example.com", [CHAT])
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
    await ping("a@example.com", [CHAT])
    assert len(sent) == 2
    assert no_waits == [7.0]


async def test_a_long_retry_after_is_capped(no_waits):
    ping, _ = _ping(_paced(3600), OK)
    await ping("a@example.com", [CHAT])
    assert no_waits == [waitlist_ping.MAX_PACED_WAIT_SECONDS]


async def test_429s_past_the_last_send_are_logged(no_waits, caplog):
    ping, sent = _ping(_paced(1))
    with caplog.at_level(logging.ERROR, logger=waitlist_ping.__name__):
        await ping("a@example.com", [CHAT])
    assert len(sent) == waitlist_ping.ATTEMPTS
    assert "waitlist ping: not sent after 3 tries" in _logged(caplog)


async def test_a_lost_answer_is_left_to_the_egress_floor_and_logged(caplog):
    lost = httpx.ConnectError(
        f"no route to https://api.telegram.org/bot{TOKEN}/sendMessage"
    )
    ping, sent = _ping(lost)
    with caplog.at_level(logging.ERROR, logger=waitlist_ping.__name__):
        await ping("a@example.com", [CHAT])
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

    ping = waitlist_ping.WaitlistPing(send, OPS)
    first = asyncio.ensure_future(ping("first@example.com", [CHAT]))
    await asyncio.sleep(0)
    queued = [
        asyncio.ensure_future(ping(f"q{i}@example.com", [CHAT]))
        for i in range(waitlist_ping.MAX_WAITING)
    ]
    await asyncio.sleep(0)
    with caplog.at_level(logging.ERROR, logger=waitlist_ping.__name__):
        await ping("dropped@example.com", [CHAT])
    assert "waitlist ping: dropped, 20 already waiting" in _logged(caplog)
    release.set()
    await asyncio.gather(first, *queued)
    assert len(sent) == 1 + waitlist_ping.MAX_WAITING
    assert not any("dropped@" in text for text in sent)


async def test_a_ping_cancelled_in_the_queue_frees_its_place():
    release = asyncio.Event()
    sent = []

    async def send(chat_id, text):
        sent.append(text)
        await release.wait()
        return "1"

    ping = waitlist_ping.WaitlistPing(send, OPS)
    first = asyncio.ensure_future(ping("first@example.com", [CHAT]))
    await asyncio.sleep(0)
    queued = [
        asyncio.ensure_future(ping(f"q{i}@example.com", [CHAT]))
        for i in range(waitlist_ping.MAX_WAITING)
    ]
    await asyncio.sleep(0)
    for task in queued:
        task.cancel()
    await asyncio.gather(*queued, return_exceptions=True)
    release.set()
    await first
    assert ping._waiting == 0
    await ping("later@example.com", [CHAT])
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

    ping = waitlist_ping.WaitlistPing(send, OPS)
    await asyncio.gather(*(ping(f"p{i}@example.com", [CHAT]) for i in range(5)))
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
        await ping("a@example.com", [CHAT])
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
        await waitlist_ping.WaitlistPing(broken, OPS)("a@example.com", [CHAT])
    assert "NO MESSAGE WAS SENT" in _logged(caplog)


async def test_each_linked_operator_gets_it_and_a_refusal_stops_no_one(caplog):
    gone = {
        "ok": False,
        "error_code": 403,
        "description": "Forbidden: bot was blocked by the user",
    }
    ping, sent = _ping(gone, OK)
    with caplog.at_level(logging.ERROR, logger=waitlist_ping.__name__):
        await ping("a@example.com", ["111", "222"])
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
        assert waitlist_ping.from_settings(bot, operators) is None
    assert f"waitlist ping: off, not set: {missing}" in _logged(caplog)


def test_with_both_it_knows_its_operators():
    assert waitlist_ping.from_settings(_Bot(), OPS).operators == OPS


@pytest.mark.parametrize(
    "env, operators, wired",
    [
        ({"TARGET_TELEGRAM_BOT_TOKEN": TOKEN}, next(iter(OPS)), True),
        ({"TARGET_TELEGRAM_BOT_TOKEN": TOKEN}, "", False),
        ({}, next(iter(OPS)), False),
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
        await waitlist_ping.WaitlistPing(send, OPS)("a@example.com", ["111", "222"])
    assert sent == ["222"]
    assert "NO MESSAGE WAS SENT to one operator" in _logged(caplog)
