"""Tests for MembershipService — tenant-access authorization (#511)."""

from unittest.mock import Mock, patch

import pytest

from src.services.core.membership_service import MembershipService


@pytest.fixture
def service():
    """MembershipService with all repositories mocked (no DB)."""
    with (
        patch("src.services.core.membership_service.UserRepository"),
        patch("src.services.core.membership_service.ChatSettingsRepository"),
        patch("src.services.core.membership_service.MembershipRepository"),
        patch("src.services.base_service.ServiceRunRepository"),
    ):
        svc = MembershipService()
    svc.user_repo = Mock()
    svc.chat_settings_repo = Mock()
    svc.membership_repo = Mock()
    return svc


@pytest.mark.unit
class TestIsActiveMember:
    """is_active_member fails closed at every missing link in the chain."""

    def test_none_user_id_returns_false(self, service):
        assert service.is_active_member(None, -100) is False
        service.user_repo.get_by_telegram_id.assert_not_called()

    def test_none_chat_id_returns_false(self, service):
        assert service.is_active_member(123, None) is False
        service.user_repo.get_by_telegram_id.assert_not_called()

    def test_unknown_user_returns_false(self, service):
        service.user_repo.get_by_telegram_id.return_value = None
        assert service.is_active_member(123, -100) is False
        service.chat_settings_repo.get_by_chat_id.assert_not_called()

    def test_unbootstrapped_chat_returns_false(self, service):
        service.user_repo.get_by_telegram_id.return_value = Mock(id="u-1")
        service.chat_settings_repo.get_by_chat_id.return_value = None
        assert service.is_active_member(123, -100) is False
        service.membership_repo.get_membership.assert_not_called()

    def test_missing_membership_returns_false(self, service):
        service.user_repo.get_by_telegram_id.return_value = Mock(id="u-1")
        service.chat_settings_repo.get_by_chat_id.return_value = Mock(id="cs-1")
        service.membership_repo.get_membership.return_value = None
        assert service.is_active_member(123, -100) is False

    def test_inactive_membership_returns_false(self, service):
        service.user_repo.get_by_telegram_id.return_value = Mock(id="u-1")
        service.chat_settings_repo.get_by_chat_id.return_value = Mock(id="cs-1")
        service.membership_repo.get_membership.return_value = Mock(is_active=False)
        assert service.is_active_member(123, -100) is False

    def test_active_membership_returns_true(self, service):
        service.user_repo.get_by_telegram_id.return_value = Mock(id="u-1")
        service.chat_settings_repo.get_by_chat_id.return_value = Mock(id="cs-1")
        service.membership_repo.get_membership.return_value = Mock(is_active=True)
        assert service.is_active_member(123, -100) is True
        service.membership_repo.get_membership.assert_called_once_with("u-1", "cs-1")
