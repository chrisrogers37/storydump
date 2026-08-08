"""Integration tests: per-tenant usage aggregation over posting_history.

Real DB, because the correctness here lives in SQL — the conditional sums, the
GROUP BY, and the window bounds. A mocked repository would assert only that the
method was called, which is the shape of test that cannot fail on the defects
worth catching.

Routes the production session factory at the current-schema test DB (same
pattern as test_queue_sweep_status); every row is deleted in ``finally``.
"""

from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.orm import sessionmaker

from src.repositories.chat_settings_repository import ChatSettingsRepository
from src.repositories.history_repository import HistoryCreateParams, HistoryRepository
from src.repositories.media_repository import MediaRepository


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
    """Create a real chat_settings row — chat_settings_id is a live FK.

    Returns ``(chat_settings_id, telegram_chat_id)``; caller cleans up.
    """
    import random

    telegram_chat_id = -random.randint(10**11, 10**12)
    repo = ChatSettingsRepository()
    try:
        settings = repo.get_or_create(telegram_chat_id)
        return str(settings.id), telegram_chat_id
    finally:
        repo.close()


def _delete_tenants(tenant_ids):
    from src.models.chat_settings import ChatSettings

    repo = ChatSettingsRepository()
    try:
        for tid in tenant_ids:
            repo.db.query(ChatSettings).filter(ChatSettings.id == tid).delete()
        repo.db.commit()
    finally:
        repo.close()


def _post(
    *,
    posted_at: datetime,
    chat_settings_id=None,
    success: bool = True,
    method: str = "telegram_manual",
):
    """Create one posting_history row. Returns (media_id, history_id)."""
    media_repo = MediaRepository()
    try:
        media = media_repo.create(
            file_path=f"/test/usage/{uuid4()}.jpg",
            file_name="usage.jpg",
            file_hash=uuid4().hex,
            file_size_bytes=1024,
            mime_type="image/jpeg",
        )
        media_id = media.id
    finally:
        media_repo.close()

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
                status="posted" if success else "failed",
                success=success,
                posting_method=method,
                chat_settings_id=chat_settings_id,
            )
        )
        return media_id, row.id
    finally:
        repo.close()


def _cleanup(created):
    from src.models.media_item import MediaItem
    from src.models.posting_history import PostingHistory

    repo = HistoryRepository()
    try:
        for media_id, history_id in created:
            repo.db.query(PostingHistory).filter(
                PostingHistory.id == history_id
            ).delete()
            repo.db.query(MediaItem).filter(MediaItem.id == media_id).delete()
        repo.db.commit()
    finally:
        repo.close()


@pytest.mark.integration
class TestUsageByTenant:
    def test_groups_by_tenant_and_splits_outcome_and_method(self):
        """The load-bearing SQL: GROUP BY plus both conditional sums."""
        now = datetime.utcnow()
        tenant_a, _ = _tenant()
        tenant_b, _ = _tenant()
        created = []
        try:
            created.append(
                _post(posted_at=now - timedelta(hours=1), chat_settings_id=tenant_a)
            )
            created.append(
                _post(
                    posted_at=now - timedelta(hours=2),
                    chat_settings_id=tenant_a,
                    method="instagram_api",
                )
            )
            created.append(
                _post(
                    posted_at=now - timedelta(hours=3),
                    chat_settings_id=tenant_a,
                    success=False,
                )
            )
            created.append(
                _post(
                    posted_at=now - timedelta(hours=1),
                    chat_settings_id=tenant_b,
                    method="instagram_api",
                )
            )

            repo = HistoryRepository()
            try:
                rows = repo.usage_by_tenant(since=now - timedelta(days=1))
            finally:
                repo.close()

            by_tenant = {r["chat_settings_id"]: r for r in rows}

            a = by_tenant[tenant_a]
            assert a["total"] == 3
            assert a["successful"] == 2
            assert a["failed"] == 1
            assert a["api_posts"] == 1
            assert a["manual_posts"] == 2

            b = by_tenant[tenant_b]
            assert b["total"] == 1
            assert b["api_posts"] == 1
            assert b["manual_posts"] == 0
        finally:
            _cleanup(created)
            _delete_tenants([tenant_a, tenant_b])

    def test_reports_untenanted_rows_rather_than_dropping_them(self):
        """Legacy rows carry a NULL tenant; totals must still reconcile."""
        now = datetime.utcnow()
        created = []
        try:
            created.append(
                _post(posted_at=now - timedelta(hours=1), chat_settings_id=None)
            )

            repo = HistoryRepository()
            try:
                rows = repo.usage_by_tenant(since=now - timedelta(hours=6))
            finally:
                repo.close()

            untenanted = [r for r in rows if r["chat_settings_id"] is None]
            assert len(untenanted) == 1
            assert untenanted[0]["total"] >= 1
        finally:
            _cleanup(created)

    def test_window_bounds_exclude_rows_outside_them(self):
        """`since` is inclusive and `until` exclusive — both are enforced in SQL."""
        now = datetime.utcnow()
        tenant, _ = _tenant()
        created = []
        try:
            created.append(
                _post(posted_at=now - timedelta(days=10), chat_settings_id=tenant)
            )
            created.append(
                _post(posted_at=now - timedelta(hours=1), chat_settings_id=tenant)
            )

            repo = HistoryRepository()
            try:
                recent = repo.usage_by_tenant(since=now - timedelta(days=2))
                old_only = repo.usage_by_tenant(
                    since=now - timedelta(days=20), until=now - timedelta(days=5)
                )
            finally:
                repo.close()

            recent_row = next(r for r in recent if r["chat_settings_id"] == tenant)
            assert recent_row["total"] == 1, "the 10-day-old row must be excluded"

            old_row = next(r for r in old_only if r["chat_settings_id"] == tenant)
            assert old_row["total"] == 1, "only the old row falls inside this window"
        finally:
            _cleanup(created)
            _delete_tenants([tenant])
