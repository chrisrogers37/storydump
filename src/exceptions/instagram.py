"""Instagram and cloud storage related exceptions."""

from typing import Optional

from src.exceptions.base import StorydumpError


class InstagramAPIError(StorydumpError):
    """
    General Instagram API error.

    Raised when Instagram's Graph API returns an error response.

    Attributes:
        message: Human-readable error description
        error_code: Instagram/Meta error code (e.g., 'OAuthException')
        error_subcode: More specific error subcode from Meta
    """

    def __init__(
        self,
        message: str,
        error_code: Optional[str] = None,
        error_subcode: Optional[int] = None,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.error_subcode = error_subcode

    def __str__(self) -> str:
        base = super().__str__()
        if self.error_code:
            return f"{base} (code: {self.error_code})"
        return base


# Instagram container ``status_code`` values that affirmatively confirm the
# media will never publish. ``InstagramAPIService._wait_for_container_ready``
# raises an ``InstagramAPIError`` carrying ``error_code=status_code`` for each
# of these. Unlike an ambiguous crash/timeout (publish outcome unknown), one of
# these means Instagram has told us nothing was posted — so a claimed
# 'publishing' row can be safely released for retry instead of held forever.
CONTAINER_CONFIRMED_FAILED_STATUS_CODES = frozenset({"ERROR", "EXPIRED"})


def is_container_confirmed_failed(exc: BaseException) -> bool:
    """Whether ``exc`` is Instagram affirmatively confirming the media container
    failed (status_code ERROR/EXPIRED) — i.e. the publish provably never
    happened, so the claimed row is safe to release for retry.

    Returns False for an ambiguous crash/timeout where a container exists but
    the publish outcome is unknown; only the confirmed-failed case is safe to
    release (the ambiguous case must stay stuck to avoid a duplicate story).
    """
    return (
        isinstance(exc, InstagramAPIError)
        and exc.error_code in CONTAINER_CONFIRMED_FAILED_STATUS_CODES
    )


class RateLimitError(InstagramAPIError):
    """
    Instagram API rate limit exceeded.

    Raised when we've hit Meta's rate limits (typically 25 posts/hour for Stories).
    The caller should back off and retry later, or route to Telegram fallback.

    Attributes:
        retry_after_seconds: Suggested wait time before retrying (if provided by API)
    """

    def __init__(
        self,
        message: str = "Instagram API rate limit exceeded",
        retry_after_seconds: Optional[int] = None,
        **kwargs,
    ):
        super().__init__(message, **kwargs)
        self.retry_after_seconds = retry_after_seconds


class TokenExpiredError(InstagramAPIError):
    """
    Instagram access token has expired or is invalid.

    Raised when the API returns an authentication error indicating
    the token needs to be refreshed or re-authorized.

    This error should trigger automatic token refresh if possible,
    or alert the admin to re-authenticate.
    """

    def __init__(
        self,
        message: str = "Instagram access token has expired",
        **kwargs,
    ):
        super().__init__(message, **kwargs)


class TokenCorruptError(InstagramAPIError):
    """
    Instagram access token cannot be parsed by Meta's API.

    Raised when Meta returns error code 190 with a message indicating the
    token value itself is unparseable (e.g., "Cannot parse access token").
    Unlike TokenExpiredError (time-based), this means the token bytes are
    wrong — truncated, corrupted, or invalidated server-side without a
    revocation subcode.

    Resolution requires a full OAuth re-authorization to obtain a new token.
    Token refresh will also fail because it passes the same broken token.
    """

    def __init__(
        self,
        message: str = "Instagram access token is invalid and cannot be used. Please reconnect your account.",
        **kwargs,
    ):
        super().__init__(message, **kwargs)


class MediaUnsupportedError(InstagramAPIError):
    """
    Instagram could not parse the uploaded file (Meta error code 9004).

    Raised when Meta returns "Only photo or video can be accepted as media
    type." This happens when the Cloudinary URL we serve produces output
    that Instagram doesn't recognize as a valid image or video — common
    causes are HEIC files masquerading as JPG, GIFs (IG Stories don't
    accept), or a Cloudinary transformation failure that served back an
    error page.

    Unlike TokenCorruptError (credential is broken) or RateLimitError
    (transient), this means THIS specific media item will fail every time
    it's posted until the file is fixed. The autopost handler creates a
    permanent_reject lock so the scheduler doesn't keep cycling through it.
    """

    def __init__(
        self,
        message: str = "Instagram could not parse the uploaded file (Meta code 9004).",
        **kwargs,
    ):
        super().__init__(message, **kwargs)


class TokenRevokedError(InstagramAPIError):
    """
    Instagram refresh token has been revoked.

    Raised when the user has deauthorized the app, changed their password,
    or Instagram has otherwise invalidated the refresh token. Unlike
    TokenExpiredError (which can be resolved by refreshing), this requires
    the user to fully reconnect their account via OAuth.

    Meta error subcodes that indicate revocation:
    - 458: App not installed (user deauthorized)
    - 460: Password changed since token issuance
    - 467: Token invalidated on server side
    """

    # Meta error subcodes that indicate refresh token revocation
    REVOCATION_SUBCODES = {458, 460, 467}

    def __init__(
        self,
        message: str = "Instagram account has been disconnected. Please reconnect.",
        **kwargs,
    ):
        super().__init__(message, **kwargs)


class MediaUploadError(StorydumpError):
    """
    Cloud storage upload failed.

    Raised when uploading media to Cloudinary (or other cloud storage) fails.
    This could be due to network issues, invalid credentials, or file problems.

    Attributes:
        file_path: Local path to the file that failed to upload
        provider: Cloud storage provider name (e.g., 'cloudinary')
    """

    def __init__(
        self,
        message: str,
        file_path: Optional[str] = None,
        provider: Optional[str] = None,
    ):
        super().__init__(message)
        self.file_path = file_path
        self.provider = provider

    def __str__(self) -> str:
        base = super().__str__()
        if self.file_path:
            return f"{base} (file: {self.file_path})"
        return base
