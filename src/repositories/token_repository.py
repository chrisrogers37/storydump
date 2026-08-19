"""Token repository - CRUD operations for API tokens."""

from typing import Optional, List, Set
from datetime import datetime, timedelta, timezone

from src.repositories.base_repository import BaseRepository
from src.repositories.tenant_scope import (
    TenantScope,
    require_tenant_context,
    tenant_value,
)
from src.models.api_token import ApiToken


class TokenRepository(BaseRepository):
    """Repository for ApiToken CRUD operations."""

    def __init__(self):
        super().__init__()

    def get_token(
        self,
        service_name: str,
        token_type: str = "access_token",
        instagram_account_id: Optional[str] = None,
    ) -> Optional[ApiToken]:
        """
        Get active (non-revoked) token by service name, type, and optionally account.

        Args:
            service_name: Service identifier (e.g., 'instagram')
            token_type: Token type (e.g., 'access_token', 'refresh_token')
            instagram_account_id: UUID of Instagram account (for multi-account support)

        Returns:
            ApiToken or None if not found
        """
        query = self.db.query(ApiToken).filter(
            ApiToken.service_name == service_name,
            ApiToken.token_type == token_type,
            ApiToken.revoked_at.is_(None),
        )

        # Filter by account if specified
        if instagram_account_id:
            query = query.filter(ApiToken.instagram_account_id == instagram_account_id)
        else:
            # For backward compatibility, match tokens without account ID
            query = query.filter(ApiToken.instagram_account_id.is_(None))

        result = query.first()
        self.end_read_transaction()
        return result

    def get_token_for_update(
        self,
        service_name: str,
        token_type: str = "access_token",
        instagram_account_id: Optional[str] = None,
    ) -> Optional[ApiToken]:
        """
        Get token with a row-level lock for safe concurrent refresh.

        Uses SELECT ... FOR UPDATE SKIP LOCKED so concurrent processes
        won't block or clobber each other's refresh. If another process
        already holds the lock, returns None (skip).

        The caller MUST commit or rollback to release the lock.

        Args:
            service_name: Service identifier (e.g., 'instagram')
            token_type: Token type (e.g., 'access_token')
            instagram_account_id: UUID of Instagram account

        Returns:
            Locked ApiToken, or None if not found or already locked by another process
        """
        query = self.db.query(ApiToken).filter(
            ApiToken.service_name == service_name,
            ApiToken.token_type == token_type,
            ApiToken.revoked_at.is_(None),
        )

        if instagram_account_id:
            query = query.filter(ApiToken.instagram_account_id == instagram_account_id)
        else:
            query = query.filter(ApiToken.instagram_account_id.is_(None))

        return query.with_for_update(skip_locked=True).first()

    def get_token_for_account(
        self,
        instagram_account_id: str,
        token_type: str = "access_token",
        auth_method: Optional[str] = None,
    ) -> Optional[ApiToken]:
        """
        Get active Instagram token for a specific account.

        Convenience method for multi-account Instagram support.

        Args:
            instagram_account_id: UUID of Instagram account
            token_type: Token type (default: 'access_token')
            auth_method: Optional OAuth-flow filter. After migration 040
                an account can hold multiple tokens (one per flow), so
                consumer services that only handle one flow should pass
                their required auth_method to disambiguate.  Omitting
                is back-compatible — returns the first matching token
                regardless of flow (matches pre-refactor behavior).

        Returns:
            ApiToken or None if not found
        """
        query = self.db.query(ApiToken).filter(
            ApiToken.service_name == "instagram",
            ApiToken.token_type == token_type,
            ApiToken.instagram_account_id == instagram_account_id,
            ApiToken.revoked_at.is_(None),
        )
        if auth_method is not None:
            query = query.filter(ApiToken.auth_method == auth_method)
        result = query.first()
        self.end_read_transaction()
        return result

    def get_owner_chat_ids(self, instagram_account_id: str) -> Set[str]:
        """Distinct chat_settings_id values across an account's tokens.

        Unstamped (legacy single-tenant) tokens are excluded, so an empty
        set means the account has no chat-stamped tokens — either no
        tokens at all, or only legacy ones. Revoked tokens still count:
        revocation changes credentials, not which chat owns the account.

        Args:
            instagram_account_id: UUID of Instagram account

        Returns:
            Set of chat_settings_id strings
        """
        rows = (
            self.db.query(ApiToken.chat_settings_id)
            .filter(
                ApiToken.instagram_account_id == instagram_account_id,
                ApiToken.chat_settings_id.isnot(None),
            )
            .distinct()
            .all()
        )
        self.end_read_transaction()
        return {str(row[0]) for row in rows}

    def get_all_instagram_tokens(
        self, token_type: str = "access_token"
    ) -> List[ApiToken]:
        """
        Get all active Instagram tokens (for token refresh iteration).

        Args:
            token_type: Token type (default: 'access_token')

        Returns:
            List of ApiToken for all Instagram accounts
        """
        result = (
            self.db.query(ApiToken)
            .filter(
                ApiToken.service_name == "instagram",
                ApiToken.token_type == token_type,
                ApiToken.revoked_at.is_(None),
            )
            .all()
        )
        self.end_read_transaction()
        return result

    def create_or_update(
        self,
        service_name: str,
        token_type: str,
        token_value: str,
        issued_at: Optional[datetime] = None,
        expires_at: Optional[datetime] = None,
        scopes: Optional[List[str]] = None,
        metadata: Optional[dict] = None,
        instagram_account_id: Optional[str] = None,
        meta_account_id: Optional[str] = None,
        auth_method: Optional[str] = None,
        issuing_app_id: Optional[str] = None,
        *,
        chat_settings_id: TenantScope,
    ) -> ApiToken:
        """
        Create or update a token (UPSERT pattern).

        If a token exists for the service/type/account combination, it's updated.
        Otherwise, a new token is created. Includes revoked tokens in the lookup
        to avoid unique constraint violations on re-issue — revoked_at is cleared
        when a token is re-issued.

        Args:
            service_name: Service identifier
            token_type: Token type
            token_value: Encrypted token value
            issued_at: When the token was issued (defaults to now)
            expires_at: When the token expires (None = never)
            scopes: OAuth scopes granted
            metadata: Additional service-specific data
            instagram_account_id: UUID of Instagram account (for multi-account support)
            meta_account_id: Meta-side identifier that issued this token
                (Business Account ID for FB Login, User ID for IG Login)
            auth_method: OAuth flow that issued this token
                ('instagram_login', 'fb_login', 'manual'). Lives on the
                credential rather than the account so the token is
                self-describing (#468).
            issuing_app_id: Meta App ID that issued this token; stored
                explicitly to eliminate env-var drift as a failure mode.
            chat_settings_id: Owning tenant of this credential — the chat
                that connected the account. Only set when provided, so a
                chat-less write (e.g. token refresh) never clears an
                existing stamp. Unstamped rows are the documented legacy
                single-tenant case (owned by the env chat).

        Returns:
            Created or updated ApiToken
        """
        require_tenant_context(chat_settings_id, where="token.create_or_update")
        if issued_at is None:
            issued_at = datetime.utcnow()

        # Query WITHOUT revoked_at filter — must find the row even if revoked,
        # otherwise INSERT hits the unique constraint.
        query = self.db.query(ApiToken).filter(
            ApiToken.service_name == service_name,
            ApiToken.token_type == token_type,
        )
        if instagram_account_id:
            query = query.filter(ApiToken.instagram_account_id == instagram_account_id)
        else:
            query = query.filter(ApiToken.instagram_account_id.is_(None))
        existing = query.first()

        if existing:
            # Update existing token (clears revocation on re-issue)
            existing.token_value = token_value
            existing.issued_at = issued_at
            existing.expires_at = expires_at
            existing.last_refreshed_at = datetime.utcnow()
            existing.revoked_at = None
            if scopes is not None:
                existing.scopes = scopes
            if metadata is not None:
                existing.token_metadata = metadata
            if meta_account_id is not None:
                existing.meta_account_id = meta_account_id
            if auth_method is not None:
                existing.auth_method = auth_method
            if issuing_app_id is not None:
                existing.issuing_app_id = issuing_app_id
            if chat_settings_id:
                existing.chat_settings_id = chat_settings_id
            self.db.commit()
            self.db.refresh(existing)
            return existing
        else:
            # Create new token
            token = ApiToken(
                service_name=service_name,
                token_type=token_type,
                token_value=token_value,
                issued_at=issued_at,
                expires_at=expires_at,
                scopes=scopes,
                token_metadata=metadata,
                instagram_account_id=instagram_account_id,
                meta_account_id=meta_account_id,
                auth_method=auth_method,
                issuing_app_id=issuing_app_id,
                chat_settings_id=tenant_value(chat_settings_id),
            )
            self.db.add(token)
            self.db.commit()
            self.db.refresh(token)
            return token

    def delete_token(self, service_name: str, token_type: str) -> bool:
        """Delete a token by service name and type.

        Returns:
            True if a token was deleted, False if not found.
        """
        token = self.get_token(service_name, token_type)
        if not token:
            return False
        self.db.delete(token)
        self.db.commit()
        return True

    def update_last_refreshed(self, service_name: str, token_type: str) -> bool:
        """Update the last_refreshed_at timestamp."""
        token = self.get_token(service_name, token_type)
        if token:
            token.last_refreshed_at = datetime.utcnow()
            self.db.commit()
            return True
        return False

    def get_token_for_chat(
        self,
        service_name: str,
        token_type: str,
        chat_settings_id: str,
    ) -> Optional[ApiToken]:
        """Get active (non-revoked) token scoped to a specific tenant (chat).

        Args:
            service_name: Service identifier (e.g., 'google_drive')
            token_type: Token type (e.g., 'oauth_access', 'oauth_refresh')
            chat_settings_id: UUID of the chat_settings record

        Returns:
            ApiToken or None if not found
        """
        require_tenant_context(chat_settings_id, where="token.get_token_for_chat")
        result = (
            self.db.query(ApiToken)
            .filter(
                ApiToken.service_name == service_name,
                ApiToken.token_type == token_type,
                ApiToken.chat_settings_id == chat_settings_id,
                ApiToken.revoked_at.is_(None),
            )
            .first()
        )
        self.end_read_transaction()
        return result

    def create_or_update_for_chat(
        self,
        service_name: str,
        token_type: str,
        token_value: str,
        chat_settings_id: str,
        issued_at: Optional[datetime] = None,
        expires_at: Optional[datetime] = None,
        scopes: Optional[List[str]] = None,
        metadata: Optional[dict] = None,
    ) -> ApiToken:
        """Create or update a token scoped to a specific tenant.

        Includes revoked tokens in the lookup to avoid unique constraint
        violations on re-issue — revoked_at is cleared when updated.

        Args:
            service_name: Service identifier
            token_type: Token type
            token_value: Encrypted token value
            chat_settings_id: UUID of the chat_settings record
            issued_at: When the token was issued (defaults to now)
            expires_at: When the token expires (None = never)
            scopes: OAuth scopes granted
            metadata: Additional service-specific data

        Returns:
            Created or updated ApiToken
        """
        require_tenant_context(
            chat_settings_id, where="token.create_or_update_for_chat"
        )
        if issued_at is None:
            issued_at = datetime.utcnow()

        # Query WITHOUT revoked_at filter for UPSERT safety
        existing = (
            self.db.query(ApiToken)
            .filter(
                ApiToken.service_name == service_name,
                ApiToken.token_type == token_type,
                ApiToken.chat_settings_id == chat_settings_id,
            )
            .first()
        )

        if existing:
            existing.token_value = token_value
            existing.issued_at = issued_at
            existing.expires_at = expires_at
            existing.last_refreshed_at = datetime.utcnow()
            existing.revoked_at = None
            if scopes is not None:
                existing.scopes = scopes
            if metadata is not None:
                existing.token_metadata = metadata
            self.db.commit()
            self.db.refresh(existing)
            return existing
        else:
            token = ApiToken(
                service_name=service_name,
                token_type=token_type,
                token_value=token_value,
                chat_settings_id=chat_settings_id,
                issued_at=issued_at,
                expires_at=expires_at,
                scopes=scopes,
                token_metadata=metadata,
            )
            self.db.add(token)
            self.db.commit()
            self.db.refresh(token)
            return token

    def delete_tokens_for_chat(
        self,
        service_name: str,
        chat_settings_id: str,
    ) -> int:
        """Delete all tokens for a service scoped to a specific tenant.

        Returns:
            Number of tokens deleted
        """
        require_tenant_context(chat_settings_id, where="token.delete_tokens_for_chat")
        count = (
            self.db.query(ApiToken)
            .filter(
                ApiToken.service_name == service_name,
                ApiToken.chat_settings_id == chat_settings_id,
            )
            .delete()
        )
        self.db.commit()
        return count

    def revoke_tokens_for_service(
        self,
        service_name: str,
        instagram_account_id: Optional[str] = None,
        *,
        chat_settings_id: TenantScope,
    ) -> List[ApiToken]:
        """Set revoked_at on all active tokens for a service.

        Returns the revoked tokens (with token_value still populated)
        so callers can call provider revoke APIs before the values
        become inaccessible.

        Args:
            service_name: Service identifier
            instagram_account_id: Scope to a specific Instagram account
            chat_settings_id: Scope to a specific tenant

        Returns:
            List of tokens that were revoked
        """
        require_tenant_context(
            chat_settings_id, where="token.revoke_tokens_for_service"
        )
        query = self.db.query(ApiToken).filter(
            ApiToken.service_name == service_name,
            ApiToken.revoked_at.is_(None),
        )
        if instagram_account_id:
            query = query.filter(ApiToken.instagram_account_id == instagram_account_id)
        if chat_settings_id:
            query = query.filter(ApiToken.chat_settings_id == chat_settings_id)

        tokens = query.all()
        now = datetime.now(timezone.utc)
        for token in tokens:
            token.revoked_at = now
        self.db.commit()
        return tokens

    def get_expiring_tokens(self, hours_until_expiry: int = 168) -> List[ApiToken]:
        """
        Get all active tokens expiring within the specified hours.

        Default is 168 hours (7 days), used to schedule refresh before expiry.

        Args:
            hours_until_expiry: Hours threshold for "expiring soon"

        Returns:
            List of tokens that will expire within the threshold
        """
        cutoff = datetime.utcnow() + timedelta(hours=hours_until_expiry)
        return (
            self.db.query(ApiToken)
            .filter(
                ApiToken.expires_at.isnot(None),
                ApiToken.expires_at <= cutoff,
                ApiToken.expires_at > datetime.utcnow(),  # Not already expired
                ApiToken.revoked_at.is_(None),
            )
            .order_by(ApiToken.expires_at.asc())
            .all()
        )
