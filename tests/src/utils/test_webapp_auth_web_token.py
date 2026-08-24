"""#1015 — the web-session credential (generate_web_token / validate_web_token).

The credential a user with no Telegram identity presents. Nothing about it is
Telegram-shaped: platform-neutral UUID subjects, and its own signing secret.
"""

import time
import uuid
from unittest.mock import patch

import pytest

from src.utils import webapp_auth
from src.utils.webapp_auth import (
    WEB_TOKEN_TTL,
    generate_web_token,
    validate_web_token,
)

SECRET = "a-web-token-secret-that-is-not-the-bot-token"


@pytest.fixture
def web_secret():
    with patch.object(webapp_auth.settings, "WEB_TOKEN_SECRET", SECRET, create=True):
        yield


@pytest.fixture
def subjects():
    """A BOUND credential's subjects: user, tenant, nonce."""
    return str(uuid.uuid4()), str(uuid.uuid4()), uuid.uuid4().hex


@pytest.mark.unit
class TestWebToken:
    def test_round_trip_returns_the_subjects_it_was_minted_with(
        self, web_secret, subjects
    ):
        user_uuid, tenant, nonce = subjects
        result = validate_web_token(generate_web_token(user_uuid, tenant, nonce))
        assert result == {
            "user_uuid": user_uuid,
            "chat_settings_id": tenant,
            "nonce": nonce,
        }

    def test_result_carries_no_telegram_shaped_keys(self, web_secret, subjects):
        """The collision that would send a UUID into a BigInteger comparison.

        A Telegram-shaped caller reading `user_id`/`chat_id` off this result
        must get nothing and fail closed, rather than receive a UUID string
        where an integer is expected.
        """
        result = validate_web_token(generate_web_token(*subjects))
        assert "user_id" not in result
        assert "chat_id" not in result

    def test_a_tampered_subject_is_rejected(self, web_secret, subjects):
        user_uuid, tenant, nonce = subjects
        token = generate_web_token(user_uuid, tenant, nonce)
        parts = token.split(".")
        parts[2] = str(uuid.uuid4())  # point it at another tenant
        with pytest.raises(ValueError, match="signature"):
            validate_web_token(".".join(parts))

    def test_an_expired_token_is_rejected(self, web_secret, subjects):
        with patch("time.time", return_value=time.time() - WEB_TOKEN_TTL - 60):
            token = generate_web_token(*subjects)
        with pytest.raises(ValueError, match="expired"):
            validate_web_token(token)

    def test_a_future_dated_token_is_rejected(self, web_secret, subjects):
        with patch("time.time", return_value=time.time() + 3600):
            token = generate_web_token(*subjects)
        with pytest.raises(ValueError, match="future"):
            validate_web_token(token)

    def test_an_unset_secret_refuses_rather_than_signing_with_an_empty_key(
        self, subjects
    ):
        with patch.object(webapp_auth.settings, "WEB_TOKEN_SECRET", None, create=True):
            with pytest.raises(ValueError, match="not configured"):
                generate_web_token(*subjects)
            with pytest.raises(ValueError, match="not configured"):
                validate_web_token("sd1b.a.b.1.c.d")

    def test_an_unbound_credential_omits_the_tenant_key_entirely(self, web_secret):
        """The shape a user between sign-in and first workspace presents.

        The tenant key must be ABSENT, not empty: a caller handed "" and
        passing it on as a tenant id is the failure this shape forecloses.
        """
        user_uuid, _, nonce = str(uuid.uuid4()), None, uuid.uuid4().hex
        result = validate_web_token(generate_web_token(user_uuid, None, nonce))
        assert result == {"user_uuid": user_uuid, "nonce": nonce}
        assert "chat_settings_id" not in result

    def test_a_bound_credential_cannot_be_downgraded_to_unbound(
        self, web_secret, subjects
    ):
        """Stripping the tenant must not yield a valid tenant-less credential.

        The signature covers the prefix, so a bound payload re-presented in the
        unbound shape does not verify -- the tenant cannot be dropped to widen
        what the credential is accepted for.
        """
        token = generate_web_token(*subjects)
        parts = token.split(".")
        stripped = ".".join(["sd1u", parts[1]] + parts[3:])
        with pytest.raises(ValueError):
            validate_web_token(stripped)

    def test_an_unbound_credential_cannot_be_promoted_to_bound(
        self, web_secret, subjects
    ):
        """The inverse, and the one that would matter: forging a tenant."""
        user_uuid, tenant, nonce = subjects
        token = generate_web_token(user_uuid, None, nonce)
        parts = token.split(".")
        promoted = ".".join(["sd1b", parts[1], tenant] + parts[2:])
        with pytest.raises(ValueError):
            validate_web_token(promoted)

    def test_an_empty_tenant_field_is_refused_not_read_as_absent(self, web_secret):
        """A sentinel in the tenant slot must not be mistaken for either shape."""
        with pytest.raises(ValueError):
            generate_web_token(str(uuid.uuid4()), "", uuid.uuid4().hex)

    def test_a_url_token_is_not_accepted_as_a_web_token(self, web_secret):
        """The two formats must be mutually unparseable, not merely different."""
        with pytest.raises(ValueError, match="format"):
            validate_web_token("-1001234567890:42:1700000000:deadbeef")

    def test_a_subject_containing_the_separator_is_refused_at_mint(self, web_secret):
        """A dot in a subject would shift every field one place on parse."""
        with pytest.raises(ValueError, match="user_uuid"):
            generate_web_token("has.a.dot", str(uuid.uuid4()), "n")

    def test_the_nonce_is_carried_but_not_yet_enforced(self, web_secret, subjects):
        """#587's field exists from birth; single-use does NOT.

        Pinned so nobody reads the nonce as replay protection it has not got:
        the same token still validates twice. Adding the store is a validator
        change, never a format migration — which is the whole reason the field
        ships now.
        """
        token = generate_web_token(*subjects)
        assert validate_web_token(token) == validate_web_token(token)
