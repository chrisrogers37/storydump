"""Daily posting cap guard — single source of truth for the daily limit."""

from datetime import datetime, timedelta

from src.utils.logger import logger

# A 'publishing' queue row is a claimed, not-yet-finalized Instagram publish. It
# taxes the daily cap only while it is plausibly still in flight: a publish
# resolves within the 180s post_story wall-clock cap, so a publishing row older
# than this bound is presumed stuck (crashed mid-publish, or an IG-confirmed
# dead container that hasn't been released yet) and no longer counts — otherwise
# a single stuck row would wedge the cap forever with no recovery. This is "stop
# counting a stale claim," not reconciliation: the row itself is never deleted
# or rolled forward here. The bound sits well beyond the 180s publish cap but
# short enough that the cap self-heals within minutes.
PUBLISHING_CAP_MAX_AGE_MINUTES = 15


def can_post_today(chat_settings, history_repo, queue_repo) -> bool:
    """Check whether the daily posting cap allows another post.

    Counts both finalized posts (posting_history) AND *recent* in-flight
    'publishing' queue rows. A 'publishing' row is a claimed-but-unconfirmed
    publish: the container was created and the story may already be live, but
    the finalize hasn't recorded history yet (or crashed before it could).
    Counting a fresh one means a crashed mid-publish story is counted exactly
    once — no under-count that would let an over-cap post slip through (#549).
    The count is time-bounded (see ``PUBLISHING_CAP_MAX_AGE_MINUTES``) so a
    stranded row can't silently consume a cap slot forever.

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
    since = datetime.utcnow() - timedelta(minutes=PUBLISHING_CAP_MAX_AGE_MINUTES)
    publishing_count = queue_repo.count_recent_by_status(
        ["publishing"], since=since, chat_settings_id=str(chat_settings.id)
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
