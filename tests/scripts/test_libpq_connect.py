"""`libpq_connect.connect` refuses a connection string libpq would misread
before it hands the string to libpq.

A misread password's tail lands in the host, the port or the database name,
where libpq quotes a value as it connects. A URL that carries a raw `@`
besides the one before the host, a string libpq cannot parse, and a database
name or a URL's host libpq reads with an `@` in it are refused by the
variable's name first. A string libpq reads is handed on as it is, and so is
every error libpq raises. No database: `psycopg2.connect` is stood in for.
"""

import psycopg2
import pytest

from scripts import libpq_connect
from tests.unreadable_urls import (
    AMBIGUOUS,
    URLS,
    assert_refused_by_name,
    cases,
    forms,
)

#: Strings libpq reads as written: a host list, the default port for an empty
#: item in a port list, an IPv6 literal; a socket directory whose `@` is the
#: only one in the URL, or encoded beside a user's; an abstract socket in a
#: key/value string, which carries no `://`; and an encoded `@` in the
#: password.
READABLE = {
    "no-port": "postgresql://owner@db.invalid/db",
    "one-port": "postgresql://owner@db.invalid:5432/db",
    "a-port-per-host": "postgresql://owner@db1.invalid:5432,db2.invalid:5433/db",
    "the-first-host-on-the-default-port": "postgresql://owner@db1.invalid,db2.invalid:5433/db",
    "an-ipv6-host-and-port": "postgresql://owner@[::1]:5432/db",
    "a-socket-directory-that-carries-the-only-at-sign": "postgresql:///db?host=/run/user@1000",
    "an-encoded-at-sign-in-a-socket-directory": "postgresql://owner@/db?host=/run/user%401000",
    "an-abstract-socket-in-a-key-value-string": "host=@pgsock dbname=db user=owner",
    "an-encoded-at-sign-in-the-password": URLS["encoded-at-sign-in-password"],
}


@pytest.fixture
def handed(monkeypatch):
    """The strings `psycopg2.connect` is handed; each gets a stand-in back."""
    seen = []

    def connect(dsn):
        seen.append(dsn)
        return ("a connection", dsn)

    monkeypatch.setattr(psycopg2, "connect", connect)
    return seen


@pytest.mark.parametrize("dsn", **cases(AMBIGUOUS))
def test_a_url_with_a_second_raw_at_sign_is_refused_before_libpq_has_it(dsn, handed):
    """libpq ends the user information at the first `@` and reads the rest of
    the password as the host, the port, the database name or the query, and
    quotes the part it cannot use; resolving such a host would send the
    password's tail to the resolver. Its `@` beside a user's, a socket
    directory's or an abstract socket's `@` is refused with them."""
    with pytest.raises(ValueError) as caught:
        libpq_connect.connect(dsn, "SOME_DSN")
    assert_refused_by_name(caught.value, "SOME_DSN")
    assert handed == []


@pytest.mark.parametrize(
    "dsn",
    **cases(
        forms(
            "raw-slash",
            "raw-slash-before-a-query",
            "leading-digits-then-raw-slash",
            "leading-digits-in-range-then-raw-slash",
            "raw-slash-in-the-user",
        )
    ),
)
def test_a_database_name_read_out_of_the_password_is_refused_before_libpq_has_it(
    dsn, handed
):
    """libpq ends the authority at the first `/`, so after a raw `/` in the
    password the user is the host, the password's head the port, words or
    digits in a port's range or out of it, and the rest, with the `@`, the
    database name; after a raw `/` in the user, the name carries the whole
    password. libpq would quote the port or the name."""
    with pytest.raises(ValueError) as caught:
        libpq_connect.connect(dsn, "SOME_DSN")
    assert_refused_by_name(caught.value, "SOME_DSN")
    assert handed == []


@pytest.mark.parametrize(
    "dsn",
    **cases(
        {
            **forms(
                "raw-slash-then-a-host-parameter",
                "encoded-abstract-socket-beside-a-password",
            ),
            "an-abstract-socket-in-a-url-with-no-user": "postgresql:///db?host=@pgsock",
            "an-encoded-abstract-socket-beside-a-user": "postgresql://owner@/db?host=%40pgsock",
        }
    ),
)
def test_a_urls_host_that_carries_an_at_sign_is_refused_before_libpq_has_it(
    dsn, handed
):
    """After a raw `/` and then a raw `?host=` in the password, libpq reads the
    password's head as the port and its tail, with the `@`, as the host, which
    it would quote or resolve. A URL's host may carry no `@` but a socket
    directory's, so an abstract socket in a URL is refused with it, encoded or
    not: a key/value string names one (`host=@pgsock`)."""
    with pytest.raises(ValueError) as caught:
        libpq_connect.connect(dsn, "SOME_DSN")
    assert_refused_by_name(caught.value, "SOME_DSN")
    assert handed == []


@pytest.mark.parametrize(
    "dsn",
    **cases(forms("invalid-escape-in-password", "undecodable-escape-in-password")),
)
def test_an_escape_libpq_cannot_decode_is_refused_before_libpq_has_it(dsn, handed):
    """libpq's parse refuses an escape it cannot decode and quotes the token,
    the password; one it decodes into bytes that are no text raises a
    UnicodeDecodeError that carries them."""
    with pytest.raises(ValueError) as caught:
        libpq_connect.connect(dsn, "SOME_DSN")
    assert_refused_by_name(caught.value, "SOME_DSN")
    assert handed == []


@pytest.mark.parametrize("dsn", **cases(READABLE))
def test_a_string_libpq_reads_is_handed_to_libpq_as_it_is(dsn, handed):
    assert libpq_connect.connect(dsn, "SOME_DSN") == ("a connection", dsn)
    assert handed == [dsn]


def test_an_error_libpq_raises_is_its_own(monkeypatch):
    """The refusal is the pre-check's alone: past it, libpq's error is raised
    unchanged, naming the host or quoting what someone typed in an option."""
    refused = psycopg2.OperationalError('invalid sslmode value: "bogus"')

    def connect(_dsn):
        raise refused

    monkeypatch.setattr(psycopg2, "connect", connect)
    with pytest.raises(psycopg2.OperationalError) as caught:
        libpq_connect.connect(
            "postgresql://owner@db.invalid/db?sslmode=bogus", "SOME_DSN"
        )
    assert caught.value is refused
