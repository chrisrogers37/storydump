"""Identity-provisioning refusal — the user plane's mint failures.

A third type beside `tenancy`'s two, on the same reasoning that split those:
`TenantResolutionError` is a closed vocabulary shared by two resolution tiers,
`TenantProvisioningError` covers a tenant MINT, and neither describes an
IDENTITY mint failing its own preconditions. An edge that maps resolution
reasons must not learn identity provisioning by accident.

The one reason that carries real product weight is `email_belongs_to_another`
(D35, below). The database says the same thing from the other side
(`uq_users_primary_email`), and this type is how that constraint reaches a
caller as a decision rather than as an integrity error.
"""

from src.exceptions.base import RefusalError


class IdentityProvisioningError(RefusalError):
    """A user/identity mint was refused — provisioning, not resolution.

    ``reason`` is a closed vocabulary so callers route without parsing prose:
    email_belongs_to_another (D35 — the verified claim is already another
    user's primary_email; never merged) | missing_subject (the caller passed
    no OIDC subject — a programming error at the edge, not a user that does
    not exist).
    """

    _prefix = "identity provisioning refused"
