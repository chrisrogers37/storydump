"""Code-owned enums — the single source of truth for the CHECK-constrained
status/method vocabularies.

Each DB ``CHECK`` constraint is derived from the matching ``Enum`` here (via
``sql_in_list``) so the model declaration, the migration DDL, and the code that
writes the values can never silently drift apart. A CI parity gate
(``tests/src/models/test_enum_ssot_parity.py``) asserts that the model
constraint and the latest migration DDL both match the Enum — drift fails a
test instead of aborting a production scheduler sweep with a ``CheckViolation``
(the #684 / #685 class).

All three vocabularies (``PostingMethod``, ``HistoryStatus``, ``QueueStatus``)
are wired to their model CHECK constraints and guarded by the parity gate.
"""

from enum import Enum


class PostingMethod(str, Enum):
    """Allowed values for ``posting_history.posting_method`` (check_posting_method)."""

    INSTAGRAM_API = "instagram_api"
    TELEGRAM_MANUAL = "telegram_manual"
    SYSTEM_EXPIRY = "system_expiry"
    AUTO_REAPPROVAL = "auto_reapproval"


class HistoryStatus(str, Enum):
    """Allowed values for ``posting_history.status`` (check_history_status)."""

    POSTED = "posted"
    FAILED = "failed"
    SKIPPED = "skipped"
    REJECTED = "rejected"
    EXPIRED = "expired"


class QueueStatus(str, Enum):
    """Allowed values for ``posting_queue.status`` (check_status).

    The lifecycle state of a row while it is active work in the queue:

    - ``pending`` — scheduled, not yet claimed for a send attempt.
    - ``processing`` — claimed; a send attempt is in flight.
    - ``publishing`` — Instagram media container created, publish call in
      flight (the #549 claim-before-publish anchor).
    - ``sent_unconfirmed`` — the Telegram send was dispatched but delivery could
      not be confirmed (an ``AmbiguousDeliveryError``); the card may or may not
      be in the chat. Resolves one-way — a recovered ``telegram_message_id``
      stamp or a button click promotes it to ``delivered``, otherwise an aged
      reconcile records it as expired.
    - ``delivered`` — a ``telegram_message_id`` is stamped; the approval card is
      confirmed in the chat, awaiting a human decision.
    - ``failed`` — the attempt failed terminally.

    Terminal outcomes (posted/skipped/rejected/expired) are written to
    ``posting_history`` and the queue row is deleted, so they are not queue
    states. The #510 ``ready``/``claimed`` lease rename lands with its lease
    columns in the lease increment (PR-L).
    """

    PENDING = "pending"
    PROCESSING = "processing"
    FAILED = "failed"
    PUBLISHING = "publishing"
    SENT_UNCONFIRMED = "sent_unconfirmed"
    DELIVERED = "delivered"


def sql_in_list(enum_cls) -> str:
    """Render an ``Enum`` as the body of a SQL ``IN (...)`` list, in definition
    order — deterministic output so constraint and migration text stay stable."""
    return ", ".join(f"'{member.value}'" for member in enum_cls)
