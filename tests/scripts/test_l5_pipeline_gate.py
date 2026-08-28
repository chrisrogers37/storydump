"""L.5 slice 2 gate — the checkpointed publish-pipeline executor against a
real database (#915, gate items 1, 2, 6 of #862).

The gate, verbatim where it binds: *"pipeline resumes correctly from EVERY
checkpoint kill (via provider_ops.resume_unresolved — a kill between permit
and publish call lands publishing_ambiguous with zero retries, resolved by
the L.3 reconciler); error-9 defers."*

Harness idioms are the L.3/slice-1 ones, deliberately unchanged: module-scoped
scratch database replayed from the advertised stream, schema-OWNER connections
(RLS bypassed — F.4's runtime harness owns policy proofs; mixing them here
would make a permit failure and a policy failure indistinguishable), one
persistent event loop, intents born directly in their state under the
migration actor.

**Kill fidelity.** A "kill" is an exception raised at an injected provider
seam — the executor deliberately catches nothing it does not type, so the
exception propagates exactly as a process death would leave the database:
committed checkpoints stay, the uncommitted transaction is gone. The database
cannot tell the difference, which is what makes the resume proofs real. Each
resume runs under a FRESH lease token (the reaper + re-claim path), so the
proofs also cover fencing of the dead owner.

**At-most-once is COUNTED**: `StubMetaAdapter` records every call (the L.3
lesson — a stub that counts, not a docstring that promises).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone

import psycopg2
import pytest

from src.services.target.meta_adapter import StubMetaAdapter
from src.services.target.publish_pipeline import (
    CANCELLED,
    DEFERRED_CAP,
    DEFERRED_META_CAP,
    FAILED,
    PARKED_AMBIGUOUS,
    POISONED,
    POSTED,
    RETRY_SCHEDULED,
    run_publish_pipeline,
)
from src.services.target.usage_precheck import UsagePrecheck
from tests.scripts.conftest import (
    _scratch,
    replay_advertised_stream,
    seed_workspace_chain,
    set_test_passwords,
)

pytestmark = pytest.mark.integration

DAY = date(2026, 3, 1)  # the recorded-debit day for intents born debited


@pytest.fixture(scope="module")
def pipe_db(admin_conn, owner_actor):
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        dsn = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        conn = psycopg2.connect(dsn)
        try:
            chain = seed_workspace_chain(conn, "l5-pipe")
        finally:
            conn.close()
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.pool import NullPool

        engine = create_async_engine(
            dsn.replace("postgresql://", "postgresql+asyncpg://", 1),
            poolclass=NullPool,
            connect_args={"server_settings": {"app.actor_kind": "system"}},
        )
        try:
            yield {
                "owner": dsn,
                "ws": chain["ws"],
                "iga": chain["iga"],
                "engine": engine,
            }
        finally:
            _run(engine.dispose())
    finally:
        gen.close()


@pytest.fixture(autouse=True)
def _clean_slate(pipe_db):
    """Per-test teardown for the module-scoped database: cap-bucket rows
    (today's AND the fixed DAY the debited-birth helper stamps) and the
    account's next_slot_at. A leaked today-row flips a later test's flip from
    PROCEED to DEFERRED — a cross-test heisenfailure this fixture deletes as
    a class rather than remembering per test."""
    yield
    _exec(
        pipe_db,
        "DELETE FROM daily_post_counts WHERE workspace_id = %s",
        (pipe_db["ws"],),
    )
    _exec(
        pipe_db,
        "UPDATE ig_accounts SET next_slot_at = NULL WHERE id = %s",
        (pipe_db["iga"],),
    )


def _exec(pipe_db, sql, params=None, fetch=False):
    conn = psycopg2.connect(pipe_db["owner"])
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            cur.execute(sql, params)
            if fetch:
                return cur.fetchall()
            return cur.rowcount
    finally:
        conn.close()


def _new_intent(
    pipe_db,
    *,
    state="approved",
    publish_step="none",
    cancel_requested=False,
    transit_ref=None,
):
    """An intent born directly in *state* on a fresh media item (the L.3/L.5
    template: migration-actor birth, fresh media per uq_intent_live_subject,
    debited states carry cap_consumed_on + a real bucket row so refunds have
    something to return)."""
    ws, iga = pipe_db["ws"], pipe_db["iga"]
    ref = f"acct-{uuid.uuid4()}"
    debited = state in ("publishing", "publishing_ambiguous", "review_required")
    rows = _exec(
        pipe_db,
        "INSERT INTO media_items (workspace_id, source_id, content_hash,"
        " file_name, media_kind, provider_file_ref)"
        " SELECT %s, id, %s, 'f.jpg', 'image', %s FROM media_sources"
        " WHERE workspace_id = %s LIMIT 1 RETURNING id",
        (ws, f"h-{uuid.uuid4()}", f"r-{uuid.uuid4()}", ws),
        fetch=True,
    )
    media = rows[0][0]
    step = "publish_called" if state == "publishing_ambiguous" else publish_step
    cols, vals, params = "", "", [ws, iga, media, ref, state, step, cancel_requested]
    if debited:
        cols += ", cap_consumed_on, ig_container_id"
        vals += ", %s, %s"
        params.extend([DAY, f"ctr-{uuid.uuid4()}"])
        _exec(
            pipe_db,
            "INSERT INTO daily_post_counts (workspace_id, ig_account_id,"
            " local_date, count, cap_at_write) VALUES (%s, %s, %s, 1, 3)"
            " ON CONFLICT (workspace_id, ig_account_id, local_date)"
            " DO UPDATE SET count = daily_post_counts.count + 1",
            (ws, iga, DAY),
        )
    if transit_ref:
        cols += ", transit_asset_ref"
        vals += ", %s"
        params.append(transit_ref)
    rows = _exec(
        pipe_db,
        "INSERT INTO post_intents (workspace_id, ig_account_id, media_item_id,"
        " provider_account_ref, approval_mode, schedule_slot_at, state,"
        " publish_step, cancel_requested" + cols + ")"
        " VALUES (%s, %s, %s, %s, 'manual', now(), %s, %s, %s" + vals + ")"
        " RETURNING id",
        params,
        fetch=True,
    )
    return str(rows[0][0]), ref


def _leased_job(pipe_db, intent_id, *, ref, attempts=1, max_attempts=5):
    """A claimed `publish_pipeline` job row, exactly as fn_claim_job hands one
    to the composition root (leased, token minted, attempts incremented)."""
    token = str(uuid.uuid4())
    rows = _exec(
        pipe_db,
        "INSERT INTO jobs (workspace_id, kind, lane, serialization_key,"
        " payload, max_attempts, attempts, state, lease_token, locked_by,"
        " locked_until)"
        " VALUES (%s, 'publish_pipeline', 'bulk', %s,"
        "         %s, %s, %s, 'leased', %s, 'w-gate', now() + interval '300s')"
        " RETURNING id",
        (
            str(pipe_db["ws"]),
            f"ig:{ref}",
            f'{{"v": 1, "intent_id": "{intent_id}"}}',
            max_attempts,
            attempts,
            token,
        ),
        fetch=True,
    )
    return _job_row(pipe_db, rows[0][0])


def _job_row(pipe_db, job_id):
    r = _exec(
        pipe_db,
        "SELECT id, workspace_id, kind, lane, serialization_key, payload,"
        " state, attempts, max_attempts, lease_token, run_at"
        " FROM jobs WHERE id = %s",
        (job_id,),
        fetch=True,
    )[0]
    return {
        "id": r[0],
        "workspace_id": r[1],
        "kind": r[2],
        "lane": r[3],
        "serialization_key": r[4],
        "payload": r[5],
        "state": r[6],
        "attempts": r[7],
        "max_attempts": r[8],
        "lease_token": r[9],
        "run_at": r[10],
    }


def _reclaim(pipe_db, job_id):
    """The reaper + re-claim path a real resume rides: fresh token, attempts
    incremented (the claim does that), lease window reopened."""
    token = str(uuid.uuid4())
    _exec(
        pipe_db,
        "UPDATE jobs SET state = 'leased', lease_token = %s,"
        " locked_by = 'w-gate2', locked_until = now() + interval '300s',"
        " attempts = attempts + 1 WHERE id = %s",
        (token, job_id),
    )
    return _job_row(pipe_db, job_id)


def _intent_row(pipe_db, intent_id):
    r = _exec(
        pipe_db,
        "SELECT state, publish_step, ig_container_id, ig_media_id,"
        " transit_asset_ref, cap_consumed_on, cap_refunded_at"
        " FROM post_intents WHERE id = %s",
        (intent_id,),
        fetch=True,
    )[0]
    return {
        "state": r[0],
        "publish_step": r[1],
        "ig_container_id": r[2],
        "ig_media_id": r[3],
        "transit_asset_ref": r[4],
        "cap_consumed_on": r[5],
        "cap_refunded_at": r[6],
    }


def _ops(pipe_db, intent_id):
    return [
        {"op_kind": r[0], "state": r[1], "generation": r[2], "response": r[3]}
        for r in _exec(
            pipe_db,
            "SELECT op_kind, state, generation, response_ref"
            " FROM provider_operations WHERE intent_id = %s"
            " ORDER BY op_kind, generation",
            (intent_id,),
            fetch=True,
        )
    ]


def _bucket(pipe_db, day):
    rows = _exec(
        pipe_db,
        "SELECT count FROM daily_post_counts WHERE workspace_id = %s"
        " AND ig_account_id = %s AND local_date = %s",
        (pipe_db["ws"], pipe_db["iga"], day),
        fetch=True,
    )
    return rows[0][0] if rows else None


class FakeTransit:
    """The transit seam, counting. The REAL TransitStore is proven by its own
    unit gate (PR-A); here it would drag the cloudinary SDK into a database
    gate — same reasoning as the reconciler's injected poll."""

    def __init__(self, *, upload_kills=None, destroy_raises=False):
        self.upload_calls: list[dict] = []
        self.url_calls: list[str] = []
        self.destroy_calls: list[str] = []
        self._upload_kills = upload_kills
        self._destroy_raises = destroy_raises
        self._n = 0

    async def upload(self, content, *, workspace_id, media_kind):
        self._n += 1
        ref = f"ws/{workspace_id}/t{self._n}"
        self.upload_calls.append(
            {"workspace_id": workspace_id, "media_kind": media_kind, "ref": ref}
        )
        if self._upload_kills is not None and self._upload_kills.pop(0):
            # The upload HAPPENED (recorded above) and the worker died before
            # the checkpoint — the K2 shape: an orphan asset for the sweep.
            raise Killed("after upload, before checkpoint")
        return ref

    def delivery_url(self, ref, *, media_kind):
        self.url_calls.append(ref)
        return f"https://cdn.example/{ref}?sig=x"

    async def destroy(self, ref, *, media_kind):
        self.destroy_calls.append(ref)
        if self._destroy_raises:
            raise ConnectionError("destroy transport lost")
        return True


class Killed(Exception):
    """The kill instrument. The executor types every catch, so this
    propagates exactly as a process death leaves the database."""


class KillBefore:
    """Wrap one adapter method to die BEFORE the provider sees the call —
    the permit-committed-call-never-made shape."""

    def __init__(self, inner, method: str):
        self._inner = inner
        self._method = method

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if name != self._method:
            return attr

        async def killer(*a, **kw):
            raise Killed(f"before {name}")

        return killer


async def _fetch_bytes(intent_row):
    return b"media-bytes"


class KillAtFetch:
    def __init__(self):
        self.calls = 0

    async def __call__(self, intent_row):
        self.calls += 1
        raise Killed("at media_fetch")


async def _no_sleep(_seconds):
    return None


def _deps(pipe_db, meta, transit=None, **overrides):
    kwargs = {
        "engine": pipe_db["engine"],
        "meta": meta,
        "transit": transit or FakeTransit(),
        "media_fetch": _fetch_bytes,
        "sleep": _no_sleep,
    }
    kwargs.update(overrides)
    return kwargs


_LOOP = asyncio.new_event_loop()


def _run(coro):
    return _LOOP.run_until_complete(coro)


def _today_utc():
    return datetime.now(timezone.utc).date()


class TestTheFlipIntegration:
    def test_proceed_debits_exactly_once_on_the_way_to_posted(self, pipe_db):
        """Gate item 2, PROCEED arm: the §4 flip inside the pipeline debits
        the account-local day exactly once for the whole ladder."""
        intent, ref = _new_intent(pipe_db)
        job = _leased_job(pipe_db, intent, ref=ref)
        outcome = _run(run_publish_pipeline(job, **_deps(pipe_db, StubMetaAdapter())))
        assert outcome == POSTED
        assert _bucket(pipe_db, _today_utc()) == 1

    def test_deferred_reschedules_to_the_next_slot_and_audits_cap_deferred(
        self, pipe_db
    ):
        """Gate item 2, DEFERRED arm, §4 verbatim: "the worker reschedules the
        job to the account's next slot (jobs.run_at); the intent stays
        'approved'; audit row 'cap_deferred'." Plus the attempt the claim
        consumed is restored — deferral is normal operation, not a failure."""
        slot = datetime.now(timezone.utc) + timedelta(hours=2)
        _exec(
            pipe_db,
            "UPDATE ig_accounts SET next_slot_at = %s WHERE id = %s",
            (slot, pipe_db["iga"]),
        )
        _exec(
            pipe_db,
            "INSERT INTO daily_post_counts (workspace_id, ig_account_id,"
            " local_date, count, cap_at_write) VALUES (%s, %s, %s, 3, 3)",
            (pipe_db["ws"], pipe_db["iga"], _today_utc()),
        )
        intent, ref = _new_intent(pipe_db)
        job = _leased_job(pipe_db, intent, ref=ref, attempts=1)
        meta = StubMetaAdapter()
        transit = FakeTransit()
        outcome = _run(run_publish_pipeline(job, **_deps(pipe_db, meta, transit)))
        assert outcome == DEFERRED_CAP
        assert _intent_row(pipe_db, intent)["state"] == "approved"
        after = _job_row(pipe_db, job["id"])
        assert after["state"] == "ready"
        assert abs((after["run_at"] - slot).total_seconds()) < 1
        assert after["attempts"] == 0, "a deferral must restore the claim's attempt"
        assert transit.upload_calls == [] and meta.create_calls == [], (
            "a cap-denied intent must cost zero provider work"
        )
        audits = _exec(
            pipe_db,
            "SELECT detail->>'reason' FROM audit_events WHERE entity_id = %s"
            " AND detail->>'event' = 'cap_deferred'",
            (intent,),
            fetch=True,
        )
        assert [a[0] for a in audits] == ["cap"]

    def test_a_raced_terminal_intent_cancels_the_job_and_leaks_no_debit(self, pipe_db):
        """Gate item 2, raise arm: (1,0) — the debit landed, the flip matched
        no approved row (the intent terminalized under us). The transaction
        rolls back WHOLE: no cap slot leaks, the job routes to cancelled."""
        intent, ref = _new_intent(pipe_db, state="cancelled")
        job = _leased_job(pipe_db, intent, ref=ref)
        outcome = _run(run_publish_pipeline(job, **_deps(pipe_db, StubMetaAdapter())))
        assert outcome == CANCELLED
        assert _bucket(pipe_db, _today_utc()) in (None, 0), (
            "the (1,0) rollback must take the debit with it"
        )
        assert _job_row(pipe_db, job["id"])["state"] == "cancelled"


class TestTheHappyPath:
    def test_the_full_ladder_lands_posted_with_every_04_effect(self, pipe_db):
        """The publishing→posted row of the §4 matrix, effect by effect, plus
        R1 (container id persisted before any publish call) and FC-3.5
        (best-effort inline destroy after commit)."""
        intent, ref = _new_intent(pipe_db)
        job = _leased_job(pipe_db, intent, ref=ref)
        meta = StubMetaAdapter(ready_after_polls=1)
        transit = FakeTransit()
        outcome = _run(run_publish_pipeline(job, **_deps(pipe_db, meta, transit)))
        assert outcome == POSTED

        row = _intent_row(pipe_db, intent)
        assert row["state"] == "posted"
        assert row["publish_step"] == "effect_confirmed"
        assert row["ig_media_id"] and row["ig_media_id"].startswith("media-")
        assert row["ig_container_id"] == meta.publish_calls[0]["container_id"], (
            "R1: the container the publish call used is the persisted one"
        )
        assert row["transit_asset_ref"] == transit.upload_calls[0]["ref"]

        ops = _ops(pipe_db, intent)
        assert [(o["op_kind"], o["state"], o["generation"]) for o in ops] == [
            ("container_create", "succeeded", 1),
            ("publish", "succeeded", 1),
        ]
        assert meta.create_calls[0]["media_url"].startswith("https://cdn.example/")
        assert len(meta.publish_calls) == 1
        assert len(meta.status_calls) >= 2, "readiness was actually polled"

        media_row = _exec(
            pipe_db,
            "SELECT m.times_posted, m.last_posted_at FROM media_items m"
            " JOIN post_intents i ON i.media_item_id = m.id WHERE i.id = %s",
            (intent,),
            fetch=True,
        )[0]
        assert media_row[0] == 1 and media_row[1] is not None
        lock = _exec(
            pipe_db,
            "SELECT l.kind, l.ig_account_id, l.expires_at FROM post_locks l"
            " JOIN post_intents i ON i.media_item_id = l.media_item_id"
            " WHERE i.id = %s",
            (intent,),
            fetch=True,
        )
        assert len(lock) == 1 and lock[0][0] == "recent"
        assert lock[0][1] is not None and lock[0][2] is not None
        acct = _exec(
            pipe_db,
            "SELECT last_posted_at FROM ig_accounts WHERE id = %s",
            (pipe_db["iga"],),
            fetch=True,
        )[0][0]
        assert acct is not None
        assert transit.destroy_calls == [row["transit_asset_ref"]], "FC-3.5 inline"
        assert _job_row(pipe_db, job["id"])["state"] == "succeeded"
        edges = _exec(
            pipe_db,
            "SELECT from_state, to_state, actor_kind FROM audit_events"
            " WHERE entity_id = %s AND from_state IS DISTINCT FROM to_state"
            " ORDER BY created_at",
            (intent,),
            fetch=True,
        )
        assert [(e[0], e[1]) for e in edges] == [
            ("approved", "publishing"),
            ("publishing", "posted"),
        ]
        assert all(e[2] == "system" for e in edges)

    def test_a_failed_inline_destroy_does_not_unpost(self, pipe_db):
        """FC-3.5 is best-effort BY DESIGN — the FC-3.6 sweep is the
        guarantee. A destroy transport loss after the terminal commit must
        not turn a posted outcome into an error."""
        intent, ref = _new_intent(pipe_db)
        job = _leased_job(pipe_db, intent, ref=ref)
        transit = FakeTransit(destroy_raises=True)
        outcome = _run(
            run_publish_pipeline(job, **_deps(pipe_db, StubMetaAdapter(), transit))
        )
        assert outcome == POSTED
        assert _intent_row(pipe_db, intent)["state"] == "posted"
        assert len(transit.destroy_calls) == 1, "the destroy was attempted"


class TestColdEntryAtEveryCheckpoint:
    """The gate's resume clause, strongest form: an intent LEFT at each
    checkpoint by a death (its transaction committed, the process gone)
    completes to posted when the job is re-claimed. Every rung is an entry
    point, and no rung re-does work a prior rung committed."""

    @pytest.mark.parametrize(
        "step", ["none", "transit_uploaded", "container_created", "container_ready"]
    )
    def test_entry_at_each_committed_checkpoint_reaches_posted(self, pipe_db, step):
        kwargs = {"state": "publishing", "publish_step": step}
        if step in ("transit_uploaded", "container_created", "container_ready"):
            kwargs["transit_ref"] = f"ws/{pipe_db['ws']}/pre-{uuid.uuid4()}"
        intent, ref = _new_intent(pipe_db, **kwargs)
        meta = StubMetaAdapter()
        transit = FakeTransit()
        if step in ("container_created", "container_ready"):
            # any cid polls FINISHED on the default stub (ready_after_polls=0);
            # minting it through create_container would pollute the counter
            # this test asserts on
            _exec(
                pipe_db,
                "UPDATE post_intents SET ig_container_id = %s WHERE id = %s",
                (f"pre-ctr-{uuid.uuid4()}", intent),
            )
        job = _leased_job(pipe_db, intent, ref=ref)
        outcome = _run(run_publish_pipeline(job, **_deps(pipe_db, meta, transit)))
        assert outcome == POSTED
        row = _intent_row(pipe_db, intent)
        assert row["state"] == "posted" and row["publish_step"] == "effect_confirmed"
        assert len(meta.publish_calls) == 1
        expected_uploads = 1 if step == "none" else 0
        assert len(transit.upload_calls) == expected_uploads, (
            "a committed checkpoint is never re-done"
        )
        expected_creates = 1 if step in ("none", "transit_uploaded") else 0
        assert len(meta.create_calls) == expected_creates


class TestKillResume:
    """The kill matrix: a death at each provider seam, then a re-claimed
    resume. What each kill leaves behind (committed checkpoints, unresolved
    permits, orphan uploads) is exactly what a process death leaves — the
    executor catches nothing untyped, so the Killed exception IS the crash."""

    def _fresh(self, pipe_db, **stub_kw):
        intent, ref = _new_intent(pipe_db)
        job = _leased_job(pipe_db, intent, ref=ref)
        return intent, ref, job, StubMetaAdapter(**stub_kw)

    def test_K1_kill_after_the_flip_resumes_from_the_top_with_one_debit(self, pipe_db):
        intent, ref, job, meta = self._fresh(pipe_db)
        killer = KillAtFetch()
        with pytest.raises(Killed):
            _run(run_publish_pipeline(job, **_deps(pipe_db, meta, media_fetch=killer)))
        assert _intent_row(pipe_db, intent)["state"] == "publishing"
        job2 = _reclaim(pipe_db, job["id"])
        transit = FakeTransit()
        assert (
            _run(run_publish_pipeline(job2, **_deps(pipe_db, meta, transit))) == POSTED
        )
        assert _bucket(pipe_db, _today_utc()) == 1, (
            "the flip committed once; the resume must not re-debit"
        )
        assert len(transit.upload_calls) == 1

    def test_K2_kill_after_the_upload_orphans_one_asset_and_reuploads(self, pipe_db):
        """`02` §6's stated recoverable-effect cost: 'a duplicate transit
        upload is a second short-lived asset' — the sweep's to reap, never a
        reason to park."""
        intent, ref, job, meta = self._fresh(pipe_db)
        transit = FakeTransit(upload_kills=[True, False])
        with pytest.raises(Killed):
            _run(run_publish_pipeline(job, **_deps(pipe_db, meta, transit)))
        assert _intent_row(pipe_db, intent)["publish_step"] == "none", (
            "the checkpoint after the dead upload must not exist"
        )
        job2 = _reclaim(pipe_db, job["id"])
        assert (
            _run(run_publish_pipeline(job2, **_deps(pipe_db, meta, transit))) == POSTED
        )
        assert len(transit.upload_calls) == 2, "the orphan plus the used one"
        assert (
            _intent_row(pipe_db, intent)["transit_asset_ref"]
            == transit.upload_calls[1]["ref"]
        )

    def test_K3_kill_between_container_permit_and_call_repermits_gen2(self, pipe_db):
        """`02` §6 step 2, container arm: permit committed, call never made.
        An orphaned container is INERT — the resume marks the op failed
        (lost_response), bumps the generation, creates fresh. NEVER
        intent-level ambiguity."""
        intent, ref, job, meta = self._fresh(pipe_db)
        killing = KillBefore(meta, "create_container")
        with pytest.raises(Killed):
            _run(run_publish_pipeline(job, **_deps(pipe_db, killing)))
        assert meta.create_calls == [], "no call was made under the dead permit"
        assert _intent_row(pipe_db, intent)["state"] == "publishing", (
            "container loss must not park the intent"
        )
        job2 = _reclaim(pipe_db, job["id"])
        assert _run(run_publish_pipeline(job2, **_deps(pipe_db, meta))) == POSTED
        ops = _ops(pipe_db, intent)
        containers = [o for o in ops if o["op_kind"] == "container_create"]
        assert [(o["generation"], o["state"]) for o in containers] == [
            (1, "failed"),
            (2, "succeeded"),
        ]
        assert containers[0]["response"]["error"] == "lost_response"

    def test_K4_kill_before_the_readiness_poll_resumes_the_poll_only(self, pipe_db):
        intent, ref, job, meta = self._fresh(pipe_db)
        transit = FakeTransit()
        killing = KillBefore(meta, "container_status")
        with pytest.raises(Killed):
            _run(run_publish_pipeline(job, **_deps(pipe_db, killing, transit)))
        assert _intent_row(pipe_db, intent)["publish_step"] == "container_created"
        job2 = _reclaim(pipe_db, job["id"])
        assert (
            _run(run_publish_pipeline(job2, **_deps(pipe_db, meta, transit))) == POSTED
        )
        assert len(transit.upload_calls) == 1 and len(meta.create_calls) == 1, (
            "resume from container_created re-does nothing upstream"
        )

    def test_K5_kill_between_publish_permit_and_call_parks_with_ZERO_calls(
        self, pipe_db
    ):
        """THE named case, verbatim in the gate: 'a kill between permit and
        publish call lands publishing_ambiguous with ZERO retries'. The
        resumed pipeline must not place the call the dead permit authorized —
        at-most-once survives because the one authorization is never
        re-called, and the count proves it."""
        intent, ref, job, meta = self._fresh(pipe_db)
        killing = KillBefore(meta, "publish")
        with pytest.raises(Killed):
            _run(run_publish_pipeline(job, **_deps(pipe_db, killing)))
        assert meta.publish_calls == []
        assert _intent_row(pipe_db, intent)["publish_step"] == "publish_called", (
            "the permit IS the durable record that a publish may have been attempted"
        )
        job2 = _reclaim(pipe_db, job["id"])
        outcome = _run(run_publish_pipeline(job2, **_deps(pipe_db, meta)))
        assert outcome == PARKED_AMBIGUOUS
        assert meta.publish_calls == [], "ZERO publish calls, ever, on this permit"
        row = _intent_row(pipe_db, intent)
        assert row["state"] == "publishing_ambiguous"
        publish_ops = [o for o in _ops(pipe_db, intent) if o["op_kind"] == "publish"]
        assert [(o["generation"], o["state"]) for o in publish_ops] == [
            (1, "ambiguous")
        ]
        assert _job_row(pipe_db, job["id"])["state"] == "succeeded"
        assert _bucket(pipe_db, _today_utc()) == 1, "the debit stands under ambiguity"

    def test_K6_lost_publish_response_parks_and_never_recalls(self, pipe_db):
        """The other half of R8: the call WAS placed and the response died on
        the wire. One call total, park, and a further resume still refuses to
        re-call — the reconciler owns it."""
        intent, ref, job, meta = self._fresh(pipe_db, publish_outcomes=["transport"])
        outcome = _run(run_publish_pipeline(job, **_deps(pipe_db, meta)))
        assert outcome == PARKED_AMBIGUOUS
        assert len(meta.publish_calls) == 1
        assert _intent_row(pipe_db, intent)["state"] == "publishing_ambiguous"
        job3 = _reclaim(pipe_db, job["id"])
        # The job terminalized 'succeeded'; a fresh claim of the SAME job is
        # not possible in production (terminal rows are never re-readied) —
        # but a DUPLICATE job for the same intent is. Model that instead.
        _exec(
            pipe_db,
            "UPDATE jobs SET state = 'cancelled', lease_token = NULL WHERE id = %s",
            (job3["id"],),
        )
        dup = _leased_job(pipe_db, intent, ref=f"dup-{uuid.uuid4()}")
        outcome2 = _run(run_publish_pipeline(dup, **_deps(pipe_db, meta)))
        assert outcome2 == PARKED_AMBIGUOUS
        assert len(meta.publish_calls) == 1, "still exactly one provider call"
        assert _job_row(pipe_db, dup["id"])["state"] == "succeeded"

    def test_a_stale_owner_cannot_resume_over_the_new_one(self, pipe_db):
        """Fencing end-to-end: after a re-claim, the DEAD owner's job dict
        (old token) is refused at the first domain transaction."""
        from src.services.target.jobs import JobFenced

        intent, ref, job, meta = self._fresh(pipe_db)
        _reclaim(pipe_db, job["id"])  # the new owner exists; old token stale
        with pytest.raises(JobFenced):
            _run(run_publish_pipeline(job, **_deps(pipe_db, meta)))
        assert meta.create_calls == [] and meta.publish_calls == []

    def test_a_stale_owner_is_fenced_before_any_provider_work(self, pipe_db):
        """The survivor-killer (found by the mutation battery): for an
        APPROVED intent a stale owner is fenced at the flip anyway, but for a
        PUBLISHING intent the first lease assert is what stands between a
        zombie worker and REAL provider work — without the load-time assert,
        the stale owner performs a genuine transit upload before any
        checkpoint refuses it.
        Executor mutation that reddens: drop assert_lease from _load."""
        from src.services.target.jobs import JobFenced

        intent, ref = _new_intent(pipe_db, state="publishing", publish_step="none")
        job = _leased_job(pipe_db, intent, ref=ref)
        _reclaim(pipe_db, job["id"])  # the old token is now a zombie's
        meta = StubMetaAdapter()
        transit = FakeTransit()
        fetch = KillAtFetch()  # counting; must never be reached
        with pytest.raises(JobFenced):
            _run(
                run_publish_pipeline(
                    job, **_deps(pipe_db, meta, transit, media_fetch=fetch)
                )
            )
        assert fetch.calls == 0 and transit.upload_calls == [], (
            "a fenced worker must be refused BEFORE it does provider work"
        )


class TestError9:
    def test_error_9_defers_to_the_next_slot_and_the_next_run_publishes_gen2(
        self, pipe_db
    ):
        """Gate item 6, whole arc. Error 9 at the publish call: a cap, not a
        fault — permit resolves failed (Meta said it definitively did not
        happen), job reschedules to the account's next slot with the attempt
        restored, cap_deferred audits, NO quarantine row, debit and key 4
        stand. The next-slot run re-permits generation 2 and publishes —
        exactly one provider call per authorization."""
        slot = datetime.now(timezone.utc) + timedelta(hours=1)
        _exec(
            pipe_db,
            "UPDATE ig_accounts SET next_slot_at = %s WHERE id = %s",
            (slot, pipe_db["iga"]),
        )
        intent, ref = _new_intent(pipe_db)
        job = _leased_job(pipe_db, intent, ref=ref, attempts=1)
        meta = StubMetaAdapter(publish_outcomes=["error_9"])
        outcome = _run(run_publish_pipeline(job, **_deps(pipe_db, meta)))
        assert outcome == DEFERRED_META_CAP

        row = _intent_row(pipe_db, intent)
        assert row["state"] == "publishing", (
            "no publishing→approved edge exists; the deferral is the JOB's"
        )
        assert row["publish_step"] == "publish_called"
        assert row["cap_refunded_at"] is None
        assert _bucket(pipe_db, _today_utc()) == 1, "the debit stands"
        after = _job_row(pipe_db, job["id"])
        assert after["state"] == "ready"
        assert abs((after["run_at"] - slot).total_seconds()) < 1
        assert after["attempts"] == 0, "deferral is normal operation, not an attempt"
        publish_ops = [o for o in _ops(pipe_db, intent) if o["op_kind"] == "publish"]
        assert [(o["generation"], o["state"]) for o in publish_ops] == [(1, "failed")]
        assert publish_ops[0]["response"]["error"] == 9
        quarantine = _exec(
            pipe_db,
            "SELECT count(*) FROM provider_quarantine WHERE workspace_id = %s",
            (pipe_db["ws"],),
            fetch=True,
        )
        assert quarantine[0][0] == 0, "a cap is not a fault: no quarantine row"
        audits = _exec(
            pipe_db,
            "SELECT detail->>'reason', from_state FROM audit_events"
            " WHERE entity_id = %s AND detail->>'event' = 'cap_deferred'",
            (intent,),
            fetch=True,
        )
        assert audits == [("error_9", "publishing")]

        job2 = _reclaim(pipe_db, job["id"])
        assert _run(run_publish_pipeline(job2, **_deps(pipe_db, meta))) == POSTED
        publish_ops = [o for o in _ops(pipe_db, intent) if o["op_kind"] == "publish"]
        assert [(o["generation"], o["state"]) for o in publish_ops] == [
            (1, "failed"),
            (2, "succeeded"),
        ]
        assert len(meta.publish_calls) == 2, "one call per authorization"
        assert _bucket(pipe_db, _today_utc()) == 1, "no re-debit on the retry"


class TestDefinitiveFailureClassification:
    def test_a_terminal_provider_error_fails_and_refunds_in_one_tx(self, pipe_db):
        """`02` §4 publishing→failed: 'terminal provider error; same tx: cap
        refund'."""
        intent, ref = _new_intent(pipe_db)
        job = _leased_job(pipe_db, intent, ref=ref)
        meta = StubMetaAdapter(publish_outcomes=["terminal"])
        outcome = _run(run_publish_pipeline(job, **_deps(pipe_db, meta)))
        assert outcome == FAILED
        row = _intent_row(pipe_db, intent)
        assert row["state"] == "failed"
        assert row["cap_refunded_at"] is not None
        assert _bucket(pipe_db, _today_utc()) == 0, "the refund returned the slot"
        assert _job_row(pipe_db, job["id"])["state"] == "failed"

    def test_a_retryable_error_reschedules_on_the_ladder_keeping_the_attempt(
        self, pipe_db
    ):
        intent, ref = _new_intent(pipe_db)
        job = _leased_job(pipe_db, intent, ref=ref, attempts=1)
        meta = StubMetaAdapter(publish_outcomes=["retryable"])
        before = datetime.now(timezone.utc)
        outcome = _run(run_publish_pipeline(job, **_deps(pipe_db, meta)))
        assert outcome == RETRY_SCHEDULED
        row = _intent_row(pipe_db, intent)
        assert row["state"] == "publishing" and row["publish_step"] == "publish_called"
        after = _job_row(pipe_db, job["id"])
        assert after["state"] == "ready"
        assert after["attempts"] == 1, "a FAILURE keeps its attempt"
        rung = (after["run_at"] - before).total_seconds()
        assert 50 < rung < 90, f"first backoff rung is 60s, got {rung}"

    def test_attempts_exhausted_on_a_retryable_class_poisons_to_review(self, pipe_db):
        """G5: publishing→review_required, debit RETAINED — only the
        operator's resolve-failed refunds from there (`02` §4)."""
        intent, ref = _new_intent(pipe_db)
        job = _leased_job(pipe_db, intent, ref=ref, attempts=5, max_attempts=5)
        meta = StubMetaAdapter(publish_outcomes=["retryable"])
        outcome = _run(run_publish_pipeline(job, **_deps(pipe_db, meta)))
        assert outcome == POISONED
        row = _intent_row(pipe_db, intent)
        assert row["state"] == "review_required"
        assert row["cap_refunded_at"] is None
        assert _bucket(pipe_db, _today_utc()) == 1, "poison retains the debit"
        assert _job_row(pipe_db, job["id"])["state"] == "review_required"

    def test_a_lost_container_create_retries_on_the_ladder_never_parks(self, pipe_db):
        intent, ref = _new_intent(pipe_db)
        job = _leased_job(pipe_db, intent, ref=ref, attempts=1)
        meta = StubMetaAdapter(create_outcomes=["transport"])
        transit = FakeTransit()
        outcome = _run(run_publish_pipeline(job, **_deps(pipe_db, meta, transit)))
        assert outcome == RETRY_SCHEDULED
        assert _intent_row(pipe_db, intent)["state"] == "publishing"
        containers = [
            o for o in _ops(pipe_db, intent) if o["op_kind"] == "container_create"
        ]
        assert [(o["generation"], o["state"]) for o in containers] == [(1, "failed")]
        job2 = _reclaim(pipe_db, job["id"])
        assert (
            _run(run_publish_pipeline(job2, **_deps(pipe_db, meta, transit))) == POSTED
        )
        assert len(transit.upload_calls) == 1, "the transit asset is reused"
        assert len(meta.create_calls) == 2

    def test_a_dead_container_steps_back_and_recreates_fresh(self, pipe_db):
        """Readiness ERROR: the container is definitively gone. The ladder
        steps BACK to transit_uploaded (the asset is still valid) and the
        next run creates a fresh container under generation 2."""
        intent, ref = _new_intent(pipe_db)
        job = _leased_job(pipe_db, intent, ref=ref, attempts=1)
        meta = StubMetaAdapter(status_script=["ERROR"])
        transit = FakeTransit()
        outcome = _run(run_publish_pipeline(job, **_deps(pipe_db, meta, transit)))
        assert outcome == RETRY_SCHEDULED
        row = _intent_row(pipe_db, intent)
        assert row["publish_step"] == "transit_uploaded"
        # #938: the rewound row carries no container id. The dead one is not
        # merely unused — it is a pointer to a resource that no longer exists,
        # and the next reader is a human. The id itself is not lost: the
        # container_create op records it in `response_ref` under its own
        # generation, which is also where the ladder reads generations from.
        assert row["ig_container_id"] is None
        job2 = _reclaim(pipe_db, job["id"])
        assert (
            _run(run_publish_pipeline(job2, **_deps(pipe_db, meta, transit))) == POSTED
        )
        containers = [
            o for o in _ops(pipe_db, intent) if o["op_kind"] == "container_create"
        ]
        assert [(o["generation"], o["state"]) for o in containers] == [
            (1, "succeeded"),
            (2, "succeeded"),
        ]
        assert len(transit.upload_calls) == 1


class TestADeadContainerIdDoesNotOutliveItsContainer:
    """#938. The issue frames the stale id as living "between two instants" —
    the step-back and the next run. Measured, that is only the RETRY path.

    On the POISON path the early `return POISONED` means the step-back UPDATE is
    never reached at all, and nothing that is currently wired transitions out of
    `review_required` (`publish_cap.resolve_retry` is the only implementation
    and has no production caller). So the poisoned row keeps the dead id
    indefinitely, which is the case an operator actually reads — and the fix the
    issue prescribes, one column on the step-back UPDATE, does not touch it.
    """

    def test_a_dead_container_that_exhausts_attempts_poisons_without_the_dead_id(
        self, pipe_db
    ):
        intent, ref = _new_intent(pipe_db)
        job = _leased_job(pipe_db, intent, ref=ref, attempts=5, max_attempts=5)
        meta = StubMetaAdapter(status_script=["ERROR"])
        transit = FakeTransit()
        outcome = _run(run_publish_pipeline(job, **_deps(pipe_db, meta, transit)))
        assert outcome == POISONED
        row = _intent_row(pipe_db, intent)
        assert row["state"] == "review_required"
        assert row["ig_container_id"] is None
        # `publish_step` is deliberately NOT rewound here. It records how far
        # the ladder got, which is what an operator resolving the intent needs;
        # the container id is a pointer to a live resource, and there is no
        # longer one. A NULL says that honestly where a dead id claims the
        # opposite.
        assert row["publish_step"] == "container_created"
        containers = [
            o for o in _ops(pipe_db, intent) if o["op_kind"] == "container_create"
        ]
        # The id is recorded where the history belongs, so clearing the row
        # column loses nothing forensic.
        assert containers and containers[0]["response"]["container_id"]

    def test_a_poison_that_did_NOT_rewind_keeps_its_container_id(self, pipe_db):
        """The guard that stops this becoming a blanket clear.

        Poison is shared by every retryable class. A poison after a failed
        PUBLISH leaves a container that is still LIVE and reusable — nulling its
        id there would throw away a valid pointer, and the resulting row would
        be no more honest than the one #938 is about, only wrong in the other
        direction. Only a caller that rewound past the container rung is saying
        the container is gone.
        """
        intent, ref = _new_intent(pipe_db)
        job = _leased_job(pipe_db, intent, ref=ref, attempts=5, max_attempts=5)
        meta = StubMetaAdapter(publish_outcomes=["retryable"])
        outcome = _run(run_publish_pipeline(job, **_deps(pipe_db, meta)))
        assert outcome == POISONED
        row = _intent_row(pipe_db, intent)
        assert row["state"] == "review_required"
        assert row["ig_container_id"] is not None, (
            "the container was never abandoned — this poison did not rewind"
        )


class TestRoutingAndCancel:
    def test_an_ambiguous_intent_is_the_reconcilers_never_touched(self, pipe_db):
        intent, ref = _new_intent(pipe_db, state="publishing_ambiguous")
        job = _leased_job(pipe_db, intent, ref=ref)
        meta = StubMetaAdapter()
        outcome = _run(run_publish_pipeline(job, **_deps(pipe_db, meta)))
        assert outcome == PARKED_AMBIGUOUS
        assert _intent_row(pipe_db, intent)["state"] == "publishing_ambiguous"
        assert meta.publish_calls == [] and meta.create_calls == []
        assert _job_row(pipe_db, job["id"])["state"] == "succeeded"

    def test_a_review_required_intent_is_the_operators(self, pipe_db):
        intent, ref = _new_intent(pipe_db, state="review_required")
        job = _leased_job(pipe_db, intent, ref=ref)
        outcome = _run(run_publish_pipeline(job, **_deps(pipe_db, StubMetaAdapter())))
        assert outcome == CANCELLED
        assert _intent_row(pipe_db, intent)["state"] == "review_required"
        assert _job_row(pipe_db, job["id"])["state"] == "cancelled"

    def test_a_terminal_intent_cancels_the_duplicate_job(self, pipe_db):
        intent, ref = _new_intent(pipe_db, state="expired")
        job = _leased_job(pipe_db, intent, ref=ref)
        outcome = _run(run_publish_pipeline(job, **_deps(pipe_db, StubMetaAdapter())))
        assert outcome == CANCELLED
        assert _job_row(pipe_db, job["id"])["state"] == "cancelled"

    def test_cancel_requested_is_honored_before_the_flip_only(self, pipe_db):
        """`02` §4: approved→cancelled is 'always honorable' pre-publish; C9
        forbids force-cancel after. Pre-flip honor costs zero provider work
        and zero debit."""
        intent, ref = _new_intent(pipe_db, cancel_requested=True)
        job = _leased_job(pipe_db, intent, ref=ref)
        meta = StubMetaAdapter()
        transit = FakeTransit()
        outcome = _run(run_publish_pipeline(job, **_deps(pipe_db, meta, transit)))
        assert outcome == CANCELLED
        assert _intent_row(pipe_db, intent)["state"] == "cancelled"
        assert _bucket(pipe_db, _today_utc()) in (None, 0)
        assert transit.upload_calls == [] and meta.create_calls == []
        assert _job_row(pipe_db, job["id"])["state"] == "cancelled"
        edges = _exec(
            pipe_db,
            "SELECT from_state, to_state FROM audit_events"
            " WHERE entity_id = %s AND to_state = 'cancelled'",
            (intent,),
            fetch=True,
        )
        assert ("approved", "cancelled") in [tuple(e) for e in edges]


class TestThePrecheck:
    def test_an_over_cap_advisory_defers_before_any_work_including_the_flip(
        self, pipe_db
    ):
        """`02` §8: the pre-check runs immediately before the flip and its
        whole value is skipping doomed work — so an over-cap advisory must
        leave NO debit, NO transit upload, NO container."""
        slot = datetime.now(timezone.utc) + timedelta(minutes=30)
        _exec(
            pipe_db,
            "UPDATE ig_accounts SET next_slot_at = %s WHERE id = %s",
            (slot, pipe_db["iga"]),
        )
        intent, ref = _new_intent(pipe_db)
        job = _leased_job(pipe_db, intent, ref=ref, attempts=1)
        meta = StubMetaAdapter(quota_usage=100, quota_total=100)
        transit = FakeTransit()
        outcome = _run(
            run_publish_pipeline(
                job,
                **_deps(
                    pipe_db, meta, transit, precheck=UsagePrecheck(ttl_seconds=300)
                ),
            )
        )
        assert outcome == DEFERRED_META_CAP
        assert _intent_row(pipe_db, intent)["state"] == "approved"
        assert _bucket(pipe_db, _today_utc()) in (None, 0), "no debit before the flip"
        assert transit.upload_calls == [] and meta.create_calls == []
        assert meta.usage_calls == [ref]
        after = _job_row(pipe_db, job["id"])
        assert after["state"] == "ready" and after["attempts"] == 0
        audits = _exec(
            pipe_db,
            "SELECT detail->>'reason' FROM audit_events"
            " WHERE entity_id = %s AND detail->>'event' = 'cap_deferred'",
            (intent,),
            fetch=True,
        )
        assert [a[0] for a in audits] == ["meta_advisory"]

    def test_the_default_is_off_no_precheck_no_usage_reads(self, pipe_db):
        """C7: the flag is OFF by default — the default executor call makes
        zero usage reads (`02` §8: a cost the check has not yet earned)."""
        intent, ref = _new_intent(pipe_db)
        job = _leased_job(pipe_db, intent, ref=ref)
        meta = StubMetaAdapter()
        assert _run(run_publish_pipeline(job, **_deps(pipe_db, meta))) == POSTED
        assert meta.usage_calls == []


class TestTheJobsDoors:
    def test_a_fenced_reschedule_is_refused_not_silently_dropped(self, pipe_db):
        """`reschedule_job` is CAS'd like finalization: after a re-claim, the
        dead owner's reschedule matches zero rows and RAISES — a silent zero
        would let a stale worker believe it deferred a job the new owner is
        actively running.
        Executor mutation that reddens: drop the rowcount check."""
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from src.services.target.jobs import JobFenced, reschedule_job

        intent, ref = _new_intent(pipe_db)
        job = _leased_job(pipe_db, intent, ref=ref)
        _reclaim(pipe_db, job["id"])  # stale-ify the first token

        async def go():
            maker = async_sessionmaker(pipe_db["engine"], expire_on_commit=False)
            async with maker() as session:
                async with session.begin():
                    await reschedule_job(
                        session,
                        job["id"],
                        job["lease_token"],
                        run_at=datetime.now(timezone.utc),
                        restore_attempt=True,
                    )

        with pytest.raises(JobFenced):
            _run(go())
        assert _job_row(pipe_db, job["id"])["state"] == "leased", (
            "the new owner's lease survives the stale reschedule attempt"
        )
