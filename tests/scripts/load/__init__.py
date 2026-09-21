"""The load harness (`-m load`): the fake Telegram, the latency proxy, the
two real processes the scenarios measure, and the scenarios themselves.

`free_port` lives here because three of the modules below each carried their
own copy of it: `processes.py` under this name, `latency_proxy.py` and
`fake_telegram.py` as a private `_free_port`, the last importing `socket`
inside the body. The same four lines three times.

`tests/test_integration_coverage_policy.py` has a fourth copy and keeps it: it
sits in a different tree, and importing across would couple a policy test to
this harness.
"""

import socket


def free_port() -> int:
    """A port nothing is listening on: bind one, note it, release it."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
