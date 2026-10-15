"""What a verified Meta callback establishes, and the receipt door's two calls
(#1644): which app signed it, the keyed fingerprint a replay is
recognised by, the order evidence, and the capability the status page is read
with. No database: the door itself is proven under the runtime login in
`tests/scripts/test_meta_deletion_receipts_gate.py`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from datetime import datetime, timezone

import pytest

from src.config.settings import settings
from src.services.target import meta_callbacks
from src.services.target.instagram_catalog import TombstoneKeys
from tests.src.api.test_meta_callbacks import make_signed_request, valid_payload

KEY_1 = bytes(range(32))
KEY_2 = bytes(range(32, 64))
SEGMENT = "eyJhbGdvcml0aG0iOiJITUFDLVNIQTI1NiJ9"
APP = "instagram:4242"


def arm(monkeypatch, *, instagram=None, facebook=None, app_id=None):
    monkeypatch.setattr(settings, "INSTAGRAM_APP_SECRET", instagram, raising=False)
    monkeypatch.setattr(settings, "FACEBOOK_APP_SECRET", facebook, raising=False)
    monkeypatch.setattr(settings, "INSTAGRAM_APP_ID", app_id, raising=False)


@pytest.fixture
def warnings():
    """The warnings the app logger records (it does not propagate, so `caplog`
    never sees it): a handler on the logger itself."""
    from src.utils.logger import logger as app_logger

    records: list[str] = []

    class _Grab(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = _Grab(level=logging.WARNING)
    previous = app_logger.level
    if previous > logging.WARNING:
        app_logger.setLevel(logging.WARNING)
    app_logger.addHandler(handler)
    try:
        yield records
    finally:
        app_logger.removeHandler(handler)
        app_logger.setLevel(previous)


class TestVerifyRequest:
    def test_it_carries_what_verification_established(self, monkeypatch):
        arm(monkeypatch, instagram="ig-secret", facebook="fb-secret", app_id="4242")
        signed = make_signed_request(valid_payload(), secret="ig-secret")
        verified = meta_callbacks.verify_request(signed)
        assert verified.payload == valid_payload()
        assert verified.secret == "ig-secret"
        assert verified.setting_name == "INSTAGRAM_APP_SECRET"
        # The signed segment exactly as received: what a replay is recognised by.
        assert verified.encoded_payload == signed.split(".")[1]
        assert (
            json.loads(base64.urlsafe_b64decode(verified.encoded_payload + "=="))
            == valid_payload()
        )

    @pytest.mark.parametrize(
        ("signer", "setting", "identity"),
        [
            ("ig-secret", "INSTAGRAM_APP_SECRET", "instagram:4242"),
            ("fb-secret", "FACEBOOK_APP_SECRET", "facebook:legacy"),
        ],
    )
    def test_the_signing_setting_names_the_app_and_the_two_never_alias(
        self, monkeypatch, signer, setting, identity
    ):
        arm(monkeypatch, instagram="ig-secret", facebook="fb-secret", app_id="4242")
        verified = meta_callbacks.verify_request(
            make_signed_request(valid_payload(), secret=signer)
        )
        assert verified.setting_name == setting
        assert meta_callbacks.app_identity(verified.setting_name) == identity

    def test_a_rotated_secret_under_one_setting_is_the_same_app(self, monkeypatch):
        identities = set()
        for secret in ("ig-secret-before", "ig-secret-after"):
            arm(monkeypatch, instagram=secret, app_id="4242")
            verified = meta_callbacks.verify_request(
                make_signed_request(valid_payload(), secret=secret)
            )
            identities.add(meta_callbacks.app_identity(verified.setting_name))
        assert identities == {"instagram:4242"}

    def test_a_secret_held_under_both_settings_cannot_name_the_app(
        self, monkeypatch, warnings
    ):
        """The signature verifies against either setting, so it cannot say which
        app signed. Naming the preferred one would read a request the legacy
        Facebook app signed as Instagram's: it names the legacy app, which no
        mapping answers to, and says so once for the request."""
        arm(monkeypatch, instagram="shared", facebook="shared", app_id="4242")
        verified = meta_callbacks.verify_request(
            make_signed_request(valid_payload(), secret="shared")
        )
        assert verified.setting_name == "FACEBOOK_APP_SECRET"
        assert meta_callbacks.app_identity(verified.setting_name) == "facebook:legacy"
        assert verified.secret == "shared"
        (line,) = warnings
        assert "INSTAGRAM_APP_SECRET and FACEBOOK_APP_SECRET" in line
        assert "shared" not in line, "the setting names, never the value"

    def test_the_ambiguity_is_warned_once_for_each_request_and_not_before_one(
        self, monkeypatch, warnings
    ):
        arm(monkeypatch, instagram="shared", facebook="shared", app_id="4242")
        assert warnings == []
        for expected in (1, 2):
            meta_callbacks.verify_request(
                make_signed_request(valid_payload(), secret="shared")
            )
            assert len(warnings) == expected

    @pytest.mark.parametrize(
        ("instagram", "facebook", "signer", "setting"),
        [
            ("ig-secret", "fb-secret", "ig-secret", "INSTAGRAM_APP_SECRET"),
            ("ig-secret", "fb-secret", "fb-secret", "FACEBOOK_APP_SECRET"),
            ("only", None, "only", "INSTAGRAM_APP_SECRET"),
            (None, "only", "only", "FACEBOOK_APP_SECRET"),
            ("only", "", "only", "INSTAGRAM_APP_SECRET"),
        ],
        ids=[
            "distinct-instagram",
            "distinct-legacy",
            "one-set",
            "one-set-legacy",
            "empty",
        ],
    )
    def test_control_a_secret_one_setting_holds_is_named_and_not_warned_about(
        self, monkeypatch, warnings, instagram, facebook, signer, setting
    ):
        """The positive control for the ambiguity: only a value held by BOTH
        settings is ambiguous. An unset or empty one is not a candidate."""
        arm(monkeypatch, instagram=instagram, facebook=facebook, app_id="4242")
        verified = meta_callbacks.verify_request(
            make_signed_request(valid_payload(), secret=signer)
        )
        assert verified.setting_name == setting
        assert warnings == []

    def test_a_request_no_candidate_signed_is_refused(self, monkeypatch):
        arm(monkeypatch, instagram="ig-secret", app_id="4242")
        with pytest.raises(meta_callbacks.SignedRequestInvalid):
            meta_callbacks.verify_request(
                make_signed_request(valid_payload(), secret="another")
            )

    def test_an_unconfigured_deployment_verifies_nothing(self, monkeypatch):
        arm(monkeypatch)
        with pytest.raises(meta_callbacks.SignedRequestInvalid):
            meta_callbacks.verify_request(make_signed_request(valid_payload()))

    def test_its_repr_names_neither_the_subject_the_secret_nor_the_segment(
        self, monkeypatch
    ):
        arm(monkeypatch, instagram="ig-secret", app_id="4242")
        signed = make_signed_request(valid_payload(user_id="1234567890"), "ig-secret")
        shown = repr(meta_callbacks.verify_request(signed))
        assert "INSTAGRAM_APP_SECRET" in shown
        for hidden in ("1234567890", "ig-secret", signed.split(".")[1]):
            assert hidden not in shown


class TestRequestFingerprints:
    def test_each_is_a_pinned_keyed_digest_of_the_signed_segment(self):
        """Pinned: a stored fingerprint must keep matching across releases, or
        every replay after a deploy becomes a new episode."""
        keys = TombstoneKeys(current_version=1, keys={1: KEY_1})
        assert meta_callbacks.request_fingerprints(keys, APP, SEGMENT) == [
            (1, "470fe3ee2ea4d695c005b1033b739d9c225a6359df2893a353263b8c7348fbe3")
        ]
        rotated = TombstoneKeys(current_version=2, keys={1: KEY_1, 2: KEY_2})
        assert meta_callbacks.request_fingerprints(rotated, APP, SEGMENT) == [
            (2, "618e7068b91130ee5673f4579b3cdbc10fa6c0e2681a91ca1f7af5a26abce5a8"),
            (1, "470fe3ee2ea4d695c005b1033b739d9c225a6359df2893a353263b8c7348fbe3"),
        ]

    def test_the_app_is_part_of_the_digest(self):
        keys = TombstoneKeys(current_version=1, keys={1: KEY_1})
        assert meta_callbacks.request_fingerprints(
            keys, "facebook:legacy", SEGMENT
        ) == [(1, "d94696d179ad9ed2020da44d9bd499cafdc3fadc19f06e89aa3ac1ffd9c6a69e")]

    def test_the_segment_is_part_of_the_digest(self):
        keys = TombstoneKeys(current_version=1, keys={1: KEY_1})
        a = meta_callbacks.request_fingerprints(keys, APP, SEGMENT)
        b = meta_callbacks.request_fingerprints(keys, APP, SEGMENT + "A")
        assert a != b

    def test_a_fingerprint_is_64_hex_and_holds_nothing_of_the_request(self):
        keys = TombstoneKeys(current_version=1, keys={1: KEY_1})
        ((_, digest),) = meta_callbacks.request_fingerprints(keys, APP, SEGMENT)
        assert re.fullmatch("[0-9a-f]{64}", digest)
        assert SEGMENT not in digest and "4242" not in digest

    def test_the_current_version_leads_even_when_it_is_not_the_highest(self):
        keys = TombstoneKeys(
            current_version=2, keys={1: KEY_1, 2: KEY_2, 3: bytes(range(64, 96))}
        )
        versions = [
            v for v, _ in meta_callbacks.request_fingerprints(keys, APP, SEGMENT)
        ]
        assert versions == [2, 3, 1]

    def test_a_ring_past_the_doors_bound_keeps_the_current_and_the_newest(self):
        ring = {v: bytes([v]) * 32 for v in range(1, 21)}
        keys = TombstoneKeys(current_version=20, keys=ring)
        versions = [
            v for v, _ in meta_callbacks.request_fingerprints(keys, APP, SEGMENT)
        ]
        assert len(versions) == meta_callbacks.MAX_FINGERPRINTS == 16
        assert versions == list(range(20, 4, -1))

    def test_a_replay_across_a_rotation_matches_by_the_older_version(self):
        """The same request under the ring before and after a rotation: what the
        door stored under version 1 is among the fingerprints presented after."""
        before = TombstoneKeys(current_version=1, keys={1: KEY_1})
        after = TombstoneKeys(current_version=2, keys={1: KEY_1, 2: KEY_2})
        stored = meta_callbacks.request_fingerprints(before, APP, SEGMENT)[0][1]
        presented = [
            d for _, d in meta_callbacks.request_fingerprints(after, APP, SEGMENT)
        ]
        assert presented[0] != stored and stored in presented[1:]


class TestOrderEvidence:
    """Python only type-checks `issued_at`; the database's clock judges it."""

    def test_an_integer_timestamp_is_that_instant_in_utc(self):
        assert meta_callbacks.order_evidence(1_760_000_000) == datetime(
            2025, 10, 9, 8, 53, 20, tzinfo=timezone.utc
        )

    def test_a_future_timestamp_is_still_passed_for_the_database_to_judge(self):
        far = meta_callbacks.order_evidence(4_102_444_800)
        assert far == datetime(2100, 1, 1, tzinfo=timezone.utc)

    @pytest.mark.parametrize(
        "value", [True, False, "1760000000", 1_760_000_000.0, None, [1], {"t": 1}]
    )
    def test_anything_but_an_integer_is_no_evidence(self, value):
        assert meta_callbacks.order_evidence(value) is None

    @pytest.mark.parametrize("value", [10**20, -(10**20)])
    def test_an_integer_no_clock_can_hold_is_no_evidence(self, value):
        assert meta_callbacks.order_evidence(value) is None


class _Result:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


class _Engine:
    """`begin()` yields a connection that records each statement and parameters
    and answers the scripted row."""

    def __init__(self, row):
        self.row = row
        self.statements = []
        self.begun = 0

    def begin(self):
        engine = self

        class _Begin:
            async def __aenter__(self):
                engine.begun += 1
                return engine

            async def __aexit__(self, *exc):
                return False

        return _Begin()

    async def execute(self, statement, params=None):
        self.statements.append((str(statement), params))
        return _Result(self.row)


FINGERPRINTS = [(2, "a" * 64), (1, "b" * 64)]


class TestReceiveDeletion:
    async def receive(self, engine, **over):
        args = dict(
            app_identity=APP,
            fingerprints=FINGERPRINTS,
            subject="1234567890",
            issued_at=1_760_000_000,
        )
        return await meta_callbacks.receive_deletion(engine, **{**args, **over})

    async def test_one_door_call_in_one_transaction_with_the_digest_not_the_code(self):
        engine = _Engine({"receipt": "r-1", "replay": False, "state": "pending"})
        got = await self.receive(engine)
        assert engine.begun == 1
        ((sql, params),) = engine.statements
        assert "FROM fn_meta_deletion_receive(" in sql
        assert re.fullmatch("[0-9a-f]{32}", got.code)
        assert params == {
            "app": APP,
            "fingerprints": ["a" * 64, "b" * 64],
            "version": 2,
            "subject": "1234567890",
            "issued": datetime(2025, 10, 9, 8, 53, 20, tzinfo=timezone.utc),
            "capability": hashlib.sha256(got.code.encode()).hexdigest(),
            "skew": meta_callbacks.ORDERING_SKEW_SECONDS,
        }
        assert (got.receipt_id, got.replay, got.state) == ("r-1", False, "pending")

    async def test_the_current_versions_fingerprint_leads_the_array(self):
        engine = _Engine({"receipt": "r-1", "replay": False, "state": "pending"})
        await self.receive(engine)
        params = engine.statements[0][1]
        assert (params["version"], params["fingerprints"][0]) == (2, "a" * 64)

    @pytest.mark.parametrize("value", [None, "1760000000", True, 1.5])
    async def test_order_evidence_that_is_no_integer_goes_as_null(self, value):
        engine = _Engine({"receipt": "r-1", "replay": False, "state": "pending"})
        await self.receive(engine, issued_at=value)
        assert engine.statements[0][1]["issued"] is None

    async def test_every_delivery_is_given_its_own_capability(self):
        engine = _Engine({"receipt": "r-1", "replay": True, "state": "pending"})
        first, second = await self.receive(engine), await self.receive(engine)
        assert first.code != second.code
        assert first.replay and second.replay
        assert (first.receipt_id == second.receipt_id) is True
        hashes = [params["capability"] for _, params in engine.statements]
        assert hashes[0] != hashes[1]

    async def test_the_capability_stays_out_of_the_repr(self):
        engine = _Engine({"receipt": "r-1", "replay": False, "state": "pending"})
        got = await self.receive(engine)
        assert got.code not in repr(got)


class TestDeletionStatus:
    async def test_it_reads_by_the_digest_of_the_code(self):
        engine = _Engine({"state": "in_progress"})
        code = "0123456789abcdef0123456789abcdef"
        assert await meta_callbacks.deletion_status(engine, code) == "in_progress"
        ((sql, params),) = engine.statements
        assert "FROM fn_meta_deletion_status(" in sql
        assert params == {"hash": hashlib.sha256(code.encode()).hexdigest()}
        assert code not in repr(params)

    async def test_a_code_nothing_issued_is_none(self):
        engine = _Engine(None)
        assert await meta_callbacks.deletion_status(engine, "f" * 32) is None

    def test_the_two_code_shapes_are_exactly_hex_of_their_length(self):
        assert meta_callbacks.CAPABILITY_CODE.fullmatch("0" * 32)
        assert meta_callbacks.LEGACY_CODE.fullmatch("a" * 16)
        for bad in ("0" * 31, "0" * 33, "A" * 32, "g" * 32, "0" * 32 + "\n", ""):
            assert not meta_callbacks.CAPABILITY_CODE.fullmatch(bad)
        for bad in ("0" * 15, "0" * 17, "A" * 16, "g" * 16, "0" * 16 + "\n", ""):
            assert not meta_callbacks.LEGACY_CODE.fullmatch(bad)
