"""Daily posting cap guard — single source of truth for the daily limit."""

from src.utils.logger import logger


def can_post_today(chat_settings, history_repo) -> bool:
    """Check whether the daily posting cap allows another post.

    Args:
        chat_settings: ChatSettings row (needs .id, .posts_per_day, .posting_timezone)
        history_repo: HistoryRepository instance

    Returns:
        True if another post is allowed, False if the cap is reached.
    """
    today_count = history_repo.count_posts_today(
        chat_settings_id=str(chat_settings.id),
        posting_timezone=chat_settings.posting_timezone,
    )

    if today_count >= chat_settings.posts_per_day:
        logger.info(
            f"Daily cap reached: {today_count}/{chat_settings.posts_per_day} "
            f"for chat_settings {chat_settings.id}"
        )
        return False

    return True
