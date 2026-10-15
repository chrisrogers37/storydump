"""Tenant-resolution refusal — the contract every resolution edge shares.

One type, one closed vocabulary, raised at every resolution edge (#842,
#1216). Sharing the TYPE is what lets every edge map a refusal once.
"""

from src.exceptions.base import RefusalError
from src.services.target import vocabulary


class TenantResolutionError(RefusalError):
    """Inbound identity did not resolve — refused, never defaulted.

    ``reason`` is the closed vocabulary in :data:`REASONS`, so callers route
    without parsing prose: unknown_binding | revoked_binding | invalid_session |
    expired_session | revoked_session | disabled_user | not_a_member |
    insufficient_role | unknown_channel | invalid_token | expired_token |
    revoked_token (a bearer API token that did not resolve — the token
    resolver's three answers, mapped like their session twins: 401, and the
    response never says which).

    A reason outside the vocabulary is a programming error and is refused at
    construction, so the closed list is closed in practice and not only in
    prose — the web adapter's status table is pinned against this tuple.
    """

    _prefix = "tenant resolution refused"

    #: The chat- and session-plane reasons this module owns, then the
    #: resolver's token reasons from the vocabulary — `disabled_user` is in
    #: both lists and is spelled once, here (#1325).
    REASONS: tuple[str, ...] = (
        "unknown_binding",
        "revoked_binding",
        "invalid_session",
        "expired_session",
        "revoked_session",
        "disabled_user",
        "not_a_member",
        "insufficient_role",
        "unknown_channel",
    ) + tuple(r for r in vocabulary.TOKEN_RESOLUTION_REASONS if r != "disabled_user")

    def __init__(self, reason: str, detail: str = ""):
        if reason not in self.REASONS:
            raise ValueError(f"not a resolution reason: {reason!r}")
        super().__init__(reason, detail)


class TokenRefused(RefusalError):
    """A bearer API token that RESOLVED was refused for what it asked.

    A separate type from :class:`TenantResolutionError` because these are
    not identity failures: the token is live and known, and the answer must
    carry its reason (403 with ``reason``) so the CLI can say the right
    sentence — the resolution handler deliberately says nothing.

    ``reason``: session_required (the route is for signed-in web sessions
    only — minting, invitations, the OAuth legs) | readonly_token (a
    ``readonly`` token, or any workspace service identity, on a write) |
    wrong_workspace (a service identity addressing a workspace that is not
    its own) | not_ops (a person outside `OPS_USER_IDS`, or a
    service identity, at an estate-wide route: the API's operating details,
    its posture, the waitlist, an admission — which is a write, so a token
    that may not write hears readonly_token there first).
    """

    _prefix = "token refused"

    REASONS: tuple[str, ...] = vocabulary.TOKEN_REFUSALS

    def __init__(self, reason: str, detail: str = ""):
        if reason not in self.REASONS:
            raise ValueError(f"not a token refusal: {reason!r}")
        super().__init__(reason, detail)


class CrossSiteRefused(RefusalError):
    """A cookie-carried session on a state-changing request that a page of
    neither the front end nor the API itself made.

    ``SameSite=Lax`` keeps the cookie off a cross-SITE post, but a site is the
    registrable domain, so a page on any sibling host of the product domain
    would post with it attached. A bearer credential is not ambient — a page
    cannot make a browser attach one — so only the cookie path is checked.

    ``reason``: cross_site (the request's ``Origin``, else its ``Referer``'s
    origin, is absent or is not one the API admits) — the same word the front
    end's own route guard answers with (`landing/src/lib/route-guards.ts`).
    """

    _prefix = "cross-site request refused"

    REASONS: tuple[str, ...] = ("cross_site",)

    def __init__(self, reason: str, detail: str = ""):
        if reason not in self.REASONS:
            raise ValueError(f"not a cross-site refusal: {reason!r}")
        super().__init__(reason, detail)
