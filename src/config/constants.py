"""Shared application constants.

Constants used by multiple modules are defined here to ensure consistency.
Module-specific constants should be defined as class-level attributes on
their respective service classes instead.
"""

# Posting schedule limits (the bounds `workspaces.SETTINGS_COLUMNS` are validated against)
MIN_POSTS_PER_DAY = 1
MAX_POSTS_PER_DAY = 50
MIN_POSTING_HOUR = 0
MAX_POSTING_HOUR = 23

# Instagram caption limits
MAX_CAPTION_LENGTH = 2200  # Instagram caption character limit

# Instagram Login API base URLs (unversioned, separate from Meta Graph API)
# Read by src/services/target/ig_login_oauth.py
IG_LOGIN_GRAPH_BASE = "https://graph.instagram.com"
IG_LOGIN_API_BASE = "https://api.instagram.com"
