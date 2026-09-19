"""Shared application constants.

Two remain: the Instagram Login API's base URLs, read by
`src/services/target/ig_login_oauth.py`. The posting-schedule bounds and the
caption limit that once lived here had no reader left after the legacy
tear-out (#1216) — a workspace's settings are bounded by the database's CHECK
constraints (`workspaces.SETTINGS_COLUMNS`'s columns), not by Python constants.
"""

# Instagram Login API base URLs (unversioned, separate from Meta Graph API)
IG_LOGIN_GRAPH_BASE = "https://graph.instagram.com"
IG_LOGIN_API_BASE = "https://api.instagram.com"
