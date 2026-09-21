"""Tenant-resolution refusal — the shared contract of the two resolution tiers.

One type, one closed vocabulary. It had two raisers that never called each
other (`04` F.3, #842): the target resolver (``src.services.target.tenant_resolution``)
and the legacy settings-service door (``SettingsService.resolve_chat_settings_id``,
deleted with the legacy tier, #1216). Sharing the TYPE is what let every edge
map a refusal once and survive the swap; the contract outlived the second raiser.
"""

from src.exceptions.base import RefusalError
from src.services.target import vocabulary


class TenantResolutionError(RefusalError):
    """Inbound identity did not resolve — refused, never defaulted.

    ``reason`` is the closed vocabulary in :data:`REASONS`, so callers route
    without parsing prose: unknown_binding | revoked_binding | invalid_session |
    expired_session | revoked_session | disabled_user | not_a_member |
    insufficient_role | unknown_channel | unprovisioned_channel (legacy-era:
    the deployment's global notification channel has no settings row — an
    operator condition, deliberately distinct from unknown_binding so no edge
    tells an operator to run /start — no raiser in src as of 2026-09-20
    (#1325 audit, TD-C9)) | invalid_token | expired_token |
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
    #: both lists and is spelled once, here (#1325 audit, TD-C9).
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
        "unprovisioned_channel",
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
    its own).
    """

    _prefix = "token refused"

    REASONS: tuple[str, ...] = vocabulary.TOKEN_REFUSALS

    def __init__(self, reason: str, detail: str = ""):
        if reason not in self.REASONS:
            raise ValueError(f"not a token refusal: {reason!r}")
        super().__init__(reason, detail)


class TenantProvisioningError(RefusalError):
    """A tenant MINT was refused — provisioning, deliberately not resolution.

    A separate type rather than a new ``TenantResolutionError`` reason: the
    resolution vocabulary is a closed contract shared across the two tiers,
    and a mint precondition failing is not an identity failing to resolve.
    Edges that map resolution reasons must not learn provisioning by accident.

    ``reason``: unknown_user (the caller asked to provision for a user row
    that does not exist) | missing_owner (no owner was named at all — a
    different fact from a user that does not exist, and an edge rendering
    "no such user" for a blank form field is why they are not one
    reason) | invalid_name (a blank or whitespace-only workspace name).

    Deliberately NOT here: an autocommit connection. That is a caller misusing
    the transaction substrate rather than a provisioning refusal, and it has
    its own type (``sync_tx.TransactionRequired``) so an edge mapping these
    reasons cannot render it as one.
    """

    _prefix = "tenant provisioning refused"
