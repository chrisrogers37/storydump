"""The `EmailSender` port's pure halves and its provider adapter (#1092).

All HTTP is faked with `httpx.MockTransport` — the egress floor runs for real
(policy, host allowlist, byte cap), the network does not. That split matters
here more than usual: the floor is the only thing standing between a template
parameter and an arbitrary host, and a test that stubbed the floor would be
asserting against the wrong object.

The database halves — the budget debit and the over-budget deferral — are in
`tests/scripts/test_email_gate.py`, because neither can be proven without the
`rate_counters` row lock and a real `jobs` row. The admission branch's order
is pinned here with its seams faked; its store against the real door is
`tests/scripts/test_waitlist_gate.py`'s.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx
import pytest

from src.services.target import email_sender, jobs, sessions, signup_admissions
from src.services.target.email_sender import (
    ADMISSION_SUBJECT,
    BUDGET_LIMIT,
    DEFAULT_POLICY,
    INTERACTIVE_POLICY,
    INVITATION_SUBJECT,
    PROVIDER_HOST,
    RETRY_LADDER_SECONDS,
    backoff_seconds,
    EmailRefused,
    execute_send_email,
    render,
    ResendSender,
    sender_from_env,
)


def _client(handler):
    """A client the TEST owns and closes.

    Deliberately not an async fixture. An `httpx.AsyncClient` holds a
    connection pool bound to the loop it was created on, and an async fixture
    under `asyncio_default_fixture_loop_scope=None` can be torn down on a
    different loop than the test ran on — leaving one loop uncollected. That
    surfaces later as `ResourceWarning: unclosed event loop`, which
    `filterwarnings = error` turns into a failure attributed to whatever test
    triggered the collection — the first test of a DIFFERENT module, which
    reads like the known timing flake (#1072). Owning the client in the test
    body keeps creation and close on one loop.
    """
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _sender(client, **over):
    """A sender whose floor policy skips DNS.

    The SSRF guard resolves EVERY allowlisted host before the request — that is
    it doing its job — so a test that fakes only the transport still performs a
    live `getaddrinfo` for the provider host: real network in a unit test, and
    slow. `without(enforce_private_address_block=False)` is the floor's own seam
    for this, the one `test_google_drive_adapter` uses. The HOST ALLOWLIST is
    checked before resolution and is left on, so the refusal test below still
    exercises the thing it names.
    """
    kwargs = {
        "api_key": "re_test_key",
        "sender": "Storydump <no-reply@example.com>",
        "policy": DEFAULT_POLICY.without(enforce_private_address_block=False),
    }
    kwargs.update(over)
    return ResendSender(client=client, **kwargs)


#: The `invitation` template's required params, as `invitations.email_params`
#: builds them from the row.
INVITE = {
    "workspace_name": "Acme",
    "accept_url": "https://x/join/tok",
    "invited_email": "partner@example.com",
    "expires_on": "October 18, 2026 (UTC)",
}


class TestRender:
    def test_the_invitation_carries_the_accept_link(self):
        subject, body = render("invitation", INVITE)
        assert subject == INVITATION_SUBJECT
        assert 'the workspace "Acme" on Storydump' in body
        assert "https://x/join/tok" in body

    def test_it_says_which_google_account_and_how_long_the_link_lasts(self):
        """Accept compares the Google account's verified email with the
        invited one, so the email says which account to sign in with; the link
        works once and expires, so it says both."""
        _, body = render("invitation", INVITE)
        assert "Sign in with Google as partner@example.com" in body
        assert "works once" in body
        assert "October 18, 2026 (UTC)" in body

    def test_the_inviter_is_optional_and_changes_the_opener(self):
        subject, with_name = render("invitation", {**INVITE, "inviter_name": "Dana"})
        _, without = render("invitation", INVITE)
        assert with_name.startswith('"Dana" has invited you')
        assert without.startswith("You have been invited")
        assert subject == INVITATION_SUBJECT

    def test_text_the_inviter_chose_is_one_quoted_line_and_never_the_subject(self):
        """The workspace's name and the inviter's Google name are the inviter's
        to choose, and this email goes to any address signed by our domain: a
        name cannot break the line, reorder it, or close its quotes."""
        subject, body = render(
            "invitation",
            {
                **INVITE,
                "workspace_name": "Acme\nRe: verify your account\u202e",
                "inviter_name": 'Dana\t"Security"',
            },
        )
        assert subject == INVITATION_SUBJECT
        first = body.split("\n", 1)[0]
        assert first == (
            "\"Dana 'Security'\" has invited you to join the workspace"
            ' "Acme Re: verify your account" on Storydump.'
        )
        assert "\u202e" not in body

    @pytest.mark.parametrize("bad", ["nope", "", None, 7])
    def test_an_unknown_template_is_refused_by_name(self, bad):
        """A permissive renderer's failure mode is an email that goes out saying
        the wrong thing. There is no generic fallback on purpose."""
        with pytest.raises(EmailRefused) as exc:
            render(bad, {})
        assert exc.value.reason == "unknown_template"

    @pytest.mark.parametrize("missing", sorted(INVITE))
    def test_a_missing_parameter_is_refused_by_name(self, missing):
        params = dict(INVITE)
        del params[missing]
        with pytest.raises(EmailRefused) as exc:
            render("invitation", params)
        assert exc.value.reason == "template_params_missing"
        assert missing in str(exc.value)


class TestTheActivationNudge:
    """#1481's one reminder: a subject and body per step, the link, and the
    line that says it is the only one."""

    LINK = "https://app.example/dashboard/queue"

    @pytest.mark.parametrize("step", ["instagram", "folder", "approval"])
    def test_each_step_carries_its_link_and_says_it_is_the_only_one(self, step):
        subject, body = render("activation_nudge", {"step": step, "link": self.LINK})
        assert subject.strip()
        assert self.LINK in body
        assert "the only reminder" in body

    def test_the_folder_step_warns_about_googles_screen(self):
        """It mirrors the Drive card, so a person who meets Google's warning
        after the email has already been told it is coming."""
        _, body = render("activation_nudge", {"step": "folder", "link": self.LINK})
        assert "hasn't verified this app" in body

    def test_a_step_with_no_copy_is_refused_by_name(self):
        with pytest.raises(EmailRefused) as exc:
            render("activation_nudge", {"step": "billing", "link": self.LINK})
        assert exc.value.reason == "template_params_missing"
        assert "step" in str(exc.value)

    @pytest.mark.parametrize("missing", ["step", "link"])
    def test_a_missing_parameter_is_refused_by_name(self, missing):
        params = {"step": "instagram", "link": self.LINK}
        del params[missing]
        with pytest.raises(EmailRefused) as exc:
            render("activation_nudge", params)
        assert exc.value.reason == "template_params_missing"
        assert missing in str(exc.value)


#: The `admission` template's params as the worker renders them: the job's
#: address, and the claim link it builds from the job's base and a token.
ADMISSION = {
    "address": "waiting@example.com",
    "claim_url": "https://app.example/claim/tok",
}


class TestTheAdmissionEmail:
    """#1723's one email to an admitted person: a fixed subject, the claim
    link, which Google account to use, and what to do if it was unexpected."""

    def test_it_carries_the_claim_link_under_a_fixed_subject(self):
        subject, body = render("admission", ADMISSION)
        assert subject == ADMISSION_SUBJECT
        assert "your spot on storydump is ready" in body.lower()
        assert "https://app.example/claim/tok" in body

    def test_it_says_any_google_account_works_once_and_the_address_without_it(self):
        _, body = render("admission", ADMISSION)
        assert "any Google account" in body
        assert "becomes your Storydump sign-in" in body
        assert "works once" in body
        assert "signing in with Google as waiting@example.com works without it" in body

    def test_it_says_to_ignore_it_if_unexpected(self):
        """The owner may admit an address that never joined."""
        _, body = render("admission", ADMISSION)
        assert "If you weren't expecting this, you can ignore this message" in body

    @pytest.mark.parametrize("missing", sorted(ADMISSION))
    def test_a_missing_parameter_is_refused_by_name(self, missing):
        params = dict(ADMISSION)
        del params[missing]
        with pytest.raises(EmailRefused) as exc:
            render("admission", params)
        assert exc.value.reason == "template_params_missing"
        assert missing in str(exc.value)


TOKEN = "drawn-token-for-this-send-0123456789abcdef"
TO = "waiting@example.com"
BASE = "https://app.example/claim/"


def _job(template="admission", params=None):
    return {
        "id": "job-7",
        "lease_token": "lease-7",
        "attempts": 0,
        "payload": {
            "v": 1,
            "to": TO,
            "template": template,
            "params": (
                {"address": TO, "claim_base": BASE} if params is None else params
            ),
        },
    }


class _Seams:
    """Every seam `execute_send_email` touches, faked and recorded in one list
    of events. A session is "open" between its `open` and `close`, and the
    sender records whether one was open when it sent."""

    def __init__(self, monkeypatch, *, budget=None, stored=True, raises=None):
        self.events, self.sent, self.stored_with = [], [], []
        self.open = 0
        self.budget, self.stored, self.raises = budget, stored, raises
        seams = self

        def new_token():
            seams.events.append("draw")
            return TOKEN

        real_render = email_sender.render

        def render_(template, params):
            seams.events.append("render")
            return real_render(template, params)

        @asynccontextmanager
        async def system_session(engine):
            seams.events.append("open")
            seams.open += 1
            try:
                yield object()
            finally:
                seams.open -= 1
                seams.events.append("close")

        async def debit_budget(session, **kw):
            seams.events.append("debit")
            return seams.budget

        async def reschedule_job(session, job_id, lease_token, **kw):
            seams.events.append("defer")

        async def store_link(session, *, to, token):
            seams.events.append("store")
            seams.stored_with.append((to, token))
            return seams.stored

        monkeypatch.setattr(sessions, "new_token", new_token)
        monkeypatch.setattr(email_sender, "render", render_)
        monkeypatch.setattr(email_sender, "system_session", system_session)
        monkeypatch.setattr(email_sender, "debit_budget", debit_budget)
        monkeypatch.setattr(jobs, "reschedule_job", reschedule_job)
        monkeypatch.setattr(signup_admissions, "store_link", store_link)

    async def send(self, *, to, subject, body):
        self.events.append("send")
        self.sent.append(
            {"to": to, "subject": subject, "body": body, "open": self.open}
        )
        if self.raises is not None:
            raise self.raises
        return "prov-7"


async def _execute(seams, job):
    return await execute_send_email(job, sender=seams, engine=object())


class TestTheAdmissionBranch:
    """`execute_send_email` for the `admission` template: draw, render, debit,
    send with no transaction open, then store the link's hash in a fresh one.
    Every other template's path is unchanged."""

    async def test_it_draws_renders_debits_sends_then_stores(self, monkeypatch):
        seams = _Seams(monkeypatch)

        assert await _execute(seams, _job()) == "prov-7"

        assert seams.events == [
            "draw",
            "render",
            "open",
            "debit",
            "close",
            "send",
            "open",
            "store",
            "close",
        ]
        [mail] = seams.sent
        assert mail["open"] == 0, "no transaction spans the provider call"
        assert mail["to"] == TO
        assert f"{BASE}{TOKEN}" in mail["body"]
        assert seams.stored_with == [(TO, TOKEN)], "the link the email carried"

    async def test_over_budget_it_defers_and_stores_nothing(self, monkeypatch):
        seams = _Seams(monkeypatch, budget="daily_limit")

        assert await _execute(seams, _job()) is None

        assert "send" not in seams.events and "store" not in seams.events
        assert "defer" in seams.events

    async def test_a_failed_send_stores_nothing_and_raises(self, monkeypatch):
        """The job retries with a fresh token, and the link already emailed
        keeps working, because nothing replaced it."""
        seams = _Seams(
            monkeypatch,
            raises=EmailRefused("provider_rejected", "http_422"),
        )

        with pytest.raises(EmailRefused):
            await _execute(seams, _job())

        assert "store" not in seams.events

    async def test_a_spot_taken_meanwhile_still_succeeds_with_one_line(
        self, monkeypatch, caplog
    ):
        seams = _Seams(monkeypatch, stored=False)
        caplog.set_level(logging.INFO, logger=email_sender.__name__)

        assert await _execute(seams, _job()) == "prov-7"

        dead = [
            r
            for r in caplog.records
            if "the admission email's link is dead" in r.message
        ]
        assert [r.getMessage() for r in dead] == [
            "send_email job-7: the admission email's link is dead: the spot was"
            " already used"
        ]

    async def test_a_missing_base_is_refused_before_anything_is_spent(
        self, monkeypatch
    ):
        seams = _Seams(monkeypatch)

        with pytest.raises(EmailRefused) as exc:
            await _execute(seams, _job(params={"address": TO}))

        assert exc.value.reason == "template_params_missing"
        assert "claim_base" in str(exc.value)
        assert seams.events == ["draw"]

    async def test_another_template_draws_no_token_and_stores_nothing(
        self, monkeypatch
    ):
        seams = _Seams(monkeypatch)
        job = _job("activation_nudge", {"step": "approval", "link": "https://x/q"})

        assert await _execute(seams, job) == "prov-7"

        assert seams.events == ["render", "open", "debit", "close", "send"]

    async def test_no_log_line_or_refusal_carries_the_address_or_the_token(
        self, monkeypatch, caplog
    ):
        caplog.set_level(logging.DEBUG)
        refusals = []
        for kw in ({}, {"stored": False}, {"budget": "daily_limit"}):
            await _execute(_Seams(monkeypatch, **kw), _job())
        failing = _Seams(monkeypatch, raises=EmailRefused("provider_rejected", "x"))
        with pytest.raises(EmailRefused) as exc:
            await _execute(failing, _job())
        refusals.append(exc.value)
        with pytest.raises(EmailRefused) as exc:
            await _execute(_Seams(monkeypatch), _job(params={"address": TO}))
        refusals.append(exc.value)

        assert caplog.records, "the paths above log"
        for secret in (TO, TOKEN, sessions.token_hash(TOKEN)):
            assert secret not in caplog.text
            for refusal in refusals:
                assert secret not in repr(refusal) and secret not in str(refusal)


class TestSenderFromEnv:
    def test_both_values_produce_a_sender(self):
        s = sender_from_env({"RESEND_API_KEY": "re_k", "EMAIL_FROM": "a@b.com"})
        assert isinstance(s, ResendSender)

    @pytest.mark.parametrize(
        "env",
        [
            {},
            {"RESEND_API_KEY": "re_k"},
            {"EMAIL_FROM": "a@b.com"},
            {"RESEND_API_KEY": "  ", "EMAIL_FROM": "a@b.com"},
            {"RESEND_API_KEY": "re_k", "EMAIL_FROM": ""},
        ],
    )
    def test_anything_less_is_None_rather_than_half_configured(self, env):
        """None is what the registry turns into a parked kind with a reason. A
        half-configured sender would fail at the provider instead of at
        composition, where it is readable."""
        assert sender_from_env(env) is None

    def test_a_caller_names_its_policy(self):
        """The API's sender sends once, on the fast class; the worker's keeps
        the default."""
        env = {"RESEND_API_KEY": "re_k", "EMAIL_FROM": "a@b.com"}
        assert sender_from_env(env).policy is None
        assert (
            sender_from_env(env, policy=INTERACTIVE_POLICY).policy is INTERACTIVE_POLICY
        )


def test_the_interactive_policy_sends_once_to_the_provider_alone():
    """A POST that timed out may still have been accepted, so a second attempt
    could send the invitation twice; one attempt cannot."""
    assert INTERACTIVE_POLICY.max_attempts == 1
    assert INTERACTIVE_POLICY.timeout_class == "fast"
    assert INTERACTIVE_POLICY.allowed_hosts == frozenset({PROVIDER_HOST})


class TestBackoff:
    def test_the_ladder_is_05s(self):
        assert [backoff_seconds(i) for i in range(3)] == list(RETRY_LADDER_SECONDS)

    def test_past_the_end_the_last_rung_repeats(self):
        assert backoff_seconds(99) == RETRY_LADDER_SECONDS[-1]


class TestResendSender:
    async def test_a_send_returns_the_provider_ref(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            seen["body"] = request.read().decode()
            return httpx.Response(200, json={"id": "prov-1"})

        async with _client(handler) as client:
            ref = await _sender(client).send(to="a@b.com", subject="s", body="b")

        assert ref == "prov-1"
        assert seen["url"].startswith(f"https://{PROVIDER_HOST}/")
        assert seen["auth"] == "Bearer re_test_key"
        assert '"a@b.com"' in seen["body"]

    @pytest.mark.parametrize("bad", ["not-an-address", "", None])
    async def test_a_recipient_that_is_not_an_address_never_reaches_the_provider(
        self, bad
    ):
        called = []

        def handler(request):  # pragma: no cover - must not run
            called.append(1)
            return httpx.Response(200, json={"id": "x"})

        async with _client(handler) as client:
            with pytest.raises(EmailRefused) as exc:
                await _sender(client).send(to=bad, subject="s", body="b")
        assert exc.value.reason == "recipient_invalid"
        assert called == []

    async def test_a_provider_rejection_is_named_and_carries_no_body(self):
        """The provider's error body is where a recipient address ends up, and
        this reason reaches logs."""

        def handler(request):
            return httpx.Response(422, json={"message": "leaked@address.com"})

        async with _client(handler) as client:
            with pytest.raises(EmailRefused) as exc:
                await _sender(client).send(to="a@b.com", subject="s", body="b")
        assert exc.value.reason == "provider_rejected"
        assert "leaked@address.com" not in str(exc.value)
        assert "http_422" in str(exc.value)

    async def test_a_success_with_no_id_is_a_broken_contract_not_a_send(self):
        """Reporting it as sent would record delivery for mail we cannot trace."""

        def handler(request):
            return httpx.Response(200, json={"ok": True})

        async with _client(handler) as client:
            with pytest.raises(EmailRefused) as exc:
                await _sender(client).send(to="a@b.com", subject="s", body="b")
        assert exc.value.reason == "provider_response_malformed"

    async def test_the_floor_refuses_a_host_this_module_does_not_declare(self):
        """The policy carries ONE host. If the send URL is ever repointed
        without the allowlist moving with it, the floor stops it here rather
        than the request leaving."""
        from src.services.target import email_sender as mod

        def handler(request):  # pragma: no cover - must not run
            return httpx.Response(200, json={"id": "x"})

        original = mod.SEND_URL
        mod.SEND_URL = "https://evil.example.com/emails"
        try:
            async with _client(handler) as client:
                with pytest.raises(mod.egress.EgressRefused):
                    await _sender(client).send(to="a@b.com", subject="s", body="b")
        finally:
            mod.SEND_URL = original


def test_the_provider_host_is_NOT_added_to_the_shared_allowlist():
    """The decision this pins: this module carries its own one-host policy, and
    `egress.DEFAULT_ALLOWED_HOSTS` — whose comment calls itself "the load-bearing
    control" — is left alone. Widening the shared set would hand
    every other adapter reach to the mail provider for no reason, and the next
    provider swap would leave the old host behind in it."""
    from src.services.target import egress

    assert PROVIDER_HOST not in egress.DEFAULT_ALLOWED_HOSTS
    assert DEFAULT_POLICY.allowed_hosts == frozenset({PROVIDER_HOST})


def test_the_budget_ceiling_stays_under_the_providers_pause():
    """90/day against a free tier that PAUSES at 100/day. The gap is the
    point — a ceiling at or above the pause defers nothing and strands sends."""
    assert BUDGET_LIMIT < 100
