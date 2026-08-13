"""Tests for onboarding API route helpers."""

from unittest.mock import patch

import pytest
from fastapi import HTTPException

from src.api.routes.onboarding.helpers import (
    _validate_admin,
    _validate_request,
    service_error_handler,
)
from tests.src.api.conftest import CHAT_ID


class TestServiceErrorHandler:
    """Tests for the service_error_handler context manager."""

    def test_passes_through_on_success(self):
        """Normal execution passes through unchanged."""
        with service_error_handler():
            result = "success"
        assert result == "success"

    def test_converts_value_error_to_http_400(self):
        """ValueError is converted to HTTPException 400."""
        with pytest.raises(HTTPException) as exc_info:
            with service_error_handler():
                raise ValueError("Invalid input")
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "Invalid input"

    def test_propagates_non_value_errors(self):
        """Non-ValueError exceptions propagate unchanged."""
        with pytest.raises(RuntimeError):
            with service_error_handler():
                raise RuntimeError("Something else")


@pytest.mark.unit
class TestValidateAdminAuthorization:
    """#667. ``_validate_admin`` — the system-operator gate.

    Separate from ``_validate_request``: that one asks whether a caller may
    act on ONE instance, this one whether they may see the deployment.
    """

    def _auth(self, user_info):
        return patch(
            "src.api.routes.onboarding.helpers._validate_auth",
            return_value=user_info,
        )

    def test_a_system_admin_passes(self, _authorize_membership_by_default):
        membership_cls = _authorize_membership_by_default
        membership_cls.return_value.is_system_admin.return_value = True

        with self._auth({"user_id": 1}):
            result = _validate_admin("token")

        assert result["user_id"] == 1
        membership_cls.return_value.is_system_admin.assert_called_once_with(1)

    def test_a_non_admin_is_refused_403(self, _authorize_membership_by_default):
        """Authentication succeeded — this is authorization, so 403 not 401.
        The distinction is what tells an operator reading the logs apart from
        a bad credential."""
        membership_cls = _authorize_membership_by_default
        membership_cls.return_value.is_system_admin.return_value = False

        with self._auth({"user_id": 1}):
            with pytest.raises(HTTPException) as exc:
                _validate_admin("token")

        assert exc.value.status_code == 403

    def test_the_refusal_is_recorded_for_monitoring(
        self, _authorize_membership_by_default
    ):
        """A refused operator probe is exactly the traffic auth_monitor
        exists to surface; a silent 403 is invisible to it."""
        membership_cls = _authorize_membership_by_default
        membership_cls.return_value.is_system_admin.return_value = False

        with self._auth({"user_id": 1}):
            with patch("src.api.routes.onboarding.helpers.auth_monitor") as monitor:
                with pytest.raises(HTTPException):
                    _validate_admin("token")

        assert monitor.record_failure.called, "the refusal was not recorded"

    def test_an_auth_failure_still_surfaces_as_401(
        self, _authorize_membership_by_default
    ):
        """The role check must not convert a bad credential into a 403 — the
        gate runs after authentication, not instead of it."""
        membership_cls = _authorize_membership_by_default
        membership_cls.return_value.is_system_admin.return_value = True

        with patch(
            "src.api.routes.onboarding.helpers._validate_auth",
            side_effect=HTTPException(status_code=401, detail="bad token"),
        ):
            with pytest.raises(HTTPException) as exc:
                _validate_admin("token")

        assert exc.value.status_code == 401
        membership_cls.return_value.is_system_admin.assert_not_called()


@pytest.mark.unit
class TestValidateRequestAuthorization:
    """_validate_request tenant authorization.

    Invariant: authorization is a server-side *active membership* for
    ``(user, chat_id)`` — the token alone never authorizes. A bound token's
    cryptographic binding fixes *which* chat the request targets (reuse against
    another chat is rejected up front), but a still-valid token can outlive the
    user's right to act on that chat — a revoked member, or a group member who
    was never provisioned, keeps a usable token until TTL expiry. So every path
    re-checks active membership before granting access.
    """

    def _auth(self, user_info):
        """Patch the auth layer to return a fixed token payload."""
        return patch(
            "src.api.routes.onboarding.helpers._validate_auth",
            return_value=user_info,
        )

    # --- Bound token (initData launched from a group, or a signed URL token) ---

    def test_bound_token_active_member_passes(self, _authorize_membership_by_default):
        """A bound token whose user is an active member is authorized."""
        membership_cls = _authorize_membership_by_default
        membership_cls.return_value.is_active_member.return_value = True
        with self._auth({"user_id": 1, "chat_id": CHAT_ID}):
            result = _validate_request("token", CHAT_ID)
        assert result["user_id"] == 1
        membership_cls.return_value.is_active_member.assert_called_once_with(1, CHAT_ID)

    def test_bound_token_non_member_is_rejected(self, _authorize_membership_by_default):
        """A bound token for a chat the user is not a member of → 403.

        The binding proves the user once belonged to this chat; it does not
        prove they still may act on it. Closing this stale-access hole is the
        whole point — the token alone is no longer sufficient.
        """
        membership_cls = _authorize_membership_by_default
        membership_cls.return_value.is_active_member.return_value = False
        with self._auth({"user_id": 1, "chat_id": CHAT_ID}):
            with patch("src.api.routes.onboarding.helpers.auth_monitor"):
                with pytest.raises(HTTPException) as exc:
                    _validate_request("token", CHAT_ID)
        assert exc.value.status_code == 403
        assert "member" in exc.value.detail.lower()
        membership_cls.return_value.is_active_member.assert_called_once_with(1, CHAT_ID)

    def test_bound_token_revoked_member_is_rejected(
        self, _authorize_membership_by_default
    ):
        """A revoked member keeps a valid bound token but is denied.

        Revocation deactivates the ``UserChatMembership`` row, so
        ``is_active_member`` returns False and access is refused even though the
        token still cryptographically binds this chat.
        """
        membership_cls = _authorize_membership_by_default
        membership_cls.return_value.is_active_member.return_value = False
        with self._auth({"user_id": 7, "chat_id": CHAT_ID}):
            with patch("src.api.routes.onboarding.helpers.auth_monitor"):
                with pytest.raises(HTTPException) as exc:
                    _validate_request("token", CHAT_ID)
        assert exc.value.status_code == 403

    def test_bound_token_mismatch_rejected_before_membership(
        self, _authorize_membership_by_default
    ):
        """A bound token replayed against a different chat_id → 403, and the
        membership lookup is never reached (the binding rejection fires first)."""
        membership_cls = _authorize_membership_by_default
        with self._auth({"user_id": 1, "chat_id": 999}):
            with patch("src.api.routes.onboarding.helpers.auth_monitor"):
                with pytest.raises(HTTPException) as exc:
                    _validate_request("token", CHAT_ID)
        assert exc.value.status_code == 403
        assert exc.value.detail == "Chat ID mismatch"
        membership_cls.return_value.is_active_member.assert_not_called()

    # --- Unbound token (initData from a DM; request chat_id is untrusted) ---

    def test_unbound_token_active_member_passes(self, _authorize_membership_by_default):
        """DM-launched (unbound) token + active membership is authorized."""
        membership_cls = _authorize_membership_by_default
        membership_cls.return_value.is_active_member.return_value = True
        with self._auth({"user_id": 1}):
            result = _validate_request("token", CHAT_ID)
        assert result["user_id"] == 1
        membership_cls.return_value.is_active_member.assert_called_once_with(1, CHAT_ID)

    def test_unbound_token_non_member_is_rejected(
        self, _authorize_membership_by_default
    ):
        """DM-launched token for a chat the user isn't a member of → 403."""
        membership_cls = _authorize_membership_by_default
        membership_cls.return_value.is_active_member.return_value = False
        with self._auth({"user_id": 1}):
            with patch("src.api.routes.onboarding.helpers.auth_monitor"):
                with pytest.raises(HTTPException) as exc:
                    _validate_request("token", CHAT_ID)
        assert exc.value.status_code == 403
        assert "member" in exc.value.detail.lower()

    def test_unbound_token_unknown_user_fails_closed(
        self, _authorize_membership_by_default
    ):
        """A token with no user_id fails closed (membership denies)."""
        membership_cls = _authorize_membership_by_default
        membership_cls.return_value.is_active_member.return_value = False
        with self._auth({"user_id": None}):
            with patch("src.api.routes.onboarding.helpers.auth_monitor"):
                with pytest.raises(HTTPException) as exc:
                    _validate_request("token", CHAT_ID)
        assert exc.value.status_code == 403
