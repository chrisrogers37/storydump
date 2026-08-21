"""Integration test: the claim-before-publish atomic invariant (#549 / #564)
still holds under concurrent handlers on isolated per-task DB sessions (#557).

Two production changes have to cooperate for a rapid double-tap on an autopost
button to be safe:

  * ``QueueRepository.claim_for_processing`` (#549 / #564) claims the queue row
    with ``SELECT ... FOR UPDATE SKIP LOCKED``. Exactly one concurrent claimer
    may lock the row; the rest skip it and get ``None`` — the dedupe that stops
    a duplicate Instagram post.

  * ``BaseRepository`` now holds its Session in per-instance ContextVars (#557),
    so every asyncio task / ``to_thread`` offload that calls ``detach_session()``
    opens its OWN task-local Session instead of sharing the singleton repo's one
    (a SQLAlchemy Session is not safe for concurrent use).

The two features could each regress the other: session isolation could let two
callbacks reach the DB truly in parallel and BOTH claim (if the row lock did not
serialize them); or a shared session could throw under concurrent use before the
lock even mattered. This test drives both at once — N genuinely concurrent
claimers, each on its own isolated session, one shared singleton repo — and
proves the DB row lock still collapses them to a single winner.

Making the contention deterministic. ``claim_for_processing`` matches ``status
IN (pending, processing)``, so a claimer that runs *after* the winner has
committed would see the row as ``processing`` and re-claim it — two winners. The
invariant only bites while the claimers actually CONTEND: their ``FOR UPDATE
SKIP LOCKED`` must execute while the winner still holds the row lock. Racing N
threads through a barrier is inherently flaky once N exceeds the core count (a
straggler can slip in after the winner commits), so instead the winner acquires
the row lock and HOLDS it — its ``claim_for_processing`` commit is gated on an
event — while every loser concurrently attempts its own real
``claim_for_processing`` against the held row and deterministically skips it.
Gating the winner's Session commit is a test-side interception; the production
``claim_for_processing`` path runs unmodified.

Real DB. These repos go through production ``get_db()`` / ``SessionLocal`` to the
``.env.test`` database — a separate connection from the conftest ``test_db``
rollback fixture, which therefore does NOT clean up rows created here. Every row
is deleted in a ``finally``.
"""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from src.repositories.media_repository import MediaRepository
from src.repositories.queue_repository import QueueRepository
from src.repositories.tenant_scope import SYSTEM_SCOPE

N_CLAIMERS = 5


@pytest.fixture(autouse=True)
def _route_repos_to_test_db(setup_test_database, monkeypatch):
    """Route the production repo session factory at the current-schema test DB.

    The production engine binds to ``settings.database_url`` (``DB_NAME``), which
    in this environment is an older schema missing current columns. conftest's
    ``setup_test_database`` builds the CURRENT schema (``Base.metadata.create_all``)
    in ``settings.test_database_url`` and yields that engine. Repositories open
    sessions through ``get_db()`` → the module-global ``SessionLocal``; rebinding
    that sessionmaker to the test engine sends every repo session — and every
    detached per-task session opened under concurrency — to the current-schema
    DB, without modifying ``src``. The session factory keeps production's
    ``expire_on_commit=False`` (issue #388) so behaviour matches runtime.
    """
    if setup_test_database is None:
        pytest.skip("Database not available - skipping integration test")

    import src.config.database as db_module

    monkeypatch.setattr(
        db_module,
        "SessionLocal",
        sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=setup_test_database,
            expire_on_commit=False,
        ),
    )
    yield


def _create_pending_queue_row() -> tuple:
    """Create a real media_item + a 'pending' posting_queue row via the
    production repos. Returns ``(media_id, queue_id)``. Caller MUST delete them
    with :func:`_delete_rows` (these live in the real ``.env.test`` DB)."""
    media_repo = MediaRepository()
    try:
        media = media_repo.create(
            file_path=f"/test/queue-claim-concurrency/{uuid4()}.jpg",
            file_name="claim_concurrency.jpg",
            file_hash=uuid4().hex,
            file_size_bytes=2048,
            mime_type="image/jpeg",
            chat_settings_id=SYSTEM_SCOPE,
        )
        media_id = media.id
    finally:
        media_repo.close()

    queue_repo = QueueRepository()
    try:
        item = queue_repo.create(
            media_item_id=media_id,
            scheduled_for=datetime.utcnow() - timedelta(minutes=1),
            chat_settings_id=SYSTEM_SCOPE,
        )
        queue_id = item.id
    finally:
        queue_repo.close()

    return media_id, queue_id


def _delete_rows(media_id, queue_id) -> None:
    """Tear down the rows created by :func:`_create_pending_queue_row`."""
    queue_repo = QueueRepository()
    try:
        queue_repo.delete(str(queue_id), SYSTEM_SCOPE)
    finally:
        queue_repo.close()

    media_repo = MediaRepository()
    try:
        media_repo.delete(str(media_id), chat_settings_id=SYSTEM_SCOPE)
    finally:
        media_repo.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_claim_yields_exactly_one_winner():
    """N isolated, concurrent claimers of one queued row → exactly one wins.

    Proves the ``FOR UPDATE SKIP LOCKED`` claim stays atomic under the new
    per-task session isolation (#557): all N claims go through the REAL
    ``claim_for_processing`` on one shared singleton repo, each on its OWN
    task-local Session; the Postgres row lock still collapses them to a single
    winner while the other N-1 get ``None``, and the row settles in
    ``processing``.

    Determinism. ``claim_for_processing`` matches ``status IN (pending,
    processing)``, so a claim that runs *after* the winner commits would see the
    row as still-claimable and re-win — the invariant only bites while claims
    genuinely CONTEND. Rather than race N threads through a barrier (inherently
    flaky once N exceeds the core count — a straggler can slip in after the
    winner commits), we make the contention explicit: the winner acquires the
    row lock and HOLDS it (its ``claim_for_processing`` commit is gated on an
    event) while every loser concurrently attempts its own real
    ``claim_for_processing`` against the locked row. Each loser's ``SKIP
    LOCKED`` deterministically skips the held row → ``None``. This exercises the
    exact guarantee the invariant provides — *while one isolated session holds
    the claimed row, no other concurrent claim can take it* — with no timing
    dependence. Gating the winner's Session commit is a test-side interception;
    the production ``claim_for_processing`` code path runs unmodified.
    """
    # to_thread uses the loop's default executor; size it so the blocked winner
    # thread plus all losers can run at once without starving the pool.
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(
        max_workers=N_CLAIMERS + 2, thread_name_prefix="claimer"
    )
    loop.set_default_executor(executor)

    media_id, queue_id = _create_pending_queue_row()
    qid = str(queue_id)

    queue_repo = QueueRepository()  # ONE shared singleton, as in production
    winner_holds_lock = threading.Event()  # set once the winner has locked the row
    release_winner = threading.Event()  # main releases the winner's commit
    errors: list = []

    def winner_claim():
        # to_thread copied the parent context; detach → this task opens its OWN
        # task-local Session (#557), then claims through the REAL method.
        queue_repo.detach_session()
        try:
            sess = queue_repo.db
            original_commit = sess.commit

            def gated_commit():
                # claim_for_processing has locked the row (SELECT ... FOR UPDATE)
                # and set status='processing'; it is about to commit. Hold the
                # row lock (txn open) until every loser has attempted its claim.
                winner_holds_lock.set()
                if not release_winner.wait(timeout=30):
                    raise TimeoutError("winner commit was never released")
                return original_commit()

            sess.commit = gated_commit  # instance-level gate, winner's session only
            row = queue_repo.claim_for_processing(qid)
            return str(row.id) if row is not None else None
        except Exception as exc:  # noqa: BLE001 — collect, never escape the thread
            errors.append(("winner", repr(exc)))
            winner_holds_lock.set()  # unblock losers so the test fails cleanly
            return None
        finally:
            queue_repo.close()  # return this task's connection to the pool

    def loser_claim():
        # A second concurrent callback on its OWN isolated session. The row is
        # locked by the winner's held txn → SKIP LOCKED skips it → None.
        queue_repo.detach_session()
        try:
            row = queue_repo.claim_for_processing(qid)
            return str(row.id) if row is not None else None
        except Exception as exc:  # noqa: BLE001
            errors.append(("loser", repr(exc)))
            return None
        finally:
            queue_repo.close()

    try:
        # 1) Winner acquires + holds the row lock, then blocks just before commit.
        winner_task = asyncio.create_task(asyncio.to_thread(winner_claim))
        assert await asyncio.to_thread(winner_holds_lock.wait, 30), (
            "winner never acquired the row lock"
        )

        # 2) N-1 losers claim concurrently WHILE the winner holds the lock.
        loser_results = await asyncio.gather(
            *(asyncio.to_thread(loser_claim) for _ in range(N_CLAIMERS - 1))
        )

        # 3) Release the winner; it commits and returns the claimed row.
        release_winner.set()
        winner_result = await winner_task

        results = [winner_result] + list(loser_results)
        winners = [r for r in results if r is not None]
        nones = [r for r in results if r is None]
        print(
            f"\n{N_CLAIMERS} concurrent claimers → "
            f"{len(winners)} winner, {len(nones)} None"
        )

        assert not errors, (
            f"claimers raised under concurrent isolated sessions: {errors}"
        )
        assert len(winners) == 1, (
            f"expected exactly 1 winner, got {len(winners)} (winners={winners}); "
            f"a claim on the held row must return None (FOR UPDATE SKIP LOCKED)"
        )
        assert winners[0] == qid
        assert len(nones) == N_CLAIMERS - 1

        # The winner's claim committed status='processing'; re-read on a fresh
        # session to confirm the row settled there.
        verify_repo = QueueRepository()
        try:
            row = verify_repo.get_by_id(qid, chat_settings_id=SYSTEM_SCOPE)
            assert row is not None, "claimed row vanished"
            assert row.status == "processing", (
                f"expected status 'processing' after claim, got {row.status!r}"
            )
        finally:
            verify_repo.close()
    finally:
        release_winner.set()  # ensure the winner can never hang on teardown
        _delete_rows(media_id, queue_id)
        executor.shutdown(wait=True)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_detach_session_isolates_session_per_offload():
    """detach_session() hands each concurrent offload its own Session object.

    Two ``to_thread`` offloads share ONE singleton repo. Each detaches, so each
    must open a distinct Session (isolation). As a control, two sequential reads
    within a SINGLE context (no detach) reuse the one cached Session — proving
    detach is what creates the isolation, not merely running on the repo.
    """
    repo = QueueRepository()  # ONE singleton shared across both offloads

    def offload_with_detach():
        # to_thread copied the parent context; detach → this offload opens its
        # OWN task-local Session rather than sharing the inherited one.
        repo.detach_session()
        sess = repo.db
        sess.execute(text("SELECT 1"))
        repo.end_read_transaction()
        # Return the live Session object (not id()): a garbage-collected
        # session's address can be reused, which would fake an id() match.
        return sess

    sess_a, sess_b = await asyncio.gather(
        asyncio.to_thread(offload_with_detach),
        asyncio.to_thread(offload_with_detach),
    )
    print(
        f"\ndetach → session ids {id(sess_a)} vs {id(sess_b)} "
        f"(distinct={sess_a is not sess_b})"
    )
    try:
        assert sess_a is not sess_b, (
            "detach_session() must give each offload its own Session; got the "
            "same object → sessions are shared across tasks"
        )

        # Control: without detach, sequential reads in one context reuse the
        # single cached Session — same object.
        def read_same_context():
            sess = repo.db
            sess.execute(text("SELECT 1"))
            repo.end_read_transaction()
            return sess

        ctrl_a = read_same_context()
        ctrl_b = read_same_context()
        print(f"no-detach (one context) → same session: {ctrl_a is ctrl_b}")
        assert ctrl_a is ctrl_b, (
            "without detach, one context must reuse its cached Session"
        )
    finally:
        sess_a.close()
        sess_b.close()
        repo.close()


@pytest.mark.integration
@pytest.mark.parametrize("delivery_status", ["sent_unconfirmed", "delivered"])
def test_claim_admits_delivery_states(delivery_status):
    """A human tap on a 'delivered' or 'sent_unconfirmed' card must still claim
    the row. Before the delivery-state split these rows sat in 'processing';
    now claim_for_processing must admit the new states or the tap resolves to
    None ("Queue item not found")."""
    media_id, queue_id = _create_pending_queue_row()
    try:
        repo = QueueRepository()
        try:
            if delivery_status == "delivered":
                # A row only reaches 'delivered' stamped (INV-1,
                # check_delivered_stamped) — go through the real seam,
                # which stamps and promotes in one step.
                repo.set_telegram_message(str(queue_id), 990001, 110011, SYSTEM_SCOPE)
            else:
                repo.update_status(str(queue_id), delivery_status, SYSTEM_SCOPE)
            claimed = repo.claim_for_processing(str(queue_id))
        finally:
            repo.close()
        assert claimed is not None, f"a {delivery_status} row must be claimable"
        assert claimed.status == "processing"
    finally:
        _delete_rows(media_id, queue_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_transition_concurrent_loser_fails():
    """Two concurrent transitions out of one 'processing' row → exactly one
    wins; the loser must return None, not silently commit a second success.

    The old read-check-write transition() let both callers read 'processing',
    both pass the allowed_from guard, and both commit — a TOCTOU double-success
    (rajan, #694). The atomic conditional UPDATE closes it: the winner's UPDATE
    takes the row lock and holds it (its commit is gated), so the loser's
    ``UPDATE ... WHERE status='processing'`` blocks on that lock; when the
    winner commits the row out of 'processing', the loser's WHERE re-evaluates
    to 0 rows and it returns None. Gating the winner's commit is a test-side
    interception; the production transition() path runs unmodified. Not live
    today (one caller), but PR3/4/5 add concurrent callers onto this seam.
    """
    media_id, queue_id = _create_pending_queue_row()
    qid = str(queue_id)
    seed = QueueRepository()
    try:
        seed.update_status(qid, "processing", SYSTEM_SCOPE)
    finally:
        seed.close()

    queue_repo = QueueRepository()  # ONE shared singleton, per-task sessions
    winner_holds = threading.Event()  # winner ran its UPDATE, gated before commit
    release_winner = threading.Event()
    loser_attempting = threading.Event()
    errors: list = []

    def winner_transition():
        queue_repo.detach_session()
        try:
            sess = queue_repo.db
            original_commit = sess.commit

            def gated_commit():
                # transition() has issued its UPDATE (row locked); hold the lock
                # open until the loser has begun its own concurrent transition.
                winner_holds.set()
                if not release_winner.wait(timeout=30):
                    raise TimeoutError("winner commit was never released")
                return original_commit()

            sess.commit = gated_commit  # instance-level gate, winner's session only
            row = queue_repo.transition(
                qid, "sent_unconfirmed", SYSTEM_SCOPE, allowed_from={"processing"}
            )
            return row is not None
        except Exception as exc:  # noqa: BLE001 — collect, never escape the thread
            errors.append(("winner", repr(exc)))
            winner_holds.set()
            return False
        finally:
            queue_repo.close()

    def loser_transition():
        queue_repo.detach_session()
        try:
            loser_attempting.set()
            # Target 'failed', not 'delivered': the loser can legitimately win
            # this race under thread scheduling, and an unstamped row may not
            # become 'delivered' (INV-1, check_delivered_stamped) — the target
            # state is incidental to what this test proves (exactly one of two
            # concurrent guarded transitions succeeds).
            row = queue_repo.transition(
                qid, "failed", SYSTEM_SCOPE, allowed_from={"processing"}
            )
            return row is not None
        except Exception as exc:  # noqa: BLE001
            errors.append(("loser", repr(exc)))
            return False
        finally:
            queue_repo.close()

    try:
        winner_task = asyncio.create_task(asyncio.to_thread(winner_transition))
        assert await asyncio.to_thread(winner_holds.wait, 30), (
            "winner never reached its gated commit"
        )
        loser_task = asyncio.create_task(asyncio.to_thread(loser_transition))
        await asyncio.to_thread(loser_attempting.wait, 30)
        # Release the winner: it commits the row out of 'processing', the loser's
        # blocked UPDATE unblocks, re-evaluates its WHERE, and matches 0 rows.
        release_winner.set()
        winner_result, loser_result = await asyncio.gather(winner_task, loser_task)

        assert not errors, f"transitions raised under concurrent sessions: {errors}"
        winners = [r for r in (winner_result, loser_result) if r]
        assert len(winners) == 1, (
            f"expected exactly 1 winner, got {len(winners)}; the loser must fail "
            f"(None), not silently commit a second success (TOCTOU)"
        )
        # Which side wins is thread scheduling's call (the gate makes the
        # gated side the overwhelmingly likely winner, not a guaranteed one);
        # the invariant is that the row settles in exactly the single
        # winner's target state.
        expected_status = "sent_unconfirmed" if winner_result else "failed"

        verify_repo = QueueRepository()
        try:
            row = verify_repo.get_by_id(qid, chat_settings_id=SYSTEM_SCOPE)
            assert row is not None and row.status == expected_status, (
                f"row must settle in the winner's target {expected_status!r}, "
                f"got {row.status!r}"
            )
        finally:
            verify_repo.close()
    finally:
        release_winner.set()  # never let the winner hang on teardown
        _delete_rows(media_id, queue_id)


@pytest.mark.integration
def test_transition_unconditional_sets_status():
    """allowed_from=None writes the status unconditionally and returns the row."""
    media_id, queue_id = _create_pending_queue_row()
    try:
        repo = QueueRepository()
        try:
            repo.update_status(qid := str(queue_id), "processing", SYSTEM_SCOPE)
            row = repo.transition(qid, "failed", SYSTEM_SCOPE)
            assert row is not None and row.status == "failed"
        finally:
            repo.close()
    finally:
        _delete_rows(media_id, queue_id)


@pytest.mark.integration
def test_transition_allowed_from_mismatch_is_noop():
    """A guarded transition from a disallowed state is a no-op → None, unchanged."""
    media_id, queue_id = _create_pending_queue_row()
    try:
        repo = QueueRepository()
        try:
            repo.update_status(qid := str(queue_id), "failed", SYSTEM_SCOPE)
            assert (
                repo.transition(
                    qid, "delivered", SYSTEM_SCOPE, allowed_from={"processing"}
                )
                is None
            )
            assert repo.get_by_id(qid, chat_settings_id=SYSTEM_SCOPE).status == "failed"
        finally:
            repo.close()
    finally:
        _delete_rows(media_id, queue_id)


@pytest.mark.integration
def test_transition_missing_row_returns_none():
    """A transition on a nonexistent row returns None (0 rows affected)."""
    repo = QueueRepository()
    try:
        assert (
            repo.transition(
                str(uuid4()), "delivered", SYSTEM_SCOPE, allowed_from={"processing"}
            )
            is None
        )
    finally:
        repo.close()
