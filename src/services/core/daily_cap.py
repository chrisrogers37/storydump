"""Daily posting cap guard — single source of truth for the daily limit."""

from src.utils.logger import logger


def can_post_today(chat_settings, history_repo, queue_repo) -> bool:
    """Check whether the daily posting cap allows another post.

    Counts both finalized posts (posting_history) AND in-flight 'publishing'
    queue rows. A 'publishing' row is a claimed-but-unconfirmed publish: the
    container was created and the story may already be live, but the finalize
    hasn't recorded history yet (or crashed before it could). Counting it means
    a crashed mid-publish story is counted exactly once — no under-count that
    would let an over-cap post slip through (#549).

    Args:
        chat_settings: ChatSettings row (needs .id, .posts_per_day, .posting_timezone)
        history_repo: HistoryRepository instance
        queue_repo: QueueRepository instance (for the in-flight publishing count)

    Returns:
        True if another post is allowed, False if the cap is reached.
    """
    today_count = history_repo.count_posts_today(
        chat_settings_id=str(chat_settings.id),
        posting_timezone=chat_settings.posting_timezone,
    )
    publishing_count = queue_repo.count_by_status(
        ["publishing"], chat_settings_id=str(chat_settings.id)
    )
    used = today_count + publishing_count

    if used >= chat_settings.posts_per_day:
        logger.info(
            f"Daily cap reached: {used}/{chat_settings.posts_per_day} "
            f"({today_count} posted + {publishing_count} publishing) "
            f"for chat_settings {chat_settings.id}"
        )
        return False

    return True
