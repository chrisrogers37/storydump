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

from typing import Optional

import pytest
from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

# ALIASED DELIBERATELY. `pydantic_settings` ships its own `SettingsError` and so
# does `src.config.settings`; an unqualified import of either would silently
# shadow the other depending on import order, in a file whose whole subject is
# which of the two a boundary catches.
from pydantic_settings.exceptions import SettingsError as PydanticSourceError

from src.config.settings import Settings, SettingsError, _redact_source

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

    IT ALSO DUMPS EACH EXCEPTION'S ATTRIBUTE STATE, and that is not
    belt-and-braces — it is the only reason this function can see the #780 leak
    at all. `json.JSONDecodeError` keeps the ENTIRE undecoded input on its
    ``.doc`` attribute, while its ``str()`` and ``repr()`` are both just
    "Expecting value: line 1 column 1 (char 0)". Measured: a renderer built from
    ``str``/``repr`` alone reports ZERO leaked characters against a chained
    JSONDecodeError that is holding the whole credential, and `pytest
    --showlocals` prints that same value nine times. A leak detector that reads
    only the message is blind in exactly the direction that matters.
    """
    parts = []
    seen = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(f"{type(current).__name__}: {current}")
        parts.append(repr(current))
        # Guarded: pydantic's ValidationError is a Rust object with no __dict__.
        try:
            parts.append(repr(vars(current)))
        except TypeError:
            pass
        parts.append(repr(current.args))
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
class TestTheSourceLayerIsRedactedToo:
    """#780 — the boundary's OTHER exit.

    `BaseSettings` construction can fail in two phases, and they raise
    unrelated exception classes. pydantic-settings' SOURCE layer resolves raw
    values from env/dotenv/secrets *before* pydantic's VALIDATION layer runs,
    and on failure raises `pydantic_settings.exceptions.SettingsError`, which
    inherits from `ValueError` and NOT from `ValidationError`. A boundary
    written as `except ValidationError` structurally cannot see it.

    WHY THIS IS A LEAK AND NOT ONLY A BYPASS, which is the part that was
    previously recorded the other way round. The escaping `SettingsError`'s own
    message carries a field name and a source class name — no value. But it is
    chained `from e`, and for a complex field that `e` is a
    `json.JSONDecodeError` whose ``.doc`` attribute holds the **entire
    undecoded input, untruncated**. Measured: a plain `pytest --showlocals` on
    the escaping path prints the whole synthetic credential NINE times, against
    ZERO for the already-fixed ValidationError path under the same invocation.
    That is strictly worse than the #775 defect this file was opened for, where
    pydantic's own ~22-character truncation at least limited the disclosure.

    DORMANT FOR `Settings` AS DECLARED, and measured rather than assumed: 42
    fields, zero complex-typed, zero aliases, no env_prefix. The trigger is a
    REQUIRED list/dict/nested-model field — an `Optional`-wrapped one degrades
    safely to the redacted ValidationError path, which the positive control
    below pins. So these tests reach the path through a SUBCLASS of the real
    `Settings`, which inherits the real boundary: the defect is exercised
    against production code rather than against a copy of it, without adding a
    complex field to the shipped configuration.
    """

    def _probe(self):
        """A subclass of the REAL Settings carrying a required complex field."""

        class Probe(Settings):
            PROBE_FIELD: list[str]

        return Probe

    def _optional_probe(self):
        class OptionalProbe(Settings):
            PROBE_FIELD: Optional[list[str]] = None

        return OptionalProbe

    def _ambient(self, monkeypatch, probe_value):
        """Satisfy the three genuinely required fields, then plant the sentinel."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unused-by-this-test")
        monkeypatch.setenv("TELEGRAM_CHANNEL_ID", "-1001234567")
        monkeypatch.setenv("ADMIN_TELEGRAM_CHAT_ID", "12345")
        monkeypatch.setenv("PROBE_FIELD", probe_value)

    def test_a_required_complex_field_does_not_escape_the_boundary(self, monkeypatch):
        """The headline: the source layer's error is converted, not leaked.

        Before the fix this raised `pydantic_settings.exceptions.SettingsError`
        straight through `Settings.__init__`.
        """
        self._ambient(monkeypatch, SENTINEL)

        with pytest.raises(SettingsError) as caught:
            self._probe()(_env_file=None)

        leaked = longest_leaked_run(_render(caught.value))
        assert leaked == 0, f"{leaked} sentinel characters reached the error"

    def test_the_source_error_is_not_chained(self, monkeypatch):
        """Severing the chain is what makes ``.doc`` unreachable.

        Redacting the message alone would be worthless here: the value never
        was in the message, it was on the chained exception's attributes. Only
        raising OUTSIDE the handler drops the reference — `from None` would
        leave the JSONDecodeError on ``__context__``, still holding ``.doc``
        for any logger, debugger or `--showlocals` traceback to reach.
        """
        self._ambient(monkeypatch, SENTINEL)

        with pytest.raises(SettingsError) as caught:
            self._probe()(_env_file=None)

        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None

    def test_the_field_name_survives_so_the_error_is_still_actionable(
        self, monkeypatch
    ):
        """Same bargain as the ValidationError path: drop values, keep names.

        The name is recovered from the library's message but is then checked
        against the model's OWN declared fields before being emitted, so this
        assertion is also what proves that lookup works — a message-parsing
        step that silently failed would render `<unknown>` and still pass every
        leak assertion above.
        """
        self._ambient(monkeypatch, SENTINEL)

        with pytest.raises(SettingsError) as caught:
            self._probe()(_env_file=None)

        assert "PROBE_FIELD" in str(caught.value)

    def test_an_optional_complex_field_still_takes_the_validation_path(
        self, monkeypatch
    ):
        """POSITIVE CONTROL, and the one that stops this being over-scoped.

        `Optional[list[str]]` fed the same malformed value degrades to a normal,
        already-redacted `ValidationError` — pydantic-settings sets
        `allow_parse_failure` for union-wrapped complex fields and falls through
        with the raw value. If this ever starts taking the source-error path the
        two are no longer distinguishable, and a fix scoped to "any complex
        field" would be flagging shapes that were never a trigger.
        """
        self._ambient(monkeypatch, SENTINEL)

        with pytest.raises(SettingsError) as caught:
            self._optional_probe()(_env_file=None)

        assert "PROBE_FIELD" in str(caught.value)
        assert "list_type" in str(caught.value), (
            "the Optional field no longer reaches pydantic's validator, so the"
            " required-vs-Optional split this fix is scoped by has changed"
        )
        assert longest_leaked_run(_render(caught.value)) == 0

    def test_a_valid_complex_value_still_loads(self, monkeypatch):
        """Positive control: a boundary that always raised would pass the rest."""
        self._ambient(monkeypatch, '["alpha","beta"]')

        loaded = self._probe()(_env_file=None)

        assert loaded.PROBE_FIELD == ["alpha", "beta"]

    def test_the_librarys_own_message_is_never_passed_through(self, monkeypatch):
        """KILLS THE ONE MUTANT THE REST LET LIVE.

        Replacing the boundary's body with ``error = str(exc)`` — handing the
        library's message straight to the caller — passed all twelve other
        tests, including the direct unit test of `_redact_source`. That test
        drives the helper by hand, so it proves the helper is correct and says
        nothing about whether the boundary CALLS it; and on the installed
        version the library's message quotes no value, so no leak assertion
        fires either.

        The property that separates them is the VOCABULARY: this project emits
        a fixed ``"<field>: source_error"`` line built from a name it has
        verified, and never the library's prose. Asserting the shape is what
        makes "no value can reach this string" a property of our code rather
        than a lucky fact about the installed release.
        """
        self._ambient(monkeypatch, SENTINEL)

        with pytest.raises(SettingsError) as caught:
            self._probe()(_env_file=None)

        message = str(caught.value)
        assert message.startswith("settings failed to load:")
        assert "source_error" in message
        assert "from source" not in message, (
            "the library's own message reached the caller — the boundary is not"
            " going through _redact_source, so nothing constrains what a future"
            " release can put in it"
        )

    def test_only_a_declared_field_name_is_ever_emitted(self):
        """THE GUARD ON THE GUARD, and it exists because a mutant survived.

        Every test above stays green if `_redact_source` is replaced with
        ``error = str(exc)`` — passing the library's message straight through.
        On the installed version that message quotes no value, so no leak
        assertion fires and the field name is still present. The check that the
        extracted token is a DECLARED FIELD is therefore invisible to them, and
        it is the whole reason a future release cannot put a value in this
        string.

        Driven directly, because the surviving mutant is only reachable through
        a message shape this library does not currently produce — which is
        precisely the situation the check is for.
        """
        known = {"PROBE_FIELD": object()}

        emitted = _redact_source(PydanticSourceError('field "PROBE_FIELD" x'), known)
        assert "PROBE_FIELD" in emitted

        # A message that interpolates something else into the same slot — the
        # shape `cli.py`'s `f'...for {field_name}: {e}'` could produce.
        smuggled = _redact_source(
            PydanticSourceError(f'field "{SENTINEL}" from source "X"'), known
        )
        assert longest_leaked_run(smuggled) == 0, "a non-field token was emitted"
        assert "<unknown>" in smuggled

        # No recognisable field slot at all.
        assert "<unknown>" in _redact_source(PydanticSourceError("opaque"), known)

    def test_the_underlying_exception_really_does_carry_the_value(self, monkeypatch):
        """THE PREMISE, asserted so it cannot rot silently.

        Every test above is a claim about a *consequence* — that the value does
        not escape. They would all stay green if pydantic-settings stopped
        putting the input on the chained exception, at which point they would be
        guarding nothing and nobody would know. So this reaches the raw library
        path with no boundary in the way and requires the value to STILL be
        reachable there.

        If this ever fails, the fix above has not regressed — the threat model
        has changed, and that is worth knowing deliberately rather than
        discovering that a security test has quietly become vacuous.
        """
        monkeypatch.setenv("PROBE_FIELD", SENTINEL)

        class Bare(BaseSettings):
            model_config = SettingsConfigDict(case_sensitive=False, extra="ignore")
            PROBE_FIELD: list[str]

        with pytest.raises(PydanticSourceError) as caught:
            Bare(_env_file=None)

        assert longest_leaked_run(_render(caught.value)) >= MIN_LEAK_RUN, (
            "the chained exception no longer carries the undecoded input, so"
            " the tests above are no longer proving anything about a live leak"
        )


@pytest.mark.unit
class TestTheBoundaryHasNoUnenumeratedExit:
    """The tail rung — found while fixing #780, and LIVE rather than dormant.

    #780 is reached only through a required complex field, which `Settings` does
    not declare. This one needs neither: a single non-UTF-8 byte in `.env` — a
    latin-1 character in a password, a pasted smart quote — raises
    `UnicodeDecodeError` while `DotEnvSettingsSource` is being CONSTRUCTED,
    before any source is called. pydantic-settings' own
    ``except Exception -> SettingsError`` funnel lives inside the call, so it
    never sees it either, and neither enumerated class matches.

    The payload is `UnicodeDecodeError.object`: the WHOLE .env file. Measured on
    the fixture below — every byte, every credential in it, and zero of them in
    `str()` or the rendered traceback. The same blind spot as `.doc`, one level
    up and much larger.

    This is why the boundary's last clause is `ValueError` rather than a third
    named class. Enumeration keeps being one release behind the library.
    """

    def _dotenv_with_bad_byte(self, tmp_path):
        """A .env carrying three sentinels and one undecodable byte."""
        path = tmp_path / ".env"
        path.write_bytes(
            f"TELEGRAM_BOT_TOKEN={SENTINEL}\n".encode()
            + "TELEGRAM_CHANNEL_ID=-100123\n".encode()
            + "ADMIN_TELEGRAM_CHAT_ID=456\n".encode()
            + b"DB_PASSWORD=pass\xe9word\n"
        )
        return path

    def test_an_undecodable_dotenv_does_not_escape_the_boundary(self, tmp_path):
        """The headline: it is converted, not propagated.

        Before the tail rung this raised `UnicodeDecodeError` straight out of
        `Settings.__init__` — and out of module import, since
        `src/config/settings.py` constructs a `Settings` at module scope.
        """
        with pytest.raises(SettingsError) as caught:
            Settings(_env_file=self._dotenv_with_bad_byte(tmp_path))

        leaked = longest_leaked_run(_render(caught.value))
        assert leaked == 0, f"{leaked} sentinel characters reached the error"
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None

    def test_it_names_the_failure_class_without_naming_a_value(self, tmp_path):
        """Diagnosability at the only altitude available here.

        The boundary cannot say which field failed — the file never parsed — so
        it says which KIND of failure occurred. A type name is code-derived and
        no user-supplied value can enter it, the same argument that lets the
        validation path emit pydantic's `type`.
        """
        with pytest.raises(SettingsError) as caught:
            Settings(_env_file=self._dotenv_with_bad_byte(tmp_path))

        message = str(caught.value)
        assert message.startswith("settings failed to load:")
        assert "UnicodeDecodeError" in message

    def test_the_undecodable_file_really_does_carry_every_credential(self, tmp_path):
        """THE PREMISE, so the tests above cannot quietly become vacuous.

        They assert a consequence — nothing escapes. They would stay green if
        Python stopped attaching the undecoded bytes, at which point they would
        be guarding nothing. This reaches the raw read with no boundary in the
        way and requires the whole file to STILL be reachable there.
        """
        path = self._dotenv_with_bad_byte(tmp_path)

        with pytest.raises(UnicodeDecodeError) as caught:
            path.read_text(encoding="utf-8")

        assert isinstance(caught.value, ValueError)
        assert longest_leaked_run(_render(caught.value)) >= MIN_LEAK_RUN, (
            "the undecodable read no longer carries the file contents, so the"
            " tests above are no longer proving anything about a live leak"
        )

    def test_a_programming_error_is_still_allowed_to_propagate(self):
        """THE COST OF THE RUNG, BOUNDED — and the reason it is not `Exception`.

        A boundary that swallowed everything would convert a typo in a property
        into "settings failed to load" with no traceback and no line number,
        which trades a credential leak for a debugging cliff. `ValueError` is
        the library's own altitude and leaves genuine programming errors alone;
        this pins that they still arrive intact.
        """

        class Broken(Settings):
            @property
            def boom(self):
                raise TypeError("a real programming error")

        loaded = Broken(
            _env_file=None,
            TELEGRAM_BOT_TOKEN="x",
            TELEGRAM_CHANNEL_ID=1,
            ADMIN_TELEGRAM_CHAT_ID=1,
        )

        with pytest.raises(TypeError, match="a real programming error"):
            loaded.boom


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
