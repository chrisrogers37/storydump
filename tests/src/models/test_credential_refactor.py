"""Tests for credential refactor Phase 1 — additive schema changes."""

from pathlib import Path

import pytest

from src.models.api_token import ApiToken


MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "scripts" / "migrations"


@pytest.mark.unit
class TestMetaAccountIdColumn:
    """Verify the meta_account_id column on ApiToken."""

    def test_column_exists_on_model(self):
        assert hasattr(ApiToken, "meta_account_id")

    def test_column_is_nullable(self):
        col = ApiToken.__table__.columns["meta_account_id"]
        assert col.nullable is True

    def test_column_type_is_string(self):
        col = ApiToken.__table__.columns["meta_account_id"]
        assert str(col.type) == "VARCHAR(100)"

    def test_column_default_is_none(self):
        """New tokens created without meta_account_id get NULL."""
        token = ApiToken(
            service_name="instagram",
            token_type="access_token",
            token_value="encrypted",
            issued_at=None,
        )
        assert token.meta_account_id is None

    def test_column_accepts_value(self):
        token = ApiToken(
            service_name="instagram",
            token_type="access_token",
            token_value="encrypted",
            issued_at=None,
            meta_account_id="17841400123456789",
        )
        assert token.meta_account_id == "17841400123456789"


@pytest.mark.unit
class TestMigration035:
    """Verify migration 035 file structure."""

    def test_migration_file_exists(self):
        path = MIGRATIONS_DIR / "035_credential_refactor_add_meta_account_id.sql"
        assert path.exists(), f"Migration file missing: {path}"

    def test_migration_adds_column(self):
        sql = (
            MIGRATIONS_DIR / "035_credential_refactor_add_meta_account_id.sql"
        ).read_text()
        assert "ADD COLUMN" in sql
        assert "meta_account_id" in sql

    def test_migration_creates_index(self):
        sql = (
            MIGRATIONS_DIR / "035_credential_refactor_add_meta_account_id.sql"
        ).read_text()
        assert "CREATE INDEX" in sql
        assert "api_tokens_meta_account_id_idx" in sql

    def test_migration_is_idempotent(self):
        """IF NOT EXISTS guards allow re-running safely."""
        sql = (
            MIGRATIONS_DIR / "035_credential_refactor_add_meta_account_id.sql"
        ).read_text()
        assert sql.count("IF NOT EXISTS") >= 2

    def test_migration_uses_transaction(self):
        sql = (
            MIGRATIONS_DIR / "035_credential_refactor_add_meta_account_id.sql"
        ).read_text()
        assert "BEGIN;" in sql
        assert "COMMIT;" in sql

    def test_migration_records_schema_version(self):
        sql = (
            MIGRATIONS_DIR / "035_credential_refactor_add_meta_account_id.sql"
        ).read_text()
        assert "INSERT INTO schema_version" in sql
        assert "35" in sql
