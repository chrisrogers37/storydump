"""Fake database URLs that a driver cannot read as written, and the checks that
a refusal of one quotes none of it.

A driver's own error quotes the part of a connection string it could not
read, and a misread password is that part: an unencoded ``@`` ends the user
information early, at one ``@`` or another, and libpq ends the authority at
an unencoded ``/``, so the password's tail becomes the host, the port, the
database name or the query. Every refusal here names the variable the string
came from and never its value, so no part of these strings may reach the
message, ``str()``, ``repr()``, a chained exception or a formatted traceback.

Every credential is a placeholder, and every host is ``db.invalid``, a name
that never resolves: whatever reads one of these strings, nothing connects.
Every password carries the word "password" before its first ``@``, so that
none reads as a real credential to a secret scanner, wherever it takes the
password to end.
"""

from __future__ import annotations

import traceback

import psycopg2

from src.config import db_url

#: The head of every fake password and the host every fake URL names, which a
#: refusal must withhold; the host stands for a misread password's tail, and
#: where a misread fragment of a password becomes the host, it is `not-a-real`.
#: The word "password" itself, in every fake password, is one a refusal's own
#: hint names. Three passwords lead with digits instead, read as a port, out of
#: a port's range or in it, and a refusal must withhold those too.
PASSWORD_HEAD = "not-a-real"
HOST = "db.invalid"
LEADING_DIGITS = ("65536", "24680")
WITHHELD = (PASSWORD_HEAD, HOST, *LEADING_DIGITS)

#: Every fake URL, by its form. What each driver makes of a form is noted by
#: the tables below that pick it.
URLS = {
    "well-formed": "postgresql://owner:not-a-real-password@db.invalid/db",
    "encoded-at-sign-in-password": "postgresql://owner:not-a-real%40password@db.invalid/db",
    "invalid-escape-in-password": "postgresql://owner:not-a-real%zzpassword@db.invalid/db",
    "undecodable-escape-in-password": "postgresql://owner:not-a-real%D8password@db.invalid/db",
    "mistyped-query-parameter": "postgresql://owner:not-a-real-password@db.invalid/db?sslmod=require",
    "query-parameter-without-equals": "postgresql://owner:not-a-real-password@db.invalid/db?sslmode",
    "unknown-key-value-option": "host=db.invalid user=owner password=not-a-real-password bogus=1",
    "key-value-string": "host=db.invalid user=owner password=not-a-real-password",
    "raw-question-mark": "postgresql://owner:not-a-real?password@db.invalid/db",
    "raw-question-mark-after-digits": "postgresql://owner:24680?not-a-real-password@db.invalid/db?sslmode=require",
    "raw-hash": "postgresql://owner:not-a-real#password@db.invalid/db",
    "raw-hash-before-a-query": "postgresql://owner:not-a-real#password@db.invalid/db?sslmode=require",
    "raw-bracket": "postgresql://owner:not-a-real[password@db.invalid/db",
    "raw-closing-bracket": "postgresql://owner:not-a-real]password@db.invalid/db",
    "compatibility-character": "postgresql://owner:not-a-real℀password@db.invalid/db",
    "raw-slash": "postgresql://owner:not-a-real/password@db.invalid/db",
    "raw-slash-before-a-query": "postgresql://owner:not-a-real/password@db.invalid/db?sslmode=require",
    "leading-digits-then-raw-slash": "postgresql://owner:65536/not-a-real-password@db.invalid/db",
    "leading-digits-in-range-then-raw-slash": "postgresql://owner:24680/password@db.invalid/db",
    "raw-slash-then-a-host-parameter": "postgresql://owner:not-a-real-password/x?host=password@db.invalid/db",
    "raw-slash-in-the-user": "postgresql://ow/ner:not-a-real-password@db.invalid/db",
    "raw-at-sign": "postgresql://owner:not-a-real-password@password@db.invalid/db",
    "raw-at-sign-in-a-host-list": "postgresql://owner:not-a-real-password@not-a-real,password@db.invalid/db",
    "raw-at-sign-then-colon": "postgresql://owner:not-a-real-password@not-a-real:password@db.invalid/db",
    "raw-at-sign-then-slash": "postgresql://owner:not-a-real-password@not-a-real/password@db.invalid/db",
    "raw-at-sign-then-question-mark": "postgresql://owner:not-a-real-password@not-a-real?password@db.invalid/db?sslmode=require&channel_binding=require",
    "raw-at-sign-then-question-mark-without-equals": "postgresql://owner:not-a-real-password@not-a-real?password@db.invalid/db",
    "raw-at-sign-then-a-query-parameter": "postgresql://owner:not-a-real-password@not-a-real?not-a-real=password@db.invalid/db",
    "two-raw-at-signs-in-a-row": "postgresql://owner:not-a-real-password@@password@db.invalid/db",
    "raw-at-sign-then-a-comma": "postgresql://owner:not-a-real-password@not-a-real,@db.invalid/db",
    "raw-at-sign-then-a-bracket": "postgresql://owner:not-a-real-password@[not-a-real]password@db.invalid/db",
    "raw-at-sign-in-a-socket-directory-beside-a-password": "postgresql://owner:not-a-real-password@/db?host=/run/user@1000",
    "raw-at-sign-in-an-abstract-socket-beside-a-password": "postgresql://owner:not-a-real-password@/db?host=@pgsock",
    "encoded-socket-directory-beside-a-password": "postgresql://owner:not-a-real-password@/db?host=/run/user%401000",
    "encoded-abstract-socket-beside-a-password": "postgresql://owner:not-a-real-password@/db?host=%40pgsock",
    "raw-at-sign-in-a-socket-directory-and-no-user": "postgresql:///db?host=/run/user@1000",
}

#: A string every driver reads, for the side of a test under no test.
READABLE = URLS["well-formed"]

#: How the owner-run tools name the URL they take as ``DATABASE_URL`` or
#: ``--database-url``: the migration runner, the schema builder, the catalog
#: comparator's ``extract`` and the schema consistency job.
OWNER_URL = "DATABASE_URL (or --database-url)"

#: The start of every refusal of an unreadable URL, after the variable's name.
REFUSAL = "cannot be read as a database URL"


def forms(*names: str) -> dict[str, str]:
    """The fake URLs *names* name, in that order."""
    return {name: URLS[name] for name in names}


#: Every URL that carries a raw `@` besides the one before the host
#: (`db_url.is_ambiguous_url`), which every door refuses before any driver
#: reads it. The drivers end the user information at one `@` and read the rest
#: of the password as the host (`password@db.invalid`, `@password@db.invalid`,
#: a host list, a bracketed host after which SQLAlchemy reads nothing), the
#: port (`password@db.invalid`, which both quote), the database name or the
#: query, whose parameter names libpq and asyncpg quote. A socket directory or
#: an abstract socket whose `@` stands beside a user's is the accepted cost:
#: its `@` must be encoded, as in the encoded forms SQLAlchemy reads, and libpq
#: reads a socket directory's alone.
AMBIGUOUS = forms(
    "raw-at-sign",
    "raw-at-sign-in-a-host-list",
    "raw-at-sign-then-colon",
    "raw-at-sign-then-slash",
    "raw-at-sign-then-question-mark",
    "raw-at-sign-then-question-mark-without-equals",
    "raw-at-sign-then-a-query-parameter",
    "two-raw-at-signs-in-a-row",
    "raw-at-sign-then-a-comma",
    "raw-at-sign-then-a-bracket",
    "raw-at-sign-in-a-socket-directory-beside-a-password",
    "raw-at-sign-in-an-abstract-socket-beside-a-password",
)

#: What libpq cannot read (the migration runner and the scripts that connect
#: through psycopg2): every ambiguous URL; what it cannot parse at all, where
#: psycopg2's ProgrammingError quotes the token it choked on, or decodes into
#: bytes that are no text, where the UnicodeDecodeError carries them; and what
#: it parses into a database name, or a URL's host, carrying an `@`. libpq ends
#: the authority at a raw `/`, in the user or in the password, so the user (or
#: its head) becomes the host, the password's head the port (`not-a-real`,
#: `65536`, out of a port's range, or `24680`) and the rest, with the `@`, the
#: database name, or after a raw `?` a `host` parameter. An abstract socket
#: named in a URL is the accepted cost, encoded or not: a key/value string
#: names one.
LIBPQ_UNREADABLE = {
    **forms(
        "invalid-escape-in-password",
        "undecodable-escape-in-password",
        "mistyped-query-parameter",
        "query-parameter-without-equals",
        "unknown-key-value-option",
    ),
    **AMBIGUOUS,
    **forms(
        "raw-slash",
        "raw-slash-before-a-query",
        "leading-digits-then-raw-slash",
        "leading-digits-in-range-then-raw-slash",
        "raw-slash-in-the-user",
        "raw-slash-then-a-host-parameter",
        "encoded-abstract-socket-beside-a-password",
    ),
}

#: What libpq reads as written: a raw `?`, `#` or `[` in the password is
#: libpq's to keep, and an encoded `@` its to decode. Connecting fails only
#: because the host never resolves, and that error names the host, never the
#: password.
LIBPQ_READABLE = forms(
    "raw-question-mark",
    "raw-hash",
    "raw-bracket",
    "encoded-at-sign-in-password",
    "well-formed",
)

#: What the API and the worker cannot read: `unit_of_work.engine_url_from_env`
#: refuses every ambiguous URL, then takes SQLAlchemy's one reading of the
#: rest: `make_url` refuses a key/value string, and after a raw `/` in the
#: user it reads no user information, the user's head as the host and the
#: rest, password and `@` among it, as the database name, which is refused.
ENGINE_UNREADABLE = {**AMBIGUOUS, **forms("key-value-string", "raw-slash-in-the-user")}

#: What the API and the worker read as written, each with the password its
#: writer meant (:data:`PASSWORDS`): SQLAlchemy keeps a raw `/`, `?`, `#`, `[`
#: or `]`, an unknown escape and a compatibility character in a password, as
#: libpq keeps the `?`, the `#` and the `[`, even a `/` and then a `?host=`,
#: which libpq misreads, and decodes an encoded `@` in the password and in a
#: `host` query value, an abstract socket's among them.
ENGINE_READABLE = forms(
    "raw-question-mark",
    "raw-question-mark-after-digits",
    "raw-hash",
    "raw-hash-before-a-query",
    "raw-bracket",
    "raw-closing-bracket",
    "compatibility-character",
    "raw-slash",
    "raw-slash-before-a-query",
    "raw-slash-then-a-host-parameter",
    "invalid-escape-in-password",
    "encoded-at-sign-in-password",
    "encoded-socket-directory-beside-a-password",
    "encoded-abstract-socket-beside-a-password",
)

#: The password each form the engine reads carries, as its writer meant it.
PASSWORDS = {
    "raw-question-mark": "not-a-real?password",
    "raw-question-mark-after-digits": "24680?not-a-real-password",
    "raw-hash": "not-a-real#password",
    "raw-hash-before-a-query": "not-a-real#password",
    "raw-bracket": "not-a-real[password",
    "raw-closing-bracket": "not-a-real]password",
    "compatibility-character": "not-a-real℀password",
    "raw-slash": "not-a-real/password",
    "raw-slash-before-a-query": "not-a-real/password",
    "raw-slash-then-a-host-parameter": "not-a-real-password/x?host=password",
    "invalid-escape-in-password": "not-a-real%zzpassword",
    "encoded-at-sign-in-password": "not-a-real@password",
    "encoded-socket-directory-beside-a-password": "not-a-real-password",
    "encoded-abstract-socket-beside-a-password": "not-a-real-password",
}

#: What the schema builder cannot read: every ambiguous URL, what SQLAlchemy
#: cannot parse, as a key/value string, and what it reads into a database name
#: carrying an `@`, as after a raw `/` in the user.
BUILDER_UNREADABLE = {
    **AMBIGUOUS,
    **forms("key-value-string", "raw-slash-in-the-user"),
}

#: What the schema builder reads as written; connecting fails on the host.
BUILDER_READABLE = forms(
    "raw-question-mark",
    "raw-hash",
    "raw-bracket",
    "raw-slash",
    "raw-slash-then-a-host-parameter",
    "invalid-escape-in-password",
    "encoded-at-sign-in-password",
)

#: Where a driver would read the password's tail, or a fragment of it, as a
#: host, alone or in a host list. Resolving one would send it to the
#: resolver, and one that resolved, under a wildcard record, would fail with
#: an error that quotes it, so it is refused before any driver is handed it.
MISREAD_HOSTS = forms(
    "raw-at-sign",
    "raw-at-sign-in-a-host-list",
    "raw-at-sign-then-slash",
    "two-raw-at-signs-in-a-row",
    "raw-at-sign-then-a-comma",
    "raw-at-sign-then-a-bracket",
)


def cases(form_table: dict) -> dict:
    """``pytest.mark.parametrize`` keywords for *form_table*, each case named
    by its form."""
    return {"argvalues": list(form_table.values()), "ids": list(form_table)}


def linked(exc: BaseException) -> list[BaseException]:
    """*exc* and every exception it carries, as a cause or a context, at any
    depth: a traceback prints them all."""
    found, pending = [], [exc]
    while pending:
        current = pending.pop()
        if current is None or any(current is seen for seen in found):
            continue
        found.append(current)
        pending += [current.__cause__, current.__context__]
    return found


def assert_withheld(*texts: str) -> None:
    """No text carries any part of a fake URL."""
    for text in texts:
        for part in WITHHELD:
            assert part not in text, f"{part!r} was printed: {text!r}"


def assert_exception_withholds(exc: BaseException) -> None:
    """Neither *exc* nor anything it carries quotes a fake URL: not its
    message, its ``repr()``, a chained exception or the traceback a process
    would print for it."""
    for link in linked(exc):
        assert_withheld(str(link), repr(link))
    assert_withheld("".join(traceback.format_exception(exc)))


def assert_refused_by_name(exc: BaseException, variable: str) -> None:
    """*exc* is the refusal of an unreadable URL by *variable*'s name: an
    ``UnreadableUrl`` that says so, with no error chained to it, quoting none
    of the fake URL."""
    assert str(exc).startswith(f"{variable} {REFUSAL}"), exc
    assert exc.__cause__ is None and exc.__context__ is None, (
        "an error is chained to the refusal"
    )
    assert_exception_withholds(exc)
    assert isinstance(exc, db_url.UnreadableUrl), type(exc)


def assert_names_only_the_host(text: str) -> None:
    """*text*, a driver's own failure to resolve the fake host, names the host
    and none of the password."""
    assert f'could not translate host name "{HOST}"' in text, text
    assert PASSWORD_HEAD not in text, text


#: A connection string `stand_in_administrator` answers with a stand-in.
STAND_IN = "host=stand-in"


class _Administrator:
    """What a script uses of an administrator connection before it gives up on
    the next one: ``autocommit`` and ``close``."""

    autocommit = False
    closed = False

    def close(self):
        self.closed = True


def stand_in_administrator(monkeypatch) -> _Administrator:
    """Make ``psycopg2.connect(STAND_IN)`` return a stand-in administrator and
    every other string connect as it would, so that a script's second URL is
    the one under test. Returns the stand-in, to show it was closed."""
    stand_in, connect = _Administrator(), psycopg2.connect
    monkeypatch.setattr(
        psycopg2, "connect", lambda dsn: stand_in if dsn == STAND_IN else connect(dsn)
    )
    return stand_in
