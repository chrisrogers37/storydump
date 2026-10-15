"""The deauthorize route hands the revocation a verified app identity, the
subject and the payload's own order evidence (#1644).

The revocation itself (the mapping door, the per-workspace transactions, the
ordering rule, the epoch writer) is proven against PostgreSQL under the API's
role in `tests/scripts/test_archive_lifecycle_gate.py`. Here the seam is
patched and what crosses it is asserted exactly: which app signed the request
is decided by the setting that verified it, never by the secret's value.
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
from src.services.target import archive_lifecycle, meta_callbacks

SUBJECT = "1234567890"
ISSUED = 1_760_000_000
_ABSENT = object()


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def signed_request(secret: str, issued_at=_ABSENT) -> str:
    """A `signed_request` the way Meta builds one: the ENCODED payload signed."""
    payload = {"algorithm": "HMAC-SHA256", "user_id": SUBJECT}
    if issued_at is not _ABSENT:
        payload["issued_at"] = issued_at
    encoded = _b64url(json.dumps(payload).encode("utf-8"))
    sig = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256)
    return f"{_b64url(sig.digest())}.{encoded}"


@pytest.fixture
def legacy(monkeypatch):
    """The account lookup and revocation, each behind a seam: every call it is
    made with, and the one connection both calls share. The route runs them
    for every request (a mapped account is skipped, not the lookup)."""
    seen = {"resolved": [], "revoked": [], "conns": []}
    matched = [meta_callbacks.AccountRef("acct-1", "ws-1")]

    async def resolve(conn, subject):
        seen["conns"].append(conn)
        seen["resolved"].append(subject)
        return matched

    async def revoke(conn, accounts):
        seen["conns"].append(conn)
        seen["revoked"].append(accounts)
        return len(accounts)

    monkeypatch.setattr(meta_callbacks, "resolve_ig_accounts", resolve)
    monkeypatch.setattr(meta_callbacks, "revoke_for_accounts", revoke)
    seen["matched"] = matched
    return seen


@pytest.fixture
def revocations(monkeypatch, legacy):
    """Every call the route makes to the revocation, with its arguments."""
    seen = []

    async def revoke(engine, *, app_identity, subject, issued_at=None):
        seen.append(
            {
                "engine": engine,
                "app_identity": app_identity,
                "subject": subject,
                "issued_at": issued_at,
            }
        )
        return archive_lifecycle.SubjectRevocation(
            workspaces=2, revoked=1, held=1, excluded=3
        )

    monkeypatch.setattr(archive_lifecycle, "revoke_subject_authorizations", revoke)
    return seen


def arm(monkeypatch, *, instagram=None, facebook=None, app_id=None):
    monkeypatch.setattr(settings, "INSTAGRAM_APP_SECRET", instagram, raising=False)
    monkeypatch.setattr(settings, "FACEBOOK_APP_SECRET", facebook, raising=False)
    monkeypatch.setattr(settings, "INSTAGRAM_APP_ID", app_id, raising=False)


def post(app, secret, issued_at=_ABSENT):
    return TestClient(app).post(
        "/webhooks/meta/deauthorize",
        data={"signed_request": signed_request(secret, issued_at)},
    )


class TestTheAppIsTheSettingThatVerified:
    def test_the_instagram_secret_names_the_configured_app(
        self, monkeypatch, app, engine, revocations
    ):
        arm(monkeypatch, instagram="ig-secret", facebook="fb-secret", app_id="4242")
        r = post(app, "ig-secret", issued_at=ISSUED)
        assert (r.status_code, r.json()) == (200, {"status": "ok"})
        assert revocations == [
            {
                "engine": engine,
                "app_identity": "instagram:4242",
                "subject": SUBJECT,
                "issued_at": ISSUED,
            }
        ]

    def test_a_rotated_instagram_secret_still_names_the_same_app(
        self, monkeypatch, app, revocations
    ):
        arm(monkeypatch, instagram="ig-secret-rotated", app_id="4242")
        assert post(app, "ig-secret-rotated", issued_at=ISSUED).status_code == 200
        assert [c["app_identity"] for c in revocations] == ["instagram:4242"]

    def test_the_legacy_secret_is_the_legacy_app_whatever_the_id(
        self, monkeypatch, app, revocations
    ):
        """Facebook and Instagram never alias: a subject the legacy app signed
        is never looked up as the Instagram app's."""
        arm(monkeypatch, instagram="ig-secret", facebook="fb-secret", app_id="4242")
        assert post(app, "fb-secret", issued_at=ISSUED).status_code == 200
        assert [c["app_identity"] for c in revocations] == ["facebook:legacy"]

    def test_a_secret_both_settings_hold_is_never_the_instagram_app(
        self, monkeypatch, app, revocations
    ):
        """The signature cannot say which app signed, so a request the legacy
        Facebook app may have signed is not looked up as Instagram's."""
        arm(monkeypatch, instagram="shared", facebook="shared", app_id="4242")
        assert post(app, "shared", issued_at=ISSUED).status_code == 200
        assert [c["app_identity"] for c in revocations] == ["facebook:legacy"]

    @pytest.mark.parametrize("app_id", [None, "", "  ", "42a", "1" * 33])
    def test_an_unset_or_malformed_app_id_is_unconfigured(
        self, monkeypatch, app, revocations, app_id
    ):
        arm(monkeypatch, instagram="ig-secret", app_id=app_id)
        assert post(app, "ig-secret", issued_at=ISSUED).status_code == 200
        assert [c["app_identity"] for c in revocations] == ["instagram:unconfigured"]

    def test_a_padded_app_id_is_the_same_app(self, monkeypatch, app, revocations):
        arm(monkeypatch, instagram="ig-secret", app_id=" 4242 ")
        assert post(app, "ig-secret", issued_at=ISSUED).status_code == 200
        assert [c["app_identity"] for c in revocations] == ["instagram:4242"]


class TestTheOrderEvidenceRidesThroughUnjudged:
    """The route passes the verified payload's `issued_at` as it came; the
    revocation decides what counts (an integer timestamp, judged by the
    database's clock)."""

    @pytest.mark.parametrize("value", [ISSUED, "1760000000", True, 1.5, None])
    def test_the_value_crosses_as_sent(self, monkeypatch, app, revocations, value):
        arm(monkeypatch, instagram="ig-secret", app_id="4242")
        assert post(app, "ig-secret", issued_at=value).status_code == 200
        assert revocations[0]["issued_at"] == value
        assert type(revocations[0]["issued_at"]) is type(value)

    def test_an_absent_value_is_none(self, monkeypatch, app, revocations):
        arm(monkeypatch, instagram="ig-secret", app_id="4242")
        assert post(app, "ig-secret").status_code == 200
        assert revocations[0]["issued_at"] is None


class TestNothingUnverifiedReachesTheRevocation:
    def test_a_wrong_secret_is_the_one_400_and_revokes_nothing(
        self, monkeypatch, app, revocations
    ):
        arm(monkeypatch, instagram="ig-secret", app_id="4242")
        r = post(app, "another-secret", issued_at=ISSUED)
        assert (r.status_code, r.json()) == (400, {"detail": "invalid signed_request"})
        assert revocations == []

    def test_control_the_right_secret_does_reach_it(
        self, monkeypatch, app, revocations
    ):
        arm(monkeypatch, instagram="ig-secret", app_id="4242")
        assert post(app, "ig-secret", issued_at=ISSUED).status_code == 200
        assert len(revocations) == 1


@pytest.fixture
def deauthorize_lines():
    """The app logger does not propagate (`src/utils/logger.py`); a handler on
    it records the route's own line."""
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
        yield lambda: [m for m in records if m.startswith("meta deauthorize:")]
    finally:
        app_logger.removeHandler(handler)
        app_logger.setLevel(previous)


def test_the_line_reports_each_count_and_no_identifier(
    monkeypatch, app, revocations, deauthorize_lines
):
    arm(monkeypatch, instagram="ig-secret", app_id="4242")
    assert post(app, "ig-secret", issued_at=ISSUED).status_code == 200
    mapped_line, legacy_line = deauthorize_lines()
    assert mapped_line == (
        "meta deauthorize: 2 workspace(s) mapped, 1 authorization(s) revoked,"
        " 1 left as ambiguous, 3 newer left alone"
    )
    assert legacy_line.startswith(
        "meta deauthorize: 1 account(s) matched, 1 credential(s) revoked"
    )
    assert all(SUBJECT not in line for line in (mapped_line, legacy_line))


@pytest.fixture
def unmapped(monkeypatch):
    """The archive-aware revocation, asked and finding no mapping for the
    subject, as it does for every request while the archive is not live."""
    seen = []

    async def revoke(engine, *, app_identity, subject, issued_at=None):
        seen.append((app_identity, subject, issued_at))
        return archive_lifecycle.SubjectRevocation(
            workspaces=0, revoked=0, held=0, excluded=0
        )

    monkeypatch.setattr(archive_lifecycle, "revoke_subject_authorizations", revoke)
    return seen


class TestWhereNoMappingNamesTheSubjectTheAccountLookupRuns:
    """No mapping is recorded while the archive is not live, so an account
    connected then has none: deauthorize runs the account lookup and the
    revocation on the subject."""

    def test_the_lookup_and_the_revocation_run_once_each_on_the_subject(
        self, monkeypatch, app, engine, unmapped, legacy
    ):
        arm(monkeypatch, instagram="ig-secret", app_id="4242")
        r = post(app, "ig-secret", issued_at=ISSUED)
        assert (r.status_code, r.json()) == (200, {"status": "ok"})
        assert unmapped == [("instagram:4242", SUBJECT, ISSUED)], "mappings come first"
        assert legacy["resolved"] == [SUBJECT]
        assert legacy["revoked"] == [legacy["matched"]]

    def test_both_calls_share_one_transaction(
        self, monkeypatch, app, engine, unmapped, legacy
    ):
        arm(monkeypatch, instagram="ig-secret", app_id="4242")
        assert post(app, "ig-secret", issued_at=ISSUED).status_code == 200
        first, second = legacy["conns"]
        assert first is second is engine.session

    def test_the_legacy_secret_takes_the_same_path(
        self, monkeypatch, app, unmapped, legacy
    ):
        arm(monkeypatch, instagram="ig-secret", facebook="fb-secret", app_id="4242")
        assert post(app, "fb-secret", issued_at=ISSUED).status_code == 200
        assert unmapped == [("facebook:legacy", SUBJECT, ISSUED)]
        assert legacy["resolved"] == [SUBJECT]

    def test_a_subject_nothing_matches_is_still_the_completed_no_op(
        self, monkeypatch, app, unmapped, legacy
    ):
        legacy["matched"].clear()
        arm(monkeypatch, instagram="ig-secret", app_id="4242")
        r = post(app, "ig-secret", issued_at=ISSUED)
        assert (r.status_code, r.json()) == (200, {"status": "ok"})
        assert legacy["revoked"] == [[]]

    def test_the_line_keeps_its_not_established_wording(
        self, monkeypatch, app, unmapped, legacy, deauthorize_lines
    ):
        arm(monkeypatch, instagram="ig-secret", app_id="4242")
        assert post(app, "ig-secret", issued_at=ISSUED).status_code == 200
        (line,) = deauthorize_lines()
        assert line.startswith(
            "meta deauthorize: 1 account(s) matched, 1 credential(s)"
        )
        assert "NOT established" in line and SUBJECT not in line


class TestAnAccountAMappingNamesIsLeftToIt:
    """The lookup by `provider_account_ref` runs for every request. It skips
    exactly the (workspace, account) pairs a mapping names for the subject, so
    an account no mapping names is still reached when another is mapped."""

    @staticmethod
    def mapped(monkeypatch, *pairs, **counts):
        async def revoke(engine, *, app_identity, subject, issued_at=None):
            return archive_lifecycle.SubjectRevocation(
                workspaces=len({ws for ws, _ in pairs}),
                revoked=counts.get("revoked", 0),
                held=counts.get("held", 0),
                excluded=counts.get("excluded", 0),
                mapped=frozenset(pairs),
            )

        monkeypatch.setattr(archive_lifecycle, "revoke_subject_authorizations", revoke)

    def test_the_lookup_runs_when_mappings_exist_and_revokes_what_none_names(
        self, monkeypatch, app, legacy
    ):
        legacy["matched"].append(meta_callbacks.AccountRef("acct-2", "ws-2"))
        self.mapped(monkeypatch, ("ws-1", "acct-1"), revoked=1)
        arm(monkeypatch, instagram="ig-secret", app_id="4242")
        r = post(app, "ig-secret", issued_at=ISSUED)
        assert (r.status_code, r.json()) == (200, {"status": "ok"})
        assert legacy["resolved"] == [SUBJECT]
        assert legacy["revoked"] == [[meta_callbacks.AccountRef("acct-2", "ws-2")]], (
            "the mapped pair is the mapped path's; the unmapped one is the lookup's"
        )

    @pytest.mark.parametrize(
        "counts",
        [
            {"revoked": 1},
            {"held": 1},
            {"excluded": 1},
        ],
        ids=["revoked", "held", "newer"],
    )
    def test_a_mapped_pair_is_skipped_whatever_the_mapped_path_decided(
        self, monkeypatch, app, legacy, counts
    ):
        """A held or newer authorization was left alone on purpose: the lookup
        would only overrule the ordering rule the mapped path applied."""
        self.mapped(monkeypatch, ("ws-1", "acct-1"), **counts)
        arm(monkeypatch, instagram="ig-secret", app_id="4242")
        assert post(app, "ig-secret", issued_at=ISSUED).status_code == 200
        assert legacy["resolved"] == [SUBJECT]
        assert legacy["revoked"] == [[]]

    def test_the_same_account_id_in_another_workspace_is_not_skipped(
        self, monkeypatch, app, legacy
    ):
        """The pair is the key: an account named by a mapping in one workspace
        says nothing about the same id elsewhere."""
        self.mapped(monkeypatch, ("ws-9", "acct-1"), revoked=1)
        arm(monkeypatch, instagram="ig-secret", app_id="4242")
        assert post(app, "ig-secret", issued_at=ISSUED).status_code == 200
        assert legacy["revoked"] == [legacy["matched"]]

    def test_both_calls_still_share_one_transaction(
        self, monkeypatch, app, engine, legacy
    ):
        self.mapped(monkeypatch, ("ws-1", "acct-1"), revoked=1)
        arm(monkeypatch, instagram="ig-secret", app_id="4242")
        assert post(app, "ig-secret", issued_at=ISSUED).status_code == 200
        first, second = legacy["conns"]
        assert first is second is engine.session

    def test_the_answer_is_the_same_200_whatever_was_matched(
        self, monkeypatch, app, legacy
    ):
        legacy["matched"].clear()
        self.mapped(monkeypatch, ("ws-1", "acct-1"), held=1)
        arm(monkeypatch, instagram="ig-secret", app_id="4242")
        r = post(app, "ig-secret", issued_at=ISSUED)
        assert (r.status_code, r.json()) == (200, {"status": "ok"})


def test_a_request_no_secret_signed_runs_neither_path(
    monkeypatch, app, unmapped, legacy
):
    arm(monkeypatch, instagram="ig-secret", app_id="4242")
    r = post(app, "another-secret", issued_at=ISSUED)
    assert (r.status_code, r.json()) == (400, {"detail": "invalid signed_request"})
    assert unmapped == [] and legacy["resolved"] == [] and legacy["revoked"] == []


class TestAppIdentity:
    """`meta_callbacks.app_identity` directly: the setting decides, the value
    never does."""

    def test_any_setting_but_the_instagram_one_is_the_legacy_app(self, monkeypatch):
        arm(monkeypatch, app_id="4242")
        assert meta_callbacks.app_identity("FACEBOOK_APP_SECRET") == "facebook:legacy"

    def test_the_instagram_setting_is_the_configured_app(self, monkeypatch):
        arm(monkeypatch, app_id="4242")
        assert meta_callbacks.app_identity("INSTAGRAM_APP_SECRET") == "instagram:4242"

    def test_the_skew_is_five_minutes(self):
        assert meta_callbacks.ORDERING_SKEW_SECONDS == 300
