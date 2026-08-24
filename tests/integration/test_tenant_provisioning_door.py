"""The user_id-keyed tenant provisioning door (§9 of the tenant-anchor doc).

Adversarial by construction: the concurrent-mint test is built to go RED when
the FOR UPDATE serialization is removed, and the scheduler test to go RED when
`get_all_active`'s NULL-chat exclusion is removed. The race window between the
door's lookup and its mint is made deterministic through
`_mint_race_window()` — a no-op seam in the repository this suite replaces
with a barrier, so "two requests inside the window" is a constructed fact,
not a timing hope.
"""

import threading
import uuid

import pytest
from sqlalchemy.orm import sessionmaker

from src.exceptions.tenancy import TenantProvisioningError
from src.models.chat_settings import ChatSettings
from src.models.user import User
from src.models.user_chat_membership import UserChatMembership


@pytest.fixture(autouse=True)
def relaxed_chat_id(routed_engine):
    """Apply the telegram_chat_id relax to the test schema.

    The production DDL is sequenced behind the #840 renumber, and the model
    stays at production truth (NOT NULL) until that migration lands, so the
    schema-parity gate keeps meaning what it says. This fixture applies the
    SAME statement the migration will carry, so this suite exercises the
    armed world the door is built for."""
    from sqlalchemy import text

    with routed_engine.connect() as conn:
        conn.execute(
            text(
                "ALTER TABLE chat_settings ALTER COLUMN telegram_chat_id DROP NOT NULL"
            )
        )
        conn.commit()


@pytest.fixture
def door(routed_engine):
    """A SettingsService whose repositories run against the test database."""
    from src.services.core.settings_service import SettingsService

    return SettingsService()


@pytest.fixture
def make_user(routed_engine):
    """Insert a user row directly and return its id (as str)."""
    session_factory = sessionmaker(bind=routed_engine)

    def _make() -> str:
        session = session_factory()
        try:
            user = User(telegram_user_id=uuid.uuid4().int % 10**12)
            session.add(user)
            session.commit()
            return str(user.id)
        finally:
            session.close()

    return _make


def _null_chat_tenant_count(engine) -> int:
    session = sessionmaker(bind=engine)()
    try:
        return (
            session.query(ChatSettings)
            .filter(ChatSettings.telegram_chat_id.is_(None))
            .count()
        )
    finally:
        session.close()


def _personal_tenants_of(engine, user_id: str):
    session = sessionmaker(bind=engine)()
    try:
        return (
            session.query(ChatSettings)
            .join(
                UserChatMembership,
                UserChatMembership.chat_settings_id == ChatSettings.id,
            )
            .filter(
                UserChatMembership.user_id == user_id,
                ChatSettings.telegram_chat_id.is_(None),
            )
            .all()
        )
    finally:
        session.close()


class TestConcurrentMint:
    def test_two_simultaneous_requests_mint_exactly_one_tenant(
        self, door, make_user, routed_engine, monkeypatch
    ):
        """Both requests are HELD INSIDE the lookup->mint window together, then
        released. With the FOR UPDATE in place only one request can be in the
        window at a time (the second blocks on the user-row lock, the barrier
        times out, the first proceeds alone); with it removed, both enter,
        both mint, and this test fails on the row count.
        """
        user_id = make_user()

        barrier = threading.Barrier(2, timeout=3.0)

        def race_window():
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                # Serialized world: the second thread never reached the
                # window, so the barrier times out and the holder proceeds.
                pass

        import src.repositories.chat_settings_repository as repo_module

        monkeypatch.setattr(repo_module, "_mint_race_window", race_window)

        results: list = []
        errors: list = []

        def call():
            try:
                results.append(door.provision_personal(user_id))
            except Exception as exc:  # noqa: BLE001 — the assertion below reports it
                errors.append(exc)

        threads = [threading.Thread(target=call) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert not errors, f"door raised under concurrency: {errors!r}"
        tenants = _personal_tenants_of(routed_engine, user_id)
        assert len(tenants) == 1, (
            f"double mint: {len(tenants)} NULL-chat tenants for one user — "
            "the FOR UPDATE serialization is not holding"
        )
        assert len(results) == 2
        assert str(results[0].id) == str(results[1].id)

        memberships = _owner_memberships(routed_engine, user_id)
        assert len(memberships) == 1

    def test_idempotent_sequential_calls_return_same_tenant(
        self, door, make_user, routed_engine
    ):
        user_id = make_user()

        first = door.provision_personal(user_id)
        second = door.provision_personal(user_id)

        assert str(first.id) == str(second.id)
        assert len(_personal_tenants_of(routed_engine, user_id)) == 1


def _owner_memberships(engine, user_id: str):
    session = sessionmaker(bind=engine)()
    try:
        return (
            session.query(UserChatMembership)
            .filter(
                UserChatMembership.user_id == user_id,
                UserChatMembership.instance_role == "owner",
                UserChatMembership.is_active.is_(True),
            )
            .all()
        )
    finally:
        session.close()


class TestMintShape:
    def test_minted_tenant_is_chatless_unpaused_and_not_onboarded(
        self, door, make_user, routed_engine
    ):
        """Web tenants are not Telegram-posting-ready: no chat id, not
        onboarding_completed (the flag get_or_create's bootstrap sets True for
        chat tenants), not paused (pause is user-facing state, not a hiding
        mechanism — the sweep exclusion below is structural instead)."""
        user_id = make_user()

        tenant = door.provision_personal(user_id)

        assert tenant.telegram_chat_id is None
        assert tenant.onboarding_completed is False
        assert tenant.is_paused is False

        memberships = _owner_memberships(routed_engine, user_id)
        assert len(memberships) == 1
        assert str(memberships[0].chat_settings_id) == str(tenant.id)

    def test_unknown_user_refused_typed_and_mints_nothing(self, door, routed_engine):
        before = _null_chat_tenant_count(routed_engine)

        with pytest.raises(TenantProvisioningError) as exc_info:
            door.provision_personal(str(uuid.uuid4()))

        assert exc_info.value.reason == "unknown_user"
        assert _null_chat_tenant_count(routed_engine) == before


class TestSchedulerExclusion:
    def test_null_chat_tenant_never_enters_the_sweep(
        self, door, make_user, routed_engine
    ):
        """Even with worst-case flags — onboarding_completed forced True, the
        OR branch get_all_active admits rows through — a chat-less tenant must
        not appear in the Telegram posting sweep. Structural exclusion, not
        flag hygiene: remove the telegram_chat_id IS NOT NULL filter and this
        goes red."""
        user_id = make_user()
        tenant = door.provision_personal(user_id)

        session = sessionmaker(bind=routed_engine)()
        try:
            row = session.get(ChatSettings, tenant.id)
            row.onboarding_completed = True
            session.commit()
        finally:
            session.close()

        swept = door.get_all_active_chats()
        assert str(tenant.id) not in {str(c.id) for c in swept}

    def test_positive_control_chat_tenant_is_swept(self, door, routed_engine):
        """The exclusion must not be an exclude-everything artifact: a normal
        chat tenant with the same flags IS in the sweep."""
        chat_tenant = door.provision(-1009900112233)

        session = sessionmaker(bind=routed_engine)()
        try:
            row = session.get(ChatSettings, chat_tenant.id)
            row.onboarding_completed = True
            session.commit()
        finally:
            session.close()

        swept = door.get_all_active_chats()
        assert str(chat_tenant.id) in {str(c.id) for c in swept}


class TestNoneNeverResolvesAPersonalTenant:
    def test_chat_lookups_handed_none_refuse_rather_than_serve(
        self, door, make_user, routed_engine
    ):
        """SQLAlchemy compiles `== None` to IS NULL, so with the column
        nullable an unguarded chat lookup handed None would return an
        ARBITRARY personal tenant — another tenant's settings for a missing
        chat id. Remove the get_by_chat_id None-guard and this goes red."""
        user_id = make_user()
        tenant = door.provision_personal(user_id)

        from src.exceptions.tenancy import TenantResolutionError
        from src.repositories.chat_settings_repository import (
            ChatSettingsRepository,
        )

        repo = ChatSettingsRepository()
        assert repo.get_by_chat_id(None) is None, (
            f"None resolved to personal tenant {tenant.id} — cross-tenant fail-open"
        )
        with pytest.raises(TenantResolutionError) as exc_info:
            repo.require_by_chat_id(None)
        assert exc_info.value.reason == "unknown_binding"


class TestGetOrCreateGuard:
    def test_get_or_create_refuses_none_rather_than_minting(self, door, routed_engine):
        """CREATE-ALWAYS on None was the measured failure shape: the `= NULL`
        lookup never matches, so every call minted a fresh tenant. The guard
        turns that silent flood into a loud contract error."""
        before = _null_chat_tenant_count(routed_engine)

        with pytest.raises(ValueError):
            door.provision(None)

        assert _null_chat_tenant_count(routed_engine) == before
