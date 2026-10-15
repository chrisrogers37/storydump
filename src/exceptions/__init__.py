"""Storydump exception classes.

This package exports the base classes and the tenancy refusal the target
tier raises (#1216, #1325).
"""

from src.exceptions.base import StorydumpError
from src.exceptions.tenancy import TenantResolutionError

__all__ = ["StorydumpError", "TenantResolutionError"]
