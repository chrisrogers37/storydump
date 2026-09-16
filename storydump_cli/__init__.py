"""The storydump CLI: the operator's terminal for the storydump API.

A separate package from the server on purpose. It reaches ``src`` through
exactly one module, ``src.services.target.vocabulary`` (the import-boundary
test pins that), speaks to the API over HTTPS with a bearer token, and never
opens a database. Its console script is ``storydump``; the legacy
``cli/`` package it replaced was deleted in phase 03.
"""

from __future__ import annotations

__version__ = "0.1.0"
