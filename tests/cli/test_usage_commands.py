"""Tests for usage CLI commands."""

import pytest
from unittest.mock import Mock, MagicMock, patch
from click.testing import CliRunner

from cli.commands.usage import usage_report


def _row(tenant, total, successful, api):
    return {
        "chat_settings_id": tenant,
        "total": total,
        "successful": successful,
        "failed": total - successful,
        "api_posts": api,
        "manual_posts": total - api,
    }


@pytest.mark.unit
class TestUsageReportCommand:
    """Tests for the usage-report CLI command."""

    @patch("cli.commands.usage.UsageService")
    def test_reports_rows_and_totals(self, mock_service_class):
        mock_service = MagicMock()
        mock_service_class.return_value.__enter__ = Mock(return_value=mock_service)
        mock_service_class.return_value.__exit__ = Mock(return_value=False)
        mock_service.usage_by_tenant.return_value = [_row("tenant-a", 10, 9, 6)]

        result = CliRunner().invoke(usage_report, ["--days", "7"])

        assert result.exit_code == 0
        assert "tenant-a" in result.output

    @patch("cli.commands.usage.UsageService")
    def test_queries_once_per_report(self, mock_service_class):
        """The totals line is folded from the rows, not fetched again."""
        mock_service = MagicMock()
        mock_service_class.return_value.__enter__ = Mock(return_value=mock_service)
        mock_service_class.return_value.__exit__ = Mock(return_value=False)
        mock_service.usage_by_tenant.return_value = [_row("tenant-a", 10, 9, 6)]

        CliRunner().invoke(usage_report, [])

        assert mock_service.usage_by_tenant.call_count == 1
        mock_service.usage_totals.assert_not_called()

    @patch("cli.commands.usage.UsageService")
    def test_empty_window_is_reported_not_an_error(self, mock_service_class):
        mock_service = MagicMock()
        mock_service_class.return_value.__enter__ = Mock(return_value=mock_service)
        mock_service_class.return_value.__exit__ = Mock(return_value=False)
        mock_service.usage_by_tenant.return_value = []

        result = CliRunner().invoke(usage_report, [])

        assert result.exit_code == 0
        assert "No posting activity" in result.output

    @patch("cli.commands.usage.UsageService")
    def test_rejects_a_nonpositive_limit(self, mock_service_class):
        """`--limit 0` once meant 'no limit' and `--limit -1` dropped a tenant."""
        result = CliRunner().invoke(usage_report, ["--limit", "0"])

        assert result.exit_code != 0
        mock_service_class.assert_not_called()
