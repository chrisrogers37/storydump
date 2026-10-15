"""The admission core (#1723, migration 129), over a scripted connection.

The SQL's truth (what each door decides, the grants, the hash the list
matches) is the gate's, `tests/scripts/test_waitlist_gate.py`, and so is
`jobs.enqueue`'s SQL, which is faked here as the nudge's tests fake it. This
pins what the service sends for what it is told: each `admit` outcome with its
statements and parameters in order, the job it asks for (the link's base and
never a token; the key is the address's ref), and the refusals made before
any statement.
"""

from __future__ import annotations

import hashlib
import json
import logging

import pytest
from sqlalchemy.exc import DBAPIError

from src.services.target import jobs, sessions, signup_admissions, vocabulary

BASE = "https://app.example/claim/"
ADDRESS = "new.person@example.com"
REF = hashlib.sha256(ADDRESS.encode()).hexdigest()[:32]


class _Result:
    def __init__(self, value=None, rows=()):
        self.value, self.rows = value, list(rows)

    def scalar_one(self):
        return self.value

    def mappings(self):
        return iter(self.rows)


class _Conn:
    """Answers each `execute` from a queue, in order, and records it; a queued
    exception is raised instead. Records each savepoint as the statement
    index it opened before. A `jobs.enqueue` call is recorded in the same
    order, and answers *job_id* (None: a pending job held the key)."""

    def __init__(self, *answers, job_id="job-1"):
        self.answers = list(answers)
        self.job_id = job_id
        self.statements = []
        self.savepoints = []

    async def execute(self, statement, params=None):
        self.statements.append((str(statement), params))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def begin_nested(self):
        conn = self

        class _Savepoint:
            async def __aenter__(self):
                conn.savepoints.append(len(conn.statements))

            async def __aexit__(self, *exc):
                return False

        return _Savepoint()


def _check_refusal(constraint="ck_signup_admissions_email"):
    """The driver error a CHECK raises inside the door, as SQLAlchemy wraps it."""
    orig = Exception("new row violates check constraint")
    orig.constraint_name = constraint
    return DBAPIError("SELECT fn_signup_admit(:email)", {}, orig)


async def _admit(conn, email=ADDRESS, *, claim_base=BASE, resend=False):
    return await signup_admissions.admit(
        conn, email=email, claim_base=claim_base, resend=resend
    )


def _job(*, resend=False):
    """The `send_email` job `jobs.enqueue` is asked for: a system kind, so no
    workspace; the key is the address's ref; and a pending job holding it
    declines only a resend."""
    return {
        "kind": "send_email",
        "workspace_id": None,
        "serialization_key": f"email:admission:{REF}",
        "payload": {
            "v": 1,
            "to": ADDRESS,
            "template": "admission",
            "params": {"address": ADDRESS, "claim_base": BASE},
        },
        "lane": "bulk",
        "unless_pending": resend,
    }


class TestRef:
    def test_it_is_the_first_32_hex_of_the_address_sha256(self):
        assert signup_admissions.ref(ADDRESS) == REF
        assert len(REF) == signup_admissions.REF_HEX == 32


class TestClaimBase:
    def test_it_is_the_origin_and_the_claim_path(self):
        assert signup_admissions.claim_base("https://app.example") == BASE

    @pytest.mark.parametrize("origin", [None, ""])
    def test_no_web_origin_is_none(self, origin):
        assert signup_admissions.claim_base(origin) is None


class TestAdmit:
    @pytest.fixture(autouse=True)
    def fake_enqueue(self, monkeypatch):
        async def enqueue(conn, **job):
            conn.statements.append(("enqueue", job))
            return conn.job_id

        monkeypatch.setattr(jobs, "enqueue", enqueue)

    async def test_a_new_admission_asks_the_door_then_queues_its_one_email(self):
        conn = _Conn(_Result("admitted"))

        assert await _admit(conn, "  New.Person@Example.COM ") == "admitted"

        (door, door_params), (call, job) = conn.statements
        assert door == "SELECT fn_signup_admit(:email)"
        assert door_params == {"email": ADDRESS}
        assert conn.savepoints == [0], "the door runs under one savepoint"
        assert call == "enqueue"
        assert job == _job(), "a new admission's email is not deduplicated"

    async def test_the_payload_carries_the_links_base_and_never_a_token(self):
        conn = _Conn(_Result("admitted"))
        await _admit(conn)
        payload = conn.statements[1][1]["payload"]
        assert payload == _job()["payload"]
        assert "claim_url" not in payload["params"]
        assert "token" not in json.dumps(payload)

    async def test_admitted_before_queues_nothing(self):
        conn = _Conn(_Result("admitted_before"))
        assert await _admit(conn) == "admitted_before"
        assert [s for s, _ in conn.statements] == ["SELECT fn_signup_admit(:email)"]

    async def test_a_resend_queues_the_email_unless_one_is_waiting(self):
        conn = _Conn(_Result("admitted_before"))
        assert await _admit(conn, resend=True) == "resent"
        (_, door_params), (_, job) = conn.statements
        assert door_params == {"email": ADDRESS}
        assert job == _job(resend=True)

    async def test_a_resend_is_resent_when_an_email_was_already_waiting(self):
        """`resent` means an email to the address is queued, whether this call
        queued it or one was already pending."""
        conn = _Conn(_Result("admitted_before"), job_id=None)
        assert await _admit(conn, resend=True) == "resent"
        assert len(conn.statements) == 2

    async def test_a_new_admission_with_resend_is_admitted(self):
        conn = _Conn(_Result("admitted"))
        assert await _admit(conn, resend=True) == "admitted"
        assert conn.statements[1] == ("enqueue", _job(resend=True))

    @pytest.mark.parametrize("resend", [False, True])
    async def test_a_used_spot_queues_nothing(self, resend):
        conn = _Conn(_Result("signed_up"))
        assert await _admit(conn, resend=resend) == "signed_up"
        assert len(conn.statements) == 1

    @pytest.mark.parametrize(
        "email",
        [
            "a" * 243 + "@example.com",  # 255 characters
            "nul\x00@example.com",
            "lone\ud800@example.com",
            42,
            None,
        ],
    )
    async def test_an_address_the_table_cannot_hold_runs_no_statement(self, email):
        conn = _Conn()
        assert await _admit(conn, email) == "invalid_email"
        assert conn.statements == [] and conn.savepoints == []

    @pytest.mark.parametrize("claim_base", [None, ""])
    async def test_no_web_origin_is_not_configured_and_runs_no_statement(
        self, claim_base
    ):
        conn = _Conn()
        assert await _admit(conn, claim_base=claim_base) == "not_configured"
        assert conn.statements == []

    async def test_the_checks_refusal_is_invalid_email_and_queues_nothing(self):
        """The service keeps no copy of the address rule: the door's CHECK
        refuses, under the savepoint, and the caller's transaction goes on."""
        conn = _Conn(_check_refusal())
        assert await _admit(conn, "two@at@example.com") == "invalid_email"
        assert [s for s, _ in conn.statements] == ["SELECT fn_signup_admit(:email)"]
        assert conn.savepoints == [0]

    async def test_any_other_database_error_propagates(self):
        conn = _Conn(_check_refusal("some_other_constraint"))
        with pytest.raises(DBAPIError):
            await _admit(conn)

    async def test_every_answer_is_a_closed_outcome(self):
        answers = set()
        for door in ("admitted", "admitted_before", "signed_up"):
            for resend in (False, True):
                answers.add(await _admit(_Conn(_Result(door)), resend=resend))
        answers.add(await _admit(_Conn(), 7))
        answers.add(await _admit(_Conn(), claim_base=None))
        assert answers == set(vocabulary.ADMIT_OUTCOMES)

    async def test_the_log_names_the_outcome_and_the_job_never_the_address(
        self, caplog
    ):
        caplog.set_level(logging.DEBUG, logger=signup_admissions.__name__)
        await _admit(_Conn(_Result("admitted"), job_id="job-9"))
        await _admit(_Conn(_check_refusal()))
        assert "admission: admitted (job job-9)" in caplog.text
        assert "invalid_email" in caplog.text
        assert ADDRESS not in caplog.text and REF not in caplog.text


class TestStoreLink:
    async def test_it_stores_the_tokens_hash_through_the_door(self):
        conn = _Conn(_Result(True))
        token = sessions.new_token()

        assert await signup_admissions.store_link(conn, to=ADDRESS, token=token) is True

        ((sql, params),) = conn.statements
        assert sql == "SELECT fn_signup_link(:to, :h)"
        assert params == {"to": ADDRESS, "h": sessions.token_hash(token)}
        assert token not in str(params)

    async def test_a_spot_taken_meanwhile_answers_false(self):
        conn = _Conn(_Result(False))
        assert await signup_admissions.store_link(conn, to=ADDRESS, token="t") is False


class TestClaim:
    async def test_a_token_is_spent_through_the_door_by_its_hash(self, caplog):
        caplog.set_level(logging.DEBUG, logger=signup_admissions.__name__)
        conn = _Conn(_Result(True))
        token = sessions.new_token()

        assert await signup_admissions.claim(conn, token=token) is True

        ((sql, params),) = conn.statements
        assert sql == "SELECT fn_signup_claim(:h)"
        assert params == {"h": sessions.token_hash(token)}
        assert token not in caplog.text
        assert sessions.token_hash(token) not in caplog.text

    async def test_a_refused_claim_answers_false(self):
        assert (
            await signup_admissions.claim(_Conn(_Result(False)), token="t0k") is False
        )

    @pytest.mark.parametrize(
        "token",
        ["x" * 257, "", "has space", "slash/ed", "pad=ded", "dot.ted", None, 12],
    )
    async def test_a_malformed_token_runs_no_statement(self, token):
        conn = _Conn()
        assert await signup_admissions.claim(conn, token=token) is False
        assert conn.statements == []

    async def test_the_longest_token_still_asks_the_door(self):
        conn = _Conn(_Result(False))
        await signup_admissions.claim(conn, token="x" * 256)
        assert len(conn.statements) == 1


def _listed(**over):
    row = {
        "email": ADDRESS,
        "joined_at": "2026-10-01",
        "admitted_at": None,
        "emailed_at": None,
        "signed_up": False,
    }
    row.update(over)
    return row


class TestWaitlist:
    async def test_it_reads_the_door_at_the_list_limit(self):
        conn = _Conn(_Result(rows=[]))
        assert await signup_admissions.waitlist(conn) == []
        ((sql, params),) = conn.statements
        assert "FROM fn_waitlist(CAST(:lim AS integer), CAST(:ref AS text))" in sql
        assert params == {"lim": signup_admissions.LIST_LIMIT, "ref": None}
        assert signup_admissions.LIST_LIMIT == 200

    async def test_a_ref_reaches_the_door(self):
        conn = _Conn(_Result(rows=[]))
        await signup_admissions.waitlist(conn, ref=REF)
        assert conn.statements[0][1] == {"lim": 200, "ref": REF}

    async def test_each_row_says_where_it_stands(self):
        conn = _Conn(
            _Result(
                rows=[
                    _listed(email="w@example.com"),
                    _listed(email="a@example.com", admitted_at="t1"),
                    _listed(email="e@example.com", admitted_at="t1", emailed_at="t2"),
                    _listed(email="s@example.com", admitted_at="t1", signed_up=True),
                    _listed(email="off@example.com", joined_at=None, admitted_at="t3"),
                ]
            )
        )
        rows = await signup_admissions.waitlist(conn)
        assert [(r["email"], r["state"], r["on_waitlist"]) for r in rows] == [
            ("w@example.com", "waiting", True),
            ("a@example.com", "admitted", True),
            ("e@example.com", "emailed", True),
            ("s@example.com", "signed_up", True),
            ("off@example.com", "admitted", False),
        ]
        assert set(rows[0]) == {
            "email",
            "on_waitlist",
            "joined_at",
            "state",
            "admitted_at",
            "emailed_at",
        }
