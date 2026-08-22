"""W5d+W5e gate — the credential lifecycle against the real machinery (#942).

Two pins pulled: without the refresh executor every credential silently
reaches `mark_dead` at ~60d (`02` D31), and without arming at store a
reconnect-stored credential is invisible to the tick forever. This gate
proves the whole loop on the replayed schema: the tick mints, the executor
refreshes through the seam, a definitive rejection flips BOTH state rows in
one transaction and mints the immediate prompt, the prompt executor lands a
notification on the workspace's binding, and the weekly cadence dedups.

Controls: every negative here has a positive sibling in the same class — a
leg that minted nothing must sit beside the same leg minting, or the zero
proves only that the test ran.
"""

import psycopg2
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.scripts.conftest import seed_workspace_chain
from tests.scripts.test_lineage_lane import run_lane

pytestmark = [pytest.mark.integration]


def _async_url(dsn: str) -> str:
    return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture()
def lane_db(bootstrapped_db):
    run_lane(bootstrapped_db)
    return bootstrapped_db


@pytest.fixture()
def sync_conn(lane_db):
    conn = psycopg2.connect(lane_db)
    yield conn
    conn.close()


def _tick(conn, max_jobs: int = 50) -> None:
    """Run the clock door exactly as the scheduler does.

    Commits first: psycopg2 opens an implicit transaction at the first
    statement after a commit, and `now()` is transaction-start time — a tick
    running inside a transaction that predates the rows it should see reads
    them as not-yet-due (measured: the arming test's insert carried a
    next_refresh_at 106ms AFTER the stale transaction's now())."""
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SET app.actor_kind = 'migration'")
        cur.execute(
            "SELECT fn_clock_tick(%s, %s::interval, %s::jsonb)",
            (max_jobs, "7 days", "{}"),
        )
    conn.commit()


def _account_of(chain: dict, conn) -> tuple[str, str]:
    """(ig_account_id, provider_account_ref) of the seeded chain."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT provider_account_ref FROM ig_accounts WHERE id = %s",
            (chain["iga"],),
        )
        ref = cur.fetchone()[0]
    return str(chain["iga"]), ref


def _set_account_state(conn, account_id: str, state: str) -> None:
    with conn.cursor() as cur:
        cur.execute("SET app.actor_kind = 'migration'")
        cur.execute(
            "UPDATE ig_accounts SET state = %s WHERE id = %s", (state, account_id)
        )
    conn.commit()


def _jobs_of_kind(conn, kind: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, payload, serialization_key, state FROM jobs"
            " WHERE kind = %s ORDER BY created_at",
            (kind,),
        )
        return [
            {
                "id": r[0],
                "payload": r[1],
                "serialization_key": r[2],
                "state": r[3],
            }
            for r in cur.fetchall()
        ]


class TestTheClockMintsTheReauthLeg:
    """Migration 062: fn_clock_tick gains the `05` reauth-prompt cadence leg."""

    def test_a_reauth_required_account_is_prompted_and_an_active_one_is_not(
        self, lane_db, sync_conn
    ):
        chain = seed_workspace_chain(sync_conn, "w5de-reauth-mint")
        account_id, ref = _account_of(chain, sync_conn)

        # Negative half FIRST, from the same world: active account, no mint.
        _tick(sync_conn)
        assert _jobs_of_kind(sync_conn, "reauth_prompt") == [], (
            "an active account must never be prompted"
        )

        # Positive half: flip to reauth_required, the same leg mints.
        _set_account_state(sync_conn, account_id, "reauth_required")
        _tick(sync_conn)
        jobs = _jobs_of_kind(sync_conn, "reauth_prompt")
        assert len(jobs) == 1, "the reauth leg must mint for a reauth_required account"
        assert jobs[0]["payload"]["v"] == 1
        assert jobs[0]["payload"]["ig_account_id"] == account_id
        assert jobs[0]["serialization_key"] == f"ig:{ref}"

        # The marker is stamped, so the mint is once-per-cadence.
        with sync_conn.cursor() as cur:
            cur.execute(
                "SELECT last_reauth_prompt_at FROM ig_accounts WHERE id = %s",
                (account_id,),
            )
            assert cur.fetchone()[0] is not None

    def test_the_weekly_cadence_dedups_and_then_reprompts(self, lane_db, sync_conn):
        chain = seed_workspace_chain(sync_conn, "w5de-reauth-cadence")
        account_id, _ = _account_of(chain, sync_conn)
        _set_account_state(sync_conn, account_id, "reauth_required")

        _tick(sync_conn)
        assert len(_jobs_of_kind(sync_conn, "reauth_prompt")) == 1

        # Same week: no second mint.
        _tick(sync_conn)
        assert len(_jobs_of_kind(sync_conn, "reauth_prompt")) == 1, (
            "within the cadence window the leg must not re-mint"
        )

        # Push the marker past the window AND complete the first job: the
        # still-open-job guard is load-bearing (a slow executor must not pile
        # up prompts), so the re-mint precondition is "executed AND a week
        # passed". The SAME leg minting again is the dedup's positive
        # control, proving the quiet tick was the window and not a dead leg.
        with sync_conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            cur.execute(
                "UPDATE jobs SET state = 'succeeded'"
                " WHERE kind = 'reauth_prompt' AND state = 'ready'"
            )
            cur.execute(
                "UPDATE ig_accounts SET last_reauth_prompt_at = now() - interval '8 days'"
                " WHERE id = %s",
                (account_id,),
            )
        sync_conn.commit()
        _tick(sync_conn)
        assert len(_jobs_of_kind(sync_conn, "reauth_prompt")) == 2


class TestStoreArmsTheTick:
    """The second pin: a stored credential must be visible to the refresh leg."""

    @pytest.mark.asyncio
    async def test_store_credential_arms_next_refresh_at_and_the_tick_mints(
        self, lane_db, sync_conn
    ):
        from src.services.target import ig_login_oauth as oauth

        chain = seed_workspace_chain(sync_conn, "w5de-arming")
        account_id, _ = _account_of(chain, sync_conn)

        engine = create_async_engine(_async_url(lane_db))
        try:
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as session:
                async with session.begin():
                    await session.execute(
                        __import__("sqlalchemy").text(
                            "SET LOCAL app.actor_kind = 'migration'"
                        )
                    )
                    cred_id = await oauth.store_credential(
                        session,
                        workspace_id=chain["ws"],
                        ig_account_id=account_id,
                        token="tok-armed",
                    )
        finally:
            await engine.dispose()

        with sync_conn.cursor() as cur:
            cur.execute(
                "SELECT next_refresh_at FROM oauth_credentials WHERE id = %s",
                (cred_id,),
            )
            armed = cur.fetchone()[0]
        assert armed is not None, (
            "store_credential must arm next_refresh_at — an unarmed credential"
            " is invisible to the refresh leg forever"
        )

        _tick(sync_conn)
        minted = _jobs_of_kind(sync_conn, "refresh_credential")
        assert any(j["payload"].get("credential_id") == cred_id for j in minted), (
            "the tick must mint a refresh job for the newly stored credential"
        )


async def _run_once_w5(lane_db, refresh_stub):
    """One bulk-lane cycle with a scripted refresh door (w1 gate's harness)."""
    from src.services.target.work_loop import WorkerConfig
    from src.worker import compose

    engine = create_async_engine(_async_url(lane_db))
    try:
        app = compose(
            engine=engine, config=WorkerConfig(), env={}, refresh=refresh_stub
        )
        wl = next(wl_ for wl_ in app.loops if wl_.lane == "bulk")
        conn = await engine.connect()
        try:
            wl.bind_claim_conn(conn)
            claimed = await wl.run_once()
        finally:
            await conn.close()
        return wl, claimed
    finally:
        await engine.dispose()


async def _store_cred(lane_db, chain) -> str:
    from sqlalchemy import text as sqltext

    from src.services.target import ig_login_oauth as oauth

    engine = create_async_engine(_async_url(lane_db))
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            async with session.begin():
                await session.execute(sqltext("SET LOCAL app.actor_kind = 'migration'"))
                return await oauth.store_credential(
                    session,
                    workspace_id=chain["ws"],
                    ig_account_id=chain["iga"],
                    token="tok-OLD",
                )
    finally:
        await engine.dispose()


def _cred_row(conn, cred_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state, expires_at, next_refresh_at FROM oauth_credentials"
            " WHERE id = %s",
            (cred_id,),
        )
        r = cur.fetchone()
    return {"state": r[0], "expires_at": r[1], "next_refresh_at": r[2]}


class TestRefreshExecutorOnTheRealMachinery:
    @pytest.mark.asyncio
    async def test_success_swaps_in_place_and_the_job_finalizes(
        self, lane_db, sync_conn
    ):
        from datetime import datetime, timezone

        from src.services.target import ig_login_oauth as oauth

        chain = seed_workspace_chain(sync_conn, "w5d-ok")
        cred_id = await _store_cred(lane_db, chain)
        _tick(sync_conn)
        assert _jobs_of_kind(sync_conn, "refresh_credential")

        seen_tokens = []

        async def stub(token):
            seen_tokens.append(token)
            return "tok-NEW", datetime(2027, 1, 1, tzinfo=timezone.utc)

        wl, claimed = await _run_once_w5(lane_db, stub)
        assert claimed is True and wl.processed == 1
        assert seen_tokens == ["tok-OLD"], "the door must receive the stored token"

        row = _cred_row(sync_conn, cred_id)
        assert row["state"] == "active"
        assert row["expires_at"] is not None

        # The NEW token is what the ring now holds — read it back through the
        # real decrypt path, not by comparing ciphertext.
        engine = create_async_engine(_async_url(lane_db))
        try:
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as s:
                got = await oauth.load_credential(s, credential_id=cred_id)
        finally:
            await engine.dispose()
        assert got == "tok-NEW"

        jobs = _jobs_of_kind(sync_conn, "refresh_credential")
        assert {j["state"] for j in jobs} == {"succeeded"}

    @pytest.mark.asyncio
    async def test_definitive_rejection_flips_both_and_the_cadence_prompts(
        self, lane_db, sync_conn
    ):
        """The whole W5d→062→W5e arc, end to end on the real schema."""
        import uuid as uuidlib

        from src.services.target.credential_lifecycle import RefreshRejected

        chain = seed_workspace_chain(sync_conn, "w5d-dead")
        cred_id = await _store_cred(lane_db, chain)
        _tick(sync_conn)

        async def rejecting(token):
            raise RefreshRejected(401)

        wl, claimed = await _run_once_w5(lane_db, rejecting)
        assert claimed is True and wl.processed == 1

        assert _cred_row(sync_conn, cred_id)["state"] == "expired"
        with sync_conn.cursor() as cur:
            cur.execute("SELECT state FROM ig_accounts WHERE id = %s", (chain["iga"],))
            assert cur.fetchone()[0] == "reauth_required", (
                "D31: both flips must land — account side missing"
            )
        refresh_jobs = _jobs_of_kind(sync_conn, "refresh_credential")
        assert {j["state"] for j in refresh_jobs} == {"succeeded"}, (
            "a definitive rejection is handled work, not a retry"
        )

        # The 062 leg is the single prompt producer: next tick mints.
        _tick(sync_conn)
        prompts_minted = _jobs_of_kind(sync_conn, "reauth_prompt")
        assert len(prompts_minted) == 1

        # Give the workspace a surface, run the prompt executor, and the
        # notification lands on the binding.
        binding = str(uuidlib.uuid4())
        with sync_conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            cur.execute(
                "INSERT INTO channel_bindings (id, workspace_id, channel, external_ref)"
                " VALUES (%s, %s, 'telegram_group', '-100555001')",
                (binding, chain["ws"]),
            )
        sync_conn.commit()

        wl, claimed = await _run_once_w5(lane_db, rejecting)
        assert claimed is True and wl.processed == 1
        with sync_conn.cursor() as cur:
            cur.execute(
                "SELECT payload FROM channel_outbox"
                " WHERE binding_id = %s AND kind = 'notification'",
                (binding,),
            )
            rows = cur.fetchall()
        assert len(rows) == 1, "exactly one notification per binding"
        assert "re-authoriz" in rows[0][0]["text"], (
            "the prompt must say what happened and what is paused"
        )
        prompt_jobs = _jobs_of_kind(sync_conn, "reauth_prompt")
        assert {j["state"] for j in prompt_jobs} == {"succeeded"}
        _no_stranded(sync_conn)

    @pytest.mark.asyncio
    async def test_transient_failure_retries_and_never_marks_dead(
        self, lane_db, sync_conn
    ):
        from src.services.target.credential_lifecycle import RefreshTransient

        chain = seed_workspace_chain(sync_conn, "w5d-flaky")
        cred_id = await _store_cred(lane_db, chain)
        _tick(sync_conn)

        async def flaky(token):
            raise RefreshTransient(503)

        wl, claimed = await _run_once_w5(lane_db, flaky)
        assert claimed is True

        assert _cred_row(sync_conn, cred_id)["state"] == "active", (
            "D31: a transient fault must never take the liveness edge"
        )
        job = _jobs_of_kind(sync_conn, "refresh_credential")[0]
        assert job["state"] == "ready", "alive on the ladder, not dead, not done"
        _no_stranded(sync_conn)

    @pytest.mark.asyncio
    async def test_undecryptable_payload_is_handled_work_not_a_retry_loop(
        self, lane_db, sync_conn
    ):
        chain = seed_workspace_chain(sync_conn, "w5d-corrupt")
        cred_id = await _store_cred(lane_db, chain)
        _tick(sync_conn)
        with sync_conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            cur.execute(
                "UPDATE oauth_credentials SET encrypted_payload = 'not-a-token'"
                " WHERE id = %s",
                (cred_id,),
            )
        sync_conn.commit()

        async def must_not_be_called(token):
            raise AssertionError("provider must not be called for an unreadable token")

        wl, claimed = await _run_once_w5(lane_db, must_not_be_called)
        assert claimed is True and wl.processed == 1
        assert _cred_row(sync_conn, cred_id)["state"] == "expired"
        job = _jobs_of_kind(sync_conn, "refresh_credential")[0]
        assert job["state"] == "succeeded", (
            "retrying an unreadable payload cannot make it readable"
        )


class TestReauthPromptStale:
    @pytest.mark.asyncio
    async def test_a_reconnected_account_gets_no_prompt(self, lane_db, sync_conn):
        chain = seed_workspace_chain(sync_conn, "w5e-stale")
        _set_account_state(sync_conn, chain["iga"], "reauth_required")
        _tick(sync_conn)
        assert len(_jobs_of_kind(sync_conn, "reauth_prompt")) == 1
        _set_account_state(sync_conn, chain["iga"], "active")

        async def unused(token):
            raise AssertionError("refresh door must not be touched")

        wl, claimed = await _run_once_w5(lane_db, unused)
        assert claimed is True and wl.processed == 1
        with sync_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM channel_outbox")
            assert cur.fetchone()[0] == 0, "a stale prompt must not fire"
        assert _jobs_of_kind(sync_conn, "reauth_prompt")[0]["state"] == "succeeded"


def _no_stranded(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM jobs WHERE state = 'leased'")
        assert cur.fetchone()[0] == 0, "a leased row nobody owns"
