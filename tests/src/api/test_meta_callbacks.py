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

from src.api.app import create_app
from src.config.settings import settings
from src.services.target import meta_callbacks

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
