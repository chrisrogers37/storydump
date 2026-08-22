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
        http_status: The HTTP status Instagram answered with, when this error
            was raised from a response. ``None`` means no response was
            classified — a transport failure, or a 2xx body we could not use.
            The distinction is load-bearing for
            :func:`is_publish_definitively_failed`.
    """

    def __init__(
        self,
        message: str,
        error_code: Optional[str] = None,
        error_subcode: Optional[int] = None,
        http_status: Optional[int] = None,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.error_subcode = error_subcode
        self.http_status = http_status

    def __str__(self) -> str:
        base = super().__str__()
        if self.error_code:
            return f"{base} (code: {self.error_code})"
        return base


# Instagram container ``status_code`` values that affirmatively confirm the
# media will never publish. ``InstagramAPIService._wait_for_container_ready``
# raises an ``InstagramAPIError`` carrying ``error_code=status_code`` for each
# of these — on an HTTP 200 body, which is why they need their own test rather
# than riding the status check below.
CONTAINER_CONFIRMED_FAILED_STATUS_CODES = frozenset({"ERROR", "EXPIRED"})


def _is_client_rejection(http_status: Optional[int]) -> bool:
    """Whether Instagram answered with a 4xx — a request it refused to act on.

    Deliberately NOT "any non-2xx". A 5xx is the classic non-idempotent-write
    ambiguity: the server may have created the story and then failed to tell
    us. A 4xx is a refusal made before any work happened.
    """
    return http_status is not None and 400 <= http_status < 500


def is_publish_definitively_failed(exc: BaseException) -> bool:
    """Whether ``exc`` means the story provably did NOT publish, so a claimed
    'publishing' row is safe to release for retry instead of held forever.

    Two independent ways to be sure, because Instagram says "this did not
    publish" in two different registers:

    - **A container status of ERROR/EXPIRED.** IG returns 200 and reports the
      failure in the body, so there is no HTTP status to read.
    - **A 4xx on the call itself.** Instagram received the request and refused
      it. Whether that refusal landed on ``media_publish`` (the publish was
      rejected) or on an earlier call (the publish was never attempted), the
      story was not created either way.

    Everything else stays ambiguous and the row stays held — this predicate is
    the release gate for #549's claim-before-publish anchor, so its default
    must be "we do not know". The cases that keep the hold, and are meant to:

    - a transport failure with no response at all — the ``media_publish`` POST
      may have reached Instagram;
    - a 5xx, which may follow a story that was actually created;
    - an HTTP 200 whose body carries no story id.

    Testing only the first registry is not sufficient, and the shortfall is
    not a corner case: a predicate that reads container status alone sends
    every error raised off a rejected response into the ambiguous branch,
    where the queue row is held and never revisited. Measured on production
    for #940, that stranded 6 of 158 container-creating attempts. The general
    argument was already written down for one error class in
    ``telegram_autopost``'s RateLimitError special case — that Instagram
    enforces the publish quota at the call itself, so a rejection there means
    the story was never created. That reasoning is not specific to quota.
    """
    if not isinstance(exc, InstagramAPIError):
        return False
    return (
        exc.error_code in CONTAINER_CONFIRMED_FAILED_STATUS_CODES
        or _is_client_rejection(exc.http_status)
    )


class RateLimitError(InstagramAPIError):
    """
    Instagram API rate limit exceeded.

    Raised when Meta reports the account's content-publishing quota is
    exhausted — the rolling-24h publish limit, shared across Stories, Reels,
    and feed posts — or when the pre-publish quota check reports no remaining.
    The caller surfaces the daily-limit state so the operator can post manually
    now or retry once the 24h window rolls.

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
