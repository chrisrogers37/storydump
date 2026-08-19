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
  internet, and provider egress is a closed set.
* **Address class** — every resolved address is checked, and a hostname
  resolving to several addresses is refused if ANY is private. Checking only
  the first is the DNS-rebinding shape.
* **Cross-host redirects** — refused outright. Same-host redirects are
  followed, and each hop is re-validated from scratch: a redirect is an
  attacker-controlled URL, so validating only the original target would make
  the allowlist decorative.

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

#: Cap on a provider response body. An unbounded read is a memory DoS.
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
            response = await client.request(
                method, url, timeout=timeout, follow_redirects=False, **kwargs
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
            continue

        if response.is_redirect:
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
            # Same-host hop: re-validate from scratch, never inherit approval.
            validate_target(str(target), policy, resolver=resolver)
            url = str(target)
            continue

        if policy.enforce_byte_cap:
            body = response.content
            if len(body) > policy.max_response_bytes:
                raise ResponseTooLarge(
                    f"response of {len(body)} bytes exceeds the "
                    f"{policy.max_response_bytes}-byte cap — cut (L.0/#857)"
                )
        return response

    raise EgressBudgetExhausted(
        f"all {policy.max_attempts} attempts failed within the absolute budget "
        f"of {policy.total_budget_s}s (L.0/#857): {last_exc!r}"
    )
