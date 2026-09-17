"""The models package.

The fourteen legacy SQLAlchemy models this module re-exported went with the
legacy tier (the tear-out, phase 01; #1216). The target schema's declarative
models live in `src.models.target` and are imported by that path; nothing is
exported here, on purpose — `tests/src/test_legacy_tier_gone.py` pins it.
"""
