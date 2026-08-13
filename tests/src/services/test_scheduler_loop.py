"""Tests for the scheduler loop's periodic sub-task ticks."""

import pytest
from unittest.mock import AsyncMock, Mock, patch

from telegram.error import ChatMigrated

from src.exceptions.instagram import TokenRevokedError
from src.exceptions.telegram import ChatMigratedError


@pytest.mark.unit
class TestTokenRefreshTick:
    """Test the _token_refresh_tick function in the scheduler loop."""

    @pytest.fixture
    def mock_token_refresh_service(self):
        service = Mock()
        service.refresh_all_instagram_tokens = AsyncMock()
        service.cleanup_transactions = Mock()
        return service

    @pytest.mark.asyncio
    async def test_tick_calls_refresh_all(self, mock_token_refresh_service):
        """Test that the tick calls refresh_all_instagram_tokens."""
        mock_token_refresh_service.refresh_all_instagram_tokens.return_value = {
            "refreshed": 1,
            "failed": 0,
            "skipped": 2,
        }

        from src.services.core.loops.scheduler_loop import _token_refresh_tick

        await _token_refresh_tick(mock_token_refresh_service)

        mock_token_refresh_service.refresh_all_instagram_tokens.assert_awaited_once()
        mock_token_refresh_service.cleanup_transactions.assert_called_once()

    @pytest.mark.asyncio
    async def test_tick_handles_revoked_error(self, mock_token_refresh_service):
        """Test that TokenRevokedError is caught without crashing."""
        mock_token_refresh_service.refresh_all_instagram_tokens.side_effect = (
            TokenRevokedError("App deauthorized", error_subcode=458)
        )

        from src.services.core.loops.scheduler_loop import _token_refresh_tick

        # Should not raise
        await _token_refresh_tick(mock_token_refresh_service)

        mock_token_refresh_service.cleanup_transactions.assert_called_once()

    @pytest.mark.asyncio
    async def test_tick_handles_generic_error(self, mock_token_refresh_service):
        """Test that generic exceptions are caught without crashing."""
        mock_token_refresh_service.refresh_all_instagram_tokens.side_effect = (
            RuntimeError("DB connection lost")
        )

        from src.services.core.loops.scheduler_loop import _token_refresh_tick

        # Should not raise
        await _token_refresh_tick(mock_token_refresh_service)

        mock_token_refresh_service.cleanup_transactions.assert_called_once()

    @pytest.mark.asyncio
    async def test_tick_cleanup_even_on_error(self, mock_token_refresh_service):
        """Test cleanup_transactions runs even when refresh fails."""
        mock_token_refresh_service.refresh_all_instagram_tokens.side_effect = Exception(
            "boom"
        )

        from src.services.core.loops.scheduler_loop import _token_refresh_tick

        await _token_refresh_tick(mock_token_refresh_service)

        mock_token_refresh_service.cleanup_transactions.assert_called_once()


def _chat(chat_id: int) -> Mock:
    """A minimal active-chat row: the sweeps only read telegram_chat_id."""
    chat = Mock()
    chat.telegram_chat_id = chat_id
    return chat


@pytest.mark.unit
class TestAlertSweepIsolation:
    """One tenant's failure must not end the alert sweep for the rest (#767).

    ``get_all_active()`` orders by ``created_at ASC``, so the iteration order
    is stable across ticks. A tenant that reliably raises therefore aborts the
    sweep at the same position every hour, permanently, and every tenant
    created after it stops being told its media pool is depleting -- the alert
    that exists to stop it running dry. The failure is silent by construction:
    the affected tenants look healthy.

    The tenant most likely to raise is also the one that sorts earliest. A
    group->supergroup migration (#743) strands an *old* chat id, so the
    tenant whose sends raise forever is near the front of the sweep.
    """

    @pytest.fixture
    def active_chats(self):
        return [_chat(-100), _chat(-200), _chat(-300)]

    @pytest.fixture
    def bot(self):
        bot = Mock()
        bot.send_message = AsyncMock()
        return bot

    @pytest.fixture
    def scheduler_service(self, bot):
        service = Mock()
        service.telegram_service.application.bot = bot
        return service

    @pytest.fixture
    def health_check_service(self):
        service = Mock()
        service.check_media_pool_for_chat.return_value = {"warnings": ["memes"]}
        service.format_pool_alert.return_value = "Pool running low"
        service.check_gdrive_token_for_chat.return_value = {"message": "expiring"}
        service.format_token_alert.return_value = "Drive token expiring"
        return service

    @staticmethod
    def _swept(bot) -> list:
        return [call.kwargs["chat_id"] for call in bot.send_message.call_args_list]

    @pytest.mark.asyncio
    async def test_pool_sweep_survives_an_unreachable_tenant(
        self, active_chats, scheduler_service, health_check_service, bot
    ):
        """A raise from the first tenant must not silence the other two."""
        bot.send_message.side_effect = [RuntimeError("chat not found"), None, None]

        from src.services.core.loops.scheduler_loop import _pool_health_tick

        await _pool_health_tick(
            active_chats, scheduler_service, health_check_service, {}
        )

        assert self._swept(bot) == [-100, -200, -300]

    @pytest.mark.asyncio
    async def test_token_sweep_survives_an_unreachable_tenant(
        self, active_chats, scheduler_service, health_check_service, bot
    ):
        """The Drive-token sweep has the same shape and the same duty."""
        bot.send_message.side_effect = [RuntimeError("chat not found"), None, None]

        from src.services.core.loops.scheduler_loop import _token_health_tick

        await _token_health_tick(
            active_chats, scheduler_service, health_check_service, {}
        )

        assert self._swept(bot) == [-100, -200, -300]

    @pytest.mark.asyncio
    async def test_sweep_survives_a_failing_health_check(
        self, active_chats, scheduler_service, health_check_service, bot
    ):
        """The guard covers the whole per-tenant unit, not just the send.

        The send is the trigger the issue names, but the property is that no
        single tenant can end the sweep. A tenant whose pool check itself
        raises -- a malformed settings row, a per-tenant query failure -- has
        exactly the same blast radius, so isolating only the send would leave
        the same bug reachable by a different route.
        """
        health_check_service.check_media_pool_for_chat.side_effect = [
            RuntimeError("malformed settings row"),
            {"warnings": ["memes"]},
            {"warnings": ["memes"]},
        ]

        from src.services.core.loops.scheduler_loop import _pool_health_tick

        await _pool_health_tick(
            active_chats, scheduler_service, health_check_service, {}
        )

        assert self._swept(bot) == [-200, -300]

    @pytest.mark.asyncio
    async def test_failed_tenant_does_not_burn_its_cooldown(
        self, active_chats, scheduler_service, health_check_service, bot
    ):
        """Only a delivered alert starts the 24h cooldown.

        Stamping on failure would convert one bad tick into a day of silence
        for that tenant; leaving it unstamped means the next tick retries.
        """
        pool_alert_last_sent: dict[int, float] = {}
        bot.send_message.side_effect = [RuntimeError("chat not found"), None, None]

        from src.services.core.loops.scheduler_loop import _pool_health_tick

        await _pool_health_tick(
            active_chats,
            scheduler_service,
            health_check_service,
            pool_alert_last_sent,
        )

        assert -100 not in pool_alert_last_sent
        assert {-200, -300} <= set(pool_alert_last_sent)

    @pytest.mark.asyncio
    async def test_migration_is_logged_in_the_recoverable_form(
        self, active_chats, scheduler_service, health_check_service, bot
    ):
        """A swallowed ChatMigrated must still surrender both ids.

        These alerts have no queue item, so there is no posting_history row to
        write the pair to and the log is the only channel available. Rendering
        it through ChatMigratedError keeps it in the one shape a recovery pass
        can parse; free prose would put the new id -- the single fact #743
        needs -- somewhere only a human re-reading logs could find it.
        """
        bot.send_message.side_effect = [ChatMigrated(-1001), None, None]

        from src.services.core.loops.scheduler_loop import _pool_health_tick

        with patch("src.services.core.loops.scheduler_loop.logger") as mock_logger:
            await _pool_health_tick(
                active_chats, scheduler_service, health_check_service, {}
            )

        warnings = " ".join(str(c) for c in mock_logger.warning.call_args_list)
        assert ChatMigratedError.parse_pair(warnings) == (-100, -1001)
        assert self._swept(bot) == [-100, -200, -300]


@pytest.mark.unit
class TestAutoApproveNotificationIsObservable:
    """#782: the auto-approve notify was sent under a bare `except Exception: pass`.

    The post itself succeeds; only the courtesy confirmation fails. So nothing
    downstream looks wrong, and with no log line there is no signal anywhere
    that a tenant has stopped hearing from the bot.

    The shape is #758's, one surface over: a failure that degrades into
    silence is indistinguishable from the thing never having happened. These
    tests pin BOTH sides of that distinction, because a fix that logs on every
    tick would satisfy the first assertion while destroying the signal.
    """

    CHAT = -100123

    def _tick_args(self, send_effect, *, auto_approved=True, with_telegram=True):
        scheduler_service = Mock()
        scheduler_service.process_slot = AsyncMock(
            return_value={
                "posted": True,
                "auto_approved": auto_approved,
                "media_file": "img.jpg",
                "category": "cat",
            }
        )
        if with_telegram:
            bot = Mock()
            bot.send_message = AsyncMock(side_effect=send_effect)
            scheduler_service.telegram_service.application.bot = bot
        else:
            scheduler_service.telegram_service = None

        chat = Mock(telegram_chat_id=self.CHAT)
        settings_service = Mock()
        settings_service.get_all_active_chats.return_value = [chat]
        queue_repo = Mock()
        queue_repo.get_stale_sent.return_value = []
        return scheduler_service, Mock(), settings_service, queue_repo

    async def _run(self, send_effect, **kw):
        from src.services.core.loops.scheduler_loop import _scheduler_tick

        args = self._tick_args(send_effect, **kw)
        with (
            patch("src.services.core.loops.scheduler_loop.session_state") as state,
            patch("src.services.core.loops.scheduler_loop.logger") as log,
        ):
            state.initial_sync_complete = True
            await _scheduler_tick(*args)
        return log

    def _alert_lines(self, log):
        return [str(c.args[0]) for c in log.warning.call_args_list]

    @pytest.mark.asyncio
    async def test_a_failed_notification_is_logged_rather_than_swallowed(self):
        """THE REGRESSION. Before the fix this produced no record of any kind."""
        log = await self._run(RuntimeError("telegram exploded"))

        lines = self._alert_lines(log)
        assert any("Auto-approve" in ln for ln in lines), (
            f"a failed auto-approve notification left no warning: {lines}"
        )
        assert any(str(self.CHAT) in ln for ln in lines), (
            f"the failure is logged without naming the tenant: {lines}"
        )

    @pytest.mark.asyncio
    async def test_a_notification_never_attempted_is_not_logged(self):
        """The other half of the distinction, and the reason the first
        assertion means anything. A post that was not auto-approved sends no
        confirmation, so there is nothing to have failed — logging here would
        make 'never happened' and 'happened and was lost' look identical
        again, in the opposite direction."""
        log = await self._run(None, auto_approved=False)

        assert not any("Auto-approve" in ln for ln in self._alert_lines(log))

    @pytest.mark.asyncio
    async def test_no_telegram_service_is_not_logged_either(self):
        log = await self._run(None, with_telegram=False)

        assert not any("Auto-approve" in ln for ln in self._alert_lines(log))

    @pytest.mark.asyncio
    async def test_a_migrated_tenant_leaves_a_RECOVERABLE_pair(self):
        """The named hole in the #743 recovery corpus, closed.

        `ChatMigratedError.parse_pair`'s docstring cites this exact site as a
        shape that produces no row, which is why 'not in this corpus' has to
        be read as *unknown* rather than *did not migrate*. A migration here
        used to lose the new chat id outright — the single fact a recovery
        pass needs. Asserted by round-tripping through the real parser rather
        than by matching prose, so a reworded message that stopped being
        machine-recoverable would fail this.
        """
        new_id = -100999
        log = await self._run(ChatMigrated(new_chat_id=new_id))

        pairs = [
            ChatMigratedError.parse_pair(ln)
            for ln in self._alert_lines(log)
            if "Auto-approve" in ln
        ]
        assert (self.CHAT, new_id) in pairs, (
            f"the new chat id is not recoverable from the log line: "
            f"{self._alert_lines(log)}"
        )
