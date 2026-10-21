"""The deletion cleanup executor and its transit drain (#1644): a Meta
deletion receipt freezes generations through the ingress door, the lifecycle
sweep mints the cleanup job, and `past_posts_deletion.execute` runs it as the
worker's login against a fake transit store whose lookups and destroys can be
scripted and whose uploads can be held.

The past-posts source is built through the real services: sources and generations by
`ensure_past_posts_source`, observations by `write_observation`, uploads admitted
by `admit_upload` under the pipeline's locks and completed by the pipeline's own
completion owner. Seeding the owner does alone: identity mappings, intents and
their provenance, cards. Each test gets a fresh lineage database, because
receipts are tenant-free and the sweep mints across every workspace.
"""

import asyncio
import json
import uuid
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.services.target import (
    past_posts_deletion,
    past_posts_transit_cleanup,
    instagram_artifacts,
    instagram_publish,
    intent_ledger,
    media_sync,
    readers,
    source_membership,
    source_state,
)
from src.services.target.google_drive_adapter import GoogleDriveAdapter
from src.services.target.instagram_catalog import lock_past_posts
from src.services.target.instagram_sources import PastPostsRefused
from src.services.target.transit import (
    PastPostsUploadEvidence,
    PastPostsUploadUncertain,
    TransitDestroyRefused,
)
from src.services.target.unit_of_work import in_transaction
from tests.scripts import test_instagram_observations_gate as obs
from tests.scripts import test_instagram_sources_gate as foundation
from tests.scripts import test_meta_receipt_doors_gate as receipts
from tests.scripts.conftest import (
    as_user,
    async_url,
    reap_as_worker,
    set_test_passwords,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]
db = receipts.db
origin = foundation.origin
owner, run, T0 = receipts.owner, receipts.run, receipts.T0


@pytest.fixture
async def worker(world_db, admin_conn):
    set_test_passwords(admin_conn)
    engine = create_async_engine(async_url(as_user(world_db, "svc_worker")))
    try:
        yield engine
    finally:
        await engine.dispose()


class WorkerDied(BaseException):
    """A process death: no handler under test may catch it."""


class Provider:
    """What the transit provider holds: public id -> its creation evidence."""

    def __init__(self):
        self.assets = {}
        self.uploads = []

    def create(self, ref, media_kind="image"):
        self.assets[ref] = PastPostsUploadEvidence(
            ref,
            "asset-" + uuid.uuid4().hex[:12],
            7,
            media_kind,
            "authenticated",
            "c" * 40,
        )
        return self.assets[ref]


class Transit:
    """A fake `TransitStore`. An upload may be held, may create its bytes and
    lose its answer, or may kill its worker after creating them. A lookup or a
    destroy reads the provider unless `answers[(verb, ref)]` answers instead."""

    def __init__(
        self, provider, *, hold=None, lose_answer=False, create=True, dies=False
    ):
        self.provider, self.hold = provider, hold
        self.lose_answer, self.create, self.dies = lose_answer, create, dies
        self.started = asyncio.Event()
        self.lookups, self.destroys, self.answers = [], [], {}

    async def upload(self, content, *, workspace_id, media_kind, preallocated_ref):
        assert not in_transaction()
        self.provider.uploads.append(preallocated_ref)
        self.started.set()
        if self.hold is not None:
            await self.hold.wait()
        evidence = (
            self.provider.create(preallocated_ref, media_kind) if self.create else None
        )
        if self.dies:
            raise WorkerDied()
        if self.lose_answer:
            raise PastPostsUploadUncertain("past-posts upload response lost")
        return evidence

    async def past_posts_lookup(self, public_id, *, resource_type):
        assert not in_transaction()
        self.lookups.append(public_id)
        answer = self.answers.get(("lookup", public_id))
        return answer() if answer else self.provider.assets.get(public_id)

    async def destroy_past_posts(self, public_id, *, resource_type):
        assert not in_transaction()
        self.destroys.append(public_id)
        answer = self.answers.get(("destroy", public_id))
        if answer:
            return answer()
        return "destroyed" if self.provider.assets.pop(public_id, None) else "not_found"


def refused():
    raise TransitDestroyRefused("provider answered neither ok nor not found")


# --- the world --------------------------------------------------------------


def one(db, sql, params=()):
    rows = owner(db, sql, params)
    assert len(rows) == 1, rows
    return rows[0]


async def write(engine, chain, sql, **params):
    async with obs.uow(engine, chain) as conn:
        await conn.execute(text(sql), params)


def mapped(db, chain, generation, who, *, authorized_at=T0, current=True):
    """The verified (app, subject) mapping of *generation*'s authorization."""
    (authorization,) = one(
        db,
        "SELECT authorization_id FROM instagram_past_posts_generations WHERE id = %s",
        (generation,),
    )
    receipts.map_identity(
        db,
        chain,
        who,
        authorized_at=authorized_at,
        authorization=authorization,
        current=current,
    )
    return authorization


def receive(db, who, *, issued=None):
    return receipts.receive(db, who, [receipts.fingerprint()], issued=issued)


def intent(db, chain, item, *, state="scheduled", account=None):
    return one(
        db,
        "INSERT INTO post_intents (workspace_id, ig_account_id, media_item_id,"
        " provider_account_ref, approval_mode, schedule_slot_at, state)"
        " VALUES (%s, %s, %s, 'past-posts-account', 'manual', now() + interval '1 day', %s)"
        " RETURNING id",
        (chain["ws"], account or chain["iga"], item, state),
    )[0]


def captured(
    db, chain, story, source, item, revision, *, asset, fingerprint, account=None
):
    """Preparation's provenance: selected and captured from the source's
    generation (`source["generation_id"]`) at *revision*."""
    owner(
        db,
        "INSERT INTO instagram_intent_provenance (workspace_id, intent_id, source_id,"
        " media_item_id, origin_ig_account_id, selection_generation_id,"
        " selection_observation_revision, capture_generation_id,"
        " capture_observation_revision, selected_asset_identity, validation_fingerprint,"
        " capture_epoch, last_authorized_policy_revision)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, 1)",
        (
            chain["ws"],
            story,
            source["source_id"],
            item,
            account or chain["iga"],
            source["generation_id"],
            revision,
            source["generation_id"],
            revision,
            asset,
            fingerprint,
        ),
    )


async def admit(engine, chain, story):
    """Admit one upload as `PastPostsPipeline.stage` does: the pipeline's locks,
    then `admit_upload` from the intent's provenance."""
    ws, iid = str(chain["ws"]), str(story)
    ref = f"ws/{ws}/{uuid.uuid4().hex}"
    async with obs.uow(engine, chain) as conn:
        subject, _ = await instagram_publish.lock_pipeline(
            conn, workspace_id=ws, intent_id=iid
        )
        p = subject["provenance"]
        artifact = await instagram_artifacts.admit_upload(
            conn,
            workspace_id=ws,
            source_id=str(p["source_id"]),
            data_generation_id=str(p["capture_generation_id"]),
            observation_revision=p["capture_observation_revision"],
            media_item_id=str(p["media_item_id"]),
            intent_id=iid,
            capture_epoch=p["capture_epoch"],
            policy_revision=p["last_authorized_policy_revision"],
            selected_asset_identity=p["selected_asset_identity"],
            validation_fingerprint=p["validation_fingerprint"],
            admitted_asset_ref=ref,
            admitted_resource_type="image",
            upload_attempt_id=str(uuid.uuid4()),
        )
    return artifact, ref


def complete(engine, chain, story, artifact, ref, transit):
    """The pipeline's completion owner as a task: it uploads once, then records
    the creation (or the uncertainty) under its own locks."""
    pipeline = instagram_publish.PastPostsPipeline(
        capability=foundation.capability(), fetch_selected=None
    )
    selected = SimpleNamespace(content=b"past-posts bytes", media_kind="image")
    return asyncio.create_task(
        pipeline._complete(
            engine, str(chain["ws"]), str(story), artifact, ref, selected, transit
        )
    )


async def stage(engine, chain, story, artifact):
    """`instagram_publish._write_stage`'s provenance half."""
    await write(
        engine,
        chain,
        "UPDATE instagram_intent_provenance SET staged_generation_id = capture_generation_id,"
        " staged_observation_revision = capture_observation_revision,"
        " transit_artifact_ref = :artifact WHERE intent_id = :id",
        artifact=str(artifact),
        id=str(story),
    )


async def past_posts(engine, chain, *parents):
    """An enabled past-posts source (G1) with one observation per parent."""
    g1 = await obs.enabled(engine, chain)
    items = [await obs.observe(engine, chain, g1, obs.bundle(p)) for p in parents]
    return g1, items


# --- one run of the job -----------------------------------------------------


def mint(db, receipt):
    """Make the receipt's scopes due, run the lifecycle sweep's deletion leg as
    the worker, and lease the one job it minted for the receipt."""
    owner(
        db,
        "UPDATE meta_deletion_receipt_scopes SET next_attempt_at = now() WHERE receipt_id = %s",
        (receipt,),
    )
    run(db["worker"], "SELECT fn_meta_receipt_cleanup_jobs(25)")
    job_id, ws, token, payload = one(
        db,
        "UPDATE jobs SET state = 'leased', lease_token = gen_random_uuid(),"
        " locked_by = 'deletion-gate', locked_until = now() + interval '5 minutes',"
        " attempts = attempts + 1 WHERE kind = 'meta_deletion_cleanup' AND state = 'ready'"
        " AND payload->>'receipt_id' = %s RETURNING id, workspace_id, lease_token, payload",
        (str(receipt),),
    )
    return {"id": job_id, "workspace_id": ws, "lease_token": token, "payload": payload}


def relet(db, job):
    """The restarted worker's claim: the same job under a new lease."""
    (token,) = one(
        db,
        "UPDATE jobs SET lease_token = gen_random_uuid(),"
        " locked_until = now() + interval '5 minutes', attempts = attempts + 1"
        " WHERE id = %s RETURNING lease_token",
        (job["id"],),
    )
    return {**job, "lease_token": token}


def finish(db, job):
    owner(db, "UPDATE jobs SET state = 'succeeded' WHERE id = %s", (job["id"],))


async def execute(worker, transit, job):
    deps = SimpleNamespace(engine=worker, transit=transit, past_posts_keys=obs.keys())
    return await past_posts_deletion.execute(deps, job)


async def cycle(db, worker, transit, receipt):
    job = mint(db, receipt)
    outcome = await execute(worker, transit, job)
    finish(db, job)
    return outcome


# --- what the database says -------------------------------------------------


def receipt_state(db, receipt):
    found = receipts.receipt(db, receipt["id"])
    return found["state"], found["reason"], receipts.status(db, receipt["capability"])


def scope_of(db, receipt, authorization):
    return one(
        db,
        "SELECT state, results FROM meta_deletion_receipt_scopes"
        " WHERE receipt_id = %s AND authorization_id = %s",
        (receipt["id"], authorization),
    )


def lifecycle(db, generation):
    return one(
        db,
        "SELECT lifecycle FROM instagram_past_posts_generations WHERE id = %s",
        (generation,),
    )[0]


def indexed(db, source, parent):
    rows = owner(
        db,
        "SELECT data_generation_id, observation_revision, permalink, like_count"
        " FROM instagram_media_index WHERE source_id = %s AND parent_media_id = %s",
        (source["source_id"], parent),
    )
    return rows[0] if rows else None


def projection(db, item):
    return one(
        db,
        "SELECT source_data_generation_id, source_projection_redacted FROM media_items"
        " WHERE id = %s",
        (item,),
    )


def artifact_of(db, artifact):
    rows = owner(
        db,
        "SELECT outcome, cleanup_state, destroyed_at IS NOT NULL FROM instagram_artifacts"
        " WHERE artifact_ref = %s",
        (artifact,),
    )
    return rows[0] if rows else None


def age_attempt(db, artifact, *, seconds):
    """An admitted upload that was admitted *seconds* earlier than it was. The
    capture is immutable by trigger, so the trigger is off for this one
    statement; DDL rolls back with its transaction, so it is back on whatever
    happens."""
    owner(
        db,
        "ALTER TABLE instagram_artifacts DISABLE TRIGGER tg_past_posts_artifact_immutable;"
        " UPDATE instagram_artifacts"
        " SET admitted_at = admitted_at - make_interval(secs => %s)"
        " WHERE artifact_ref = %s;"
        " ALTER TABLE instagram_artifacts ENABLE TRIGGER tg_past_posts_artifact_immutable",
        (seconds, artifact),
    )


def story_of(db, story):
    return one(
        db, "SELECT state, cancel_requested FROM post_intents WHERE id = %s", (story,)
    )


def held_records(db, source):
    """Every per-generation record a refusal must leave in place."""
    return one(
        db,
        "SELECT (SELECT count(*) FROM instagram_media_index WHERE source_id = %(s)s),"
        " (SELECT count(*) FROM instagram_intent_provenance WHERE source_id = %(s)s),"
        " (SELECT count(*) FROM instagram_operation_provenance WHERE source_id = %(s)s),"
        " (SELECT count(*) FROM instagram_artifacts WHERE source_id = %(s)s),"
        " (SELECT count(*) FROM media_items WHERE source_id = %(s)s"
        " AND NOT source_projection_redacted)",
        {"s": source["source_id"]},
    )


def zero_left(results):
    return {c: results[c] for c in past_posts_deletion.CATEGORIES if results[c]}


# --- the two G1/G2 sequences ------------------------------------------------


async def test_g1_import_g2_partial_then_reobserve_late_g1_then_g2_deletion(
    db, worker, origin
):
    provider = Provider()
    pipeline = Transit(provider)
    g1 = await obs.enabled(worker, origin)
    p1 = await obs.observe(
        worker,
        origin,
        g1,
        obs.bundle("123", permalink="https://www.instagram.com/p/abc/", like_count=8),
    )
    q1 = await obs.observe(worker, origin, g1, obs.bundle("789"))
    story = intent(db, origin, p1.media_item_id)
    captured(
        db, origin, story, g1, p1.media_item_id, 1, asset="123", fingerprint="fp-g1"
    )
    t1, t1_ref = await admit(worker, origin, story)
    await complete(worker, origin, story, t1, t1_ref, pipeline)
    await stage(worker, origin, story, t1)
    # Bystanders: a second origin with its own settled upload, and Drive.
    other = await obs.second_origin(worker, origin)
    (other_account,) = one(
        db,
        "SELECT origin_ig_account_id FROM media_sources WHERE id = %s",
        (other["source_id"],),
    )
    r1 = await obs.observe(worker, origin, other, obs.bundle("555"))
    bystander = intent(db, origin, r1.media_item_id, account=other_account)
    captured(
        db, origin, bystander, other, r1.media_item_id, 1,
        asset="555", fingerprint="fp-other", account=other_account,
    )  # fmt: skip
    t_other, t_other_ref = await admit(worker, origin, bystander)
    await complete(worker, origin, bystander, t_other, t_other_ref, pipeline)
    drive = one(
        db,
        "SELECT provider_file_ref, state, source_data_generation_id FROM media_items"
        " WHERE id = %s",
        (origin["media"],),
    )

    g2 = await foundation.ensure(worker, origin, foundation.authorization())
    # G2's first read of P is partial and refused whole: P stays G1's, and G1
    # is superseded, so nothing can use it.
    with pytest.raises(PastPostsRefused, match="invalid_past_posts_observation"):
        await obs.observe(
            worker,
            origin,
            g2,
            replace(obs.bundle("123"), permalink="https://example.invalid/p/abc/"),
        )
    assert indexed(db, g1, "123") == (
        g1["generation_id"], 1, "https://www.instagram.com/p/abc/", 8
    )  # fmt: skip
    assert lifecycle(db, g1["generation_id"]) == "superseded"
    # Its own later read lands P whole, the optional fields absent (null).
    p2 = await obs.observe(worker, origin, g2, obs.bundle("123"))
    assert (p2.media_item_id, p2.observation_revision) == (p1.media_item_id, 2)
    assert indexed(db, g1, "123") == (g2["generation_id"], 2, None, None)
    # Validated, and restaged as T2: the stale T1 stage is released first.
    await write(
        worker,
        origin,
        "UPDATE instagram_media_assets SET validation_state = 'supported',"
        " validation_generation_id = data_generation_id,"
        " validation_observation_revision = observation_revision,"
        " validation_fingerprint = 'fp-g2', validated_at = now()"
        " WHERE source_id = :sid AND parent_media_id = '123'",
        sid=g1["source_id"],
    )
    await write(
        worker,
        origin,
        "UPDATE instagram_intent_provenance SET staged_generation_id = NULL,"
        " staged_observation_revision = NULL, transit_artifact_ref = NULL,"
        " capture_generation_id = :gid, capture_observation_revision = 2,"
        " validation_fingerprint = 'fp-g2' WHERE intent_id = :id",
        gid=g2["generation_id"],
        id=str(story),
    )
    t2, t2_ref = await admit(worker, origin, story)
    await complete(worker, origin, story, t2, t2_ref, pipeline)
    await stage(worker, origin, story, t2)

    who = receipts.subject()
    a1 = mapped(db, origin, g1["generation_id"], who, authorized_at=T0, current=False)
    a2 = mapped(
        db, origin, g2["generation_id"], who, authorized_at=T0 + timedelta(days=2)
    )
    # A late request, ordered between the grants: G1 only.
    late = receive(db, who, issued=T0 + timedelta(days=1))
    assert receipts.frozen(db, late["id"]) == {g1["generation_id"]}
    cleanup = Transit(provider)
    assert await cycle(db, worker, cleanup, late["id"]) == "completed"
    assert receipt_state(db, late) == ("completed", None, [("completed",)])
    state, results = scope_of(db, late, a1)
    assert state == "completed" and zero_left(results) == {}
    assert results["transit_destroyed"] == 1
    assert lifecycle(db, g1["generation_id"]) == "deleted"
    assert lifecycle(db, g2["generation_id"]) == "active"
    # T1 and the G1-only Q are gone; P's G2 bundle, T2 and the story stay.
    assert cleanup.destroys == [t1_ref] and t1_ref not in provider.assets
    assert artifact_of(db, t1) is None
    assert indexed(db, g1, "789") is None
    assert projection(db, q1.media_item_id) == (None, True)
    assert one(
        db,
        "SELECT count(*) FROM instagram_item_tombstones WHERE media_item_id = %s",
        (q1.media_item_id,),
    ) == (1,)
    assert indexed(db, g1, "123") == (g2["generation_id"], 2, None, None)
    assert projection(db, p1.media_item_id) == (g2["generation_id"], False)
    assert artifact_of(db, t2) == ("settled", "pending", False)
    assert t2_ref in provider.assets
    assert story_of(db, story) == ("scheduled", False)

    # The G2 request: both mappings are now in scope, G1's with nothing left.
    final = receive(db, who, issued=T0 + timedelta(days=3))
    assert receipts.frozen(db, final["id"]) == {g2["generation_id"]}
    assert await cycle(db, worker, cleanup, final["id"]) == "completed,intent_live"
    assert story_of(db, story) == ("scheduled", True)
    assert artifact_of(db, t2) == ("settled", "destroyed", True)
    assert receipt_state(db, final) == ("pending", None, [("in_progress",)])
    await reap_as_worker(db["owner"])
    assert story_of(db, story) == ("cancelled", True)
    assert await cycle(db, worker, cleanup, final["id"]) == "completed"
    assert receipt_state(db, final) == ("completed", None, [("completed",)])
    assert zero_left(scope_of(db, final, a2)[1]) == {}
    assert lifecycle(db, g2["generation_id"]) == "deleted"
    assert projection(db, p1.media_item_id) == (None, True)
    assert indexed(db, g1, "123") is None and artifact_of(db, t2) is None
    assert sorted(cleanup.destroys) == sorted([t1_ref, t2_ref])
    # The second origin and Drive survived both requests.
    assert indexed(db, other, "555")[0] == other["generation_id"]
    assert projection(db, r1.media_item_id) == (other["generation_id"], False)
    assert lifecycle(db, other["generation_id"]) == "active"
    assert artifact_of(db, t_other) == ("settled", "pending", False)
    assert t_other_ref in provider.assets and t_other_ref not in cleanup.lookups
    assert story_of(db, bystander) == ("scheduled", False)
    assert (
        one(
            db,
            "SELECT provider_file_ref, state, source_data_generation_id FROM media_items"
            " WHERE id = %s",
            (origin["media"],),
        )
        == drive
    )
    assert story_of(db, origin["intent"])[1] is False
    assert provider.uploads == [t1_ref, t_other_ref, t2_ref]


async def test_g1_deleted_before_g2_reobservation_restores_item_only_by_new_read(
    db, worker, origin
):
    g1, (p1,) = await past_posts(worker, origin, "123")
    item = p1.media_item_id
    g2 = await foundation.ensure(worker, origin, foundation.authorization())
    who = receipts.subject()
    mapped(db, origin, g1["generation_id"], who, authorized_at=T0, current=False)
    mapped(db, origin, g2["generation_id"], who, authorized_at=T0 + timedelta(days=2))
    request = receive(db, who, issued=T0 + timedelta(days=1))
    assert await cycle(db, worker, Transit(Provider()), request["id"]) == "completed"
    assert projection(db, item) == (None, True)
    assert indexed(db, g1, "123") is None

    # No old job: a G1 page captured before the deletion is dropped.
    old = await obs.observe(worker, origin, g1, obs.bundle("123"))
    assert (old.accepted, old.reason) == (False, "past_posts_stale_capture")
    # No generic Drive adoption: a Drive page naming the raw parent id or the
    # item's redaction sentinel lands as Drive's own rows.
    outcome = await media_sync._land_page(
        lambda: obs.uow(worker, origin),
        {"id": str(uuid.uuid4())},
        adapters={"gdrive": GoogleDriveAdapter},
        adapter=GoogleDriveAdapter,
        provider="gdrive",
        items=[
            {"kind": "image", "content_hash": h, "ref": f"drive-{n}", "name": "d.jpg"}
            for n, h in enumerate(("123", f"redacted:{item}"))
        ],
        new_checkpoint={"v": 2},
        stored=None,
        walk="fixture-walk",
        source_id=str(origin["src"]),
        workspace_id=str(origin["ws"]),
        reason="fixture",
    )
    assert outcome == "synced"
    assert one(
        db,
        "SELECT source_id, provider_file_ref, source_projection_redacted FROM media_items"
        " WHERE id = %s",
        (item,),
    ) == (g1["source_id"], f"redacted:{item}", True)
    # No epoch renewal: the credential expires and is renewed, two bumps
    # through the single epoch writer, and nothing comes back.
    ws, sid = str(origin["ws"]), g2["source_id"]
    async with obs.uow(worker, origin) as conn:
        await source_membership.expire_past_posts_credential(
            conn, workspace_id=ws, source_id=sid, generation_id=g2["generation_id"]
        )
    async with obs.uow(worker, origin) as conn:
        await lock_past_posts(conn, workspace_id=ws, source_id=sid)
        before = await source_state.past_posts_state(
            conn, workspace_id=ws, source_id=sid
        )
        await conn.execute(
            text("UPDATE oauth_credentials SET state = 'active' WHERE id = :id"),
            {"id": str(origin["credential"])},
        )
        renewed = await source_state.apply_past_posts_transition(
            conn,
            workspace_id=ws,
            source_id=sid,
            before=before,
            reason="credential_renewed",
        )
    assert renewed == g2["source_use_epoch"] + 2
    assert projection(db, item) == (None, True) and indexed(db, g1, "123") is None
    # Only G2's own authorized read restores the stable item from its tombstone.
    read = await obs.observe(
        worker, origin, {**g2, "source_use_epoch": renewed}, obs.bundle("123")
    )
    assert read.accepted and read.media_item_id == item
    assert projection(db, item) == (g2["generation_id"], False)
    assert indexed(db, g1, "123")[0] == g2["generation_id"]
    assert lifecycle(db, g1["generation_id"]) == "deleted"


# --- transit: the drain order -----------------------------------------------


async def test_held_upload_not_found_then_late_creation_never_completes_early(
    db, worker, origin
):
    g1, (p1,) = await past_posts(worker, origin, "123")
    story = intent(db, origin, p1.media_item_id)
    captured(db, origin, story, g1, p1.media_item_id, 1, asset="123", fingerprint="fp")
    provider, hold = Provider(), asyncio.Event()
    pipeline = Transit(provider, hold=hold)
    artifact, ref = await admit(worker, origin, story)
    upload = complete(worker, origin, story, artifact, ref, pipeline)
    await asyncio.wait_for(pipeline.started.wait(), 10)
    who = receipts.subject()
    authorization = mapped(db, origin, g1["generation_id"], who)
    request = receive(db, who)

    # G1 is fenced and the provider holds nothing yet: the lookup answers
    # absent, and so would a destroy. Neither settles the admitted upload.
    cleanup = Transit(provider)
    cleanup.answers[("destroy", ref)] = lambda: "not_found"
    assert await cycle(db, worker, cleanup, request["id"]) == "transit_unresolved"
    assert lifecycle(db, g1["generation_id"]) == "deletion_pending"
    assert (cleanup.lookups, cleanup.destroys) == ([ref], [])
    assert artifact_of(db, artifact) == ("unsettled", "pending", False)
    assert receipt_state(db, request) == ("pending", None, [("in_progress",)])
    state, results = scope_of(db, request, authorization)
    assert (state, results["transit"], results["transit_overdue"]) == ("cleaning", 1, 0)

    # The held request lands after the fence; its completion owner records it.
    hold.set()
    created = await asyncio.wait_for(upload, 30)
    assert created == provider.assets[ref]
    assert artifact_of(db, artifact) == ("settled", "pending", False)
    assert receipt_state(db, request) == ("pending", None, [("in_progress",)])
    await reap_as_worker(db["owner"])
    del cleanup.answers[("destroy", ref)]
    assert await cycle(db, worker, cleanup, request["id"]) == "completed"
    assert cleanup.destroys == [ref] and ref not in provider.assets
    assert cleanup.lookups == [ref, ref], "settlement lookup, then verification"
    assert receipt_state(db, request) == ("completed", None, [("completed",)])
    assert artifact_of(db, artifact) is None
    assert provider.uploads == [ref]


@pytest.mark.parametrize("restart", ["admit", "destroy", "late"])
async def test_restart_between_admission_destroy_and_late_completion(
    db, worker, origin, restart
):
    g1, (p1,) = await past_posts(worker, origin, "123")
    story = intent(db, origin, p1.media_item_id)
    captured(db, origin, story, g1, p1.media_item_id, 1, asset="123", fingerprint="fp")
    provider = Provider()
    artifact, ref = await admit(worker, origin, story)
    if restart == "destroy":
        await complete(worker, origin, story, artifact, ref, Transit(provider))
    elif restart == "late":
        # The provider created the bytes; the worker died before recording them.
        with pytest.raises(WorkerDied):
            await complete(
                worker, origin, story, artifact, ref, Transit(provider, dies=True)
            )
    # restart == "admit": the worker died with the upload admitted, unsent.
    who = receipts.subject()
    mapped(db, origin, g1["generation_id"], who)
    request = receive(db, who)
    job = mint(db, request["id"])
    first = Transit(provider)
    if restart == "destroy":

        def destroyed_then_died():
            provider.assets.pop(ref)
            raise WorkerDied()

        first.answers[("destroy", ref)] = destroyed_then_died
        with pytest.raises(WorkerDied):
            await execute(worker, first, job)
        # The provider no longer holds it; the row has not heard.
        assert ref not in provider.assets
        assert artifact_of(db, artifact) == ("settled", "pending", False)
        job = relet(db, job)
    else:
        assert (
            await execute(worker, first, job)
            == {
                "admit": "transit_unresolved",
                "late": "intent_live",
            }[restart]
        )
        finish(db, job)
        if restart == "admit":
            assert artifact_of(db, artifact) == ("unsettled", "pending", False)
            # The orphaned request lands after the restart.
            provider.create(ref)
        else:
            assert artifact_of(db, artifact) == ("settled", "destroyed", True)
    assert receipt_state(db, request) == ("pending", None, [("in_progress",)])

    await reap_as_worker(db["owner"])
    restarted = Transit(provider)
    if restart == "destroy":
        assert await execute(worker, restarted, job) == "completed"
        finish(db, job)
    else:
        assert await cycle(db, worker, restarted, request["id"]) == "completed"
    assert receipt_state(db, request) == ("completed", None, [("completed",)])
    assert ref not in provider.assets and artifact_of(db, artifact) is None
    assert provider.uploads == ([] if restart == "admit" else [ref])


async def test_lost_upload_response_keeps_receipt_pending_without_second_upload(
    db, worker, origin, monkeypatch
):
    g1, (p1,) = await past_posts(worker, origin, "123")
    story = intent(db, origin, p1.media_item_id)
    captured(db, origin, story, g1, p1.media_item_id, 1, asset="123", fingerprint="fp")
    provider = Provider()
    artifact, ref = await admit(worker, origin, story)
    lost = Transit(provider, lose_answer=True, create=False)
    with pytest.raises(
        instagram_publish.PastPostsEffectRefused, match="past_posts_upload_uncertain"
    ):
        await complete(worker, origin, story, artifact, ref, lost)
    assert artifact_of(db, artifact) == ("uncertain", "pending", False)
    who = receipts.subject()
    authorization = mapped(db, origin, g1["generation_id"], who)
    request = receive(db, who)
    cleanup = Transit(provider)
    assert await cycle(db, worker, cleanup, request["id"]) == "transit_unresolved"
    await reap_as_worker(db["owner"])
    assert await cycle(db, worker, cleanup, request["id"]) == "transit_unresolved"
    assert receipt_state(db, request) == ("pending", None, [("in_progress",)])
    # Past the drain horizon the outcome is still unknown: review, not drain.
    monkeypatch.setattr(past_posts_transit_cleanup, "DRAIN_REVIEW_AFTER_SECONDS", 0)
    assert await cycle(db, worker, cleanup, request["id"]) == "transit_unresolved"
    assert receipt_state(db, request) == (
        "needs_review", "transit_unresolved", [("in_progress",)]
    )  # fmt: skip
    state, results = scope_of(db, request, authorization)
    assert (state, results["transit"], results["transit_overdue"]) == ("cleaning", 1, 1)
    assert artifact_of(db, artifact) == ("uncertain", "pending", False)
    assert cleanup.destroys == [] and len(cleanup.lookups) == 3
    assert provider.uploads == [ref], "one upload, never a second"


async def test_the_drain_takes_a_bounded_batch_oldest_first_and_the_next_run_finishes(
    db, worker, origin
):
    parents = ("123", "456", "789")
    g1, items = await past_posts(worker, origin, *parents)
    provider, refs = Provider(), []
    for item, parent in zip(items, parents):
        story = intent(db, origin, item.media_item_id)
        captured(
            db, origin, story, g1, item.media_item_id, 1,
            asset=parent, fingerprint="fp-" + parent,
        )  # fmt: skip
        _, ref = await admit(worker, origin, story)
        provider.create(ref)
        refs.append(ref)
    transit = Transit(provider)

    first = await past_posts_transit_cleanup.settle_and_destroy(
        worker, workspace_id=origin["ws"], transit=transit, limit=2
    )

    # Two were taken, oldest first; the third is still owed and says so.
    taken = (first.settled, first.destroyed, first.unresolved, first.overdue)
    assert taken == (2, 2, 1, 0)
    assert transit.destroys == refs[:2]
    assert list(provider.assets) == refs[2:]

    # The next run, with the default bound, finishes it.
    second = await past_posts_transit_cleanup.settle_and_destroy(
        worker, workspace_id=origin["ws"], transit=transit
    )
    assert (second.settled, second.destroyed, second.unresolved) == (1, 1, 0)
    assert transit.destroys == refs and provider.assets == {}


async def test_overdue_uploads_the_provider_never_answers_do_not_starve_a_newer_one(
    db, worker, origin
):
    parents = ("123", "456", "789")
    g1, items = await past_posts(worker, origin, *parents)
    provider, attempts = Provider(), []
    for item, parent in zip(items, parents):
        story = intent(db, origin, item.media_item_id)
        captured(
            db, origin, story, g1, item.media_item_id, 1,
            asset=parent, fingerprint="fp-" + parent,
        )  # fmt: skip
        attempts.append(await admit(worker, origin, story))
    (stuck, stuck_ref), (also_stuck, also_stuck_ref), (fresh, fresh_ref) = attempts
    # Two admitted two hours ago, which the provider holds nothing for: still
    # unsettled, past the review horizon. One admitted just now, which it holds.
    for artifact in (stuck, also_stuck):
        age_attempt(db, artifact, seconds=2 * 3600)
    provider.create(fresh_ref)
    transit = Transit(provider)

    first = await past_posts_transit_cleanup.settle_and_destroy(
        worker, workspace_id=origin["ws"], transit=transit, limit=2
    )

    # Oldest first would have spent the whole batch on the two it can never
    # settle. Not yet overdue goes first: the new one is settled and destroyed in
    # this run, and the batch's other place goes to the oldest overdue one.
    taken = (first.settled, first.destroyed, first.unresolved, first.overdue)
    assert taken == (1, 1, 2, 2)
    assert artifact_of(db, fresh) == ("settled", "destroyed", True)
    assert transit.destroys == [fresh_ref] and provider.assets == {}
    assert transit.lookups == [fresh_ref, fresh_ref, stuck_ref]
    for artifact in (stuck, also_stuck):
        assert artifact_of(db, artifact) == ("unsettled", "pending", False)

    # Positive control: overdue is a lower priority, never an abandonment. Once
    # the provider answers for them, the next run settles and destroys both.
    provider.create(stuck_ref)
    provider.create(also_stuck_ref)
    second = await past_posts_transit_cleanup.settle_and_destroy(
        worker, workspace_id=origin["ws"], transit=transit, limit=2
    )
    taken = (second.settled, second.destroyed, second.unresolved, second.overdue)
    assert taken == (2, 2, 0, 0)
    assert transit.destroys == [fresh_ref, stuck_ref, also_stuck_ref]
    assert provider.assets == {}


async def test_a_worker_without_a_transit_store_completes_a_scope_with_no_upload(
    db, worker, origin
):
    g1, (p1,) = await past_posts(worker, origin, "123")
    who = receipts.subject()
    mapped(db, origin, g1["generation_id"], who)
    request = receive(db, who)

    assert await cycle(db, worker, None, request["id"]) == "completed"

    assert receipt_state(db, request) == ("completed", None, [("completed",)])
    assert lifecycle(db, g1["generation_id"]) == "deleted"


async def test_without_a_transit_store_an_owed_upload_is_unresolved_then_reviewed(
    db, worker, origin, monkeypatch
):
    g1, (p1,) = await past_posts(worker, origin, "123")
    story = intent(db, origin, p1.media_item_id)
    captured(db, origin, story, g1, p1.media_item_id, 1, asset="123", fingerprint="fp")
    provider = Provider()
    artifact, ref = await admit(worker, origin, story)
    provider.create(ref)
    who = receipts.subject()
    authorization = mapped(db, origin, g1["generation_id"], who)
    request = receive(db, who)

    # No store: nothing is looked up or destroyed, and the upload stays owed.
    assert await cycle(db, worker, None, request["id"]) == "transit_unresolved"
    assert ref in provider.assets
    assert artifact_of(db, artifact) == ("unsettled", "pending", False)
    assert receipt_state(db, request) == ("pending", None, [("in_progress",)])
    state, results = scope_of(db, request, authorization)
    assert (state, results["transit"], results["transit_overdue"]) == ("cleaning", 1, 0)

    # Past the drain horizon the one-hour rule applies, store or none: review.
    monkeypatch.setattr(past_posts_transit_cleanup, "DRAIN_REVIEW_AFTER_SECONDS", 0)
    assert await cycle(db, worker, None, request["id"]) == "transit_unresolved"
    assert receipt_state(db, request) == (
        "needs_review", "transit_unresolved", [("in_progress",)]
    )  # fmt: skip
    state, results = scope_of(db, request, authorization)
    assert (state, results["transit"], results["transit_overdue"]) == ("cleaning", 1, 1)
    assert ref in provider.assets and artifact_of(db, artifact)[0] == "unsettled"

    # Positive control: a worker with a store settles and destroys it, and the
    # receipt that waited in review for exactly that completes.
    await reap_as_worker(db["owner"])
    cleanup = Transit(provider)
    assert await cycle(db, worker, cleanup, request["id"]) == "completed"
    assert cleanup.destroys == [ref] and ref not in provider.assets
    assert receipt_state(db, request) == ("completed", None, [("completed",)])


# --- deletion in flight -----------------------------------------------------


async def test_deletion_during_indexing_drops_late_pages_and_clears_the_checkpoint(
    db, worker, origin
):
    g1, (p,) = await past_posts(worker, origin, "123")
    await write(
        worker,
        origin,
        "UPDATE media_sources SET sync_checkpoint = CAST(:cp AS jsonb) WHERE id = :sid",
        cp=json.dumps(
            {"v": 2, "generation_id": g1["generation_id"], "pending_helpers": ["456"]}
        ),
        sid=g1["source_id"],
    )
    who = receipts.subject()
    authorization = mapped(db, origin, g1["generation_id"], who)
    request = receive(db, who)
    # The sync in flight lands one more page before the cleanup runs: G1's too.
    q = await obs.observe(worker, origin, g1, obs.bundle("789"))
    assert q.accepted
    assert await cycle(db, worker, Transit(Provider()), request["id"]) == "completed"
    # Its next page was captured before the fence: dropped, nothing returns.
    late = await obs.observe(worker, origin, g1, obs.bundle("456"))
    assert (late.accepted, late.reason) == (False, "past_posts_stale_capture")
    assert one(
        db,
        "SELECT sync_checkpoint FROM media_sources WHERE id = %s",
        (g1["source_id"],),
    ) == (None,)
    assert held_records(db, g1) == (0, 0, 0, 0, 0)
    assert projection(db, p.media_item_id) == (None, True)
    assert projection(db, q.media_item_id) == (None, True)
    assert zero_left(scope_of(db, request, authorization)[1]) == {}
    # A deletion request is not a deauthorization.
    assert one(
        db,
        "SELECT state FROM oauth_credentials WHERE id = %s",
        (origin["credential"],),
    ) == ("active",)


async def test_deletion_during_pending_posting_redacts_card_payloads_only_after_the_stories_end(
    db, worker, origin
):
    g1, (p, q) = await past_posts(worker, origin, "123", "789")
    ws = origin["ws"]
    (binding,) = one(
        db,
        "INSERT INTO channel_bindings (workspace_id, channel, external_ref)"
        " VALUES (%s, 'telegram_group', %s) RETURNING id",
        (ws, f"tg-{uuid.uuid4().hex[:8]}"),
    )
    awaiting = intent(db, origin, p.media_item_id, state="awaiting_approval")
    captured(
        db, origin, awaiting, g1, p.media_item_id, 1, asset="123", fingerprint="fp"
    )
    owner(
        db,
        "INSERT INTO channel_outbox (workspace_id, binding_id, kind, intent_id, payload,"
        " state, external_message_ref) VALUES (%s, %s, 'approval_prompt', %s, %s, 'sent',"
        " '-100:41')",
        (
            ws,
            binding,
            awaiting,
            json.dumps({"v": 2, "caption": "an archived caption", "sent_as": "media"}),
        ),
    )
    # Selected but not yet prepared: no provenance, bound by the item alone.
    planned = intent(db, origin, q.media_item_id)
    who = receipts.subject()
    mapped(db, origin, g1["generation_id"], who)
    request = receive(db, who)

    def cards():
        return owner(
            db,
            "SELECT kind, state, payload FROM channel_outbox WHERE intent_id = %s"
            " ORDER BY created_at, kind",
            (awaiting,),
        )

    assert await cycle(db, worker, Transit(Provider()), request["id"]) == "intent_live"
    assert story_of(db, awaiting) == ("awaiting_approval", True)
    assert story_of(db, planned) == ("scheduled", True)
    assert story_of(db, origin["intent"])[1] is False, "Drive's story is not touched"
    (card, card_state, card_payload), (edit, edit_state, edit_payload) = cards()
    assert (card, card_state, edit, edit_state) == (
        "approval_prompt", "superseded", "prompt_supersede", "pending"
    )  # fmt: skip
    assert card_payload["caption"] == edit_payload["header"] == "an archived caption"

    # The prepared story ends first: the door now passes, and the unprepared
    # one, which the door cannot see, still holds the redaction alone.
    owner(db, "UPDATE post_intents SET state = 'cancelled' WHERE id = %s", (awaiting,))
    assert await cycle(db, worker, Transit(Provider()), request["id"]) == "intent_live"
    assert [r[2] for r in cards()] == [card_payload, edit_payload]
    assert held_records(db, g1) == (2, 1, 0, 0, 2)
    await reap_as_worker(db["owner"])
    assert story_of(db, awaiting)[0] == story_of(db, planned)[0] == "cancelled"
    # Both stories ended, but the edit is still owed delivery: nothing redacted.
    assert await cycle(db, worker, Transit(Provider()), request["id"]) == "cleaning"
    assert [r[2] for r in cards()] == [card_payload, edit_payload]
    assert projection(db, p.media_item_id) == (g1["generation_id"], False)
    owner(
        db,
        "UPDATE channel_outbox SET state = 'sent', external_message_ref = '-100:42'"
        " WHERE intent_id = %s AND kind = 'prompt_supersede'",
        (awaiting,),
    )
    assert await cycle(db, worker, Transit(Provider()), request["id"]) == "completed"
    assert [r[2] for r in cards()] == [
        {"v": 1, "redacted": True, "kind": "approval_prompt"},
        {"v": 1, "redacted": True, "kind": "prompt_supersede"},
    ]
    assert held_records(db, g1) == (0, 0, 0, 0, 0)
    assert receipt_state(db, request)[0] == "completed"


async def test_the_recount_reads_each_category_from_the_stories_bound_to_the_scope(
    db, worker, origin
):
    """`_REMAINING` itself, over a scope that has not been touched: the stories
    bound to it by provenance or by the item alone are counted live and their
    unredacted payloads are counted owed; a bound story that has ended, a
    redacted payload and another source's story are not. The ten aliases are
    exactly the categories `_report` reads."""
    g1, (p, q) = await past_posts(worker, origin, "123", "789")
    ws = origin["ws"]
    (binding,) = one(
        db,
        "INSERT INTO channel_bindings (workspace_id, channel, external_ref)"
        " VALUES (%s, 'telegram_group', %s) RETURNING id",
        (ws, f"tg-{uuid.uuid4().hex[:8]}"),
    )

    def card(story, ref, payload):
        owner(
            db,
            "INSERT INTO channel_outbox (workspace_id, binding_id, kind, intent_id,"
            " payload, state, external_message_ref)"
            " VALUES (%s, %s, 'approval_prompt', %s, %s, 'sent', %s)",
            (ws, binding, story, json.dumps(payload), ref),
        )

    unredacted = {"v": 2, "caption": "an archived caption", "sent_as": "media"}
    awaiting = intent(db, origin, p.media_item_id, state="awaiting_approval")
    captured(
        db, origin, awaiting, g1, p.media_item_id, 1, asset="123", fingerprint="fp"
    )
    card(awaiting, "-100:41", unredacted)
    # One live story per item: the ended one goes first, then the planned one.
    ended = intent(db, origin, q.media_item_id)
    captured(db, origin, ended, g1, q.media_item_id, 1, asset="789", fingerprint="fp")
    owner(db, "UPDATE post_intents SET state = 'cancelled' WHERE id = %s", (ended,))
    card(ended, "-100:43", {"v": 1, "redacted": True, "kind": "approval_prompt"})
    planned = intent(db, origin, q.media_item_id)
    card(planned, "-100:42", unredacted)
    card(origin["intent"], "-100:44", unredacted)

    async with obs.uow(worker, origin) as conn:
        found = await readers.row(
            conn,
            past_posts_deletion._REMAINING,
            terminal=list(intent_ledger.TERMINAL_STATES),
            **past_posts_deletion._params(
                str(ws), g1["source_id"], {g1["generation_id"]}
            ),
        )

    assert found == {
        "observations": 2,
        "assets": 2,
        "shared_fields": 2,
        "intent_provenance": 2,
        "operation_provenance": 0,
        "artifacts": 0,
        "operations": 0,
        "live_intents": 2,
        "payload_snapshots": 2,
        "checkpoints": 0,
        "generations": 1,
    }
    assert set(found) == set(past_posts_deletion._COUNTED)


async def test_held_scope_stays_unavailable_but_nothing_is_destroyed(
    db, worker, origin
):
    g1, (p,) = await past_posts(worker, origin, "123")
    story = intent(db, origin, p.media_item_id)
    captured(db, origin, story, g1, p.media_item_id, 1, asset="123", fingerprint="fp")
    provider = Provider()
    artifact, ref = await admit(worker, origin, story)
    await complete(worker, origin, story, artifact, ref, Transit(provider))
    (epoch,) = one(
        db,
        "SELECT source_use_epoch FROM media_sources WHERE id = %s",
        (g1["source_id"],),
    )
    before = held_records(db, g1)
    who = receipts.subject()
    authorization = mapped(db, origin, g1["generation_id"], who, authorized_at=T0)
    # Issued inside the skew of the grant: which grant it meant is unknowable.
    request = receive(db, who, issued=T0 + timedelta(seconds=100))
    assert receipt_state(db, request)[:2] == ("needs_review", "ordering_ambiguous")
    cleanup = Transit(provider)
    assert await cycle(db, worker, cleanup, request["id"]) == "held"
    assert lifecycle(db, g1["generation_id"]) == "review_hold"
    assert one(
        db,
        "SELECT source_use_epoch FROM media_sources WHERE id = %s",
        (g1["source_id"],),
    ) == (epoch + 1,)
    # Unavailable: a page captured before the hold is dropped.
    dropped = await obs.observe(worker, origin, g1, obs.bundle("789"))
    assert (dropped.accepted, dropped.reason) == (False, "past_posts_stale_capture")
    # Not destroyed: no provider call, every record and the bytes in place.
    assert (cleanup.lookups, cleanup.destroys) == ([], [])
    assert ref in provider.assets
    assert artifact_of(db, artifact) == ("settled", "pending", False)
    assert held_records(db, g1) == before
    assert story_of(db, story) == ("scheduled", False)
    assert scope_of(db, request, authorization) == ("held", {})
    # Fenced, the held scope is no longer due work: the sweep mints nothing.
    owner(
        db,
        "UPDATE meta_deletion_receipt_scopes SET next_attempt_at = now() WHERE receipt_id = %s",
        (request["id"],),
    )
    assert run(db["worker"], "SELECT fn_meta_receipt_cleanup_jobs(25)") == [(0,)]
    assert receipt_state(db, request) == (
        "needs_review", "ordering_ambiguous", [("in_progress",)]
    )  # fmt: skip


async def test_redaction_door_refusals_reach_the_executor_and_redact_nothing(
    db, worker, origin
):
    g1, (p,) = await past_posts(worker, origin, "123")
    story = intent(db, origin, p.media_item_id)
    captured(db, origin, story, g1, p.media_item_id, 1, asset="123", fingerprint="fp")
    provider = Provider()
    artifact, ref = await admit(worker, origin, story)
    await complete(worker, origin, story, artifact, ref, Transit(provider))
    await stage(worker, origin, story, artifact)
    (operation,) = one(
        db,
        "INSERT INTO provider_operations (workspace_id, intent_id, provider, op_kind,"
        " business_key, lease_token) VALUES (%s, %s, 'ig', 'container_create', %s,"
        " gen_random_uuid()) RETURNING id",
        (origin["ws"], story, f"past-posts-gate:{story}"),
    )
    async with obs.uow(worker, origin) as conn:
        subject, _ = await instagram_publish.lock_pipeline(
            conn, workspace_id=str(origin["ws"]), intent_id=str(story)
        )
        await instagram_publish.copy_permit(
            conn,
            operation_id=str(operation),
            workspace_id=str(origin["ws"]),
            intent_id=str(story),
            subject=subject,
        )
    who = receipts.subject()
    authorization = mapped(db, origin, g1["generation_id"], who)
    request = receive(db, who)
    records = held_records(db, g1)
    assert records == (1, 1, 1, 1, 1)
    cleanup = Transit(provider)
    cleanup.answers[("destroy", ref)] = refused

    # The permitted operation is the ledger's to resolve first.
    assert await cycle(db, worker, cleanup, request["id"]) == "operation_unresolved"
    state, results = scope_of(db, request, authorization)
    assert (results["outcome"], results["operations"], results["transit"]) == (
        "operation_unresolved", 1, 1
    )  # fmt: skip
    assert results["live_intents"] == 1 and held_records(db, g1) == records
    owner(
        db,
        "UPDATE provider_operations SET state = 'succeeded' WHERE id = %s",
        (operation,),
    )
    # The provider refuses the destroy: the upload is not proved gone.
    assert await cycle(db, worker, cleanup, request["id"]) == "transit_unresolved"
    assert held_records(db, g1) == records
    assert artifact_of(db, artifact) == ("settled", "pending", False)
    # The destroy answers ok, but the verifying lookup still finds the asset.
    cleanup.answers[("destroy", ref)] = lambda: "destroyed"
    assert await cycle(db, worker, cleanup, request["id"]) == "transit_unresolved"
    assert artifact_of(db, artifact) == ("settled", "pending", False)
    assert ref in provider.assets
    del cleanup.answers[("destroy", ref)]
    # Destroyed and verified, but the flagged story has not ended yet.
    assert await cycle(db, worker, cleanup, request["id"]) == "intent_live"
    assert artifact_of(db, artifact) == ("settled", "destroyed", True)
    assert held_records(db, g1) == records
    assert receipt_state(db, request) == ("pending", None, [("in_progress",)])
    await reap_as_worker(db["owner"])
    assert await cycle(db, worker, cleanup, request["id"]) == "completed"
    assert held_records(db, g1) == (0, 0, 0, 0, 0)
    assert zero_left(scope_of(db, request, authorization)[1]) == {}
    # The operation itself is the ledger's record and stays.
    assert one(
        db, "SELECT state FROM provider_operations WHERE id = %s", (operation,)
    ) == ("succeeded",)
    assert receipt_state(db, request) == ("completed", None, [("completed",)])
