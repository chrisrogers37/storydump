"""X.3 — the `send_email` executor against a real database (#1092).

What lives here rather than in the unit tier: the budget debit and the
over-budget deferral. Neither is provable without the `rate_counters` row lock
and a real `jobs` row, and the deferral in particular is a claim about a row's
state, attempts and `run_at` after the executor returns.

The provider is faked; the database is not. That is the opposite split from
`tests/src/services/target/test_email_sender.py`, and deliberate: there the
egress floor is the thing under test, here the transaction boundaries are.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone

import psycopg2
import pytest

from src.services.target.email_sender import (
    BUDGET_LIMIT,
    BUDGET_WINDOW_SECONDS,
    execute_send_email,
)
from src.services.target.rate_counters import window_start
from tests.scripts.conftest import (
    _scratch,
    as_user,
    ingress_engine,
    replay_advertised_stream,
    seed_workspace_chain,
    set_test_passwords,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]


PAYLOAD = {
    "v": 1,
    "to": "invitee@example.com",
    "template": "invitation",
    "params": {"workspace_name": "Acme", "accept_url": "https://x/join/tok"},
}


class _Sender:
    """Records what it was asked to send. Never reaches a network."""

    def __init__(self, ref="prov-1"):
        self.ref = ref
        self.sent = []

    async def send(self, *, to, subject, body):
        self.sent.append({"to": to, "subject": subject, "body": body})
        return self.ref


@pytest.fixture(scope="module")
def email_db(admin_conn, owner_actor):
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        dsn = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        conn = psycopg2.connect(dsn)
        try:
            chain = seed_workspace_chain(conn, "email-gate")
        finally:
            conn.close()
        yield {"owner_stream": dsn, "ws": chain["ws"]}
    finally:
        gen.close()


def _owner(email_db, sql, params=None, fetch=False):
    conn = psycopg2.connect(email_db["owner_stream"])
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if fetch else cur.rowcount
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _hermetic(email_db):
    """Each test starts with no jobs and no email counter: the budget is a
    DEPLOYMENT-wide bucket on one key, so a leftover row from an earlier test
    is a leftover for every later one."""
    # ONE statement, one connection: `_owner` opens and closes a connection per
    # call, and two DELETEs per test is six needless cycles across this module.
    _owner(
        email_db,
        "WITH j AS (DELETE FROM jobs)"
        " DELETE FROM rate_counters WHERE scope = 'email_global'",
    )


def _seed_leased_job(email_db, *, attempts=0):
    """One LEASED `send_email` row with a known token, inserted as the owner.

    `workspace_id` is NULL because `send_email` is a system kind — `02` §5's
    classing rule, enforced by the `ck_jobs_system_kinds` biconditional.
    """
    token = str(uuid.uuid4())
    rows = _owner(
        email_db,
        "INSERT INTO jobs (workspace_id, kind, lane, serialization_key, run_at,"
        "                  payload, max_attempts, attempts, state, locked_by,"
        "                  lease_token, locked_until)"
        " VALUES (NULL, 'send_email', 'interactive', %s, now(), %s, 3,"
        "         %s, 'leased', 'w', %s, now() + interval '60 seconds')"
        " RETURNING id",
        (f"email:{uuid.uuid4()}", json.dumps(PAYLOAD), attempts, token),
        fetch=True,
    )
    return {
        "id": rows[0][0],
        "lease_token": token,
        "attempts": attempts,
        "payload": PAYLOAD,
    }


def _budget_window():
    """The window the CODE will address — never a second derivation of it.

    `date_trunc('day', now())` was the obvious spelling and it is wrong: it
    floors in the DATABASE SESSION's timezone, while `window_start` floors epoch
    seconds and is therefore UTC unconditionally. The two coincide only on a UTC
    server, so on any other host the seed below landed in a row the code never
    read, the ceiling was never exercised, and the file was permanently red
    (#1186). Calling the shipped function is what removes the assumption; pinning
    the session timezone would only document it.
    """
    return window_start(datetime.now(timezone.utc), BUDGET_WINDOW_SECONDS)


def _seed_budget(email_db, count):
    """Put the counter at *count* in the window the code will look in."""
    _owner(
        email_db,
        "INSERT INTO rate_counters (scope, key, window_start, count)"
        " VALUES ('email_global', '', %s, %s)",
        (_budget_window(), count),
    )


def _counter(email_db):
    """The count in THIS window, never whichever row Postgres returns first.

    The old form selected on `scope`/`key` alone with no `ORDER BY`, so with two
    window rows present the answer was arbitrary — nondeterministic across a
    window boundary even on a UTC host (#1187). Production is not exposed to
    this: `rate_counters` holds exactly one statement, an
    `INSERT ... ON CONFLICT (scope, key, window_start)`, so every real read is
    addressed by full key and there is no unfiltered SELECT anywhere in the
    tier. This was only ever a test-helper defect, but an assertion that reads
    an arbitrary row cannot fail honestly.
    """
    rows = _owner(
        email_db,
        "SELECT count FROM rate_counters"
        " WHERE scope = 'email_global' AND key = '' AND window_start = %s",
        (_budget_window(),),
        fetch=True,
    )
    return rows[0][0] if rows else None


def _run(email_db, job, sender, now=None):
    async def drive():
        async with ingress_engine(
            as_user(email_db["owner_stream"], "svc_worker")
        ) as engine:
            kwargs = {"sender": sender, "engine": engine}
            if now is not None:
                kwargs["now"] = now
            return await execute_send_email(job, **kwargs)

    return asyncio.run(drive())


def _run_many(email_db, jobs_, sender):
    """Several jobs through ONE loop and one engine."""

    async def drive():
        async with ingress_engine(
            as_user(email_db["owner_stream"], "svc_worker")
        ) as engine:
            for job in jobs_:
                await execute_send_email(job, sender=sender, engine=engine)

    return asyncio.run(drive())


class TestTheSendPath:
    def test_a_send_debits_the_budget_and_returns_the_provider_ref(self, email_db):
        job = _seed_leased_job(email_db)
        sender = _Sender()

        ref = _run(email_db, job, sender)

        assert ref == "prov-1"
        assert _counter(email_db) == 1
        assert sender.sent[0]["to"] == "invitee@example.com"
        assert "https://x/join/tok" in sender.sent[0]["body"]

    def test_the_job_row_is_left_leased_for_the_loop_to_finalize(self, email_db):
        """The executor does not finalize. The loop does, in its own session —
        so a successful send must leave the row exactly as it found it."""
        job = _seed_leased_job(email_db)
        _run(email_db, job, _Sender())

        state, token = _owner(
            email_db,
            "SELECT state, lease_token::text FROM jobs WHERE id = %s",
            (job["id"],),
            fetch=True,
        )[0]
        assert state == "leased"
        assert token == job["lease_token"]


class TestTheBudget:
    def test_over_budget_defers_instead_of_sending(self, email_db):
        """`05`: over budget the job DEFERS on its retry schedule. Nothing is
        sent, and the deferral restores the attempt the claim consumed —
        a daily ceiling would otherwise exhaust three rungs measured in
        minutes and drop the mail entirely."""
        job = _seed_leased_job(email_db, attempts=1)
        _seed_budget(email_db, BUDGET_LIMIT)
        sender = _Sender()

        result = _run(email_db, job, sender)

        assert result is None
        assert sender.sent == []
        state, attempts, future = _owner(
            email_db,
            "SELECT state, attempts, run_at > now() FROM jobs WHERE id = %s",
            (job["id"],),
            fetch=True,
        )[0]
        assert state == "ready"
        assert future is True
        assert attempts == 0, "the deferral did not restore the attempt"

    def test_a_refused_hit_costs_no_budget(self, email_db):
        """`rate_counters.increment`'s `WHERE rc.count < :limit` — a job that
        defers has not spent anything, so the ceiling is not walked past by
        jobs that never sent."""
        job = _seed_leased_job(email_db)
        _seed_budget(email_db, BUDGET_LIMIT)

        _run(email_db, job, _Sender())

        assert _counter(email_db) == BUDGET_LIMIT

    def test_the_budget_is_one_bucket_for_the_deployment(self, email_db):
        """Key `''`, not per tenant: the ceiling protects a provider account,
        which every workspace shares."""
        # One loop and one engine for three sends: the claim is about the KEY,
        # and three `asyncio.run`s would prove it three times over at the cost
        # of three engines.
        jobs_ = [_seed_leased_job(email_db) for _ in range(3)]
        _run_many(email_db, jobs_, _Sender())

        rows = _owner(
            email_db,
            "SELECT key, count FROM rate_counters WHERE scope = 'email_global'",
            fetch=True,
        )
        assert rows == [("", 3)]


# The `ck_jobs_system_kinds` biconditional is NOT re-proven here. `test_jobs_lease
# _gate.py` proves it in both directions AND with the guard dropped, and its
# `PAIRING_CHECK_SQL` names `send_email` explicitly — a second copy would mean a
# change to that guard breaks two suites while the second failure says nothing
# the first did not.
