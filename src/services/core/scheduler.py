"""Scheduler service - JIT posting schedule with per-slot media selection."""

from datetime import datetime, timedelta, timezone
from typing import Optional, List, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import random


from src.exceptions.google_drive import GoogleDriveAuthError
from src.exceptions.instagram import is_container_confirmed_failed
from src.services.base_service import BaseService
from src.services.core.settings_service import SettingsService
from src.repositories.atomic_session import atomic_session
from src.repositories.media_repository import MediaRepository
from src.repositories.queue_repository import QueueRepository
from src.repositories.history_repository import HistoryRepository
from src.repositories.lock_repository import LockRepository
from src.repositories.category_mix_repository import CategoryMixRepository
from src.config.settings import settings
from src.utils.datetime_utils import ensure_utc
from src.utils.logger import logger


class SchedulerService(BaseService):
    """Just-in-time posting scheduler.

    Instead of pre-populating the queue days in advance, computes whether
    a posting slot is due on each scheduler tick and selects media at that
    moment.  The posting_queue narrows to an "in-flight tracker" — items
    enter when selected and leave when the team acts (Posted/Skip/Reject).
    """

    SCHEDULE_JITTER_MINUTES = 30

    def __init__(self):
        super().__init__()
        self.media_repo = MediaRepository()
        self.queue_repo = QueueRepository()
        self.history_repo = HistoryRepository()
        self.lock_repo = LockRepository()
        self.category_mix_repo = CategoryMixRepository()
        self.settings_service = SettingsService()
        # Injected by main.py after construction
        self.telegram_service = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_slot_due(self, chat_settings) -> Union[str, None, bool]:
        """Check whether a posting slot should fire now.

        Uses posts_per_day and the posting window to compute an even
        interval, then compares against last_post_sent_at.

        Returns:
            str  — target category name (slot is due, use this category)
            None — slot is due, no category preference
            False — slot is not due
        """
        now = datetime.now(timezone.utc)

        if not self._in_posting_window(now, chat_settings):
            return False

        window_hours = self._posting_window_hours(chat_settings)
        interval_seconds = (window_hours * 3600) / chat_settings.posts_per_day

        last_sent = ensure_utc(chat_settings.last_post_sent_at)
        if last_sent and (now - last_sent).total_seconds() < interval_seconds:
            return False  # Too soon

        # Pick category for this slot
        return self._pick_category_for_slot()

    def _compute_catchup_sent_at(
        self, chat_settings, *, first_tick: bool = False
    ) -> Optional[datetime]:
        """If behind by >= 2 intervals, return the timestamp to advance to.

        On a normal tick, advances by one interval so the next tick
        re-evaluates and catches up gradually — one post per tick.

        On the first tick after startup, returns None (use now) so the
        post counts as current.  This prevents redeploy churn from
        starving the schedule: even if deploys restart the worker every
        few minutes, each startup fires one immediate post that resets
        the timer to now.

        Returns:
            datetime to use as last_post_sent_at, or None for normal
            behavior (set to now).
        """
        last_sent = ensure_utc(chat_settings.last_post_sent_at)
        if not last_sent:
            return None

        now = datetime.now(timezone.utc)
        window_hours = self._posting_window_hours(chat_settings)
        interval_seconds = (window_hours * 3600) / chat_settings.posts_per_day
        elapsed = (now - last_sent).total_seconds()

        if elapsed >= 2 * interval_seconds:
            missed_slots = int(elapsed / interval_seconds) - 1
            if first_tick:
                logger.info(
                    f"[catchup] First tick after startup, behind by "
                    f"{missed_slots} slot(s) "
                    f"(last_sent={last_sent.isoformat()}, "
                    f"elapsed={elapsed:.0f}s). "
                    f"Posting immediately, resetting to now"
                )
                return None
            catchup_to = last_sent + timedelta(seconds=interval_seconds)
            logger.info(
                f"[catchup] Behind by {missed_slots} slot(s) "
                f"(last_sent={last_sent.isoformat()}, "
                f"interval={interval_seconds:.0f}s, "
                f"elapsed={elapsed:.0f}s). "
                f"Advancing last_post_sent_at to {catchup_to.isoformat()}"
            )
            return catchup_to

        return None

    async def process_slot(
        self, telegram_chat_id: int, *, first_tick: bool = False
    ) -> dict:
        """Process a single scheduler tick for a tenant.

        This is the main entry point called by the scheduler loop every
        60 seconds.  No service_run is created for no-op ticks.

        Args:
            telegram_chat_id: Tenant to check
            first_tick: True on the first tick after worker startup.
                When catching up, the first tick posts immediately
                (last_post_sent_at = now) instead of advancing
                gradually, so redeploy churn doesn't starve the
                schedule.

        Returns:
            Dict with keys: posted (bool), reason (str), and optionally
            queue_item_id, media_file, category.
        """
        # Defense-in-depth: clean up failed/stale queue items from prior ticks
        self.queue_repo.delete_stale_pending(max_age_minutes=10)
        self.queue_repo.requeue_stale_processing(max_age_minutes=10)

        chat_settings = self.settings_service.get_settings(telegram_chat_id)

        if chat_settings.is_paused:
            return {"posted": False, "reason": "paused"}

        # Daily cap guard — applies before any slot/media evaluation
        from src.services.core.daily_cap import can_post_today

        if not can_post_today(chat_settings, self.history_repo, self.queue_repo):
            return {"posted": False, "reason": "daily_cap_reached"}

        slot_result = self.is_slot_due(chat_settings)
        if slot_result is False:
            return {"posted": False, "reason": "not_due"}

        # Catch-up: on first tick, post immediately (reset to now).
        # On subsequent ticks, advance by one interval per tick.
        sent_at_override = self._compute_catchup_sent_at(
            chat_settings, first_tick=first_tick
        )

        category = slot_result if isinstance(slot_result, str) else None
        return await self._select_and_send(
            chat_settings,
            category=category,
            triggered_by="scheduler",
            sent_at_override=sent_at_override,
        )

    async def force_send_next(
        self,
        telegram_chat_id: int,
        user_id: Optional[str] = None,
        force_sent_indicator: bool = False,
    ) -> dict:
        """JIT select and send immediately.  Used by /next command.

        Differs from process_slot():
        - Ignores is_slot_due() — always fires
        - Updates last_post_sent_at to prevent immediate follow-up slot

        Args:
            telegram_chat_id: Tenant chat
            user_id: User who triggered (for observability)
            force_sent_indicator: If True, caption shows ⚡ indicator

        Returns:
            Dict with posted, queue_item_id, media_item, error keys
        """
        chat_settings = self.settings_service.get_settings(telegram_chat_id)

        # Daily cap guard — even /next respects the daily limit
        from src.services.core.daily_cap import can_post_today

        if not can_post_today(chat_settings, self.history_repo, self.queue_repo):
            return {
                "posted": False,
                "reason": "daily_cap_reached",
                "error": "Daily posting limit reached",
            }

        return await self._select_and_send(
            chat_settings,
            category=None,
            triggered_by="telegram",
            user_id=user_id,
            force_sent_indicator=force_sent_indicator,
        )

    def get_queue_preview(
        self,
        telegram_chat_id: int,
        count: int = 5,
    ) -> list:
        """Compute the next N selections without persisting.

        Passes previously-selected IDs as exclude_ids so each iteration
        returns a different item.

        Returns:
            List of dicts with media_id, file_name, category.
        """
        previews = []
        seen_ids: list[str] = []

        for _ in range(count):
            media_item = self._select_media(category=None, exclude_ids=seen_ids or None)
            if not media_item:
                break
            seen_ids.append(str(media_item.id))
            previews.append(
                {
                    "media_id": str(media_item.id),
                    "file_name": media_item.file_name,
                    "category": media_item.category,
                }
            )

        return previews

    # ------------------------------------------------------------------
    # Queue management (kept from old scheduler)
    # ------------------------------------------------------------------

    def _resolve_chat_settings_id(
        self, telegram_chat_id: Optional[int] = None
    ) -> Optional[str]:
        """Derive chat_settings_id from telegram_chat_id."""
        if telegram_chat_id is None:
            telegram_chat_id = settings.ADMIN_TELEGRAM_CHAT_ID
        chat_settings = self.settings_service.get_settings(telegram_chat_id)
        return str(chat_settings.id) if chat_settings else None

    def clear_pending_queue(self, telegram_chat_id: Optional[int] = None) -> int:
        """Delete all pending queue items for a chat."""
        chat_settings_id = self._resolve_chat_settings_id(telegram_chat_id)
        return self.queue_repo.delete_all_pending(chat_settings_id=chat_settings_id)

    def count_pending(self, telegram_chat_id: Optional[int] = None) -> int:
        """Count pending queue items for a chat."""
        chat_settings_id = self._resolve_chat_settings_id(telegram_chat_id)
        return self.queue_repo.count_pending(chat_settings_id=chat_settings_id)

    def check_availability(self, media_id: str) -> bool:
        """Check if media item is available for scheduling."""
        if self.lock_repo.is_locked(media_id):
            return False
        if self.queue_repo.get_by_media_id(media_id):
            return False
        return True

    # ------------------------------------------------------------------
    # Internal: JIT core
    # ------------------------------------------------------------------

    async def _select_and_send(
        self,
        chat_settings,
        *,
        category: Optional[str],
        triggered_by: str,
        user_id: Optional[str] = None,
        force_sent_indicator: bool = False,
        sent_at_override: Optional[datetime] = None,
    ) -> dict:
        """Core JIT flow: select media → create queue item → send.

        Only creates a service_run when there is actual work to do.

        Args:
            sent_at_override: If set, use this instead of now for
                last_post_sent_at (used during catch-up to advance
                by one interval rather than jumping to now).
        """
        from src.services.core.loops.lifecycle import session_state

        if session_state.shutdown_in_progress:
            return {"posted": False, "reason": "shutdown_in_progress"}

        with self.track_execution(
            method_name="select_and_send",
            user_id=user_id,
            triggered_by=triggered_by,
        ) as run_id:
            media_item = self._select_media(category=category)
            if not media_item:
                result = {
                    "posted": False,
                    "reason": "no_eligible_media",
                    "queue_item_id": None,
                    "media_item": None,
                    "error": "No eligible media available",
                }
                self.set_result_summary(
                    run_id, {k: v for k, v in result.items() if k != "media_item"}
                )
                return result

            # Generate AI caption if enabled and no manual caption
            if (
                chat_settings.enable_ai_captions
                and not media_item.caption
                and not media_item.generated_caption
            ):
                try:
                    from src.services.core.caption_service import CaptionService

                    with CaptionService(media_repo=self.media_repo) as caption_service:
                        generated = await caption_service.generate_caption(media_item)
                    if generated:
                        media_item = self.media_repo.get_by_id(str(media_item.id))
                except Exception as e:  # noqa: BLE001 — caption failures must never block posting
                    logger.warning(f"AI caption generation failed, continuing: {e}")

            # Auto-approve previously-approved media (skip Telegram)
            if media_item.times_posted > 0 and triggered_by == "scheduler":
                result = await self._auto_approve(
                    media_item, chat_settings, sent_at_override=sent_at_override
                )
                self.set_result_summary(
                    run_id,
                    {
                        "posted": result["posted"],
                        "auto_approved": True,
                        "media_file": media_item.file_name,
                        "category": media_item.category,
                    },
                )
                return result

            # Create in-flight queue item
            queue_item = self.queue_repo.create(
                media_item_id=str(media_item.id),
                scheduled_for=datetime.now(timezone.utc),
                chat_settings_id=str(chat_settings.id),
            )

            # Send to Telegram
            success = await self._send_to_telegram(
                queue_item, force_sent=force_sent_indicator
            )

            if success:
                self.settings_service.update_last_post_sent_at(
                    chat_settings.telegram_chat_id,
                    sent_at_override or datetime.now(timezone.utc),
                )

            result = {
                "posted": success,
                "queue_item_id": str(queue_item.id),
                "media_item": media_item,
                "media_file": media_item.file_name,
                "category": media_item.category,
                "error": None if success else "Failed to send Telegram notification",
            }
            self.set_result_summary(
                run_id,
                {
                    "posted": success,
                    "queue_item_id": str(queue_item.id),
                    "media_file": media_item.file_name,
                    "category": media_item.category,
                },
            )
            return result

    # Track consecutive send failures for systemic-issue detection
    _consecutive_send_failures: int = 0
    _SEND_MAX_RETRIES: int = 3
    _SEND_RETRY_DELAY_SECONDS: float = 5.0
    _CONSECUTIVE_FAILURE_CRITICAL_THRESHOLD: int = 3

    async def _send_to_telegram(self, queue_item, force_sent: bool = False) -> bool:
        """Claim queue item and send to Telegram with retry.

        Claims (status -> processing) BEFORE sending to prevent duplicate
        sends if the scheduler fires again before Telegram responds.

        On failure: retries up to 3 times with 5s backoff. On final failure,
        marks the queue item as 'failed' and records to posting_history with
        the error message. Never silently deletes the queue item.

        GoogleDriveAuthError is not retried (auth won't self-heal).
        """
        import asyncio

        queue_item_id = str(queue_item.id)
        self.queue_repo.update_status(queue_item_id, "processing")

        last_error: Optional[str] = None

        for attempt in range(1, self._SEND_MAX_RETRIES + 1):
            try:
                success = await self.telegram_service.send_notification(
                    queue_item_id, force_sent=force_sent
                )
                if success:
                    logger.info(f"Sent Telegram notification for {queue_item_id}")
                    self._consecutive_send_failures = 0
                    return True

                last_error = "send_notification returned False"

            except GoogleDriveAuthError:
                # Auth errors won't self-heal — fail immediately, don't retry
                logger.error(
                    f"Google Drive auth error for {queue_item_id} — "
                    "marking failed (not retryable)"
                )
                self._record_send_failure(
                    queue_item, "GoogleDriveAuthError: credentials invalid"
                )
                raise

            except Exception as e:  # noqa: BLE001
                last_error = str(e)

            # Log retry or final failure
            if attempt < self._SEND_MAX_RETRIES:
                logger.warning(
                    f"Telegram send failed for {queue_item_id} "
                    f"(attempt {attempt}/{self._SEND_MAX_RETRIES}): {last_error}"
                )
                await asyncio.sleep(self._SEND_RETRY_DELAY_SECONDS)
            else:
                logger.warning(
                    f"Telegram send failed for {queue_item_id} after "
                    f"{self._SEND_MAX_RETRIES} attempts: {last_error}"
                )

        # All retries exhausted — record failure
        self._record_send_failure(queue_item, last_error)
        return False

    def _record_send_failure(self, queue_item, error_message: Optional[str]) -> None:
        """Mark queue item as failed and record to posting_history."""
        from src.repositories.history_repository import HistoryCreateParams

        queue_item_id = str(queue_item.id)
        now = datetime.now(timezone.utc)

        # Mark queue item as failed (don't delete — preserves evidence)
        try:
            self.queue_repo.update_status(queue_item_id, "failed")
        except Exception:  # noqa: BLE001 — best-effort
            logger.error(f"Failed to mark queue item {queue_item_id} as failed")

        # Record to posting_history
        try:
            self.history_repo.create(
                HistoryCreateParams(
                    media_item_id=str(queue_item.media_item_id),
                    queue_item_id=queue_item_id,
                    queue_created_at=queue_item.created_at or now,
                    queue_deleted_at=now,
                    scheduled_for=queue_item.scheduled_for or now,
                    posted_at=now,
                    status="failed",
                    success=False,
                    posting_method="telegram_manual",
                    chat_settings_id=(
                        str(queue_item.chat_settings_id)
                        if queue_item.chat_settings_id
                        else None
                    ),
                    error_message=error_message,
                )
            )
        except Exception as e:  # noqa: BLE001 — best-effort
            logger.error(f"Failed to record posting_history for {queue_item_id}: {e}")

        # Track consecutive failures for systemic issue detection
        self._consecutive_send_failures += 1
        if (
            self._consecutive_send_failures
            >= self._CONSECUTIVE_FAILURE_CRITICAL_THRESHOLD
        ):
            logger.critical(
                f"SYSTEMIC FAILURE: {self._consecutive_send_failures} consecutive "
                f"Telegram send failures — possible revoked bot token or "
                f"network outage. Last error: {error_message}"
            )

    async def _auto_approve(
        self,
        media_item,
        chat_settings,
        sent_at_override: Optional[datetime] = None,
    ) -> dict:
        """Auto-approve a previously-approved media item without Telegram interaction.

        Creates a transient queue item, records history, applies a repost lock,
        and increments times_posted.

        When Instagram API is enabled, uploads to Cloudinary and posts via the
        Graph API. On API failure the item is NOT recorded as successful —
        the queue item is cleaned up and the scheduler advances its clock,
        but times_posted and locks are not touched so the item stays eligible
        for future selection.
        """
        from src.repositories.history_repository import HistoryCreateParams

        now = datetime.now(timezone.utc)
        media_id = str(media_item.id)
        cs_id = str(chat_settings.id)

        # Daily cap guard — redundant with process_slot but defends this
        # path if called from a future entry point
        from src.services.core.daily_cap import can_post_today

        if not can_post_today(chat_settings, self.history_repo, self.queue_repo):
            logger.info(
                f"Auto-approve skipped for {media_item.file_name}: daily cap reached"
            )
            return {
                "posted": False,
                "auto_approved": True,
                "queue_item_id": None,
                "media_item": media_item,
                "media_file": media_item.file_name,
                "category": media_item.category,
                "error": "Daily posting limit reached",
            }

        # Create + immediately delete queue item (needed for history record)
        queue_item = self.queue_repo.create(
            media_item_id=media_id,
            scheduled_for=now,
            chat_settings_id=cs_id,
        )
        queue_id = str(queue_item.id)

        posting_method = "auto_reapproval"
        instagram_story_id = None

        if chat_settings.enable_instagram_api and not chat_settings.dry_run_mode:
            # Claim-before-publish (#549): the container callback flips the row
            # to 'publishing' and persists the container_id the instant the IG
            # container exists — BEFORE the publish. On failure we classify from
            # two signals: whether a container_id was persisted, and whether the
            # error is an IG-confirmed dead container (ERROR/EXPIRED).
            container_created = {"id": None}

            def _persist_container(container_id):
                container_created["id"] = container_id
                self.queue_repo.mark_publishing(queue_id, container_id)

            confirmed_failed = False
            try:
                instagram_story_id = await self._auto_approve_instagram(
                    media_item,
                    chat_settings,
                    on_container_created=_persist_container,
                )
            except Exception as e:  # noqa: BLE001
                instagram_story_id = None
                confirmed_failed = is_container_confirmed_failed(e)
                logger.warning(
                    f"Auto-approve Instagram posting failed for "
                    f"{media_item.file_name} [{media_item.category}]: {e}"
                )

            if instagram_story_id is None:
                # Advance the clock so the scheduler doesn't re-fire this slot.
                self.settings_service.update_last_post_sent_at(
                    chat_settings.telegram_chat_id, sent_at_override or now
                )
                if container_created["id"] is not None and not confirmed_failed:
                    # AMBIGUOUS: a container was created and IG did NOT confirm
                    # failure, so the story may have published. Leave the row in
                    # 'publishing' — excluded from every sweep, it blocks
                    # reselection and is never re-published. No history, no lock.
                    logger.warning(
                        f"Auto-approve Instagram unconfirmed for "
                        f"{media_item.file_name} [{media_item.category}] "
                        f"(container {container_created['id']}) — holding row "
                        f"in 'publishing' to prevent a duplicate story"
                    )
                    return {
                        "posted": False,
                        "auto_approved": True,
                        "queue_item_id": queue_id,
                        "media_item": media_item,
                        "media_file": media_item.file_name,
                        "category": media_item.category,
                        "error": "Instagram publish unconfirmed — held for review",
                    }
                # SAFE RETRY: nothing published — either no container was ever
                # created, or IG affirmatively confirmed the container is dead
                # (ERROR/EXPIRED). Release the transient row (deleting a claimed
                # 'publishing' row too) and leave the media eligible.
                if container_created["id"] is not None:
                    logger.warning(
                        f"Auto-approve Instagram container confirmed-failed for "
                        f"{media_item.file_name} [{media_item.category}] "
                        f"(container {container_created['id']}) — releasing row "
                        f"for retry"
                    )
                self.queue_repo.delete(queue_id)
                logger.warning(
                    f"Auto-approve Instagram failed for {media_item.file_name} "
                    f"[{media_item.category}] — not recording as posted"
                )
                return {
                    "posted": False,
                    "auto_approved": True,
                    "queue_item_id": queue_id,
                    "media_item": media_item,
                    "media_file": media_item.file_name,
                    "category": media_item.category,
                    "error": "Instagram API posting failed",
                }

            posting_method = "instagram_api"

        # Finalize atomically: a crash between the publish and the bookkeeping
        # must never leave a story recorded-but-still-queued (double count) or
        # the queue row deleted-without-history (re-serve). One transaction
        # writes history (idempotent, #551), increments, locks, and deletes.
        from src.services.core.media_lock import MediaLockService

        lock_service = MediaLockService()
        with atomic_session(
            [
                self.history_repo,
                self.media_repo,
                self.queue_repo,
                lock_service.lock_repo,
            ]
        ):
            self.history_repo.create_idempotent(
                HistoryCreateParams(
                    media_item_id=media_id,
                    queue_item_id=queue_id,
                    queue_created_at=now,
                    queue_deleted_at=now,
                    scheduled_for=now,
                    posted_at=now,
                    status="posted",
                    success=True,
                    posting_method=posting_method,
                    instagram_story_id=instagram_story_id,
                    chat_settings_id=cs_id,
                )
            )
            self.media_repo.increment_times_posted(media_id)
            lock_service.create_lock(
                media_id, telegram_chat_id=chat_settings.telegram_chat_id
            )
            self.queue_repo.delete(queue_id)

        self.settings_service.update_last_post_sent_at(
            chat_settings.telegram_chat_id, sent_at_override or now
        )

        logger.info(
            f"Auto-approved returning media: {media_item.file_name} "
            f"[{media_item.category}] (posted {media_item.times_posted}x before, "
            f"method={posting_method}"
            f"{f', story_id={instagram_story_id}' if instagram_story_id else ''})"
        )

        return {
            "posted": True,
            "auto_approved": True,
            "queue_item_id": queue_id,
            "media_item": media_item,
            "media_file": media_item.file_name,
            "category": media_item.category,
            "error": None,
        }

    async def _auto_approve_instagram(
        self,
        media_item,
        chat_settings,
        on_container_created=None,
    ) -> Optional[str]:
        """Post auto-approved media to Instagram.

        Runs safety check, uploads to Cloudinary, posts via Graph API,
        cleans up Cloudinary. Returns the story_id on success, or None when it
        bails out BEFORE any container is created (safety check / upload
        failure — nothing published, safe to retry).

        A failure AFTER the Graph API call begins PROPAGATES as an exception so
        the caller can classify the outcome from the error (an IG-confirmed dead
        container is safe to release; an ambiguous crash/timeout must stay
        stuck). The Cloudinary/service cleanup in ``finally`` still runs first.

        ``on_container_created`` is forwarded to ``post_story`` so the caller
        can persist the container_id (claim-before-publish) the moment the
        container exists and before the publish call.
        """

        import asyncio

        from src.services.integrations.cloud_storage import (
            CloudStorageService,
            CLOUD_UPLOAD_FOLDER,
        )
        from src.services.integrations.instagram_api import InstagramAPIService
        from src.services.media_sources.factory import MediaSourceFactory

        instagram_service = InstagramAPIService()
        cloud_service = CloudStorageService()
        cloud_public_id = None

        try:
            safety = instagram_service.safety_check_before_post(
                telegram_chat_id=chat_settings.telegram_chat_id
            )
            if not safety["safe_to_post"]:
                logger.warning(
                    f"Auto-approve Instagram safety check failed: {safety['errors']}"
                )
                return None

            provider = MediaSourceFactory.get_provider_for_media_item(
                media_item, telegram_chat_id=chat_settings.telegram_chat_id
            )
            # Offload the blocking media transfer so the shared bot event loop
            # stays free to service Telegram callbacks while this runs.
            file_bytes = await asyncio.to_thread(
                provider.download_file, media_item.source_identifier
            )

            folder = f"{CLOUD_UPLOAD_FOLDER}/{chat_settings.id}"
            upload_result = await asyncio.to_thread(
                cloud_service.upload_media,
                file_bytes=file_bytes,
                filename=media_item.file_name,
                folder=folder,
            )
            cloud_url = upload_result.get("url")
            cloud_public_id = upload_result.get("public_id")

            if not cloud_url:
                logger.warning("Auto-approve Cloudinary upload returned no URL")
                return None

            media_type = (
                "VIDEO"
                if media_item.file_path
                and media_item.file_path.lower().endswith((".mp4", ".mov"))
                else "IMAGE"
            )
            story_url = (
                cloud_service.get_story_optimized_url(cloud_url)
                if media_type == "IMAGE"
                else cloud_url
            )

            post_result = await instagram_service.post_story(
                media_url=story_url,
                media_type=media_type,
                telegram_chat_id=chat_settings.telegram_chat_id,
                on_container_created=on_container_created,
            )

            return post_result.get("story_id")

        finally:
            # A post-Graph-API failure propagates to the caller (which classifies
            # IG-confirmed-dead vs ambiguous) — but the Cloudinary/service cleanup
            # must run either way.
            if cloud_public_id:
                try:
                    cloud_service.delete_media(cloud_public_id)
                except Exception:
                    pass
            instagram_service.close()
            cloud_service.close()

    # ------------------------------------------------------------------
    # Internal: slot timing
    # ------------------------------------------------------------------

    @staticmethod
    def _in_posting_window(now: datetime, chat_settings) -> bool:
        """Check if current time is within the posting window.

        Posting hours are in the user's local timezone (chat_settings.posting_timezone).
        Converts UTC now to local time before comparing.

        When start == end, the window is treated as "always on" (full 24h).
        """

        tz_name = getattr(chat_settings, "posting_timezone", None)
        if tz_name:
            try:
                local_now = now.astimezone(ZoneInfo(tz_name))
            except (ZoneInfoNotFoundError, KeyError):
                logger.warning(
                    "Invalid posting_timezone %r — falling back to UTC", tz_name
                )
                local_now = now
        else:
            local_now = now

        current_hour = local_now.hour + local_now.minute / 60.0
        start = chat_settings.posting_hours_start
        end = chat_settings.posting_hours_end

        if start == end:
            return True

        if end < start:
            # Window crosses midnight (e.g., 22-2)
            return current_hour >= start or current_hour < end
        else:
            return start <= current_hour < end

    @staticmethod
    def _posting_window_hours(chat_settings) -> float:
        """Compute the length of the posting window in hours.

        When start == end, returns 24.0 (full day).
        """
        start = chat_settings.posting_hours_start
        end = chat_settings.posting_hours_end
        if start == end:
            return 24.0
        if end < start:
            return (24 - start) + end
        return end - start

    # ------------------------------------------------------------------
    # Internal: category selection
    # ------------------------------------------------------------------

    def _pick_category_for_slot(self) -> Optional[str]:
        """Pick a single category for this slot using configured ratios.

        Uses weighted random selection so that over many slots the
        distribution matches the configured mix.

        Returns:
            Category name, or None if no ratios configured.
        """
        current_mix = self.category_mix_repo.get_current_mix_as_dict()
        if not current_mix:
            return None

        categories = list(current_mix.keys())
        weights = [float(r) for r in current_mix.values()]
        return random.choices(categories, weights=weights, k=1)[0]

    def _allocate_slots_to_categories(self, total_slots: int) -> List[Optional[str]]:
        """Allocate slots to categories based on configured ratios.

        Kept for queue_preview and backwards compatibility.
        """
        current_mix = self.category_mix_repo.get_current_mix_as_dict()

        if not current_mix:
            return []

        slot_allocation = []
        remaining_slots = total_slots

        sorted_categories = sorted(
            current_mix.items(), key=lambda x: x[1], reverse=True
        )

        for i, (category, ratio) in enumerate(sorted_categories):
            if i == len(sorted_categories) - 1:
                slots_for_category = remaining_slots
            else:
                slots_for_category = round(float(ratio) * total_slots)
                remaining_slots -= slots_for_category

            slot_allocation.extend([category] * slots_for_category)

        random.shuffle(slot_allocation)
        return slot_allocation

    def _summarize_allocation(self, slot_categories: List[str]) -> str:
        """Summarize slot allocation for logging."""
        summary = {}
        for cat in slot_categories:
            summary[cat] = summary.get(cat, 0) + 1
        return ", ".join(f"{cat}: {count}" for cat, count in sorted(summary.items()))

    # ------------------------------------------------------------------
    # Internal: media selection (unchanged)
    # ------------------------------------------------------------------

    def _select_media(
        self,
        category: Optional[str] = None,
        exclude_ids: Optional[list] = None,
    ):
        """Select next media item to post.

        Selection priority:
        1. Filter by category (if specified)
        2. Never posted items first (last_posted_at IS NULL)
        3. Least posted items (times_posted ASC)
        4. Random from eligible items (variety)

        Falls back to any category if target category is exhausted.
        """
        media_item = self._select_media_from_pool(
            category=category, exclude_ids=exclude_ids
        )

        if not media_item and category:
            logger.warning(
                f"Category '{category}' exhausted, falling back to any available media"
            )
            media_item = self._select_media_from_pool(
                category=None, exclude_ids=exclude_ids
            )

        return media_item

    def _select_media_from_pool(
        self,
        category: Optional[str] = None,
        exclude_ids: Optional[list] = None,
    ):
        """Select media from a specific pool (category or all).

        Delegates to MediaRepository.get_next_eligible_for_posting().
        """
        return self.media_repo.get_next_eligible_for_posting(
            category=category, exclude_ids=exclude_ids
        )
