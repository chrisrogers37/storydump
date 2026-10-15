"""The data-deletion route and its status page (#1644), the receipt door
behind a seam: what the route hands it, what it answers, and what it never
writes to a log. The door itself (the classification, the replay, the frozen
scope, the roles) is proven against PostgreSQL as the API's own login in
`tests/scripts/test_meta_deletion_receipts_gate.py`.

Without `app.state.archive_keys` nothing about the route changes: the receipt
is the logged 16-hex one and the door is never called.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging

import pytest
from fastapi.testclient import TestClient

from src.config.settings import settings
from src.services.target import meta_callbacks
from src.services.target.instagram_catalog import TombstoneKeys

SUBJECT = "1234567890"
ISSUED = 1_760_000_000
KEYS = TombstoneKeys(
    current_version=2, keys={1: bytes(range(32)), 2: bytes(range(32, 64))}
)
CODE = "0123456789abcdef0123456789abcdef"
RECEIPT = "11111111-1111-4111-8111-111111111111"
_ABSENT = object()


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def signed(secret: str, *, user_id=SUBJECT, issued_at=ISSUED) -> str:
    """A `signed_request` the way Meta builds one: the ENCODED payload signed."""
    payload = {"algorithm": "HMAC-SHA256", "user_id": user_id}
    if issued_at is not _ABSENT:
        payload["issued_at"] = issued_at
    encoded = _b64url(json.dumps(payload).encode("utf-8"))
    sig = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256)
    return f"{_b64url(sig.digest())}.{encoded}"


@pytest.fixture(autouse=True)
def armed(monkeypatch):
    monkeypatch.setattr(settings, "INSTAGRAM_APP_SECRET", "ig-secret", raising=False)
    monkeypatch.setattr(settings, "FACEBOOK_APP_SECRET", "fb-secret", raising=False)
    monkeypatch.setattr(settings, "INSTAGRAM_APP_ID", "4242", raising=False)


@pytest.fixture
def door(monkeypatch):
    """The receipt door: every call it is made with, answering a fresh receipt
    the first time a fingerprint set is seen and a replay of it after."""
    calls, seen = [], {}

    async def receive(engine, *, app_identity, fingerprints, subject, issued_at):
        calls.append(
            {
                "engine": engine,
                "app_identity": app_identity,
                "fingerprints": fingerprints,
                "subject": subject,
                "issued_at": issued_at,
            }
        )
        key = (app_identity, fingerprints[0])
        replay = key in seen
        seen.setdefault(key, f"{RECEIPT[:-1]}{len(seen)}")
        code = f"{len(calls):032x}"
        return meta_callbacks.DeletionReceipt(seen[key], replay, "pending", code)

    monkeypatch.setattr(meta_callbacks, "receive_deletion", receive)
    return calls


@pytest.fixture
def keyed(app):
    app.state.archive_keys = KEYS
    return app


def post(app, secret="ig-secret", **kw):
    return TestClient(app, raise_server_exceptions=False).post(
        "/webhooks/meta/data-deletion", data={"signed_request": signed(secret, **kw)}
    )


class TestWithoutTheKeyRingNothingChanges:
    def test_the_receipt_is_the_logged_16_hex_one_and_the_door_is_never_called(
        self, app, door
    ):
        r = post(app, "fb-secret")
        assert r.status_code == 200, r.text
        code = meta_callbacks.confirmation_code(SUBJECT, "fb-secret")
        assert len(code) == 16
        assert r.json() == {
            "url": f"http://testserver/webhooks/meta/data-deletion/status?code={code}",
            "confirmation_code": code,
        }
        assert door == []

    def test_a_non_numeric_subject_is_answered_with_the_16_hex_receipt(self, app, door):
        """Only the durable path insists on an id; the route without the key
        ring does not."""
        r = post(app, "fb-secret", user_id="some-subject")
        assert r.status_code == 200 and len(r.json()["confirmation_code"]) == 16
        assert door == []

    def test_an_unset_ring_is_the_same_as_an_absent_one(self, app, door):
        app.state.archive_keys = None
        assert len(post(app).json()["confirmation_code"]) == 16
        assert door == []


class TestAVerifiedRequestIsRecorded:
    def test_the_door_is_given_the_app_the_fingerprints_the_subject_and_the_evidence(
        self, keyed, engine, door
    ):
        secret_request = signed("ig-secret")
        r = TestClient(keyed, raise_server_exceptions=False).post(
            "/webhooks/meta/data-deletion", data={"signed_request": secret_request}
        )
        assert r.status_code == 200, r.text
        (call,) = door
        assert call["engine"] is engine
        assert call["app_identity"] == "instagram:4242"
        assert call["subject"] == SUBJECT
        assert call["issued_at"] == ISSUED
        segment = secret_request.split(".")[1]
        assert call["fingerprints"] == meta_callbacks.request_fingerprints(
            KEYS, "instagram:4242", segment
        )
        assert [v for v, _ in call["fingerprints"]] == [2, 1]

    def test_the_answer_is_the_capability_and_the_status_url_it_reads(
        self, keyed, door
    ):
        r = post(keyed)
        code = f"{1:032x}"
        assert r.json() == {
            "url": f"http://testserver/webhooks/meta/data-deletion/status?code={code}",
            "confirmation_code": code,
        }
        assert meta_callbacks.CAPABILITY_CODE.fullmatch(r.json()["confirmation_code"])

    @pytest.mark.parametrize("value", [ISSUED, "1760000000", True, 1.5, None])
    def test_the_order_evidence_crosses_as_sent(self, keyed, door, value):
        assert post(keyed, issued_at=value).status_code == 200
        assert door[0]["issued_at"] == value
        assert type(door[0]["issued_at"]) is type(value)

    def test_an_absent_evidence_is_none(self, keyed, door):
        assert post(keyed, issued_at=_ABSENT).status_code == 200
        assert door[0]["issued_at"] is None

    def test_the_legacy_facebook_secret_is_the_legacy_app_never_an_instagram_one(
        self, keyed, door
    ):
        assert post(keyed, "fb-secret").status_code == 200
        assert door[0]["app_identity"] == "facebook:legacy"
        instagram = meta_callbacks.request_fingerprints(
            KEYS, "instagram:4242", signed("fb-secret").split(".")[1]
        )
        assert door[0]["fingerprints"] != instagram

    def test_a_secret_both_settings_hold_is_recorded_as_the_legacy_app(
        self, keyed, door, monkeypatch
    ):
        """The signature cannot say which app signed, so the door must not be
        told Instagram: the legacy identity is the one it holds for review."""
        monkeypatch.setattr(settings, "FACEBOOK_APP_SECRET", "ig-secret", raising=False)
        assert post(keyed, "ig-secret").status_code == 200
        assert door[0]["app_identity"] == "facebook:legacy"
        instagram = meta_callbacks.request_fingerprints(
            KEYS, "instagram:4242", signed("ig-secret").split(".")[1]
        )
        assert door[0]["fingerprints"] != instagram

    @pytest.mark.parametrize("app_id", [None, "", "42a"])
    def test_an_unconfigured_instagram_app_is_recorded_as_unconfigured(
        self, keyed, door, monkeypatch, app_id
    ):
        monkeypatch.setattr(settings, "INSTAGRAM_APP_ID", app_id, raising=False)
        assert post(keyed).status_code == 200
        assert door[0]["app_identity"] == "instagram:unconfigured"

    def test_a_rotated_secret_under_one_setting_presents_the_same_fingerprints(
        self, keyed, door, monkeypatch
    ):
        """Same app, same signed segment: the door sees one request, so the
        retry after a secret rotation is a replay, not a second episode."""
        assert post(keyed).status_code == 200
        monkeypatch.setattr(
            settings, "INSTAGRAM_APP_SECRET", "ig-rotated", raising=False
        )
        # The signature covers the segment under the NEW secret; the segment
        # itself is what Meta re-sends, so rebuild the request around it.
        original = signed("ig-secret")
        segment = original.split(".")[1]
        sig = hmac.new(b"ig-rotated", segment.encode("ascii"), hashlib.sha256).digest()
        retried = TestClient(keyed, raise_server_exceptions=False).post(
            "/webhooks/meta/data-deletion",
            data={"signed_request": f"{_b64url(sig)}.{segment}"},
        )
        assert retried.status_code == 200
        assert door[0]["fingerprints"] == door[1]["fingerprints"]
        assert door[0]["app_identity"] == door[1]["app_identity"] == "instagram:4242"

    def test_an_exact_retry_is_the_same_receipt_behind_a_new_capability(
        self, keyed, door
    ):
        first, again = post(keyed), post(keyed)
        assert door[0]["fingerprints"] == door[1]["fingerprints"]
        assert first.json()["confirmation_code"] != again.json()["confirmation_code"]

    def test_a_request_with_other_evidence_presents_other_fingerprints(
        self, keyed, door
    ):
        post(keyed, issued_at=ISSUED)
        post(keyed, issued_at=ISSUED + 1)
        assert door[0]["fingerprints"][0] != door[1]["fingerprints"][0]


class TestNothingUnverifiedOrUnrecordableReachesTheDoor:
    def test_a_wrong_secret_is_the_one_400_and_records_nothing(self, keyed, door):
        r = post(keyed, "another-secret")
        assert (r.status_code, r.json()) == (400, {"detail": "invalid signed_request"})
        assert door == []

    @pytest.mark.parametrize("user_id", ["abc", "12 34", "-5", "1" * 65, "٣٤٥"])
    def test_a_verified_subject_that_is_no_id_is_refused_not_retried(
        self, keyed, door, user_id
    ):
        r = post(keyed, user_id=user_id)
        assert (r.status_code, r.json()) == (400, {"detail": "invalid signed_request"})
        assert door == []

    def test_the_positive_control_a_numeric_subject_is_recorded(self, keyed, door):
        assert post(keyed, user_id="1" * 64).status_code == 200
        assert door[0]["subject"] == "1" * 64

    def test_a_door_that_fails_is_a_500_and_no_receipt(self, keyed, door, monkeypatch):
        async def broken(engine, **kw):
            raise RuntimeError("the door is down")

        monkeypatch.setattr(meta_callbacks, "receive_deletion", broken)
        r = post(keyed)
        assert r.status_code == 500
        assert "confirmation_code" not in r.text


@pytest.fixture
def lines():
    """The app logger does not propagate, so a handler on it records the lines."""
    from src.utils.logger import logger as app_logger

    records: list[str] = []

    class _Grab(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = _Grab(level=logging.DEBUG)
    previous = app_logger.level
    app_logger.setLevel(logging.DEBUG)
    app_logger.addHandler(handler)
    try:
        yield records
    finally:
        app_logger.removeHandler(handler)
        app_logger.setLevel(previous)


class TestNothingOfTheRequestIsLogged:
    def test_the_log_names_the_receipt_and_never_the_subject_segment_or_secret(
        self, keyed, door, lines
    ):
        request = signed("ig-secret")
        r = TestClient(keyed, raise_server_exceptions=False).post(
            "/webhooks/meta/data-deletion", data={"signed_request": request}
        )
        code = r.json()["confirmation_code"]
        fingerprints = [d for _, d in door[0]["fingerprints"]]
        joined = "\n".join(lines)
        assert f"{RECEIPT[:-1]}0" in joined, "the receipt id is what an operator needs"
        for secret_thing in (
            SUBJECT,
            request,
            request.split(".")[1],
            "ig-secret",
            "fb-secret",
            code,
            *fingerprints,
        ):
            assert secret_thing not in joined

    def test_a_refused_request_logs_no_subject_either(self, keyed, door, lines):
        post(keyed, user_id="abc")
        joined = "\n".join(lines)
        assert "meta data-deletion rejected" in joined
        assert "abc" not in joined


class TestTheStatusPage:
    @pytest.fixture
    def states(self, monkeypatch):
        """The status door: the state each capability reads as, and every code
        it was asked about."""
        known, asked = {}, []

        async def status(engine, code):
            asked.append(code)
            return known.get(code)

        monkeypatch.setattr(meta_callbacks, "deletion_status", status)
        known["asked"] = asked
        return known

    def get(self, app, code):
        return TestClient(app, raise_server_exceptions=False).get(
            "/webhooks/meta/data-deletion/status", params={"code": code}
        )

    def test_a_receipt_in_progress_says_so_and_is_never_cached(self, app, states):
        states[CODE] = "in_progress"
        r = self.get(app, CODE)
        assert r.status_code == 200
        assert r.headers["cache-control"] == "no-store"
        assert r.json() == {
            "code": CODE,
            "status": "in_progress",
            "detail": (
                "Your deletion request was received and is being carried out:"
                " content imported from the Instagram accounts you authorized is"
                " removed from Storydump as that work finishes. Storydump cannot"
                " withdraw copies that were already delivered to a Telegram chat,"
                " such as approval cards. A workspace itself is removed only by"
                " its owner, through a confirmed offboarding with a grace window."
            ),
        }

    def test_a_completed_receipt_says_so_and_is_never_cached(self, app, states):
        states[CODE] = "completed"
        r = self.get(app, CODE)
        assert r.status_code == 200
        assert r.headers["cache-control"] == "no-store"
        assert r.json() == {
            "code": CODE,
            "status": "completed",
            "detail": (
                "Your deletion request was carried out: Storydump no longer holds"
                " content imported from the Instagram accounts you authorized."
                " Storydump cannot withdraw copies that were already delivered to"
                " a Telegram chat, such as approval cards. A workspace itself is"
                " removed only by its owner, through a confirmed offboarding with"
                " a grace window."
            ),
        }

    @pytest.mark.parametrize("state", ["in_progress", "completed"])
    def test_both_states_record_that_delivered_telegram_copies_cannot_be_withdrawn(
        self, app, states, state
    ):
        """The limit is not Storydump's to lift, and neither state may leave it
        out: an approval card already in a chat is outside this system's reach."""
        states[CODE] = state
        detail = self.get(app, CODE).json()["detail"].lower()
        assert "cannot withdraw copies" in detail
        assert "telegram chat" in detail and "approval cards" in detail

    def test_completed_speaks_only_of_what_storydump_holds(self, app, states):
        """A remote erasure is never counted as done: "completed" says what
        Storydump no longer holds, and never that nothing remains anywhere."""
        states[CODE] = "completed"
        detail = self.get(app, CODE).json()["detail"]
        assert "Storydump no longer holds content imported" in detail
        assert "remains" not in detail.lower()
        states[CODE] = "in_progress"
        assert "no longer holds" not in self.get(app, CODE).json()["detail"], (
            "the same sentence before the work is done would be the overclaim"
        )

    def test_a_code_nothing_issued_is_the_404_and_is_never_cached(self, app, states):
        r = self.get(app, CODE)
        assert (r.status_code, r.json()) == (
            404,
            {"detail": "unknown confirmation code"},
        )
        assert r.headers["cache-control"] == "no-store"

    def test_the_code_is_trimmed_before_it_is_looked_up(self, app, states):
        states[CODE] = "in_progress"
        assert self.get(app, f"  {CODE}\t").status_code == 200
        assert states["asked"] == [CODE]

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "   ",
            "short",
            "0" * 31,
            "0" * 33,
            CODE.upper(),
            "g" * 32,
            CODE[:-1] + "١",
            "0" * 15,
            "0" * 17,
            "G" * 16,
        ],
        ids=lambda v: repr(v)[:20],
    )
    def test_a_code_of_no_known_shape_is_400_and_asks_nobody(self, app, states, bad):
        r = self.get(app, bad)
        assert (r.status_code, r.json()) == (
            400,
            {"detail": "malformed confirmation code"},
        )
        assert states["asked"] == []

    def test_a_status_the_door_never_reports_is_never_echoed(self, app, states):
        """Only `in_progress` and `completed` are public: a receipt held for
        review reads as in progress at the door itself, and anything else is a
        failure here rather than a word to a stranger."""
        states[CODE] = "needs_review"
        r = self.get(app, CODE)
        assert r.status_code == 500
        assert "needs_review" not in r.text

    def test_no_text_the_page_says_claims_a_credential_was_revoked(self, app, states):
        """A deletion request does not revoke the publishing credential
        (deauthorize does), so no page says it did."""
        details = []
        for state in ("in_progress", "completed"):
            states[CODE] = state
            details.append(self.get(app, CODE).json()["detail"])
        details.append(self.get(app, "0123456789abcdef").json()["detail"])
        for detail in details:
            assert "revok" not in detail.lower() and "credential" not in detail.lower()


class TestTheLegacyStatusPage:
    """A deployment without the key ring hands out 16-hex receipts that were
    logged, never stored: the page answers them `received` and touches no database."""

    def get(self, client, code):
        return client.get("/webhooks/meta/data-deletion/status", params={"code": code})

    def test_it_says_received_and_touches_no_database(self, app, monkeypatch):
        async def never(engine, code):  # pragma: no cover — must not run
            raise AssertionError("a legacy receipt is not looked up")

        monkeypatch.setattr(meta_callbacks, "deletion_status", never)
        code = meta_callbacks.confirmation_code(SUBJECT, "ig-secret")
        r = self.get(TestClient(app), code)
        assert r.status_code == 200
        assert r.json() == {
            "code": code,
            "status": "received",
            "detail": (
                "Your deletion request was received. Removing a workspace and its"
                " contents is performed by that workspace's owner through a"
                " confirmed offboarding with a grace window, and is not carried"
                " out automatically from this request."
            ),
        }
        assert r.headers["cache-control"] == "no-store"

    def test_it_never_claims_completion(self, app):
        body = self.get(TestClient(app), "0123456789abcdef").json()
        assert "complet" not in body["detail"].lower()
        assert "delet" in body["detail"].lower()

    def test_the_durable_lookup_needs_an_engine_and_a_legacy_page_does_not(self):
        from src.api.app import create_app

        client = TestClient(create_app(env={}), raise_server_exceptions=False)
        assert self.get(client, "0123456789abcdef").status_code == 200
        assert self.get(client, CODE).status_code == 503
