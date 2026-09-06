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

import json

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


def _due_now(conn, cred_id) -> None:
    """Bring a freshly stored credential's first refresh forward to now.

    `store_credential` arms the first refresh ~7 days out — Meta refuses to
    refresh a long-lived token younger than 24 hours (#1221) — so a test that
    exercises the refresh leg itself must make the credential due first."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE oauth_credentials SET next_refresh_at = now() WHERE id = %s",
            (cred_id,),
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


class TestTheRefreshLegIsProviderGuarded:
    """Migration 063 (#982 prerequisite, disclosed on #978).

    062's refresh leg selected due credentials on `state`/`next_refresh_at` with
    NO provider filter, and `ig_refresh` then builds IG-shaped params against
    graph.instagram.com unconditionally. That was safe BY CONSTRUCTION ONLY:
    `store_credential` takes no provider argument and binds `PROVIDER =
    "ig_login"`, and it is the one INSERT site in the tree.

    But `ck_credentials_provider` already admits 'gdrive', so the row is
    insertable the moment a gdrive writer lands — and an armed gdrive row would
    have its token posted to Instagram's host, draw a definitive 400, and be
    wrongly `mark_dead`-ed. Both D31 flips, permanent until reconnect.

    THE GUARD IS ONLY WORTH ANYTHING IF IT CAN GO RED, so this drives both
    providers through one tick: the ig_login row must still mint (or the guard
    is refusing everything and would pass by breaking the feature), and the
    gdrive row must not. Deleting `AND provider = 'ig_login'` from 063 turns the
    second assertion red.
    """

    @staticmethod
    def _arm(conn, chain, provider: str, account_id, token_label: str) -> str:
        """Insert an ARMED credential directly — bypassing store_credential.

        Deliberate: `store_credential` cannot produce a gdrive row (that is the
        construction the guard replaces), so a test routed through it could not
        reach the state under test at all.

        The owner column differs by provider and the schema enforces it —
        `ck_credentials_one_owner` (069) requires an `ig_login` credential to
        name its account and a `gdrive` credential to name NOTHING (the
        workspace is its owner). Writing a Drive credential account-owned
        raises CheckViolation rather than reaching the leg under test.
        """
        ig_owned = provider == "ig_login"
        with conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            cur.execute(
                "INSERT INTO oauth_credentials"
                " (workspace_id, ig_account_id, media_source_id, provider,"
                "  encrypted_payload, state, next_refresh_at)"
                " VALUES (%s, %s, %s, %s, %s, 'active', now()) RETURNING id",
                (
                    chain["ws"],
                    account_id if ig_owned else None,
                    None,
                    provider,
                    token_label,
                ),
            )
            cred_id = cur.fetchone()[0]
        conn.commit()
        return str(cred_id)

    def test_a_gdrive_credential_is_never_minted_for_refresh(self, lane_db, sync_conn):
        chain = seed_workspace_chain(sync_conn, "w5de-guard")
        account_id, _ = _account_of(chain, sync_conn)

        ig_id = self._arm(sync_conn, chain, "ig_login", account_id, "ct-ig")
        gd_id = self._arm(sync_conn, chain, "gdrive", account_id, "ct-gd")

        _tick(sync_conn)
        minted = {
            j["payload"].get("credential_id")
            for j in _jobs_of_kind(sync_conn, "refresh_credential")
        }

        # POSITIVE CONTROL FIRST: a guard that refused everything would satisfy
        # the assertion below while silently breaking the refresh leg.
        assert ig_id in minted, (
            "the ig_login credential must still mint — without this the next"
            " assertion passes for a guard that refuses every provider"
        )
        assert gd_id not in minted, (
            "a gdrive credential was minted for refresh. ig_refresh would post"
            " its token to graph.instagram.com, take a definitive 400, and"
            " mark_dead it — permanent until reconnect (D31)"
        )


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
        # Armed for LATER, not for now: Meta refuses to refresh a long-lived
        # token younger than 24 hours (`0.4`), so a credential due on the very
        # next tick would be refreshed into a definitive 400 and marked dead
        # minutes after it was connected (#1221 review). The first refresh is
        # the `05` row-56 cadence out; the tick must NOT mint yet.
        with sync_conn.cursor() as cur:
            cur.execute(
                "SELECT next_refresh_at > now() + interval '6 days'"
                "   AND next_refresh_at <= now() + interval '8 days'"
                "  FROM oauth_credentials WHERE id = %s",
                (cred_id,),
            )
            assert cur.fetchone()[0], "the first refresh must be ~7 days out"
        _tick(sync_conn)
        assert not any(
            j["payload"].get("credential_id") == cred_id
            for j in _jobs_of_kind(sync_conn, "refresh_credential")
        ), "a brand-new credential must not be refreshed on the next tick"
        # Once it IS due, the leg sees it — the property this test exists for.
        _due_now(sync_conn, cred_id)

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
        _due_now(sync_conn, cred_id)
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
        _due_now(sync_conn, cred_id)
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
        _due_now(sync_conn, cred_id)
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
        _due_now(sync_conn, cred_id)
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


class TestTheRevokeDispositionIsDecidedByTheCaller:
    """#1088 review (rajan): `revoke_workspace_credentials`'s own decision
    logic had ZERO direct coverage — measured, by mutating the retryable
    branch to swallow instead of raise and watching 68/68 tests still pass.

    The gap has a precise shape worth naming, because it is not "someone
    forgot a test". `google_oidc.revoke_token` is tested to REPORT the status
    and interpret nothing — its own test says "only the caller can say whether
    an answer is a failure". So the module under test correctly documented
    that the decision lives elsewhere, and elsewhere was never checked. A
    disclaimer is not a delegation.

    Follows the sibling disposition trio above (`ig_refresh`: success,
    definitive rejection, transient retries, undecryptable) and asserts the
    same observable — the JOB ROW state — rather than the exception type. A
    swallow returns a value, the job succeeds, and `state == "ready"` fails;
    an assertion on the raise alone would be satisfied by any raise at all.
    """

    async def _store_gdrive_cred(self, lane_db, chain) -> str:
        """A gdrive credential with a REAL Drive v1 envelope.

        `_store_cred` above writes an ig_login payload, and the revoke path
        decodes with `google_drive_oauth.decode_payload` — so an ig_login row
        takes the `undecryptable` branch and returns before Google is ever
        called. My first version of this class did exactly that: the retryable
        cases failed for the right symptom and the wrong cause, and the
        SETTLED cases passed for that same wrong cause, because `succeeded` is
        what the undecryptable branch produces too. The control controlled
        nothing.
        """
        from sqlalchemy import text as sqltext

        from src.services.target import google_drive_oauth as gdrive

        engine = create_async_engine(_async_url(lane_db))
        try:
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as session:
                async with session.begin():
                    await session.execute(
                        sqltext("SET LOCAL app.actor_kind = 'migration'")
                    )
                    return await gdrive.store_credential(
                        session,
                        workspace_id=chain["ws"],
                        grant=gdrive.DriveGrant(
                            access_token="ya29.x",
                            refresh_token="1//revoke-me",
                            expires_at=None,
                        ),
                    )
        finally:
            await engine.dispose()

    def _arm(self, sync_conn, chain, cred_id):
        """A revoke job for a credential a disconnect has already marked."""
        with sync_conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            cur.execute(
                "UPDATE oauth_credentials SET state = 'revoked' WHERE id = %s",
                (cred_id,),
            )
            cur.execute(
                "INSERT INTO jobs (kind, workspace_id, lane, serialization_key,"
                " run_at, max_attempts, payload)"
                " VALUES ('revoke_workspace_credentials', %s, 'bulk', %s, now(), 5,"
                "         CAST(%s AS jsonb))",
                (
                    chain["ws"],
                    f"cred:{cred_id}",
                    json.dumps({"v": 1, "credential_id": str(cred_id)}),
                ),
            )
        sync_conn.commit()

    @pytest.mark.parametrize("status", [429, 500, 503])
    @pytest.mark.asyncio
    async def test_an_unsettled_status_keeps_the_job_alive_on_the_ladder(
        self, lane_db, sync_conn, monkeypatch, status
    ):
        """THE MUTANT THIS EXISTS TO KILL. Swap the `raise RevokeRetryable`
        for a return and this goes red: the job finalizes as `succeeded`, and
        a grant Google never revoked is recorded as revoked."""
        from src.services.target import google_oidc

        chain = seed_workspace_chain(sync_conn, f"w5d-revoke-{status}")
        cred_id = await self._store_gdrive_cred(lane_db, chain)
        self._arm(sync_conn, chain, cred_id)

        called = []

        async def answered(client, *, token):
            called.append(token)
            return status

        monkeypatch.setattr(google_oidc, "revoke_token", answered)
        _wl, claimed = await _run_once_w5(lane_db, None)
        assert claimed is True

        assert called == ["1//revoke-me"], (
            "Google must actually have been asked — without this the test"
            " passes on any branch that returns before the provider call,"
            " which is how the first version of it fooled itself"
        )
        job = _jobs_of_kind(sync_conn, "revoke_workspace_credentials")[0]
        assert job["state"] == "ready", (
            f"google answered {status} — the grant may still be live, so the"
            " job must ride the ladder rather than finalize as done"
        )

    @pytest.mark.parametrize("status,why", [(200, "revoked"), (400, "already invalid")])
    @pytest.mark.asyncio
    async def test_a_settled_status_finalizes_the_job(
        self, lane_db, sync_conn, monkeypatch, status, why
    ):
        """The positive control, and it is what stops the test above passing
        for the wrong reason: if every status left the job `ready`, the
        retryable assertion would hold while the logic did nothing. 400 is
        settled BECAUSE the grant is already invalid — the outcome a revoke
        wanted."""
        from src.services.target import google_oidc

        chain = seed_workspace_chain(sync_conn, f"w5d-revoke-ok-{status}")
        cred_id = await self._store_gdrive_cred(lane_db, chain)
        self._arm(sync_conn, chain, cred_id)

        called = []

        async def answered(client, *, token):
            called.append(token)
            return status

        monkeypatch.setattr(google_oidc, "revoke_token", answered)
        _wl, claimed = await _run_once_w5(lane_db, None)
        assert claimed is True

        assert called == ["1//revoke-me"], "the provider call must have happened"
        job = _jobs_of_kind(sync_conn, "revoke_workspace_credentials")[0]
        assert job["state"] == "succeeded", f"{status} is settled ({why})"


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
