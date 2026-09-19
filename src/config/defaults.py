"""The starting values of a workspace's product settings.

The database is the source of truth at runtime: a workspace's settings are
columns on `workspaces` (`workspaces.SETTINGS_COLUMNS`), an account's
overrides columns on `ig_accounts`, and both move only through the command
port. These constants are read in two places of the target tier
(`command_executors.py`, `intent_ledger.py`): the values a workspace starts
with, and the fallback for a column that is NULL. Once a person changes a
setting on the web the column holds an explicit value and the constant is
not consulted again. There is no environment-variable override for a
product setting (`02` §4).
"""

# Posting cadence
DEFAULT_POSTS_PER_DAY = 3
DEFAULT_POSTING_HOURS_START = 9  # User-local time (interpreted via posting_timezone)
DEFAULT_POSTING_HOURS_END = 22  # User-local time (interpreted via posting_timezone)
DEFAULT_POSTING_TIMEZONE = "America/New_York"

# Lock TTLs (days)
DEFAULT_REPOST_TTL_DAYS = 30
DEFAULT_SKIP_TTL_DAYS = 45

# Toggles
DEFAULT_DRY_RUN_MODE = False
DEFAULT_ENABLE_INSTAGRAM_API = False
DEFAULT_SHOW_VERBOSE_NOTIFICATIONS = True
DEFAULT_MEDIA_SYNC_ENABLED = False
DEFAULT_SEND_LIFECYCLE_NOTIFICATIONS = True

# Caption rendering
DEFAULT_CAPTION_STYLE = "enhanced"  # or "simple"

# Media source (NULL/None forces the user through the setup wizard)
DEFAULT_MEDIA_SOURCE_TYPE = "local"

# Instagram deep-link fallback used by the bot keyboard's "Open Instagram"
# button when an active account has no `instagram_username` set. The
# plain instagram.com URL works on every device; a per-username deep
# link (instagram://user?username=...) is preferred when available.
DEFAULT_INSTAGRAM_DEEPLINK_URL = "https://www.instagram.com/"
