"""Tests for MembershipService — tenant-access authorization (#511)."""

from unittest.mock import Mock, patch

import pytest

from src.services.core.membership_service import MembershipService


@pytest.fixture
def service():
    """MembershipService with all repositories mocked (no DB)."""
    with (
        patch("src.services.core.membership_service.UserRepository"),
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
        assert service.is_active_member(123, "cs-1") is False
        service.membership_repo.get_membership.assert_not_called()

    # The unknown-CHAT case no longer exists here (#842): the predicate is
    # keyed by the RESOLVED tenant id, and a chat with no tenant is refused
    # at the caller's boundary before membership is ever asked.

    def test_missing_membership_returns_false(self, service):
        service.user_repo.get_by_telegram_id.return_value = Mock(id="u-1")
        service.membership_repo.get_membership.return_value = None
        assert service.is_active_member(123, "cs-1") is False

    def test_inactive_membership_returns_false(self, service):
        service.user_repo.get_by_telegram_id.return_value = Mock(id="u-1")
        service.membership_repo.get_membership.return_value = Mock(is_active=False)
        assert service.is_active_member(123, "cs-1") is False

    def test_active_membership_returns_true(self, service):
        service.user_repo.get_by_telegram_id.return_value = Mock(id="u-1")
        service.membership_repo.get_membership.return_value = Mock(is_active=True)
        assert service.is_active_member(123, "cs-1") is True
        service.membership_repo.get_membership.assert_called_once_with(
            "u-1", chat_settings_id="cs-1"
        )


@pytest.mark.unit
class TestIsSystemAdmin:
    """#667. The system-level role, resolved fail-closed.

    Each negative below is a separate way the gate can be wrong, and each
    would let a different population through, so none of them share an
    assertion.
    """

    def test_an_admin_is_an_admin(self, service):
        """The control. Without it every assertion below is satisfied by a
        method that returns False unconditionally."""
        service.user_repo.get_by_telegram_id.return_value = Mock(
            role="admin", is_active=True
        )
        assert service.is_system_admin(123) is True

    def test_a_member_is_not(self, service):
        service.user_repo.get_by_telegram_id.return_value = Mock(
            role="member", is_active=True
        )
        assert service.is_system_admin(123) is False

    def test_a_deactivated_admin_is_not(self, service):
        """The role outlives the account otherwise: deactivating a departed
        operator would revoke nothing on any admin-gated route."""
        service.user_repo.get_by_telegram_id.return_value = Mock(
            role="admin", is_active=False
        )
        assert service.is_system_admin(123) is False

    def test_an_unknown_user_is_not(self, service):
        service.user_repo.get_by_telegram_id.return_value = None
        assert service.is_system_admin(123) is False

    def test_an_absent_user_id_is_not(self, service):
        """A token carrying no user_id must not reach the repository at all —
        get_by_telegram_id(None) is a query whose answer we should never be
        in a position to trust."""
        assert service.is_system_admin(None) is False
        service.user_repo.get_by_telegram_id.assert_not_called()

    def test_an_instance_role_is_not_a_system_role(self, service):
        """The distinction the two columns exist to keep.

        'owner' is a legitimate UserChatMembership.instance_role — it makes
        someone the owner of their own instance. It is not a users.role value
        at all, and must not admit anyone to a deployment-wide view.
        """
        service.user_repo.get_by_telegram_id.return_value = Mock(
            role="owner", is_active=True
        )
        assert service.is_system_admin(123) is False
