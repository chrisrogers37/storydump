"""#1740: an admission email the provider took, then a fault storing its link.

`email_sender.execute_send_email` sends an admission's email first and stores
its link second (#1723): it draws the link's token as it sends, and stores the
token's hash, in a transaction of its own, only once the provider has taken
the email. This gate holds that order against the real database at the two
places the store can fail after the send:

- **the store raises** after `fn_signup_link` has written: the hash is read
  back inside the transaction, the store raises, and the real
  `system_session` rolls the write back;
- **the commit is refused**: the store writes and returns, and a deferred
  constraint trigger, created in this gate's own database and matching only
  this address and the first link's hash, refuses the commit at the
  session's end, never inside the store.

Each runs as `svc_worker` through the real registry adapter and
`WorkLoop._run_job`, on a job admitted as `svc_ingress` and claimed through
`fn_claim_job`. Before the last attempt the job is rescheduled with its
attempt kept, and its retry, a new claim, emails a fresh link whose hash
alone is stored, replacing an earlier link only then. On the last attempt the
job ends `failed`, with no retry and no notice: its email carried a link that
was never stored, and an earlier link still works. Neither raw token, nor the
failed link's hash, rests in any base table, the job's payload included.

No worker process, no clock and no provider: `_run_job` takes each claimed
row, a retry is made due by moving this gate's own job alone, and the sender
records. Both seams wrap the real code, and neither fakes a commit.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import psycopg2.sql
import pytest
from asyncpg.exceptions import RaiseError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.services.target import (
    email_sender,
    jobs,
    sessions,
    signup_admissions,
    unit_of_work,
)
from src.services.target._dbapi import driver_error_is
from src.services.target.work_loop import (
    WorkerConfig,
    WorkerDeps,
    WorkLoop,
    build_registry,
)
from tests.scripts.conftest import (
    as_user,
    execute,
    fetch_all,
    in_user_plane,
    ingress_engine,
    rows_holding,
)
from tests.src.services.target.conftest import RecordingSender

pytestmark = [pytest.mark.integration, pytest.mark.slow]

#: The base of the claim link an admission's email carries.
BASE = "https://app.example.test/claim/"
WORKER = "admission-fault-gate"

#: A read of the link a transaction has staged, for `svc_worker`, which holds
#: no grant on `signup_admissions`: the in-transaction view a store's fault
#: must roll back. Test-only, it lives and dies with this gate's database.
READ_DOOR = (
    "CREATE FUNCTION gate_staged_link(p_email text) RETURNS text"
    " LANGUAGE sql STABLE SECURITY DEFINER"
    " SET search_path = pg_catalog, public, pg_temp AS"
    " $$ SELECT claim_token_hash FROM signup_admissions"
    " WHERE email = lower(p_email) $$;"
    " REVOKE ALL ON FUNCTION gate_staged_link(text) FROM PUBLIC;"
    " GRANT EXECUTE ON FUNCTION gate_staged_link(text) TO svc_worker"
)

#: The refusal's trigger and its function, one name for both, and what it
#: raises: a neutral message that names no address, token or hash.
REFUSAL = "gate_refuses_the_first_link"
REFUSED = "gate: the commit is refused"


@pytest.fixture(scope="module")
def world(ingress_world):
    """The module's replayed database, so a claim here meets only this gate's
    jobs; with its read door (`READ_DOOR`) and a `svc_worker` login."""
    execute(ingress_world["owner"], READ_DOOR)
    return {**ingress_world, "worker": as_user(ingress_world["owner"], "svc_worker")}


@pytest.fixture(autouse=True)
def _alone(world):
    """Each case starts with no job and an unspent email budget: a claim takes
    whichever job is due, and the budget is one bucket for the deployment."""
    execute(world["owner"], "DELETE FROM jobs")
    execute(world["owner"], "DELETE FROM rate_counters WHERE scope = 'email_global'")


def _one(world, sql, params=None):
    """The one row *sql* answers on a fresh owner connection, committed."""
    (row,) = fetch_all(world["owner"], sql, params)
    return row


def _link(world, email):
    """*email*'s stored link, as a fresh owner connection reads it."""
    return _one(
        world,
        "SELECT claim_token_hash, emailed_at FROM signup_admissions WHERE email = %s",
        (email,),
    )


def _job_row(world, job_id):
    return _one(
        world,
        "SELECT state, attempts, lease_token, run_at > now() AS deferred, payload"
        " FROM jobs WHERE id = %s",
        (str(job_id),),
    )


def _queued(email):
    """The payload `admit` queues: the address and the link's base, no token."""
    return {
        "v": 1,
        "to": email,
        "template": "admission",
        "params": {"address": email, "claim_base": BASE},
    }


def _due_now(world, job_id):
    """End this job's backoff, and no other's: the retry is a new claim, never
    a sleep through the ladder."""
    _one(
        world,
        "UPDATE jobs SET run_at = now() WHERE id = %s AND state = 'ready' RETURNING id",
        (str(job_id),),
    )


def _last_attempt_next(world, email):
    """Spend all but one of *email*'s waiting job's attempts, so its next
    claim is its last."""
    row = _one(
        world,
        "UPDATE jobs SET attempts = max_attempts - 1"
        " WHERE serialization_key = %s AND state = 'ready' RETURNING max_attempts",
        (f"email:admission:{signup_admissions.ref(email)}",),
    )
    assert row["max_attempts"] == 5, "the bulk lane's budget"


def _arm_refusal(world, email, token):
    """Refuse, at its commit, a transaction that stores *token*'s hash as
    *email*'s link: a deferred constraint trigger matching that address and
    that hash alone, so every other row, and every other link, commits."""
    sql = psycopg2.sql
    execute(
        world["owner"],
        # A send inside an open store transaction would block this DDL on its
        # lock for ever: fail in seconds instead of hanging the run.
        sql.SQL(
            "SET LOCAL lock_timeout = '5s'; "
            "CREATE FUNCTION {fn}() RETURNS trigger LANGUAGE plpgsql AS"
            " $$BEGIN RAISE EXCEPTION {refused}; END$$;"
            " CREATE CONSTRAINT TRIGGER {trigger} AFTER UPDATE ON signup_admissions"
            " DEFERRABLE INITIALLY DEFERRED FOR EACH ROW"
            " WHEN (NEW.email = {email} AND NEW.claim_token_hash = {hash})"
            " EXECUTE FUNCTION {fn}()"
        ).format(
            fn=sql.Identifier(REFUSAL),
            trigger=sql.Identifier(REFUSAL),
            refused=sql.Literal(REFUSED),
            email=sql.Literal(email),
            hash=sql.Literal(sessions.token_hash(token)),
        ),
    )


def _disarm_refusal(world):
    sql = psycopg2.sql
    execute(
        world["owner"],
        sql.SQL(
            "DROP TRIGGER IF EXISTS {trigger} ON signup_admissions;"
            " DROP FUNCTION IF EXISTS {fn}()"
        ).format(trigger=sql.Identifier(REFUSAL), fn=sql.Identifier(REFUSAL)),
    )


def _refusal_armed(world):
    row = _one(
        world,
        "SELECT count(*) AS n FROM pg_trigger"
        " WHERE tgname = %s AND tgrelid = 'signup_admissions'::regclass",
        (REFUSAL,),
    )
    return row["n"] == 1


async def _admit(world, email, *, resend=False):
    return await in_user_plane(
        world["ingress"],
        lambda c: signup_admissions.admit(
            c, email=email, claim_base=BASE, resend=resend
        ),
    )


async def _spend(world, token):
    """Spend *token*'s link through the sign-in's own door, as `svc_ingress`."""
    return await in_user_plane(
        world["ingress"], lambda c: signup_admissions.claim(c, token=token)
    )


class _Provider(RecordingSender):
    """The recording sender. As it takes each email it notes whether the
    sending task is in a transaction (the flag `system_session` and
    `make_session_for` both arm), and it runs *on_next_send* once, with the
    token the email carried, as it takes the next."""

    def __init__(self):
        super().__init__()
        self.in_transaction, self.on_next_send = [], None

    async def send(self, *, to, subject, body):
        self.in_transaction.append(unit_of_work.in_transaction())
        ref = await super().send(to=to, subject=subject, body=body)
        hook, self.on_next_send = self.on_next_send, None
        if hook is not None:
            hook(self.sent[-1]["token"])
        return ref


class _Worker:
    """`svc_worker` running a job as the deployed worker does: the real
    registry over the recording provider, `make_session_for`, one bulk-lane
    `WorkLoop`, and a claim through `fn_claim_job`. No worker process and no
    clock: each claimed row goes to `_run_job`, the loop's one method for it."""

    def __init__(self, engine, provider):
        self.engine, self.config = engine, WorkerConfig()
        registry = build_registry(
            WorkerDeps(email=provider, engine=engine, config=self.config)
        )
        assert getattr(registry["send_email"], "owns_transactions", False)
        self.loop = WorkLoop(
            session_for=unit_of_work.make_session_for(engine),
            lane="bulk",
            registry=registry,
            heartbeat=None,
            config=self.config,
            worker_name=WORKER,
        )

    async def claim(self):
        async with self.engine.connect() as conn:
            who = (await conn.execute(text("SELECT current_user"))).scalar()
            assert who == "svc_worker", who
            return await jobs.claim_job(
                conn,
                lane="bulk",
                worker=WORKER,
                lease_seconds=self.config.lease_seconds,
                ws_lane_cap=self.config.ws_lane_cap_for("bulk"),
            )

    async def run_claimed(self):
        job = await self.claim()
        assert job is not None, "a due job to claim"
        await self.loop._run_job(job)
        return job

    async def lease_is_live(self, job):
        async with self.engine.connect() as conn:
            return await jobs.lease_is_live(conn, job["id"], job["lease_token"])


class _StoreFault(Exception):
    """The store's synthetic failure. Its message names no address, token or
    hash."""


#: Marks a session the store wrote in, so that its end is the one recorded.
_STORED = "gate_stored_a_link"


class _FaultAfterTheSend:
    """One fault after the provider took the email, on the next attempt.

    Both seams wrap the real code. The store calls the real `store_link`,
    reads the hash back inside its transaction (`READ_DOOR`) and, for
    `store`, then raises. For `commit`, the refusal is armed from the token
    the email carried, as the provider takes it, and the database refuses
    the commit. The real `system_session` is wrapped only to record how a
    session the store wrote in ended: `store` (its body raised, and the real
    exit rolled it back), `commit` (its exit raised) or `committed`."""

    def __init__(self, world, monkeypatch, provider, *, seam, email):
        self.world = world
        self.staged, self.ends, self.errors, self.tokens = [], [], [], []
        faults = [_StoreFault("gate: the store failed")] if seam == "store" else []
        real_store = signup_admissions.store_link
        real_session = email_sender.system_session

        async def store_link(session, *, to, token):
            self.tokens.append(token)
            stored = await real_store(session, to=to, token=token)
            staged = (
                await session.execute(
                    text("SELECT gate_staged_link(:email)"), {"email": to}
                )
            ).scalar()
            self.staged.append(stored is True and staged == sessions.token_hash(token))
            session.info[_STORED] = True
            if faults:
                raise faults.pop()
            return stored

        @asynccontextmanager
        async def system_session(engine):
            session = in_body = None
            try:
                async with real_session(engine) as session:
                    try:
                        yield session
                    except BaseException as exc:
                        in_body = exc
                        raise
            except BaseException as exc:
                if session is not None and session.info.get(_STORED):
                    self.ends.append("store" if exc is in_body else "commit")
                    self.errors.append(exc)
                raise
            if session.info.get(_STORED):
                self.ends.append("committed")

        monkeypatch.setattr(signup_admissions, "store_link", store_link)
        monkeypatch.setattr(email_sender, "system_session", system_session)
        if seam == "commit":
            provider.on_next_send = lambda token: _arm_refusal(world, email, token)

    def disarm(self):
        _disarm_refusal(self.world)


def _capture(caplog):
    """Every application log line: the executor's, the loop's, the doors'."""
    for name in ("src", "target"):
        caplog.set_level(logging.DEBUG, logger=name)


def _logged_nothing_of(caplog, email, *tokens):
    """No log line carries the address, a token or a token's hash."""
    assert "Traceback" in caplog.text, "the read covers the fault's traceback"
    for secret in (email, *tokens, *map(sessions.token_hash, tokens)):
        assert secret not in caplog.text


@pytest.mark.parametrize("seam", ["store", "commit"])
@pytest.mark.parametrize("history", ["first", "resend"])
class TestALinkThatFailsToStoreAfterTheSend:
    """The provider took the email, and then the store raised or the commit
    was refused (*seam*): for an admission's first email, or for a resend
    after an earlier link was stored (*history*)."""

    @staticmethod
    async def _admitted(world, engine, provider, email, history):
        """*email* admitted, its email's job waiting. For a resend, the first
        email went through the same loop and its link is stored; returns that
        link's token, else None."""
        assert await _admit(world, email) == "admitted"
        if history == "first":
            return None
        await _Worker(engine, provider).run_claimed()
        older = provider.sent[-1]["token"]
        assert _link(world, email)["claim_token_hash"] == sessions.token_hash(older)
        assert await _admit(world, email, resend=True) == "resent"
        return older

    async def test_the_retry_emails_a_fresh_link_and_only_its_hash_is_stored(
        self, world, monkeypatch, caplog, seam, history
    ):
        email = f"retry-{seam}-{history}@example.com"
        _capture(caplog)
        async with ingress_engine(world["worker"]) as engine:
            provider = _Provider()
            older = await self._admitted(world, engine, provider, email, history)
            before, sent_before = _link(world, email), len(provider.sent)
            worker = _Worker(engine, provider)
            fault = _FaultAfterTheSend(
                world, monkeypatch, provider, seam=seam, email=email
            )
            try:
                first = await worker.run_claimed()

                assert first["attempts"] == 1
                (mail,) = provider.sent[sent_before:]
                token_a = mail["token"]
                assert mail["to"] == email and BASE + token_a in mail["body"]
                assert fault.staged == [True], "the store wrote the link's hash"
                assert fault.tokens == [token_a], "the stored link is the emailed one"
                assert fault.ends == [seam]
                if seam == "commit":
                    (refused,) = fault.errors
                    assert isinstance(refused, DBAPIError) and REFUSED in str(refused)
                    assert driver_error_is(refused, RaiseError), "the database's"
                assert _link(world, email) == before, "rolled back: nothing changed"
                row = _job_row(world, first["id"])
                assert row["state"] == "ready" and row["lease_token"] is None
                assert row["attempts"] == 1, "rescheduled, its consumed attempt kept"
                assert row["deferred"] and row["payload"] == _queued(email)
                assert (worker.loop.failures, worker.loop.processed) == (1, 0)
                for needle in filter(None, (older, token_a)):
                    assert rows_holding(world["owner"], needle) == {}

                _due_now(world, first["id"])
                if seam == "commit":
                    assert _refusal_armed(world), "the refusal stands for the retry"
                second = await worker.run_claimed()

                assert second["id"] == first["id"] and second["attempts"] == 2
                assert second["lease_token"] != first["lease_token"], "a new claim"
                assert len(provider.sent) == sent_before + 2, (
                    "two emails: the duplicate is the cost of sending first"
                )
                token_b = provider.sent[-1]["token"]
                assert token_b != token_a, "the retry draws a fresh token"
                assert provider.sent[-1]["to"] == email
                assert BASE + token_b in provider.sent[-1]["body"]
                assert fault.staged == [True, True]
                assert fault.ends == [seam, "committed"]
                stored = _link(world, email)
                assert stored["claim_token_hash"] == sessions.token_hash(token_b)
                assert stored["emailed_at"] is not None
                row = _job_row(world, first["id"])
                assert (row["state"], row["attempts"]) == ("succeeded", 2)
                assert row["payload"] == _queued(email)
                assert not await worker.lease_is_live(first)
                assert not await worker.lease_is_live(second)
                loop = worker.loop
                assert (loop.failures, loop.processed, loop.exhausted) == (1, 1, 0)
                assert provider.in_transaction == [False] * len(provider.sent)
                failed_hash = sessions.token_hash(token_a)
                for needle in filter(None, (older, token_a, token_b, failed_hash)):
                    assert rows_holding(world["owner"], needle) == {}
            finally:
                fault.disarm()

        assert await _spend(world, token_a) is False, "the failed link never worked"
        if older is not None:
            assert await _spend(world, older) is False, "the retry's link replaced it"
        assert await _spend(world, token_b) is True
        _logged_nothing_of(caplog, email, *filter(None, (older, token_a, token_b)))

    async def test_on_the_last_attempt_the_job_fails_and_an_earlier_link_stands(
        self, world, monkeypatch, caplog, seam, history
    ):
        email = f"last-{seam}-{history}@example.com"
        _capture(caplog)
        async with ingress_engine(world["worker"]) as engine:
            provider = _Provider()
            older = await self._admitted(world, engine, provider, email, history)
            before, sent_before = _link(world, email), len(provider.sent)
            if older is None:
                assert before == {"claim_token_hash": None, "emailed_at": None}
            _last_attempt_next(world, email)
            worker = _Worker(engine, provider)
            fault = _FaultAfterTheSend(
                world, monkeypatch, provider, seam=seam, email=email
            )
            try:
                last = await worker.run_claimed()

                assert last["attempts"] == last["max_attempts"] == 5
                (mail,) = provider.sent[sent_before:]
                token_a = mail["token"]
                assert mail["to"] == email and BASE + token_a in mail["body"]
                assert fault.staged == [True] and fault.ends == [seam]
                assert _link(world, email) == before, (
                    "a first email leaves no link; a resend keeps the earlier one"
                )
                row = _job_row(world, last["id"])
                assert (row["state"], row["attempts"]) == ("failed", 5)
                assert not row["deferred"] and row["payload"] == _queued(email)
                assert not await worker.lease_is_live(last)
                assert await worker.claim() is None, "nothing is left to retry"
                loop = worker.loop
                assert (loop.failures, loop.processed, loop.exhausted) == (1, 0, 1)
                notices = _one(world, "SELECT count(*) AS n FROM channel_outbox")
                assert notices["n"] == 0, "a system job tells no one"
                errors = [
                    r.getMessage()
                    for r in caplog.records
                    if r.name == "target.work_loop" and r.levelno >= logging.ERROR
                ]
                assert len(errors) == 1, f"the failure alone, no notice tried: {errors}"
                assert provider.in_transaction == [False] * len(provider.sent)
                failed_hash = sessions.token_hash(token_a)
                for needle in filter(None, (older, token_a, failed_hash)):
                    assert rows_holding(world["owner"], needle) == {}
            finally:
                fault.disarm()

        assert await _spend(world, token_a) is False, "the last email's link is dead"
        if older is not None:
            assert await _spend(world, older) is True, "the earlier link still works"
        _logged_nothing_of(caplog, email, *filter(None, (older, token_a)))
