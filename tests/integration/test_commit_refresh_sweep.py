"""#908 — a representative swept `commit_and_refresh` site proven on backend
state: after `UserRepository.create`, no connection is left idle-in-transaction
on `users`. Born-red against the pre-sweep `commit(); refresh()` form (revert
the one site and this reddens). The shared probe and routing fixture live in
`conftest.py`; the primitive's own leak-safety is #907's gate.
"""

from __future__ import annotations

import uuid

import pytest

from src.repositories.user_repository import UserRepository

pytestmark = [pytest.mark.integration]


class TestASweptSiteLeavesNoIdleInTransaction:
    def test_user_repository_create_parks_no_backend(
        self, routed_engine, idle_in_transaction_touching
    ):
        """A representative swept `add → commit_and_refresh` site: after the
        create, no backend is left idle-in-transaction on `users`."""
        before = idle_in_transaction_touching(routed_engine, "users")
        repo = UserRepository()
        try:
            repo.create(
                telegram_user_id=int(uuid.uuid4().int % (10**11)),
                telegram_username=f"sweep-{uuid.uuid4().hex[:8]}",
                telegram_first_name="Sweep",
                telegram_last_name="Probe",
            )
            leaked = idle_in_transaction_touching(routed_engine, "users") - before
            assert not leaked, (
                f"create left {len(leaked)} backend(s) idle-in-transaction on"
                f" users (#908): {leaked}"
            )
        finally:
            repo.close()
