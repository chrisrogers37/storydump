"""Settings validation must never put a field's VALUE in its error (#775).

`Settings` declares bare-named fields like ``TELEGRAM_BOT_TOKEN``. pydantic
reads whatever is in the ambient environment under that name, and on a
validation failure its ``ValidationError`` renders ``input_value=`` containing a
truncated copy of the offending input. Where the ambient variable belongs to
some *other* Telegram bot, that prints part of an unrelated real credential.

This fired four times in one evening for four different operators, which is what
makes it a property of the code rather than of anyone's shell.

Every test here uses a SYNTHETIC sentinel. No real credential is involved, and
no test prints the sentinel on failure — they assert on its absence and report
only how many characters leaked.

WHY NOT SecretStr, since that is the obvious tool and the issue suggests it:
measured, it does not fix this shape. The observed error is ``missing`` on a
DIFFERENT field, and that error's ``input_value`` is the whole RAW input dict,
which pydantic assembles *before* field types apply — so a ``SecretStr``
annotation, which only changes how a parsed value reprs, never gets a chance to
mask anything. ``test_secretstr_alone_would_not_have_been_enough`` pins that
finding so the fix is not "simplified" back to it later.
"""

import pytest
from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.config.settings import Settings, SettingsError

# Long enough that pydantic's truncation still leaves a distinctive run, and
# obviously fake so it can never be mistaken for a real token in a log.
SENTINEL = "1234567890:SYNTHETICsentinelTOKENvalueDoNotUseXYZ"

# The shortest run of sentinel characters we treat as a leak. Pydantic's
# truncated form keeps a leading char and a long tail, so a real disclosure
# shows far more than this; 8 is short enough to catch a partial echo and long
# enough not to fire on incidental substrings like "1234".
MIN_LEAK_RUN = 8


def longest_leaked_run(text: str) -> int:
    """Length of the longest SENTINEL substring appearing in `text`.

    Returns 0 when nothing of length >= MIN_LEAK_RUN appears. Deliberately
    returns a LENGTH rather than the fragment: a failing assertion must not
    print credential-shaped material into CI output, which would recreate the
    defect inside its own test.
    """
    best = 0
    for start in range(len(SENTINEL)):
        for end in range(len(SENTINEL), start + MIN_LEAK_RUN - 1, -1):
            if SENTINEL[start:end] in text:
                best = max(best, end - start)
                break
    return best


def _render(exc: BaseException) -> str:
    """Everything a caller could plausibly print, including the cause chain.

    `str(exc)` alone is not the surface: an uncaught exception prints its
    __cause__ and __context__ too, so `raise ... from exc` would re-expose the
    original ValidationError's message under "The above exception was the
    direct cause". Walking the chain here is what makes
    test_the_original_error_is_not_chained meaningful.
    """
    parts = []
    seen = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(f"{type(current).__name__}: {current}")
        parts.append(repr(current))
        current = current.__cause__ or current.__context__
    return "\n".join(parts)


@pytest.mark.unit
class TestSettingsErrorCarriesNoValues:
    """A failed Settings load names fields; it never quotes their contents."""

    def test_missing_required_field_does_not_echo_a_sibling_value(self, monkeypatch):
        """The exact #775 shape: token present, a sibling required field absent.

        Before the fix this raised ValidationError whose input_value held a
        truncated copy of the token.
        """
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", SENTINEL)
        monkeypatch.delenv("TELEGRAM_CHANNEL_ID", raising=False)
        monkeypatch.delenv("ADMIN_TELEGRAM_CHAT_ID", raising=False)

        with pytest.raises(SettingsError) as caught:
            Settings(_env_file=None)

        leaked = longest_leaked_run(_render(caught.value))
        assert leaked == 0, f"{leaked} sentinel characters reached the error"

    def test_the_failing_field_itself_is_not_echoed(self, monkeypatch):
        """A credential that fails its OWN validation must not be quoted either.

        The sibling case above is how it was found; this is the same defect
        pointed directly at the field, and a fix that only redacted other
        fields' input would pass the first test and fail this one.
        """
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", SENTINEL)
        monkeypatch.setenv("TELEGRAM_CHANNEL_ID", SENTINEL)  # not an int
        monkeypatch.setenv("ADMIN_TELEGRAM_CHAT_ID", "123")

        with pytest.raises(SettingsError) as caught:
            Settings(_env_file=None)

        leaked = longest_leaked_run(_render(caught.value))
        assert leaked == 0, f"{leaked} sentinel characters reached the error"

    def test_the_original_error_is_not_chained(self, monkeypatch):
        """`raise ... from exc` would re-expose the value in the traceback.

        Python prints __cause__ and __context__ for an uncaught exception, so
        chaining the original ValidationError puts input_value back on screen
        under "The above exception was the direct cause" — a redaction that
        redacts nothing. This is the single most likely way to regress the fix.
        """
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", SENTINEL)
        monkeypatch.delenv("TELEGRAM_CHANNEL_ID", raising=False)

        with pytest.raises(SettingsError) as caught:
            Settings(_env_file=None)

        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None or not isinstance(
            caught.value.__context__, ValidationError
        )

    def test_field_names_survive_so_the_error_is_still_actionable(self, monkeypatch):
        """Redaction must not cost diagnosability.

        The point is to drop values, not to make a startup failure unreadable —
        an error that says only "settings failed" sends someone to add a print
        statement, which is how the value gets echoed again.
        """
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", SENTINEL)
        monkeypatch.delenv("TELEGRAM_CHANNEL_ID", raising=False)
        monkeypatch.delenv("ADMIN_TELEGRAM_CHAT_ID", raising=False)

        with pytest.raises(SettingsError) as caught:
            Settings(_env_file=None)

        message = str(caught.value)
        assert "TELEGRAM_CHANNEL_ID" in message
        assert "ADMIN_TELEGRAM_CHAT_ID" in message
        assert "missing" in message

    def test_a_valid_load_still_works(self, monkeypatch):
        """Positive control: the wrapper must not break the success path.

        Without this, deleting the try/except entirely would pass every test
        above except by raising ValidationError, and a wrapper that always
        raised would look equally 'safe'.
        """
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", SENTINEL)
        monkeypatch.setenv("TELEGRAM_CHANNEL_ID", "-1001234567")
        monkeypatch.setenv("ADMIN_TELEGRAM_CHAT_ID", "12345")

        loaded = Settings(_env_file=None)

        assert loaded.TELEGRAM_CHANNEL_ID == -1001234567
        assert loaded.TELEGRAM_BOT_TOKEN == SENTINEL


@pytest.mark.unit
class TestWhySecretStrIsNotTheFix:
    """Pins the measurement that ruled out the obvious fix (#775)."""

    def test_secretstr_alone_would_not_have_been_enough(self, monkeypatch):
        """A SecretStr field's value STILL reaches a sibling's missing-error.

        pydantic builds `input_value` for a `missing` error from the raw input
        mapping, before field types are applied, so the annotation never runs.
        Documented as a test rather than a comment because it is the reason the
        fix is a boundary catch, and a future reader will otherwise try
        SecretStr again and believe it worked.
        """
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", SENTINEL)
        monkeypatch.delenv("TELEGRAM_CHANNEL_ID", raising=False)

        class Probe(BaseSettings):
            model_config = SettingsConfigDict(case_sensitive=False, extra="ignore")
            TELEGRAM_BOT_TOKEN: SecretStr
            TELEGRAM_CHANNEL_ID: int

        with pytest.raises(ValidationError) as caught:
            Probe(_env_file=None)

        assert longest_leaked_run(_render(caught.value)) >= MIN_LEAK_RUN
