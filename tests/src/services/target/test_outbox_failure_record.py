"""Why a delivery failed, recorded on the outbox row (101, #1482).

`settle` maps what the transport raised to a class and the provider's code, and
writes both, with the time, in the SAME CAS update that moves the row out of
`sending`, so a writer that lost the row records nothing. Asserted on the real
statements, through a scripted session, from a real Telegram reply (the
transport's own error mapping, with httpx faked as `test_telegram_transport.py`
fakes it) to the parameters of that one UPDATE. The recovery of a stranded row
records too, and a resolution of an ambiguous row records nothing: the columns
describe the last failed ATTEMPT, and a resolution is not one.
"""

from __future__ import annotations

import httpx
import pytest

from src.services.target import outbox, vocabulary
from tests.src.channels.test_telegram_transport import _transport

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

    with pytest.raises(Exception) as caught:
        await _transport(handler).for_chat("7")(ROW)
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
        "failed",
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

    async def test_a_dead_token_keeps_the_binding(self):
        """A 401 fails its row (#1493; the 401 case above), but it is the bot's
        credential, not the chat: the result names no gone destination, which
        is what the sender reads before it retires a binding."""
        error = await _raised_by(401, _refusal(401, "Unauthorized"))
        result = await outbox.settle(_Session(), ROW, error=error)
        assert not result.get("destination_gone")

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


class TestTheErrorCarriesItsClassAndCode:
    """Each definitive answer names its own class, set on the type, and the
    code is set once, by the base: an int the provider sent, or nothing."""

    def test_each_error_names_a_class_the_column_allows(self):
        classes = {
            outbox.ChannelSendError: "ambiguous",
            outbox.ChannelPaced: "rate_limited",
            outbox.DestinationGone: "destination_gone",
            outbox.ChannelRefused: "refused",
            outbox.CredentialDead: "credential_dead",
        }
        assert {cls: cls.failure_class for cls in classes} == classes
        assert set(classes.values()) == set(vocabulary.OUTBOX_FAILURE_CLASSES)

    def test_an_int_is_kept(self):
        assert outbox.ChannelRefused("x", code=400).code == 400
        assert outbox.DestinationGone("kicked", code=403).code == 403

    @pytest.mark.parametrize(
        "error",
        [
            outbox.ChannelRefused("x"),
            outbox.ChannelRefused("x", code="400"),
            outbox.ChannelRefused("x", code=True),
            outbox.ChannelPaced("429", retry_after_s=1),
        ],
        ids=["none sent", "a string", "a bool", "a 429 with no code sent"],
    )
    def test_anything_else_is_none(self, error):
        assert error.code is None

    async def test_a_foreign_error_is_a_lost_answer_whatever_it_carries(self):
        """A `.code` on an exception that is not the transport's is not the
        provider's, and is not recorded as one."""

        class Foreign(Exception):
            code = 418
            failure_class = "refused"

        session = _Session()
        await outbox.settle(session, ROW, error=Foreign("boom"))
        _, params = session.statements[0]
        assert (params["fc"], params["fcode"], params["s"]) == (
            "ambiguous",
            None,
            "ambiguous",
        )


class TestTheOtherWriters:
    @pytest.mark.parametrize(
        "row, to_state",
        [
            (("notification", outbox.MAX_NOTIFICATION_RESENDS + 1, False), "failed"),
            (("notification", outbox.MAX_NOTIFICATION_RESENDS, False), "pending"),
            (("prompt_supersede", 1, True), "superseded"),
        ],
        ids=["failed", "resent", "superseded"],
    )
    async def test_a_resolution_never_touches_the_record(self, row, to_state):
        """The columns describe the last failed attempt, and a resolution is
        not one: a row that fails here keeps the class, code and time its last
        attempt recorded, and is counted from then."""
        session = _Session(first=row)
        assert await outbox.resolve_ambiguous(session, outbox_id="row-1") == to_state
        sql, params = session.statements[-1]
        assert sql == (
            "UPDATE channel_outbox SET state = :s WHERE id = :i AND state = 'ambiguous'"
        )
        assert params == {"s": to_state, "i": "row-1"}

    async def test_a_stranded_row_is_recorded_ambiguous_with_no_code(self):
        session = _Session(fetched=["row-1"])
        assert await outbox.recover_stranded(session, binding_id="b-1") == ["row-1"]
        sql, params = session.statements[0]
        assert "state = 'ambiguous'" in sql
        assert "last_failure_class = 'ambiguous'" in sql
        assert "last_error_code = NULL" in sql and "last_failed_at = now()" in sql
        assert sql.endswith("WHERE binding_id = :b AND state = 'sending' RETURNING id")
        assert params == {"b": "b-1"}
