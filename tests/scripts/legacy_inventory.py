"""The legacy lineage's table inventory, as a literal — ONE HOME.

Until the tear-out (plan ``2026-09-16-legacy-tear-out``, phase 01) the lineage
lane derived this inventory from the legacy SQLAlchemy models'
``Base.metadata``; the models are gone, and a literal is the honest
replacement. Two facts live here because two consumers need different
subsets:

- ``LEGACY_TABLES`` — the sixteen tables production's ``legacy`` schema holds
  (fork F4): the fourteen of ``02`` §9, ``schema_version`` (the legacy
  lineage's own ledger), and ``posting_history_dedup_archive``, made by hand
  during #941 and declared by no file in the tree. Phase 03's snapshot file
  copies every one of these, and its gate seeds the hand-made one from
  production's pasted DDL.
- ``LINEAGE_TABLES`` — what the lineage's replay (``scripts/setup_database.sql``
  + 001–050, then the 051 move) actually creates: ``LEGACY_TABLES`` minus the
  hand-made table. The lineage lane compares the replayed ``legacy`` schema
  against THIS, so the lane is not asked to find a table no file creates.

A name added here is a claim about production; the read-only probe of phase
03 step 1 is what verifies it.
"""

from __future__ import annotations

#: Made by hand in production, declared by no file in the tree (#941).
HAND_MADE = ("posting_history_dedup_archive",)

LEGACY_TABLES = (
    "api_tokens",
    "audit_log",
    "category_post_case_mix",
    "chat_settings",
    "instagram_accounts",
    "media_items",
    "media_posting_locks",
    "onboarding_sessions",
    "posting_history",
    "posting_history_dedup_archive",
    "posting_queue",
    "schema_version",
    "service_runs",
    "user_chat_memberships",
    "user_interactions",
    "users",
)

LINEAGE_TABLES = tuple(t for t in LEGACY_TABLES if t not in HAND_MADE)
