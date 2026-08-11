"""Tests for UsageService — read-only per-tenant usage measurement."""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from src.services.core.usage_service import UsageService


@pytest.fixture
def usage_service():
    service = UsageService()
    service.history_repo = Mock()
    return service


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
class TestUsageByTenant:
    def test_passes_a_trailing_window_to_the_repository(self, usage_service):
        """The window is days back from `until`, not an open-ended scan."""
        until = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
        usage_service.history_repo.usage_by_tenant.return_value = []

        usage_service.usage_by_tenant(days=7, until=until)

        kwargs = usage_service.history_repo.usage_by_tenant.call_args.kwargs
        assert kwargs["until"] == until
        assert kwargs["since"] == until - timedelta(days=7)

    def test_defaults_to_now_when_no_end_given(self, usage_service):
        usage_service.history_repo.usage_by_tenant.return_value = []
        before = datetime.now(timezone.utc)

        usage_service.usage_by_tenant(days=1)

        since = usage_service.history_repo.usage_by_tenant.call_args.kwargs["since"]
        assert before - timedelta(days=1, seconds=5) <= since

    def test_rejects_a_window_shorter_than_a_day(self, usage_service):
        """A zero or negative window would silently report nothing."""
        with pytest.raises(ValueError, match="at least 1"):
            usage_service.usage_by_tenant(days=0)

        usage_service.history_repo.usage_by_tenant.assert_not_called()

    def test_returns_repository_rows_unchanged(self, usage_service):
        rows = [_row("tenant-a", 10, 9, 6)]
        usage_service.history_repo.usage_by_tenant.return_value = rows

        assert usage_service.usage_by_tenant(days=30) == rows


@pytest.mark.unit
class TestUsageTotals:
    def test_sums_every_column_across_tenants(self):
        totals = UsageService.summarize(
            [_row("tenant-a", 10, 9, 6), _row("tenant-b", 5, 4, 0)]
        )

        assert totals == {
            "tenants": 2,
            "total": 15,
            "successful": 13,
            "failed": 2,
            "api_posts": 6,
            "manual_posts": 9,
        }

    def test_counts_the_untenanted_bucket_as_a_tenant_row(self):
        """Pre-tenancy rows are reported, not dropped — totals must reconcile."""
        totals = UsageService.summarize(
            [_row("tenant-a", 4, 4, 4), _row(None, 6, 5, 0)]
        )

        assert totals["tenants"] == 2
        assert totals["total"] == 10

    def test_empty_window_totals_to_zero_not_an_error(self):
        assert UsageService.summarize([]) == {
            "tenants": 0,
            "total": 0,
            "successful": 0,
            "failed": 0,
            "api_posts": 0,
            "manual_posts": 0,
        }

    def test_usage_totals_queries_once_and_folds(self, usage_service):
        """One window per report: the fold must not re-run the aggregate."""
        usage_service.history_repo.usage_by_tenant.return_value = [
            _row("tenant-a", 3, 3, 1)
        ]

        totals = usage_service.usage_totals(days=30)

        assert usage_service.history_repo.usage_by_tenant.call_count == 1
        assert totals["total"] == 3


@pytest.mark.unit
class TestEnforcementIsAbsentByConstruction:
    """#661 ships metering with enforcement OFF.

    These assert the property structurally rather than trusting review: this
    slice must not acquire a way to refuse a request, and a future edit that
    adds one should fail here rather than ship quietly.
    """

    def test_service_never_writes(self, usage_service):
        """No repository call from this service mutates."""
        usage_service.history_repo.usage_by_tenant.return_value = []

        usage_service.usage_by_tenant(days=30)
        UsageService.summarize([])

        called = {c[0] for c in usage_service.history_repo.method_calls}
        assert called == {"usage_by_tenant"}

    def test_module_raises_nothing_that_could_refuse_a_request(self):
        """Structural, not textual: the only exception raised is input validation.

        An earlier version of this test grepped the source for words like
        "deny" and "entitle" — and tripped on the docstring explaining that
        this module does not enforce. Matching prose is not a property check;
        the AST is.
        """
        import ast
        import pathlib as _pathlib

        import src.services.core.usage_service as module

        tree = ast.parse(_pathlib.Path(module.__file__).read_text())
        raised = {
            node.exc.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Raise)
            and isinstance(node.exc, ast.Call)
            and isinstance(node.exc.func, ast.Name)
        }
        assert raised <= {"ValueError"}, (
            f"usage_service raises {raised - {'ValueError'}} — this slice is "
            "measurement only; a refusal path belongs behind FC-9's admission "
            "seam, not here"
        )

    def test_module_imports_nothing_that_could_enforce(self):
        """It reaches one repository and the base service. Nothing else."""
        import ast
        import pathlib as _pathlib

        import src.services.core.usage_service as module

        tree = ast.parse(_pathlib.Path(module.__file__).read_text())
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        imported |= {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        # Bound the FIRST-PARTY edges only. Asserting the exact set would fail
        # on adding a logger, which says nothing about enforcement; what must
        # not appear is a reach into settings, limits, or admission.
        first_party = {name for name in imported if name.startswith("src.")}
        assert first_party <= {
            "src.repositories.history_repository",
            "src.services.base_service",
        }, f"unexpected first-party import: {first_party}"
