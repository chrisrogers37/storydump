"""Tests for base exception hierarchy."""

import pytest

from src.exceptions.base import RefusalError, StorydumpError
from src.services.target.bindings import BindingRefused
from src.services.target.category_mix import MixInvalid
from src.services.target.email_sender import EmailRefused
from src.services.target.google_drive_oauth import DriveOAuthRefused
from src.services.target.google_oidc import OidcRefused
from src.services.target.ig_login_oauth import IgOAuthRefused
from src.services.target.invitations import InvitationRefused


@pytest.mark.unit
class TestStorydumpError:
    """Tests for the StorydumpError base exception."""

    def test_inherits_from_exception(self):
        """StorydumpError inherits from Exception."""
        assert issubclass(StorydumpError, Exception)

    def test_can_be_raised_and_caught(self):
        """StorydumpError can be raised and caught."""
        with pytest.raises(StorydumpError, match="test error"):
            raise StorydumpError("test error")

    def test_caught_by_exception_handler(self):
        """StorydumpError is caught by a generic Exception handler."""
        with pytest.raises(Exception):
            raise StorydumpError("generic catch")

    def test_message_stored(self):
        """Error message is accessible via args."""
        err = StorydumpError("my message")
        assert str(err) == "my message"
        assert err.args == ("my message",)

    def test_empty_message(self):
        """StorydumpError works with empty message."""
        err = StorydumpError()
        assert str(err) == ""


#: Every `RefusalError` subclass that declares a prefix, with a reason its own
#: vocabulary guard accepts. The pin is the rendered message: a class that
#: hand-rolls `__init__` again fails here rather than at review.
_REFUSALS = [
    (OidcRefused, "nonce", "sign-in refused"),
    (IgOAuthRefused, "exchange_failed", "instagram grant refused"),
    (BindingRefused, "unknown_chat_type", "binding refused"),
    (MixInvalid, "sum_not_one", "mix invalid"),
    (InvitationRefused, "not_acceptable", "invitation refused"),
    (DriveOAuthRefused, "exchange_failed", "drive grant refused"),
    (EmailRefused, "recipient_invalid", "email refused"),
]


@pytest.mark.unit
@pytest.mark.parametrize(
    "cls,reason,prefix", _REFUSALS, ids=[c.__name__ for c, _, _ in _REFUSALS]
)
class TestRefusalErrorSubclasses:
    """The base owns the constructor; a subclass declares only its prefix."""

    def test_subclasses_refusal_error(self, cls, reason, prefix):
        assert issubclass(cls, RefusalError)

    def test_message_is_prefix_reason_detail(self, cls, reason, prefix):
        assert str(cls(reason, "why")) == f"{prefix}: {reason} — why"

    def test_message_without_detail_is_bare(self, cls, reason, prefix):
        assert str(cls(reason)) == f"{prefix}: {reason}"

    def test_reason_is_carried(self, cls, reason, prefix):
        assert cls(reason, "why").reason == reason
