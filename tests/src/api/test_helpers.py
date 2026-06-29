"""Tests for onboarding API route helpers."""

from unittest.mock import patch

import pytest
from fastapi import HTTPException

from src.api.routes.onboarding.helpers import _validate_request, service_error_handler
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
class TestValidateRequestAuthorization:
    """_validate_request tenant authorization — cross-tenant IDOR fix (#511)."""

    def _auth(self, user_info):
        """Patch the auth layer to return a fixed token payload."""
        return patch(
            "src.api.routes.onboarding.helpers._validate_auth",
            return_value=user_info,
        )

    def test_bound_token_matching_chat_authorizes_without_membership(
        self, _authorize_membership_by_default
    ):
        """A token that binds chat_id is authorized by the binding alone."""
        membership_cls = _authorize_membership_by_default
        with self._auth({"user_id": 1, "chat_id": CHAT_ID}):
            result = _validate_request("token", CHAT_ID)
        assert result["user_id"] == 1
        membership_cls.return_value.is_active_member.assert_not_called()

    def test_bound_token_mismatch_is_rejected(self):
        """A bound token replayed against a different chat_id → 403."""
        with self._auth({"user_id": 1, "chat_id": 999}):
            with patch("src.api.routes.onboarding.helpers.auth_monitor"):
                with pytest.raises(HTTPException) as exc:
                    _validate_request("token", CHAT_ID)
        assert exc.value.status_code == 403
        assert exc.value.detail == "Chat ID mismatch"

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
