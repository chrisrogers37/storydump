"""The fallbacks the target tier reads for the two TTL columns that may be NULL.

A workspace's product settings are columns on `workspaces`
(`workspaces.SETTINGS_COLUMNS`), an account's overrides columns on
`ig_accounts`, and both move only through the command port. The values a
workspace STARTS with are the DDL's defaults (053: `posts_per_day` 3,
`posting_hours_start` 14, `tz` 'UTC' ...), never a Python constant. What
this module holds is the fallback for the two columns 053 declares NULL:
`command_executors.py` reads `DEFAULT_SKIP_TTL_DAYS` for a skip on a
workspace with no `skip_ttl_days`, `intent_ledger.py` reads
`DEFAULT_REPOST_TTL_DAYS` for a repost lock with no `repost_ttl_days`. Once
a person sets either on the web the column holds a value and the constant is
not consulted. There is no environment-variable override for a product
setting (`02` §4). `tests/src/config/test_defaults.py` keeps this file at
what is read: twelve constants nothing consulted (a posting cadence that
disagreed with the DDL, the toggles, a caption style, a deep link) were
deleted with the legacy tier that read them.
"""

# Lock TTLs (days): the fallback for a NULL column
DEFAULT_REPOST_TTL_DAYS = 30
DEFAULT_SKIP_TTL_DAYS = 45
