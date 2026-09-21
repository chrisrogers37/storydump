"""Storydump exception classes.

The legacy tier's `backfill`, `google_drive` and `instagram` modules went with
it (the tear-out, phase 01; #1216); the sync writer lane's `identity` and the
unused `telegram` module went with the tech-debt fold (#1325). This package
exports the base classes and the tenancy refusal the target tier raises.
"""

from src.exceptions.base import StorydumpError
from src.exceptions.tenancy import TenantResolutionError

__all__ = ["StorydumpError", "TenantResolutionError"]
