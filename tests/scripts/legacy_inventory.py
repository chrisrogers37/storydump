"""The legacy lineage's table inventory, as a literal — ONE HOME.

Until the tear-out (plan ``2026-09-16-legacy-tear-out``, phase 01) the lineage
lane derived this inventory from the legacy SQLAlchemy models'
``Base.metadata``; the models are gone, and a literal is the honest
replacement: the sixteen tables production's ``legacy`` schema holds (fork F4;
measured by the tear-out's read-only probe on 2026-09-17, exactly these) — the
fourteen of ``02`` §9, ``schema_version`` (the legacy lineage's own ledger),
and ``posting_history_dedup_archive``, made by hand during #941 and created by
no migration. The lane compares the replayed ``legacy`` schema to this list
both ways; phase 03's snapshot file (078) copies every name in it.

``HAND_MADE`` records which of the sixteen no migration creates: the lane's
world seeds those from ``tests/scripts/fixtures/legacy_by_hand.sql`` (the DDL
the probe measured) beside the legacy seed, so a replay holds what production
holds. A name added here is a claim about production; the probe is what
verifies it.
"""

from __future__ import annotations

#: Made by hand in production, declared by no migration (#941).
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
