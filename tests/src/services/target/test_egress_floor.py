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
import datetime
import gzip
import json
import socket
import ssl

import httpx
import pytest

from src.services.target import egress as egress_mod
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
        approved = validate_target(
            ALLOWED,
            policy,
            resolver=self._resolver({"graph.instagram.com": ["10.0.0.5"]}),
        )
        assert approved.host == "graph.instagram.com"
        # Nothing was RESOLVED, so there is nothing to pin (#871). Empty here
        # means "not checked", never "checked and found none" — a host that
        # resolves to no addresses is refused, and that is a separate proof.
        assert approved.addresses == ()

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
        approved = validate_target(
            "https://evil.example/x",
            EgressPolicy().without(enforce_host_allowlist=False),
            resolver=lambda h: ["93.184.216.34"],
        )
        assert approved.host == "evil.example"
        # The address block is still on, so the approved address comes back for
        # `request` to pin to.
        assert approved.addresses == ("93.184.216.34",)


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


class TestAGzipReplyStaysReadableThroughTheFloor:
    """`aiter_bytes()` yields DECODED bytes, so the rebuilt response must not
    keep claiming the body is still encoded — httpx would decode a second time
    and every gzip reply would die on `DecodingError: incorrect header check`.

    That is every real provider reply, because httpx advertises
    `Accept-Encoding: gzip` by default. The floor's other proofs all use
    uncompressed bodies, which is exactly why this went unnoticed: nothing here
    ever handed the floor something that had been decoded on the way in.

    `content-length` is dropped for the same reason rather than corrected — it
    described the COMPRESSED body, so carrying it onto the plaintext states a
    length that is simply wrong.
    """

    PAYLOAD = {"files": [{"id": "f1", "name": "cat.jpg"}]}

    def _gzip_handler(self):
        raw = gzip.compress(json.dumps(self.PAYLOAD).encode())
        wire = {}

        async def handler(request):
            wire["body"] = raw
            return httpx.Response(
                200,
                content=raw,
                headers={
                    "content-encoding": "gzip",
                    "content-type": "application/json",
                },
            )

        return handler, wire

    @pytest.mark.asyncio
    async def test_a_gzip_body_arrives_as_readable_json(self):
        handler, wire = self._gzip_handler()
        async with httpx.AsyncClient(transport=_transport(handler)) as client:
            resp = await request(client, "GET", ALLOWED, policy=EgressPolicy())
        # The positive control, and it is load-bearing: without it a fake that
        # quietly sent plaintext would pass this whether or not the floor is
        # correct, which is the shape that let the defect ship.
        assert wire["body"][:2] == b"\x1f\x8b", "fixture did not actually gzip"
        assert resp.json() == self.PAYLOAD

    @pytest.mark.asyncio
    async def test_the_rebuilt_response_states_no_stale_encoding_or_length(self):
        """The mechanism itself, pinned where it broke."""
        handler, _ = self._gzip_handler()
        async with httpx.AsyncClient(transport=_transport(handler)) as client:
            resp = await request(client, "GET", ALLOWED, policy=EgressPolicy())
        assert "content-encoding" not in resp.headers
        declared = resp.headers.get("content-length")
        assert declared is None or int(declared) == len(resp.content)


# ---------------------------------------------------------------------------
# S.3 / #871 — the validated address is PINNED to the connection.
#
# The gap these close is that validation and connection used to resolve
# INDEPENDENTLY, so a resolver answering public-then-private opened a
# TOCTOU/DNS-rebind window. A happy-path test proves nothing about that: the
# defect is precisely that a SECOND resolution can differ from the first, so
# every proof below is built to be false if the second resolution can still
# happen.
# ---------------------------------------------------------------------------

PINNED_HOST = "pinned-target.invalid"
PINNED_TLS_HOST = "pinned-tls.invalid"


def _name_cannot_resolve(name: str) -> bool:
    """Is *name* genuinely unresolvable on THIS machine?

    The rebind proofs below rest entirely on this being true — they work by
    guaranteeing that any second resolution FAILS, so that success can only mean
    no second resolution happened. A captive DNS or a wildcard resolver would
    make them vacuous while still green, so the property is measured rather than
    assumed. RFC 2606 reserves `.invalid` for exactly this.
    """
    try:
        socket.getaddrinfo(name, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return True
    return False


async def _serve_once(ssl_context=None):
    """A real HTTP/1.1 server on 127.0.0.1. Returns (port, stop, hits)."""
    hits = []

    async def handle(reader, writer):
        try:
            request_line = await reader.readline()
            headers = {}
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                k, _, v = line.decode("latin-1").partition(":")
                headers[k.strip().lower()] = v.strip()
            hits.append((request_line.decode("latin-1").strip(), headers))
            body = b'{"ok":true}'
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
            )
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=ssl_context)
    port = server.sockets[0].getsockname()[1]

    async def stop():
        server.close()
        await server.wait_closed()

    return port, stop, hits


def _self_signed(hostname: str, tmp_path):
    """A cert valid for *hostname* only — never for the IP we connect to.

    That asymmetry is the whole proof in the TLS test: the handshake can only
    succeed if verification was performed against the NAME while the socket went
    to the address.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
        )
        .not_valid_after(
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
        )
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    cert_pem = tmp_path / "cert.pem"
    key_pem = tmp_path / "key.pem"
    cert_pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_pem.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return cert_pem, key_pem


class TestTheConnectionGoesToTheValidatedAddress:
    """The structural half: the transport is handed an ADDRESS, never a name.

    If the transport never sees a hostname, no second resolution is possible —
    that is the mechanism, and it is what these assert. A version that validated
    and then handed `httpx` the hostname passes every behavioural test in this
    file and fails every test in this class.
    """

    def _policy(self):
        return EgressPolicy(allowed_hosts=frozenset({"graph.instagram.com"}))

    async def test_the_transport_receives_an_ip_literal_not_the_hostname(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"ok": True})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async with client:
            await request(
                client,
                "GET",
                "https://graph.instagram.com/v1/me",
                policy=self._policy(),
                resolver=lambda h: ["93.184.216.34"],
            )

        assert seen[0].url.host == "93.184.216.34", (
            "the transport was handed a NAME, so it can still resolve it a "
            "second time — the #871 window is open"
        )
        # Everything else about the request is untouched.
        assert seen[0].url.path == "/v1/me"
        assert seen[0].url.scheme == "https"

    async def test_sni_and_host_still_carry_the_original_name(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"ok": True})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async with client:
            await request(
                client,
                "GET",
                "https://graph.instagram.com/v1/me",
                policy=self._policy(),
                resolver=lambda h: ["93.184.216.34"],
            )

        # httpcore 1.0.9 reads this at `_async/connection.py:107` and passes it
        # as `server_hostname` to `start_tls` (`:151`). Without it the handshake
        # would be performed against the IP — SNI wrong and the certificate
        # verified against the wrong identity, which would be a WORSE outcome
        # than the gap being closed.
        assert seen[0].extensions["sni_hostname"] == "graph.instagram.com"
        # httpx synthesises `Host` from the URL only when absent
        # (`_models.py:450-456`), so this override is what stops the server
        # seeing an IP.
        assert seen[0].headers["host"] == "graph.instagram.com"

    async def test_the_caller_is_shown_the_name_it_asked_for(self):
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
        )
        async with client:
            response = await request(
                client,
                "GET",
                "https://graph.instagram.com/v1/me",
                policy=self._policy(),
                resolver=lambda h: ["93.184.216.34"],
            )
        assert response.request.url.host == "graph.instagram.com"

    async def test_with_the_address_block_off_nothing_is_pinned(self):
        """The guard flags stay independent. Nothing was resolved, so there is
        nothing to pin, and the call must look exactly as it did before S.3."""
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async with client:
            await request(
                client,
                "GET",
                "https://graph.instagram.com/v1/me",
                policy=self._policy().without(enforce_private_address_block=False),
                resolver=lambda h: ["93.184.216.34"],
            )
        assert seen[0].url.host == "graph.instagram.com"
        assert "sni_hostname" not in seen[0].extensions


class TestTheSecondResolutionCannotHappen:
    """The rebind half, and the reason it is not vacuous.

    A mock transport never resolves anything, so a mock can only ever prove the
    SHAPE of the request. These use the REAL `httpx` transport against a REAL
    socket, and choose a hostname that CANNOT resolve — so any second resolution
    is guaranteed to fail. Success therefore proves no second resolution
    occurred. That is the only construction found that a rebind-shaped resolver
    cannot pass with the bug still present.
    """

    async def test_the_unresolvable_name_is_reached_anyway_because_it_is_pinned(
        self, monkeypatch
    ):
        # POSITIVE CONTROL ON THE CONTROL: if a captive or wildcard DNS resolves
        # `.invalid`, the whole construction is vacuous while still green.
        assert _name_cannot_resolve(PINNED_HOST), (
            f"{PINNED_HOST} resolves on this machine, so 'the second lookup "
            "would fail' is not true here and this proof means nothing"
        )

        port, stop, hits = await _serve_once()
        try:
            # The loopback server is the point; the address-class check is
            # proven by its own pair elsewhere in this file.
            monkeypatch.setattr(egress_mod, "_is_forbidden", lambda addr: False)
            calls = []

            def resolver(host):
                calls.append(host)
                # A REBINDING resolver: public first, private second. With the
                # window open the second answer is what `httpx` would act on.
                return ["127.0.0.1"] if len(calls) == 1 else ["169.254.169.254"]

            client = httpx.AsyncClient()
            async with client:
                response = await request(
                    client,
                    "GET",
                    f"http://{PINNED_HOST}:{port}/x",
                    policy=EgressPolicy(allowed_hosts=frozenset({PINNED_HOST})),
                    resolver=resolver,
                )
            assert response.status_code == 200
            assert response.json() == {"ok": True}
            # Resolved ONCE. A second lookup is the window; there is no second.
            assert calls == [PINNED_HOST]
            # And the server saw the NAME, not the address it was reached at.
            assert hits[0][1]["host"] == f"{PINNED_HOST}:{port}"
        finally:
            await stop()

    async def test_without_the_pin_the_same_call_cannot_connect(self, monkeypatch):
        """The other half of the pair, and the thing that makes the test above
        evidence rather than decoration.

        Same server, same unresolvable name — but the address block is off, so
        nothing is pinned and `httpx` is handed the name. It must fail to
        connect. If this passed, the test above would be proving nothing,
        because the name would have been reachable either way.
        """
        assert _name_cannot_resolve(PINNED_HOST)
        port, stop, _ = await _serve_once()
        try:
            policy = EgressPolicy(
                allowed_hosts=frozenset({PINNED_HOST}), max_attempts=1
            ).without(enforce_private_address_block=False)
            client = httpx.AsyncClient()
            async with client:
                with pytest.raises(EgressBudgetExhausted):
                    await request(
                        client,
                        "GET",
                        f"http://{PINNED_HOST}:{port}/x",
                        policy=policy,
                        resolver=lambda h: ["127.0.0.1"],
                    )
        finally:
            await stop()


class TestTlsSurvivesThePin:
    """`Done requires` #2 — SNI and certificate verification unchanged.

    The certificate is valid for the NAME and never for the address, so a
    successful handshake is only possible if verification was performed against
    the name while the socket went to the pinned address. A version that pinned
    by rewriting the URL and forgot `sni_hostname` fails here, which is the
    failure mode worth catching: it would be a silent weakening of TLS shipped
    under the banner of an SSRF fix.
    """

    async def test_a_real_handshake_verifies_against_the_name(
        self, tmp_path, monkeypatch
    ):
        assert _name_cannot_resolve(PINNED_TLS_HOST)
        cert_pem, key_pem = _self_signed(PINNED_TLS_HOST, tmp_path)

        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(str(cert_pem), str(key_pem))
        port, stop, hits = await _serve_once(ssl_context=server_ctx)
        try:
            monkeypatch.setattr(egress_mod, "_is_forbidden", lambda addr: False)
            # The client trusts the self-signed cert as a CA and NOTHING else —
            # `verify` is a real verification context, not disabled.
            client = httpx.AsyncClient(verify=str(cert_pem))
            async with client:
                response = await request(
                    client,
                    "GET",
                    f"https://{PINNED_TLS_HOST}:{port}/x",
                    policy=EgressPolicy(allowed_hosts=frozenset({PINNED_TLS_HOST})),
                    resolver=lambda h: ["127.0.0.1"],
                )
            assert response.status_code == 200
            assert hits[0][1]["host"] == f"{PINNED_TLS_HOST}:{port}"
        finally:
            await stop()


class TestRetriesStayInsideTheApprovedSet:
    async def test_attempts_rotate_through_the_validated_addresses(self):
        seen = []

        def handler(request):
            seen.append(request.url.host)
            raise httpx.ConnectError("down", request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async with client:
            with pytest.raises(EgressBudgetExhausted):
                await request(
                    client,
                    "GET",
                    "https://graph.instagram.com/v1/me",
                    policy=EgressPolicy(
                        allowed_hosts=frozenset({"graph.instagram.com"}),
                        max_attempts=3,
                    ),
                    resolver=lambda h: ["93.184.216.34", "93.184.216.35"],
                )
        # Rotated, and never outside the approved set — a retry must not become
        # a fresh lookup, which is the window reopening one attempt later.
        assert seen == ["93.184.216.34", "93.184.216.35", "93.184.216.34"]

    async def test_a_single_resolution_serves_every_attempt(self):
        calls = []

        def resolver(host):
            calls.append(host)
            return ["93.184.216.34"]

        def handler(request):
            raise httpx.ConnectError("down", request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async with client:
            with pytest.raises(EgressBudgetExhausted):
                await request(
                    client,
                    "GET",
                    "https://graph.instagram.com/v1/me",
                    policy=EgressPolicy(
                        allowed_hosts=frozenset({"graph.instagram.com"}),
                        max_attempts=3,
                    ),
                    resolver=resolver,
                )
        assert calls == ["graph.instagram.com"], (
            "three attempts caused more than one resolution — each extra lookup "
            "is another chance for the answer to change (#871)"
        )
