"""#891 — the list door must not be the existence oracle the mutation door
refuses to be.

Born RED against the unscoped ``get_all_active()``: Tenant C's dashboard
listed every account on the deployment (names, handles, UUIDs) while Switch
on any of them correctly 400'd — isolation enforced on mutations, disclosed
on reads.

Real stack below the auth boundary, deliberately: the ONLY mock is the
initData HMAC (`validate_init_data`), which sits ABOVE the defect's layer.
Tenant resolution, membership, the service, the repository and the SQL all
run against the real test database — a wholesale service mock here would sit
exactly on top of the bug, which is how the #874 no-op read as coverage.

The agreement test is the durable half of virgil's framing: for every
(chat, account) pair, list-membership must equal what the mutation door
would rule — one ownership derivation, two consumers, pinned together so
the read and write sides cannot drift apart again.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from src.api.app import app

pytestmark = [pytest.mark.integration]


@pytest.fixture(autouse=True)
def _authorize_membership_by_default():
    """Shadows the api conftest's autouse boundary mocks with a no-op: this
    file runs the REAL resolution + membership stack against the test
    database — the conftest's mocked SettingsService/MembershipService sit
    exactly at the layer whose honesty this test exists to check."""
    yield


@pytest.fixture
def tenancy_world(setup_test_database):
    """Two committed tenants on the session's test database.

    Tenant A owns two accounts (one by active pointer + stamp, one by stamp
    alone); Tenant C owns nothing. Committed — the route side opens its own
    sessions, and uncommitted seeds would be the vacuity trap the L.1 suite
    documents. Torn down row-by-row in FK order.
    """
    engine = setup_test_database
    if engine is None:
        pytest.skip("Integration test requires a database")

    from src.models.api_token import ApiToken
    from src.models.chat_settings import ChatSettings
    from src.models.instagram_account import InstagramAccount
    from src.models.user import User
    from src.models.user_chat_membership import UserChatMembership

    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = factory()
    made = []

    def _add(obj):
        session.add(obj)
        made.append(obj)
        return obj

    tag = uuid.uuid4().hex[:8]
    chat_a_tg, chat_c_tg = -910_000_001, -910_000_003
    user_a_tg, user_c_tg = 910_101, 910_103

    chat_a = _add(ChatSettings(telegram_chat_id=chat_a_tg))
    chat_c = _add(ChatSettings(telegram_chat_id=chat_c_tg))
    user_a = _add(User(telegram_user_id=user_a_tg, telegram_username=f"a-{tag}"))
    user_c = _add(User(telegram_user_id=user_c_tg, telegram_username=f"c-{tag}"))
    session.flush()

    _add(
        UserChatMembership(
            user_id=user_a.id, chat_settings_id=chat_a.id, is_active=True
        )
    )
    _add(
        UserChatMembership(
            user_id=user_c.id, chat_settings_id=chat_c.id, is_active=True
        )
    )

    acct1 = _add(
        InstagramAccount(
            display_name=f"Main {tag}",
            instagram_username=f"audit_main_{tag}",
            instagram_account_id=f"1780{tag}",
            is_active=True,
        )
    )
    acct2 = _add(
        InstagramAccount(
            display_name=f"Second {tag}",
            instagram_username=f"audit_second_{tag}",
            instagram_account_id=f"1781{tag}",
            is_active=True,
        )
    )
    session.flush()

    for acct in (acct1, acct2):
        _add(
            ApiToken(
                service_name="instagram",
                token_type="access_token",
                instagram_account_id=acct.id,
                chat_settings_id=chat_a.id,
                token_value=f"enc-{tag}-{acct.instagram_username}",
                issued_at=datetime.utcnow(),
            )
        )
    chat_a.active_instagram_account_id = acct1.id
    session.commit()

    world = {
        "factory": factory,
        "a": {"chat_tg": chat_a_tg, "user_tg": user_a_tg, "cs_id": str(chat_a.id)},
        "c": {"chat_tg": chat_c_tg, "user_tg": user_c_tg, "cs_id": str(chat_c.id)},
        "accounts": {str(acct1.id), str(acct2.id)},
        "chat_a": chat_a,
        "chat_c": chat_c,
    }
    try:
        with patch("src.config.database.SessionLocal", factory):
            yield world
    finally:
        for obj in reversed(made):
            session.delete(obj)
        session.commit()
        session.close()


def _list_accounts_as(world, tenant) -> dict:
    """Hit the real route as *tenant*, mocking only the initData HMAC."""
    t = world[tenant]
    client = TestClient(app)
    with patch(
        "src.api.routes.onboarding.helpers.validate_init_data",
        return_value={"user_id": t["user_tg"], "chat_id": t["chat_tg"]},
    ):
        return client.get(
            "/api/onboarding/accounts",
            params={"init_data": "signed", "chat_id": t["chat_tg"]},
        )


class TestTheListDoorIsNotAnExistenceOracle:
    def test_the_owner_still_sees_its_accounts(self, tenancy_world):
        """Positive control first: the fix must not blank the owner's list —
        an empty-list pass for tenant C proves nothing if A sees nothing
        either."""
        resp = _list_accounts_as(tenancy_world, "a")
        assert resp.status_code == 200
        ids = {item["id"] for item in resp.json()["accounts"]}
        assert ids == tenancy_world["accounts"]

    def test_an_unrelated_tenant_sees_nothing(self, tenancy_world):
        """The #891 door, exactly as clicked: tenant C lists, and must not
        learn that tenant A's accounts exist at all."""
        resp = _list_accounts_as(tenancy_world, "c")
        assert resp.status_code == 200
        body = resp.json()
        leaked = [i for i in body["accounts"] if i["id"] in tenancy_world["accounts"]]
        assert leaked == [], (
            "cross-tenant disclosure: tenant C's list contains accounts it"
            f" does not own: {leaked}"
        )
        assert body["accounts"] == []


class TestReadAndWriteAgreeOnOwnership:
    def test_list_membership_equals_the_mutation_doors_ruling(self, tenancy_world):
        """The asymmetry virgil exploited, pinned as a property: for every
        (chat, account) pair, appearing in the tenant's list must equal what
        `_require_account_ownership` — the door that guards Switch/Remove —
        would rule. One ownership derivation, two consumers; if the read and
        write spellings ever drift, this is the test that goes red."""
        from src.services.core.instagram_account_service import (
            InstagramAccountService,
        )

        for tenant in ("a", "c"):
            chat_row = tenancy_world["chat_" + tenant]
            resp = _list_accounts_as(tenancy_world, tenant)
            assert resp.status_code == 200
            listed = {i["id"] for i in resp.json()["accounts"]}
            with InstagramAccountService() as svc:
                for account_id in tenancy_world["accounts"]:
                    try:
                        svc._require_account_ownership(
                            account_id, chat_row.telegram_chat_id, "audit"
                        )
                        owns = True
                    except ValueError:
                        owns = False
                    assert (account_id in listed) == owns, (
                        f"tenant {tenant}: list and mutation door disagree on"
                        f" {account_id} (listed={account_id in listed},"
                        f" owns={owns})"
                    )
