"""The `EmailSender` port's pure halves and its provider adapter (#1092).

All HTTP is faked with `httpx.MockTransport` — the egress floor runs for real
(policy, host allowlist, byte cap), the network does not. That split matters
here more than usual: the floor is the only thing standing between a template
parameter and an arbitrary host, and a test that stubbed the floor would be
asserting against the wrong object.

The database halves — the budget debit and the over-budget deferral — are in
`tests/scripts/test_email_gate.py`, because neither can be proven without the
`rate_counters` row lock and a real `jobs` row.
"""

from __future__ import annotations

import httpx
import pytest

from src.services.target.email_sender import (
    BUDGET_LIMIT,
    DEFAULT_POLICY,
    PROVIDER_HOST,
    RETRY_LADDER_SECONDS,
    EmailRefused,
    ResendSender,
    backoff_seconds,
    render,
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
    triggered the collection: here, the first test of a DIFFERENT module, which
    read exactly like the known timing flake (#1072). It was not — it
    reproduced 2/2 with these tests present and 0/2 without them. Owning the
    client in the test body keeps creation and close on one loop.
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


class TestRender:
    def test_the_invitation_carries_the_accept_link(self):
        subject, body = render(
            "invitation", {"workspace_name": "Acme", "accept_url": "https://x/join/tok"}
        )
        assert "Acme" in subject
        assert "https://x/join/tok" in body

    def test_the_inviter_is_optional_and_changes_the_opener(self):
        with_name, _ = render(
            "invitation",
            {
                "workspace_name": "Acme",
                "accept_url": "https://x",
                "inviter_name": "Dana",
            },
        )
        without, _ = render(
            "invitation", {"workspace_name": "Acme", "accept_url": "https://x"}
        )
        assert with_name.startswith("Dana has invited you")
        assert without.startswith("You have been invited")

    @pytest.mark.parametrize("bad", ["nope", "", None, 7])
    def test_an_unknown_template_is_refused_by_name(self, bad):
        """A permissive renderer's failure mode is an email that goes out saying
        the wrong thing. There is no generic fallback on purpose."""
        with pytest.raises(EmailRefused) as exc:
            render(bad, {})
        assert exc.value.reason == "unknown_template"

    @pytest.mark.parametrize("missing", ["workspace_name", "accept_url"])
    def test_a_missing_parameter_is_refused_by_name(self, missing):
        params = {"workspace_name": "Acme", "accept_url": "https://x"}
        del params[missing]
        with pytest.raises(EmailRefused) as exc:
            render("invitation", params)
        assert exc.value.reason == "template_params_missing"
        assert missing in str(exc.value)


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
    control until #871 lands" — is left alone. Widening the shared set would hand
    every other adapter reach to the mail provider for no reason, and the next
    provider swap would leave the old host behind in it."""
    from src.services.target import egress

    assert PROVIDER_HOST not in egress.DEFAULT_ALLOWED_HOSTS
    assert DEFAULT_POLICY.allowed_hosts == frozenset({PROVIDER_HOST})


def test_the_budget_ceiling_stays_under_the_providers_pause():
    """`05`: 90/day against a free tier that PAUSES at 100/day. The gap is the
    point — a ceiling at or above the pause defers nothing and strands sends."""
    assert BUDGET_LIMIT < 100
