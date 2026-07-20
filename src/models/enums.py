"""Code-owned enums — the single source of truth for the CHECK-constrained
status/method vocabularies.

Each DB ``CHECK`` constraint is derived from the matching ``Enum`` here (via
``sql_in_list``) so the model declaration, the migration DDL, and the code that
writes the values can never silently drift apart. A CI parity gate
(``tests/src/models/test_enum_ssot_parity.py``) asserts that the model
constraint and the latest migration DDL both match the Enum — drift fails a
test instead of aborting a production scheduler sweep with a ``CheckViolation``
(the #684 / #685 class).

Scope note: this increment wires ``PostingMethod`` and ``HistoryStatus`` to
``posting_history``'s CHECK constraints. ``QueueStatus`` is the *target* queue
vocabulary; it is wired to ``posting_queue.check_status`` in the follow-up
status-model migration (M-B), not here — ``posting_queue.check_status`` stays
hand-declared until then.
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
    """Target vocabulary for ``posting_queue.status`` (check_status).

    Wired to the model constraint + migrated in the status-model follow-up
    (M-B), reconciling #510's ``ready``/``claimed`` with the delivery states
    (``sent_unconfirmed``/``delivered``/``expired``). NOT applied in this
    increment.
    """

    READY = "ready"
    CLAIMED = "claimed"
    SENT_UNCONFIRMED = "sent_unconfirmed"
    DELIVERED = "delivered"
    POSTED = "posted"
    EXPIRED = "expired"
    FAILED = "failed"


def sql_in_list(enum_cls) -> str:
    """Render an ``Enum`` as the body of a SQL ``IN (...)`` list, in definition
    order — deterministic output so constraint and migration text stay stable."""
    return ", ".join(f"'{member.value}'" for member in enum_cls)
