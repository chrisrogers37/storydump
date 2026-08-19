"""#909 — a naive DateTime column compared against an aware cutoff.

The defect this pins does **not** raise. `posted_at` is a naive `DateTime`
column; comparing it to `datetime.now(timezone.utc) - timedelta(...)` makes
Postgres cast the aware parameter using the **session's** `TimeZone`, so the
window silently shifts by that offset. Right on a UTC session, wrong on any
other — right on one connection and wrong on another, with a believable number
either way. A crash reports itself; this recruits the reader as its accomplice.

**The session timezone is PINNED here, not inherited.** That is the whole
point: a test that takes whatever the server happens to default to passes on
this host's CI (the `postgres:15` image defaults to `Etc/UTC`) and would have
gone on passing while the bug shipped. Each case sets `TimeZone` explicitly and
asserts the result is the same under both.

Real database, because the defect lives in the SQL cast. A mocked session would
assert that `.filter()` was called with something, which is exactly the shape of
test that cannot fail on this.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from src.repositories.chat_settings_repository import ChatSettingsRepository
from src.repositories.history_repository import HistoryCreateParams, HistoryRepository
from src.repositories.media_repository import MediaRepository
from src.repositories.tenant_scope import SYSTEM_SCOPE

#: Far enough outside a 1-hour window that no rounding explains a match, and
#: far enough INSIDE any plausible session offset (>= 1h) that the buggy cast
#: pulls it in. New York is -4/-5h, so a 90-minute-old row lands inside the
#: shifted window and outside the true one — the discriminator.
ROW_AGE = timedelta(minutes=90)
WINDOW_HOURS = 1

#: Two sessions whose UTC offsets differ by hours. `UTC` is the case CI
#: happens to run under, so it is the one that hides the bug; the other is
#: this host's own default, which is how it was found.
TIMEZONES = ["UTC", "America/New_York"]


@pytest.fixture(autouse=True)
def _route_repos_to_test_db(setup_test_database, monkeypatch):
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


def _tenant() -> tuple:
    import random

    telegram_chat_id = -random.randint(10**11, 10**12)
    settings = ChatSettingsRepository().get_or_create(telegram_chat_id)
    return str(settings.id), telegram_chat_id


def _post(tenant_id: str, *, age: timedelta) -> str:
    """One posting_history row `age` old, written in the naive-UTC convention.

    Signatures follow `test_history_usage_by_tenant`'s seeding helper — the
    same schema, so a second spelling here would drift from it.
    """
    media_repo = MediaRepository()
    try:
        media = media_repo.create(
            file_path=f"/test/tz/{uuid4()}.jpg",
            file_name="tz.jpg",
            file_hash=uuid4().hex,
            file_size_bytes=1024,
            mime_type="image/jpeg",
            chat_settings_id=SYSTEM_SCOPE,
        )
        media_id = media.id
    finally:
        media_repo.close()

    posted_at = datetime.now(timezone.utc).replace(tzinfo=None) - age
    repo = HistoryRepository()
    try:
        row = repo.create(
            HistoryCreateParams(
                media_item_id=media_id,
                queue_item_id=str(uuid4()),
                queue_created_at=posted_at,
                queue_deleted_at=posted_at,
                scheduled_for=posted_at,
                posted_at=posted_at,
                status="posted",
                success=True,
                chat_settings_id=tenant_id,
            )
        )
        return row.id
    finally:
        repo.close()


def _recent_ids(tenant_id: str, tz_name: str) -> list:
    """`get_recent_posts` under a PINNED session timezone.

    The timezone is set on the REPOSITORY'S OWN session rather than on a
    session made here, because `HistoryRepository.db` is a read-only property
    backed by the shared session ContextVar — there is no way to inject one,
    and a separately-created session would be pinned while the query ran
    somewhere else. Setting it on `repo.db` puts it on exactly the connection
    the query uses, which is the only placement that means anything.

    Reset to UTC before closing: the connection goes back to the pool, and a
    later test inheriting `America/New_York` would be this same bug wearing a
    test-suite costume.
    """
    repo = HistoryRepository()
    try:
        repo.db.execute(text(f"SET TIME ZONE '{tz_name}'"))
        confirmed = repo.db.execute(text("SHOW TimeZone")).scalar()
        assert confirmed == tz_name, (
            f"the session timezone did not take ({confirmed!r}) — this test"
            " would then be measuring whatever the server defaults to, which"
            " is exactly the thing that let #909 ship"
        )
        rows = repo.get_recent_posts(
            hours=WINDOW_HOURS, chat_settings_id=tenant_id, limit=100
        )
        return [r.id for r in rows]
    finally:
        try:
            repo.db.execute(text("SET TIME ZONE 'UTC'"))
            repo.db.commit()
        finally:
            repo.close()


@pytest.mark.integration
class TestGetRecentPostsIsSessionTimezoneInvariant:
    def test_a_row_outside_the_window_is_excluded_under_BOTH_timezones(self):
        """The fix, stated as the property that matters.

        Under the defect this fails on `America/New_York` alone: the aware
        cutoff is cast with the session offset, moving the window boundary
        hours earlier, and a 90-minute-old row lands inside a 1-hour window.
        """
        tenant_id, _ = _tenant()
        record_id = _post(tenant_id, age=ROW_AGE)
        try:
            results = {tz: _recent_ids(tenant_id, tz) for tz in TIMEZONES}
        finally:
            pass

        for tz, ids in results.items():
            assert record_id not in ids, (
                f"a {int(ROW_AGE.total_seconds() // 60)}-minute-old row matched"
                f" a {WINDOW_HOURS}-hour window under TimeZone={tz!r} — the"
                " cutoff was cast with the session offset (#909)"
            )
        assert results[TIMEZONES[0]] == results[TIMEZONES[1]], (
            f"the same query returned different rows under different session"
            f" timezones: {results} — correctness is riding on the server's"
            " default rather than on the code"
        )

    def test_a_row_INSIDE_the_window_is_included_under_both_timezones(self):
        """The positive control. Without it, a `get_recent_posts` that returned
        nothing at all would satisfy the exclusion test above."""
        tenant_id, _ = _tenant()
        record_id = _post(tenant_id, age=timedelta(minutes=5))
        try:
            results = {tz: _recent_ids(tenant_id, tz) for tz in TIMEZONES}
        finally:
            pass

        for tz, ids in results.items():
            assert record_id in ids, (
                f"a 5-minute-old row was excluded from a {WINDOW_HOURS}-hour"
                f" window under TimeZone={tz!r} — the window is wrong in the"
                " other direction"
            )


@pytest.mark.integration
class TestTheCastIsWhatMovedTheWindow:
    """The mechanism, isolated from the repository.

    Named separately because the repository test above proves the OUTCOME, and
    an outcome test cannot say *why*. This says why, in one statement, so a
    future reader does not have to re-derive that Postgres casts the aware
    parameter rather than the naive column.
    """

    def test_an_aware_parameter_shifts_with_the_session_but_a_naive_one_does_not(
        self,
    ):
        import src.config.database as db_module

        aware = datetime.now(timezone.utc)
        naive = aware.replace(tzinfo=None)

        seen = {}
        session = db_module.SessionLocal()
        try:
            for tz_name in TIMEZONES:
                session.execute(text(f"SET TIME ZONE '{tz_name}'"))
                seen[tz_name] = session.execute(
                    text("SELECT CAST(:a AS timestamp), CAST(:n AS timestamp)"),
                    {"a": aware, "n": naive},
                ).first()
            session.execute(text("SET TIME ZONE 'UTC'"))
            session.commit()
        finally:
            session.close()

        aware_utc, naive_utc_val = seen["UTC"]
        aware_ny, naive_ny = seen["America/New_York"]

        assert naive_utc_val == naive_ny, (
            "a NAIVE parameter changed with the session timezone — then the"
            " fix would not be a fix"
        )
        assert aware_utc != aware_ny, (
            "an AWARE parameter did NOT change with the session timezone on"
            " this server, so this test is not exercising the cast #909 is"
            " about — check the server build before trusting the fix"
        )
        assert abs(aware_utc - aware_ny) >= timedelta(hours=1), (
            f"the shift was only {abs(aware_utc - aware_ny)} — too small to"
            " move a row across a one-hour window, so the repository test"
            " above would pass for the wrong reason"
        )
