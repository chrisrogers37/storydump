"""L.0 egress-floor proofs (#857, `04` §L.0 gate).

The four floor proofs, each written as a PAIR: the guard refuses, and with that
one guard disabled the same input is accepted. The pair is the point. A floor
proof is unusually easy to write green-by-construction — a timeout test whose
fake never hangs, an SSRF test whose hostname resolves to a public address
anyway — and a single positive assertion cannot tell "the guard refused this"
from "nothing would have happened regardless". The negative half is what makes
each proof falsifiable, and it stays in the suite permanently rather than being
a demonstration someone ran once.

`EgressPolicy.without(...)` exists for that negative half only; production
leaves every flag on.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from src.services.target.egress import (
    TIMEOUT_CLASSES,
    EgressBudgetExhausted,
    EgressPolicy,
    EgressRefused,
    ResponseTooLarge,
    request,
    validate_target,
)
from src.services.target.unit_of_work import TransactionDisciplineError

ALLOWED = "https://graph.instagram.com/v1/me"


class HangingTransport(httpx.AsyncBaseTransport):
    """A server that hangs for `hang_s`.

    It reads the timeout httpx passes and raises `ReadTimeout` when the hang
    would outlast it, because `MockTransport` performs no real I/O and so does
    not enforce timeouts on its own. This models the real thing faithfully —
    a server slower than the client's timeout produces exactly this exception —
    and, critically, it is sensitive to the timeout VALUE, so the paired test
    below can show a longer timeout accepting the same hang.
    """

    def __init__(self, hang_s: float):
        self.hang_s = hang_s
        self.seen_timeouts: list = []

    async def handle_async_request(self, request):
        timeout = request.extensions.get("timeout", {}).get("read")
        self.seen_timeouts.append(timeout)
        if timeout is not None and self.hang_s > timeout:
            raise httpx.ReadTimeout(
                "server hung past the timeout class", request=request
            )
        await asyncio.sleep(0)
        return httpx.Response(200, text="ok")


def _transport(handler):
    return httpx.MockTransport(handler)


class TestProofOneHangingFakeHitsItsTimeoutClass:
    @pytest.mark.asyncio
    async def test_a_hang_longer_than_the_class_is_refused(self):
        hang = HangingTransport(hang_s=TIMEOUT_CLASSES["fast"] + 10)
        async with httpx.AsyncClient(transport=hang) as client:
            with pytest.raises(EgressBudgetExhausted):
                await request(
                    client,
                    "GET",
                    ALLOWED,
                    policy=EgressPolicy(timeout_class="fast", max_attempts=1),
                )
        assert hang.seen_timeouts, "the transport never saw a timeout at all"

    @pytest.mark.asyncio
    async def test_the_SAME_hang_passes_under_a_longer_class(self):
        """The negative half: the refusal above was caused by the timeout
        class, not by the fake being unreachable or the call being malformed."""
        hang = HangingTransport(hang_s=TIMEOUT_CLASSES["fast"] + 10)
        async with httpx.AsyncClient(transport=hang) as client:
            resp = await request(
                client,
                "GET",
                ALLOWED,
                policy=EgressPolicy(timeout_class="upload", total_budget_s=120),
            )
        assert resp.status_code == 200


class TestProofTwoARetryStormExhaustsONEAbsoluteBudget:
    @pytest.mark.asyncio
    async def test_attempts_stop_at_the_budget_not_at_the_attempt_count(self):
        """The budget is absolute, so a storm ends on wall clock. Proven by the
        budget being spent with retries still nominally available."""
        calls = {"n": 0}

        async def always_fail(request):
            calls["n"] += 1
            raise httpx.ConnectError("refused", request=request)

        async with httpx.AsyncClient(transport=_transport(always_fail)) as client:
            with pytest.raises(EgressBudgetExhausted):
                await request(
                    client,
                    "GET",
                    ALLOWED,
                    policy=EgressPolicy(
                        timeout_class="fast", total_budget_s=0.0, max_attempts=50
                    ),
                )
        assert calls["n"] < 50, (
            f"the budget did not bound the storm — {calls['n']} of 50 attempts ran, "
            "so attempts were bounded by the retry COUNT rather than by the one "
            "absolute budget"
        )

    @pytest.mark.asyncio
    async def test_with_the_budget_disabled_the_storm_runs_to_the_attempt_count(self):
        """The negative half: it really is the budget doing the bounding."""
        calls = {"n": 0}

        async def always_fail(request):
            calls["n"] += 1
            raise httpx.ConnectError("refused", request=request)

        async with httpx.AsyncClient(transport=_transport(always_fail)) as client:
            with pytest.raises(EgressBudgetExhausted):
                await request(
                    client,
                    "GET",
                    ALLOWED,
                    policy=EgressPolicy(
                        timeout_class="fast", total_budget_s=0.0, max_attempts=4
                    ).without(enforce_budget=False),
                )
        assert calls["n"] == 4


class TestProofThreeAnOversizedResponseCutsAtTheByteCap:
    @pytest.mark.asyncio
    async def test_a_body_over_the_cap_is_refused(self):
        async def big(request):
            return httpx.Response(200, content=b"x" * 5000)

        async with httpx.AsyncClient(transport=_transport(big)) as client:
            with pytest.raises(ResponseTooLarge):
                await request(
                    client,
                    "GET",
                    ALLOWED,
                    policy=EgressPolicy(max_response_bytes=1000),
                )

    @pytest.mark.asyncio
    async def test_with_the_cap_disabled_the_same_body_is_accepted(self):
        async def big(request):
            return httpx.Response(200, content=b"x" * 5000)

        async with httpx.AsyncClient(transport=_transport(big)) as client:
            resp = await request(
                client,
                "GET",
                ALLOWED,
                policy=EgressPolicy(max_response_bytes=1000).without(
                    enforce_byte_cap=False
                ),
            )
        assert len(resp.content) == 5000

    @pytest.mark.asyncio
    async def test_a_body_at_the_cap_is_accepted(self):
        """Boundary: the cap cuts ABOVE it, not at it."""

        async def exact(request):
            return httpx.Response(200, content=b"x" * 1000)

        async with httpx.AsyncClient(transport=_transport(exact)) as client:
            resp = await request(
                client, "GET", ALLOWED, policy=EgressPolicy(max_response_bytes=1000)
            )
        assert len(resp.content) == 1000


class TestProofFourARedirectTowardAPrivateAddressIsRefused:
    """The SSRF half. Resolution is injected so the private address is real to
    the validator rather than depending on what public DNS happens to return —
    the failure mode where the test's own hostname resolves publicly and the
    proof passes having exercised nothing."""

    def _resolver(self, mapping):
        def resolve(host):
            return mapping[host]

        return resolve

    def test_a_host_resolving_to_a_private_address_is_refused(self):
        policy = EgressPolicy(allowed_hosts=frozenset({"graph.instagram.com"}))
        with pytest.raises(EgressRefused, match="non-public address"):
            validate_target(
                ALLOWED,
                policy,
                resolver=self._resolver({"graph.instagram.com": ["10.0.0.5"]}),
            )

    def test_with_the_block_disabled_the_same_private_address_is_accepted(self):
        policy = EgressPolicy(allowed_hosts=frozenset({"graph.instagram.com"})).without(
            enforce_private_address_block=False
        )
        assert (
            validate_target(
                ALLOWED,
                policy,
                resolver=self._resolver({"graph.instagram.com": ["10.0.0.5"]}),
            )
            == "graph.instagram.com"
        )

    @pytest.mark.parametrize(
        "addr", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1"]
    )
    def test_every_non_public_class_is_refused_including_the_metadata_address(
        self, addr
    ):
        policy = EgressPolicy(allowed_hosts=frozenset({"graph.instagram.com"}))
        with pytest.raises(EgressRefused):
            validate_target(
                ALLOWED,
                policy,
                resolver=self._resolver({"graph.instagram.com": [addr]}),
            )

    def test_a_host_resolving_to_BOTH_public_and_private_is_refused(self):
        """Checking only the first address is the DNS-rebinding shape."""
        policy = EgressPolicy(allowed_hosts=frozenset({"graph.instagram.com"}))
        with pytest.raises(EgressRefused):
            validate_target(
                ALLOWED,
                policy,
                resolver=self._resolver(
                    {"graph.instagram.com": ["93.184.216.34", "10.0.0.5"]}
                ),
            )

    @pytest.mark.asyncio
    async def test_a_cross_host_redirect_is_refused(self):
        async def redirect(request):
            return httpx.Response(302, headers={"location": "https://evil.example/x"})

        async with httpx.AsyncClient(transport=_transport(redirect)) as client:
            with pytest.raises(EgressRefused, match="cross-host redirect"):
                await request(
                    client,
                    "GET",
                    ALLOWED,
                    policy=EgressPolicy(),
                    resolver=self._resolver({"graph.instagram.com": ["93.184.216.34"]}),
                )

    @pytest.mark.asyncio
    async def test_a_SAME_host_redirect_is_revalidated_not_inherited(self):
        """A same-host hop is allowed but re-checked: if the host's address
        turns private between hops, the second validation refuses."""
        hops = {"n": 0}

        async def redirect_once(request):
            hops["n"] += 1
            if hops["n"] == 1:
                return httpx.Response(
                    302, headers={"location": "https://graph.instagram.com/v2/me"}
                )
            return httpx.Response(200, text="ok")

        rebinding = {"n": 0}

        def resolver(host):
            rebinding["n"] += 1
            return ["93.184.216.34"] if rebinding["n"] == 1 else ["10.0.0.5"]

        async with httpx.AsyncClient(transport=_transport(redirect_once)) as client:
            with pytest.raises(EgressRefused, match="non-public address"):
                await request(
                    client, "GET", ALLOWED, policy=EgressPolicy(), resolver=resolver
                )
        assert rebinding["n"] == 2, "the redirect target was never re-validated"


class TestTheHostAllowlist:
    def test_an_unlisted_host_is_refused(self):
        with pytest.raises(EgressRefused, match="not on the provider allowlist"):
            validate_target("https://evil.example/x", EgressPolicy())

    def test_with_the_allowlist_disabled_the_same_host_is_accepted(self):
        assert (
            validate_target(
                "https://evil.example/x",
                EgressPolicy().without(enforce_host_allowlist=False),
                resolver=lambda h: ["93.184.216.34"],
            )
            == "evil.example"
        )


class TestTransactionDisciplineIsEnforcedAtTheEgressPoint:
    @pytest.mark.asyncio
    async def test_a_provider_call_inside_an_open_transaction_fails(self):
        """`02` §5. Driven through the real ContextVar the UoW sets."""
        from src.services.target import unit_of_work as uow_mod

        token = uow_mod._IN_TRANSACTION.set(True)
        try:
            async with httpx.AsyncClient(
                transport=_transport(lambda r: httpx.Response(200))
            ) as client:
                with pytest.raises(TransactionDisciplineError):
                    await request(client, "GET", ALLOWED, policy=EgressPolicy())
        finally:
            uow_mod._IN_TRANSACTION.reset(token)

    @pytest.mark.asyncio
    async def test_the_same_call_outside_a_transaction_succeeds(self):
        """Negative half: the refusal is the discipline, not a broken call."""

        async def ok(request):
            return httpx.Response(200, text="ok")

        async with httpx.AsyncClient(transport=_transport(ok)) as client:
            resp = await request(
                client,
                "GET",
                ALLOWED,
                policy=EgressPolicy(),
                resolver=lambda h: ["93.184.216.34"],
            )
        assert resp.status_code == 200


class CountingStream(httpx.AsyncByteStream):
    """A body delivered in chunks, counting how many were actually pulled.

    This is what makes the cap's mechanism testable at all. navi's review noted
    — correctly, of the previous implementation — that `MockTransport` hands
    back an already-materialised `Response`, so no test could distinguish
    "capped during the read" from "capped after". Streaming changes that: the
    chunk counter is the observable that separates the two.
    """

    def __init__(self, chunk: bytes, count: int):
        self.chunk = chunk
        self.count = count
        self.pulled = 0

    async def __aiter__(self):
        for _ in range(self.count):
            self.pulled += 1
            yield self.chunk


class StreamingTransport(httpx.AsyncBaseTransport):
    def __init__(self, stream):
        self.stream = stream

    async def handle_async_request(self, request):
        return httpx.Response(200, stream=self.stream)


class TestTheByteCapBoundsMemoryRatherThanReportingAfterwards:
    """navi's finding, closed. The cap must ABANDON the read, not measure it.

    A post-hoc `len(response.content)` check rejects an oversized body only
    after httpx has materialised the whole thing — which is the very DoS the
    cap names. These tests assert on the chunk counter, so they fail if the
    implementation ever reverts to buffering.
    """

    @pytest.mark.asyncio
    async def test_the_read_STOPS_EARLY_rather_than_draining_the_body(self):
        # 100 chunks of 1 KiB available; cap at 4 KiB.
        stream = CountingStream(b"x" * 1024, 100)
        async with httpx.AsyncClient(transport=StreamingTransport(stream)) as client:
            with pytest.raises(ResponseTooLarge):
                await request(
                    client,
                    "GET",
                    ALLOWED,
                    policy=EgressPolicy(max_response_bytes=4096),
                    resolver=lambda h: ["93.184.216.34"],
                )
        assert stream.pulled <= 6, (
            f"the body was drained to {stream.pulled} of 100 chunks before the cap "
            "fired — the read is being measured, not bounded, which is the "
            "post-hoc shape this test exists to prevent"
        )
        assert stream.pulled >= 5, "the cap fired before reaching its own limit"

    @pytest.mark.asyncio
    async def test_a_body_under_the_cap_is_fully_read(self):
        """Positive control: early abort above, complete delivery below."""
        stream = CountingStream(b"y" * 1024, 3)
        async with httpx.AsyncClient(transport=StreamingTransport(stream)) as client:
            resp = await request(
                client,
                "GET",
                ALLOWED,
                policy=EgressPolicy(max_response_bytes=4096),
                resolver=lambda h: ["93.184.216.34"],
            )
        assert len(resp.content) == 3072 and stream.pulled == 3


class TestTheRedirectRefusalSurvivesAChain:
    """navi verified a 3-hop chain by hand and noted no shipped test asserts
    it, so a refactor of the loop could silently lose it. This is that test."""

    @pytest.mark.asyncio
    async def test_a_cross_host_hop_is_refused_at_the_END_of_a_same_host_chain(self):
        hops = []

        async def chain(request):
            hops.append(str(request.url))
            n = len(hops)
            if n == 1:
                return httpx.Response(
                    302, headers={"location": "https://graph.instagram.com/hop2"}
                )
            if n == 2:
                return httpx.Response(
                    302, headers={"location": "https://evil.example/hop3"}
                )
            return httpx.Response(200, text="should never be reached")

        async with httpx.AsyncClient(transport=_transport(chain)) as client:
            with pytest.raises(EgressRefused, match="cross-host redirect"):
                await request(
                    client,
                    "GET",
                    ALLOWED,
                    policy=EgressPolicy(max_attempts=5),
                    resolver=lambda h: ["93.184.216.34"],
                )
        assert len(hops) == 2, (
            f"expected refusal at hop 2, saw {len(hops)} hops — the loop either "
            "stopped early or followed the cross-host hop"
        )
