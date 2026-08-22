"""Storydump exception classes."""

from src.exceptions.base import StorydumpError
from src.exceptions.google_drive import (
    GoogleDriveError,
    GoogleDriveAuthError,
    GoogleDriveRateLimitError,
    GoogleDriveFileNotFoundError,
)
from src.exceptions.instagram import (
    InstagramAPIError,
    RateLimitError,
    TokenExpiredError,
    TokenCorruptError,
    TokenRevokedError,
    MediaUploadError,
    MediaUnsupportedError,
    is_publish_definitively_failed,
)
from src.exceptions.tenancy import TenantResolutionError
from src.exceptions.backfill import (
    BackfillError,
    BackfillMediaExpiredError,
    BackfillMediaNotFoundError,
)

__all__ = [
    "StorydumpError",
    "GoogleDriveError",
    "GoogleDriveAuthError",
    "GoogleDriveRateLimitError",
    "GoogleDriveFileNotFoundError",
    "InstagramAPIError",
    "RateLimitError",
    "TokenExpiredError",
    "TokenCorruptError",
    "TokenRevokedError",
    "MediaUploadError",
    "MediaUnsupportedError",
    "is_publish_definitively_failed",
    "BackfillError",
    "BackfillMediaExpiredError",
    "BackfillMediaNotFoundError",
    "TenantResolutionError",
]
