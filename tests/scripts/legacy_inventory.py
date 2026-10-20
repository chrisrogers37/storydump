"""The retired legacy tier's table names, as a literal — ONE HOME.

The sixteen tables production's ``legacy`` schema held before the tier was
retired in September 2026. Nothing creates them any more; the list exists so
`tests/test_agent_docs.py` can fail a live page that still names one.
"""

from __future__ import annotations

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
