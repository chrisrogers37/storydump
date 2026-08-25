"""Tenant-resolution refusal — the shared contract of the two resolution tiers.

One type, one closed vocabulary, two raisers that never call each other
(`04` F.3, #842): the target resolver (``src.services.target.tenant_resolution``)
and the legacy settings-service door (``SettingsService.resolve_chat_settings_id``).
Sharing the TYPE is what lets every edge map a refusal once and survive the M.3
swap of the legacy door's internals; sharing code between the tiers stays
forbidden — the tiers agree on the contract, not the implementation.
"""

from src.exceptions.base import RefusalError


class TenantResolutionError(RefusalError):
    """Inbound identity did not resolve — refused, never defaulted.

    ``reason`` is the closed vocabulary in :data:`REASONS`, so callers route
    without parsing prose: unknown_binding | revoked_binding | invalid_session |
    expired_session | revoked_session | disabled_user | not_a_member |
    insufficient_role | membership_list_unreadable (the caller asked which
    workspaces a user belongs to on a connection whose RLS filters that table
    — a structurally partial answer, refused rather than returned; see
    ``workspaces.list_for_user``) | unknown_channel | unprovisioned_channel
    (legacy-era: the deployment's global notification channel has no settings
    row — an operator condition, deliberately distinct from unknown_binding so
    no edge tells an operator to run /start).

    A reason outside the vocabulary is a programming error and is refused at
    construction, so the closed list is closed in practice and not only in
    prose — the web adapter's status table is pinned against this tuple.
    """

    _prefix = "tenant resolution refused"

    REASONS = (
        "unknown_binding",
        "revoked_binding",
        "invalid_session",
        "expired_session",
        "revoked_session",
        "disabled_user",
        "not_a_member",
        "insufficient_role",
        "membership_list_unreadable",
        "unknown_channel",
        "unprovisioned_channel",
    )

    def __init__(self, reason: str, detail: str = ""):
        if reason not in self.REASONS:
            raise ValueError(f"not a resolution reason: {reason!r}")
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
