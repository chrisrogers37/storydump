"""Meta's policy callbacks (#410) — the signature path, proven both directions.

**How these tests prove the signature path without a database.** Verification
runs before `require_engine`, and the app built with `env={}` has no engine. So
the status code discriminates precisely:

* **400** — the request was refused at the signature. Nothing downstream ran.
* **503** — the signature VERIFIED and the route reached the engine gate.

That second one is the positive control, and it is the assertion that matters:
a suite that only ever checked for 400 would pass against a verifier that
refuses everything, including honest requests from Meta.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient
from starlette.formparsers import MultiPartParser

from src.api.app import create_app
from src.api.routes.meta import SIGNED_REQUEST_MAX_BYTES
from src.config.settings import settings
from src.services.target import meta_callbacks
from tests.src.api.conftest import post_body, post_messages

SECRET = "test-app-secret-not-a-real-one"
SUBJECT = "1234567890"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def make_signed_request(payload: dict, secret: str = SECRET) -> str:
    """Build a `signed_request` the way Meta does: sign the ENCODED payload."""
    encoded = _b64url(json.dumps(payload).encode("utf-8"))
    sig = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256)
    return f"{_b64url(sig.digest())}.{encoded}"


def valid_payload(**over) -> dict:
    return {"algorithm": "HMAC-SHA256", "user_id": SUBJECT, "issued_at": 1, **over}


def _tampered_payload() -> str:
    sig, _ = make_signed_request(valid_payload()).split(".")
    return f"{sig}.{_b64url(json.dumps(valid_payload(user_id='99')).encode())}"


def _tampered_signature() -> str:
    sig, enc = make_signed_request(valid_payload()).split(".")
    flipped = _b64url(bytes(b ^ 0x01 for b in base64.urlsafe_b64decode(sig + "==")))
    return f"{flipped}.{enc}"


def _wrong_secret() -> str:
    return make_signed_request(valid_payload(), secret="a-different-secret")


@pytest.fixture
def client(monkeypatch):
    # Only the LEGACY setting is armed here, so the tests exercise the fallback
    # arm of `app_secrets()`. The preferred arm gets its own test below.
    monkeypatch.setattr(settings, "INSTAGRAM_APP_SECRET", None, raising=False)
    monkeypatch.setattr(settings, "FACEBOOK_APP_SECRET", SECRET, raising=False)
    return TestClient(create_app(env={}), raise_server_exceptions=False)


class TestTheSignatureIsActuallyChecked:
    """Both directions. Accepting nothing is as broken as accepting anything."""

    def test_a_genuine_signed_request_passes_verification(self, client):
        """The positive control — without it every other test here is vacuous."""
        r = client.post(
            "/webhooks/meta/deauthorize",
            data={"signed_request": make_signed_request(valid_payload())},
        )
        assert r.status_code == 503, (
            "a correctly signed request must clear verification and reach the"
            f" engine gate; got {r.status_code} {r.text!r}"
        )

    @pytest.mark.parametrize(
        "build",
        [
            _tampered_payload,
            _tampered_signature,
            _wrong_secret,
            lambda: "notasignedrequest",
            lambda: "a.b.c",
            lambda: "!!!!.!!!!",
            lambda: "",
        ],
        ids=[
            "tampered payload",
            "tampered signature",
            "wrong secret",
            "no dot",
            "three parts",
            "undecodable",
            "empty",
        ],
    )
    def test_a_bad_signed_request_is_refused(self, client, build):
        r = client.post("/webhooks/meta/deauthorize", data={"signed_request": build()})
        assert r.status_code == 400

    def test_an_unexpected_algorithm_is_refused_even_when_signed(self, client):
        """Algorithm confusion: the payload does not get to choose the check.

        This request is signed correctly with the real secret — only the
        declared algorithm differs. A verifier that dispatched on the payload's
        own `algorithm` field would accept it.
        """
        signed = make_signed_request(valid_payload(algorithm="none"))
        assert (
            client.post(
                "/webhooks/meta/deauthorize", data={"signed_request": signed}
            ).status_code
            == 400
        )

    def test_a_verified_payload_with_no_user_id_is_refused(self, client):
        signed = make_signed_request({"algorithm": "HMAC-SHA256", "issued_at": 1})
        assert (
            client.post(
                "/webhooks/meta/deauthorize", data={"signed_request": signed}
            ).status_code
            == 400
        )


class TestItFailsClosed:
    """A deployment holding no secret must refuse EVERYTHING.

    This is the property that decides whether the endpoint is a policy callback
    or a public door onto a destructive operation, so it is asserted on the
    deletion route as well as the deauthorize one — the two have separate
    handlers and could regress independently.
    """

    @pytest.mark.parametrize(
        "path", ["/webhooks/meta/deauthorize", "/webhooks/meta/data-deletion"]
    )
    def test_no_configured_secret_refuses_an_otherwise_valid_request(
        self, monkeypatch, path
    ):
        signed = make_signed_request(valid_payload())
        monkeypatch.setattr(settings, "INSTAGRAM_APP_SECRET", None, raising=False)
        monkeypatch.setattr(settings, "FACEBOOK_APP_SECRET", None, raising=False)
        client = TestClient(create_app(env={}), raise_server_exceptions=False)
        assert client.post(path, data={"signed_request": signed}).status_code == 400

    def test_the_same_request_would_have_been_accepted_with_a_secret(self, client):
        """Pins the test above to the SECRET rather than to the request.

        Without this, a payload that was simply malformed would make the
        fail-closed test pass for the wrong reason.
        """
        signed = make_signed_request(valid_payload())
        assert (
            client.post(
                "/webhooks/meta/deauthorize", data={"signed_request": signed}
            ).status_code
            == 503
        )


class TestTheConfirmationCode:
    def test_it_is_stable_for_one_subject(self):
        a = meta_callbacks.confirmation_code(SUBJECT, SECRET)
        b = meta_callbacks.confirmation_code(SUBJECT, SECRET)
        assert a == b, "Meta retries; a retry must not mint a second receipt"

    def test_it_differs_by_subject_and_by_secret(self):
        base = meta_callbacks.confirmation_code(SUBJECT, SECRET)
        assert meta_callbacks.confirmation_code("other", SECRET) != base
        assert meta_callbacks.confirmation_code(SUBJECT, "other-secret") != base

    def test_it_does_not_contain_the_subject(self):
        """It is handed to Meta and put in a URL; it must not carry the id."""
        assert SUBJECT not in meta_callbacks.confirmation_code(SUBJECT, SECRET)


def _sql_literals(module) -> list[str]:
    """Every string handed to `text(...)` in *module* — the actual SQL surface.

    Deliberately not a substring scan of the source. The first version of this
    guard searched the whole file for "DELETE" and failed on the word appearing
    in a docstring that PROMISES not to delete: it matched the form rather than
    the property, and prose is not the SQL that runs.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(module))
    out = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "text"
        ):
            for arg in node.args:
                out.append(
                    " ".join(
                        v.value
                        for v in ast.walk(arg)
                        if isinstance(v, ast.Constant) and isinstance(v.value, str)
                    )
                )
    return out


class TestDeauthorizeIsNotDelete:
    """The conflation guard. If either callback ever issues a SQL DELETE, a
    disconnect starts destroying tenant data — the exact failure the
    two-callback split exists to prevent."""

    def test_the_service_layer_issues_no_delete(self):
        statements = _sql_literals(meta_callbacks)
        assert statements, "found no SQL at all — the guard would pass vacuously"
        assert not [s for s in statements if "DELETE" in s.upper()]

    def test_the_revoke_path_is_an_update_on_credentials_only(self):
        revoking = [
            s for s in _sql_literals(meta_callbacks) if "oauth_credentials" in s
        ]
        assert len(revoking) == 1, "expected exactly one credential statement"
        assert "UPDATE" in revoking[0].upper()

    def test_the_route_layer_issues_no_sql_at_all(self):
        """Empty TODAY by design — the routes hold no SQL, per the layer rule in
        CLAUDE.md (API -> Services, never straight to the data). Deliberately
        NOT given the non-vacuity assert its sibling carries: here emptiness is
        the property being asserted, so demanding a non-empty result would
        invert the test. It is a tripwire for a route that grows SQL later."""
        from src.api.routes import meta as meta_routes

        assert _sql_literals(meta_routes) == []


class TestTheAppSecretIsNotHardKeyedToOneSetting:
    """Both Meta app secrets are candidates. Keying to one fails App Review
    outright if the URLs are registered under the other app, and the only
    symptom would be a warning line among ordinary prober noise."""

    def test_the_preferred_instagram_secret_also_verifies(self, monkeypatch):
        signed = make_signed_request(valid_payload(), secret="ig-secret")
        monkeypatch.setattr(
            settings, "INSTAGRAM_APP_SECRET", "ig-secret", raising=False
        )
        monkeypatch.setattr(settings, "FACEBOOK_APP_SECRET", None, raising=False)
        c = TestClient(create_app(env={}), raise_server_exceptions=False)
        assert (
            c.post(
                "/webhooks/meta/deauthorize", data={"signed_request": signed}
            ).status_code
            == 503
        )

    def test_a_secret_matching_neither_is_still_refused(self, client):
        signed = make_signed_request(valid_payload(), secret="third-secret")
        assert (
            client.post(
                "/webhooks/meta/deauthorize", data={"signed_request": signed}
            ).status_code
            == 400
        )


class TestTheDeletionReceipt:
    """The deletion route touches no database, so unlike deauthorize its real
    response is assertable here rather than stopping at the engine gate."""

    def test_a_verified_request_returns_a_url_and_code(self, client):
        r = client.post(
            "/webhooks/meta/data-deletion",
            data={"signed_request": make_signed_request(valid_payload())},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["confirmation_code"] == meta_callbacks.confirmation_code(
            SUBJECT, SECRET
        )
        assert body["confirmation_code"] in body["url"]
        assert body["url"].endswith(f"?code={body['confirmation_code']}")

    def test_the_returned_url_matches_the_real_mount_path(self, client):
        """Meta stores this URL. If the mount in app.py moves, every receipt
        already issued breaks — so the URL is built from the route, not from a
        literal, and this asserts the two agree."""
        r = client.post(
            "/webhooks/meta/data-deletion",
            data={"signed_request": make_signed_request(valid_payload())},
        )
        url = r.json()["url"]
        assert "/webhooks/meta/data-deletion/status" in url
        assert (
            client.get(
                url.split("?")[0], params={"code": r.json()["confirmation_code"]}
            ).status_code
            == 200
        )

    @pytest.mark.parametrize(
        "bad", ["", "short", "g" * 16, "ABCDEF0123456789", "0123456789abcdef0"]
    )
    def test_the_status_door_refuses_a_malformed_code(self, client, bad):
        assert (
            client.get(
                "/webhooks/meta/data-deletion/status", params={"code": bad}
            ).status_code
            == 400
        )

    def test_the_status_door_never_claims_completion(self, client):
        code = meta_callbacks.confirmation_code(SUBJECT, SECRET)
        body = client.get(
            "/webhooks/meta/data-deletion/status", params={"code": code}
        ).json()
        assert "complet" not in body["detail"].lower()
        assert "delet" in body["detail"].lower()


class TestTheGuardsMutationFoundUnpinned:
    """Three mutants survived the first pass. Two were real gaps and are pinned
    here; the third was diagnosed INERT and is documented rather than chased.

    * Removing the single-secret `if not app_secret` guard survived, because
      `verify_signed_request` filters falsy secrets before ever calling
      `parse_signed_request` — so the route can no longer reach it. It is still
      the primitive's own contract for direct callers, so it is pinned
      DIRECTLY here rather than through a route.
    * Dropping the workspace half of the revoke predicate survived, because no
      test executes SQL at all.
    * Removing `if not secrets:` survived and CANNOT be killed: with the loop
      not entered, `raise last` fires on the pre-seeded refusal, so the
      fail-closed property is held twice over. An inert mutant is a redundant
      guard, not a weak test, and the two diagnoses want opposite responses.
    """

    @pytest.mark.parametrize("secret", [None, ""])
    def test_the_primitive_refuses_directly_when_it_has_no_secret(self, secret):
        with pytest.raises(meta_callbacks.SignedRequestInvalid):
            meta_callbacks.parse_signed_request(
                make_signed_request(valid_payload()), secret
            )

    def test_an_empty_candidate_list_refuses(self):
        with pytest.raises(meta_callbacks.SignedRequestInvalid):
            meta_callbacks.verify_signed_request(
                make_signed_request(valid_payload()), []
            )

    def test_the_revoke_predicate_is_scoped_by_workspace_not_account_alone(self):
        """No migration declares FORCE ROW LEVEL SECURITY, so whether RLS applies
        depends on whether the connecting role owns the tables — which this code
        cannot know. Scoped on the pair, the statement is correct either way;
        scoped on the account alone it is a cross-tenant write whenever RLS is
        bypassed. Asserted against the statement because the predicate IS the
        property at this layer."""
        revoking = [
            s for s in _sql_literals(meta_callbacks) if "oauth_credentials" in s
        ]
        assert len(revoking) == 1
        sql = revoking[0]
        assert "UPDATE" in sql.upper()
        assert "workspace_id" in sql, (
            "the revoke predicate lost its workspace scope — under an owning"
            " role that is a cross-tenant credential write"
        )


@pytest.fixture
def verified_lines():
    """The app logger does not propagate (`src/utils/logger.py`), so `caplog`
    never sees it: a handler on the logger itself records the lines."""
    import logging

    from src.utils.logger import logger as app_logger

    records: list[str] = []

    class _Grab(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = _Grab(level=logging.INFO)
    previous = app_logger.level
    if previous > logging.INFO:
        app_logger.setLevel(logging.INFO)
    app_logger.addHandler(handler)
    try:
        yield lambda: [m for m in records if "meta callback verified by" in m]
    finally:
        app_logger.removeHandler(handler)
        app_logger.setLevel(previous)


class TestTheLineNamesWhichSecretVerified:
    """#739's instrument: after a callback verifies, one INFO line says WHICH
    setting signed it — by name and position, never by value. Which Meta app
    the URLs are registered under is a dashboard fact the code cannot read;
    this line and one press of Meta's test button rule it."""

    def _post(self, client, body):
        return client.post("/webhooks/meta/deauthorize", data={"signed_request": body})

    def test_the_legacy_secret_alone_is_named_as_candidate_one_of_one(
        self, client, verified_lines
    ):
        assert (
            self._post(client, make_signed_request(valid_payload())).status_code == 503
        )
        assert verified_lines() == [
            "meta callback verified by FACEBOOK_APP_SECRET (candidate 1 of 1)"
        ]

    @pytest.mark.parametrize(
        ("signer", "expected"),
        [
            ("ig-secret", "INSTAGRAM_APP_SECRET (candidate 1 of 2)"),
            (SECRET, "FACEBOOK_APP_SECRET (candidate 2 of 2)"),
        ],
    )
    def test_with_both_configured_the_line_names_the_one_that_signed(
        self, monkeypatch, verified_lines, signer, expected
    ):
        monkeypatch.setattr(
            settings, "INSTAGRAM_APP_SECRET", "ig-secret", raising=False
        )
        monkeypatch.setattr(settings, "FACEBOOK_APP_SECRET", SECRET, raising=False)
        c = TestClient(create_app(env={}), raise_server_exceptions=False)
        body = make_signed_request(valid_payload(), secret=signer)
        assert self._post(c, body).status_code == 503
        lines = verified_lines()
        assert lines == [f"meta callback verified by {expected}"]
        assert signer not in lines[0] and "ig-secret" not in lines[0], (
            "the value never reaches the log"
        )

    def test_the_names_and_the_secrets_share_one_order(self, monkeypatch):
        monkeypatch.setattr(
            settings, "INSTAGRAM_APP_SECRET", "ig-secret", raising=False
        )
        monkeypatch.setattr(settings, "FACEBOOK_APP_SECRET", SECRET, raising=False)
        assert meta_callbacks.app_secrets() == ["ig-secret", SECRET]
        assert meta_callbacks.app_secret_names() == [
            "INSTAGRAM_APP_SECRET",
            "FACEBOOK_APP_SECRET",
        ]
        monkeypatch.setattr(settings, "INSTAGRAM_APP_SECRET", None, raising=False)
        assert meta_callbacks.app_secret_names() == ["FACEBOOK_APP_SECRET"]

    def test_a_refused_request_logs_no_verified_line(self, client, verified_lines):
        assert self._post(client, _wrong_secret()).status_code == 400
        assert verified_lines() == []


#: Both write doors. Every bound below is asserted on each: the two have
#: separate handlers and could regress independently.
META_POSTS = ("/webhooks/meta/deauthorize", "/webhooks/meta/data-deletion")
#: Meta's body type.
FORM_TYPE = "application/x-www-form-urlencoded"


def _form_in_two(size: int) -> list[bytes]:
    """A urlencoded body of exactly *size* bytes, one field, as two messages."""
    prefix = b"signed_request="
    body = prefix + b"a" * (size - len(prefix))
    return [body[: size // 2], body[size // 2 :]]


class TestTheBodyIsBoundedBeforeItIsParsed:
    """Both doors are anonymous until the signature verifies, so neither lets
    the framework parse what arrives: only Meta's urlencoded form is read, only
    up to `SIGNED_REQUEST_MAX_BYTES`, and only those bytes are parsed — then
    verified exactly as before."""

    @pytest.mark.parametrize("path", META_POSTS)
    def test_a_multipart_body_is_refused_without_being_parsed(
        self, client, monkeypatch, path
    ):
        """Correctly signed, so the refusal is the body's type and nothing else."""
        parsed = []
        parse = MultiPartParser.parse

        async def spy(parser):
            parsed.append(parser)
            return await parse(parser)

        monkeypatch.setattr(MultiPartParser, "parse", spy)
        r = client.post(
            path,
            data={"signed_request": make_signed_request(valid_payload())},
            files={"upload": ("upload.bin", b"\0" * 2048)},
        )
        assert r.status_code == 415, r.text
        assert parsed == [], "the multipart parser ran on an unverified body"

    @pytest.mark.parametrize("streamed", [False, True], ids=["declared", "streamed"])
    @pytest.mark.parametrize("path", META_POSTS)
    def test_a_form_just_over_the_cap_is_refused(self, client, path, streamed):
        from src.api.routes import meta as meta_routes

        prefix = b"signed_request="
        filler = meta_routes.SIGNED_REQUEST_MAX_BYTES + 1 - len(prefix)
        r = post_body(
            client,
            path,
            prefix + b"a" * filler,
            streamed=streamed,
            content_type=FORM_TYPE,
        )
        assert r.status_code == 413, r.text

    @pytest.mark.parametrize("path", META_POSTS)
    async def test_a_declared_length_over_the_cap_is_refused_before_the_body_is_read(
        self, client, path
    ):
        """The declared length alone refuses the body: no body message is
        received."""
        chunk = b"a" * 1024
        reads = []

        async def receive():
            reads.append(len(chunk))
            return {"type": "http.request", "body": chunk, "more_body": True}

        sent = []

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"testserver"),
                (b"content-type", FORM_TYPE.encode()),
                (b"content-length", str(SIGNED_REQUEST_MAX_BYTES + 1).encode()),
            ],
            "client": ("127.0.0.1", 123),
            "server": ("testserver", 80),
        }
        await client.app(scope, receive, send)
        starts = [m["status"] for m in sent if m["type"] == "http.response.start"]
        assert starts == [413]
        assert reads == []

    @pytest.mark.parametrize("path", META_POSTS)
    async def test_the_cap_is_on_the_total_across_messages(self, client, path):
        """Each message is under the cap and their total is over it: the cap
        counts the whole body, not one message at a time."""
        resp, received = await post_messages(
            client.app,
            path,
            _form_in_two(SIGNED_REQUEST_MAX_BYTES + 1),
            content_type=FORM_TYPE,
        )
        assert len(received) == 2
        assert max(received) <= SIGNED_REQUEST_MAX_BYTES < sum(received)
        assert resp.status_code == 413, resp.text

    @pytest.mark.parametrize("declared", [True, False], ids=["declared", "streamed"])
    @pytest.mark.parametrize("path", META_POSTS)
    async def test_a_form_of_exactly_the_cap_reaches_verification(
        self, client, path, declared
    ):
        """The cap is the most that is read: a body of exactly
        `SIGNED_REQUEST_MAX_BYTES`, its length declared or not, is read in full
        and answered by verification."""
        resp, received = await post_messages(
            client.app,
            path,
            _form_in_two(SIGNED_REQUEST_MAX_BYTES),
            declared=declared,
            content_type=FORM_TYPE,
        )
        assert len(received) == 2
        assert sum(received) == SIGNED_REQUEST_MAX_BYTES
        assert resp.status_code == 400, resp.text
        assert resp.json() == {"detail": "invalid signed_request"}

    @pytest.mark.parametrize(
        "body",
        [b"", b"other=1", b"signed_request"],
        ids=["empty", "no field", "malformed"],
    )
    @pytest.mark.parametrize("path", META_POSTS)
    def test_a_form_without_a_readable_field_is_refused_as_before(
        self, client, path, body
    ):
        """A missing `signed_request` reads as an empty one did: the one 400
        every verification failure gets."""
        r = post_body(client, path, body, content_type=FORM_TYPE)
        assert r.status_code == 400
        assert r.json() == {"detail": "invalid signed_request"}

    @pytest.mark.parametrize("path", META_POSTS)
    def test_a_signed_form_still_verifies(self, client, path):
        """Meta's type with a parameter on it, as a client may send it, is read
        and verified: deauthorize reaches the engine gate (`client` has no
        engine, so 503), and data-deletion, which needs none, returns the
        receipt."""
        r = client.post(
            path,
            data={"signed_request": make_signed_request(valid_payload())},
            headers={"Content-Type": f"{FORM_TYPE}; charset=UTF-8"},
        )
        if path == "/webhooks/meta/deauthorize":
            assert r.status_code == 503, r.text
        else:
            assert r.status_code == 200, r.text
            assert r.json()["confirmation_code"] == meta_callbacks.confirmation_code(
                SUBJECT, SECRET
            )
