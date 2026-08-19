"""L.0 — the egress floor (#857, `04` §L.0).

Every provider adapter call carries four guards: a **timeout class**, **one
absolute retry budget**, a **response byte cap**, and **SSRF-safe resolution**.
L.5, L.6 and L.8 inherit the floor by phase order, so no live provider path
ever runs ahead of it. S.3 keeps the deployment-wide budgets, the full
hostile-fake battery and re-verification; this is the floor, not the ceiling.

## Each guard is separately disableable, deliberately

Every guard reads a single flag on :class:`EgressPolicy`. That is not a
configuration feature — production always runs them all — it exists so each
floor proof can be shown to FAIL when the thing it guards is switched off.
A floor proof that cannot be made to fail has proven nothing, and these four
are unusually easy to write green-by-construction: a timeout test whose fake
never actually hangs, an SSRF test whose hostname resolves to a public address
anyway. The flags make the negative case reachable in a test rather than
requiring the guard to be commented out.

## The retry budget is ONE ABSOLUTE budget, not per-attempt

`04` says "one absolute retry budget", and the distinction is the whole point:
a per-attempt timeout multiplied by N attempts is not a bound a caller can
reason about, and it is how a "5 second timeout" becomes a 40-second stall.
:attr:`EgressPolicy.total_budget_s` is a wall-clock deadline measured once,
before the first attempt, and checked before every subsequent one. Attempts
stop when the budget is spent even if retries remain.

## SSRF resolution refuses first and asks nothing

Three independent refusals, because each catches what the others cannot:

* **Host allowlist** — the host must be named. A denylist cannot enumerate the
  internet, and provider egress is a closed set. **This is the load-bearing
  control**, for the reason in the next paragraph.
* **Address class** — every resolved address is checked, and a hostname
  resolving to several addresses is refused if ANY is private. Checking only
  the first is the DNS-rebinding shape.
* **Cross-host redirects** — refused outright. Same-host redirects are
  followed, and each hop is re-validated from scratch: a redirect is an
  attacker-controlled URL, so validating only the original target would make
  the allowlist decorative.

**KNOWN GAP, DEFERRED TO S.3 AND NAMED HERE RATHER THAN IMPLIED AWAY:
validation and connection resolve independently.** :func:`validate_target`
resolves the host and checks every address; ``httpx`` then performs its **own**
resolution when it opens the connection, and nothing pins the validated
addresses to that connection. So the address-class check proves *"this hostname
resolved to a public address at that moment"*, **not** *"this connection went
to a public address"* — a TOCTOU / DNS-rebind window. Verified against httpx
0.28.1: the transport passes ``host=request.url.raw_host`` to the pool and
exposes no resolver hook, so closing this needs a custom transport that
resolves, pins, and still gets TLS SNI and verification right.

Two consequences, both stated so no reader infers more than is true:

1. **The host allowlist is the control actually holding here**, not the address
   check. Six closed, high-reputation provider hostnames means an attacker
   would need DNS control over Meta, Telegram or Google — not an injectable
   target. That is why this is deferred rather than blocking, and it is also
   why the allowlist must not be widened casually: admitting anything less
   trusted moves this from theoretical to reachable.
2. **It is scoped to S.3 and tracked as #871**, which `04` already charters
   with *"SSRF-safe streaming"* and *"the full hostile-fake battery"* — the
   plan doc names this specific carried item, so it is deferred visibly rather
   than forgotten.

The address-class check is kept regardless: it is real defence in depth against
a misconfigured or compromised-at-rest allowlist entry, and it costs nothing.

## Transaction discipline is enforced from here

`02` §5 says a transaction never spans a provider call. The check lives on this
side because the egress point is the one place every provider call passes
through, and the alternative — asking each adapter to remember — is the kind of
rule that holds until someone forgets.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from dataclasses import dataclass, field, replace
from typing import Optional

import httpx

from src.exceptions.base import StorydumpError
from src.services.target.unit_of_work import (
    TransactionDisciplineError,
    in_transaction,
)

#: Named timeout classes. A call names its class; it does not pass seconds.
#: `04` says "a timeout class" rather than "a timeout" for that reason — the
#: value is an operational number, and naming it keeps the adapter honest about
#: which kind of call it is making.
TIMEOUT_CLASSES = {
    "fast": 5.0,
    "standard": 15.0,
    "upload": 60.0,
}

#: The closed set of provider hosts. Additions are deliberate and reviewable.
DEFAULT_ALLOWED_HOSTS = frozenset(
    {
        "graph.instagram.com",
        "graph.facebook.com",
        "api.instagram.com",
        "api.telegram.org",
        "www.googleapis.com",
        "oauth2.googleapis.com",
    }
)

#: Cap on a provider response body, enforced DURING the read.
#:
#: An unbounded read is a memory DoS, and a post-hoc length check does not
#: prevent one — it reports it after the fact. The first version of this floor
#: called `client.request()` and compared `len(response.content)`, but httpx
#: materialises the whole body before returning (`Client.send`: `if not
#: stream: response.read()`), so a gigabyte arrived in memory before the cap
#: ever fired. `_read_capped` streams and abandons the read past the cap
#: instead, which is what makes the rationale above true rather than aspirational.
DEFAULT_MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class EgressRefused(StorydumpError):
    """A provider call was refused by the floor before leaving the process."""


class EgressBudgetExhausted(StorydumpError):
    """The one absolute retry budget was spent."""


class ResponseTooLarge(StorydumpError):
    """The response exceeded the byte cap and was cut."""


@dataclass(frozen=True)
class EgressPolicy:
    """The floor's four guards. Every flag defaults ON."""

    timeout_class: str = "standard"
    total_budget_s: float = 30.0
    max_attempts: int = 3
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    allowed_hosts: frozenset = field(default_factory=lambda: DEFAULT_ALLOWED_HOSTS)

    # Guard switches — see the module docstring. Production leaves all True.
    enforce_host_allowlist: bool = True
    enforce_private_address_block: bool = True
    enforce_cross_host_redirect_block: bool = True
    enforce_byte_cap: bool = True
    enforce_budget: bool = True
    enforce_transaction_discipline: bool = True

    @property
    def timeout_s(self) -> float:
        if self.timeout_class not in TIMEOUT_CLASSES:
            raise EgressRefused(
                f"unknown timeout class {self.timeout_class!r} — one of "
                f"{sorted(TIMEOUT_CLASSES)} (L.0/#857)"
            )
        return TIMEOUT_CLASSES[self.timeout_class]

    def without(self, **flags) -> "EgressPolicy":
        """A copy with guards disabled — for floor proofs only."""
        return replace(self, **flags)


def _resolve_addresses(host: str):
    """Every address *host* resolves to. Split out so tests can substitute."""
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return [info[4][0] for info in infos]


def _is_forbidden(addr: str) -> bool:
    ip = ipaddress.ip_address(addr)
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def validate_target(url: str, policy: EgressPolicy, *, resolver=_resolve_addresses):
    """Refuse a URL the floor will not allow. Returns the parsed host."""
    parsed = httpx.URL(url)
    host = parsed.host
    if not host:
        raise EgressRefused(f"no host in {url!r}")

    if policy.enforce_host_allowlist and host not in policy.allowed_hosts:
        raise EgressRefused(
            f"host {host!r} is not on the provider allowlist (L.0/#857)"
        )

    if policy.enforce_private_address_block:
        try:
            addrs = resolver(host)
        except socket.gaierror as exc:
            raise EgressRefused(f"cannot resolve {host!r}: {exc}") from exc
        if not addrs:
            raise EgressRefused(f"{host!r} resolved to no addresses")
        # EVERY address, not the first: a host resolving to one public and one
        # private address is the DNS-rebinding shape and must be refused.
        for addr in addrs:
            if _is_forbidden(addr):
                raise EgressRefused(
                    f"{host!r} resolves to non-public address {addr} — refused "
                    "(L.0/#857 SSRF floor)"
                )
    return host


async def _read_capped(response: httpx.Response, policy: EgressPolicy) -> bytes:
    """Accumulate the body, abandoning the read past the cap.

    The abort happens DURING iteration, so an oversized body is never fully
    held: leaving the `client.stream(...)` block closes the connection. That is
    the difference between BOUNDING memory and merely reporting afterwards that
    it was exceeded — the distinction the previous post-hoc check missed.
    """
    chunks = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if policy.enforce_byte_cap and total > policy.max_response_bytes:
            raise ResponseTooLarge(
                f"response exceeded the {policy.max_response_bytes}-byte cap "
                f"after {total} bytes — read abandoned (L.0/#857)"
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    policy: Optional[EgressPolicy] = None,
    resolver=_resolve_addresses,
    **kwargs,
):
    """Make a provider call under the floor.

    *client* is supplied by the caller so adapters share connection pools and
    tests can inject a transport; the floor is about policy, not plumbing.
    """
    policy = policy or EgressPolicy()

    if policy.enforce_transaction_discipline and in_transaction():
        raise TransactionDisciplineError(
            "a provider call was attempted inside an open transaction — `02` §5 "
            "requires transaction-per-checkpoint: commit the checkpoint, then "
            "call the provider (L.0/#857)"
        )

    validate_target(url, policy, resolver=resolver)

    deadline = time.monotonic() + policy.total_budget_s
    last_exc = None
    for attempt in range(policy.max_attempts):
        if policy.enforce_budget and time.monotonic() >= deadline:
            raise EgressBudgetExhausted(
                f"the absolute retry budget of {policy.total_budget_s}s was spent "
                f"after {attempt} attempt(s) (L.0/#857)"
            )
        remaining = deadline - time.monotonic()
        # The per-attempt timeout never exceeds what is left of the ONE budget.
        timeout = (
            min(policy.timeout_s, max(remaining, 0.0))
            if policy.enforce_budget
            else policy.timeout_s
        )
        try:
            # STREAMED, not buffered. `client.request()` reads the whole body
            # before returning (httpx 0.28.1 `Client.send`: `if not stream:
            # response.read()`), so a post-hoc length check rejects an
            # oversized body only AFTER materialising it — no memory bound at
            # all, which is the very threat the cap names. Streaming lets the
            # read be abandoned mid-body. `httpx.Limits` carries no
            # response-size concept to lean on instead; checked.
            async with client.stream(
                method, url, timeout=timeout, follow_redirects=False, **kwargs
            ) as response:
                if response.is_redirect:
                    # Headers arrive before the body; a redirect's body is
                    # never read.
                    location = response.headers.get("location", "")
                    target = httpx.URL(url).join(location)
                    if (
                        policy.enforce_cross_host_redirect_block
                        and target.host != httpx.URL(url).host
                    ):
                        raise EgressRefused(
                            f"cross-host redirect {httpx.URL(url).host!r} -> "
                            f"{target.host!r} refused (L.0/#857)"
                        )
                    # Same-host hop: re-validate, never inherit approval.
                    validate_target(str(target), policy, resolver=resolver)
                    url = str(target)
                    continue

                body = await _read_capped(response, policy)
                return httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    content=body,
                    request=response.request,
                )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
            continue

    raise EgressBudgetExhausted(
        f"all {policy.max_attempts} attempts failed within the absolute budget "
        f"of {policy.total_budget_s}s (L.0/#857): {last_exc!r}"
    )
