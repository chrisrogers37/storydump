"""Telegram auto-post handler - Instagram API posting flow with safety gates."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING


from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from src.exceptions.instagram import (
    InstagramAPIError,
    MediaUnsupportedError,
    is_publish_definitively_failed,
    MediaUploadError,
    RateLimitError,
    TokenCorruptError,
    TokenExpiredError,
    TokenRevokedError,
)
from src.repositories.atomic_session import atomic_session
from src.repositories.history_repository import HistoryCreateParams
from src.services.core.telegram_service import _escape_markdown
from src.services.core.telegram_utils import (
    build_queue_action_keyboard,
    reconcile_card_messages,
    validate_queue_item,
)
from src.utils.logger import logger
from src.utils.resilience import telegram_edit_with_retry
from datetime import datetime, timezone
from src.repositories.tenant_scope import scope_of_row

if TYPE_CHECKING:
    from src.services.core.telegram_service import TelegramService


async def _update_autopost_caption(query, caption: str, reply_markup=None):
    """Update autopost message caption with standard formatting.

    Wraps the common telegram_edit_with_retry + edit_message_caption pattern
    used throughout the autopost flow.
    """
    await telegram_edit_with_retry(
        query.edit_message_caption,
        caption=caption,
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )


@dataclass
class AutopostContext:
    """Shared state passed through the autopost call chain.

    Bundles parameters that are constant for a single _do_autopost() invocation
    and shared across _upload_to_cloudinary, _handle_dry_run,
    _execute_instagram_post, _record_successful_post, etc.
    """

    queue_id: str
    queue_item: object
    media_item: object
    user: object
    query: object
    chat_id: int
    chat_settings: object
    cloud_service: object
    instagram_service: object
    cancel_flag: object = None
    cloud_url: str | None = None
    cloud_public_id: str | None = None
    # Set (with status='publishing') the instant the IG container is created,
    # before publish. Presence => the publish outcome is unconfirmed on failure.
    container_id: str | None = None


class TelegramAutopostHandler:
    """Handles Instagram API auto-posting flow with safety gates.

    CRITICAL: This handler contains safety gates that prevent accidental
    posting to Instagram. Do not modify the safety check flow without
    careful review.

    Uses composition: receives a TelegramService reference for shared state.
    """

    def __init__(self, service: TelegramService):
        self.service = service
        self._background_tasks: set[asyncio.Task] = set()

    async def handle_autopost(self, queue_id: str, user, query):
        """
        Handle 'Auto Post' button click.

        This uploads the media to Cloudinary and posts to Instagram via API.
        Includes CRITICAL safety gates to prevent accidental Facebook posting.

        Duplicate rapid taps are rejected by the in-flight marker (held for the
        whole background task); terminal actions (Posted/Skip/Reject) can still
        abort via the cancel flag. The operation lock only guards the brief
        claim + spawn critical section — it is released before the slow, rate-
        limited edits, which run in the background task.
        """
        # Durable dupe guard: an in-flight autopost holds the marker until its
        # background task finishes, so a re-tap is rejected even after the lock is
        # released (below) ahead of the slow edits.
        if self.service.is_autopost_inflight(queue_id):
            await query.answer("⏳ Already posting this item...", show_alert=False)
            return

        # Acknowledge the click IMMEDIATELY so the button stops spinning.
        # Telegram requires answer_callback_query within ~30s of the click,
        # but the Cloudinary upload + Meta publish below can take longer
        # than that. Without this early answer the user sees a stale spinner.
        try:
            await query.answer("⏳ Posting…", show_alert=False)
        except Exception as e:  # noqa: BLE001 — best-effort ack
            logger.debug(f"Could not pre-answer autopost callback {queue_id}: {e}")

        cancel_flag = self.service.get_cancel_flag(queue_id)
        cancel_flag.clear()

        # The operation lock guards only the claim → mark-in-flight → spawn
        # critical section (plus the cheap keyboard strip and card reconcile). It
        # is released the instant the task is spawned — BEFORE the slow, rate-
        # limited edits (Cloudinary upload, Meta publish, caption updates) that
        # run in the background task — so it no longer blocks a concurrent action
        # for the whole task. The in-flight marker, set before the spawn and
        # cleared when the task finishes, is the durable guard that a second tap
        # can never start a second autopost while the lock is down.
        lock = self.service.get_operation_lock(queue_id)
        spawned = False
        await lock.acquire()
        try:
            # Re-check under the lock: a tap that raced us may have claimed first.
            if self.service.is_autopost_inflight(queue_id):
                await query.answer("⏳ Already posting this item...", show_alert=False)
                return

            # Immediate visual feedback: remove buttons to signal action received.
            # Single attempt on purpose — cosmetic; the caption edits later strip
            # the keyboard implicitly, and retrying here would stall under the lock.
            try:
                await query.edit_message_reply_markup(
                    reply_markup=InlineKeyboardMarkup([])
                )
            except Exception as e:  # noqa: BLE001
                logger.debug(
                    f"Could not remove keyboard for autopost item {queue_id}: {e}"
                )

            # Atomic claim: prevents duplicate auto-posts from rapid double-taps.
            queue_item = self.service.queue_repo.claim_for_processing(queue_id)
            if not queue_item:
                await validate_queue_item(self.service, queue_id, query)
                return

            await reconcile_card_messages(self.service, queue_id, queue_item, query)

            media_item = self.service.media_repo.get_by_id(
                str(queue_item.media_item_id),
                chat_settings_id=scope_of_row(
                    queue_item, where="autopost.handle_autopost"
                ),
            )
            if not media_item:
                await query.edit_message_caption(caption="⚠️ Media item not found")
                return

            # Mark in-flight and spawn — no await between them, and the whole
            # block runs under the lock, so a concurrent tap can't slip a second
            # claim through. The background task clears the marker when done.
            self.service.mark_autopost_inflight(queue_id)
            task = asyncio.create_task(
                self._autopost_background(
                    queue_id, queue_item, media_item, user, query, cancel_flag
                )
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
            spawned = True
        finally:
            lock.release()
            # On a non-spawn path, free this item's state — unless a concurrent
            # autopost owns the marker (never clear a live in-flight marker).
            if not spawned and not self.service.is_autopost_inflight(queue_id):
                self.service.cleanup_operation_state(queue_id)

    async def _autopost_background(
        self, queue_id, queue_item, media_item, user, query, cancel_flag
    ):
        """Background task that performs the heavy auto-post work.

        Runs the slow, rate-limited work (Cloudinary upload, Instagram publish,
        caption edits) WITHOUT holding the operation lock — it was released the
        instant this task was spawned. The in-flight marker keeps a concurrent tap
        from starting a second autopost; it is cleared (with the lock and cancel
        flag) when this task finishes.
        """
        # This task copied the callback's context (incl. its per-repo Sessions).
        # Detach to fresh, task-local sessions so our DB work does not share — and
        # the callback's ``finally: cleanup_transactions()`` cannot commit/close —
        # the very session we are mid-write on (use-after-free). See the ContextVar
        # session isolation in BaseRepository.
        self.service.begin_isolated_transactions()
        try:
            # ============================================
            # CRITICAL SAFETY GATES
            # ============================================
            from src.services.integrations.instagram_api import InstagramAPIService
            from src.services.integrations.cloud_storage import CloudStorageService

            instagram_service = InstagramAPIService()
            cloud_service = CloudStorageService()
            try:
                await self._do_autopost(
                    queue_id,
                    queue_item,
                    media_item,
                    user,
                    query,
                    instagram_service,
                    cloud_service,
                    cancel_flag,
                )
            finally:
                instagram_service.close()
                cloud_service.close()
        except Exception as e:  # noqa: BLE001
            logger.error(
                f"Background autopost failed for {queue_id[:8]}: {e}", exc_info=True
            )
        finally:
            self.service.cleanup_operation_state(queue_id)
            self.service.cleanup_transactions()

    async def _do_autopost(
        self,
        queue_id,
        queue_item,
        media_item,
        user,
        query,
        instagram_service,
        cloud_service,
        cancel_flag=None,
    ):
        """Internal method to perform auto-post with pre-created services.

        Orchestrates: safety check → upload → dry-run check → post → record.
        """
        chat_id = query.message.chat_id
        chat_settings = self.service.settings_service.get_settings(chat_id)
        if not chat_settings:
            await query.edit_message_caption(
                caption="⚠️ Chat settings not found.",
            )
            return

        # Run comprehensive safety check
        safety_result = instagram_service.safety_check_before_post(
            telegram_chat_id=chat_id
        )

        if not safety_result["safe_to_post"]:
            error_list = "\n".join([f"• {e}" for e in safety_result["errors"]])
            caption = (
                f"🚫 *SAFETY CHECK FAILED*\n\n"
                f"Cannot auto-post due to:\n{error_list}\n\n"
                f"Please check your configuration."
            )
            await query.edit_message_caption(caption=caption, parse_mode="Markdown")
            logger.error(f"Auto-post safety check failed: {safety_result['errors']}")
            return

        ctx = AutopostContext(
            queue_id=queue_id,
            queue_item=queue_item,
            media_item=media_item,
            user=user,
            query=query,
            chat_id=chat_id,
            chat_settings=chat_settings,
            cloud_service=cloud_service,
            instagram_service=instagram_service,
            cancel_flag=cancel_flag,
        )

        try:
            if not await self._upload_to_cloudinary(ctx):
                return

            if ctx.chat_settings.dry_run_mode:
                await self._handle_dry_run(ctx)
                return

            story_id = await self._execute_instagram_post(ctx)
            if story_id is None:
                return

            self._record_successful_post(ctx, story_id)
            await self._send_success_message(ctx, story_id)
            self._cleanup_cloudinary(ctx)

        except Exception as e:  # noqa: BLE001
            await self._handle_autopost_error(ctx, e)

    # ==================== Extracted Helpers ====================

    async def _get_account_display(self, ctx: AutopostContext) -> str:
        """Get formatted account display string for messages."""
        try:
            account_info = await ctx.instagram_service.get_account_info(
                telegram_chat_id=ctx.chat_id
            )
            return f"@{account_info.get('username', 'Unknown')}"
        except Exception as e:  # noqa: BLE001 — best-effort display info
            logger.warning(f"Could not fetch account display info: {e}")
            return "Unknown account"

    async def _upload_to_cloudinary(self, ctx: AutopostContext) -> bool:
        """Upload media to Cloudinary for Instagram posting.

        Sets ctx.cloud_url and ctx.cloud_public_id.
        Returns False if cancelled, True on success.
        """
        await ctx.query.edit_message_caption(
            caption="⏳ *Uploading to Cloudinary...*", parse_mode="Markdown"
        )

        from src.services.media_sources.factory import MediaSourceFactory

        provider = MediaSourceFactory.get_provider_for_media_item(
            ctx.media_item, telegram_chat_id=ctx.chat_id
        )
        # The download and Cloudinary upload are blocking synchronous network
        # transfers; run them off the event loop so a slow transfer can't FREEZE
        # it and stall everything else on the single bot loop (the update poller,
        # scheduler tick, cleanup loops). Each touches only this task's own
        # client/session, so the thread offload is safe. (Making a competing
        # tap's ack fire concurrently additionally needs concurrent_updates.)
        file_bytes = await asyncio.to_thread(
            provider.download_file, ctx.media_item.source_identifier
        )

        from src.services.integrations.cloud_storage import CLOUD_UPLOAD_FOLDER

        folder = (
            f"{CLOUD_UPLOAD_FOLDER}/{ctx.queue_item.chat_settings_id}"
            if ctx.queue_item.chat_settings_id
            else CLOUD_UPLOAD_FOLDER
        )
        upload_result = await asyncio.to_thread(
            ctx.cloud_service.upload_media,
            file_bytes=file_bytes,
            filename=ctx.media_item.file_name,
            folder=folder,
        )

        ctx.cloud_url = upload_result.get("url")
        ctx.cloud_public_id = upload_result.get("public_id")

        if not ctx.cloud_url:
            raise Exception("Cloudinary upload failed: No URL returned")

        logger.info(f"Uploaded to Cloudinary: {ctx.cloud_public_id}")

        if ctx.cancel_flag and ctx.cancel_flag.is_set():
            logger.info(
                f"Auto-post cancelled after Cloudinary upload for {ctx.media_item.file_name}"
            )
            self._cleanup_cloudinary(ctx)
            await ctx.query.edit_message_caption(
                caption="❌ Auto-post cancelled (another action was taken)"
            )
            return False

        self.service.media_repo.update_cloud_info(
            media_id=str(ctx.media_item.id),
            cloud_url=ctx.cloud_url,
            cloud_public_id=ctx.cloud_public_id,
            cloud_uploaded_at=datetime.now(timezone.utc),
            chat_settings_id=scope_of_row(
                ctx.queue_item, where="autopost.upload_to_cloudinary"
            ),
        )

        return True

    async def _handle_dry_run(self, ctx: AutopostContext) -> None:
        """Handle dry-run mode: log what would happen, cleanup cloud, show message."""
        keyboard = [
            [
                InlineKeyboardButton(
                    "🔄 Test Again",
                    callback_data=f"autopost:{ctx.queue_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "↩️ Back to Queue Item",
                    callback_data=f"back:{ctx.queue_id}",
                ),
            ],
        ]

        account_display = await self._get_account_display(ctx)
        verbose = self.service._is_verbose(ctx.chat_id, chat_settings=ctx.chat_settings)

        if verbose:
            media_type = (
                "VIDEO"
                if ctx.media_item.mime_type
                and ctx.media_item.mime_type.startswith("video")
                else "IMAGE"
            )
            if media_type == "IMAGE":
                preview_url = ctx.cloud_service.get_story_optimized_url(ctx.cloud_url)
            else:
                preview_url = ctx.cloud_url

            caption = (
                f"🧪 DRY RUN - Cloudinary Upload Complete\n\n"
                f"📁 File: {ctx.media_item.file_name}\n"
                f"📸 Account: {account_display}\n\n"
                f"✅ Cloudinary upload: Success\n"
                f"🔗 Preview (with blur): {preview_url}\n\n"
                f"⏸️ Stopped before Instagram API\n"
                f"(DRY_RUN_MODE=true)\n\n"
                f"• No Instagram post made\n"
                f"• No history recorded\n"
                f"• No TTL lock created\n"
                f"• Queue item preserved\n\n"
                f"Tested by: {self.service._get_display_name(ctx.user)}"
            )
        else:
            caption = (
                f"🧪 DRY RUN ✅\n\n"
                f"📸 Account: {account_display}\n"
                f"Tested by: {self.service._get_display_name(ctx.user)}"
            )
        await _update_autopost_caption(
            ctx.query, caption, reply_markup=InlineKeyboardMarkup(keyboard)
        )

        self.service.interaction_service.log_callback(
            user_id=str(ctx.user.id),
            callback_name="autopost",
            context={
                "queue_item_id": ctx.queue_id,
                "media_id": str(ctx.queue_item.media_item_id),
                "media_filename": ctx.media_item.file_name,
                "cloud_public_id": ctx.cloud_public_id,
                "dry_run": True,
            },
            telegram_chat_id=ctx.query.message.chat_id,
            telegram_message_id=ctx.query.message.message_id,
        )

        logger.info(
            f"[DRY RUN] Cloudinary upload complete, stopped before Instagram API. "
            f"User: {self.service._get_display_name(ctx.user)}, "
            f"File: {ctx.media_item.file_name}"
        )

        self._cleanup_cloudinary(ctx)

    async def _execute_instagram_post(self, ctx: AutopostContext) -> str | None:
        """Post media to Instagram via the Graph API.

        Returns:
            Instagram story_id string, or None if cancelled.
        """
        if ctx.cancel_flag and ctx.cancel_flag.is_set():
            logger.info(
                f"Auto-post cancelled before Instagram API for {ctx.media_item.file_name}"
            )
            await ctx.query.edit_message_caption(
                caption="❌ Auto-post cancelled (another action was taken)"
            )
            return None

        await ctx.query.edit_message_caption(
            caption="⏳ *Posting to Instagram...*", parse_mode="Markdown"
        )

        media_type = (
            "VIDEO"
            if ctx.media_item.file_path.lower().endswith((".mp4", ".mov"))
            else "IMAGE"
        )

        if media_type == "IMAGE":
            story_url = ctx.cloud_service.get_story_optimized_url(ctx.cloud_url)
        else:
            story_url = ctx.cloud_url

        post_result = await ctx.instagram_service.post_story(
            media_url=story_url,
            media_type=media_type,
            telegram_chat_id=ctx.chat_id,
            # Claim-before-publish (#549): flip the row to 'publishing' and
            # persist the container_id the instant the container exists, before
            # the publish. A crash after publish then leaves a stuck row that
            # can't be reaped or re-served instead of a duplicate story.
            on_container_created=lambda cid: self._mark_container_created(ctx, cid),
        )

        story_id = post_result.get("story_id")
        logger.info(f"Posted to Instagram: story_id={story_id}")
        return story_id

    def _mark_container_created(self, ctx: AutopostContext, container_id: str) -> None:
        """Claim-before-publish: record the container on the context and flip
        the queue row to 'publishing' with the persisted container_id (#549)."""
        ctx.container_id = container_id
        self.service.queue_repo.mark_publishing(
            ctx.queue_id,
            container_id,
            scope_of_row(ctx.queue_item, where="autopost.mark_container_created"),
        )

    def _record_successful_post(self, ctx: AutopostContext, story_id: str) -> None:
        """Record a successful Instagram post in all relevant tables.

        Runs as one atomic transaction so a crash can't leave the story
        recorded-but-still-queued or the row deleted-without-history. The
        history write is idempotent (#551) so a replayed finalize can't
        double-insert.
        """
        now = datetime.now(timezone.utc)
        queue_scope = scope_of_row(
            ctx.queue_item, where="autopost.record_successful_post"
        )
        with atomic_session(
            [
                self.service.history_repo,
                self.service.media_repo,
                self.service.queue_repo,
                self.service.user_repo,
                self.service.lock_service.lock_repo,
            ]
        ):
            self.service.history_repo.create_idempotent(
                HistoryCreateParams(
                    media_item_id=str(ctx.queue_item.media_item_id),
                    queue_item_id=ctx.queue_id,
                    queue_created_at=ctx.queue_item.created_at,
                    queue_deleted_at=now,
                    scheduled_for=ctx.queue_item.scheduled_for,
                    posted_at=now,
                    status="posted",
                    success=True,
                    posted_by_user_id=str(ctx.user.id),
                    posted_by_telegram_username=ctx.user.telegram_username,
                    posting_method="instagram_api",
                    instagram_story_id=story_id,
                    chat_settings_id=str(ctx.queue_item.chat_settings_id)
                    if ctx.queue_item.chat_settings_id
                    else None,
                )
            )
            self.service.media_repo.increment_times_posted(
                str(ctx.queue_item.media_item_id),
                chat_settings_id=queue_scope,
            )
            self.service.lock_service.create_lock(
                str(ctx.queue_item.media_item_id),
                telegram_chat_id=ctx.chat_id,
            )
            self.service.queue_repo.delete(
                ctx.queue_id,
                queue_scope,
            )
            self.service.user_repo.increment_posts(str(ctx.user.id))

    async def _send_success_message(self, ctx: AutopostContext, story_id: str) -> None:
        """Send success message and log interaction after a successful post."""
        verbose = self.service._is_verbose(ctx.chat_id, chat_settings=ctx.chat_settings)
        account_display = await self._get_account_display(ctx)

        if verbose:
            escaped_filename = _escape_markdown(ctx.media_item.file_name)
            caption = (
                f"✅ *Posted to Instagram!*\n\n"
                f"📁 {escaped_filename}\n"
                f"📸 Account: {account_display}\n"
                f"🆔 Story ID: {story_id[:20]}...\n\n"
                f"Posted by: {self.service._get_display_name(ctx.user)}"
            )
        else:
            caption = (
                f"✅ Posted to {account_display} by "
                f"{self.service._get_display_name(ctx.user)}"
            )

        await _update_autopost_caption(ctx.query, caption)

        self.service.interaction_service.log_callback(
            user_id=str(ctx.user.id),
            callback_name="autopost",
            context={
                "queue_item_id": ctx.queue_id,
                "media_id": str(ctx.queue_item.media_item_id),
                "media_filename": ctx.media_item.file_name,
                "instagram_story_id": story_id,
                "dry_run": False,
                "success": True,
            },
            telegram_chat_id=ctx.query.message.chat_id,
            telegram_message_id=ctx.query.message.message_id,
        )

        self.service.interaction_service.log_bot_response(
            response_type="caption_update",
            context={
                "caption": caption,
                "action": "autopost_success",
                "media_filename": ctx.media_item.file_name,
                "instagram_story_id": story_id,
                "edited": True,
            },
            telegram_chat_id=ctx.query.message.chat_id,
            telegram_message_id=ctx.query.message.message_id,
        )

        logger.info(
            f"Auto-posted to Instagram by {self.service._get_display_name(ctx.user)}: "
            f"{ctx.media_item.file_name} (story_id={story_id})"
        )

    def _cleanup_cloudinary(self, ctx: AutopostContext) -> None:
        """Delete the temporary Cloudinary upload and clear DB references.

        Best-effort: failures are logged but never propagate. The safety-net
        cleanup loop in main.py catches any orphaned uploads.
        """
        if not ctx.cloud_public_id:
            return

        try:
            deleted = ctx.cloud_service.delete_media_for_item(ctx.media_item)
            if deleted:
                self.service.media_repo.update_cloud_info(
                    media_id=str(ctx.media_item.id),
                    cloud_url=None,
                    cloud_public_id=None,
                    cloud_uploaded_at=None,
                    cloud_expires_at=None,
                    chat_settings_id=scope_of_row(
                        ctx.queue_item, where="autopost.cleanup_cloudinary"
                    ),
                )
                logger.info(f"Cleaned up Cloudinary upload: {ctx.cloud_public_id}")
            else:
                logger.warning(
                    f"Cloudinary delete returned false for {ctx.cloud_public_id}"
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"Failed to clean up Cloudinary upload {ctx.cloud_public_id}: {e}"
            )

    async def _handle_autopost_error(self, ctx: AutopostContext, e: Exception) -> None:
        """Handle auto-post failure: show error message with recovery options.

        For ``MediaUnsupportedError`` (Meta code 9004) we ALSO create a
        permanent_reject lock on the underlying media_item. The error is
        deterministic per-file (HEIC content, GIF, etc.), so without the
        lock the scheduler would keep re-serving the same item and
        burning through the same failure on every cycle.
        """
        logger.error(f"Auto-post failed: {e}", exc_info=True)

        # A rate-limit rejection is definitive, not ambiguous: Instagram enforces
        # the publishing quota at the media_publish call, so a RateLimitError
        # means the story was never created — whether or not a container already
        # exists. (Fail-open on the pre-publish gate makes a 429 after the
        # container was created a designed path, not a fluke.) So this is NOT the
        # #549 unconfirmed-publish case: if a container was claimed, release the
        # row for retry (the same safe outcome as an IG-confirmed-dead container —
        # nothing published, no duplicate risk), then show the graceful
        # daily-limit card with the manual buttons — never the ambiguous
        # "held for review" dead-end that would strand the row in 'publishing'.
        queue_scope = scope_of_row(
            ctx.queue_item, where="autopost.handle_autopost_error"
        )
        if isinstance(e, RateLimitError):
            if ctx.container_id is not None:
                self.service.queue_repo.update_status(
                    ctx.queue_id,
                    "processing",
                    queue_scope,
                )
            caption = (
                "⚠️ *Instagram daily limit reached*\n\n"
                "You've hit Instagram's daily posting limit. Post manually "
                "below, or try auto-post again tomorrow."
            )
            reply_markup = build_queue_action_keyboard(
                ctx.queue_id,
                enable_instagram_api=bool(ctx.chat_settings.enable_instagram_api),
                error_recovery=True,
            )
            await _update_autopost_caption(
                ctx.query, caption, reply_markup=reply_markup
            )
            self._cleanup_cloudinary(ctx)
            return

        # Claim-before-publish fail-safe (#549): once a container was created the
        # row is in 'publishing'. Classify the failure:
        #   - AMBIGUOUS (IG did NOT confirm failure): the publish outcome is
        #     unknown — the story may be live. Hold the row stuck (excluded from
        #     every sweep, blocks reselection) and do NOT retry; a retry can't
        #     re-claim a 'publishing' row and could otherwise duplicate.
        #   - IG-CONFIRMED-DEAD (ERROR/EXPIRED): IG affirmatively says nothing
        #     published, so release the row for retry (below) — never stranded.
        if ctx.container_id is not None:
            if not is_publish_definitively_failed(e):
                # AMBIGUOUS: hold the row stuck; do NOT retry.
                logger.warning(
                    f"Autopost unconfirmed for {ctx.media_item.file_name} "
                    f"(container {ctx.container_id}) — holding row in 'publishing'; "
                    f"not retrying to prevent a duplicate story"
                )
                await _update_autopost_caption(
                    ctx.query,
                    "⚠️ The post may have gone through but couldn't be confirmed. "
                    "It's been held for review so it can't post twice.",
                )
                self._cleanup_cloudinary(ctx)
                return

            # IG-CONFIRMED-DEAD: release the claimed row — flip it out of
            # 'publishing' back to 'processing' so the retry button can re-claim
            # it (the same safe-retry outcome as if no container had been
            # created) — then fall through to the normal error UI + retry button.
            logger.warning(
                f"Autopost container confirmed-failed for {ctx.media_item.file_name} "
                f"(container {ctx.container_id}) — releasing row for retry"
            )
            self.service.queue_repo.update_status(
                ctx.queue_id,
                "processing",
                queue_scope,
            )

        if isinstance(e, MediaUnsupportedError):
            try:
                self.service.lock_service.create_lock(
                    str(ctx.media_item.id),
                    telegram_chat_id=ctx.chat_id,
                    lock_reason="permanent_reject",
                    ttl_days=None,  # NULL = permanent (per CLAUDE.md lock model)
                )
                logger.warning(
                    f"Permanent-rejected media {ctx.media_item.file_name} "
                    f"after Meta 9004: {e}"
                )
            except Exception as lock_err:  # noqa: BLE001
                # Best-effort — don't let the lock failure mask the
                # original error from the user.
                logger.error(
                    f"Failed to create permanent_reject lock for "
                    f"media {ctx.media_item.id}: {lock_err}"
                )

        user_msg = self._get_user_friendly_error(e)
        caption = (
            f"❌ *Auto Post Failed*\n\n"
            f"{user_msg}\n\n"
            f"You can try again or use manual posting."
        )

        reply_markup = build_queue_action_keyboard(
            ctx.queue_id,
            enable_instagram_api=bool(ctx.chat_settings.enable_instagram_api),
            error_recovery=True,
        )

        await _update_autopost_caption(ctx.query, caption, reply_markup=reply_markup)

        self.service.interaction_service.log_callback(
            user_id=str(ctx.user.id),
            callback_name="autopost",
            context={
                "queue_item_id": ctx.queue_id,
                "media_id": str(ctx.queue_item.media_item_id),
                "media_filename": ctx.media_item.file_name,
                "dry_run": False,
                "success": False,
                "error": str(e)[:200],
            },
            telegram_chat_id=ctx.query.message.chat_id,
            telegram_message_id=ctx.query.message.message_id,
        )

        self._cleanup_cloudinary(ctx)

    @staticmethod
    def _get_user_friendly_error(e: Exception) -> str:
        """Map internal exceptions to user-friendly error messages."""
        if isinstance(e, MediaUploadError):
            return "Failed to prepare media for Instagram. This is a server issue — please contact the admin."
        if isinstance(e, MediaUnsupportedError):
            return (
                "Instagram couldn't process this file (Meta error 9004). "
                "It may be a HEIC photo or an unsupported format. This media "
                "has been permanently rejected and won't be scheduled again."
            )
        if isinstance(e, TokenRevokedError):
            return "Instagram account has been disconnected (password change or app removed). Please reconnect in Settings."
        if isinstance(e, TokenCorruptError):
            return "Instagram token is invalid and cannot be used. Please reconnect your account in Settings."
        if isinstance(e, TokenExpiredError):
            return "Instagram connection has expired. Please reconnect your account in Settings."
        if isinstance(e, InstagramAPIError):
            return f"Instagram rejected the post: {e}"
        return f"An unexpected error occurred: {str(e)[:150]}"
