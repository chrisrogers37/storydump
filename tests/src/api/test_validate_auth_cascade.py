"""#1015 — the three-branch auth cascade and the web-credential refusal.

`_validate_auth` dispatches across initData, URL token and web token, and
`_validate_request` refuses a web credential outright. Both read correct and
neither had a test; rajan's review on #1017 named that, and "reads correct" is
not covered.

The load-bearing one here is `test_a_reused_telegram_dict_is_not_mistaken_for_a
_web_credential`. That bug was hit, fixed and written up in a comment — and a
comment recording a bug with nothing pinning it is the exact shape that
regresses, because the next reader believes the case is handled and has nothing
that fails when it stops being.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from src.api.routes.onboarding import helpers
from src.api.routes.onboarding.helpers import _validate_auth, _validate_request

TG_USER = {"user_id": 12345, "first_name": "Chris"}
TG_BOUND = {"user_id": 12345, "chat_id": -100999, "first_name": "Chris"}
WEB_USER = {"user_uuid": "11111111-1111-1111-1111-111111111111", "nonce": "n"}
TENANT = "cs-test-1"


def _fail(msg):
    return MagicMock(side_effect=ValueError(msg))


@pytest.fixture
def authorized():
    """Tenant resolves and membership passes, so only the cascade is under test."""
    with (
        patch.object(helpers, "SettingsService") as settings_cls,
        patch.object(helpers, "MembershipService") as member_cls,
    ):
        svc = settings_cls.return_value.__enter__.return_value
        svc.resolve_chat_settings_id.return_value = TENANT
        member_cls.return_value.__enter__.return_value.is_active_member.return_value = (
            True
        )
        yield


@pytest.mark.unit
class TestCascadeBranches:
    def test_initdata_wins_and_the_later_branches_are_never_consulted(self):
        with (
            patch.object(helpers, "validate_init_data", return_value=dict(TG_USER)),
            patch.object(helpers, "validate_url_token") as url_tok,
            patch.object(helpers, "validate_web_token") as web_tok,
        ):
            assert _validate_auth("anything")["user_id"] == 12345
            url_tok.assert_not_called()
            web_tok.assert_not_called()

    def test_url_token_is_the_second_branch(self):
        with (
            patch.object(helpers, "validate_init_data", _fail("bad initData")),
            patch.object(helpers, "validate_url_token", return_value=dict(TG_BOUND)),
            patch.object(helpers, "validate_web_token") as web_tok,
        ):
            assert _validate_auth("tok")["chat_id"] == -100999
            web_tok.assert_not_called()

    def test_web_token_is_the_third_branch(self):
        with (
            patch.object(helpers, "validate_init_data", _fail("bad initData")),
            patch.object(helpers, "validate_url_token", _fail("bad urlToken")),
            patch.object(helpers, "validate_web_token", return_value=dict(WEB_USER)),
        ):
            assert _validate_auth("sd1u.…")["user_uuid"] == WEB_USER["user_uuid"]

    def test_all_three_failing_is_a_401_carrying_all_three_reasons(self):
        """Every reason is recorded, never one picked by the shape of the input.

        Whichever validator the caller MEANT, its reason has to be in the
        record — the other two report "invalid format" whatever was actually
        wrong, so keeping only one spells most rejections identically.
        """
        with (
            patch.object(helpers, "validate_init_data", _fail("bad initData")),
            patch.object(helpers, "validate_url_token", _fail("bad urlToken")),
            patch.object(helpers, "validate_web_token", _fail("bad webToken")),
            patch.object(helpers.auth_monitor, "record_failure") as recorded,
        ):
            with pytest.raises(HTTPException) as exc:
                _validate_auth("garbage")
            assert exc.value.status_code == 401
            assert exc.value.detail == helpers.AUTH_FAILURE_DETAIL

            reason = recorded.call_args[0][1]
            for fragment in ("bad initData", "bad urlToken", "bad webToken"):
                assert fragment in reason, f"{fragment!r} missing from {reason!r}"

    def test_the_401_detail_leaks_no_reason_to_the_caller(self):
        """The specific reason is an oracle; it goes to the monitor, not out."""
        with (
            patch.object(helpers, "validate_init_data", _fail("signature mismatch")),
            patch.object(helpers, "validate_url_token", _fail("expired")),
            patch.object(helpers, "validate_web_token", _fail("not configured")),
            patch.object(helpers.auth_monitor, "record_failure"),
        ):
            with pytest.raises(HTTPException) as exc:
                _validate_auth("garbage")
            for leak in ("signature", "expired", "configured"):
                assert leak not in exc.value.detail


@pytest.mark.unit
class TestWebCredentialRefusal:
    def test_a_web_credential_is_refused_403_on_a_chat_keyed_route(self, authorized):
        """Authentic but not routable: handlers are still chat-keyed."""
        with (
            patch.object(helpers, "validate_init_data", _fail("x")),
            patch.object(helpers, "validate_url_token", _fail("x")),
            patch.object(helpers, "validate_web_token", return_value=dict(WEB_USER)),
            patch.object(helpers.auth_monitor, "record_failure") as recorded,
        ):
            with pytest.raises(HTTPException) as exc:
                _validate_request("sd1u.…", -100999)
            assert exc.value.status_code == 403
            assert recorded.call_args[0][1] == "web credential not routable"

    def test_the_refusal_fires_before_the_tenant_is_resolved(self, authorized):
        """It must not resolve a tenant from the request's untrusted chat_id.

        Falling through would authorize against one tenant and serve another,
        which is why this is an explicit refusal and not a happy accident of
        `is_active_member(None, ...)` returning False.
        """
        with (
            patch.object(helpers, "validate_init_data", _fail("x")),
            patch.object(helpers, "validate_url_token", _fail("x")),
            patch.object(helpers, "validate_web_token", return_value=dict(WEB_USER)),
            patch.object(helpers.auth_monitor, "record_failure"),
            patch.object(helpers, "SettingsService") as settings_cls,
        ):
            with pytest.raises(HTTPException):
                _validate_request("sd1u.…", -100999)
            settings_cls.assert_not_called()

    def test_a_reused_telegram_dict_is_not_mistaken_for_a_web_credential(
        self, authorized
    ):
        """THE REGRESSION. Measured on #1017 before the discriminator changed.

        `_validate_request` WRITES `chat_settings_id` into the dict it returns,
        and `validate_init_data`'s result is not always a fresh object — a
        module-level fixture, a cache, any caller reusing one. So a second call
        sees a Telegram result already carrying the tenant key.

        Keyed on `chat_settings_id`, the web refusal fired on that legitimate
        Telegram caller and turned an oauth-route 400 into a 403. Keyed on
        `user_uuid` — which only `validate_web_token` produces and
        `_validate_request` never writes — it cannot be back-contaminated.

        The reuse is the whole mechanism, so the mock returns ONE dict object
        across both calls rather than two equal ones.
        """
        shared = dict(TG_USER)  # the same object both times, as the bug required
        with (
            patch.object(helpers, "validate_init_data", return_value=shared),
            patch.object(helpers, "validate_url_token") as url_tok,
            patch.object(helpers, "validate_web_token") as web_tok,
        ):
            first = _validate_request("init", -100999)
            assert first["chat_settings_id"] == TENANT
            # The first call mutated the shared dict — that is the precondition.
            assert "chat_settings_id" in shared

            second = _validate_request("init", -100999)
            assert second["chat_settings_id"] == TENANT

            url_tok.assert_not_called()
            web_tok.assert_not_called()
