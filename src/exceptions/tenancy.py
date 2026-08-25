"""Tenant-resolution refusal — the shared contract of the two resolution tiers.

One type, one closed vocabulary, two raisers that never call each other
(`04` F.3, #842): the target resolver (``src.services.target.tenant_resolution``)
and the legacy settings-service door (``SettingsService.resolve_chat_settings_id``).
Sharing the TYPE is what lets every edge map a refusal once and survive the M.3
swap of the legacy door's internals; sharing code between the tiers stays
forbidden — the tiers agree on the contract, not the implementation.
"""

from src.exceptions.base import StorydumpError


class TenantResolutionError(StorydumpError):
    """Inbound identity did not resolve — refused, never defaulted.

    ``reason`` is a closed vocabulary so callers can route without parsing
    prose: unknown_binding | revoked_binding | invalid_session |
    expired_session | revoked_session | disabled_user | not_a_member | insufficient_role |
    unknown_channel | unprovisioned_channel (legacy-era: the deployment's
    global notification channel has no settings row — an operator condition,
    deliberately distinct from unknown_binding so no edge tells an operator
    to run /start).
    """

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        super().__init__(
            f"tenant resolution refused: {reason}" + (f" — {detail}" if detail else "")
        )


class TenantProvisioningError(StorydumpError):
    """A tenant MINT was refused — provisioning, deliberately not resolution.

    A separate type rather than a new ``TenantResolutionError`` reason: the
    resolution vocabulary is a closed contract shared across the two tiers,
    and a mint precondition failing (``unknown_user`` — the caller asked to
    provision for a user row that does not exist) is not an identity failing
    to resolve. Edges that map resolution reasons must not learn provisioning
    by accident.
    """

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        super().__init__(
            f"tenant provisioning refused: {reason}"
            + (f" — {detail}" if detail else "")
        )
