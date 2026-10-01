"""090 against PostgreSQL (#1482): the outbox's failure record and its doors.

What only the database can say, each against the replayed schema as the
production roles:

* the record is written by the real `settle` as `svc_worker`, in the one CAS
  that leaves `sending`: the class and the provider's code bind under asyncpg
  (a NULL code included), and a fenced settle writes nothing;
* a resolution that fails the row moves the time and keeps the class, and a
  stranded row is recorded `ambiguous`;
* the CHECK refuses a class outside the list;
* the doors count EVERY workspace's rows as `svc_ingress`, whose own read of
  the table sees none of them (the vacuity guard), with the window clamped to
  [60 s, 24 h] and only `failed` and `ambiguous` rows alerting;
* EXECUTE is the two runtime roles' and not PUBLIC's.

Every door test seeds rows under its own provider codes (9xxx), so it reads
its own rows by (class, code) whatever the rest of the module left behind.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import psycopg2
import pytest
from psycopg2 import errors as pg_errors
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.services.target import delivery_health, outbox
from tests.scripts.conftest import (
    _scratch,
    as_user,
    async_url,
    replay_advertised_stream,
    seed_workspace_chain,
    set_test_passwords,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def world(admin_conn, owner_actor):
    """The replayed schema, two workspaces and a binding in each."""
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        dsn = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        conn = psycopg2.connect(dsn)
        try:
            chains = {
                key: seed_workspace_chain(conn, f"delivery-{key}") for key in "ab"
            }
            conn.autocommit = True
            bindings = {}
            with conn.cursor() as cur:
                cur.execute("SET app.actor_kind = 'migration'")
                for key, chain in chains.items():
                    cur.execute(
                        "INSERT INTO channel_bindings"
                        " (workspace_id, channel, external_ref)"
                        " VALUES (%s, 'telegram_group', %s) RETURNING id",
                        (chain["ws"], f"tg-{uuid.uuid4().hex[:8]}"),
                    )
                    bindings[key] = str(cur.fetchone()[0])
        finally:
            conn.close()
        yield {
            "owner": dsn,
            "worker": as_user(db, "svc_worker"),
            "ingress": as_user(db, "svc_ingress"),
            "ws": {key: str(chain["ws"]) for key, chain in chains.items()},
            "binding": bindings,
        }
    finally:
        gen.close()


def _owner(world, sql, params=None, fetch=False):
    """One statement as the schema owner, as the migration actor."""
    conn = psycopg2.connect(world["owner"])
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            cur.execute(sql, params)
            return cur.fetchall() if fetch else cur.rowcount
    finally:
        conn.close()


def _row(
    world,
    ws="a",
    *,
    state,
    attempts=0,
    failure=None,
    failed_ago_s=None,
    updated_ago_s=0,
):
    """One outbox row as the owner, with an optional failure record."""
    failure_class, code = failure or (None, None)
    return str(
        _owner(
            world,
            "INSERT INTO channel_outbox (workspace_id, binding_id, kind, payload,"
            " state, attempts, last_failure_class, last_error_code, last_failed_at,"
            " updated_at)"
            " VALUES (%s, %s, 'notification', %s, %s, %s, %s, %s,"
            " CASE WHEN %s::int IS NULL THEN NULL"
            "      ELSE now() - make_interval(secs => %s::int) END,"
            " now() - make_interval(secs => %s::int))"
            " RETURNING id",
            (
                world["ws"][ws],
                world["binding"][ws],
                json.dumps({"v": 1, "text": "hi"}),
                state,
                attempts,
                failure_class,
                code,
                failed_ago_s,
                failed_ago_s,
                updated_ago_s,
            ),
            fetch=True,
        )[0][0]
    )


def _record(world, outbox_id):
    """(state, attempts, class, code, seconds since the failure or None)."""
    return _owner(
        world,
        "SELECT state, attempts, last_failure_class, last_error_code,"
        " EXTRACT(EPOCH FROM now() - last_failed_at)::int"
        " FROM channel_outbox WHERE id = %s",
        (outbox_id,),
        fetch=True,
    )[0]


async def _as_worker(world, ws, work):
    """*work(conn)* as `svc_worker`, under the tenant's GUCs, committed."""
    engine = create_async_engine(async_url(world["worker"]))
    try:
        async with engine.connect() as conn:
            await conn.execute(
                text("SELECT set_config('app.tenant_id', :v, false)"),
                {"v": world["ws"][ws]},
            )
            await conn.execute(
                text("SELECT set_config('app.actor_kind', 'system', false)")
            )
            result = await work(conn)
            await conn.commit()
            return result
    finally:
        await engine.dispose()


def _settle(world, outbox_id, *, ws="a", attempts=1, **kwargs):
    row = {
        "id": outbox_id,
        "binding_id": world["binding"][ws],
        "kind": "notification",
        "attempts": attempts,
    }
    return _run(_as_worker(world, ws, lambda c: outbox.settle(c, row, **kwargs)))


async def _doors(dsn, window):
    """Both doors as one login: {(class, code): (rows, alerting)}, and sent."""
    engine = create_async_engine(async_url(dsn))
    try:
        async with engine.connect() as c:
            rows = (
                await c.execute(
                    text(
                        "SELECT o_failure_class, o_error_code, o_rows, o_alerting_rows"
                        " FROM fn_health_outbox_failures(CAST(:w AS integer))"
                    ),
                    {"w": window},
                )
            ).all()
            sent = (
                await c.execute(
                    text("SELECT fn_health_outbox_sent(CAST(:w AS integer))"),
                    {"w": window},
                )
            ).scalar_one()
            return {(r[0], r[1]): (r[2], r[3]) for r in rows}, sent
    finally:
        await engine.dispose()


async def _service(dsn):
    engine = create_async_engine(async_url(dsn))
    try:
        async with engine.connect() as c:
            return await delivery_health.outbox_failures(c)
    finally:
        await engine.dispose()


class TestTheRecordIsWrittenInTheCAS:
    def test_a_dead_token_is_recorded_and_the_row_stays_ambiguous(self, world):
        oid = _row(world, state="sending", attempts=1)
        result = _settle(world, oid, error=outbox.CredentialDead("401", code=401))
        assert result["state"] == "ambiguous"
        state, _, cls, code, age = _record(world, oid)
        assert (state, cls, code) == ("ambiguous", "credential_dead", 401)
        assert age is not None and age < 60

    def test_a_429_is_recorded_and_the_attempt_given_back(self, world):
        oid = _row(world, state="sending", attempts=2)
        _settle(world, oid, error=outbox.ChannelPaced("429", retry_after_s=3))
        state, attempts, cls, code, age = _record(world, oid)
        assert (state, attempts, cls, code) == ("pending", 1, "rate_limited", 429)
        assert age is not None and age < 60

    def test_a_lost_answer_with_no_code_binds_a_null(self, world):
        """The parameter asyncpg must type from the column, with no value."""
        oid = _row(world, state="sending", attempts=1)
        _settle(world, oid, error=RuntimeError("timeout"))
        state, _, cls, code, age = _record(world, oid)
        assert (state, cls, code) == ("ambiguous", "ambiguous", None)
        assert age is not None

    def test_a_gone_destination_and_a_refusal_fail_the_row_with_their_codes(
        self, world
    ):
        gone = _row(world, state="sending", attempts=1)
        _settle(world, gone, error=outbox.DestinationGone("kicked", code=403))
        refused = _row(world, state="sending", attempts=1)
        _settle(world, refused, error=outbox.ChannelRefused("too long", code=400))
        assert _record(world, gone)[:4] == ("failed", 1, "destination_gone", 403)
        assert _record(world, refused)[:4] == ("failed", 1, "refused", 400)

    def test_a_fenced_settle_records_nothing(self, world):
        """The row left `sending` under us: no state change and no record."""
        oid = _row(world, state="superseded", attempts=1)
        with pytest.raises(outbox.OutboxFenced):
            _settle(world, oid, error=outbox.CredentialDead("401", code=401))
        assert _record(world, oid) == ("superseded", 1, None, None, None)

    def test_a_success_keeps_the_last_failure(self, world):
        oid = _row(
            world,
            state="sending",
            attempts=1,
            failure=("rate_limited", 429),
            failed_ago_s=600,
        )
        _settle(world, oid, receipt="4242")
        state, _, cls, code, age = _record(world, oid)
        assert (state, cls, code) == ("sent", "rate_limited", 429)
        assert age >= 600, "a later success does not move the failure's time"


class TestTheOtherWriters:
    def test_a_resolution_that_fails_the_row_moves_the_time_and_keeps_the_class(
        self, world
    ):
        oid = _row(
            world,
            state="ambiguous",
            attempts=2,
            failure=("credential_dead", 401),
            failed_ago_s=3000,
        )
        got = _run(
            _as_worker(world, "a", lambda c: outbox.resolve_ambiguous(c, outbox_id=oid))
        )
        assert got == "failed"
        state, _, cls, code, age = _record(world, oid)
        assert (state, cls, code) == ("failed", "credential_dead", 401)
        assert age < 60, "counted when it FAILED, not when it went ambiguous"

    def test_a_row_from_before_the_columns_fails_as_ambiguous(self, world):
        oid = _row(world, state="ambiguous", attempts=2)
        _run(
            _as_worker(world, "a", lambda c: outbox.resolve_ambiguous(c, outbox_id=oid))
        )
        state, _, cls, code, age = _record(world, oid)
        assert (state, cls, code) == ("failed", "ambiguous", None)
        assert age is not None

    def test_a_resolution_that_resends_records_nothing_new(self, world):
        oid = _row(
            world,
            state="ambiguous",
            attempts=1,
            failure=("ambiguous", 502),
            failed_ago_s=600,
        )
        got = _run(
            _as_worker(world, "a", lambda c: outbox.resolve_ambiguous(c, outbox_id=oid))
        )
        assert got == "pending"
        state, _, cls, code, age = _record(world, oid)
        assert (state, cls, code) == ("pending", "ambiguous", 502) and age >= 600

    def test_a_stranded_row_is_recorded_ambiguous_with_no_code(self, world):
        binding = world["binding"]["b"]
        oid = _row(world, "b", state="sending", attempts=1)
        stranded = _run(
            _as_worker(
                world, "b", lambda c: outbox.recover_stranded(c, binding_id=binding)
            )
        )
        assert oid in stranded
        state, _, cls, code, age = _record(world, oid)
        assert (state, cls, code) == ("ambiguous", "ambiguous", None)
        assert age is not None and age < 60


class TestTheCheck:
    def test_a_class_outside_the_list_is_refused(self, world):
        oid = _row(world, state="failed")
        with pytest.raises(pg_errors.CheckViolation, match="ck_outbox_failure_class"):
            _owner(
                world,
                "UPDATE channel_outbox SET last_failure_class = 'timeout' WHERE id = %s",
                (oid,),
            )


class TestTheDoors:
    def test_the_ingress_login_cannot_read_the_table_itself(self, world):
        """The vacuity guard: if `svc_ingress` could read the rows directly,
        the doors would prove nothing about reach."""
        _row(world, state="failed", failure=("refused", 9000), failed_ago_s=60)

        async def plain(dsn):
            engine = create_async_engine(async_url(dsn))
            try:
                async with engine.connect() as c:
                    return (
                        await c.execute(text("SELECT count(*) FROM channel_outbox"))
                    ).scalar_one()
            finally:
                await engine.dispose()

        assert _run(plain(world["ingress"])) == 0
        assert _run(plain(world["owner"])) > 0

    def test_failures_are_counted_across_workspaces_by_class_and_code(self, world):
        for ws in "ab":
            _row(
                world,
                ws,
                state="failed",
                failure=("destination_gone", 9001),
                failed_ago_s=600,
            )
        _row(world, state="pending", failure=("rate_limited", 9002), failed_ago_s=300)
        _row(
            world,
            "b",
            state="ambiguous",
            failure=("credential_dead", 9003),
            failed_ago_s=60,
        )
        _row(world, state="sent", failure=("rate_limited", 9004), failed_ago_s=900)
        _row(world, state="failed", failure=("refused", 9005), failed_ago_s=7200)
        _row(world, state="failed")  # no failure recorded on it

        got, _ = _run(_doors(world["ingress"], 3600))
        assert got[("destination_gone", 9001)] == (2, 2), (
            "both workspaces, both alerting"
        )
        assert got[("rate_limited", 9002)] == (1, 0), "a deferral is context"
        assert got[("credential_dead", 9003)] == (1, 1)
        assert got[("rate_limited", 9004)] == (1, 0), "sent in the end: context"
        assert ("refused", 9005) not in got, "two hours ago is outside the hour"
        assert (None, None) not in got, (
            "a row with no failure recorded is never counted"
        )

    def test_the_window_is_clamped_to_a_minute_and_a_day(self, world):
        _row(world, state="failed", failure=("refused", 9011), failed_ago_s=15)
        _row(world, state="failed", failure=("refused", 9012), failed_ago_s=100)
        _row(world, state="failed", failure=("refused", 9013), failed_ago_s=23 * 3600)
        _row(world, state="failed", failure=("refused", 9014), failed_ago_s=48 * 3600)

        def codes(window):
            got, _ = _run(_doors(world["ingress"], window))
            return {code for (_, code) in got if 9011 <= (code or 0) <= 9014}

        for small in (-100, 0, 1):
            assert codes(small) == {9011}, f"window {small} reads as one minute"
        assert codes(3600) == {9011, 9012}
        assert codes(10**7) == {9011, 9012, 9013}, "a day at most"

    def test_the_sent_door_counts_the_window(self, world):
        _, before_hour = _run(_doors(world["ingress"], 3600))
        _, before_day = _run(_doors(world["ingress"], 86400))
        _row(world, "a", state="sent", updated_ago_s=60)
        _row(world, "b", state="sent", updated_ago_s=60)
        _row(world, "a", state="sent", updated_ago_s=7200)
        _row(world, "a", state="failed", updated_ago_s=60)
        _, after_hour = _run(_doors(world["ingress"], 3600))
        _, after_day = _run(_doors(world["ingress"], 86400))
        assert after_hour - before_hour == 2
        assert after_day - before_day == 3

    def test_the_service_reads_the_doors_as_svc_ingress(self, world):
        """The route's own path: the window binds as an integer under asyncpg,
        and codes come back as string keys."""
        _row(
            world,
            "b",
            state="failed",
            failure=("destination_gone", 9021),
            failed_ago_s=120,
        )
        _row(
            world, "a", state="ambiguous", failure=("ambiguous", None), failed_ago_s=120
        )
        got = _run(_service(world["ingress"]))
        assert got["window_seconds"] == 3600
        assert got["by_class"]["destination_gone"]["codes"]["9021"] == 1
        assert got["by_class"]["ambiguous"]["codes"]["none"] >= 1
        assert got["failed_or_ambiguous"] == sum(
            entry["alerting"] for entry in got["by_class"].values()
        )

    def test_the_worker_login_can_read_them_too(self, world):
        got, sent = _run(_doors(world["worker"], 3600))
        assert got and sent >= 0

    def test_execute_is_the_runtime_roles_and_not_publics(self, world):
        rows = _owner(
            world,
            "SELECT p.proname, r.rolname, p.prosecdef,"
            "  has_function_privilege('svc_ingress', p.oid, 'EXECUTE'),"
            "  has_function_privilege('svc_worker', p.oid, 'EXECUTE'),"
            "  has_function_privilege('public', p.oid, 'EXECUTE')"
            " FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner"
            " WHERE p.proname IN ('fn_health_outbox_failures', 'fn_health_outbox_sent')"
            " ORDER BY p.proname",
            fetch=True,
        )
        assert rows == [
            ("fn_health_outbox_failures", "svc_maintenance", True, True, True, False),
            ("fn_health_outbox_sent", "svc_maintenance", True, True, True, False),
        ]
