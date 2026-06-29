"""Shared fixtures for API-layer tests."""

import pytest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from src.api.app import app
from src.api.rate_limit import limiter

VALID_USER = {"user_id": 12345, "first_name": "Chris"}
CHAT_ID = -1001234567890


@pytest.fixture(autouse=True)
def _disable_rate_limits():
    """Disable SlowAPI rate limiting during tests to avoid cross-test 429s."""
    limiter.enabled = False
    yield
    limiter.enabled = True


@pytest.fixture(autouse=True)
def _authorize_membership_by_default():
    """Default every API test to an authorized member.

    ``_validate_request`` requires an active membership when the auth token
    carries no bound ``chat_id`` — the DM Mini App case that ``mock_validate``
    simulates by default. Tests that exercise the membership gate request this
    fixture and tweak ``is_active_member``; all others assume a real member.
    """
    with patch("src.api.routes.onboarding.helpers.MembershipService") as mock_cls:
        svc = service_ctx(mock_cls)
        svc.is_active_member.return_value = True
        yield mock_cls


@pytest.fixture
def client():
    return TestClient(app)


def mock_validate(return_value=None):
    """Patch validate_init_data to skip HMAC validation in tests.

    The default return has no chat_id, simulating DM-opened Mini Apps.
    Pass chat_id in return_value to test group-chat initData.
    """
    return patch(
        "src.api.routes.onboarding.helpers.validate_init_data",
        return_value=return_value or VALID_USER,
    )


def service_ctx(mock_cls):
    """Set up __enter__/__exit__ on mock_cls.return_value for context manager use.

    Returns the mock service instance (mock_cls.return_value) configured
    so that ``with ServiceClass() as svc:`` works in the code under test.
    """
    mock_svc = mock_cls.return_value
    mock_svc.__enter__ = Mock(return_value=mock_svc)
    mock_svc.__exit__ = Mock(return_value=False)
    return mock_svc
