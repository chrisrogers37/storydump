"""W6 gate — the media-sync executors against the real machinery (#942, #982).

The clock's leg 4 nulls `next_sync_at` at mint and ONLY the sync executor
re-arms it, so until now a minted sync job parked forever and its source
dropped out of the due-scan with the parked job as the sole carrier. These
scenarios prove the whole loop on the replayed schema: mint → list through
the drive seam → media_items upserted with the per-workspace dedup the
schema enforces → checkpoint persisted → next_sync_at re-armed with the `05`
baseline → first_ingest_chunk chained while pages remain → the error/probe
state machine (`02` §2: the sync IS the probe).

The drive door is the #982 seam; a scripted stub drives every scenario and
each stub outcome can go red on its own (the controls-can-fail standard).
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from src.services.target.commands import CommandRefused
from src.services.target.work_loop import WorkerConfig
from src.worker import compose
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


def _arm_source(conn, source_id, when="now()"):
    with conn.cursor() as cur:
        cur.execute("SET app.actor_kind = 'migration'")
        cur.execute(
            f"UPDATE media_sources SET next_sync_at = {when} WHERE id = %s",
            (source_id,),
        )
    conn.commit()


def _tick(conn, max_jobs: int = 50) -> None:
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SET app.actor_kind = 'migration'")
        cur.execute(
            "SELECT fn_clock_tick(%s, %s::interval, %s::jsonb)",
            (max_jobs, "7 days", "{}"),
        )
    conn.commit()


def _source_row(conn, source_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state, next_sync_at, sync_checkpoint, last_sync_success_at,"
            "       alerted_at"
            " FROM media_sources WHERE id = %s",
            (source_id,),
        )
        r = cur.fetchone()
    return {
        "state": r[0],
        "next_sync_at": r[1],
        "sync_checkpoint": r[2],
        "last_sync_success_at": r[3],
        "alerted_at": r[4],
    }


def _jobs(conn, kind):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, payload, state FROM jobs WHERE kind = %s ORDER BY created_at",
            (kind,),
        )
        return [{"id": r[0], "payload": r[1], "state": r[2]} for r in cur.fetchall()]


def _items(conn, ws):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT provider_file_ref, content_hash, state FROM media_items"
            " WHERE workspace_id = %s ORDER BY provider_file_ref",
            (ws,),
        )
        return cur.fetchall()


#: Sentinel so `_item(mime=None)` can mean "the adapter said nothing", which is
#: a different fixture from "the caller did not choose".
_UNSET = object()


def _media_row(conn, ws, ref):
    """The columns this gate asserts CONTENT on, by ref."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT file_name, media_kind, mime_type FROM media_items"
            " WHERE workspace_id = %s AND provider_file_ref = %s",
            (ws, ref),
        )
        r = cur.fetchone()
    return None if r is None else {"file_name": r[0], "kind": r[1], "mime": r[2]}


class ScriptedDrive:
    """The #982 seam, scripted. `pages` is a list of (items, next_page_token);
    an exception instance anywhere in the list is RAISED at that call."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    async def list_changes(self, config, checkpoint, *, source_id, workspace_id):
        # Both ids are RECORDED, not just accepted. They are the only channel a
        # per-source credential can travel on (`oauth_credentials` points at the
        # source and is RLS-scoped to the workspace), so a fake that swallowed
        # them would let the adapter resolve the wrong tenant's credential with
        # every test still green.
        self.calls.append(
            {
                "config": dict(config or {}),
                "checkpoint": checkpoint,
                "source_id": source_id,
                "workspace_id": workspace_id,
            }
        )
        step = self.pages.pop(0)
        if isinstance(step, Exception):
            raise step
        items, token = step
        return items, {"v": 1, **({"page_token": token} if token else {})}


#: What the real adapter emits for each `kind`. Kept beside `_item` so the
#: fixture's mime and its kind cannot drift into disagreeing.
_MIME_FOR = {"image": "image/jpeg", "video": "video/mp4"}


def _item(ref, h=None, kind="image", name=None, mime=_UNSET):
    """One `list_changes` item. `mime=None` models an adapter that cannot
    state a content type; the default models the Drive adapter, which always
    can (`_kind_for` refuses the entry otherwise)."""
    item = {
        "ref": ref,
        "name": name if name is not None else f"{ref}.jpg",
        "kind": kind,
        "content_hash": h or f"hash-{ref}",
    }
    resolved = _MIME_FOR.get(kind) if mime is _UNSET else mime
    if resolved is not None:
        item["mime_type"] = resolved
    return item


async def _run_once_w6(lane_db, drive):
    engine = create_async_engine(_async_url(lane_db))
    try:
        app = compose(engine=engine, config=WorkerConfig(), env={}, drive=drive)
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


class TestBaselineSyncEndToEnd:
    @pytest.mark.asyncio
    async def test_mint_list_upsert_checkpoint_and_rearm(self, lane_db, sync_conn):
        chain = seed_workspace_chain(sync_conn, "w6-base")
        _arm_source(sync_conn, chain["src"])
        _tick(sync_conn)
        minted = _jobs(sync_conn, "sync_media_source")
        assert len(minted) == 1 and minted[0]["payload"]["reason"] == "baseline"
        assert _source_row(sync_conn, chain["src"])["next_sync_at"] is None, (
            "leg 4 nulls at mint; only the executor re-arms"
        )

        drive = ScriptedDrive([([_item("f1"), _item("f2")], None)])
        wl, claimed = await _run_once_w6(lane_db, drive)
        assert claimed is True and wl.processed == 1

        rows = _items(sync_conn, chain["ws"])
        refs = [r[0] for r in rows]
        assert "f1" in refs and "f2" in refs
        src = _source_row(sync_conn, chain["src"])
        assert src["state"] == "active"
        assert src["last_sync_success_at"] is not None
        assert src["sync_checkpoint"] == {"v": 1}
        assert src["next_sync_at"] is not None, "the executor must re-arm"
        eta = (src["next_sync_at"] - datetime.now(timezone.utc)).total_seconds()
        assert 5 * 3600 < eta < 8 * 3600, (
            f"re-arm must be the 05 baseline (6h jittered), got {eta}s"
        )
        assert _jobs(sync_conn, "sync_media_source")[0]["state"] == "succeeded"
        assert drive.calls[0]["checkpoint"] is None or drive.calls[0]["checkpoint"] == {
            "v": 1
        }

    @pytest.mark.asyncio
    async def test_the_workspace_hash_dedup_is_respected_not_violated(
        self, lane_db, sync_conn
    ):
        """A second sync offering the same content hash must not crash on
        uq_media_dedup and must not mint a second row."""
        chain = seed_workspace_chain(sync_conn, "w6-dedup")
        _arm_source(sync_conn, chain["src"])
        _tick(sync_conn)
        drive = ScriptedDrive([([_item("d1", h="samehash")], None)])
        await _run_once_w6(lane_db, drive)

        _arm_source(sync_conn, chain["src"])
        _tick(sync_conn)
        drive2 = ScriptedDrive([([_item("d1-renamed", h="samehash")], None)])
        wl, _ = await _run_once_w6(lane_db, drive2)
        assert wl.processed == 1
        hashes = [r[1] for r in _items(sync_conn, chain["ws"])]
        assert hashes.count("samehash") == 1, "per-workspace dedup by schema"
        assert _jobs(sync_conn, "sync_media_source")[-1]["state"] == "succeeded"


class TestTheProviderContentTypeReachesTheColumn:
    """`media_items.mime_type` exists in `054` and the media read already
    serves it. The seam derived `kind` from `mimeType` and then dropped it, so
    the column was NULL for every provider-sourced row and recoverable only by
    re-listing the folder.

    Asserted at the DATABASE, not at the adapter: carrying the key one function
    further and dropping it at the INSERT looks identical from the seam's side.
    """

    @pytest.mark.asyncio
    async def test_the_mime_lands_on_a_row_whose_name_has_no_extension(
        self, lane_db, sync_conn
    ):
        """The fixture is extensionless BY DESIGN — this is the whole trap.

        Every other item in this gate is named `<ref>.jpg`, and a row like that
        passes whether or not the content type survived, because the filename
        carries the answer anyway. A Drive name is whatever a person typed and
        need not have a suffix, so this is the row that can only be classified
        from what the provider said.
        """
        chain = seed_workspace_chain(sync_conn, "w6-mime")
        _arm_source(sync_conn, chain["src"])
        _tick(sync_conn)

        drive = ScriptedDrive(
            [
                (
                    [
                        _item(
                            "f1",
                            kind="video",
                            name="sunset clip",
                            mime="video/quicktime",
                        )
                    ],
                    None,
                )
            ]
        )
        wl, claimed = await _run_once_w6(lane_db, drive)
        assert claimed is True and wl.processed == 1

        row = _media_row(sync_conn, chain["ws"], "f1")
        assert row is not None, "the row must exist before its columns mean anything"
        assert row["mime"] == "video/quicktime"
        assert row["kind"] == "video"
        # The control: nothing about this stored name could have produced that
        # kind, so the value can only have come from the provider.
        assert row["file_name"] == "sunset clip"
        assert Path(row["file_name"]).suffix == ""

    @pytest.mark.asyncio
    async def test_an_adapter_that_states_no_content_type_leaves_it_null(
        self, lane_db, sync_conn
    ):
        """The column is nullable and the port does not require the key. An
        adapter that cannot know the type must be able to say so and still
        write a row — absent is NULL, which is exactly the row this wrote
        before, rather than a crash or an invented default."""
        chain = seed_workspace_chain(sync_conn, "w6-mime-absent")
        _arm_source(sync_conn, chain["src"])
        _tick(sync_conn)

        drive = ScriptedDrive([([_item("f2", mime=None)], None)])
        wl, claimed = await _run_once_w6(lane_db, drive)
        assert claimed is True and wl.processed == 1

        row = _media_row(sync_conn, chain["ws"], "f2")
        assert row is not None
        assert row["mime"] is None
        assert row["kind"] == "image"


class TestChunkChaining:
    @pytest.mark.asyncio
    async def test_pages_chain_first_ingest_chunk_and_the_last_rearms(
        self, lane_db, sync_conn
    ):
        chain = seed_workspace_chain(sync_conn, "w6-chain")
        _arm_source(sync_conn, chain["src"])
        _tick(sync_conn)

        drive = ScriptedDrive(
            [
                ([_item("p1")], "tok-2"),
                ([_item("p2")], "tok-3"),
                ([_item("p3")], None),
            ]
        )
        wl, _ = await _run_once_w6(lane_db, drive)
        assert wl.processed == 1
        chunks = _jobs(sync_conn, "first_ingest_chunk")
        assert len(chunks) == 1 and chunks[0]["payload"]["page_token"] == "tok-2", (
            "a page remaining must chain a first_ingest_chunk with the token"
        )
        assert _source_row(sync_conn, chain["src"])["next_sync_at"] is None, (
            "mid-chain must NOT re-arm — the chain is the carrier"
        )

        wl, _ = await _run_once_w6(lane_db, drive)
        chunks = _jobs(sync_conn, "first_ingest_chunk")
        assert len(chunks) == 2 and chunks[1]["payload"]["page_token"] == "tok-3"

        wl, _ = await _run_once_w6(lane_db, drive)
        src = _source_row(sync_conn, chain["src"])
        assert src["next_sync_at"] is not None, "the LAST chunk re-arms"
        assert src["last_sync_success_at"] is not None
        refs = [r[0] for r in _items(sync_conn, chain["ws"])]
        assert {"p1", "p2", "p3"} <= set(refs)
        assert {j["state"] for j in _jobs(sync_conn, "first_ingest_chunk")} == {
            "succeeded"
        }


def _credential(conn, ws, src, state="active"):
    """A gdrive credential row, so `connect_purpose` reports `reconnect`."""
    with conn.cursor() as cur:
        cur.execute("SET app.actor_kind = 'migration'")
        cur.execute(
            "INSERT INTO oauth_credentials (workspace_id, media_source_id, provider,"
            " encrypted_payload, state) VALUES (%s, %s, 'gdrive', %s, %s)",
            (ws, src, "ciphertext-placeholder", state),
        )
    conn.commit()


async def _command(lane_db, kind, chain, **args):
    """Run one command executor against the real database."""
    from src.services.target import command_executors as ex
    from src.services.target.commands import Command
    from src.services.target.unit_of_work import unit_of_work

    # A UoW, not a bare session: `trg_governance_audit` REFUSES an anonymous
    # write to oauth_credentials, so a bare session does not merely skip the
    # GUCs — it cannot execute the statement at all. Running the executor any
    # other way would test a path production never takes.
    engine = create_async_engine(_async_url(lane_db))
    try:
        uow = unit_of_work(
            engine,
            str(chain["ws"]),
            actor_kind="user",
            actor_user_id=str(chain["user"]),
            channel="web",
        )
        async with uow.begin() as session:
            cmd = Command(
                kind=kind,
                workspace_id=str(chain["ws"]),
                actor_user_id=str(chain["user"]),
                channel="web",
                args={k: str(v) for k, v in args.items()},
            )
            result = await getattr(ex, kind)(session, cmd)
        return result
    finally:
        await engine.dispose()


async def _rearm(lane_db, workspace_id, source_id) -> bool:
    """The P4 re-arm against the real database."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.services.target import media_sync

    engine = create_async_engine(_async_url(lane_db))
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            moved = await media_sync.rearm_after_connect(
                session, workspace_id=str(workspace_id), source_id=str(source_id)
            )
            await session.commit()
        return moved
    finally:
        await engine.dispose()


async def _sweep(lane_db, *, age_seconds=0, limit=200) -> int:
    """Run the #1061 re-alert beat against the real database."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.services.target import media_sync

    engine = create_async_engine(_async_url(lane_db))
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            n = await media_sync.alert_stranded_sources(
                session, stale_after_seconds=age_seconds, limit=limit
            )
            await session.commit()
        return n
    finally:
        await engine.dispose()


def _bind(conn, workspace_id) -> str:
    binding = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("SET app.actor_kind = 'migration'")
        cur.execute(
            "INSERT INTO channel_bindings (id, workspace_id, channel, external_ref)"
            " VALUES (%s, %s, 'telegram_group', %s)",
            (binding, workspace_id, "-100" + uuid.uuid4().hex[:9]),
        )
    conn.commit()
    return binding


def _notifications(conn, binding) -> list:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT payload FROM channel_outbox"
            " WHERE binding_id = %s AND kind = 'notification'",
            (binding,),
        )
        return [r[0] for r in cur.fetchall()]


def _strand(conn, source_id, *, state="error", alerted="now()"):
    """Put a source in a stranded shape directly, for the cases the real
    failure path cannot reach (an old `alerted_at`, `paused`, a NULL stamp)."""
    with conn.cursor() as cur:
        cur.execute("SET app.actor_kind = 'migration'")
        cur.execute(
            f"UPDATE media_sources SET state = %s, alerted_at = {alerted}"
            " WHERE id = %s",
            (state, source_id),
        )
    conn.commit()


class TestAStrandedSourceKeepsSayingSo:
    """#1061 — the self-silencing stranded source.

    The persistent branch alerts once. Recovery to `active` happens only on a
    successful sync, and the clock enqueues only `active` sources, so the
    branch never runs again and no second alert ever fires. Stuckness caused
    the silence. These prove the mouth re-opens WITHOUT the stuckness being
    touched: nothing here re-arms, and the F4 connect flow keeps that job.
    """

    @pytest.mark.asyncio
    async def test_the_defect_and_the_fix_in_one_pass(self, lane_db, sync_conn):
        """Reaches `error` through the REAL failure path, not a hand-UPDATE.

        The first two assertions ARE the defect: after the failure the clock
        mints nothing for this source ever again, so the branch that alerts
        cannot run a second time. The third is the fix.
        """
        from src.services.target.media_sync import DriveSourceGone

        chain = seed_workspace_chain(sync_conn, "w6-strand")
        binding = _bind(sync_conn, chain["ws"])
        _arm_source(sync_conn, chain["src"])
        _tick(sync_conn)
        await _run_once_w6(lane_db, ScriptedDrive([DriveSourceGone("folder deleted")]))

        assert _source_row(sync_conn, chain["src"])["state"] == "error"
        assert len(_notifications(sync_conn, binding)) == 1, "the one alert it gets"

        # The stuckness, asserted rather than described: the clock will not
        # schedule this source again, so nothing can re-enter the alert branch.
        before = len(_jobs(sync_conn, "sync_media_source"))
        _tick(sync_conn)
        assert len(_jobs(sync_conn, "sync_media_source")) == before, (
            "an error source is never re-enqueued — this is why it fell silent"
        )

        # The fix: a beat that does not depend on the source being scheduled.
        assert await _sweep(lane_db, age_seconds=0) == 1
        assert len(_notifications(sync_conn, binding)) == 2

    @pytest.mark.asyncio
    async def test_the_sweep_re_alerts_but_does_not_re_arm(self, lane_db, sync_conn):
        """The scope fence, as an assertion.

        Re-arming is fork F4 (a) and belongs to the connect flow. If this beat
        ever set `state` or `next_sync_at`, two things would be racing to
        revive one row — which is the reason the halves were split.
        """
        chain = seed_workspace_chain(sync_conn, "w6-noarm")
        _bind(sync_conn, chain["ws"])
        _strand(sync_conn, chain["src"], alerted="now() - interval '30 days'")
        jobs_before = len(_jobs(sync_conn, "sync_media_source"))

        assert await _sweep(lane_db, age_seconds=3600) == 1

        src = _source_row(sync_conn, chain["src"])
        assert src["state"] == "error", "still errored — the beat does not revive"
        assert src["next_sync_at"] is None, "and does not re-arm the clock"
        assert len(_jobs(sync_conn, "sync_media_source")) == jobs_before, (
            "no sync enqueued: this path makes no provider call"
        )

    @pytest.mark.asyncio
    async def test_a_recent_alert_is_not_repeated_until_the_bound_passes(
        self, lane_db, sync_conn
    ):
        """`alerted_at` becomes a real dedup bound. Today it is stamped and
        never read as one, which is only invisible because the branch that
        stamps it runs at most once."""
        chain = seed_workspace_chain(sync_conn, "w6-bound")
        binding = _bind(sync_conn, chain["ws"])
        _strand(sync_conn, chain["src"], alerted="now()")

        assert await _sweep(lane_db, age_seconds=3600) == 0
        assert _notifications(sync_conn, binding) == []

        _strand(sync_conn, chain["src"], alerted="now() - interval '2 hours'")
        assert await _sweep(lane_db, age_seconds=3600) == 1
        assert len(_notifications(sync_conn, binding)) == 1

    @pytest.mark.asyncio
    async def test_a_source_stranded_before_this_existed_is_picked_up(
        self, lane_db, sync_conn
    ):
        """`alerted_at` is nullable and rows written by paths that never
        stamped it are the realistic case. NULL must mean overdue, not
        skip-forever — the same silence in a different column."""
        chain = seed_workspace_chain(sync_conn, "w6-null")
        binding = _bind(sync_conn, chain["ws"])
        _strand(sync_conn, chain["src"], alerted="NULL")

        assert await _sweep(lane_db, age_seconds=3600) == 1
        assert len(_notifications(sync_conn, binding)) == 1
        assert _source_row(sync_conn, chain["src"])["alerted_at"] is not None

    @pytest.mark.asyncio
    async def test_paused_is_the_acknowledgement_and_it_is_silent(
        self, lane_db, sync_conn
    ):
        """Why this is not the recurring noise F4 (b) was rejected for.

        A source dead on purpose has a way to say so — `ck_sources_state`
        already admits `paused`. Silence then means somebody CHOSE it, which
        is exactly the property the current behaviour destroys by making a
        stranded source and an acknowledged one look identical.
        """
        chain = seed_workspace_chain(sync_conn, "w6-paused")
        binding = _bind(sync_conn, chain["ws"])
        _strand(sync_conn, chain["src"], state="paused", alerted="NULL")

        assert await _sweep(lane_db, age_seconds=0) == 0
        assert _notifications(sync_conn, binding) == []

    @pytest.mark.asyncio
    async def test_a_row_the_connect_flow_cleared_is_never_alerted(
        self, lane_db, sync_conn
    ):
        """The F4 seam.

        The connect flow re-arms in one transaction: `state='active'`,
        `alerted_at=NULL`, `next_sync_at=now()`. That shape is what this beat
        must not fire against — and `alerted_at IS NULL` is precisely the
        predicate the previous test relies on, so the two could collide if the
        state filter were ever dropped. Both are asserted so neither can be
        loosened alone.
        """
        chain = seed_workspace_chain(sync_conn, "w6-f4")
        binding = _bind(sync_conn, chain["ws"])
        _strand(sync_conn, chain["src"], state="active", alerted="NULL")
        _arm_source(sync_conn, chain["src"])

        assert await _sweep(lane_db, age_seconds=0) == 0
        assert _notifications(sync_conn, binding) == []

    @pytest.mark.asyncio
    async def test_the_f4_reconnect_wins_the_row_and_leaves_no_stale_stamp(
        self, lane_db, sync_conn
    ):
        """The F4 seam, exercised as a real interleaving rather than argued.

        astrid's P3 re-arms in the SAME transaction as the credential write:
        `state='active'`, `alerted_at=NULL`, `next_sync_at=now()`. This beat
        re-alerts on `error` rows. The two touch one row, so the ordering has
        to be demonstrated, not reasoned about — and P3 is in draft, so this
        stands in for it with the statement it will issue.

        Asserted, in order:

        1. While the sweep's transaction is open, P3's UPDATE **blocks** — the
           single `UPDATE … RETURNING` took the row lock, so the two cannot
           interleave mid-decision. That is what makes the shape safe rather
           than the timing.
        2. After the sweep commits, P3 proceeds and **wins the row**: `active`
           and `alerted_at IS NULL`. No stale stamp survives a reconnect, so
           the next strand is not silently deduped against this alert.
        3. Exactly ONE notification exists — the bounded staleness the module
           docstring discloses. It is one message naming a state the source
           was in moments earlier, not an ongoing wrong alert.
        4. A subsequent sweep alerts NOTHING, because the row is no longer in
           `error`. The reconnect actually stops the beat.
        """
        import psycopg2.extensions
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from src.services.target import media_sync

        chain = seed_workspace_chain(sync_conn, "w6-f4-race")
        binding = _bind(sync_conn, chain["ws"])
        _strand(sync_conn, chain["src"], alerted="NULL")

        # P3's statement, verbatim in shape: re-arm in one transaction.
        rearm = (
            "UPDATE media_sources"
            " SET state = 'active', alerted_at = NULL, next_sync_at = now()"
            " WHERE id = %s"
        )

        engine = create_async_engine(_async_url(lane_db))
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                alerted = await media_sync.alert_stranded_sources(
                    session, stale_after_seconds=0, limit=10
                )
                assert alerted == 1

                # (1) P3 cannot proceed while the sweep holds the row.
                blocked = psycopg2.connect(lane_db)
                try:
                    with blocked.cursor() as cur:
                        cur.execute("SET app.actor_kind = 'migration'")
                        cur.execute("SET statement_timeout = '1500ms'")
                        with pytest.raises(psycopg2.errors.QueryCanceled):
                            cur.execute(rearm, (chain["src"],))
                finally:
                    blocked.rollback()
                    blocked.close()

                await session.commit()
        finally:
            await engine.dispose()

        # (2) Now P3 runs and wins the row.
        with sync_conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            cur.execute(rearm, (chain["src"],))
        sync_conn.commit()

        src = _source_row(sync_conn, chain["src"])
        assert src["state"] == "active"
        assert src["alerted_at"] is None, (
            "a reconnect must clear the stamp, or the next strand dedups"
            " against an alert about the previous one"
        )

        # (3) exactly one message, and (4) the beat stops.
        assert len(_notifications(sync_conn, binding)) == 1
        assert await _sweep(lane_db, age_seconds=0) == 0
        assert len(_notifications(sync_conn, binding)) == 1

    @pytest.mark.asyncio
    async def test_only_the_stranded_workspace_is_told(self, lane_db, sync_conn):
        """A second workspace with its own binding hears nothing. Two, not one,
        for the same reason every tenancy assertion needs two: "it went to A"
        is only a claim if there was a B it could have gone to."""
        a = seed_workspace_chain(sync_conn, "w6-tenant-a")
        b = seed_workspace_chain(sync_conn, "w6-tenant-b")
        bind_a = _bind(sync_conn, a["ws"])
        bind_b = _bind(sync_conn, b["ws"])
        _strand(sync_conn, a["src"], alerted="NULL")

        assert await _sweep(lane_db, age_seconds=0) == 1
        assert len(_notifications(sync_conn, bind_a)) == 1
        assert _notifications(sync_conn, bind_b) == []


class TestTheConnectReArmMeetsTheAlertingFix:
    """gdrive epic P4 (F4 (a)) against #1061's beat — the two halves meeting.

    #1069 stopped a stranded source going silent. This is what lets it
    recover. ari asked for both directions checked now that the re-arm is real
    code rather than a docstring describing P4 as forthcoming.
    """

    @pytest.mark.asyncio
    async def test_the_re_arm_makes_the_clock_see_the_source_again(
        self, lane_db, sync_conn
    ):
        """Asserted against the CLOCK, not against the UPDATE.

        The UPDATE is the thing that already existed in spirit; what was broken
        is that no source ever became DUE. `state` alone would not fix it —
        leg 4's predicate is a conjunction and `next_sync_at` has no default —
        so the only assertion that means anything is a real `fn_clock_tick`
        minting a job for this source.
        """
        chain = seed_workspace_chain(sync_conn, "p4-rearm")
        _strand(sync_conn, chain["src"], alerted="now()")
        _tick(sync_conn)
        assert _jobs(sync_conn, "sync_media_source") == [], (
            "before-state: an errored source is invisible to the clock"
        )

        assert await _rearm(lane_db, chain["ws"], chain["src"]) is True

        row = _source_row(sync_conn, chain["src"])
        assert row["state"] == "active"
        assert row["alerted_at"] is None
        assert row["next_sync_at"] is not None, (
            "the cursor is the half that was missing"
        )

        _tick(sync_conn)
        minted = _jobs(sync_conn, "sync_media_source")
        assert len(minted) == 1, "the clock can see it now — this is the whole point"
        assert minted[0]["payload"]["source_id"] == str(chain["src"])

    @pytest.mark.asyncio
    async def test_the_beat_cannot_re_alert_a_row_the_re_arm_just_cleared(
        self, lane_db, sync_conn
    ):
        """ari's question 1, now answerable against real code.

        The beat scans `state = 'error'`. The re-arm moves the row out of that
        set and NULLs the stamp in one statement, so there is no window where
        a recovered source is still alertable — not because the two are timed,
        but because neither can observe the other mid-decision.
        """
        chain = seed_workspace_chain(sync_conn, "p4-seam")
        binding = _bind(sync_conn, chain["ws"])
        _strand(sync_conn, chain["src"], alerted="now() - interval '30 days'")

        assert await _sweep(lane_db, age_seconds=3600) == 1, "overdue before recovery"
        assert len(_notifications(sync_conn, binding)) == 1

        assert await _rearm(lane_db, chain["ws"], chain["src"]) is True

        assert await _sweep(lane_db, age_seconds=0) == 0, "recovered — nothing to say"
        assert len(_notifications(sync_conn, binding)) == 1, "no second alert"

    @pytest.mark.asyncio
    async def test_a_reconnect_leaves_no_orphaned_alert_job(self, lane_db, sync_conn):
        """ari's question 2, re-verified now the re-arm is real.

        The beat is a SINGLETON — leg 1 keys it on the kind itself, one row for
        the estate with `workspace_id NULL` — so there has never been a
        per-source alert job that a recovery could orphan. Asserted rather than
        restated: after a re-arm, no job anywhere names this source except the
        sync the clock legitimately minted.
        """
        chain = seed_workspace_chain(sync_conn, "p4-orphan")
        _strand(sync_conn, chain["src"], alerted="NULL")
        await _sweep(lane_db, age_seconds=0)
        await _rearm(lane_db, chain["ws"], chain["src"])

        with sync_conn.cursor() as cur:
            cur.execute(
                "SELECT kind, workspace_id, payload FROM jobs"
                " WHERE kind = 'alert_stranded_sources'"
            )
            alert_jobs = cur.fetchall()
        for kind, ws, payload in alert_jobs:
            assert ws is None, f"{kind} must be a system singleton, not per-workspace"
            assert "source_id" not in (payload or {}), (
                "a per-source alert job would be orphaned by this recovery;"
                " the singleton carries no source and cannot be"
            )

    @pytest.mark.asyncio
    async def test_a_paused_source_is_re_armed_too(self, lane_db, sync_conn):
        """A reconnect is the explicit undo of a deliberate disconnect (F5 (a)
        leaves the source `paused`), so the re-arm must not filter on `error`.
        A state filter would no-op on exactly the case a user is reversing."""
        chain = seed_workspace_chain(sync_conn, "p4-paused")
        _strand(sync_conn, chain["src"], state="paused", alerted="NULL")

        assert await _rearm(lane_db, chain["ws"], chain["src"]) is True
        assert _source_row(sync_conn, chain["src"])["state"] == "active"

    @pytest.mark.asyncio
    async def test_the_re_arm_is_bound_to_its_tenant(self, lane_db, sync_conn):
        """RLS is inert under the deployed owner role (#751), so the WHERE
        clause is what binds the row to its workspace. Another tenant's id must
        move nothing — and the row must be left exactly as it was."""
        a = seed_workspace_chain(sync_conn, "p4-tenant-a")
        b = seed_workspace_chain(sync_conn, "p4-tenant-b")
        _strand(sync_conn, a["src"], alerted="now()")

        assert await _rearm(lane_db, b["ws"], a["src"]) is False
        row = _source_row(sync_conn, a["src"])
        assert row["state"] == "error" and row["next_sync_at"] is None


class TestTheConnectTrioIsThinAndTenantBound:
    """gdrive epic P4, F1 (a): the trio is provider-general and the executor is
    the chat-side door that INITIATES and RECORDS. Exercised against the real
    database rather than a stand-in session, because every refusal below is a
    property of the SQL — a fake session would agree with whatever was written.
    """

    @pytest.mark.asyncio
    async def test_disconnect_revokes_the_credential_and_pauses_the_source(
        self, lane_db, sync_conn
    ):
        """F5 (a): revoke, KEEP the row, pause. `paused` not `error` — a
        disconnect is a decision, not a fault."""
        chain = seed_workspace_chain(sync_conn, "p4-disc")
        _credential(sync_conn, chain["ws"], chain["src"])

        res = await _command(
            lane_db, "disconnect_account", chain, source_id=chain["src"]
        )
        assert res.outcome == "executed"

        assert _source_row(sync_conn, chain["src"])["state"] == "paused"
        with sync_conn.cursor() as cur:
            cur.execute(
                "SELECT state FROM oauth_credentials WHERE media_source_id = %s",
                (chain["src"],),
            )
            rows = cur.fetchall()
        assert rows == [("revoked",)], (
            "the row is KEPT — the FK cascade would erase audit"
        )

    @pytest.mark.asyncio
    async def test_a_disconnected_source_is_silent_to_the_alerting_beat(
        self, lane_db, sync_conn
    ):
        """Why F5 (a) chose `paused` and why it matters to #1061: the beat
        scans `error`. A user who disconnects on purpose must not then be
        alerted daily about it — that is how a real alert gets silenced."""
        chain = seed_workspace_chain(sync_conn, "p4-disc-quiet")
        _bind(sync_conn, chain["ws"])
        _credential(sync_conn, chain["ws"], chain["src"])

        await _command(lane_db, "disconnect_account", chain, source_id=chain["src"])
        assert await _sweep(lane_db, age_seconds=0) == 0

    @pytest.mark.asyncio
    async def test_another_tenants_source_is_refused_by_name(self, lane_db, sync_conn):
        """RLS is inert under the deployed owner role (#751), so the executor's
        own WHERE is the tenant bind. A source id is a caller-supplied UUID."""
        a = seed_workspace_chain(sync_conn, "p4-x-a")
        b = seed_workspace_chain(sync_conn, "p4-x-b")
        _credential(sync_conn, a["ws"], a["src"])

        with pytest.raises(CommandRefused) as exc:
            await _command(lane_db, "disconnect_account", b, source_id=a["src"])
        assert exc.value.reason == "not_found"
        assert _source_row(sync_conn, a["src"])["state"] == "active", "untouched"

    @pytest.mark.asyncio
    async def test_connect_on_a_credentialed_source_is_refused_not_silently_reconnected(
        self, lane_db, sync_conn
    ):
        """The split is the SCHEMA's answer, not the caller's. A reconnect run
        as a connect would skip `issue_state`'s invalidate-prior-states step
        (`07` §2, last-issued-wins) and leave two live callbacks for one
        source."""
        chain = seed_workspace_chain(sync_conn, "p4-wrongway")
        _credential(sync_conn, chain["ws"], chain["src"])

        with pytest.raises(CommandRefused) as exc:
            await _command(lane_db, "connect_account", chain, source_id=chain["src"])
        assert exc.value.reason == "illegal_transition"

    @pytest.mark.asyncio
    async def test_the_port_REFUSES_to_start_a_browser_flow_it_cannot_bind(
        self, lane_db, sync_conn
    ):
        """**The port REFUSES to start this flow, and that is the fix, not a
        regression.**

        This executor used to mint the state row. It cannot any more: a
        `connect` state is only safe if the callback can prove it is the same
        browser that started it, that proof is a nonce whose hash the row
        pins, and a nonce is a transport value -- which `Command` is documented
        never to carry ("Resolved ids only -- never a chat id, a session value
        or a transport payload"). A channel-agnostic port has nothing to bind
        to, so minting here could only ever produce a state that `consume_state`
        must refuse.

        Without the refusal the executor minted an UNBOUND state: a legitimate
        admin could pass the resulting link to someone else and receive their
        publish-capable credential. The web route
        `POST /workspaces/{ws}/sources/{source_id}/connect` owns this flow --
        it serves both purposes, it is what the dashboard calls, and it is
        where a cookie can be set.

        Asserted on the ROW as well as the refusal: a refusal that still left
        a state behind would be the same defect wearing an error message.
        """
        chain = seed_workspace_chain(sync_conn, "p4-mint")

        with pytest.raises(CommandRefused) as exc:
            await _command(lane_db, "connect_account", chain, source_id=chain["src"])
        assert exc.value.reason == "illegal_transition"
        assert "session binding" in str(exc.value)

        with sync_conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM oauth_states"
                " WHERE workspace_id = %s AND reconnect_target = %s",
                (chain["ws"], chain["src"]),
            )
            assert cur.fetchone()[0] == 0, "a refused connect must mint nothing"


class TestFailureRouting:
    @pytest.mark.asyncio
    async def test_transient_failure_rides_the_ladder_and_arms_nothing(
        self, lane_db, sync_conn
    ):
        chain = seed_workspace_chain(sync_conn, "w6-flaky")
        _arm_source(sync_conn, chain["src"])
        _tick(sync_conn)
        drive = ScriptedDrive([RuntimeError("network blip")])
        wl, claimed = await _run_once_w6(lane_db, drive)
        assert claimed is True
        src = _source_row(sync_conn, chain["src"])
        assert src["state"] == "active"
        assert src["next_sync_at"] is None, "the alive job is the carrier"
        job = _jobs(sync_conn, "sync_media_source")[0]
        assert job["state"] == "ready", "on the ladder, not dead, not done"

    @pytest.mark.asyncio
    async def test_persistent_failure_flips_error_alerts_once_and_succeeds(
        self, lane_db, sync_conn
    ):
        from src.services.target.media_sync import DriveSourceGone

        chain = seed_workspace_chain(sync_conn, "w6-gone")
        binding = str(uuid.uuid4())
        with sync_conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            cur.execute(
                "INSERT INTO channel_bindings (id, workspace_id, channel, external_ref)"
                " VALUES (%s, %s, 'telegram_group', '-100666001')",
                (binding, chain["ws"]),
            )
        sync_conn.commit()
        _arm_source(sync_conn, chain["src"])
        _tick(sync_conn)
        drive = ScriptedDrive([DriveSourceGone("folder deleted")])
        wl, _ = await _run_once_w6(lane_db, drive)
        assert wl.processed == 1
        src = _source_row(sync_conn, chain["src"])
        assert src["state"] == "error", "persistent failure is the error state"
        assert src["alerted_at"] is not None
        with sync_conn.cursor() as cur:
            cur.execute(
                "SELECT payload FROM channel_outbox"
                " WHERE binding_id = %s AND kind = 'notification'",
                (binding,),
            )
            rows = cur.fetchall()
        assert len(rows) == 1 and "sync" in rows[0][0]["text"].lower()
        assert _jobs(sync_conn, "sync_media_source")[0]["state"] == "succeeded", (
            "a classified persistent failure is handled work, not a retry loop"
        )

    @pytest.mark.asyncio
    async def test_an_error_source_recovers_to_active_on_a_successful_sync(
        self, lane_db, sync_conn
    ):
        """`02` §2: error → active on successful sync — the sync IS the probe."""
        chain = seed_workspace_chain(sync_conn, "w6-probe")
        with sync_conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            cur.execute(
                "UPDATE media_sources SET state = 'error', alerted_at = now()"
                " WHERE id = %s",
                (chain["src"],),
            )
            cur.execute(
                "INSERT INTO jobs (kind, workspace_id, lane, serialization_key,"
                " run_at, max_attempts, payload)"
                " VALUES ('sync_media_source', %s, 'bulk', %s, now(), 5, %s::jsonb)",
                (
                    chain["ws"],
                    f"src:{chain['src']}",
                    json.dumps(
                        {"v": 1, "source_id": str(chain["src"]), "reason": "demand"}
                    ),
                ),
            )
        sync_conn.commit()
        drive = ScriptedDrive([([_item("back")], None)])
        wl, _ = await _run_once_w6(lane_db, drive)
        assert wl.processed == 1
        src = _source_row(sync_conn, chain["src"])
        assert src["state"] == "active", "recovery is the successful sync itself"
        assert src["alerted_at"] is None, "recovery clears the alert dedup"
