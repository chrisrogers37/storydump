"""Telegram-specific exceptions."""


class AmbiguousDeliveryError(Exception):
    """A Telegram send failed in a way that leaves delivery unknown.

    Raised when the request may have reached Telegram but the response was
    lost (e.g. a client-side timeout on send_photo). The message may already
    be in the chat, so callers must not blind-retry the send — a resend can
    post a duplicate. Recovery is owned by reconciliation paths (button-click
    backfill, stale-processing sweep), not by retrying.
    """
