"""The owner's routes for admitting from the waitlist (#1723):
`GET /api/v1/ops/waitlist` and `POST /api/v1/admissions`, for the people in
`OPS_USER_IDS` alone.

Each test patches `signup_admissions` at its seam, so a route that grew a
statement of its own would meet the fake engine's refusal. The doors behind
the seam are proven against the replayed schema by
`tests/scripts/test_waitlist_gate.py`.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import replace

import pytest

from src.api.principal import current_principal
from src.api.routes import admissions
from src.config.settings import settings
from src.services.target import signup_admissions, vocabulary
from src.utils.logger import logger as app_logger
from tests.src.api.conftest import FRONT, PRINCIPAL
from tests.src.api.test_token_principal import PERSON_TOKEN, SERVICE_TOKEN

OTHER_USER = "55555555-5555-4555-8555-555555555555"
ADDRESS = "person@example.com"
WAITLIST = "/api/v1/ops/waitlist"
ADMISSIONS = "/api/v1/admissions"
AT = dt.datetime(2026, 10, 12, 9, 30, tzinfo=dt.timezone.utc)
ROWS = [
    {
        "email": ADDRESS,
        "on_waitlist": True,
        "joined_at": AT,
        "state": "waiting",
        "admitted_at": None,
        "emailed_at": None,
    },
    {
        "email": "never-joined@example.com",
        "on_waitlist": False,
        "joined_at": None,
        "state": "admitted",
        "admitted_at": AT,
        "emailed_at": None,
    },
]
#: A person-bound token of an operator, minted read-only.
READONLY_TOKEN = replace(PERSON_TOKEN, token_role="readonly")


@pytest.fixture
def as_principal(app):
    def install(principal):
        app.dependency_overrides[current_principal] = lambda: principal
        return principal

    yield install
    app.dependency_overrides.clear()


@pytest.fixture
def operators(monkeypatch):
    """`OPS_USER_IDS` names the test's person; a test may name others."""

    def name(value: str) -> None:
        monkeypatch.setattr(settings, "OPS_USER_IDS", value)

    name(PRINCIPAL.user_id)
    return name


@pytest.fixture
def origin(monkeypatch):
    """The web origin the claim link is built from."""
    monkeypatch.setattr(settings, "WEB_APP_URL", FRONT, raising=False)


@pytest.fixture
def listed(monkeypatch):
    """`signup_admissions.waitlist`, answering ROWS and recording each call."""
    calls = []

    async def waitlist(conn, *, ref=None):
        calls.append({"conn": conn, "ref": ref})
        return ROWS

    monkeypatch.setattr(signup_admissions, "waitlist", waitlist)
    return calls


@pytest.fixture
def admitted(monkeypatch):
    """`signup_admissions.admit`, recording what it was asked and answering
    the seam's ``outcome`` (``admitted`` unless a test sets another)."""

    class Seam(list):
        outcome = "admitted"

    seam = Seam()

    async def admit(conn, *, email, claim_base, resend=False):
        seam.append(
            {"conn": conn, "email": email, "claim_base": claim_base, "resend": resend}
        )
        return seam.outcome

    monkeypatch.setattr(signup_admissions, "admit", admit)
    return seam


@pytest.fixture
def lines():
    """The app logger's lines. It does not propagate (`src/utils/logger.py`),
    so `caplog` never sees it, and a handler on the logger records them."""
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
        yield records
    finally:
        app_logger.removeHandler(handler)
        app_logger.setLevel(previous)


class TestTheWaitlist:
    def test_an_operator_reads_its_rows_in_the_envelope(
        self, client, as_principal, operators, listed, engine
    ):
        as_principal(PERSON_TOKEN)
        resp = client.get(WAITLIST)
        assert resp.status_code == 200, resp.text
        at = AT.isoformat()
        assert resp.json() == {
            "v": 1,
            "kind": "waitlist",
            "data": {
                "rows": [
                    {**ROWS[0], "joined_at": at},
                    {**ROWS[1], "admitted_at": at},
                ]
            },
            "error": None,
        }
        assert listed == [{"conn": engine.session, "ref": None}]

    @pytest.mark.parametrize(
        "who", [PRINCIPAL, READONLY_TOKEN], ids=["session", "readonly-token"]
    )
    def test_a_session_and_a_readonly_token_read_it_too(
        self, client, as_principal, operators, listed, who
    ):
        as_principal(who)
        assert client.get(WAITLIST).status_code == 200
        assert len(listed) == 1

    def test_it_takes_no_parameter(self, client, as_principal, operators, listed):
        """The newest `LIST_LIMIT` rows, whatever the query says: no ref and no
        limit reach the service."""
        as_principal(PERSON_TOKEN)
        ref = signup_admissions.ref(ADDRESS)
        assert client.get(f"{WAITLIST}?ref={ref}&limit=5").status_code == 200
        assert [call["ref"] for call in listed] == [None]

    @pytest.mark.parametrize(
        "who, configured",
        [
            (PERSON_TOKEN, OTHER_USER),
            (PERSON_TOKEN, ""),
            (PRINCIPAL, OTHER_USER),
            (PRINCIPAL, ""),
            (SERVICE_TOKEN, PRINCIPAL.user_id),
        ],
        ids=["token", "token-empty", "session", "session-empty", "service-identity"],
    )
    def test_anyone_else_is_refused_not_ops(
        self, client, as_principal, operators, listed, who, configured
    ):
        operators(configured)
        as_principal(who)
        resp = client.get(WAITLIST)
        assert (resp.status_code, resp.json()["reason"]) == (403, "not_ops")
        assert ADDRESS not in resp.text
        assert listed == []

    def test_without_a_principal_it_is_401(self, client, listed):
        assert client.get(WAITLIST).status_code == 401
        assert listed == []


class TestAnAdmission:
    def test_an_operator_admits_the_address_the_body_names(
        self, client, as_principal, operators, origin, admitted, engine
    ):
        as_principal(PERSON_TOKEN)
        resp = client.post(ADMISSIONS, json={"email": ADDRESS})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"outcome": "admitted"}
        assert admitted == [
            {
                "conn": engine.session,
                "email": ADDRESS,
                "claim_base": f"{FRONT}/claim/",
                "resend": False,
            }
        ]

    def test_resend_reaches_the_pathway(
        self, client, as_principal, operators, origin, admitted
    ):
        admitted.outcome = "resent"
        as_principal(PERSON_TOKEN)
        resp = client.post(ADMISSIONS, json={"email": ADDRESS, "resend": True})
        assert resp.json() == {"outcome": "resent"}
        assert admitted[0]["resend"] is True

    @pytest.mark.parametrize(
        "outcome, status",
        [
            ("admitted", 200),
            ("resent", 200),
            ("admitted_before", 200),
            ("signed_up", 409),
            ("invalid_email", 400),
            ("not_configured", 503),
        ],
    )
    def test_each_outcome_answers_its_status(
        self, client, as_principal, operators, origin, admitted, outcome, status
    ):
        admitted.outcome = outcome
        as_principal(PERSON_TOKEN)
        resp = client.post(ADMISSIONS, json={"email": ADDRESS})
        assert resp.status_code == status, resp.text
        if status == 200:
            assert resp.json() == {"outcome": outcome}
        else:
            assert resp.json() == {
                "detail": admissions.DETAIL[outcome],
                "reason": outcome,
            }
            assert ADDRESS not in resp.text

    def test_the_status_table_is_total_over_the_outcomes(self):
        assert set(admissions.STATUS) == set(vocabulary.ADMIT_OUTCOMES)
        refused = {outcome for outcome, s in admissions.STATUS.items() if s != 200}
        assert set(admissions.DETAIL) == refused
        # the CLI takes exactly the 200 answers as an admission
        assert set(admissions.STATUS) - refused == set(vocabulary.ADMIT_SENTENCES)

    def test_an_address_off_the_waitlist_is_admitted_as_one_on_it(
        self, client, as_principal, operators, origin, admitted, monkeypatch
    ):
        """Any address: the route hands the pathway what it was given,
        and never reads the waitlist first."""

        async def never(conn, *, ref=None):
            raise AssertionError("an admission does not read the waitlist first")

        monkeypatch.setattr(signup_admissions, "waitlist", never)
        as_principal(PERSON_TOKEN)
        for address in (ADDRESS, "never-joined@example.com"):
            resp = client.post(ADMISSIONS, json={"email": address})
            assert resp.json() == {"outcome": "admitted"}
        assert [call["email"] for call in admitted] == [
            ADDRESS,
            "never-joined@example.com",
        ]

    def test_the_address_is_read_from_the_body_only(
        self, client, as_principal, operators, origin, admitted
    ):
        """An address in the query is not read: the body names none, so the
        pathway is handed none, which it answers `invalid_email`."""
        admitted.outcome = "invalid_email"
        as_principal(PERSON_TOKEN)
        resp = client.post(f"{ADMISSIONS}?email={ADDRESS}", json={})
        assert (resp.status_code, resp.json()["reason"]) == (400, "invalid_email")
        assert admitted[0]["email"] is None

    def test_the_claim_base_is_the_one_spelling_of_the_web_origin(
        self, client, as_principal, operators, admitted, monkeypatch
    ):
        monkeypatch.setattr(settings, "WEB_APP_URL", FRONT + "/", raising=False)
        as_principal(PERSON_TOKEN)
        client.post(ADMISSIONS, json={"email": ADDRESS})
        assert admitted[0]["claim_base"] == signup_admissions.claim_base(FRONT)
        assert admitted[0]["claim_base"] == f"{FRONT}/claim/"

    def test_with_no_web_origin_there_is_no_claim_base_and_it_says_which_variable(
        self, client, as_principal, operators, admitted, monkeypatch
    ):
        monkeypatch.setattr(settings, "WEB_APP_URL", None, raising=False)
        admitted.outcome = "not_configured"
        as_principal(PERSON_TOKEN)
        resp = client.post(ADMISSIONS, json={"email": ADDRESS})
        assert resp.status_code == 503
        assert "WEB_APP_URL" in resp.json()["detail"]
        assert admitted[0]["claim_base"] is None

    @pytest.mark.parametrize("body", [b"[1]", b"not json", b'"x"'])
    def test_a_body_that_is_not_an_object_is_400_and_admits_nothing(
        self, client, as_principal, operators, origin, admitted, body
    ):
        as_principal(PERSON_TOKEN)
        resp = client.post(
            ADMISSIONS, content=body, headers={"Content-Type": "application/json"}
        )
        assert resp.status_code == 400
        assert admitted == []

    @pytest.mark.parametrize("resend", ["yes", 1, None])
    def test_a_resend_that_is_not_a_boolean_is_400_and_admits_nothing(
        self, client, as_principal, operators, origin, admitted, resend
    ):
        as_principal(PERSON_TOKEN)
        resp = client.post(ADMISSIONS, json={"email": ADDRESS, "resend": resend})
        assert resp.status_code == 400
        assert admitted == []

    def test_an_answer_logs_its_outcome_and_who_asked_never_the_address(
        self, client, as_principal, operators, origin, admitted, lines
    ):
        admitted.outcome = "signed_up"
        as_principal(PERSON_TOKEN)
        client.post(ADMISSIONS, json={"email": ADDRESS})
        (line,) = [m for m in lines if m.startswith("admission:")]
        assert "signed_up" in line
        assert PERSON_TOKEN.user_id in line and PERSON_TOKEN.token_id in line
        assert not [m for m in lines if ADDRESS in m]


class TestWhoMayAdmit:
    @pytest.mark.parametrize("who", [PRINCIPAL, PERSON_TOKEN], ids=["session", "token"])
    def test_an_operator_admits_on_a_session_or_a_token(
        self, client, as_principal, operators, origin, admitted, who
    ):
        as_principal(who)
        assert client.post(ADMISSIONS, json={"email": ADDRESS}).status_code == 200
        assert len(admitted) == 1

    @pytest.mark.parametrize(
        "who", [READONLY_TOKEN, SERVICE_TOKEN], ids=["readonly-token", "service"]
    )
    def test_a_token_that_may_not_write_is_refused_before_the_body_is_read(
        self, client, as_principal, operators, origin, admitted, who
    ):
        """`readonly_token`, the write rule's own refusal, before `require_ops`
        and before the body: a body that is not JSON would have been a 400,
        and a service identity, which has no person, would have been
        `not_ops`."""
        as_principal(who)
        resp = client.post(
            ADMISSIONS,
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert (resp.status_code, resp.json()["reason"]) == (403, "readonly_token")
        assert admitted == []

    @pytest.mark.parametrize("who", [PRINCIPAL, PERSON_TOKEN], ids=["session", "token"])
    @pytest.mark.parametrize("configured", ["", OTHER_USER], ids=["empty", "another"])
    def test_anyone_outside_ops_user_ids_is_refused_not_ops(
        self, client, as_principal, operators, origin, admitted, who, configured
    ):
        operators(configured)
        as_principal(who)
        resp = client.post(ADMISSIONS, json={"email": ADDRESS})
        assert (resp.status_code, resp.json()["reason"]) == (403, "not_ops")
        assert admitted == []

    def test_without_a_principal_it_is_401(self, client, origin, admitted):
        assert client.post(ADMISSIONS, json={"email": ADDRESS}).status_code == 401
        assert admitted == []
