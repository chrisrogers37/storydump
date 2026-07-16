"""Instagram account service - manage connected accounts."""

from typing import Optional, List, Dict, Any
from datetime import datetime

from src.config.settings import settings
from src.services.base_service import BaseService
from src.repositories.instagram_account_repository import InstagramAccountRepository
from src.repositories.chat_settings_repository import ChatSettingsRepository
from src.repositories.token_repository import TokenRepository
from src.models.chat_settings import ChatSettings
from src.models.instagram_account import InstagramAccount
from src.models.user import User
from src.utils.logger import logger
from src.utils.encryption import TokenEncryption


class InstagramAccountService(BaseService):
    """
    Manage Instagram accounts within a deployment.

    Handles:
    - Listing available accounts
    - Adding new accounts (with token storage)
    - Switching active account
    - Account status management

    Separation of concerns:
    - InstagramAccount = Identity (what accounts exist)
    - ApiToken = Credentials (how do we authenticate)
    - ChatSettings = Selection (which account is active)
    """

    def __init__(self):
        super().__init__()
        self.account_repo = InstagramAccountRepository()
        self.settings_repo = ChatSettingsRepository()
        self.token_repo = TokenRepository()
        self.encryption = TokenEncryption()

    def list_accounts(self, include_inactive: bool = False) -> List[InstagramAccount]:
        """
        Get Instagram accounts.

        Args:
            include_inactive: If True, include deactivated accounts

        Returns:
            List of InstagramAccount objects
        """
        if include_inactive:
            return self.account_repo.get_all()
        return self.account_repo.get_all_active()

    def get_account_by_id(self, account_id: str) -> Optional[InstagramAccount]:
        """Get account by UUID."""
        return self.account_repo.get_by_id(account_id)

    def get_account_by_id_prefix(self, id_prefix: str) -> Optional[InstagramAccount]:
        """Get account by ID prefix (for shortened callback data).

        Used when Telegram callback data is too long and we need to use
        shortened UUIDs. Returns the first matching account.

        Args:
            id_prefix: First N characters of a UUID (typically 8)

        Returns:
            InstagramAccount or None if not found
        """
        return self.account_repo.get_by_id_prefix(id_prefix)

    def get_account_by_username(self, username: str) -> Optional[InstagramAccount]:
        """Get account by Instagram username."""
        return self.account_repo.get_by_username(username)

    def get_active_account(self, telegram_chat_id: int) -> Optional[InstagramAccount]:
        """
        Get the currently active account for a chat.

        Args:
            telegram_chat_id: Telegram chat/channel ID

        Returns:
            Active InstagramAccount or None if not set
        """
        settings = self.settings_repo.get_or_create(telegram_chat_id)
        if settings.active_instagram_account_id:
            return self.account_repo.get_by_id(
                str(settings.active_instagram_account_id)
            )
        return None

    def switch_account(
        self, telegram_chat_id: int, account_id: str, user: Optional[User] = None
    ) -> InstagramAccount:
        """
        Switch the active Instagram account.

        Args:
            telegram_chat_id: Chat to update
            account_id: UUID of account to switch to
            user: User performing the switch

        Returns:
            The newly active InstagramAccount

        Raises:
            ValueError: If the account is not owned by the requesting chat,
                not found, or disabled
        """
        with self.track_execution(
            "switch_account",
            user_id=user.id if user else None,
            triggered_by="user",
            input_params={"account_id": account_id},
        ) as run_id:
            # Switching decides which credentials this chat posts with, so
            # the chat must own the account.
            chat_settings = self._require_account_ownership(
                account_id, telegram_chat_id, "switch to"
            )

            account = self.account_repo.get_by_id(account_id)
            if not account:
                raise ValueError(f"Account {account_id} not found")

            if not account.is_active:
                raise ValueError(f"Account '{account.display_name}' is disabled")

            # Get old account for logging (pointer already in hand)
            old_id = chat_settings.active_instagram_account_id
            old_account = self.account_repo.get_by_id(str(old_id)) if old_id else None

            # Update settings
            self.settings_repo.update(
                telegram_chat_id, active_instagram_account_id=account_id
            )

            self.set_result_summary(
                run_id,
                {
                    "old_account": old_account.display_name if old_account else None,
                    "new_account": account.display_name,
                    "changed_by": user.telegram_username if user else "system",
                },
            )

            logger.info(
                f"Switched Instagram account: "
                f"{old_account.display_name if old_account else 'None'} -> {account.display_name}"
            )

            return account

    def add_account(
        self,
        display_name: str,
        instagram_account_id: str,
        instagram_username: str,
        access_token: str,
        token_expires_at: Optional[datetime] = None,
        user: Optional[User] = None,
        set_as_active: bool = False,
        telegram_chat_id: Optional[int] = None,
        auth_method: Optional[str] = None,
        issuing_app_id: Optional[str] = None,
    ) -> InstagramAccount:
        """
        Add a new Instagram account with its token.

        Args:
            display_name: User-friendly name
            instagram_account_id: Meta's account ID (numeric string)
            instagram_username: @username
            access_token: OAuth access token
            token_expires_at: When token expires
            user: User adding the account
            set_as_active: If True, set this as the active account
            telegram_chat_id: Required if set_as_active is True
            auth_method: How account was connected ('oauth', 'manual', or None)
            issuing_app_id: Meta App ID that issued the token; dual-
                written to api_tokens.issuing_app_id alongside
                auth_method so the credential is self-describing
                (#468 dual-write phase).

        Returns:
            Created InstagramAccount

        Raises:
            ValueError: If account already exists or invalid params
        """
        with self.track_execution(
            "add_account",
            user_id=user.id if user else None,
            triggered_by="user",
            input_params={
                "display_name": display_name,
                "instagram_username": instagram_username,
            },
        ) as run_id:
            self._validate_new_account(instagram_account_id, instagram_username)

            account = self._create_account_with_token(
                display_name=display_name,
                instagram_account_id=instagram_account_id,
                instagram_username=instagram_username,
                access_token=access_token,
                token_expires_at=token_expires_at,
                auth_method=auth_method,
                issuing_app_id=issuing_app_id,
            )

            # Optionally set as active
            if set_as_active:
                if not telegram_chat_id:
                    raise ValueError(
                        "telegram_chat_id required when set_as_active=True"
                    )
                self.settings_repo.update(
                    telegram_chat_id, active_instagram_account_id=str(account.id)
                )

            self.set_result_summary(
                run_id,
                {
                    "account_id": str(account.id),
                    "display_name": display_name,
                    "username": instagram_username,
                    "set_as_active": set_as_active,
                },
            )

            logger.info(
                f"Added Instagram account: {display_name} (@{instagram_username})"
            )

            return account

    def _validate_new_account(
        self, instagram_account_id: str, instagram_username: str
    ) -> None:
        """Validate that an account doesn't already exist.

        Raises:
            ValueError: If account already exists by ID or username
        """
        existing = self.get_account_by_meta_id(instagram_account_id)
        if existing:
            raise ValueError(
                f"Account with ID {instagram_account_id} already exists "
                f"as '{existing.display_name}'"
            )

        existing_by_username = self.account_repo.get_by_username(instagram_username)
        if existing_by_username:
            raise ValueError(
                f"Account @{instagram_username} already exists "
                f"as '{existing_by_username.display_name}'"
            )

    def _create_account_with_token(
        self,
        display_name: str,
        instagram_account_id: str,
        instagram_username: str,
        access_token: str,
        token_expires_at: Optional[datetime] = None,
        auth_method: Optional[str] = None,
        issuing_app_id: Optional[str] = None,
    ) -> InstagramAccount:
        """Create account record and store its encrypted token.

        Dual-writes ``auth_method`` and ``issuing_app_id`` to the new
        ``api_tokens`` columns alongside the existing write to
        ``instagram_accounts.auth_method``. After the read-switch
        sub-PR (#468), consumers read these straight off the token
        and the account-side column gets dropped.
        """
        account = self.account_repo.create(
            display_name=display_name,
            instagram_account_id=instagram_account_id,
            instagram_username=instagram_username,
            auth_method=auth_method,
        )

        encrypted_token = self.encryption.encrypt(access_token)
        self.token_repo.create_or_update(
            service_name="instagram",
            token_type="access_token",
            token_value=encrypted_token,
            expires_at=token_expires_at,
            instagram_account_id=str(account.id),
            meta_account_id=instagram_account_id,
            auth_method=auth_method,
            issuing_app_id=issuing_app_id,
            metadata={
                "account_id": instagram_account_id,
                "username": instagram_username,
            },
        )

        return account

    def update_account(
        self,
        account_id: str,
        display_name: Optional[str] = None,
        instagram_username: Optional[str] = None,
        user: Optional[User] = None,
    ) -> InstagramAccount:
        """
        Update an Instagram account's display info.

        Args:
            account_id: UUID of account to update
            display_name: New display name (optional)
            instagram_username: New username (optional)
            user: User performing the update

        Returns:
            Updated InstagramAccount
        """
        with self.track_execution(
            "update_account",
            user_id=user.id if user else None,
            triggered_by="user",
            input_params={"account_id": account_id},
        ) as run_id:
            updates = {}
            if display_name is not None:
                updates["display_name"] = display_name
            if instagram_username is not None:
                updates["instagram_username"] = instagram_username

            if not updates:
                raise ValueError("No updates provided")

            account = self.account_repo.update(account_id, **updates)

            self.set_result_summary(
                run_id, {"account_id": str(account.id), "updates": updates}
            )

            logger.info(f"Updated Instagram account: {account.display_name}")

            return account

    def update_account_token(
        self,
        instagram_account_id: str,
        access_token: str,
        instagram_username: Optional[str] = None,
        token_expires_at: Optional[datetime] = None,
        user: Optional[User] = None,
        set_as_active: bool = False,
        telegram_chat_id: Optional[int] = None,
        auth_method: Optional[str] = None,
        issuing_app_id: Optional[str] = None,
    ) -> InstagramAccount:
        """
        Update the token for an existing Instagram account.

        Use this when re-adding an account that already exists
        (e.g., token expired and user is re-authenticating).

        Args:
            instagram_account_id: Meta's account ID (numeric string)
            access_token: New OAuth access token
            instagram_username: Update username if changed (optional)
            token_expires_at: When new token expires
            user: User performing the update
            set_as_active: If True, set this as the active account
            telegram_chat_id: Required if set_as_active is True
            auth_method: How account was connected ('oauth', 'manual', or None)

        Returns:
            Updated InstagramAccount

        Raises:
            ValueError: If account not found
        """
        with self.track_execution(
            "update_account_token",
            user_id=user.id if user else None,
            triggered_by="user",
            input_params={"instagram_account_id": instagram_account_id},
        ) as run_id:
            # Find existing account. Use the cross-flow helper so reconnects
            # for legacy FB-Login rows resolve via the username branch when
            # their stored Meta-side ID doesn't match the live IG Login one.
            account = self.find_existing_account_for_oauth(
                instagram_account_id, username=instagram_username
            )
            if not account:
                raise ValueError(f"Account with ID {instagram_account_id} not found")

            # Update username if changed. ``auth_method`` is no longer
            # written here — it lives on api_tokens (#468 PR 5;
            # migration 041 dropped the legacy column on
            # instagram_accounts) and is dual-written via the token
            # repo call below.
            update_kwargs = {}
            if instagram_username and instagram_username != account.instagram_username:
                update_kwargs["instagram_username"] = instagram_username
            if update_kwargs:
                account = self.account_repo.update(str(account.id), **update_kwargs)

            # Encrypt and update/create token. Dual-write auth_method and
            # issuing_app_id alongside the existing fields so the token
            # carries its own provenance (#468 dual-write phase).
            encrypted_token = self.encryption.encrypt(access_token)
            self.token_repo.create_or_update(
                service_name="instagram",
                token_type="access_token",
                token_value=encrypted_token,
                expires_at=token_expires_at,
                instagram_account_id=str(account.id),
                meta_account_id=instagram_account_id,
                auth_method=auth_method,
                issuing_app_id=issuing_app_id,
                metadata={
                    "account_id": instagram_account_id,
                    "username": account.instagram_username,
                },
            )

            # Reactivate if was deactivated
            if not account.is_active:
                account = self.account_repo.activate(str(account.id))
                logger.info(f"Reactivated Instagram account: {account.display_name}")

            # Optionally set as active
            if set_as_active:
                if not telegram_chat_id:
                    raise ValueError(
                        "telegram_chat_id required when set_as_active=True"
                    )
                self.settings_repo.update(
                    telegram_chat_id, active_instagram_account_id=str(account.id)
                )

            self.set_result_summary(
                run_id,
                {
                    "account_id": str(account.id),
                    "display_name": account.display_name,
                    "username": account.instagram_username,
                    "token_updated": True,
                },
            )

            logger.info(
                f"Updated token for Instagram account: "
                f"{account.display_name} (@{account.instagram_username})"
            )

            return account

    def find_existing_account_for_oauth(
        self,
        meta_account_id: str,
        username: Optional[str] = None,
    ) -> Optional[InstagramAccount]:
        """Resolve an existing Instagram account for an OAuth refresh.

        Lookup order:
          1. ``api_tokens.meta_account_id`` — credential-keyed, the credential
             refactor's target state (#380, phase 3).
          2. ``instagram_accounts.instagram_account_id`` — legacy column,
             retained until phase 5 drops it.
          3. ``instagram_accounts.instagram_username`` — cross-flow recovery
             for legacy rows whose stored Meta-side identifier doesn't match
             what the current OAuth flow returns. Only consulted when
             ``username`` is provided.

        The username branch is the only path that can succeed when migration
        036's backfill put an FB-Login-era IGSID into ``meta_account_id`` for
        an account whose IG Login ``user_id`` is a different value. Callers
        that hit it should write the new ``meta_account_id`` back to the
        token row so subsequent reconnects resolve via branch 1.
        """
        account = self.account_repo.get_by_meta_account_id(meta_account_id)
        if account:
            return account
        account = self.account_repo.get_by_instagram_id(meta_account_id)
        if account:
            return account
        if username:
            account = self.account_repo.get_by_username(username)
        return account

    def get_account_by_meta_id(
        self, meta_account_id: str
    ) -> Optional[InstagramAccount]:
        """Narrow Meta-side-identifier lookup. Alias for callers that don't
        want the cross-flow username recovery (e.g. ``_validate_new_account``).
        """
        return self.find_existing_account_for_oauth(meta_account_id, username=None)

    def get_account_by_instagram_id(
        self, instagram_account_id: str
    ) -> Optional[InstagramAccount]:
        """Get account by Instagram's numeric ID.

        Deprecated: prefer get_account_by_meta_id which resolves across
        OAuth flows via api_tokens.meta_account_id.
        """
        return self.account_repo.get_by_instagram_id(instagram_account_id)

    def _account_owned_by_chat(self, account_id: str, chat_settings) -> bool:
        """Whether a chat owns an account.

        Ownership is derived (accounts carry no tenant column): a chat owns
        an account when it has the account selected as active, or holds a
        token stamped with its chat_settings_id. Accounts with no
        chat-stamped tokens are legacy single-tenant data and belong to the
        deployment's env chat only.
        """
        active_id = chat_settings.active_instagram_account_id
        if active_id is not None and str(active_id) == str(account_id):
            return True

        stamped = self.token_repo.get_owner_chat_ids(account_id)
        if str(chat_settings.id) in stamped:
            return True
        return (
            not stamped
            and chat_settings.telegram_chat_id == settings.TELEGRAM_CHANNEL_ID
        )

    def _require_account_ownership(
        self, account_id: str, telegram_chat_id: int, action: str
    ) -> ChatSettings:
        """Resolve the caller's settings and reject non-owners.

        Runs before any existence lookup and raises the same "not found"
        shape whether the account exists or not — a foreign probe must not
        be able to distinguish real account ids from invented ones. Every
        tenant-gated account mutation goes through here so the message and
        ordering can't drift apart. Returns the caller's ChatSettings so
        gated methods don't re-fetch it.
        """
        chat_settings = self.settings_repo.get_or_create(telegram_chat_id)
        if not self._account_owned_by_chat(account_id, chat_settings):
            logger.warning(
                f"Chat {telegram_chat_id} attempted to {action} account "
                f"{account_id} it does not own"
            )
            raise ValueError(f"Account {account_id} not found for this chat")
        return chat_settings

    def deactivate_account(
        self, account_id: str, telegram_chat_id: int, user: Optional[User] = None
    ) -> InstagramAccount:
        """
        Soft-delete an account for the chat that owns it.

        The account and its tokens are preserved for audit purposes.
        ``is_active`` is a deployment-wide flag, so the requesting chat
        must own the account (have it selected, or hold a token for it) —
        otherwise one tenant could disable another tenant's account.

        Args:
            account_id: UUID of account to deactivate
            telegram_chat_id: Chat requesting the removal
            user: User performing the action

        Returns:
            Deactivated InstagramAccount

        Raises:
            ValueError: If the account is not owned by the requesting chat
        """
        with self.track_execution(
            "deactivate_account",
            user_id=user.id if user else None,
            triggered_by="user",
            input_params={
                "account_id": account_id,
                "telegram_chat_id": telegram_chat_id,
            },
        ) as run_id:
            self._require_account_ownership(account_id, telegram_chat_id, "deactivate")

            account = self.account_repo.deactivate(account_id)

            self.set_result_summary(
                run_id,
                {"account_id": str(account.id), "display_name": account.display_name},
            )

            logger.info(f"Deactivated Instagram account: {account.display_name}")

            return account

    def reactivate_account(
        self, account_id: str, user: Optional[User] = None
    ) -> InstagramAccount:
        """
        Reactivate a previously deactivated account.

        Args:
            account_id: UUID of account to reactivate
            user: User performing the action

        Returns:
            Reactivated InstagramAccount
        """
        with self.track_execution(
            "reactivate_account",
            user_id=user.id if user else None,
            triggered_by="user",
            input_params={"account_id": account_id},
        ) as run_id:
            account = self.account_repo.activate(account_id)

            self.set_result_summary(
                run_id,
                {"account_id": str(account.id), "display_name": account.display_name},
            )

            logger.info(f"Reactivated Instagram account: {account.display_name}")

            return account

    def count_active_accounts(self) -> int:
        """Count active Instagram accounts (lightweight, single COUNT query)."""
        return self.account_repo.count_active()

    def get_accounts_for_display(self, telegram_chat_id: int) -> Dict[str, Any]:
        """
        Get account info formatted for /settings display.

        Args:
            telegram_chat_id: Chat to get settings for

        Returns:
            Dict with accounts list and active account info
        """
        accounts = self.list_accounts()
        active = self.get_active_account(telegram_chat_id)

        return {
            "accounts": [
                {
                    "id": str(a.id),
                    "display_name": a.display_name,
                    "username": a.instagram_username,
                }
                for a in accounts
            ],
            "active_account_id": str(active.id) if active else None,
            "active_account_name": active.display_name if active else "Not selected",
            "active_account_username": active.instagram_username if active else None,
        }

    def get_token_for_active_account(self, telegram_chat_id: int) -> Optional[str]:
        """
        Get the access token for the currently active account.

        Convenience method for posting services.

        Args:
            telegram_chat_id: Chat to get active account for

        Returns:
            Access token string or None if no active account/token
        """
        active = self.get_active_account(telegram_chat_id)
        if not active:
            return None

        token = self.token_repo.get_token_for_account(
            str(active.id), token_type="access_token"
        )
        return token.token_value if token else None

    def auto_select_account_if_single(
        self, telegram_chat_id: int
    ) -> Optional[InstagramAccount]:
        """
        Auto-select an account if exactly one exists and none is selected.

        Convenience for new deployments.

        Args:
            telegram_chat_id: Chat to check/update

        Returns:
            Auto-selected account, or None if not applicable
        """
        current = self.get_active_account(telegram_chat_id)
        if current:
            return None  # Already has an account selected

        accounts = self.list_accounts()
        if len(accounts) == 1:
            # Auto-select the only account
            self.settings_repo.update(
                telegram_chat_id, active_instagram_account_id=str(accounts[0].id)
            )
            logger.info(f"Auto-selected Instagram account: {accounts[0].display_name}")
            return accounts[0]

        return None
