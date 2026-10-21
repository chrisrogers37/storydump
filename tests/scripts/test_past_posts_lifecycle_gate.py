"""Past-posts lifecycle through the epoch writer, under the deployed roles (#1644).

Account removal, the sources DELETE route, Meta's deauthorize, credential
refresh, `sync_now` through the command port and offboarding: each goes
through the common source lock order and the single epoch writer. The API's
paths run as `svc_ingress`, the worker's as `svc_worker`. Facts are read back
as the tenant. Providers are fakes; the database is not.

Two cases cross a lifecycle event with work already under way. A publish waits
at each rung of its ladder (staged, container made, publish requested) across a
routine token refresh, which changes nothing, and across a reauthorization,
which takes the rung's staged copy and container away. And a revocation lands
while a past-posts read's Graph listing is in flight, which must land nothing.
The publish cases drive the real pipeline over the selected-media validator,
which runs only on Linux.

The lock-order cases hold one credential row in another transaction, wait
until the path under test is blocked on it, and then look at what it already
holds. The canonical order puts the workspace's source prefix (its advisory
lock and its account rows) before any credential row, so the prefix must
already be held at that point.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import httpx
import psycopg2
import psycopg2.errors
import psycopg2.extras
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.api.app import create_app
from src.config.settings import settings
from src.services.target import (
    past_posts_lifecycle,
    category_mix,
    commands,
    publish_pipeline,
)
from src.services.target.meta_adapter import StubMetaAdapter
from src.services.target.unit_of_work import unit_of_work
from tests.scripts import test_past_posts_preview_gate as preview_gate
from tests.scripts import test_instagram_observations_gate as observations
from tests.scripts import test_instagram_publish_gate as publish_gate
from tests.scripts import test_instagram_sources_gate as foundation
from tests.scripts import test_instagram_sync_gate as sync_gate
from tests.scripts import test_source_read_admission_gate as admission
from tests.scripts import test_versioned_mix_gate as mix_gate
from tests.scripts.conftest import (
    as_user,
    async_url,
    seed_workspace_chain,
    set_test_passwords,
)

pytestmark = [pytest.mark.integration]

origin = foundation.origin
#: The API's own login.
ingress = preview_gate.ingress

APP_ID = "4242"
APP = f"instagram:{APP_ID}"
SUBJECT = "1234567890"
STRANGER = "5550001"


@pytest.fixture
async def worker(world_db, admin_conn):
    """The worker's own login."""
    set_test_passwords(admin_conn)
    engine = create_async_engine(async_url(as_user(world_db, "svc_worker")))
    try:
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT current_user"))).scalar_one() == (
                "svc_worker"
            )
        yield engine
    finally:
        await engine.dispose()


# --- reading the world back, as the tenant --------------------------------------


def uow(engine, chain):
    return unit_of_work(engine, str(chain["ws"]), actor_kind="system").begin()


def person(engine, chain):
    """The owner's own unit of work, as the web's commands open it."""
    return unit_of_work(
        engine,
        str(chain["ws"]),
        actor_kind="user",
        actor_user_id=str(chain["user"]),
        channel="web",
    ).begin()


async def fact(engine, chain, sql, **params):
    async with uow(engine, chain) as conn:
        return (await conn.execute(text(sql), params)).scalar_one()


async def epochs(engine, chain):
    """{source id: use epoch} for every source of the workspace."""
    async with uow(engine, chain) as conn:
        found = await conn.execute(
            text("SELECT id::text, source_use_epoch FROM media_sources")
        )
        return dict(found.all())


async def lifecycles(engine, chain):
    async with uow(engine, chain) as conn:
        found = await conn.execute(
            text("SELECT id::text, lifecycle FROM instagram_past_posts_generations")
        )
        return dict(found.all())


async def audit_reasons(engine, chain, sid):
    """What the epoch writer audited for this source, oldest first."""
    async with uow(engine, chain) as conn:
        found = await conn.execute(
            text(
                "SELECT detail->>'reason' FROM audit_events"
                " WHERE entity_kind='instagram_source' AND entity_id=:sid ORDER BY id"
            ),
            {"sid": sid},
        )
        return [reason for (reason,) in found.all()]


async def mix(engine, chain):
    """(revision, mode, {source id: ratio}) of the saved mix."""
    async with uow(engine, chain) as conn:
        saved = (
            await conn.execute(
                text("SELECT source_mix_revision, source_mix_mode FROM workspaces")
            )
        ).one()
        ratios = await conn.execute(
            text(
                "SELECT source_id::text, ratio FROM category_post_case_mix"
                " WHERE effective_to IS NULL"
            )
        )
        return saved[0], saved[1], dict(ratios.all())


async def credential_state(engine, chain):
    return await fact(
        engine,
        chain,
        "SELECT state FROM oauth_credentials WHERE id=:id",
        id=str(chain["credential"]),
    )


async def account_state(engine, chain):
    return await fact(
        engine,
        chain,
        "SELECT state FROM ig_accounts WHERE id=:id",
        id=str(chain["iga"]),
    )


# --- building the world ------------------------------------------------------


def authorization_at(at, authorization_id=None):
    from src.services.target.instagram_sources import PastPostsAuthorization

    return PastPostsAuthorization(
        authorization_id or str(uuid.uuid4()), APP_ID, "900000000000001", at
    )


def credentialed(sync_conn, name):
    """Another workspace with one account and its `ig_login` credential."""
    chain = seed_workspace_chain(sync_conn, name)
    with sync_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO oauth_credentials (workspace_id, ig_account_id, provider, encrypted_payload)"
            " VALUES (%s, %s, 'ig_login', 'test-ciphertext') RETURNING id",
            (chain["ws"], chain["iga"]),
        )
        chain["credential"] = cur.fetchone()[0]
    sync_conn.commit()
    return chain


def map_identity(sync_conn, chain, *, authorization_id, authorized_at, subject=SUBJECT):
    """A verified mapping, made current as the OAuth callback makes it."""
    with sync_conn.cursor() as cur:
        cur.execute(
            "UPDATE meta_identity_mappings SET is_current = false"
            " WHERE workspace_id = %s AND credential_id = %s AND is_current",
            (chain["ws"], chain["credential"]),
        )
        cur.execute(
            "INSERT INTO meta_identity_mappings (workspace_id, ig_account_id, credential_id,"
            " authorization_id, app_identity, subject_ref, account_ref, method, authorized_at,"
            " verified_at) VALUES (%s, %s, %s, %s, %s, %s, '900000000000001',"
            " 'oauth_exchange', %s, %s)",
            (
                chain["ws"],
                chain["iga"],
                chain["credential"],
                authorization_id,
                APP,
                subject,
                authorized_at,
                authorized_at,
            ),
        )
    sync_conn.commit()


async def converted_with_past_posts(engine, chain, tmp_path, *, on):
    """Two Drive folders at a custom 70/30, converted, then the account's past-posts source
    connected On (equal, revision 2) or Off (custom kept, revision 2)."""
    from src.services.target.source_membership import (
        PastPostsChoice,
        connect_past_posts,
    )

    ws = str(chain["ws"])
    async with uow(engine, chain) as conn:
        drives = await mix_gate.add(conn, chain, [".7", ".3"])
        await category_mix.convert_workspace(
            conn, workspace_id=ws, evidence=mix_gate.evidence()
        )
    async with uow(engine, chain) as conn:
        connected = await connect_past_posts(
            conn,
            workspace_id=ws,
            origin_ig_account_id=str(chain["iga"]),
            authorization=foundation.authorization(),
            had_prior_grant=False,
            choice=PastPostsChoice(on, 1),
            allocation_writer=category_mix.allocate_membership,
            capability=foundation.capability(),
        )
    assert connected.mix_revision == 2 and connected.history_enabled is on
    return drives, connected.source_id


async def disable(engine, chain):
    """The web's Remove, through the command port."""
    async with person(engine, chain) as session:
        return await commands.execute(
            session,
            commands.Command(
                kind="disable_account",
                workspace_id=str(chain["ws"]),
                actor_user_id=str(chain["user"]),
                channel="web",
                args={"ig_account_id": str(chain["iga"])},
            ),
        )


async def store_token(engine, chain, token):
    """A real ring-encrypted token on the chain's credential, in place."""
    from src.services.target import ig_login_oauth

    async with uow(engine, chain) as conn:
        stored = await ig_login_oauth.store_credential(
            conn,
            workspace_id=str(chain["ws"]),
            ig_account_id=str(chain["iga"]),
            token=token,
        )
    assert stored == str(chain["credential"])


async def refresh(engine, chain, door):
    """One `refresh_credential` job, with *door* as the provider."""
    from src.services.target import credential_lifecycle

    deps = SimpleNamespace(engine=engine, refresh=door)
    job = {
        "id": str(uuid.uuid4()),
        "workspace_id": str(chain["ws"]),
        "payload": {"v": 1, "credential_id": str(chain["credential"])},
    }
    return await credential_lifecycle.refresh_credential(deps, None, job)


async def rejecting(token):
    from src.services.target.credential_lifecycle import RefreshRejected

    raise RefreshRejected(401)


# --- lock-order probes (the owner's connections) ------------------------------


async def until_blocked(dsn, timeout=15.0):
    """Wait until some backend waits on a lock."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        conn = psycopg2.connect(dsn)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM pg_locks WHERE NOT granted")
                if cur.fetchone()[0]:
                    return
        finally:
            conn.close()
        await asyncio.sleep(0.05)
    raise AssertionError("the path under test never waited on the held row")


def prefix_held(dsn, ws) -> bool:
    """Whether another transaction holds the workspace's sources advisory lock."""
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"sources:{ws}",),
            )
            free = cur.fetchone()[0]
        conn.rollback()
        return not free
    finally:
        conn.close()


def row_locked(dsn, table, row_id) -> bool:
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    f"SELECT id FROM {table} WHERE id = %s FOR UPDATE NOWAIT",
                    (str(row_id),),
                )
                return False
            except psycopg2.errors.LockNotAvailable:
                return True
    finally:
        conn.rollback()
        conn.close()


@asynccontextmanager
async def holding_credential(engine, chain):
    async with uow(engine, chain) as holder:
        await holder.execute(
            text("SELECT id FROM oauth_credentials WHERE id = :id FOR UPDATE"),
            {"id": str(chain["credential"])},
        )
        yield


# --- account removal ---------------------------------------------------------


@pytest.mark.parametrize("on", [True, False], ids=["past_posts_on", "past_posts_off"])
async def test_removing_an_account_retires_its_past_posts_through_the_epoch_writer(
    ingress, origin, tmp_path, on
):
    """On: the enabled set changed, so the mix resets to equal. Off: the custom
    ratios survive. Either way the revision moves by one, only the past-posts source's
    epoch moves, and the destination half (account, credential, intents)
    follows in the same transaction."""
    ws = str(origin["ws"])
    drives, past_posts = await converted_with_past_posts(
        ingress, origin, tmp_path, on=on
    )
    if on:
        # Custom with the past-posts source On, so the reset the removal makes is visible.
        async with uow(ingress, origin) as conn:
            saved = await category_mix.mutate(
                conn,
                workspace_id=ws,
                body={
                    "protocol_version": 2,
                    "action": "save_custom",
                    "expected_revision": 2,
                    "rows": [
                        {"source_id": drives[0], "ratio": "0.5"},
                        {"source_id": drives[1], "ratio": "0.3"},
                        {"source_id": past_posts, "ratio": "0.2"},
                    ],
                },
                by_user_id=None,
            )
        assert (saved["revision"], saved["mode"]) == (3, "custom")
    revision, _, _ = await mix(ingress, origin)
    before = await epochs(ingress, origin)
    audited = await audit_reasons(ingress, origin, past_posts)
    generations = await lifecycles(ingress, origin)

    result = await disable(ingress, origin)

    assert result.outcome == "executed" and result.data["state"] == "disabled"
    after_revision, mode, ratios = await mix(ingress, origin)
    assert after_revision == revision + 1
    if on:
        assert mode == "equal"
        assert ratios == {
            drives[0]: Decimal("0.5"),
            drives[1]: Decimal("0.5"),
            past_posts: Decimal(0),
        }
    else:
        assert mode == "custom"
        assert ratios == {
            drives[0]: Decimal("0.7"),
            drives[1]: Decimal("0.3"),
            past_posts: Decimal(0),
        }
    assert await epochs(ingress, origin) == {
        **before,
        past_posts: before[past_posts] + 1,
    }
    assert await audit_reasons(ingress, origin, past_posts) == [*audited, "retired"]
    assert await lifecycles(ingress, origin) == generations, (
        "removal stops every use; it supersedes no generation"
    )
    async with uow(ingress, origin) as conn:
        source = (
            await conn.execute(
                text("SELECT state, config FROM media_sources WHERE id=:id"),
                {"id": past_posts},
            )
        ).one()
        assigned = await conn.execute(
            text("SELECT state FROM media_source_destinations WHERE source_id=:id"),
            {"id": past_posts},
        )
        assert (source[0], source[1].get("removed")) == ("paused", True)
        assert [s for (s,) in assigned.all()] == ["retired"]
    assert await credential_state(ingress, origin) == "revoked"
    assert await account_state(ingress, origin) == "disabled"


async def test_a_reconnect_after_removal_restores_the_same_source_off(
    ingress, origin, sync_conn, tmp_path
):
    """Removal moved no origin ownership: the account's reconnect (here the
    callback's landing) finds the same source, restores
    it with the saved Off and the custom ratios, under a new generation."""
    from src.services.target.source_membership import connect_past_posts

    drives, past_posts = await converted_with_past_posts(
        ingress, origin, tmp_path, on=False
    )
    old = await lifecycles(ingress, origin)
    await disable(ingress, origin)
    with sync_conn.cursor() as cur:
        # What the callback's destination landing does before the past-posts source's.
        cur.execute(
            "UPDATE ig_accounts SET state = 'active' WHERE id = %s", (origin["iga"],)
        )
        cur.execute(
            "UPDATE oauth_credentials SET state = 'active' WHERE id = %s",
            (origin["credential"],),
        )
    sync_conn.commit()
    async with uow(ingress, origin) as conn:
        restored = await connect_past_posts(
            conn,
            workspace_id=str(origin["ws"]),
            origin_ig_account_id=str(origin["iga"]),
            authorization=foundation.authorization(),
            had_prior_grant=True,
            choice=None,
            allocation_writer=category_mix.allocate_membership,
            capability=foundation.capability(),
        )
    assert (restored.source_id, restored.outcome) == (
        past_posts,
        "reconnected_preserved",
    )
    assert restored.history_enabled is False
    revision, mode, ratios = await mix(ingress, origin)
    assert (revision, mode) == (4, "custom")
    assert ratios == {
        drives[0]: Decimal("0.7"),
        drives[1]: Decimal("0.3"),
        past_posts: Decimal(0),
    }
    generations = await lifecycles(ingress, origin)
    assert {gid: generations[gid] for gid in old} == dict.fromkeys(old, "superseded")
    assert sorted(generations.values()) == sorted(
        ["active", *(["superseded"] * len(old))]
    )


async def test_removing_an_account_with_no_past_posts_writes_no_mix(ingress, origin):
    """The control: a Drive-only workspace's account removal writes no mix."""
    async with uow(ingress, origin) as conn:
        await mix_gate.add(conn, origin, [".7", ".3"])
    before = await mix(ingress, origin)
    epochs_before = await epochs(ingress, origin)
    result = await disable(ingress, origin)
    assert result.outcome == "executed"
    assert await mix(ingress, origin) == before
    assert await epochs(ingress, origin) == epochs_before
    assert await credential_state(ingress, origin) == "revoked"


async def test_account_removal_takes_the_source_prefix_before_any_credential_row(
    ingress, origin, world_db, tmp_path
):
    await converted_with_past_posts(ingress, origin, tmp_path, on=False)
    async with holding_credential(ingress, origin):
        removing = asyncio.create_task(disable(ingress, origin))
        await until_blocked(world_db)
        assert prefix_held(world_db, origin["ws"]), (
            "the removal reached a credential row before the source prefix"
        )
        assert row_locked(world_db, "ig_accounts", origin["iga"])
        assert not removing.done()
    result = await asyncio.wait_for(removing, timeout=30)
    assert result.outcome == "executed"


# --- the sources DELETE route ------------------------------------------------


async def test_the_delete_route_retires_a_past_posts_idempotently_and_answers_conflicts(
    ingress, origin, tmp_path
):
    """Production's composition, no capability: removal closes, so it works.
    The destination credential is untouched; a replay changes nothing; a stale
    revision is the protocol's 409 with the current one."""
    ws = str(origin["ws"])
    drives, past_posts = await converted_with_past_posts(
        ingress, origin, tmp_path, on=True
    )
    epoch = (await epochs(ingress, origin))[past_posts]
    url = f"/api/v1/workspaces/{ws}/sources/{past_posts}"

    async with mix_gate.mix_api(ingress, origin["user"], capability=None) as client:
        stale = await client.request(
            "DELETE", url, json={"protocol_version": 2, "expected_revision": 1}
        )
        assert stale.status_code == 409, stale.text
        assert stale.json() == {"reason": "mix_conflict", "current_revision": 2}
        assert (await epochs(ingress, origin))[past_posts] == epoch

        done = await client.request(
            "DELETE", url, json={"protocol_version": 2, "expected_revision": 2}
        )
        assert done.status_code == 200, done.text
        body = done.json()
        assert (body["source_id"], body["state"]) == (past_posts, "removed")
        assert body["mix"]["protocol_version"] == 2
        assert body["mix"]["revision"] == 3 and body["mix"]["mode"] == "equal"
        assert {r["source_id"] for r in body["mix"]["rows"]} == set(drives)

        again = await client.request(
            "DELETE", url, json={"protocol_version": 2, "expected_revision": 3}
        )
        assert again.status_code == 200, again.text
        assert (again.json()["state"], again.json()["mix"]["revision"]) == (
            "removed",
            3,
        )

        unknown = await client.request(
            "DELETE",
            f"/api/v1/workspaces/{ws}/sources/{uuid.uuid4()}",
            json={"protocol_version": 2, "expected_revision": 3},
        )
        assert unknown.status_code == 404, unknown.text

    assert (await epochs(ingress, origin))[past_posts] == epoch + 1, (
        "one bump for the removal, none for the replay"
    )
    assert await credential_state(ingress, origin) == "active"
    assert await account_state(ingress, origin) == "active"
    revision, mode, ratios = await mix(ingress, origin)
    assert (revision, mode, ratios[past_posts]) == (3, "equal", Decimal(0))


# --- deauthorize -------------------------------------------------------------


def signed_deauthorize(secret, issued_at):
    def b64(raw):
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    payload = {"algorithm": "HMAC-SHA256", "user_id": SUBJECT, "issued_at": issued_at}
    encoded = b64(json.dumps(payload).encode("utf-8"))
    sig = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256)
    return f"{b64(sig.digest())}.{encoded}"


async def test_a_deauthorize_revokes_the_subject_in_every_workspace_and_nobody_else(
    ingress, origin, sync_conn, monkeypatch
):
    """Through the real route, as the API's login, with no tenant to start from:
    one subject mapped in two workspaces. The past-posts account's current
    generation is fenced; the plain account's credential is revoked. Another
    person's mapping is untouched."""
    then = datetime.now(timezone.utc) - timedelta(days=3)
    fenced = str(uuid.uuid4())
    source = await foundation.ensure(ingress, origin, authorization_at(then, fenced))
    map_identity(sync_conn, origin, authorization_id=fenced, authorized_at=then)
    plain = credentialed(sync_conn, "deauthorize-plain")
    map_identity(
        sync_conn, plain, authorization_id=str(uuid.uuid4()), authorized_at=then
    )
    stranger = credentialed(sync_conn, "deauthorize-stranger")
    map_identity(
        sync_conn,
        stranger,
        authorization_id=str(uuid.uuid4()),
        authorized_at=then,
        subject=STRANGER,
    )
    epoch = (await epochs(ingress, origin))[source["source_id"]]

    monkeypatch.setattr(settings, "INSTAGRAM_APP_SECRET", "ig-secret", raising=False)
    monkeypatch.setattr(settings, "INSTAGRAM_APP_ID", APP_ID, raising=False)
    monkeypatch.setattr(settings, "FACEBOOK_APP_SECRET", None, raising=False)
    app = create_app(engine=ingress, env={})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://api.example.test"
    ) as client:
        answered = await client.post(
            "/webhooks/meta/deauthorize",
            data={"signed_request": signed_deauthorize("ig-secret", int(time.time()))},
        )
    assert (answered.status_code, answered.json()) == (200, {"status": "ok"})

    assert await lifecycles(ingress, origin) == {source["generation_id"]: "superseded"}
    assert await credential_state(ingress, origin) == "revoked"
    assert (await epochs(ingress, origin))[source["source_id"]] == epoch + 1
    assert (await audit_reasons(ingress, origin, source["source_id"]))[-1] == (
        "revocation"
    )
    assert await credential_state(ingress, plain) == "revoked"
    assert await credential_state(ingress, stranger) == "active"
    for chain in (origin, plain, stranger):
        assert await account_state(ingress, chain) == "active", (
            "a disconnect deletes and disables nothing"
        )


async def deauthorize_through_the_route(ingress, monkeypatch, issued_at):
    """Meta's deauthorize for SUBJECT, through the real route as the API's own
    login, with nothing patched."""
    monkeypatch.setattr(settings, "INSTAGRAM_APP_SECRET", "ig-secret", raising=False)
    monkeypatch.setattr(settings, "INSTAGRAM_APP_ID", APP_ID, raising=False)
    monkeypatch.setattr(settings, "FACEBOOK_APP_SECRET", None, raising=False)
    app = create_app(engine=ingress, env={})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://api.example.test"
    ) as client:
        return await client.post(
            "/webhooks/meta/deauthorize",
            data={"signed_request": signed_deauthorize("ig-secret", issued_at)},
        )


def named_by_reference(sync_conn, chain, reference=SUBJECT):
    """The account's `provider_account_ref` is *reference*: all the legacy
    lookup can see of it. (The chain's own connection holds the actor GUC.)"""
    with sync_conn.cursor() as cur:
        cur.execute(
            "UPDATE ig_accounts SET provider_account_ref = %s WHERE id = %s",
            (reference, chain["iga"]),
        )
    sync_conn.commit()


async def times_revoked(engine, chain):
    """How many times the credential was audited going to `revoked`."""
    return await fact(
        engine,
        chain,
        "SELECT count(*) FROM audit_events WHERE entity_kind = 'credential'"
        " AND entity_id = :id AND to_state = 'revoked'",
        id=str(chain["credential"]),
    )


async def test_a_deauthorize_reaches_the_account_no_mapping_names_even_when_another_is_mapped(
    ingress, origin, sync_conn, monkeypatch
):
    """The fallback lookup is per account, not all or nothing. The subject is
    mapped in one workspace and merely named by an account's reference in
    another (no mapping is recorded while the past-posts source is not live): both
    credentials are revoked, each once, and a bystander is not touched."""
    then = datetime.now(timezone.utc) - timedelta(days=3)
    map_identity(
        sync_conn, origin, authorization_id=str(uuid.uuid4()), authorized_at=then
    )
    named_by_reference(sync_conn, origin)
    unmapped = credentialed(sync_conn, "deauthorize-unmapped")
    named_by_reference(sync_conn, unmapped)
    bystander = credentialed(sync_conn, "deauthorize-bystander")
    named_by_reference(sync_conn, bystander, STRANGER)

    answered = await deauthorize_through_the_route(
        ingress, monkeypatch, int(time.time())
    )

    assert (answered.status_code, answered.json()) == (200, {"status": "ok"})
    assert await credential_state(ingress, origin) == "revoked", "mapped"
    assert await credential_state(ingress, unmapped) == "revoked", "by reference"
    assert await credential_state(ingress, bystander) == "active"
    assert [
        await times_revoked(ingress, chain) for chain in (origin, unmapped, bystander)
    ] == [1, 1, 0]
    for chain in (origin, unmapped, bystander):
        assert await account_state(ingress, chain) == "active"


async def test_the_fallback_lookup_leaves_a_mapped_account_to_the_mapping(
    ingress, origin, sync_conn, monkeypatch
):
    """A mapping is evidence, whatever it decided. The account's authorization
    is newer than the request, so the mapped path left it alone on purpose; its
    reference also names the subject, so a fallback that ignored the mapping
    would revoke the newer grant. The unmapped account beside it still is."""
    now = datetime.now(timezone.utc)
    map_identity(
        sync_conn,
        origin,
        authorization_id=str(uuid.uuid4()),
        authorized_at=now - timedelta(hours=1),
    )
    named_by_reference(sync_conn, origin)
    unmapped = credentialed(sync_conn, "deauthorize-unmapped")
    named_by_reference(sync_conn, unmapped)

    answered = await deauthorize_through_the_route(
        ingress, monkeypatch, int((now - timedelta(days=1)).timestamp())
    )

    assert (answered.status_code, answered.json()) == (200, {"status": "ok"})
    assert await credential_state(ingress, origin) == "active", (
        "the request predates this grant: the mapping decided, the lookup must not"
    )
    assert await times_revoked(ingress, origin) == 0
    assert await credential_state(ingress, unmapped) == "revoked"
    assert await times_revoked(ingress, unmapped) == 1


@pytest.mark.parametrize(
    "issued,counts",
    [
        ("before_the_reconnect", (0, 0, 1)),
        ("inside_the_skew", (0, 1, 0)),
        ("absent", (0, 1, 0)),
        ("after_the_reconnect", (1, 0, 0)),
    ],
)
async def test_a_newer_or_ambiguous_authorization_is_left_alone(
    ingress, origin, sync_conn, issued, counts
):
    """The account reconnected a day ago. A request issued before that is about
    the old grant (excluded); one within the skew of it, or with no order
    evidence while two grants exist, is ambiguous (held). Only a request issued
    after it revokes the current grant, and it fences only the current
    generation."""
    now = datetime.now(timezone.utc)
    first_at, second_at = now - timedelta(days=10), now - timedelta(days=1)
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    old = await foundation.ensure(ingress, origin, authorization_at(first_at, first))
    map_identity(sync_conn, origin, authorization_id=first, authorized_at=first_at)
    current = await foundation.ensure(
        ingress, origin, authorization_at(second_at, second)
    )
    map_identity(sync_conn, origin, authorization_id=second, authorized_at=second_at)
    sid = current["source_id"]
    epoch = (await epochs(ingress, origin))[sid]
    issued_at = {
        "before_the_reconnect": int((second_at - timedelta(hours=1)).timestamp()),
        "inside_the_skew": int((second_at + timedelta(seconds=60)).timestamp()),
        "absent": None,
        "after_the_reconnect": int(now.timestamp()),
    }[issued]

    outcome = await past_posts_lifecycle.revoke_subject_authorizations(
        ingress, app_identity=APP, subject=SUBJECT, issued_at=issued_at
    )

    assert (outcome.workspaces, outcome.revoked, outcome.held, outcome.excluded) == (
        1,
        *counts,
    )
    revoked = counts[0] == 1
    assert await lifecycles(ingress, origin) == {
        old["generation_id"]: "superseded",
        current["generation_id"]: "superseded" if revoked else "active",
    }
    assert await credential_state(ingress, origin) == (
        "revoked" if revoked else "active"
    )
    assert (await epochs(ingress, origin))[sid] == epoch + (1 if revoked else 0)


async def test_an_only_authorization_is_in_scope_without_order_evidence(
    ingress, origin, sync_conn
):
    authorized = str(uuid.uuid4())
    at = datetime.now(timezone.utc) - timedelta(days=2)
    source = await foundation.ensure(ingress, origin, authorization_at(at, authorized))
    map_identity(sync_conn, origin, authorization_id=authorized, authorized_at=at)
    outcome = await past_posts_lifecycle.revoke_subject_authorizations(
        ingress, app_identity=APP, subject=SUBJECT, issued_at=None
    )
    assert (outcome.revoked, outcome.held, outcome.excluded) == (1, 0, 0)
    assert await lifecycles(ingress, origin) == {source["generation_id"]: "superseded"}


async def test_a_future_issued_at_is_no_evidence(ingress, origin, sync_conn):
    """Beyond the skew in the future, the database's clock discards it: with
    two grants on the account, the request is then ambiguous."""
    now = datetime.now(timezone.utc)
    for days in (10, 1):
        at = now - timedelta(days=days)
        authorization = str(uuid.uuid4())
        await foundation.ensure(ingress, origin, authorization_at(at, authorization))
        map_identity(
            sync_conn, origin, authorization_id=authorization, authorized_at=at
        )
    outcome = await past_posts_lifecycle.revoke_subject_authorizations(
        ingress,
        app_identity=APP,
        subject=SUBJECT,
        issued_at=int((now + timedelta(days=1)).timestamp()),
    )
    assert (outcome.revoked, outcome.held, outcome.excluded) == (0, 1, 0)
    assert await credential_state(ingress, origin) == "active"


async def test_another_apps_subject_matches_nothing(ingress, origin, sync_conn):
    """Only `instagram:<digits>` identities are mapped: the legacy app, or an
    unconfigured one, never reaches an account by a raw subject."""
    at = datetime.now(timezone.utc) - timedelta(days=2)
    authorized = str(uuid.uuid4())
    await foundation.ensure(ingress, origin, authorization_at(at, authorized))
    map_identity(sync_conn, origin, authorization_id=authorized, authorized_at=at)
    for app in ("facebook:legacy", "instagram:unconfigured", "instagram:4243"):
        outcome = await past_posts_lifecycle.revoke_subject_authorizations(
            ingress, app_identity=app, subject=SUBJECT, issued_at=int(time.time())
        )
        assert outcome == past_posts_lifecycle.SubjectRevocation(0, 0, 0, 0), app
    assert await credential_state(ingress, origin) == "active"


async def test_a_past_posts_held_by_another_authorization_still_bumps_its_epoch(
    ingress, origin, sync_conn
):
    """The in-scope grant is not the past-posts source's current generation (a mapping
    reconciled after the past-posts source was connected): its credential is revoked
    directly, the generation it does not own is left alone, and the past-posts source's
    epoch still moves, because its credential is past-posts state."""
    at = datetime.now(timezone.utc) - timedelta(days=2)
    source = await foundation.ensure(ingress, origin, authorization_at(at))
    map_identity(
        sync_conn, origin, authorization_id=str(uuid.uuid4()), authorized_at=at
    )
    epoch = (await epochs(ingress, origin))[source["source_id"]]
    outcome = await past_posts_lifecycle.revoke_subject_authorizations(
        ingress, app_identity=APP, subject=SUBJECT, issued_at=int(time.time())
    )
    assert outcome.revoked == 1
    assert await credential_state(ingress, origin) == "revoked"
    assert await lifecycles(ingress, origin) == {source["generation_id"]: "active"}
    assert (await epochs(ingress, origin))[source["source_id"]] == epoch + 1
    assert (await audit_reasons(ingress, origin, source["source_id"]))[-1] == (
        "revocation"
    )


async def test_a_deauthorize_takes_the_source_prefix_before_the_credential_row(
    ingress, origin, sync_conn, world_db
):
    at = datetime.now(timezone.utc) - timedelta(days=2)
    map_identity(
        sync_conn, origin, authorization_id=str(uuid.uuid4()), authorized_at=at
    )
    async with holding_credential(ingress, origin):
        revoking = asyncio.create_task(
            past_posts_lifecycle.revoke_subject_authorizations(
                ingress, app_identity=APP, subject=SUBJECT, issued_at=int(time.time())
            )
        )
        await until_blocked(world_db)
        assert prefix_held(world_db, origin["ws"]), (
            "the deauthorize reached a credential row before the source prefix"
        )
        assert not revoking.done()
    outcome = await asyncio.wait_for(revoking, timeout=30)
    assert outcome.revoked == 1
    assert await credential_state(ingress, origin) == "revoked"


# --- credential refresh ------------------------------------------------------


async def test_a_refresh_rejection_expires_the_exact_generation_and_bumps_once(
    ingress, worker, origin
):
    await store_token(ingress, origin, "tok-OLD")
    source = await foundation.ensure(worker, origin, foundation.authorization())
    sid = source["source_id"]
    epoch = (await epochs(worker, origin))[sid]
    audited = await audit_reasons(worker, origin, sid)
    seen = []

    async def door(token):
        seen.append(token)
        await rejecting(token)

    assert await refresh(worker, origin, door) == "rejected"
    assert seen == ["tok-OLD"]
    assert await credential_state(worker, origin) == "expired"
    assert await account_state(worker, origin) == "reauth_required"
    assert await lifecycles(worker, origin) == {source["generation_id"]: "active"}, (
        "an expiry closes reads; the generation is still the current authorization"
    )
    assert (
        await fact(
            worker,
            origin,
            "SELECT current_authorization_generation_id::text FROM media_sources WHERE id=:id",
            id=sid,
        )
        == source["generation_id"]
    )
    assert (await epochs(worker, origin))[sid] == epoch + 1
    assert await audit_reasons(worker, origin, sid) == [*audited, "credential_expired"]


async def test_a_routine_refresh_keeps_its_generation_and_epoch(
    ingress, worker, origin
):
    from src.services.target import ig_login_oauth

    await store_token(ingress, origin, "tok-OLD")
    source = await foundation.ensure(worker, origin, foundation.authorization())
    sid = source["source_id"]
    before = await epochs(worker, origin)
    audited = await audit_reasons(worker, origin, sid)

    async def door(token):
        return "tok-NEW", datetime(2027, 1, 1, tzinfo=timezone.utc)

    assert await refresh(worker, origin, door) == "refreshed"
    assert await lifecycles(worker, origin) == {source["generation_id"]: "active"}
    assert await epochs(worker, origin) == before
    assert await audit_reasons(worker, origin, sid) == audited
    assert await credential_state(worker, origin) == "active"
    async with uow(worker, origin) as conn:
        assert (
            await ig_login_oauth.load_credential(
                conn, credential_id=str(origin["credential"]), captured=None
            )
            == "tok-NEW"
        )


@pytest.mark.parametrize("held_by", ["past_posts", "mapping"])
async def test_a_reauthorization_makes_the_old_capture_stale(
    ingress, worker, origin, sync_conn, held_by
):
    """The account is reconnected while its old token is with the provider:
    the rejection that comes back is about a token nothing holds any more.
    Nothing flips and the epoch is the reconnect's."""
    await store_token(ingress, origin, "tok-OLD")
    at = datetime.now(timezone.utc) - timedelta(days=2)
    if held_by == "past_posts":
        await foundation.ensure(worker, origin, foundation.authorization())
    else:
        map_identity(
            sync_conn, origin, authorization_id=str(uuid.uuid4()), authorized_at=at
        )
    reconnected = {}

    async def door(token):
        if held_by == "past_posts":
            reconnected.update(
                await foundation.ensure(worker, origin, foundation.authorization())
            )
        else:
            map_identity(
                sync_conn,
                origin,
                authorization_id=str(uuid.uuid4()),
                authorized_at=datetime.now(timezone.utc),
            )
        reconnected["epochs"] = await epochs(worker, origin)
        await rejecting(token)

    assert await refresh(worker, origin, door) == "stale"
    assert await credential_state(worker, origin) == "active"
    assert await account_state(worker, origin) == "active"
    assert await epochs(worker, origin) == reconnected["epochs"]
    if held_by == "past_posts":
        assert (await lifecycles(worker, origin))[reconnected["generation_id"]] == (
            "active"
        )
        assert (await audit_reasons(worker, origin, reconnected["source_id"]))[-1] == (
            "reauthorized"
        )


async def test_control_a_mapped_plain_account_still_dies_on_a_rejection(
    ingress, worker, origin, sync_conn
):
    await store_token(ingress, origin, "tok-OLD")
    map_identity(
        sync_conn,
        origin,
        authorization_id=str(uuid.uuid4()),
        authorized_at=datetime.now(timezone.utc) - timedelta(days=2),
    )
    assert await refresh(worker, origin, rejecting) == "rejected"
    assert await credential_state(worker, origin) == "expired"
    assert await account_state(worker, origin) == "reauth_required"


async def test_a_rejection_captured_before_a_reconnect_flips_nothing_after_it(
    ingress, worker, origin
):
    """A plain account, with no mapping and no past-posts source, looks the same before
    and after a reconnect: `store_credential` replaces the token in place under
    the same credential id. Only the stored ciphertext tells the two tokens
    apart, so the capture holds its digest, and the rejection that comes back
    for the old token flips the new one nothing."""
    await store_token(ingress, origin, "tok-OLD")
    seen = []

    async def door(token):
        seen.append(token)
        await store_token(ingress, origin, "tok-NEW")  # the reconnect, mid-flight
        await rejecting(token)

    assert await refresh(worker, origin, door) == "stale"
    assert seen == ["tok-OLD"]
    assert await credential_state(worker, origin) == "active"
    assert await account_state(worker, origin) == "active"

    # The control: the token that IS held, rejected in its turn, is the flip.
    assert await refresh(worker, origin, rejecting) == "rejected"
    assert await credential_state(worker, origin) == "expired"
    assert await account_state(worker, origin) == "reauth_required"


async def never_called(token):  # pragma: no cover - must not run
    raise AssertionError("a token nothing can read never reaches the provider")


@pytest.mark.parametrize("past_posts", [True, False], ids=["past_posts", "plain"])
async def test_an_undecryptable_credential_flips_through_the_rejection_wrapper(
    ingress, worker, origin, past_posts
):
    """The origin's stored payload is no ring ciphertext, so the refresh cannot
    read it (`load_credential` fails closed). The flip is the rejection
    wrapper's: a past-posts source's epoch moves once and is audited as an expiry, like
    any other definitive rejection; a plain account has no source, so its flip
    moves no epoch."""
    if past_posts:
        source = await foundation.ensure(worker, origin, foundation.authorization())
        sid = source["source_id"]
    before = await epochs(worker, origin)
    audited = await audit_reasons(worker, origin, sid) if past_posts else []

    assert await refresh(worker, origin, never_called) == "undecryptable"

    assert await credential_state(worker, origin) == "expired"
    assert await account_state(worker, origin) == "reauth_required"
    if past_posts:
        assert (await epochs(worker, origin))[sid] == before[sid] + 1
        assert await audit_reasons(worker, origin, sid) == [
            *audited,
            "credential_expired",
        ]
        assert await lifecycles(worker, origin) == {
            source["generation_id"]: "active"
        }, "an expiry closes reads; the generation is still the authorization"
    else:
        assert await epochs(worker, origin) == before


async def test_an_undecryptable_credential_locks_the_account_before_the_credential(
    ingress, worker, origin, world_db
):
    """`mark_dead` writes the credential, then the account: the unreadable
    payload's flip is wrapped like the rejection's, so the account's prefix is
    taken first."""
    async with holding_credential(worker, origin):
        refreshing = asyncio.create_task(refresh(worker, origin, never_called))
        await until_blocked(world_db)
        assert prefix_held(world_db, origin["ws"]), (
            "the flip reached the credential row before the source prefix"
        )
        assert row_locked(world_db, "ig_accounts", origin["iga"])
        assert not refreshing.done()
    assert await asyncio.wait_for(refreshing, timeout=30) == "undecryptable"
    assert await credential_state(worker, origin) == "expired"


async def test_an_undecryptable_credential_reconnected_since_it_was_read_flips_nothing(
    ingress, worker, origin, sync_conn, monkeypatch
):
    """The capture is read before the payload. A reconnect that lands after the
    unreadable payload was read, and before the flip, leaves the capture older
    than what is stored: the new token is not flipped for the old one's fault."""
    from src.services.target import ig_login_oauth

    real = ig_login_oauth.ring()

    class Reconnected:
        """The ring that cannot read the old payload, and reconnects the account
        at the moment it fails (as the owner, so the refresh's own transaction
        holds no row the reconnect has to wait for)."""

        @staticmethod
        def decrypt(payload):
            with sync_conn.cursor() as cur:
                cur.execute(
                    "UPDATE oauth_credentials SET encrypted_payload = %s WHERE id = %s",
                    (real.encrypt("tok-NEW"), origin["credential"]),
                )
            sync_conn.commit()
            raise ValueError("no key reads the old payload")

    with monkeypatch.context() as patched:
        patched.setattr(ig_login_oauth, "ring", lambda: Reconnected)
        assert await refresh(worker, origin, never_called) == "undecryptable"
    assert await credential_state(worker, origin) == "active"
    assert await account_state(worker, origin) == "active"
    async with uow(worker, origin) as conn:
        assert (
            await ig_login_oauth.load_credential(
                conn, credential_id=str(origin["credential"]), captured=None
            )
            == "tok-NEW"
        ), "the reconnect is what is stored, and it reads"


async def test_a_refresh_rejection_locks_the_account_before_the_credential(
    ingress, worker, origin, world_db
):
    """`mark_dead` writes the credential, then the account, and is not
    reordered: it is wrapped, so the account's prefix is taken first."""
    await store_token(ingress, origin, "tok-OLD")
    async with holding_credential(worker, origin):
        refreshing = asyncio.create_task(refresh(worker, origin, rejecting))
        await until_blocked(world_db)
        assert prefix_held(world_db, origin["ws"])
        assert row_locked(world_db, "ig_accounts", origin["iga"]), (
            "the rejection reached the credential before the account"
        )
        assert not refreshing.done()
    assert await asyncio.wait_for(refreshing, timeout=30) == "rejected"


# --- the publish ladder across a routine refresh and a reauthorization --------


#: The rungs a publish can wait at, each with the step it leaves on the story and
#: the one scripted answer from Meta that leaves it there. The real pipeline
#: climbs to the rung and stops where Meta's answer stops it; no row is placed.
#: `stage`: the staged copy waits, the container call having failed. `container`:
#: the container is made and not ready yet. `publish`: the publish was requested
#: and Meta's cap refused it, a definitive non-effect (a publish whose answer is
#: unknown is parked for the reconciler and is never restaged).
RUNGS = {
    "stage": ("transit_uploaded", dict(create_outcomes=["retryable"])),
    "container": (
        "container_created",
        dict(status_script=["IN_PROGRESS"] * publish_pipeline.POLL_BUDGET),
    ),
    "publish": ("publish_called", dict(publish_outcomes=["error_9"])),
}
#: How many times Meta was asked to publish by the end of a routine refresh: the
#: `publish` rung's first request was the refused one.
PUBLISH_REQUESTS = {"stage": 1, "container": 1, "publish": 2}


async def drive_forbidden(*args):
    raise AssertionError("a past-posts story used Drive's fetch")


def reclaim(sync_conn, job):
    """The job claimed afresh, as the loop claims it once its wait is over: a
    new lease, the attempt counted. The owner moves the clock by taking it."""
    with sync_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "UPDATE jobs SET state = 'leased', lease_token = %s, locked_by = 'past-posts-gate',"
            " locked_until = now() + interval '300s', attempts = attempts + 1"
            " WHERE id = %s RETURNING *",
            (str(uuid.uuid4()), str(job["id"])),
        )
        claimed = dict(cur.fetchone())
    sync_conn.commit()
    return claimed


async def run_ladder(story, *, claim=True):
    """One run of the real publish pipeline over *story*, as a restarted worker
    makes it: on a freshly claimed job (the first run uses the lease the story
    was approved under) and a coordinator built afresh. Nothing is remembered
    but what the database holds; Meta and the transit store are the world's, so
    they keep what they were given."""
    if claim:
        story.job = reclaim(story.sync_conn, story.job)
    return await publish_pipeline.run_publish_pipeline(
        story.job,
        engine=story.engine,
        meta=story.meta,
        transit=story.transit,
        media_fetch=drive_forbidden,
        past_posts=publish_gate.module().PastPostsPipeline(
            capability=foundation.capability(), fetch_selected=story.fetch_selected
        ),
        sleep=publish_gate.no_sleep,
    )


async def ladder_state(story):
    """What the ladder has done to *story*: its row, each provider operation in
    the order it was permitted with the generation its provenance names, the
    staged copies by generation, and what the provenance holds staged."""
    provenance, _ = await publish_gate.state(story.engine, story.chain, story.intent)
    async with observations.uow(story.engine, story.chain) as conn:
        row = await observations.row(
            conn,
            "SELECT state, publish_step, ig_container_id, transit_asset_ref, last_error"
            "  FROM post_intents WHERE id = :id",
            id=story.intent,
        )
        operations = await conn.execute(
            text(
                "SELECT o.op_kind, o.generation, o.state,"
                "       p.data_generation_id::text AS data_generation"
                "  FROM provider_operations o"
                "  LEFT JOIN instagram_operation_provenance p ON p.operation_id = o.id"
                " WHERE o.intent_id = :id ORDER BY o.created_at"
            ),
            {"id": story.intent},
        )
        artifacts = await conn.execute(
            text(
                "SELECT data_generation_id::text, outcome, cleanup_state"
                "  FROM instagram_artifacts WHERE intent_id = :id ORDER BY admitted_at"
            ),
            {"id": story.intent},
        )
        staged = provenance["staged_generation_id"]
        return {
            "intent": dict(row),
            "operations": [dict(o) for o in operations.mappings().all()],
            "artifacts": [tuple(a) for a in artifacts.all()],
            "staged_generation": None if staged is None else str(staged),
            "capture_epoch": provenance["capture_epoch"],
            "requests": (
                len(story.transit.calls),
                len(story.meta.create_calls),
                len(story.meta.publish_calls),
            ),
        }


async def waiting_at(rung, *, engine, chain, sync_conn):
    """An approved past-posts story the real pipeline has climbed to *rung* and
    left waiting there for its next run, and what it held at that point."""
    if sys.platform != "linux":
        pytest.fail(
            "the selected-media validator runs only on Linux"
            " (`media_validation.run_bounded`): run this gate on the Linux lane",
            pytrace=False,
        )
    step, script = RUNGS[rung]
    src, _, intent, job, pipeline, _ = await publish_gate.ready(engine, chain)
    story = SimpleNamespace(
        engine=engine,
        chain=chain,
        sync_conn=sync_conn,
        src=src,
        intent=intent,
        job=job,
        fetch_selected=pipeline.fetch_selected,
        transit=publish_gate.Transit(),
        meta=StubMetaAdapter(**script),
    )
    outcome = await run_ladder(story, claim=False)
    assert outcome in (
        publish_pipeline.RETRY_SCHEDULED,
        publish_pipeline.DEFERRED_META_CAP,
    ), outcome
    waiting = await ladder_state(story)
    held = waiting["intent"]
    assert (held["state"], held["publish_step"]) == ("approved", step)
    assert held["transit_asset_ref"] == story.transit.calls[0]
    assert waiting["staged_generation"] == src["generation_id"]
    return story, waiting


@pytest.mark.parametrize("rung", list(RUNGS))
async def test_publish_rung_survives_routine_refresh(
    ingress, worker, origin, sync_conn, rung
):
    """A routine refresh swaps the token in place and keeps its authorization:
    the same generation, the same epoch. A publish waiting at any rung climbs on
    from there over the copy and container it already holds: one upload, one
    container, and the one publish its authorization allows each try."""
    from src.services.target import ig_login_oauth

    await store_token(ingress, origin, "tok-OLD")
    story, waiting = await waiting_at(
        rung, engine=worker, chain=origin, sync_conn=sync_conn
    )
    staged, container = (
        waiting["intent"]["transit_asset_ref"],
        waiting["intent"]["ig_container_id"],
    )
    generations, before = await lifecycles(worker, origin), await epochs(worker, origin)

    async def door(token):
        assert token == "tok-OLD"
        return "tok-NEW", datetime(2027, 1, 1, tzinfo=timezone.utc)

    assert await refresh(worker, origin, door) == "refreshed"
    assert await lifecycles(worker, origin) == generations
    assert await epochs(worker, origin) == before
    async with uow(worker, origin) as conn:
        loaded = await ig_login_oauth.load_credential(
            conn, credential_id=str(origin["credential"]), captured=None
        )
    assert loaded == "tok-NEW", "the token really was swapped"

    assert await run_ladder(story) == publish_pipeline.POSTED
    done = await ladder_state(story)
    # One upload, the copy the story held; one container, the one published.
    assert story.transit.calls == [staged]
    assert done["intent"]["transit_asset_ref"] == staged
    made = [
        o
        for o in done["operations"]
        if o["op_kind"] == "container_create" and o["state"] == "succeeded"
    ]
    assert len(made) == 1
    assert container in (None, done["intent"]["ig_container_id"])
    assert {c["container_id"] for c in story.meta.publish_calls} == {
        done["intent"]["ig_container_id"]
    }
    assert len(story.meta.publish_calls) == PUBLISH_REQUESTS[rung]
    # Under the one generation the whole way: nothing was staged again.
    assert done["staged_generation"] == waiting["staged_generation"]
    assert done["capture_epoch"] == waiting["capture_epoch"]
    assert done["artifacts"] == waiting["artifacts"]
    assert {o["data_generation"] for o in done["operations"]} == {
        waiting["staged_generation"]
    }


@pytest.mark.parametrize("rung", list(RUNGS))
async def test_publish_rung_restages_after_reauthorization(
    worker, origin, sync_conn, rung
):
    """A reauthorization is a new generation and a bumped epoch, and what the
    story staged under the old one is not its to use. Its next run refuses by
    name before any effect and lets go of the copy and the container; only once
    the post is read again under the new generation does it stage a new copy,
    make a new container over it and publish that one. The old copy stays the
    old generation's, its cleanup pending."""
    story, waiting = await waiting_at(
        rung, engine=worker, chain=origin, sync_conn=sync_conn
    )
    source = story.src["source_id"]
    old, old_ref = waiting["staged_generation"], waiting["intent"]["transit_asset_ref"]
    old_container = waiting["intent"]["ig_container_id"]
    epoch = (await epochs(worker, origin))[source]

    second = await foundation.ensure(worker, origin, foundation.authorization())
    new = second["generation_id"]
    assert new != old
    assert await lifecycles(worker, origin) == {old: "superseded", new: "active"}
    assert (await epochs(worker, origin))[source] == epoch + 1

    # Refused by name: nothing is sent, nothing is reused, and the old copy is
    # let go of without being destroyed.
    assert await run_ladder(story) == publish_pipeline.RETRY_SCHEDULED
    refused = await ladder_state(story)
    assert refused["intent"]["last_error"]["error"]["reason"] == (
        "past_posts_revalidation_required"
    )
    held = refused["intent"]
    assert (held["state"], held["publish_step"]) == ("approved", "none")
    assert (held["transit_asset_ref"], held["ig_container_id"]) == (None, None)
    assert refused["staged_generation"] is None
    assert refused["operations"] == waiting["operations"], "no permit was issued"
    assert refused["artifacts"] == waiting["artifacts"] == [(old, "settled", "pending")]
    assert refused["requests"] == waiting["requests"], "no provider call was made"
    assert (
        await fact(
            worker,
            origin,
            "SELECT count(*) FROM audit_events WHERE entity_id = :id"
            " AND detail->>'reason' = 'past_posts_revalidation_required'",
            id=story.intent,
        )
        == 1
    )

    # The post is read again under the new generation; the story restages.
    await observations.observe(
        worker, origin, second, observations.bundle(attribution="not_detected")
    )
    assert await run_ladder(story) == publish_pipeline.POSTED
    done = await ladder_state(story)
    new_ref = done["intent"]["transit_asset_ref"]
    assert story.transit.calls == [old_ref, new_ref] and new_ref != old_ref
    assert story.meta.create_calls[-1]["media_url"] == story.transit.delivery_url(
        new_ref, media_kind="image"
    )
    container = done["intent"]["ig_container_id"]
    assert container != old_container
    assert story.meta.publish_calls[-1]["container_id"] == container
    assert done["staged_generation"] == new and done["capture_epoch"] == epoch + 1
    assert done["artifacts"] == [
        (old, "settled", "pending"),
        (new, "settled", "pending"),
    ]
    # What came before the reauthorization is the old generation's, untouched;
    # every effect after it is the new one's: a container, then the publish.
    before = len(waiting["operations"])
    assert done["operations"][:before] == waiting["operations"]
    assert [(o["op_kind"], o["state"]) for o in done["operations"][before:]] == [
        ("container_create", "succeeded"),
        ("publish", "succeeded"),
    ]
    assert {o["data_generation"] for o in done["operations"][before:]} == {new}
    assert {o["data_generation"] for o in done["operations"][:before]} == {old}


# --- revocation while a Graph call is in flight ------------------------------


def listing(*posts):
    """Graph's answer to a listing: the posts as `(id, epoch seconds)`."""
    return httpx.Response(
        200,
        json={
            "data": [
                {
                    "id": post,
                    "timestamp": datetime.fromtimestamp(at, timezone.utc).isoformat(),
                    "media_type": "IMAGE",
                    "media_url": f"https://scontent-test.cdninstagram.com/{post}.jpg",
                }
                for post, at in posts
            ]
        },
    )


def midway(request):
    """A time inside the window a listing asked for, whichever lane asked."""
    params = request.url.params
    return (int(params["since"]) + int(params["until"])) // 2


@asynccontextmanager
async def listing_in_flight(world_db, ingress, origin, sync_conn, *, revoke, shape):
    """A past-posts read over a fake Graph. Its first listing lands one post. Its
    next listing is in flight when *revoke* (an async callable, or None) runs,
    and the provider then answers with *shape*: a page of one more post, or an
    empty one, after which the walk would probe for older history. Yields what
    the turn that listed did, the index just before and after it, every request
    the provider saw, and `retry()`, which runs that turn's job once more."""
    m = sync_gate.service()
    await store_token(ingress, origin, "tok-OLD")
    src, runner, job, capture = await sync_gate.quality_source(
        world_db, ingress, origin
    )
    authorization = await fact(
        ingress,
        origin,
        "SELECT authorization_id::text FROM instagram_past_posts_generations"
        " WHERE id = :id",
        id=src["generation_id"],
    )
    map_identity(
        sync_conn,
        origin,
        authorization_id=authorization,
        authorized_at=datetime.now(timezone.utc) - timedelta(days=3),
    )
    windows = []

    async def graph(request):
        if "since" not in request.url.params:
            return listing()  # the walk's probe for older history: there is none
        windows.append(request)
        if len(windows) == 1:
            return listing(("111", midway(request)))
        assert len(windows) == 2, "a provider call was made after the revocation"
        if revoke is not None:
            await revoke()
        return listing(("222", midway(request))) if shape == "a_page" else listing()

    injected, client, requests = await sync_gate.quality_ports(m, capture, graph)
    try:
        first = await m.run_sync(runner, job, ports=injected)
        # A turn that makes no listing call (the helper's) is followed by the
        # next, until one does.
        for _ in range(5):
            job = await sync_gate.next_job(runner, origin, job)
            before = await sync_gate.state(ingress, origin, src)
            listed = await m.run_sync(runner, job, ports=injected)
            if len(requests) > 1:
                break
        else:
            raise AssertionError("the read never listed a second time")
        yield SimpleNamespace(
            first=first,
            listed=listed,
            before=before,
            after=await sync_gate.state(ingress, origin, src),
            requests=requests,
            retry=lambda: m.run_sync(runner, job, ports=injected),
        )
    finally:
        await client.aclose()
        await runner.dispose()


@pytest.mark.parametrize("shape", ["a_page", "an_empty_page"])
async def test_the_second_listing_lands_when_nothing_is_revoked(
    world_db, ingress, origin, sync_conn, shape
):
    """The control: with nothing revoked, the same listing lands. A page of one
    more post is indexed and the checkpoint moves; an empty page is followed by
    the walk's probe for older history."""
    async with listing_in_flight(
        world_db, ingress, origin, sync_conn, revoke=None, shape=shape
    ) as read:
        first, listed = read.first, read.listed
        assert (first.outcome, first.committed_observations) == ("committed", 1)
        assert [parent for parent, *_ in read.before[1]] == ["111"]
        assert listed.outcome == "committed" and listed.drop_reason is None
        assert read.after[0]["revision"] == read.before[0]["revision"] + 1
        if shape == "a_page":
            assert listed.committed_observations == 1
            assert [parent for parent, *_ in read.after[1]] == ["111", "222"]
            assert len(read.requests) == 2
        else:
            assert [parent for parent, *_ in read.after[1]] == ["111"]
            assert len(read.requests) == 3, "the empty page was followed by a probe"


@pytest.mark.parametrize("shape", ["a_page", "an_empty_page"])
@pytest.mark.parametrize(
    "revocation,reason",
    [
        ("deauthorize", "generation_unavailable"),
        ("refresh_rejected", "origin_unavailable"),
    ],
)
async def test_a_revocation_during_a_graph_listing_lands_nothing_and_is_refused_by_name(
    world_db, ingress, worker, origin, sync_conn, revocation, reason, shape
):
    """The person disconnects (Meta's deauthorize) or the refresh is rejected
    while a listing call is in flight. The provider answers all the same: the
    page must not land, the checkpoint must not move, the turn ends refused by
    the reason that closed the source, and no provider call follows it, in that
    turn (no probe) or on a retry of its job."""
    done = []

    async def revoke():
        if revocation == "deauthorize":
            done.append(
                await past_posts_lifecycle.revoke_subject_authorizations(
                    ingress, app_identity=APP, subject=SUBJECT, issued_at=None
                )
            )
        else:
            done.append(await refresh(worker, origin, rejecting))

    async with listing_in_flight(
        world_db, ingress, origin, sync_conn, revoke=revoke, shape=shape
    ) as read:
        # The revocation happened, mid-call, and did what it does.
        if revocation == "deauthorize":
            assert (done[0].revoked, done[0].held) == (1, 0)
            assert await credential_state(worker, origin) == "revoked"
            assert set((await lifecycles(worker, origin)).values()) == {"superseded"}
        else:
            assert done == ["rejected"]
            assert await credential_state(worker, origin) == "expired"
            assert await account_state(worker, origin) == "reauth_required"

        # The turn was refused by name, after its one call, and landed nothing.
        first, listed = read.first, read.listed
        assert (first.outcome, first.committed_observations) == ("committed", 1)
        assert listed.outcome == "dropped" and listed.drop_reason == reason
        assert (listed.committed_observations, listed.graph_attempts) == (0, 1)
        assert read.after == read.before, "the index and the checkpoint did not move"
        assert [parent for parent, *_ in read.after[1]] == ["111"]
        assert read.after[2] == 0, "no continuation was queued"

        # No provider call followed it: not a probe in the turn, nor any on a retry.
        assert len(read.requests) == 2
        retried = await read.retry()
        assert retried.outcome == "dropped" and retried.drop_reason == reason
        assert retried.graph_attempts == 0
        assert len(read.requests) == 2


# --- sync_now through the command port ---------------------------------------


async def test_sync_now_through_the_port_reads_the_capability_from_the_session(
    ingress, origin
):
    """The port passes no capability. Without one in the session, the
    production default, the past-posts source refuses by name and nothing is queued.
    With the composition root's capability in the session, the sync is
    queued with its source-read capture."""
    source = await admission.ready(ingress, origin)
    command = commands.Command(
        kind="sync_now",
        workspace_id=str(origin["ws"]),
        actor_user_id=str(origin["user"]),
        channel="web",
        args={"source_id": source["source_id"]},
    )
    async with person(ingress, origin) as session:
        with pytest.raises(commands.CommandRefused) as refused:
            await commands.execute(session, command)
    assert refused.value.facts == {
        "source_id": source["source_id"],
        "source_read_reason": "past_posts_not_activated",
    }
    assert await fact(ingress, origin, "SELECT count(*) FROM jobs") == 0

    async with person(ingress, origin) as session:
        session.info["past_posts_capability"] = foundation.capability()
        result = await commands.execute(session, command)
    assert result.outcome == "enqueued"
    payload = await fact(
        ingress,
        origin,
        "SELECT payload FROM jobs WHERE id=:id",
        id=result.data["job_id"],
    )
    assert payload["source_id"] == source["source_id"]
    assert payload["source_read_capture"]["generation_id"] == source["generation_id"]


# --- offboarding -------------------------------------------------------------


def offboarding(sync_conn, chain, *, started="now() - interval '2 days'"):
    with sync_conn.cursor() as cur:
        cur.execute(
            "UPDATE workspaces SET state = 'offboarding', offboarding_at = "
            + started
            + " WHERE id = %s",
            (chain["ws"],),
        )
    sync_conn.commit()


async def offboard_run(engine, chain, *, grace=0):
    """One `offboard_workspace` run in the loop's shape, as `svc_worker`."""
    from src.services.target.offboarding import execute_offboard
    from src.services.target.work_loop import WorkerConfig, WorkerDeps

    deps = WorkerDeps(
        engine=engine,
        transit=None,
        config=WorkerConfig(
            offboard_grace_seconds=grace,
            offboard_drain_timeout_seconds=15 * 60,
            offboard_drain_recheck_seconds=30,
        ),
    )
    async with uow(engine, chain) as session:
        return await execute_offboard(
            deps, session, {"id": str(uuid.uuid4()), "workspace_id": str(chain["ws"])}
        )


async def test_offboarding_fences_every_past_posts_once_then_the_cascade_takes_them(
    worker, origin, sync_conn
):
    """The first run drains, fences the past-posts source (superseded, credential
    revoked, one bump) and schedules the finalizer. The second finds the fence
    in place and finalizes in the same run, which a source prefix held by the
    loop's session would have blocked for good. The past-posts source goes with the
    workspace; its audit does not."""
    source = await foundation.ensure(worker, origin, foundation.authorization())
    sid = source["source_id"]
    epoch = (await epochs(worker, origin))[sid]
    audited = await audit_reasons(worker, origin, sid)
    offboarding(sync_conn, origin)

    first = await offboard_run(worker, origin)

    assert (first["outcome"], first["past_posts_fenced"]) == ("drained", 1)
    assert await lifecycles(worker, origin) == {source["generation_id"]: "superseded"}
    assert await credential_state(worker, origin) == "revoked"
    assert (await epochs(worker, origin))[sid] == epoch + 1

    second = await asyncio.wait_for(offboard_run(worker, origin), timeout=60)

    assert second["outcome"] == "finalized"
    assert await audit_reasons(worker, origin, sid) == [*audited, "revocation"], (
        "one fence across both runs"
    )
    with sync_conn.cursor() as cur:
        cur.execute(
            "SELECT (SELECT count(*) FROM workspaces WHERE id = %(ws)s),"
            " (SELECT count(*) FROM media_sources WHERE workspace_id = %(ws)s),"
            " (SELECT count(*) FROM instagram_past_posts_generations WHERE workspace_id = %(ws)s),"
            " (SELECT count(*) FROM media_source_destinations WHERE workspace_id = %(ws)s),"
            " (SELECT count(*) FROM oauth_credentials WHERE workspace_id = %(ws)s)",
            {"ws": str(origin["ws"])},
        )
        assert cur.fetchone() == (0, 0, 0, 0, 0)
    sync_conn.commit()


async def test_offboarding_leaves_a_past_posts_working_while_publishing_drains(
    worker, origin, sync_conn
):
    """Drain before revoke: a past-posts source is not fenced while a post is still
    publishing, so the work it may need is not cut from under it."""
    source = await foundation.ensure(worker, origin, foundation.authorization())
    sid = source["source_id"]
    epoch = (await epochs(worker, origin))[sid]
    with sync_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO media_items (workspace_id, source_id, content_hash, file_name,"
            " media_kind, provider_file_ref) VALUES (%s, %s, 'h-publishing', 'f.jpg',"
            " 'image', 'r-publishing') RETURNING id",
            (origin["ws"], origin["src"]),
        )
        media = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO post_intents (workspace_id, ig_account_id, media_item_id,"
            " provider_account_ref, approval_mode, schedule_slot_at, state, publish_step,"
            " cap_consumed_on) VALUES (%s, %s, %s, 'acct-publishing', 'manual',"
            " now() + interval '1 hour', 'publishing', 'publish_called', 'today')",
            (origin["ws"], origin["iga"], media),
        )
    sync_conn.commit()
    offboarding(sync_conn, origin, started="now()")

    out = await offboard_run(worker, origin, grace=3600)

    assert out["outcome"] == "draining" and out["publishing"] == 1
    assert await lifecycles(worker, origin) == {source["generation_id"]: "active"}
    assert await credential_state(worker, origin) == "active"
    assert (await epochs(worker, origin))[sid] == epoch


async def test_the_offboarding_fence_waits_behind_the_source_prefix(
    worker, origin, sync_conn, world_db
):
    from src.services.target import source_state

    await foundation.ensure(worker, origin, foundation.authorization())
    offboarding(sync_conn, origin)
    async with uow(worker, origin) as holder:
        await source_state.lock_workspace_sources(
            holder, workspace_id=str(origin["ws"])
        )
        running = asyncio.create_task(offboard_run(worker, origin))
        await until_blocked(world_db)
        await asyncio.sleep(0.5)
        assert not running.done(), "the offboarding fence ran without the source prefix"
    out = await asyncio.wait_for(running, timeout=30)
    assert out["past_posts_fenced"] == 1
