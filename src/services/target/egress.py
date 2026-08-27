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

**The validated address is PINNED to the connection (S.3/#871).** Validation and
connection used to resolve independently: :func:`validate_target` checked every
address a host resolved to, and then ``httpx`` performed its **own** resolution
when it opened the socket. The address-class check therefore proved *"this
hostname resolved to a public address at that moment"* and not *"this connection
went to a public address"* — a TOCTOU / DNS-rebind window.

The second resolution no longer happens. :func:`validate_target` returns the
addresses it approved, and :func:`request` connects to one of *those*, by handing
the transport a URL whose host is an **IP literal**. There is no hostname left
for the transport to look up, which is why this closes the window rather than
narrowing it: a resolver that answered public and then answers private is not
consulted a second time at all.

Getting that right without weakening TLS is the whole difficulty, and it is
solved by two overrides rather than by a custom transport:

- ``extensions["sni_hostname"]`` carries the ORIGINAL hostname. ``httpcore``
  1.0.9 reads it at ``_async/connection.py:107`` and passes it as
  ``server_hostname`` to ``start_tls`` (``:151``), falling back to the origin
  host only when it is absent. So SNI is sent for the hostname and the
  certificate is verified against the hostname, while the socket goes to the
  pinned address.
- An explicit ``Host`` header carries the original ``host:port``. ``httpx``
  synthesises ``Host`` from the URL **only when the header is absent**
  (``_models.py:450-456``), so the override wins and the server sees the name it
  is expecting rather than an IP.

Neither ``verify`` nor the SSL context is touched.

**Retries rotate through the approved addresses rather than re-resolving.** A
provider with four A records and one dead host still recovers, and the recovery
path cannot reach an address the floor never approved.

**A redirect re-pins from its own validation.** The logical URL keeps the
hostname throughout so a ``Location`` is joined against the name the provider
used; only the per-attempt connect URL carries an IP.

**What this does NOT do**, stated so no reader infers more than is true: it does
not make the address-class check the load-bearing control. The check now decides
what the connection is allowed to reach rather than merely observing a moment,
which is a real strengthening — but the host allowlist is still what keeps an
attacker from naming a target at all, and widening it still admits whatever that
host resolves to at the instant of the call. The pin closes the window between
the two resolutions; it does not vouch for the host.

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
from typing import NamedTuple, Optional

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
#: Widening this before #871 lands moves the SSRF TOCTOU gap (module
#: docstring, above) from theoretical to reachable — this allowlist is the
#: load-bearing control until then.
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


class ValidatedTarget(NamedTuple):
    """What validation approved: the host, and the addresses it approved it AT.

    *addresses* is empty when ``enforce_private_address_block`` is off — nothing
    was resolved, so there is nothing to pin, and :func:`request` then behaves
    exactly as it did before pinning existed. Empty means "not checked", never
    "checked and found none": a host that resolves to nothing is refused above.
    """

    host: str
    addresses: tuple


def _pinned(url: httpx.URL, address: str) -> httpx.URL:
    """The same URL, addressed to *address* instead of to a name.

    The transport receives an IP literal, so there is no name left for it to
    resolve — which is the mechanism, not an optimisation. `httpx` brackets an
    IPv6 literal for us once the host is handed over bracketed; path, query and
    port are carried through untouched.
    """
    literal = f"[{address}]" if ":" in address else address
    return url.copy_with(host=literal)


def _pin_call(url: str, target: ValidatedTarget, attempt: int, kwargs: dict):
    """(URL to hand the transport, kwargs carrying the Host and SNI overrides).

    Returns the call UNCHANGED when there is nothing to pin, so a policy with
    the address block switched off behaves exactly as it did before S.3 — the
    guard flags stay independent of one another.

    Attempts rotate through the approved addresses. A provider with four A
    records and one dead host still recovers, and the recovery cannot reach an
    address the floor never approved, because the list is the one validation
    returned rather than a fresh lookup.
    """
    if not target.addresses:
        return url, kwargs
    parsed = httpx.URL(url)
    address = target.addresses[attempt % len(target.addresses)]
    # The caller's own headers and extensions are MERGED, never replaced: this
    # function owns exactly two keys and must not silently drop an adapter's
    # auth header or timeout extension.
    headers = httpx.Headers(kwargs.get("headers"))
    headers["Host"] = parsed.netloc.decode("ascii")
    extensions = dict(kwargs.get("extensions") or {})
    extensions["sni_hostname"] = target.host
    return (
        str(_pinned(parsed, address)),
        {**kwargs, "headers": headers, "extensions": extensions},
    )


def validate_target(
    url: str, policy: EgressPolicy, *, resolver=_resolve_addresses
) -> ValidatedTarget:
    """Refuse a URL the floor will not allow. Returns the host and its addresses.

    The addresses are RETURNED rather than merely inspected, because a check
    whose result is discarded is what made this a TOCTOU window in the first
    place (#871): the caller has to be able to connect to the very addresses
    that were approved.
    """
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
        return ValidatedTarget(host, tuple(addrs))
    return ValidatedTarget(host, ())


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

    target = validate_target(url, policy, resolver=resolver)

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
        # `url` stays the LOGICAL, named URL for the whole loop; only the
        # per-attempt connect URL carries an address. A `Location` must be
        # joined against the name the provider used, not against an IP.
        connect_url, call_kwargs = _pin_call(url, target, attempt, kwargs)
        try:
            # STREAMED, not buffered. `client.request()` reads the whole body
            # before returning (httpx 0.28.1 `Client.send`: `if not stream:
            # response.read()`), so a post-hoc length check rejects an
            # oversized body only AFTER materialising it — no memory bound at
            # all, which is the very threat the cap names. Streaming lets the
            # read be abandoned mid-body. `httpx.Limits` carries no
            # response-size concept to lean on instead; checked.
            async with client.stream(
                method,
                connect_url,
                timeout=timeout,
                follow_redirects=False,
                **call_kwargs,
            ) as response:
                if response.is_redirect:
                    # Headers arrive before the body; a redirect's body is
                    # never read.
                    location = response.headers.get("location", "")
                    next_url = httpx.URL(url).join(location)
                    if (
                        policy.enforce_cross_host_redirect_block
                        and next_url.host != httpx.URL(url).host
                    ):
                        raise EgressRefused(
                            f"cross-host redirect {httpx.URL(url).host!r} -> "
                            f"{next_url.host!r} refused (L.0/#857)"
                        )
                    # Same-host hop: re-validate, never inherit approval —
                    # and re-pin from THAT validation, so the hop connects to
                    # an address the hop itself approved.
                    target = validate_target(str(next_url), policy, resolver=resolver)
                    url = str(next_url)
                    continue

                body = await _read_capped(response, policy)
                # `aiter_bytes()` yields DECODED bytes, so `body` is plaintext.
                # Carrying the original `content-encoding` onto the rebuilt
                # response makes httpx decode it a SECOND time on first read —
                # `DecodingError: incorrect header check` on every gzip reply,
                # which is every real provider reply, since httpx advertises
                # `Accept-Encoding: gzip` by default. `content-length` is a lie
                # for the same reason: it described the compressed body.
                # Both are dropped rather than corrected; httpx derives the
                # length from `content` itself.
                headers = httpx.Headers(response.headers)
                headers.pop("content-encoding", None)
                headers.pop("content-length", None)
                return httpx.Response(
                    response.status_code,
                    headers=headers,
                    content=body,
                    # The NAMED url, not the pinned one. `egress.request` is the
                    # single door every provider call passes through, so a
                    # caller reading `response.request.url` must see the target
                    # it asked for; the address is an implementation detail of
                    # how the socket got there.
                    request=httpx.Request(
                        method, url, headers=response.request.headers
                    ),
                )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
            continue

    raise EgressBudgetExhausted(
        f"all {policy.max_attempts} attempts failed within the absolute budget "
        f"of {policy.total_budget_s}s (L.0/#857): {last_exc!r}"
    )
