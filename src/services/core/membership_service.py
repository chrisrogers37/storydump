"""Membership service — tenant-access authorization for the API and Telegram layers."""

from typing import Optional

from src.models.user import ROLE_ADMIN
from src.repositories.chat_settings_repository import ChatSettingsRepository
from src.repositories.membership_repository import MembershipRepository
from src.repositories.user_repository import UserRepository
from src.services.base_service import BaseService


class MembershipService(BaseService):
    """Authorize a Telegram identity against a chat instance (tenant).

    A user's right to act on a chat instance is the existence of an *active*
    ``UserChatMembership`` binding their ``users`` row to the instance's
    ``chat_settings`` row. This service is the single place that resolves a
    raw Telegram identity (user id + chat id) to that binding, so API glue
    never reaches across the repository layer itself.
    """

    def __init__(self):
        super().__init__()
        self.user_repo = UserRepository()
        self.chat_settings_repo = ChatSettingsRepository()
        self.membership_repo = MembershipRepository()

    def is_active_member(
        self, telegram_user_id: Optional[int], telegram_chat_id: Optional[int]
    ) -> bool:
        """Return True iff the Telegram user is an active member of the chat.

        Fail-closed: an absent user id or chat id, an unknown user, a chat
        with no settings row, a missing membership, or an inactive membership
        all return False. Never raises — authorization is a boolean gate.
        """
        if telegram_user_id is None or telegram_chat_id is None:
            return False

        user = self.user_repo.get_by_telegram_id(telegram_user_id)
        if not user:
            return False

        chat_settings = self.chat_settings_repo.get_by_chat_id(telegram_chat_id)
        if not chat_settings:
            return False

        membership = self.membership_repo.get_membership(
            str(user.id), str(chat_settings.id)
        )
        return bool(membership and membership.is_active)

    def is_system_admin(self, telegram_user_id: Optional[int]) -> bool:
        """Return True iff the Telegram user holds the system-level admin role.

        This resolves ``users.role``, the DEPLOYMENT-wide role — deliberately
        not ``UserChatMembership.instance_role``, which is scoped to a single
        instance. The two are separate columns so that the distinction
        survives: an owner of their own instance is not an operator of the
        deployment, and admitting instance owners to a deployment-wide view
        would re-open that view one tenant at a time.

        Fail-closed on the same terms as ``is_active_member``: an absent user
        id, an unknown user, a deactivated user, or any non-admin role returns
        False. A deactivated admin is refused because the role outlives the
        account otherwise. Never raises — authorization is a boolean gate.
        """
        if telegram_user_id is None:
            return False

        user = self.user_repo.get_by_telegram_id(telegram_user_id)
        return bool(user and user.is_active and user.role == ROLE_ADMIN)
