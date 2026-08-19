"""Instagram account repository - CRUD for connected accounts."""

from typing import Optional, List
from datetime import datetime

from sqlalchemy import and_, exists, or_

from src.repositories.base_repository import BaseRepository
from src.models.api_token import ApiToken
from src.models.instagram_account import InstagramAccount


class InstagramAccountRepository(BaseRepository):
    """Repository for InstagramAccount CRUD operations."""

    def get_owned(
        self,
        chat_settings_id: str,
        active_account_id: Optional[str],
        include_unstamped_legacy: bool,
        include_inactive: bool = False,
    ) -> List[InstagramAccount]:
        """Accounts the tenant OWNS — the same derivation the mutation door
        enforces (#891): the chat's active pointer, any account holding a
        token stamped with this chat_settings_id, and — for the deployment's
        env chat only (`include_unstamped_legacy`) — accounts with no
        chat-stamped tokens at all (legacy single-tenant data).

        `instagram_accounts` carries no tenant column, so ownership is set
        membership under this derivation rather than a WHERE on a column;
        the read and the write sides must answer it identically or the list
        becomes the existence oracle the mutation door refuses to be. The
        agreement test pins the two together.
        """
        stamped_for_chat = exists().where(
            and_(
                ApiToken.instagram_account_id == InstagramAccount.id,
                ApiToken.chat_settings_id == chat_settings_id,
            )
        )
        stamped_for_anyone = exists().where(
            and_(
                ApiToken.instagram_account_id == InstagramAccount.id,
                ApiToken.chat_settings_id.isnot(None),
            )
        )
        ownership = [stamped_for_chat]
        if active_account_id is not None:
            ownership.append(InstagramAccount.id == active_account_id)
        predicate = or_(*ownership)
        if include_unstamped_legacy:
            predicate = or_(predicate, ~stamped_for_anyone)

        query = self.db.query(InstagramAccount).filter(predicate)
        if not include_inactive:
            query = query.filter(InstagramAccount.is_active)
        result = query.order_by(InstagramAccount.display_name).all()
        self.end_read_transaction()
        return result

    def get_all_active(self) -> List[InstagramAccount]:
        """Get all active Instagram accounts — DEPLOYMENT-WIDE, no tenant
        scope. Operator surfaces only (#891): a tenant-facing caller wants
        `get_owned`."""
        result = (
            self.db.query(InstagramAccount)
            .filter(InstagramAccount.is_active)
            .order_by(InstagramAccount.display_name)
            .all()
        )
        self.end_read_transaction()
        return result

    def get_all(self) -> List[InstagramAccount]:
        """Get all Instagram accounts (including inactive)."""
        result = (
            self.db.query(InstagramAccount)
            .order_by(InstagramAccount.display_name)
            .all()
        )
        self.end_read_transaction()
        return result

    def get_by_id(self, account_id: str) -> Optional[InstagramAccount]:
        """Get account by UUID."""
        result = (
            self.db.query(InstagramAccount)
            .filter(InstagramAccount.id == account_id)
            .first()
        )
        self.end_read_transaction()
        return result

    def get_by_id_prefix(self, id_prefix: str) -> Optional[InstagramAccount]:
        """Get account by ID prefix (for shortened callback data).

        Used when Telegram callback data is too long and we need to use
        shortened UUIDs. Returns the first matching account.

        Args:
            id_prefix: First N characters of a UUID (typically 8)

        Returns:
            InstagramAccount or None if not found
        """
        from sqlalchemy import cast, String

        result = (
            self.db.query(InstagramAccount)
            .filter(cast(InstagramAccount.id, String).like(f"{id_prefix}%"))
            .first()
        )
        self.end_read_transaction()
        return result

    def get_by_instagram_id(
        self, instagram_account_id: str
    ) -> Optional[InstagramAccount]:
        """Get account by Instagram's account ID."""
        result = (
            self.db.query(InstagramAccount)
            .filter(InstagramAccount.instagram_account_id == instagram_account_id)
            .first()
        )
        self.end_read_transaction()
        return result

    def get_by_meta_account_id(
        self, meta_account_id: str
    ) -> Optional[InstagramAccount]:
        """Find an InstagramAccount via any of its api_tokens.meta_account_id.

        This resolves across OAuth flows: both the Business Account ID
        (FB Login) and the Instagram User ID (IG Login) land in
        api_tokens.meta_account_id, so a single lookup finds the account
        regardless of which flow originally connected it.
        """
        from src.models.api_token import ApiToken

        result = (
            self.db.query(InstagramAccount)
            .join(ApiToken, ApiToken.instagram_account_id == InstagramAccount.id)
            .filter(
                ApiToken.meta_account_id == meta_account_id,
                ApiToken.revoked_at.is_(None),
            )
            .first()
        )
        self.end_read_transaction()
        return result

    def get_by_username(self, username: str) -> Optional[InstagramAccount]:
        """Get account by Instagram username."""
        # Strip @ if present
        username = username.lstrip("@")
        result = (
            self.db.query(InstagramAccount)
            .filter(InstagramAccount.instagram_username == username)
            .first()
        )
        self.end_read_transaction()
        return result

    def create(
        self,
        display_name: str,
        instagram_account_id: str,
        instagram_username: Optional[str] = None,
        auth_method: Optional[str] = None,  # noqa: ARG002 — kept for back-compat
    ) -> InstagramAccount:
        """Create a new Instagram account record.

        ``auth_method`` parameter is accepted for back-compat with
        callers that haven't been updated to drop it but is ignored —
        provenance now lives on ``api_tokens.auth_method`` and is
        written via ``TokenRepository.create_or_update``. Migration
        041 dropped the legacy column.
        """
        # Strip @ if present in username
        if instagram_username:
            instagram_username = instagram_username.lstrip("@")

        account = InstagramAccount(
            display_name=display_name,
            instagram_account_id=instagram_account_id,
            instagram_username=instagram_username,
        )
        self.db.add(account)
        self.db.commit()
        self.db.refresh(account)
        return account

    def update(self, account_id: str, **kwargs) -> InstagramAccount:
        """Update an Instagram account."""
        account = (
            self.db.query(InstagramAccount)
            .filter(InstagramAccount.id == account_id)
            .first()
        )

        if not account:
            raise ValueError(f"Account {account_id} not found")

        for key, value in kwargs.items():
            if hasattr(account, key):
                # Strip @ from username if updating
                if key == "instagram_username" and value:
                    value = value.lstrip("@")
                setattr(account, key, value)

        account.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(account)
        return account

    def deactivate(self, account_id: str) -> InstagramAccount:
        """Soft-delete an account by marking inactive."""
        return self.update(account_id, is_active=False)

    def activate(self, account_id: str) -> InstagramAccount:
        """Re-activate a previously deactivated account."""
        return self.update(account_id, is_active=True)

    def count_active(self) -> int:
        """Count active Instagram accounts."""
        result = (
            self.db.query(InstagramAccount).filter(InstagramAccount.is_active).count()
        )
        self.end_read_transaction()
        return result
