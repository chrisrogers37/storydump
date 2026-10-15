"""Offboarding drains an archive's admitted uploads before it finalizes
(#1644).

`fn_offboard_finalize` refuses over an archive upload that is unsettled or not
yet destroyed, so leg 3 settles and destroys them (`archive_transit_cleanup`)
after the archives are fenced and before the credentials go. That drain asks
the provider between transactions of its own and takes the pipeline locks of
the intents it settles, so it cannot run while the loop's session holds the
write that cancelled one of them: it would wait on the job session, which waits
on it. These cases run `execute_offboard` in the loop's shape (one open session)
as the worker's login, over a real archive with a real admitted upload and a
fake transit store whose answers can be scripted, and every run has a bound: a
drain that deadlocked itself fails the case rather than hanging the suite.

The archive and its upload are built by the services the pipeline uses; what the
owner alone does (an intent, its provenance) is seeded.

The loop's job session is open for the whole run, as it always is for this
workflow (its cancels, its revoke and its successor ride it). The drain is
arranged so that session holds no row lock when it asks the provider, which is
what the deferral guarantees; the cleanup gate's fake store also refuses any
question asked with a unit of work open, a rule that cannot hold under this
session, so it is lifted here and kept there.

A run whose drain leaves an upload owed never finalizes: it writes an audit row,
mints one successor and returns `archive_transit_pending`. That holds with no
transit store as well: nothing is asked of a provider then, but the rows still
owe what they owe. The next run is that successor, read back from the `jobs`
table as the loop would lease it, never a run made by hand. The successor is due
after the recheck interval, or after an hour (never sooner than the interval)
while an owed upload is overdue, so one the provider never accounts for writes
one audit row an hour.
"""

import asyncio
import uuid

import pytest

from src.services.target import intent_ledger, offboarding as offboarding_module
from src.services.target.offboarding import execute_offboard
from src.services.target.work_loop import WorkerConfig, WorkerDeps
from tests.scripts import test_archive_deletion_cleanup_gate as cleanup
from tests.scripts import test_instagram_observations_gate as obs
from tests.scripts import test_instagram_sources_gate as foundation
from tests.scripts import test_meta_receipt_doors_gate as receipts

pytestmark = [pytest.mark.integration, pytest.mark.slow]

db = receipts.db
origin = foundation.origin
worker = cleanup.worker
owner, one = cleanup.owner, cleanup.one

RUN_BOUND_SECONDS = 60


@pytest.fixture(autouse=True)
def job_session_is_open_by_design(monkeypatch):
    monkeypatch.setattr(cleanup, "in_transaction", lambda: False)


@pytest.fixture
def finalizations(monkeypatch):
    """The workspaces `finalize` was called for, the real door still behind it."""
    calls, real = [], offboarding_module.finalize

    async def spy(factory, workspace_id, grace_seconds):
        calls.append(workspace_id)
        return await real(factory, workspace_id, grace_seconds)

    monkeypatch.setattr(offboarding_module, "finalize", spy)
    return calls


async def cancel_every_live_intent(db, worker, origin):
    """Committed before the run, so the run itself cancels nothing."""
    live = owner(
        db,
        "SELECT id FROM post_intents WHERE workspace_id = %s AND NOT (state = ANY(%s))",
        (origin["ws"], list(intent_ledger.TERMINAL_STATES)),
    )
    for (story,) in live:
        async with obs.uow(worker, origin) as conn:
            await intent_ledger.transition(conn, str(story), "cancelled")


def live_intents(db, origin):
    return one(
        db,
        "SELECT count(*) FROM post_intents WHERE workspace_id = %s"
        " AND NOT (state = ANY(%s))",
        (origin["ws"], list(intent_ledger.TERMINAL_STATES)),
    )[0]


async def world(db, worker, origin, provider, *, cancelled=False):
    """An archive whose upload was admitted and whose answer never came back:
    the asset exists at the provider, the attempt is `unsettled`. The workspace's
    intents, the one that admitted it included, are live, or already cancelled
    and committed."""
    g1 = await obs.enabled(worker, origin)
    parent = await obs.observe(worker, origin, g1, obs.bundle("123"))
    story = cleanup.intent(db, origin, parent.media_item_id)
    cleanup.captured(
        db, origin, story, g1, parent.media_item_id, 1, asset="123", fingerprint="fp"
    )
    artifact, ref = await cleanup.admit(worker, origin, story)
    provider.create(ref)
    if cancelled:
        await cancel_every_live_intent(db, worker, origin)
        assert live_intents(db, origin) == 0
    return story, artifact, ref


def offboarding(db, origin):
    owner(
        db,
        "UPDATE workspaces SET state = 'offboarding',"
        " offboarding_at = now() - interval '2 days' WHERE id = %s",
        (origin["ws"],),
    )


async def run_once(worker, origin, transit, job=None, *, recheck=30):
    """One `offboard_workspace` run in the loop's shape: one open job session,
    grace already elapsed, bounded. *job* is the minted successor being run, or
    a fresh job for the workspace's first run; *recheck* is the configured
    recheck interval, in seconds."""
    deps = WorkerDeps(
        engine=worker,
        transit=transit,
        config=WorkerConfig(
            offboard_grace_seconds=0,
            offboard_drain_timeout_seconds=15 * 60,
            offboard_drain_recheck_seconds=recheck,
        ),
    )
    job = job or {"id": str(uuid.uuid4()), "workspace_id": str(origin["ws"])}
    async with obs.uow(worker, origin) as session:
        return await asyncio.wait_for(
            execute_offboard(deps, session, job), timeout=RUN_BOUND_SECONDS
        )


def workspaces(db, origin):
    return one(db, "SELECT count(*) FROM workspaces WHERE id = %s", (origin["ws"],))[0]


def workspace_state(db, origin):
    return one(db, "SELECT state FROM workspaces WHERE id = %s", (origin["ws"],))[0]


def minted(db, origin):
    """The workspace's `offboard_workspace` jobs: id, state, seconds until due."""
    return owner(
        db,
        "SELECT id::text, state, EXTRACT(EPOCH FROM run_at - clock_timestamp())"
        " FROM jobs WHERE workspace_id = %s AND kind = 'offboard_workspace'",
        (origin["ws"],),
    )


def successor_job(db, origin, outcome):
    """The one job a run minted, leased for the next run as the loop would."""
    ((job_id, state, due_in),) = minted(db, origin)
    assert (job_id, state) == (outcome["successor"], "ready")
    return {"id": job_id, "workspace_id": str(origin["ws"])}, float(due_in)


def audited(db, origin, event):
    return [
        detail
        for (detail,) in owner(
            db,
            "SELECT detail FROM audit_events WHERE workspace_id = %s"
            " AND detail->>'event' = %s",
            (origin["ws"], event),
        )
    ]


def credential_states(db, origin):
    return [
        state
        for (state,) in owner(
            db,
            "SELECT state FROM oauth_credentials WHERE workspace_id = %s",
            (origin["ws"],),
        )
    ]


async def test_the_run_that_cancels_defers_the_drain_and_its_successor_finalizes(
    db, worker, origin
):
    provider = cleanup.Provider()
    transit = cleanup.Transit(provider)
    story, artifact, ref = await world(db, worker, origin, provider)
    live = live_intents(db, origin)
    assert live >= 1, "positive control: there is work to cancel"
    offboarding(db, origin)

    first = await run_once(worker, origin, transit)

    # The cancels are this session's own writes, and one intent it holds is the
    # one the drain would lock: the drain waits for the successor, which writes
    # nothing before it. Not one provider question was asked meanwhile.
    assert (first["outcome"], first["cancelled"]) == ("drained", live)
    assert first["archive_transit"] == {"seam": "deferred"}
    assert (transit.lookups, transit.destroys) == ([], [])
    assert cleanup.artifact_of(db, artifact) == ("unsettled", "pending", False)
    assert workspaces(db, origin) == 1

    job, _ = successor_job(db, origin, first)
    second = await run_once(worker, origin, transit, job)

    assert second["outcome"] == "finalized"
    assert second["archive_transit"] == {
        "seam": "wired",
        "settled": 1,
        "destroyed": 1,
        "unresolved": 0,
        "overdue": 0,
    }
    assert transit.destroys == [ref] and provider.assets == {}
    assert workspaces(db, origin) == 0, "the guard let the finalizer delete it"


async def test_a_run_that_has_written_nothing_drains_in_place_before_it_revokes(
    db, worker, origin
):
    provider = cleanup.Provider()
    transit = cleanup.Transit(provider)
    story, artifact, ref = await world(db, worker, origin, provider, cancelled=True)
    # A credential the archive fence does not take: the revoke has a write to
    # make in this run, so the drain has to come first or wait on it.
    owner(
        db,
        "INSERT INTO oauth_credentials (workspace_id, provider, encrypted_payload)"
        " VALUES (%s, 'gdrive', 'synthetic')",
        (origin["ws"],),
    )
    offboarding(db, origin)

    first = await run_once(worker, origin, transit)

    # Nothing to cancel, so this run drains in place: the upload is settled and
    # destroyed, and only then does the revoke write.
    assert (first["outcome"], first["cancelled"], first["revoked"]) == ("drained", 0, 1)
    assert first["archive_transit"] == {
        "seam": "wired",
        "settled": 1,
        "destroyed": 1,
        "unresolved": 0,
        "overdue": 0,
    }
    assert cleanup.artifact_of(db, artifact) == ("settled", "destroyed", True)
    assert credential_states(db, origin) == ["revoked", "revoked"]
    assert workspaces(db, origin) == 1, "the finalizer is always a later run"

    job, _ = successor_job(db, origin, first)
    second = await run_once(worker, origin, transit, job)

    assert second["outcome"] == "finalized" and workspaces(db, origin) == 0


async def test_an_owed_upload_holds_the_finalize_until_its_successor_finds_none(
    db, worker, origin, finalizations
):
    provider = cleanup.Provider()
    transit = cleanup.Transit(provider)
    story, artifact, ref = await world(db, worker, origin, provider, cancelled=True)
    transit.answers[("destroy", ref)] = cleanup.refused
    offboarding(db, origin)

    # The provider found the upload and would not destroy it: settled, still
    # owed. The run does not reach the finalizer, which would have raised.
    first = await run_once(worker, origin, transit)

    assert first["outcome"] == "archive_transit_pending"
    assert first["archive_transit"] == {
        "seam": "wired",
        "settled": 1,
        "destroyed": 0,
        "unresolved": 1,
        "overdue": 0,
    }
    assert finalizations == [], "the finalizer was never called"
    assert cleanup.artifact_of(db, artifact) == ("settled", "pending", False)
    assert (workspaces(db, origin), workspace_state(db, origin)) == (1, "offboarding")
    # Exactly one successor, due after the recheck interval, and the record.
    job, due_in = successor_job(db, origin, first)
    assert 0 < due_in <= 30, "the recheck interval, not now and not the grace window"
    (detail,) = audited(db, origin, "offboard_archive_transit_unresolved")
    assert (detail["unresolved"], detail["overdue"]) == (1, 0)

    # Positive control: the provider answers again, and the successor run the
    # first one minted is the run that finalizes.
    del transit.answers[("destroy", ref)]
    second = await run_once(worker, origin, transit, job)

    assert second["outcome"] == "finalized"
    assert second["archive_transit"]["destroyed"] == 1
    assert finalizations == [str(origin["ws"])]
    assert workspaces(db, origin) == 0
    assert minted(db, origin) == [], "the successor went with the workspace"


@pytest.mark.parametrize(
    ("recheck", "due"), [(30, 3600), (2 * 3600, 2 * 3600)], ids=["hourly", "no_sooner"]
)
async def test_an_overdue_upload_is_asked_about_hourly_never_sooner_than_the_recheck(
    db, worker, origin, finalizations, recheck, due
):
    provider = cleanup.Provider()
    transit = cleanup.Transit(provider)
    story, artifact, ref = await world(db, worker, origin, provider, cancelled=True)
    # Admitted two hours ago, and the provider does not account for it: still
    # unsettled, past the review horizon. A successor at the configured recheck
    # would write an audit row each time, for as long as that stays true.
    transit.answers[("lookup", ref)] = lambda: None
    cleanup.age_attempt(db, artifact, seconds=2 * 3600)
    offboarding(db, origin)

    first = await run_once(worker, origin, transit, recheck=recheck)

    assert first["outcome"] == "archive_transit_pending"
    assert first["archive_transit"] == {
        "seam": "wired",
        "settled": 0,
        "destroyed": 0,
        "unresolved": 1,
        "overdue": 1,
    }
    assert finalizations == [], "the finalizer was never called"
    assert cleanup.artifact_of(db, artifact) == ("unsettled", "pending", False)
    assert (workspaces(db, origin), workspace_state(db, origin)) == (1, "offboarding")
    # One successor, an hour out, or the configured interval where that is longer.
    job, due_in = successor_job(db, origin, first)
    assert due - 60 < due_in <= due, f"due in {due_in:.0f}s, wanted {due}s"
    (detail,) = audited(db, origin, "offboard_archive_transit_unresolved")
    assert (detail["unresolved"], detail["overdue"]) == (1, 1)

    # Positive control: the provider answers at last, and the successor, however
    # far out it was minted, is the run that settles, destroys and finalizes.
    del transit.answers[("lookup", ref)]
    second = await run_once(worker, origin, transit, job, recheck=recheck)

    assert second["outcome"] == "finalized"
    assert second["archive_transit"]["destroyed"] == 1
    assert finalizations == [str(origin["ws"])]
    assert workspaces(db, origin) == 0


async def test_without_a_transit_store_an_owed_upload_holds_the_finalize_too(
    db, worker, origin, finalizations
):
    provider = cleanup.Provider()
    story, artifact, ref = await world(db, worker, origin, provider, cancelled=True)
    offboarding(db, origin)

    # Nothing can be asked of a provider that is not configured, so nothing is
    # settled or destroyed. The upload is still owed, and the run waits for it as
    # it does with a store: it does not reach the finalizer, whose guard would
    # have raised and stranded the workspace.
    first = await run_once(worker, origin, None)

    assert first["outcome"] == "archive_transit_pending"
    assert first["archive_transit"] == {
        "seam": "absent",
        "settled": 0,
        "destroyed": 0,
        "unresolved": 1,
        "overdue": 0,
    }
    assert finalizations == [], "the finalizer was never called"
    assert cleanup.artifact_of(db, artifact) == ("unsettled", "pending", False)
    assert (workspaces(db, origin), workspace_state(db, origin)) == (1, "offboarding")
    job, due_in = successor_job(db, origin, first)
    assert 0 < due_in <= 30, "the recheck interval, not now and not the grace window"
    (detail,) = audited(db, origin, "offboard_archive_transit_unresolved")
    assert (detail["unresolved"], detail["overdue"]) == (1, 0)

    # Positive control: a store is configured, and the successor the first run
    # minted settles, destroys and finalizes.
    transit = cleanup.Transit(provider)
    second = await run_once(worker, origin, transit, job)

    assert second["outcome"] == "finalized"
    assert second["archive_transit"]["seam"] == "wired"
    assert second["archive_transit"]["destroyed"] == 1
    assert transit.destroys == [ref] and provider.assets == {}
    assert finalizations == [str(origin["ws"])]
    assert workspaces(db, origin) == 0


async def test_without_a_transit_store_a_workspace_that_owes_nothing_finalizes(
    db, worker, origin, finalizations
):
    """The control the case above needs: an absent store holds nothing back when
    nothing is owed, so it is the debt that waits and not the missing seam."""
    await obs.enabled(worker, origin)
    await cancel_every_live_intent(db, worker, origin)
    offboarding(db, origin)

    first = await run_once(worker, origin, None)

    assert first["outcome"] == "finalized"
    assert first["archive_transit"] == {
        "seam": "absent",
        "settled": 0,
        "destroyed": 0,
        "unresolved": 0,
        "overdue": 0,
    }
    assert finalizations == [str(origin["ws"])]
    assert workspaces(db, origin) == 0
