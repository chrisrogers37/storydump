"""Why a delivery failed, recorded on the outbox row (090, #1482).

`settle` maps what the transport raised to a class and the provider's code, and
writes both, with the time, in the SAME CAS update that moves the row out of
`sending`, so a writer that lost the row records nothing. Asserted on the real
statements, through a scripted session, from a real Telegram reply (the
transport's own error mapping, with httpx faked as `test_telegram_transport.py`
fakes it) to the parameters of that one UPDATE. The two other writers, a
resolution that fails the row and the recovery of a stranded one, are pinned
the same way.
"""

from __future__ import annotations

import httpx
import pytest

from src.channels.telegram_transport import TelegramTransport
from src.services.target import outbox

TOKEN = "8675309:AAtestSECRETtokenVALUExyz"

ROW = {
    "id": "row-1",
    "binding_id": "b-1",
    "kind": "notification",
    "attempts": 1,
    "intent_id": None,
    "payload": {"v": 1, "text": "hi"},
}


class _Session:
    """Records every statement and answers with *rowcount* (0 = fenced)."""

    def __init__(self, rowcount=1, first=None, fetched=()):
        self.statements = []
        self.rowcount = rowcount
        self._first = first
        self._fetched = list(fetched)

    async def execute(self, stmt, params=None):
        self.statements.append((" ".join(str(stmt).split()), dict(params or {})))
        session = self

        class _Result:
            rowcount = session.rowcount

            def first(self_inner):
                return session._first

            def fetchall(self_inner):
                return [(i,) for i in session._fetched]

        return _Result()


async def _raised_by(status=None, body=None, *, text=None, exc=None):
    """What the transport raises for one reply (or for no reply at all)."""

    def handler(request):
        if exc is not None:
            raise exc
        if text is not None:
            return httpx.Response(status, text=text)
        return httpx.Response(status, json=body)

    transport = TelegramTransport(
        TOKEN, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(Exception) as caught:
        await transport.for_chat("7")(ROW)
    return caught.value


def _refusal(code, description, **parameters):
    body = {"ok": False, "error_code": code, "description": description}
    if parameters:
        body["parameters"] = parameters
    return body


#: (a Telegram reply, the class and code the row records, the state it moves to)
CASES = [
    pytest.param(
        dict(status=401, body=_refusal(401, "Unauthorized")),
        ("credential_dead", 401),
        "ambiguous",
        id="a dead token is credential_dead, not a lost response",
    ),
    pytest.param(
        dict(
            status=403,
            body=_refusal(403, "Forbidden: bot was kicked from the supergroup chat"),
        ),
        ("destination_gone", 403),
        "failed",
        id="a kicked bot is destination_gone with Telegram's 403",
    ),
    pytest.param(
        dict(
            status=400,
            body=_refusal(
                400,
                "Bad Request: group chat was upgraded to a supergroup chat",
                migrate_to_chat_id=-1009999,
            ),
        ),
        ("destination_gone", 400),
        "failed",
        id="a moved chat is destination_gone with Telegram's 400",
    ),
    pytest.param(
        dict(status=400, body=_refusal(400, "Bad Request: message is too long")),
        ("refused", 400),
        "failed",
        id="a message Telegram will never take is refused",
    ),
    pytest.param(
        dict(status=413, body=_refusal(413, "Request Entity Too Large")),
        ("refused", 413),
        "failed",
        id="a payload too large is refused with its 413",
    ),
    pytest.param(
        dict(
            status=429,
            body=_refusal(429, "Too Many Requests: retry after 3", retry_after=3),
        ),
        ("rate_limited", 429),
        "pending",
        id="a 429 is recorded as rate_limited and still goes back to pending",
    ),
    pytest.param(
        dict(status=502, body=_refusal(502, "Bad Gateway")),
        ("ambiguous", 502),
        "ambiguous",
        id="a 5xx is ambiguous with the provider's code",
    ),
    pytest.param(
        dict(status=502, text="<html>bad gateway</html>"),
        ("ambiguous", 502),
        "ambiguous",
        id="a 5xx with no JSON is ambiguous with the HTTP status",
    ),
    pytest.param(
        dict(exc=httpx.ConnectError("down")),
        ("ambiguous", None),
        "ambiguous",
        id="no answer at all is ambiguous with no code",
    ),
]


class TestTheClassAndCodeRideTheOneCAS:
    @pytest.mark.parametrize("reply, recorded, to_state", CASES)
    async def test_a_telegram_reply_is_recorded_in_the_update_that_leaves_sending(
        self, reply, recorded, to_state
    ):
        error = await _raised_by(**reply)
        session = _Session()
        result = await outbox.settle(session, ROW, error=error)

        assert len(session.statements) == 1, "one statement: no second write"
        sql, params = session.statements[0]
        assert sql.startswith("UPDATE channel_outbox SET state = :s,")
        assert sql.endswith("WHERE id = :i AND state = 'sending'"), "the one CAS"
        for column in (
            "last_failure_class = :fc",
            "last_error_code = :fcode",
            "last_failed_at = now()",
        ):
            assert column in sql
        assert (params["fc"], params["fcode"]) == recorded
        assert params["s"] == to_state and params["i"] == "row-1"
        assert result["external_message_ref"] is None

    async def test_a_dead_token_changes_no_state_until_1493_decides(self):
        """The record is new; the behaviour is not. A 401's row still takes
        the ambiguous path, the one it took before 090."""
        error = await _raised_by(401, _refusal(401, "Unauthorized"))
        result = await outbox.settle(_Session(), ROW, error=error)
        assert result["state"] == "ambiguous"
        assert set(result) == set(ROW) | {"state", "external_message_ref"}

    async def test_a_fenced_settle_records_nothing(self):
        """The row left `sending` under us: the CAS matches nothing, so the
        failure record is not written either, and the writer learns it lost."""
        error = await _raised_by(401, _refusal(401, "Unauthorized"))
        session = _Session(rowcount=0)
        with pytest.raises(outbox.OutboxFenced):
            await outbox.settle(session, ROW, error=error)
        assert len(session.statements) == 1, "no retry, no separate record"

    async def test_a_success_writes_no_failure_and_clears_none(self):
        """A later success leaves the last failure where it is; `state` says
        how the row ended."""
        session = _Session()
        await outbox.settle(session, ROW, receipt="4242")
        sql, params = session.statements[0]
        assert "last_failure" not in sql and "last_error_code" not in sql
        assert "last_failed_at" not in sql
        assert params["s"] == "sent"


class TestTheProviderCode:
    """Only a code the provider actually sent is recorded."""

    def test_an_int_is_kept(self):
        assert outbox._provider_code(outbox.ChannelRefused("x", code=400)) == 400

    @pytest.mark.parametrize(
        "error",
        [
            RuntimeError("timeout"),
            outbox.ChannelRefused("x"),
            outbox.ChannelRefused("x", code="400"),
            outbox.ChannelRefused("x", code=True),
        ],
        ids=["no attribute", "None", "a string", "a bool"],
    )
    def test_anything_else_is_none(self, error):
        assert outbox._provider_code(error) is None

    def test_a_channel_neutral_error_carries_its_code(self):
        assert outbox.CredentialDead("x", code=401).code == 401
        assert outbox.DestinationGone("kicked", code=403).code == 403
        assert outbox.ChannelPaced("429", retry_after_s=1).code == 429


class TestTheOtherWritersRecordIt:
    async def test_a_resolution_that_fails_the_row_moves_the_time_and_keeps_the_class(
        self,
    ):
        """The row is counted when it FAILS, not when it first went ambiguous;
        its class stays what the send recorded (`ambiguous` for a row older
        than the columns)."""
        spent = outbox.MAX_NOTIFICATION_RESENDS + 1
        session = _Session(first=("notification", spent, False))
        assert await outbox.resolve_ambiguous(session, outbox_id="row-1") == "failed"
        sql, params = session.statements[-1]
        assert sql.startswith("UPDATE channel_outbox SET state = :s,")
        assert "last_failed_at = now()" in sql
        assert "last_failure_class = COALESCE(last_failure_class, 'ambiguous')" in sql
        assert "last_error_code" not in sql, "the code the send recorded stays"
        assert sql.endswith("WHERE id = :i AND state = 'ambiguous'")
        assert params == {"s": "failed", "i": "row-1"}

    @pytest.mark.parametrize(
        "row, to_state",
        [
            (("notification", outbox.MAX_NOTIFICATION_RESENDS, False), "pending"),
            (("prompt_supersede", 1, True), "superseded"),
        ],
        ids=["resent", "superseded"],
    )
    async def test_a_resolution_that_does_not_fail_it_records_nothing(
        self, row, to_state
    ):
        session = _Session(first=row)
        assert await outbox.resolve_ambiguous(session, outbox_id="row-1") == to_state
        sql, _ = session.statements[-1]
        assert sql == (
            "UPDATE channel_outbox SET state = :s WHERE id = :i AND state = 'ambiguous'"
        )

    async def test_a_stranded_row_is_recorded_ambiguous_with_no_code(self):
        session = _Session(fetched=["row-1"])
        assert await outbox.recover_stranded(session, binding_id="b-1") == ["row-1"]
        sql, params = session.statements[0]
        assert "state = 'ambiguous'" in sql
        assert "last_failure_class = 'ambiguous'" in sql
        assert "last_error_code = NULL" in sql and "last_failed_at = now()" in sql
        assert sql.endswith("WHERE binding_id = :b AND state = 'sending' RETURNING id")
        assert params == {"b": "b-1"}
