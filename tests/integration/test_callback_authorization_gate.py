"""Integration test: the central callback authorization gate.

The Telegram callback dispatcher (``TelegramService._handle_callback``) is the
one chokepoint every inline-button tap flows through. ``callback_data`` is
client-supplied, so authorization cannot rely on it — it is resolved instead
from the one field the client cannot choose, ``query.message.chat_id`` (the chat
the tapped message actually lives in). This is the callback-layer mirror of the
web ``_validate_request`` gate.

The gate fails closed when:

* the caller is not an active member of the chat the callback fired in, or
* a queue id carried in ``callback_data`` resolves to a row owned by a
  *different* instance than the caller's.

Ownership is owned-OR-NULL: a legacy row with no instance stamp is allowed
through (so the gate does not depend on the ownership backfill); only a
populated, foreign instance id is refused.

Real DB. Rows are created through the production repositories against the
``.env.test`` database (routed via ``_route_repos_to_test_db``) — a separate
connection from the conftest ``test_db`` rollback fixture, so the ``seed``
factory deletes every row it creates (children first) and leaves zero residue
for later tests in the session.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from src.repositories.chat_settings_repository import ChatSettingsRepository
from src.repositories.media_repository import MediaRepository
from src.repositories.membership_repository import MembershipRepository
from src.repositories.queue_repository import QueueRepository
from src.repositories.user_repository import UserRepository
from src.services.core.telegram_service import TelegramService
from src.services.core.telegram_accounts import TelegramAccountHandlers
from src.repositories.tenant_scope import SYSTEM_SCOPE


@pytest.fixture(autouse=True)
def _route_repos_to_test_db(setup_test_database, monkeypatch):
    """Route the production repo session factory at the current-schema test DB.

    Repositories open sessions through ``get_db()`` → the module-global
    ``SessionLocal``; rebinding that sessionmaker to the conftest test engine
    sends every repo session to the current-schema ``.env.test`` DB without
    modifying ``src``. Mirrors the queue-claim concurrency integration test.
    """
    if setup_test_database is None:
        pytest.skip("Database not available - skipping integration test")

    import src.config.database as db_module

    monkeypatch.setattr(
        db_module,
        "SessionLocal",
        sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=setup_test_database,
            expire_on_commit=False,
        ),
    )
    yield


@pytest.fixture(scope="module", autouse=True)
def _dispose_pool_after_module(setup_test_database):
    """Return every pooled connection this module opened before later files run.

    These tests commit through the shared session-scoped engine; disposing its
    idle connections afterward hands a clean pool to the timing-sensitive
    concurrency tests that run later in the session.
    """
    yield
    if setup_test_database is not None:
        setup_test_database.dispose()


def _unique_chat_id() -> int:
    """A unique group chat id (Telegram groups are large negatives)."""
    return -(uuid4().int % (10**12)) - 1


def _unique_user_id() -> int:
    return uuid4().int % (10**11)


class _Seed:
    """Create instance/user/membership/queue rows and remember them for purge."""

    def __init__(self) -> None:
        self.users: list[str] = []
        self.instances: list[str] = []
        self.memberships: list[tuple[str, str]] = []
        self.media: list[str] = []
        self.queue: list[str] = []
        self.accounts: list[str] = []

    def instance(self, telegram_chat_id: int | None = None) -> str:
        if telegram_chat_id is None:
            telegram_chat_id = _unique_chat_id()
        repo = ChatSettingsRepository()
        try:
            cs_id = str(repo.get_or_create(telegram_chat_id).id)
        finally:
            repo.close()
        self.instances.append(cs_id)
        return cs_id

    def user(self, telegram_user_id: int | None = None) -> tuple[int, str]:
        tid = telegram_user_id if telegram_user_id is not None else _unique_user_id()
        repo = UserRepository()
        try:
            uid = str(
                repo.create(
                    telegram_user_id=tid,
                    telegram_username=f"user{tid}",
                    telegram_first_name="Test",
                    telegram_last_name="User",
                ).id
            )
        finally:
            repo.close()
        self.users.append(uid)
        return tid, uid

    def membership(self, user_id: str, cs_id: str) -> None:
        repo = MembershipRepository()
        try:
            repo.create_membership(user_id=user_id, chat_settings_id=cs_id)
        finally:
            repo.close()
        self.memberships.append((user_id, cs_id))

    def instagram_account(self, cs_id: str) -> str:
        """An account this chat OWNS, via a token stamped with its id.

        `instagram_accounts` carries no tenant column — ownership is DERIVED
        (`_ownership_predicate`), and an ApiToken carrying both the account id
        and the chat id is one of the two routes that derivation recognises.
        Built that way rather than by poking the chat's active pointer, so the
        fixture exercises how ownership actually works rather than the easiest
        route to make a test pass. The token needs BOTH columns set, which no
        single repository method does, so it is inserted directly.
        """
        from src.repositories.instagram_account_repository import (
            InstagramAccountRepository,
        )

        repo = InstagramAccountRepository()
        try:
            acct_id = str(
                repo.create(
                    display_name=f"acct-{uuid4().hex[:8]}",
                    instagram_account_id=f"ig-{uuid4().hex[:10]}",
                    instagram_username=f"handle_{uuid4().hex[:6]}",
                ).id
            )
        finally:
            repo.close()

        import src.config.database as db_module

        session = db_module.SessionLocal()
        try:
            session.execute(
                text(
                    "INSERT INTO api_tokens (id, service_name, token_type,"
                    " token_value, issued_at, instagram_account_id,"
                    " chat_settings_id)"
                    " VALUES (gen_random_uuid(), 'instagram', 'access_token',"
                    " 'tok', now(), :acct, :cs)"
                ),
                {"acct": acct_id, "cs": cs_id},
            )
            session.commit()
        finally:
            session.close()

        self.accounts.append(acct_id)
        return acct_id

    def queue_item(self, cs_id: str | None) -> tuple[str, str]:
        media_repo = MediaRepository()
        try:
            media_id = str(
                media_repo.create(
                    file_path=f"/test/callback-gate/{uuid4()}.jpg",
                    file_name="gate.jpg",
                    file_hash=uuid4().hex,
                    file_size_bytes=2048,
                    mime_type="image/jpeg",
                    chat_settings_id=SYSTEM_SCOPE,
                ).id
            )
        finally:
            media_repo.close()

        queue_repo = QueueRepository()
        try:
            queue_id = str(
                queue_repo.create(
                    media_item_id=media_id,
                    scheduled_for=datetime.now(timezone.utc) - timedelta(minutes=1),
                    chat_settings_id=cs_id if cs_id is not None else SYSTEM_SCOPE,
                ).id
            )
        finally:
            queue_repo.close()
        self.media.append(media_id)
        self.queue.append(queue_id)
        return media_id, queue_id

    def purge(self) -> None:
        """Delete every created row (children first) via one routed session.

        ``user_id``-keyed rather than membership-pair so an auto-provisioned
        membership (created by ``_get_or_create_user`` on the dispatch path) is
        swept too.
        """
        import src.config.database as db_module

        session = db_module.SessionLocal()
        try:
            # audit_log FK-references both users and chat_settings (membership
            # creation writes audit rows) — clear it before its parents.
            for uid in self.users:
                session.execute(
                    text("DELETE FROM audit_log WHERE changed_by_user_id = :u"),
                    {"u": uid},
                )
            for cs_id in self.instances:
                session.execute(
                    text("DELETE FROM audit_log WHERE chat_settings_id = :i"),
                    {"i": cs_id},
                )
            # api_tokens FK-references both instagram_accounts and
            # chat_settings, so it clears before either parent.
            for acct in self.accounts:
                session.execute(
                    text("DELETE FROM api_tokens WHERE instagram_account_id = :a"),
                    {"a": acct},
                )
            for cs_id in self.instances:
                session.execute(
                    text("DELETE FROM api_tokens WHERE chat_settings_id = :i"),
                    {"i": cs_id},
                )
            for acct in self.accounts:
                session.execute(
                    text(
                        "UPDATE chat_settings SET active_instagram_account_id = NULL"
                        " WHERE active_instagram_account_id = :a"
                    ),
                    {"a": acct},
                )
                session.execute(
                    text("DELETE FROM instagram_accounts WHERE id = :a"), {"a": acct}
                )
            for uid in self.users:
                session.execute(
                    text("DELETE FROM user_chat_memberships WHERE user_id = :u"),
                    {"u": uid},
                )
            for queue_id in self.queue:
                session.execute(
                    text("DELETE FROM posting_queue WHERE id = :i"), {"i": queue_id}
                )
            for media_id in self.media:
                session.execute(
                    text("DELETE FROM media_items WHERE id = :i"), {"i": media_id}
                )
            for uid in self.users:
                session.execute(text("DELETE FROM users WHERE id = :i"), {"i": uid})
            for cs_id in self.instances:
                session.execute(
                    text("DELETE FROM chat_settings WHERE id = :i"), {"i": cs_id}
                )
            session.commit()
        finally:
            session.close()


@pytest.fixture
def seed():
    """A row factory that purges everything it created after the test."""
    s = _Seed()
    try:
        yield s
    finally:
        s.purge()


@pytest.fixture
def service():
    """A real TelegramService (no bot/network; __init__ wires repos only).

    Closed after each test so its repo sessions are returned to the pool.
    """
    svc = TelegramService()
    try:
        yield svc
    finally:
        svc.close()


@pytest.fixture
def accounts(service):
    """The account-callback handler, built directly.

    ``TelegramService.__init__`` wires repos but sub-handlers are created in
    ``initialize()`` (which needs a live bot), so the gate tests never touch
    ``service.accounts``. The handler only needs the service — construct it."""
    return TelegramAccountHandlers(service)


def _make_query(from_user_id: int, chat_id: int, data: str) -> AsyncMock:
    """A stand-in Telegram callback query.

    Async methods (answer, edit_message_*) are AsyncMocks; ``from_user`` and
    ``message`` are plain Mocks so attribute reads are values, not coroutines.
    """
    query = AsyncMock()
    query.data = data
    query.from_user = Mock()
    query.from_user.id = from_user_id
    query.from_user.username = f"user{from_user_id}"
    query.from_user.first_name = "Test"
    query.from_user.last_name = "User"
    query.message = Mock()
    query.message.chat_id = chat_id
    query.message.message_id = 1
    query.message.chat = Mock()
    query.message.chat.type = "supergroup"
    return query


def _queue_status(queue_id: str) -> str | None:
    repo = QueueRepository()
    try:
        row = repo.get_by_id(queue_id, chat_settings_id=SYSTEM_SCOPE)
        return row.status if row else None
    finally:
        repo.close()


# ---------------------------------------------------------------------------
# Gate decision — _authorize_callback
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestCallbackAuthorizationDecision:
    """The gate's allow/deny decision against real instance + queue rows."""

    async def test_member_acting_on_own_instance_item_is_authorized(
        self, service, seed
    ):
        """A member acting on a queue item owned by their own instance passes."""
        chat_id = _unique_chat_id()
        cs_id = seed.instance(chat_id)
        caller_tid, caller_uid = seed.user()
        seed.membership(caller_uid, cs_id)
        _, queue_id = seed.queue_item(cs_id)
        query = _make_query(caller_tid, chat_id, f"posted:{queue_id}")

        allowed = await service._authorize_callback("posted", queue_id, query)
        assert allowed is True

    async def test_callback_for_item_in_another_instance_is_refused(
        self, service, seed
    ):
        """A queue id owned by a different instance is refused (owned check)."""
        caller_chat = _unique_chat_id()
        caller_cs = seed.instance(caller_chat)
        other_cs = seed.instance()
        caller_tid, caller_uid = seed.user()
        seed.membership(caller_uid, caller_cs)
        _, other_queue_id = seed.queue_item(other_cs)
        query = _make_query(caller_tid, caller_chat, f"posted:{other_queue_id}")

        allowed = await service._authorize_callback("posted", other_queue_id, query)
        assert allowed is False
        query.answer.assert_awaited()  # caller answered, no handler ran

    async def test_caller_without_membership_is_refused(self, service, seed):
        """A caller with no active membership in the chat is refused.

        The queue item is owned by the caller's own chat, so ownership would
        pass — the refusal can only come from the membership check.
        """
        chat_id = _unique_chat_id()
        cs_id = seed.instance(chat_id)
        caller_tid, _ = seed.user()  # user exists, but no membership row
        _, queue_id = seed.queue_item(cs_id)
        query = _make_query(caller_tid, chat_id, f"posted:{queue_id}")

        allowed = await service._authorize_callback("posted", queue_id, query)
        assert allowed is False

    async def test_owned_or_null_allows_legacy_unstamped_item(self, service, seed):
        """A queue row with no instance stamp is allowed (no backfill dependency)."""
        chat_id = _unique_chat_id()
        cs_id = seed.instance(chat_id)
        caller_tid, caller_uid = seed.user()
        seed.membership(caller_uid, cs_id)
        _, legacy_queue_id = seed.queue_item(None)  # NULL instance stamp
        query = _make_query(caller_tid, chat_id, f"posted:{legacy_queue_id}")

        allowed = await service._authorize_callback("posted", legacy_queue_id, query)
        assert allowed is True


# ---------------------------------------------------------------------------
# End-to-end dispatch — _handle_callback must not reach the handler on refusal
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestCallbackDispatchGating:
    """The dispatcher must not invoke a handler for a refused callback."""

    async def test_foreign_instance_callback_does_not_reach_handler(
        self, service, seed
    ):
        """A queue id from another instance never reaches the autopost handler.

        Proves zero state mutation and zero media action: the handler — the only
        code that mutates the row or posts media — is never called, and the
        queue row is left untouched.
        """
        caller_chat = _unique_chat_id()
        caller_cs = seed.instance(caller_chat)
        other_cs = seed.instance()
        caller_tid, caller_uid = seed.user()
        seed.membership(caller_uid, caller_cs)
        _, other_queue_id = seed.queue_item(other_cs)

        spy = AsyncMock()
        service._callback_dispatch = {"autopost": spy}
        query = _make_query(caller_tid, caller_chat, f"autopost:{other_queue_id}")
        update = Mock()
        update.callback_query = query

        await service._handle_callback(update, Mock())

        spy.assert_not_called()
        assert _queue_status(other_queue_id) == "pending"

    async def test_own_instance_callback_reaches_handler(self, service, seed):
        """A member's callback on their own instance item still dispatches."""
        chat_id = _unique_chat_id()
        cs_id = seed.instance(chat_id)
        caller_tid, caller_uid = seed.user()
        seed.membership(caller_uid, cs_id)
        _, queue_id = seed.queue_item(cs_id)

        spy = AsyncMock()
        service._callback_dispatch = {"posted": spy}
        query = _make_query(caller_tid, chat_id, f"posted:{queue_id}")
        update = Mock()
        update.callback_query = query

        await service._handle_callback(update, Mock())

        spy.assert_awaited_once()


# ---------------------------------------------------------------------------
# #895 — the sap:/btp: short-prefix bypass
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestShortPrefixCallbacksAreTenantScoped:
    """The `sap:`/`btp:` callbacks carry a SHORT queue-id prefix, which the
    gate's exact-UUID ownership resolution cannot match — so before #895 they
    were absent from `_QUEUE_ID_ACTIONS` entirely (no membership, no ownership)
    and the handler read the prefix cross-tenant with SYSTEM_SCOPE, rebuilding
    a FOREIGN tenant's post content into the caller's chat.

    Born RED against that: the handler must refuse a foreign prefix exactly as
    the mutation door would, and the gate must now enforce membership on these.
    """

    async def test_back_to_post_refuses_a_foreign_tenants_queue_prefix(
        self, service, accounts, seed
    ):
        """The subject bypass: a member of chat C taps `btp:` on chat A's card.

        Positive control first — the OWNER's prefix rebuilds — so a refusal for
        C is not a refusal for everyone."""
        # Tenant A owns the queue item; tenant C is unrelated, owns nothing.
        chat_a = _unique_chat_id()
        cs_a = seed.instance(chat_a)
        a_tid, a_uid = seed.user()
        seed.membership(a_uid, cs_a)
        _, a_queue_id = seed.queue_item(cs_a)

        chat_c = _unique_chat_id()
        cs_c = seed.instance(chat_c)
        c_tid, c_uid = seed.user()
        seed.membership(c_uid, cs_c)

        prefix = a_queue_id[:8]

        # Owner (A) in chat A: the prefix resolves and the workflow rebuilds.
        rebuilt: list[str] = []
        accounts.rebuild_posting_workflow = AsyncMock(
            side_effect=lambda qid, *a, **k: rebuilt.append(qid)
        )
        owner_q = _make_query(a_tid, chat_a, f"btp:{prefix}")
        await accounts.handle_back_to_post(prefix, Mock(), owner_q)
        assert rebuilt == [a_queue_id], "positive control: the owner still rebuilds"

        # Unrelated tenant C in chat C tapping A's prefix: REFUSED, no rebuild,
        # neutral "not found" — A's content never reaches C's chat.
        rebuilt.clear()
        foreign_q = _make_query(c_tid, chat_c, f"btp:{prefix}")
        await accounts.handle_back_to_post(prefix, Mock(), foreign_q)

        assert rebuilt == [], (
            "cross-tenant disclosure: chat C rebuilt chat A's post content"
            f" from prefix {prefix}"
        )
        foreign_q.edit_message_caption.assert_awaited()
        caption = foreign_q.edit_message_caption.call_args.kwargs.get("caption", "")
        assert "not found" in caption.lower()

    async def test_the_handler_ownership_equals_the_gates_ruling(
        self, service, accounts, seed
    ):
        """Agreement invariant (the #891 shape): what the prefix handler grants
        must equal the owned-OR-NULL rule — for the owner (own stamp), the
        unrelated tenant (foreign stamp), and a legacy NULL-stamped row (which
        owned-OR-NULL deliberately still permits, so this fix does not break
        single-tenant cards)."""
        chat_a = _unique_chat_id()
        cs_a = seed.instance(chat_a)
        _, a_queue_id = seed.queue_item(cs_a)
        _, legacy_queue_id = seed.queue_item(None)  # no instance stamp

        chat_c = _unique_chat_id()
        seed.instance(chat_c)

        owned = service.queue_repo.get_by_id(a_queue_id, chat_settings_id=SYSTEM_SCOPE)
        legacy = service.queue_repo.get_by_id(
            legacy_queue_id, chat_settings_id=SYSTEM_SCOPE
        )

        # Owner's chat: owns its row, and the legacy NULL row (owned-OR-NULL).
        assert accounts._caller_owns_queue_item(owned, chat_a) is True
        assert accounts._caller_owns_queue_item(legacy, chat_a) is True
        # Unrelated chat: refused the foreign row, still permitted the legacy one.
        assert accounts._caller_owns_queue_item(owned, chat_c) is False
        assert accounts._caller_owns_queue_item(legacy, chat_c) is True
        # Nothing to act on is refused.
        assert accounts._caller_owns_queue_item(None, chat_a) is False

    async def test_a_member_through_the_gate_on_a_prefix_does_not_raise(
        self, service, seed
    ):
        """The regression the first cut shipped (#895 review): a MEMBER passes
        membership and then reaches the gate's ownership branch, which resolved
        `get_by_id(<8-char prefix>)` on a UUID column — a DataError, not a
        clean pass, breaking sap/btp for the OWNER. The non-member case below
        short-circuits before this branch, so only a member exercises it. The
        gate must DEFER a non-UUID payload to the handler, never error."""
        chat_id = _unique_chat_id()
        cs_id = seed.instance(chat_id)
        caller_tid, caller_uid = seed.user()
        seed.membership(caller_uid, cs_id)
        _, queue_id = seed.queue_item(cs_id)

        prefix = queue_id[:8]
        query = _make_query(caller_tid, chat_id, f"btp:{prefix}")
        # Must return a decision, not raise. (Ownership for prefixes is the
        # handler's; the gate only enforces membership here.)
        allowed = await service._authorize_callback("btp", prefix, query)
        assert allowed is True

        # The compound sap payload (`q:a`) is likewise not a UUID.
        sap_query = _make_query(caller_tid, chat_id, f"sap:{prefix}:abcd1234")
        allowed_sap = await service._authorize_callback(
            "sap", f"{prefix}:abcd1234", sap_query
        )
        assert allowed_sap is True

    async def test_a_non_member_is_refused_at_the_gate_for_btp(self, service, seed):
        """sap/btp are now in the gated set (#895): a non-member of the card's
        chat is refused BEFORE any handler resolves the prefix — the hole that
        let a non-member trigger these at all."""
        chat_id = _unique_chat_id()
        cs_id = seed.instance(chat_id)
        _, queue_id = seed.queue_item(cs_id)
        caller_tid, _ = seed.user()  # exists, but no membership in chat_id

        prefix = queue_id[:8]
        query = _make_query(caller_tid, chat_id, f"btp:{prefix}")
        allowed = await service._authorize_callback("btp", prefix, query)

        assert allowed is False
        query.answer.assert_awaited()


class TestAccountRemoveConfirmDoesNotDiscloseAForeignAccount:
    """#923 — a guarded write behind an unguarded read is still a disclosure.

    `handle_account_remove_execute` passes `chat_id` into `deactivate_account`,
    which enforces ownership and refuses. The CONFIRM step that precedes it read
    the account unscoped and rendered its display name and username into the
    card, so a tenant could see an account the very next step would decline to
    touch. Two tenants, one account, and the assertion is on the DISCLOSURE
    rather than on the refusal — a test asserting only "it answered not found"
    would pass even while the card leaked.
    """

    @pytest.mark.asyncio
    async def test_a_foreign_account_is_not_disclosed_in_the_confirmation_card(
        self, accounts, seed
    ):
        owner_cs = seed.instance()
        intruder_chat = _unique_chat_id()
        seed.instance(intruder_chat)
        account_id = seed.instagram_account(owner_cs)

        query = _make_query(
            _unique_user_id(), intruder_chat, f"account_remove:{account_id}"
        )
        await accounts.handle_account_remove_confirm(account_id, None, query)

        query.edit_message_text.assert_not_called()
        rendered = " ".join(str(c) for c in query.edit_message_text.call_args_list)
        assert "handle_" not in rendered and "acct-" not in rendered, (
            "the card must not carry the foreign account's username or display "
            "name — that disclosure is the defect, not the refusal"
        )
        query.answer.assert_awaited()

    @pytest.mark.asyncio
    async def test_the_OWNER_still_sees_their_own_confirmation_card(
        self, accounts, seed
    ):
        """Paired positive control. Without it, a handler that refused
        EVERYTHING would pass the test above while breaking the feature — and
        a gate that blocks the legitimate path gets reverted, not fixed."""
        owner_chat = _unique_chat_id()
        owner_cs = seed.instance(owner_chat)
        account_id = seed.instagram_account(owner_cs)

        query = _make_query(
            _unique_user_id(), owner_chat, f"account_remove:{account_id}"
        )
        await accounts.handle_account_remove_confirm(account_id, None, query)

        query.edit_message_text.assert_called_once()
        rendered = str(query.edit_message_text.call_args)
        assert "Confirm Remove Account" in rendered
        assert "acct-" in rendered, "the owner must see their own account named"

    @pytest.mark.asyncio
    async def test_an_INVENTED_id_is_refused_the_same_way_as_a_foreign_one(
        self, accounts, seed
    ):
        """The refusals must be indistinguishable, or the guard becomes the
        existence oracle it closes — `_require_account_ownership`'s own rule."""
        chat = _unique_chat_id()
        seed.instance(chat)
        invented = str(uuid4())

        query = _make_query(_unique_user_id(), chat, f"account_remove:{invented}")
        await accounts.handle_account_remove_confirm(invented, None, query)

        query.edit_message_text.assert_not_called()
        query.answer.assert_awaited_with("Account not found", show_alert=True)
