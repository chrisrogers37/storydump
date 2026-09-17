"""Storydump exception classes.

The legacy tier's `backfill`, `google_drive` and `instagram` modules went with
it (the tear-out, phase 01; #1216). This package exports the base classes and
the tenancy refusal the target tier raises; `identity` and `telegram` are
imported by their own path.
"""

from src.exceptions.base import RefusalError, StorydumpError
from src.exceptions.tenancy import TenantResolutionError

__all__ = ["RefusalError", "StorydumpError", "TenantResolutionError"]
