"""The invitation email, sent after the invitation commits.

`invitations.InvitationEmail` debits the person's day and then the provider's,
sends once, and answers a delivery state rather than raising, because by then
the invitation exists and the inviter holds its link. Nothing it logs or shows
in its ``repr`` carries the address or the link.

`invitations.reissue` is "Email me the link again" (115): the invitee's day
and the provider's are debited before the door rotates the token, so a spent
day leaves the old link working; its answer never carries the new link.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

from src.services.target import (
    egress,
    email_sender,
    invitations,
    rate_counters,
    vocabulary,
)
from src.services.target.email_sender import EmailRefused
from tests.src.services.target.conftest import RecordingSender

LINK = "https://app.test/join/tok"
ADDRESS = "partner@example.com"
PARAMS = invitations.email_params(
    workspace_name="Acme",
    accept_url=LINK,
    invited_email=ADDRESS,
    expires_at=datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc),
    inviter_name="Dana",
)
EMAIL = invitations.InvitationEmail(
    invitation_id="inv-1", inviter_user_id="u-1", to=ADDRESS, params=PARAMS
)


class _Counters:
    """`rate_counters.increment`, scripted: each call takes the next answer."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    async def __call__(self, executor, *, scope, key, window_start, limit):
        self.calls.append((scope, key, limit))
        return self.answers.pop(0)


async def _run(sender):
    """`EMAIL.run`'s answer, held to the closed lists landing pins
    (`wire-contract.test.ts`): a state outside them reads there as failed."""
    answer = await EMAIL.run(sender, "engine")
    delivery = answer["delivery"]
    assert delivery["state"] in vocabulary.INVITATION_DELIVERY_STATES
    if delivery["state"] == "over_budget":
        assert delivery["reason"] in vocabulary.EMAIL_BUDGET_REASONS
    return answer


def _delivery(state, reason=None):
    return {
        "delivery": {
            "channel": "email",
            "state": state,
            **({"reason": reason} if reason else {}),
        }
    }


@pytest.fixture
def opened(monkeypatch):
    """`email_sender.system_session`, recorded: the debit's own transaction."""
    log = []

    @asynccontextmanager
    async def session(engine):
        log.append(engine)
        yield object()

    monkeypatch.setattr(email_sender, "system_session", session)
    return log


class TestTheEmailAfterTheCommit:
    async def test_no_sender_is_not_configured_and_spends_nothing(
        self, monkeypatch, opened
    ):
        counters = _Counters()
        monkeypatch.setattr(rate_counters, "increment", counters)
        assert await _run(None) == _delivery("not_configured")
        assert counters.calls == []
        assert opened == []

    async def test_the_person_then_the_provider_are_debited_and_one_email_goes(
        self, monkeypatch, opened
    ):
        counters = _Counters(1, 1)
        monkeypatch.setattr(rate_counters, "increment", counters)
        sender = RecordingSender()
        assert await _run(sender) == _delivery("sent")
        assert opened == ["engine"]
        assert counters.calls == [
            ("email_global", "invite:u-1", invitations.INVITES_PER_PERSON_PER_DAY),
            ("email_global", "", email_sender.BUDGET_LIMIT),
        ]
        [mail] = sender.sent
        assert mail["to"] == ADDRESS
        assert mail["subject"] == email_sender.INVITATION_SUBJECT
        assert mail["body"].startswith('"Dana" has invited you')
        assert LINK in mail["body"]

    @pytest.mark.parametrize(
        "answers, reason, debits",
        [((None,), "person_limit", 1), ((1, None), "daily_limit", 2)],
    )
    async def test_a_spent_day_sends_nothing_and_says_whose(
        self, monkeypatch, opened, answers, reason, debits
    ):
        counters = _Counters(*answers)
        monkeypatch.setattr(rate_counters, "increment", counters)
        sender = RecordingSender()
        assert await _run(sender) == _delivery("over_budget", reason)
        assert len(counters.calls) == debits
        assert sender.sent == []

    @pytest.mark.parametrize(
        "raised, reason",
        [
            (EmailRefused("provider_rejected", "http_422"), "provider_rejected"),
            (egress.EgressBudgetExhausted("timed out"), "provider_unreachable"),
            (egress.EgressRefused("cannot resolve"), "provider_unreachable"),
            (RuntimeError("boom"), "internal"),
        ],
    )
    async def test_a_failed_send_is_a_delivery_state_never_a_raise(
        self, monkeypatch, opened, raised, reason
    ):
        monkeypatch.setattr(rate_counters, "increment", _Counters(1, 1))
        assert await _run(RecordingSender(raises=raised)) == _delivery("failed", reason)

    async def test_its_logs_name_the_invitation_never_the_address_or_the_link(
        self, monkeypatch, opened, caplog
    ):
        caplog.set_level(logging.DEBUG, logger=invitations.__name__)
        refused = EmailRefused("provider_rejected", "http_422")
        for answers, sender in (
            ((1, 1), RecordingSender()),
            ((1, 1), RecordingSender(raises=refused)),
            ((None,), RecordingSender()),
        ):
            monkeypatch.setattr(rate_counters, "increment", _Counters(*answers))
            await _run(sender)
        assert "inv-1" in caplog.text
        assert ADDRESS not in caplog.text
        assert LINK not in caplog.text and "tok" not in caplog.text

    def test_its_repr_names_the_invitation_and_hides_the_address_and_the_link(self):
        shown = repr(EMAIL)
        assert "inv-1" in shown
        assert ADDRESS not in shown
        assert LINK not in shown


class TestTheTemplateParams:
    def test_the_expiry_is_a_utc_date_and_a_blank_name_reads_your_workspace(self):
        assert invitations.email_params(
            workspace_name="  ",
            accept_url=LINK,
            invited_email=ADDRESS,
            expires_at=datetime(2026, 10, 18, 23, 30, tzinfo=timezone.utc),
            inviter_name=None,
        ) == {
            "workspace_name": "your workspace",
            "accept_url": LINK,
            "invited_email": ADDRESS,
            "expires_on": "October 18, 2026 (UTC)",
        }


class TestTheAddressIsOneAnEmailCanReach:
    """`create` refuses an address the email could not go to, before any SQL:
    invitations have no CHECK of their own, so the writer holds the rule."""

    class _NoSql:
        async def execute(self, *args, **kwargs):
            raise AssertionError("SQL ran for a malformed address")

    @pytest.mark.parametrize(
        "bad",
        [
            "no-at-sign",
            "two@@example.com",
            "a@nodot",
            "a b@example.com",
            "a@exa mple.com",
            "x@example.com​",
            "a" * 250 + "@example.com",
        ],
    )
    async def test_a_malformed_address_is_refused_before_any_sql(self, bad):
        with pytest.raises(invitations.InvitationRefused) as refused:
            await invitations.create(
                self._NoSql(),
                workspace_id="ws",
                invited_by_user_id="u",
                role="member",
                delivery_channel="email",
                email=bad,
            )
        assert refused.value.reason == "email_invalid"


#: What the list door answers for the invitation being re-sent, as `reissue`
#: reads it, with the caller's verified address.
LISTED = {
    "email": ADDRESS,
    "expires_at": datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc),
    "invited_by_user_id": "u-1",
    "workspace_name": "Acme",
    "inviter": "Dana",
}


@pytest.fixture
def door(monkeypatch):
    """`reissue`'s two reads, scripted in order: the list door's row for the
    invitation (None: not one of theirs), then the rotation's answer."""
    calls = {"listed": LISTED, "rotated": True, "asked": []}

    async def row(executor, sql, **params):
        calls["asked"].append(params)
        if "fn_invitation_reissue" in sql:
            return {"done": calls["rotated"]}
        return calls["listed"]

    monkeypatch.setattr(invitations.readers, "row", row)
    return calls


async def _reissue(sender, *, origin="https://app.test"):
    return await invitations.reissue(
        "engine", sender, user_id="u-9", invitation_id="inv-1", web_app_origin=origin
    )


class TestTheLinkSentAgain:
    """The token is replaced only once the provider has taken the email, so a
    send that fails leaves the link the person already has working."""

    @pytest.mark.parametrize(
        "sender, origin", [(None, "https://app.test"), ("set", None)]
    )
    async def test_with_nothing_to_send_nothing_is_opened(
        self, monkeypatch, opened, door, sender, origin
    ):
        monkeypatch.setattr(rate_counters, "increment", _Counters())
        sender = RecordingSender() if sender == "set" else None
        assert await _reissue(sender, origin=origin) == _delivery("not_configured")
        assert opened == [] and door["asked"] == []

    async def test_one_not_theirs_is_not_acceptable_before_anything_is_spent(
        self, monkeypatch, opened, door
    ):
        counters = _Counters()
        monkeypatch.setattr(rate_counters, "increment", counters)
        door["listed"] = None
        sender = RecordingSender()
        with pytest.raises(invitations.InvitationRefused) as refused:
            await _reissue(sender)
        assert refused.value.reason == "not_acceptable"
        assert counters.calls == [] and sender.sent == []

    async def test_a_spent_day_sends_nothing_and_replaces_nothing(
        self, monkeypatch, opened, door
    ):
        counters = _Counters(None)
        monkeypatch.setattr(rate_counters, "increment", counters)
        sender = RecordingSender()
        assert await _reissue(sender) == _delivery("over_budget", "person_limit")
        assert counters.calls == [
            ("email_global", "resend:u-9", invitations.RESENDS_PER_PERSON_PER_DAY)
        ]
        assert sender.sent == []
        assert len(door["asked"]) == 1, "the rotation was never asked"

    async def test_a_failed_send_replaces_nothing(self, monkeypatch, opened, door):
        monkeypatch.setattr(rate_counters, "increment", _Counters(1, 1))
        refused = EmailRefused("provider_rejected", "http_422")
        answer = await _reissue(RecordingSender(raises=refused))
        assert answer == _delivery("failed", "provider_rejected")
        assert len(door["asked"]) == 1, "the rotation was never asked"

    async def test_the_new_link_goes_to_their_address_and_is_stored_after_it_went(
        self, monkeypatch, opened, door, caplog
    ):
        caplog.set_level(logging.DEBUG, logger=invitations.__name__)
        monkeypatch.setattr(rate_counters, "increment", _Counters(1, 1))
        sender = RecordingSender()
        answer = await _reissue(sender)
        assert answer == _delivery("sent")
        [mail] = sender.sent
        assert mail["to"] == ADDRESS
        assert mail["subject"] == email_sender.INVITATION_SUBJECT
        assert mail["body"].startswith('"Dana" has invited you')
        _listed, rotation = door["asked"]
        token = mail["token"]
        assert f"https://app.test/join/{token}" in mail["body"]
        assert rotation["h"] == invitations.sessions.token_hash(token)
        assert token not in str(answer) and token not in caplog.text
        assert ADDRESS not in caplog.text

    async def test_one_that_closed_before_the_rotation_is_not_acceptable(
        self, monkeypatch, opened, door
    ):
        """An accept or a revoke that lands between the send and the rotation
        wins: the email went, and its link is the one that is dead."""
        monkeypatch.setattr(rate_counters, "increment", _Counters(1, 1))
        door["rotated"] = False
        sender = RecordingSender()
        with pytest.raises(invitations.InvitationRefused) as refused:
            await _reissue(sender)
        assert refused.value.reason == "not_acceptable"
        assert len(sender.sent) == 1
