"""A recording stand-in for every way a test run could reach a database or a
socket, for ``test_isolation_guard.py``'s subprocess runs.

Loaded with ``-p`` BEFORE ``tests.isolation_guard``, so the guard wraps these
functions: whatever the guard refuses never gets here, and whatever it lets
through is logged and then blocked, so a run under the spy cannot touch a real
database or network whatever the guard does. "The log is empty" therefore means
"the guard refused first".

The log holds entry-point names only, one per line, at ``$ISOLATION_SPY_LOG``.
It never holds an address or a parameter.
"""

import errno
import os
import socket

import psycopg2

try:
    LOG = os.environ["ISOLATION_SPY_LOG"]
except KeyError:
    raise RuntimeError(
        "ISOLATION_SPY_LOG must name the file the spy appends to: a spy that logs"
        " nowhere would let every 'nothing was attempted' assertion pass"
    ) from None


def _record(entry_point: str) -> None:
    with open(LOG, "a", encoding="utf-8") as handle:
        handle.write(entry_point + "\n")


def _blocking(entry_point: str, error: type) -> object:
    def spy(*args, **kwargs):
        _record(entry_point)
        raise error("isolation spy: blocked")

    return spy


def _connect_ex(self, address):
    _record("socket.socket.connect_ex")
    return errno.ECONNREFUSED


psycopg2.connect = _blocking("psycopg2.connect", psycopg2.OperationalError)
socket.socket.connect = _blocking("socket.socket.connect", ConnectionRefusedError)
socket.socket.connect_ex = _connect_ex
socket.create_connection = _blocking("socket.create_connection", ConnectionRefusedError)
socket.getaddrinfo = _blocking("socket.getaddrinfo", socket.gaierror)
