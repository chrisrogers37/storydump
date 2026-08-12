"""Telegram-specific exceptions."""

import re
from typing import Optional, Tuple


class AmbiguousDeliveryError(Exception):
    """A Telegram send failed in a way that leaves delivery unknown.

    Raised when the request may have reached Telegram but the response was
    lost (e.g. a client-side timeout on send_photo). The message may already
    be in the chat, so callers must not blind-retry the send — a resend can
    post a duplicate. Recovery is owned by reconciliation paths (button-click
    backfill, stale-processing sweep), not by retrying.
    """


class ChatMigratedError(Exception):
    """A send failed because the chat migrated to a supergroup and changed id.

    Telegram signals a group->supergroup migration on two channels: a service
    message at migration time (handled live by #744), and this permanent
    send-path error. The send-path one is the ONLY channel that can still reach
    a chat whose live event was missed, which is every chat stranded before
    #744 shipped — so it is the recovery signal for #743's remaining half.

    Carries BOTH ids because neither alone is actionable: ``telegram.error.
    ChatMigrated`` supplies only ``new_chat_id``, and the old id is known only
    at the send site, from the chat the send was addressed to.

    Not retryable. A migrated id never un-migrates, so the three retries the
    generic path would spend are three guaranteed failures against a dead id --
    the same reasoning that makes GoogleDriveAuthError fail fast.
    """

    # The durable form written to posting_history.error_message. Rendering and
    # parsing live together here, next to the regex, so the future recovery
    # pass reads the pair with the writer's own definition instead of
    # inventing a second one that drifts.
    #
    # Shape is key=value rather than prose because the consumer is a query, not
    # a reader: "chat_migrated" is the greppable anchor and both ids are
    # recoverable positionally. Negative ids are the norm (groups are negative,
    # supergroups -100-prefixed), so the sign is part of the pattern.
    _PATTERN = re.compile(r"chat_migrated old_chat_id=(-?\d+) new_chat_id=(-?\d+)")

    def __init__(self, old_chat_id: int, new_chat_id: int):
        self.old_chat_id: int = old_chat_id
        self.new_chat_id: int = new_chat_id
        super().__init__(self.durable_message())

    def durable_message(self) -> str:
        """The string persisted to posting_history.error_message.

        This is the whole point of the class: before it, the new id existed
        only inside a ``logger.error`` line and never reached any durable
        store, so the one fact needed to recover a stranded tenant aged out
        with log retention.
        """
        return (
            f"chat_migrated old_chat_id={self.old_chat_id} "
            f"new_chat_id={self.new_chat_id} — tenant is stranded at the old "
            f"id until reconciled (#743)"
        )

    @classmethod
    def parse_pair(cls, error_message: Optional[str]) -> Optional[Tuple[int, int]]:
        """Recover ``(old_chat_id, new_chat_id)`` from a persisted message.

        Returns None when the message is absent or is any other send failure,
        so a caller can sweep posting_history rows indiscriminately and keep
        only the migrations. Deliberately does not guess: a partial or
        reworded match yields None rather than a half-built pair, because a
        wrong id here would re-point a live tenant at someone else's chat.

        THE CORPUS THIS SWEEPS IS A LOWER BOUND, NOT A CENSUS. A row is
        written only where a send both (a) goes through the queue-card
        delivery path and (b) has a queue item to record against. Three
        common shapes produce no row at all, and they are not randomly
        distributed — they correlate with the tenants an operator most wants
        to find:

        - a tenant with no eligible media never reaches a send, so a
          depleted-pool tenant is invisible here;
        - an auto-approved post bypasses Telegram except for one notify whose
          handler is ``except Exception: pass`` (scheduler_loop.py);
        - the pool-depletion, token and lifecycle alerts are best-effort
          sends with no queue item, so they have nowhere to write.

        A recovery pass must therefore treat "not in this corpus" as unknown,
        never as "did not migrate".
        """
        if not error_message:
            return None
        match = cls._PATTERN.search(error_message)
        if not match:
            return None
        return int(match.group(1)), int(match.group(2))
