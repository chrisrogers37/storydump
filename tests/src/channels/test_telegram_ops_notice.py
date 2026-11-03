"""The operators' Telegram message (`telegram_ops_notice`): sent once per key
and window to each operator who has linked Telegram, and never raised."""

import logging

import pytest

from src.channels.telegram_ops_notice import OpsNotice
from src.channels.telegram_transport import TelegramRefused
from tests.src.channels.test_telegram_waitlist_ping import _to

ONE, TWO = "424242", "535353"


class _Sends:
    """The transport's `send_text`, recording each send; Telegram refuses
    the chats in *refuse*."""

    def __init__(self, refuse=()):
        self.sent = []
        self._refuse = set(refuse)

    async def __call__(self, chat, text, *, reply_markup=None):
        if chat in self._refuse:
            raise TelegramRefused("Forbidden: bot was blocked by the user")
        self.sent.append((chat, text))
        return "1"


class _Latch:
    """The window's latch: True for the first claim of each key, as the
    `rate_counters` row answers."""

    def __init__(self):
        self.claims = []

    async def __call__(self, key):
        first = key not in self.claims
        self.claims.append(key)
        return first


async def _fails(*_):
    raise RuntimeError("pool exhausted")


async def test_a_notice_goes_to_every_linked_operator_once_per_key():
    sends = _Sends()
    notice = OpsNotice(sends, _to(ONE, TWO), _Latch())
    await notice("spec_drift:9004/2207052", "first")
    await notice("spec_drift:9004/2207052", "again")
    await notice("spec_drift:36003/None", "other")
    assert sends.sent == [
        (ONE, "first"),
        (TWO, "first"),
        (ONE, "other"),
        (TWO, "other"),
    ]


async def test_nothing_is_claimed_while_no_operator_has_linked_telegram(caplog):
    """The latch waits for someone to tell, so the first notice after an
    operator links still goes."""
    sends, latch = _Sends(), _Latch()
    with caplog.at_level(logging.WARNING):
        await OpsNotice(sends, _to(), latch)("k", "text")
    assert latch.claims == [] and sends.sent == []
    assert "no one in OPS_USER_IDS has linked Telegram" in caplog.text


async def test_a_refused_send_is_logged_and_the_next_operator_still_gets_it(caplog):
    sends = _Sends(refuse={ONE})
    with caplog.at_level(logging.ERROR):
        await OpsNotice(sends, _to(ONE, TWO), _Latch())("k", "text")
    assert sends.sent == [(TWO, "text")]
    assert "not sent to one operator: Forbidden" in caplog.text
    assert ONE not in caplog.text, "no chat id in a log line"


@pytest.mark.parametrize("broken", ["recipients", "claim"])
async def test_a_notice_whose_read_fails_is_logged_and_never_raised(broken, caplog):
    sends = _Sends()
    notice = OpsNotice(
        sends,
        _fails if broken == "recipients" else _to(ONE),
        _fails if broken == "claim" else _Latch(),
    )
    with caplog.at_level(logging.ERROR):
        await notice("k", "text")
    assert sends.sent == [] and "NOT SENT" in caplog.text
