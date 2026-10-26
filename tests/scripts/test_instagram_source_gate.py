"""The Instagram source, end to end through the real composition, over fakes
(#1644).

Every other past-posts gate drives one service under its database role, with the
past-posts capability handed in by the test. This one drives the whole thing the
way the deployment assembles it:

* The ARMED composition. `past_posts_composition.build_worker` and
  `create_app(env=...)` (which calls `build_api`) are given an activation
  record, a conversion record and an HMAC key ring that this module writes
  into a temporary directory. Nothing is read from the process environment,
  and no test hands a service a capability: it comes from the composition
  root, as it will in production once an operator arms it.
* The real services over the replayed target schema, as the real logins: the
  API as `svc_ingress` (sign-in, the OAuth callback, the source-mix and
  source-policy routes, the command port, the Telegram webhook, the
  data-deletion callback), the worker as `svc_worker` (the clock, the
  claim, the executors, the prompt and sender sweeps). The worker's own
  `WorkLoop`s claim and run its jobs one at a time, a tick of its clock
  mints what is due, and its sweepers make one pass each: `src.main` and
  `src.worker.main` are never entered, and no clock loop, cadence sleep
  or background task runs. Where a cadence would have passed (a retry's
  backoff, the lifecycle sweep's minute) the scene says so with an owner
  `UPDATE` of the due time, as the sibling gates do. A source's first
  read is not one of those places: connecting, switching On or
  reconnecting arms it, and a tick of the clock mints it.
* Fake providers at their seams (`tests/fixtures/fake_providers.py`): the
  OAuth exchange, Instagram's Graph and CDN (the generated onboarding past-posts sources,
  paged the way Meta pages them), the transit store, the Telegram Bot API and
  the Meta publish stub. Every client is in-process; the worker's Graph and
  Telegram clients carry an egress policy that resolves no name (the API's
  past-posts client serves only the thumbnail route, which no scene calls).

The past-posts source each scene indexes is `onboarding.useful(10, ...)` trimmed to the
posts that give a scene one candidate: the recent, credited and unknown posts
(each an exclusion with its own reason) and the past-posts source's single 2022 image,
the one post that qualifies, so what is selected is known in advance. Only the
first scene walks the history to its end (a window that lands nothing is
skipped by one probe, and the walk ends when a probe finds nothing older, or at
Instagram's 2010 launch floor); the others stop once every post is indexed.

The selected-media validator runs for real and is Linux-only
(`media_validation.run_bounded`): run this module on the Linux lane. It is also
run under the isolated-provider guard (`-p tests.isolation_guard
--isolated-provider-gate`, the disposable database on loopback port 65433),
which refuses any socket, name lookup or worker entrypoint outside the fakes.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import sys
import time
import uuid
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import psycopg2
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src import worker
from src.api.app import create_app
from src.api.routes.webhooks import SECRET_HEADER
from src.config.settings import settings
from src.services.target import (
    past_posts_activation,
    past_posts_composition,
    ig_login_oauth,
    jobs,
    media_validation,
    unit_of_work,
    versioned_mix,
    work_loop,
)
from src.services.target.instagram_sources import PastPostsCapability
from src.services.target.meta_adapter import StubMetaAdapter
from src.services.target.unit_of_work import asyncpg_url
from src.services.target.work_loop import WorkerConfig
from tests.fixtures import fake_providers as fakes
from tests.fixtures import instagram_onboarding as onboarding
from tests.scripts import test_meta_deletion_receipts_gate as receipts
from tests.scripts import test_oauth_past_posts_choice_gate as oauth
from tests.scripts.conftest import (
    _scratch,
    as_user,
    async_url,
    in_tenant,
    build_world,
    rows_holding,
    set_test_passwords,
)
from tests.src.api.conftest import API, owned_workspace
from tests.src.services.target import test_past_posts_activation as activation_gate

#: The OAuth gate's configured sign-in and Instagram world, by assignment.
google_configured = oauth.google_configured
instagram_configured = oauth.instagram_configured
fresh_preauth_budget = oauth.fresh_preauth_budget

pytestmark = [pytest.mark.integration, pytest.mark.slow]

UTC = timezone.utc
REF_A, REF_B = "17841400000000301", "17841400000000302"
#: A synthetic Telegram group, and the app secret a deletion request is signed with.
CHAT = "-1001234567890"
SECRET = "fixture-past-posts-" + "a" * 32
APP = "instagram:4242"
#: The owner's synthetic Telegram id, and the webhook's secret, for the scene
#: that taps a card through the real route.
TAPPER = "7007"
WEBHOOK_SECRET = "fixture-webhook-secret"


@pytest.fixture
def world(admin_conn, owner_actor):
    """A fresh replayed target schema per test: jobs are claimed from the whole
    database, so a shared one would let a scene run another's work."""
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        stream = build_world(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        yield {
            "stream": stream,
            "ingress": as_user(db, "svc_ingress"),
            "worker": as_user(db, "svc_worker"),
        }
    finally:
        gen.close()


# --- the armed environments ----------------------------------------------------


def armed_environments(tmp_path):
    """The API's and the worker's environments as production's are: each on its
    own release, with the whole key ring and nothing else the past-posts source needs."""
    shared = {past_posts_activation.KEYS_VAR: activation_gate.RING}
    return (
        {**shared, past_posts_composition.RELEASE_VAR: activation_gate.API_SHA},
        {**shared, past_posts_composition.RELEASE_VAR: activation_gate.WORKER_SHA},
    )


# --- the past-posts sources ---------------------------------------------------------------


def past_posts_of(ref, *, offset=0):
    """`useful(10)` as account *ref* serves it, trimmed to one candidate: the
    posts that are each excluded for a reason of their own, and the 2022 image."""
    full = onboarding.useful(10, int(time.time()), owner_id=ref)
    kept = tuple(
        p
        for p in full.posts
        if p.group in ("recent", "credited", "unknown")
        or p.at == onboarding.IMAGE_2022_AT
    )
    return fakes.retarget(replace(full, posts=kept), owner=ref, offset=offset)


def groups_of(past_posts):
    return Counter(
        p.group for p in past_posts.posts for _ in range(len(p.children) or 1)
    )


def candidate_of(past_posts):
    """The 2022 image: the one post of the past-posts source that qualifies."""
    return next(p for p in past_posts.posts if p.at == onboarding.IMAGE_2022_AT)


# --- the composed world ---------------------------------------------------------


class DriveForbidden:
    """The Drive adapter the worker composes with: a past-posts item is never
    read through it, and no test syncs a Drive folder."""

    async def fetch_bytes(self, *args, **kwargs):
        raise AssertionError("a past-posts item was read through Drive")


class GateMeta(StubMetaAdapter):
    """The counting Meta stub, with a hook that runs after a container exists."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.after_create = None

    async def create_container(self, *args, **kwargs):
        result = await super().create_container(*args, **kwargs)
        if self.after_create is not None:
            await self.after_create()
        return result


class Gate:
    """One workspace under the armed composition: the API the callback, the
    routes and the commands go through, and the worker whose clock, jobs,
    sweeps and sender run."""

    def __init__(self, **parts):
        self.__dict__.update(parts)

    @property
    def ws(self):
        return self.scene.ws

    # -- reading and seeding, as the migration actor ---------------------------

    def rows(self, query, *params):
        return oauth.rows(self.world, query, *params)

    def owner(self, sql, *params):
        conn = psycopg2.connect(self.world["stream"])
        try:
            with conn.cursor() as cur:
                cur.execute("SET app.actor_kind = 'migration'")
                cur.execute(sql, params or None)
            conn.commit()
        finally:
            conn.close()

    def bind(self):
        """The workspace's Telegram group: where a card is delivered."""
        self.owner(
            "INSERT INTO channel_bindings (workspace_id, channel, external_ref)"
            " VALUES (%s, 'telegram_group', %s)",
            self.ws,
            CHAT,
        )

    def publishing(self, enabled=True):
        self.owner(
            "UPDATE workspaces SET api_publishing_enabled = %s WHERE id = %s",
            enabled,
            self.ws,
        )

    def link_telegram(self, telegram_user_id=TAPPER):
        """The owner's Telegram identity, linked: who a tap in the bound group
        is (`identity.tapper_for_identity`)."""
        self.owner(
            "INSERT INTO user_identities"
            " (user_id, provider, external_id, display_name, verified_at)"
            " VALUES (%s, 'telegram', %s, 'Ada', now())",
            self.scene.user_id,
            telegram_user_id,
        )

    # -- the API --------------------------------------------------------------

    async def command(self, kind, **args):
        """A command through the API's port, as the workspace's owner."""
        return await self.scene.client.post(
            f"/api/v1/workspaces/{self.ws}/commands/{kind}",
            json=args,
            headers={**self.scene.owner, "Idempotency-Key": f"{kind}:{uuid.uuid4()}"},
        )

    async def tap(self, action, intent_id, *, message_id, telegram_user_id=TAPPER):
        """A tap on the bound group's card as Telegram delivers it: the real
        webhook route, its secret, admission, the app's own dispatcher and the
        commit (`POST /webhooks/telegram`)."""
        self.monkeypatch.setattr(
            settings,
            "TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN",
            WEBHOOK_SECRET,
            raising=False,
        )
        return await self.scene.client.post(
            "/webhooks/telegram",
            json={
                "update_id": uuid.uuid4().int % 1_000_000_000,
                "callback_query": {
                    "id": f"q-{uuid.uuid4().hex[:8]}",
                    "from": {"id": int(telegram_user_id), "first_name": "Ada"},
                    "message": {
                        "message_id": message_id,
                        "chat": {"id": int(CHAT), "type": "supergroup"},
                    },
                    "data": f"v1:{action}:{intent_id}",
                },
            },
            headers={SECRET_HEADER: WEBHOOK_SECRET},
        )

    def card_ref(self, intent_id):
        """The message id the story's card was delivered under."""
        (card,) = self.rows(
            "SELECT external_message_ref AS ref FROM channel_outbox"
            " WHERE intent_id = %s AND kind = 'approval_prompt'",
            intent_id,
        )
        return int(card["ref"])

    async def mix(self, **body):
        """A source-mix write (`PUT category-mix`, protocol 2), at the mix's
        current revision."""
        return await self.scene.client.put(
            self.scene.url("category-mix"),
            json={
                "protocol_version": 2,
                "expected_revision": self.scene.mix()["revision"],
                **body,
            },
            headers=self.scene.owner,
        )

    async def switch(self, source, enabled):
        return await self.mix(action="set_enabled", source_id=source, enabled=enabled)

    # -- the worker -----------------------------------------------------------

    async def tick(self):
        """One clock tick of the composed worker: its election, its recurring
        set, and the past-posts source's activation handed to every leg."""
        async with self.engine.connect() as election:
            clock = worker._clock_for(self.app, election)
            try:
                return await clock.tick_once()
            finally:
                # The election is a session lock: a pooled connection would
                # keep it, and the next tick would find itself not elected.
                await clock.stop()

    async def step(self):
        """Claim and run one job, on whichever lane has one."""
        for loop in self.app.loops:
            if await loop.run_once():
                return True
        return False

    async def until(self, query, *params, wait=10):
        """Wait, by the database's own clock, until *query* (one row, a boolean
        `ready`) holds: real time passing, with nothing moved to make it pass."""
        for _ in range(int(wait / 0.2)):
            if self.rows(query, *params)[0]["ready"]:
                return
            await asyncio.sleep(0.2)
        raise AssertionError(f"still not true after {wait} s: {query}")

    async def run_until(self, done, limit=200):
        """Jobs, one at a time, until *done()* holds: work that is not the
        point of the scene (the rest of a history walk) stays queued."""
        for _ in range(limit):
            if done():
                return
            assert await self.step(), "the worker ran dry before the work was done"
        raise AssertionError(f"the work was still not done after {limit} jobs")

    async def drain(self, limit=1000):
        """Every job the composed loops can claim, one at a time, until none is
        left: the entrypoint's loops without their sleeps."""
        ran = 0
        while await self.step():
            ran += 1
            assert ran < limit, f"the worker was still finding jobs after {limit}"
        return ran

    async def sweep_prompts(self):
        """The worker's prompt sweep as it serves a past post: one pass mints
        each due story's prepare job, the media lane runs them (each story's
        file fetched and judged), and the next pass gives every due story its
        card. The sweeper's own loop, stopped at its first wait, each time."""
        if sys.platform != "linux":
            pytest.fail(
                "the selected-media validator runs only on Linux"
                " (`media_validation.run_bounded`): run this gate on the Linux lane",
                pytrace=False,
            )
        await self._sweep_once()
        await self.prepare_files()
        await self._sweep_once()

    async def _sweep_once(self):
        stop = asyncio.Event()

        async def at_the_wait(event, seconds):
            event.set()

        with self.monkeypatch.context() as patch:
            patch.setattr(jobs, "wait_or_stop", at_the_wait)
            await worker.PromptSweeper(self.app).run(stop)

    async def prepare_files(self, limit=100):
        """The media lane's jobs, one at a time, until it has none it may run:
        the prepare jobs the sweep minted. A job put back for a later try is
        not run again here."""
        media = [loop for loop in self.app.loops if loop.lane == "media"]
        assert media, "the composed worker serves no media lane"
        for _ in range(limit):
            if not any([await loop.run_once() for loop in media]):
                return
        raise AssertionError(f"the media lane was still finding jobs after {limit}")

    async def deliver(self):
        """The sender sweep's one pass, and the `deliver_outbox` jobs it mints."""
        sessions = unit_of_work.make_session_for(self.engine)
        async with sessions({}) as session:
            minted = await work_loop.ensure_sender_jobs(session)
        await self.drain()
        return minted

    # -- the story's steps ------------------------------------------------------

    async def connect(self, ref, handle, past_posts, *, history=True):
        """The real connect, as the web does it: preview, start, callback, result."""
        self.ig.add(past_posts)
        return await self.scene.add(ref, handle, history=history)

    def indexed(self, source, past_posts):
        """Every post is indexed and the past-posts source's candidates are known as such
        (the walk still owes the recent post's caption read: it can never be
        one, so the card does not wait for it)."""
        summary = self.summary(source)
        return (
            summary.get("observed") == len(past_posts.posts)
            and summary.get("candidates") == groups_of(past_posts)["not_detected"]
        )

    def summary(self, source):
        """The index's own account of itself (the sync checkpoint's summary)."""
        return (
            self.rows(
                "SELECT sync_checkpoint->'summary' AS s FROM media_sources"
                " WHERE id = %s",
                source,
            )[0]["s"]
            or {}
        )

    def read_due(self, source):
        """Whether *source*'s read is due: the transition that made it readable set
        its due time, and the clock has not taken it yet."""
        return self.rows(
            "SELECT next_sync_at IS NOT NULL AND next_sync_at <= clock_timestamp()"
            "  AS due FROM media_sources WHERE id = %s",
            source,
        )[0]["due"]

    async def index(self, source, past_posts, *, whole=False, ready=None, tick=True):
        """The first read, as the deployment starts it: connecting (or switching
        On, or reconnecting) armed the source, so one tick of the worker's clock
        mints its sync, nobody demands it, and the worker's loops run it. *tick*
        is False for a caller that has ticked already. *whole* runs the history
        walk to its end (its gaps skipped by a probe each, as the adapter's
        walk does); otherwise the gate stops once every post is indexed and
        cancels the walk's queued continuation. Two sources' reads are claimed
        in turn, in whichever order their equal due times break, so a scene
        with two waits for both in one *ready*: indexing one at a time would
        cancel the continuation the second still needs."""
        if tick:
            assert self.read_due(source), "nothing armed the source's read"
            minted = await self.tick()
            assert minted["sync_jobs"] == 1, minted
        if whole:
            return await self.drain()
        await self.run_until(
            ready or (lambda: self.indexed(source, past_posts)), limit=150
        )
        self.owner(
            "UPDATE jobs SET state = 'cancelled' WHERE workspace_id = %s"
            " AND state = 'ready' AND kind = 'first_ingest_chunk'",
            self.ws,
        )

    async def plan(self, *accounts):
        """The slot arrives for each account: the clock mints it, the worker
        plans it, and the story is the selected one."""
        for account in accounts:
            self.owner(
                "UPDATE ig_accounts SET next_slot_at = now() - interval '1 minute'"
                " WHERE id = %s",
                account,
            )
        minted = await self.tick()
        assert minted["slot_jobs"] == len(accounts), minted
        await self.drain()
        return [self.intent(a) for a in accounts]

    def intent(self, account):
        found = self.rows(
            "SELECT id::text AS id, state, origin, media_item_id::text AS item"
            "  FROM post_intents WHERE ig_account_id = %s",
            account,
        )
        assert len(found) == 1, found
        return found[0]

    def state_of(self, intent):
        return self.rows("SELECT state FROM post_intents WHERE id = %s", intent)[0][
            "state"
        ]

    async def card(self, account):
        """From the arrived slot to a delivered card: plan, prepare the selected
        story's media, prompt, send."""
        (story,) = await self.plan(account)
        assert story["state"] == "scheduled", story
        await self.sweep_prompts()
        assert self.state_of(story["id"]) == "awaiting_approval"
        await self.deliver()
        return story["id"]

    async def sweep(self):
        """The lifecycle sweep's beat: the clock mints it, a minute passes (every
        waiting job and every scope's next attempt falls due), and the worker
        runs it and the cleanup jobs it finds."""
        self.app.recurring["past_posts_lifecycle_sweep"] = 60.0
        await self.tick()
        self.owner(
            "UPDATE jobs SET run_at = now() WHERE state = 'ready' AND run_at > now()"
        )
        self.owner(
            "UPDATE meta_deletion_receipt_scopes SET next_attempt_at = now()"
            " WHERE state IN ('pending', 'cleaning')"
        )
        return await self.drain()

    def residue(self, source):
        """What the past-posts source still holds of *source*: its per-generation records,
        the items not yet redacted, and the tombstones that keep item identity."""
        return self.rows(
            "WITH p AS (SELECT CAST(%s AS uuid) AS s, CAST(%s AS uuid) AS w)"
            " SELECT"
            " (SELECT count(*) FROM instagram_media_index, p WHERE source_id = p.s) AS indexed,"
            " (SELECT count(*) FROM instagram_media_assets, p WHERE source_id = p.s) AS assets,"
            " (SELECT count(*) FROM instagram_intent_provenance, p WHERE source_id = p.s) AS intent_provenance,"
            " (SELECT count(*) FROM instagram_operation_provenance, p WHERE source_id = p.s) AS operation_provenance,"
            " (SELECT count(*) FROM instagram_artifacts, p WHERE source_id = p.s) AS artifacts,"
            " (SELECT count(*) FROM media_items, p WHERE source_id = p.s AND NOT source_projection_redacted) AS live_items,"
            " (SELECT count(*) FROM media_items, p WHERE source_id = p.s) AS items,"
            " (SELECT count(*) FROM instagram_item_tombstones, p WHERE workspace_id = p.w) AS tombstones",
            source,
            self.ws,
        )[0]

    def identities(self, source):
        """The stable item each parent post has: {parent media id: item id}."""
        return {
            r["parent"]: r["item"]
            for r in self.rows(
                "SELECT asset_media_id AS parent, media_item_id::text AS item"
                "  FROM instagram_media_assets WHERE source_id = %s",
                source,
            )
        }

    def job_counts(self, kind=None):
        sql = "SELECT state, count(*) AS n FROM jobs WHERE workspace_id = %s"
        sql += " AND kind = %s" if kind else ""
        sql += " GROUP BY state"
        found = self.rows(sql, self.ws, *([kind] if kind else []))
        return {r["state"]: r["n"] for r in found}


@contextlib.asynccontextmanager
async def armed_gate(world, monkeypatch, tmp_path, *, drive=()):
    """The composed world: the API and the worker, both armed from records
    written here, the fakes, and a workspace of its own. *drive* is
    `(name, enabled)` Drive folders connected (and left quiet) beforehand."""
    ig = fakes.FakeInstagram()
    api_env, worker_env = armed_environments(tmp_path)
    ingress = create_async_engine(asyncpg_url(world["ingress"]), poolclass=NullPool)
    engine = create_async_engine(async_url(world["worker"]))
    clients = [ig.client(), ig.client()]
    telegram, transit, meta = fakes.FakeTelegram(), fakes.FakeTransit(), GateMeta()
    validated = []
    measured = media_validation.validate_media

    async def spy(value, **kwargs):
        validated.append(value.data)
        return await measured(value, **kwargs)

    monkeypatch.setattr(media_validation, "validate_media", spy)
    try:
        app = create_app(engine=ingress, env=api_env, past_posts_client=clients[0])
        app.state.email = None
        assert app.state.past_posts_capability == PastPostsCapability(enabled=True)
        provider = oauth.Provider()
        monkeypatch.setattr(ig_login_oauth, "exchange_code", provider.exchange_code)
        asgi = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=asgi, base_url=API) as client:
            scene = oauth.Scene(
                world=world,
                client=client,
                app=app,
                engine=ingress,
                provider=provider,
                monkeypatch=monkeypatch,
                tag=f"s8-{uuid.uuid4().hex[:8]}",
            )
            scene.owner, scene.user_id, scene.ws = await owned_workspace(
                client, monkeypatch, tag=scene.tag
            )
            armed = past_posts_composition.build_worker(
                engine=engine,
                env=worker_env,
                client=clients[1],
                policy=fakes.OFFLINE,
            )
            assert armed is not None, "the worker's key ring did not arm it"
            adapters = {"gdrive": DriveForbidden()}
            transport = telegram.transport(
                media_fetch=worker._card_media_fetch(
                    engine, adapters, fetch_selected=armed.coordinator.fetch_selected
                )
            )
            clients.append(transport)  # closes the Bot API client it holds
            composed = worker.compose(
                engine=engine,
                config=WorkerConfig(
                    poller_interval_seconds=0.01, sender_hold_seconds=3
                ),
                env=worker_env,
                transport=transport,
                adapters=adapters,
                past_posts=armed,
                meta=meta,
                transit=transit,
            )
            # The clock mints only what a scene asks for.
            composed.recurring.clear()
            composed.recurring["v"] = 1
            gate = Gate(
                world=world,
                scene=scene,
                ig=ig,
                engine=engine,
                app=composed,
                armed=armed,
                telegram=telegram,
                transit=transit,
                meta=meta,
                validated=validated,
                monkeypatch=monkeypatch,
            )
            for name, enabled in drive:
                await drive_folder(gate, name, enabled=enabled)
            yield gate
    finally:
        for client in clients:
            await client.aclose()
        await ingress.dispose()
        await engine.dispose()


async def drive_folder(gate, name, *, enabled=True):
    """A connected Drive folder with one file, as the pick leaves it, and quiet:
    nothing is due, so no test's clock tick syncs a folder through Drive."""
    scene = gate.scene
    slug = name.lower().replace(" ", "-")
    body = {
        "protocol_version": 2,
        "expected_revision": scene.mix()["revision"],
        "folder_ref": f"drive-folder-{slug}",
        "folder_name": name,
        "enabled": enabled,
    }
    created = await scene.in_tenant(
        lambda s: versioned_mix.connect_drive(s, workspace_id=scene.ws, body=body)
    )
    source = created["source_id"]
    scene.drive[name] = source
    gate.owner("UPDATE media_sources SET next_sync_at = NULL WHERE id = %s", source)
    gate.owner(
        "INSERT INTO media_items (workspace_id, source_id, content_hash, file_name,"
        " media_kind, provider_file_ref)"
        " VALUES (%s, %s, %s, 'drive.jpg', 'image', %s)",
        scene.ws,
        source,
        f"drive-hash-{slug}",
        f"drive-file-{slug}",
    )
    return source


def cdn_path(post):
    return urlsplit(post.parent["media_url"]).path


def refusal(response, status=409):
    """A refused request's body, read only once the status is the refusal's: a
    request that was accepted fails here, by name, not on a missing key."""
    assert response.status_code == status, response.text
    return response.json()


async def test_a_connected_past_posts_runs_from_connect_to_a_posted_audit(
    world, google_configured, instagram_configured, monkeypatch, tmp_path
):
    async with armed_gate(world, monkeypatch, tmp_path) as g:
        past_posts = past_posts_of(REF_A)
        post, groups = candidate_of(past_posts), groups_of(past_posts)
        g.publishing()
        g.bind()

        # Armed by the key ring alone, not by anything the test hands over.
        assert g.app.deps.past_posts_capability == PastPostsCapability(enabled=True)
        assert g.armed.evidence == past_posts_activation.release_evidence(
            activation_gate.WORKER_SHA
        )
        assert g.app.deps.past_posts is g.armed.coordinator
        assert not isinstance(g.app.registry["sync_media_source"], work_loop.Parked)

        # Connect with history On: one source, its origin assignment, one
        # generation, one verified mapping, and not a single provider read, job
        # or byte. The connect armed the first read, though: it is due, and
        # nobody has asked for it.
        landed = await g.connect(REF_A, "example_handle_a", past_posts)
        assert landed.result["outcome"] == "connected_history_on"
        assert landed.result["history_enabled"] is True
        assert g.ig.requests == [] and g.job_counts() == {}
        assert g.read_due(landed.source)
        # The source is assigned to the account it was read from, and to no other:
        # exactly one destination, active, the origin's.
        assert g.rows(
            "SELECT a.provider_account_ref AS origin, d.ig_account_id::text AS account,"
            "       d.state"
            "  FROM media_source_destinations d JOIN ig_accounts a"
            "    ON a.workspace_id = d.workspace_id AND a.id = d.ig_account_id"
            " WHERE d.source_id = %s",
            landed.source,
        ) == [{"origin": REF_A, "account": landed.account, "state": "active"}]
        (generation,) = g.rows(
            "SELECT app_id, subject_id, lifecycle FROM instagram_past_posts_generations"
            " WHERE workspace_id = %s",
            g.ws,
        )
        assert generation == {
            "app_id": oauth.APP_ID,
            "subject_id": REF_A,
            "lifecycle": "active",
        }
        (mapping,) = g.rows(
            "SELECT app_identity, subject_ref, account_ref, method, is_current"
            "  FROM meta_identity_mappings WHERE workspace_id = %s",
            g.ws,
        )
        assert mapping == {
            "app_identity": APP,
            "subject_ref": oauth.app_scoped_of(REF_A),
            "account_ref": REF_A,
            "method": "oauth_exchange",
            "is_current": True,
        }

        # One tick of the clock mints the first read, as the source's own
        # baseline sync: no command asked for it, and the tick takes the arm.
        minted = await g.tick()
        assert minted["sync_jobs"] == 1, minted
        assert g.rows(
            "SELECT kind, payload->>'reason' AS reason FROM jobs WHERE workspace_id = %s",
            g.ws,
        ) == [{"kind": "sync_media_source", "reason": "baseline"}]
        assert not g.read_due(landed.source)

        # Indexing is metadata only: the whole history walk, the exclusions each
        # with their own reason, the 2022 post parsed from Graph's own time
        # format, and no byte, decode or durable URL.
        walked = await g.index(landed.source, past_posts, whole=True, tick=False)
        # The walk is bounded by the posts it finds, not by the account's age: a
        # gap is one probe, never a job for each empty window.
        assert walked < 60, walked
        checkpoint = g.rows(
            "SELECT sync_checkpoint FROM media_sources WHERE id = %s", landed.source
        )[0]["sync_checkpoint"]
        summary = checkpoint["summary"]
        assert checkpoint["recent"]["complete"] and checkpoint["history"]["complete"]
        assert (summary["observed"], summary["represented"]) == (5, 5)
        assert summary["candidates"] == groups["not_detected"]
        assert summary["candidate_exclusions"] == {
            "too_recent": groups["recent"],
            "credited_excluded": groups["credited"],
            "attribution_unknown": groups["unknown"],
        }
        assert (summary["eligible"], summary["verified_ready"]) == (0, 0)
        assert (
            summary["coverage_from"] == datetime.fromtimestamp(post.at, UTC).isoformat()
        )
        assert g.ig.cdn == [] and g.validated == []
        assert {s.account for s in g.ig.graph} == {REF_A}
        indexed = g.rows(
            "SELECT i.parent_media_id, i.original_at, i.attribution, a.validation_state"
            "  FROM instagram_media_index i JOIN instagram_media_assets a"
            " ON a.workspace_id=i.workspace_id AND a.source_id=i.source_id AND a.parent_media_id=i.parent_media_id"
            " WHERE i.source_id = %s ORDER BY i.original_at",
            landed.source,
        )
        assert indexed[0] == {
            "parent_media_id": post.parent["id"],
            "original_at": datetime(2022, 6, 15, 9, 30, tzinfo=UTC),
            "attribution": "not_detected",
            "validation_state": "pending",
        }
        assert {r["parent_media_id"]: r["attribution"] for r in indexed} == {
            p.parent["id"]: p.group
            if p.group in ("credited", "unknown")
            else "not_detected"
            for p in past_posts.posts
        }
        assert {r["validation_state"] for r in indexed} == {"pending"}
        assert rows_holding(g.world["stream"], onboarding.URL_MARKER) == {}

        # The slot arrives and selection picks the one candidate; choosing it
        # reads nothing.
        (story,) = await g.plan(landed.account)
        assert (story["state"], story["origin"]) == ("scheduled", "cadence")
        assert g.rows(
            "SELECT provider_file_ref, source_id::text AS source FROM media_items"
            " WHERE id = %s",
            story["item"],
        ) == [{"provider_file_ref": post.parent["id"], "source": landed.source}]
        assert g.ig.cdn == []

        # Only now are bytes read, once, and judged by the real validator.
        await g.sweep_prompts()
        assert g.state_of(story["id"]) == "awaiting_approval"
        assert [s.path for s in g.ig.cdn] == [cdn_path(post)]
        assert g.validated == [fakes.jpeg()]
        assert g.rows(
            "SELECT i.validation_state, p.selected_asset_identity, p.capture_epoch"
            "  FROM instagram_media_assets i JOIN instagram_intent_provenance p"
            "    ON p.media_item_id = i.media_item_id WHERE p.intent_id = %s",
            story["id"],
        ) == [
            {
                "validation_state": "supported",
                "selected_asset_identity": post.parent["id"],
                "capture_epoch": 1,
            }
        ]

        # The card is the rendered approval: the picture and the account, then
        # the edit that gives that message its taps.
        await g.deliver()
        (photo,) = g.telegram.sent("sendPhoto")
        assert fakes.jpeg() in photo.body
        assert fakes.multipart_field(photo, "caption").startswith(
            "\U0001f4f8 @example_handle_a"
        )
        assert fakes.multipart_field(photo, "reply_markup") is None
        (armed,) = g.telegram.sent("editMessageReplyMarkup")
        markup = armed.json["reply_markup"]
        assert [
            b["callback_data"]
            for row in markup["inline_keyboard"]
            for b in row
            if "callback_data" in b
        ] == [
            # The decision taps, then Configure, which every approval card
            # carries (#1477).
            f"v1:{verb}:{story['id']}"
            for verb in ("post", "posted", "skip", "reject", "cfg")
        ]
        (notice,) = g.telegram.sent("sendMessage")
        assert "@example_handle_a has no content left" in notice.json["text"]
        assert len(g.ig.cdn) == 2 and len(g.validated) == 2

        # The tap is the command port's: it queues the publish and posts nothing.
        approved = await g.command("approve", intent_id=story["id"])
        assert approved.status_code == 202, approved.text
        assert approved.json() == {
            "outcome": "enqueued",
            "intent_id": story["id"],
            "state": "approved",
            "job": "publish_pipeline",
            "dry_run": False,
            "paused": False,
        }
        assert g.meta.create_calls == [] and g.transit.uploads == []

        # The worker publishes it through the past-posts coordinator: the bytes are
        # staged to transit once, the container is made over that copy, the
        # post is published, and the staged copy is destroyed.
        await g.drain()
        (intent,) = g.rows(
            "SELECT state, publish_step, published_via FROM post_intents WHERE id = %s",
            story["id"],
        )
        assert intent == {
            "state": "posted",
            "publish_step": "effect_confirmed",
            "published_via": "api",
        }
        (ref,) = g.transit.uploads
        assert g.meta.create_calls == [
            {
                "ref": REF_A,
                "media_url": g.transit.delivery_url(ref, media_kind="image"),
                "media_kind": "image",
                "caption": None,
            }
        ]
        assert len(g.meta.publish_calls) == 1
        assert g.transit.destroyed == [ref] and g.transit.assets == {}
        assert len(g.ig.cdn) == 3 and g.validated == [fakes.jpeg()] * 3
        assert g.rows(
            "SELECT outcome, cleanup_state FROM instagram_artifacts WHERE intent_id = %s",
            story["id"],
        ) == [{"outcome": "settled", "cleanup_state": "pending"}]
        assert g.rows(
            "SELECT op_kind, state, generation FROM provider_operations"
            " WHERE intent_id = %s ORDER BY op_kind",
            story["id"],
        ) == [
            {"op_kind": "container_create", "state": "succeeded", "generation": 1},
            {"op_kind": "publish", "state": "succeeded", "generation": 1},
        ]
        assert (
            g.rows(
                "SELECT capture_epoch FROM instagram_operation_provenance WHERE intent_id = %s",
                story["id"],
            )
            == [{"capture_epoch": 1}] * 2
        )

        # The final audit and locks: the ledger's whole path, the repost lock,
        # and the story marked posted on its item alone.
        audit = g.rows(
            "SELECT from_state, to_state FROM audit_events WHERE entity_id = %s",
            story["id"],
        )
        assert sorted((a["from_state"], a["to_state"]) for a in audit) == sorted(
            [
                ("scheduled", "prompt_pending"),
                ("prompt_pending", "awaiting_approval"),
                ("awaiting_approval", "approved"),
                ("approved", "publishing"),
                ("publishing", "posted"),
            ]
        )
        (lock,) = g.rows(
            "SELECT kind, media_item_id::text AS item, expires_at FROM post_locks"
            " WHERE workspace_id = %s",
            g.ws,
        )
        assert (lock["kind"], lock["item"]) == ("recent", story["item"])
        assert lock["expires_at"] > datetime.now(UTC) + timedelta(days=29)
        assert {
            r["id"]: r["last_posted_at"] is not None
            for r in g.rows(
                "SELECT id::text AS id, last_posted_at FROM media_items"
                " WHERE workspace_id = %s",
                g.ws,
            )
        } == {
            r["media_item_id"]: r["media_item_id"] == story["item"]
            for r in g.rows(
                "SELECT media_item_id::text AS media_item_id FROM instagram_media_assets"
                " WHERE source_id = %s",
                landed.source,
            )
        }

        # The card learns the outcome from the ledger, with no byte read.
        await g.deliver()
        edits = [c.json["caption"] for c in g.telegram.sent("editMessageCaption")]
        assert len(edits) == 2
        assert "Approved by" in edits[0] and "Posted" in edits[1]
        assert len(g.ig.cdn) == 3


async def test_posted_myself_closes_a_past_posts_card_without_a_provider_call(
    world, google_configured, instagram_configured, monkeypatch, tmp_path
):
    async with armed_gate(world, monkeypatch, tmp_path) as g:
        past_posts = past_posts_of(REF_A)
        g.bind()  # the workspace publishes by hand: api publishing stays off
        landed = await g.connect(REF_A, "example_handle_a", past_posts)
        await g.index(landed.source, past_posts)
        story = await g.card(landed.account)
        reads = (len(g.ig.graph), len(g.ig.cdn))
        assert reads[1] == 2, (
            "the card's media is read once to prepare it, once to send"
        )

        # A Post now tap is refused by name here, and nothing moves.
        refused = await g.command("approve", intent_id=story)
        assert refusal(refused)["reason"] == "manual_mode"
        assert g.state_of(story) == "awaiting_approval"
        assert g.job_counts("publish_pipeline") == {}

        # The person posts by hand and taps Posted myself.
        done = await g.command("mark_posted", intent_id=story)
        assert done.status_code == 200, done.text
        assert done.json() == {
            "outcome": "executed",
            "intent_id": story,
            "state": "posted",
            "published_via": "manual",
        }
        assert g.rows(
            "SELECT state, publish_step, published_via FROM post_intents WHERE id = %s",
            story,
        ) == [{"state": "posted", "publish_step": "none", "published_via": "manual"}]
        audit = g.rows(
            "SELECT from_state, to_state FROM audit_events WHERE entity_id = %s", story
        )
        assert sorted((a["from_state"], a["to_state"]) for a in audit) == sorted(
            [
                ("scheduled", "prompt_pending"),
                ("prompt_pending", "awaiting_approval"),
                ("awaiting_approval", "posted"),
            ]
        )
        assert [r["kind"] for r in g.rows("SELECT kind FROM post_locks")] == ["recent"]
        assert g.rows(
            "SELECT last_posted_at IS NOT NULL AS posted FROM media_items WHERE id = %s",
            g.intent(landed.account)["item"],
        ) == [{"posted": True}]

        # No provider was asked for anything, and the card learns the outcome
        # without fetching a byte.
        await g.deliver()
        (edit,) = g.telegram.sent("editMessageCaption")
        assert "Posted by" in edit.json["caption"]
        assert g.meta.create_calls == [] and g.transit.uploads == []
        assert (len(g.ig.graph), len(g.ig.cdn)) == reads


async def test_off_then_on_holds_a_waiting_card_without_reading_and_resumes_it(
    world, google_configured, instagram_configured, monkeypatch, tmp_path
):
    async with armed_gate(world, monkeypatch, tmp_path) as g:
        past_posts = past_posts_of(REF_A)
        g.publishing()
        g.bind()
        landed = await g.connect(REF_A, "example_handle_a", past_posts)
        await g.index(landed.source, past_posts)
        story = await g.card(landed.account)
        reads = (len(g.ig.graph), len(g.ig.cdn), len(g.validated))
        epoch = g.rows(
            "SELECT source_use_epoch FROM media_sources WHERE id = %s", landed.source
        )[0]["source_use_epoch"]

        # Off, through the mix route: the switch is the source's share, and the
        # epoch moves, which fences everything that captured the old one.
        off = await g.switch(landed.source, False)
        assert off.status_code == 200, off.text
        assert off.json()["revision"] == 2 and off.json()["reset_explanation"] == (
            "enabled_membership_changed"
        )
        assert g.scene.mix()["ratios"] == {landed.source: 0}
        assert g.rows(
            "SELECT source_use_epoch FROM media_sources WHERE id = %s", landed.source
        ) == [{"source_use_epoch": epoch + 1}]

        # While Off: a read is refused by name, so is the waiting card's
        # approval, and neither the sweeps nor the sender touch a provider. The
        # switch armed nothing: the first read's due time was taken at its mint.
        assert not g.read_due(landed.source)
        assert g.rows(
            "SELECT next_sync_at FROM media_sources WHERE id = %s", landed.source
        ) == [{"next_sync_at": None}]
        sync = await g.command("sync_now", source_id=landed.source)
        assert refusal(sync)["facts"] == {
            "source_id": landed.source,
            "source_read_reason": "source_off",
        }
        tap = refusal(await g.command("approve", intent_id=story))
        assert (tap["reason"], tap["facts"]) == (
            "media_unsuitable",
            {"source_reason": "source_off"},
        )
        await g.sweep_prompts()
        await g.deliver()
        assert g.state_of(story) == "awaiting_approval"
        assert (len(g.ig.graph), len(g.ig.cdn), len(g.validated)) == reads
        assert g.job_counts("publish_pipeline") == {}

        # On again: the same story is approvable, and the read is open again.
        # Switching On armed it, so the clock resumes it: one tick mints the
        # sync, and nobody has to ask.
        on = await g.switch(landed.source, True)
        assert on.status_code == 200 and on.json()["revision"] == 3
        assert g.scene.mix()["ratios"] == {landed.source: 1}
        assert g.read_due(landed.source)
        resumed = await g.tick()
        assert resumed["sync_jobs"] == 1, resumed
        assert not g.read_due(landed.source)
        tap = await g.command("approve", intent_id=story)
        assert tap.status_code == 202, tap.text

        # The publish renews the authorization (audited, at the new epoch)
        # without staging the story twice, and posts it once.
        await g.run_until(lambda: g.state_of(story) == "posted")
        assert len(g.meta.create_calls) == len(g.meta.publish_calls) == 1
        assert len(g.transit.uploads) == 1
        assert g.rows(
            "SELECT detail FROM audit_events WHERE entity_id = %s"
            " AND detail->>'reason' = 'past_posts_authorization_renewed'",
            story,
        ) == [
            {
                "detail": {
                    "reason": "past_posts_authorization_renewed",
                    "capture_epoch": epoch + 2,
                    "policy_revision": 1,
                }
            }
        ]
        assert g.rows(
            "SELECT capture_epoch FROM instagram_intent_provenance WHERE intent_id = %s",
            story,
        ) == [{"capture_epoch": epoch + 2}]
        assert len(g.validated) == len(g.ig.cdn) == reads[1] + 1


async def test_a_source_policy_revision_withdraws_a_waiting_cards_approval_until_it_qualifies_again(
    world, google_configured, instagram_configured, monkeypatch, tmp_path
):
    async with armed_gate(world, monkeypatch, tmp_path) as g:
        past_posts = past_posts_of(REF_A)
        g.publishing()
        g.bind()
        landed = await g.connect(REF_A, "example_handle_a", past_posts)
        await g.index(landed.source, past_posts)
        story = await g.card(landed.account)
        url = g.scene.url(f"sources/{landed.source}/policy")

        def save(revision, minimum_age_days):
            return g.scene.client.put(
                url,
                json={
                    "schema_version": 1,
                    "expected_revision": revision,
                    "minimum_age_days": minimum_age_days,
                    "include_credited": False,
                    "include_unknown": False,
                },
                headers=g.scene.owner,
            )

        saved = await g.scene.client.get(url, headers=g.scene.owner)
        assert (
            saved.json()["revision"],
            saved.json()["policy"]["minimum_age_days"],
        ) == (
            1,
            90,
        )

        # A 100-year minimum age: the 2022 post is now too recent, so the card
        # that was approvable a moment ago is refused by name, and nothing queues.
        raised = await save(1, 36500)
        assert raised.status_code == 200, raised.text
        assert (raised.json()["revision"], raised.json()["effective"]) == (
            2,
            {"minimum_age_days": 36500},
        )
        tap = refusal(await g.command("approve", intent_id=story))
        assert (tap["reason"], tap["facts"]) == (
            "media_unsuitable",
            {"source_reason": "too_recent"},
        )
        assert g.state_of(story) == "awaiting_approval"
        assert g.job_counts("publish_pipeline") == {}

        # A save against the revision it has already left is a conflict that
        # names the current one, and changes nothing.
        stale = await save(1, 90)
        conflict = refusal(stale)
        assert (conflict["reason"], conflict["current_revision"]) == (
            "policy_conflict",
            2,
        )

        # The policy moves back on: the same card is approvable, and the publish
        # records the revision it was authorized under.
        restored = await save(2, 90)
        assert (restored.status_code, restored.json()["revision"]) == (200, 3)
        tap = await g.command("approve", intent_id=story)
        assert tap.status_code == 202, tap.text
        await g.run_until(lambda: g.state_of(story) == "posted")
        assert g.rows(
            "SELECT last_authorized_policy_revision, capture_epoch"
            "  FROM instagram_intent_provenance WHERE intent_id = %s",
            story,
        ) == [{"last_authorized_policy_revision": 3, "capture_epoch": 1}]
        assert g.rows(
            "SELECT detail FROM audit_events WHERE entity_id = %s"
            " AND detail->>'reason' = 'past_posts_authorization_renewed'",
            story,
        ) == [
            {
                "detail": {
                    "reason": "past_posts_authorization_renewed",
                    "capture_epoch": 1,
                    "policy_revision": 3,
                }
            }
        ]
        assert len(g.meta.create_calls) == len(g.transit.uploads) == 1


async def test_a_permit_refused_at_the_boundary_keeps_the_staged_copy_and_resumes_without_a_second_upload(
    world, google_configured, instagram_configured, monkeypatch, tmp_path
):
    async with armed_gate(world, monkeypatch, tmp_path) as g:
        past_posts = past_posts_of(REF_A)
        g.publishing()
        g.bind()
        landed = await g.connect(REF_A, "example_handle_a", past_posts)
        await g.index(landed.source, past_posts)
        story = await g.card(landed.account)
        epoch = g.rows(
            "SELECT source_use_epoch FROM media_sources WHERE id = %s", landed.source
        )[0]["source_use_epoch"]

        # The source goes Off the moment the container exists: the container
        # permit was already issued, and the publish permit is the next door.
        async def off():
            assert (await g.switch(landed.source, False)).status_code == 200

        g.meta.after_create = off
        assert (await g.command("approve", intent_id=story)).status_code == 202
        await g.drain()
        (held,) = g.rows(
            "SELECT state, publish_step, ig_container_id, last_error"
            "  FROM post_intents WHERE id = %s",
            story,
        )
        assert (held["state"], held["publish_step"], held["ig_container_id"]) == (
            "approved",
            "container_ready",
            "ctr-1",
        )
        assert held["last_error"]["error"]["reason"] == "source_off"
        # Nothing was published, the staged copy is still held for the retry, and
        # the job waits (its attempt given back) rather than failing.
        assert len(g.meta.create_calls) == 1 and g.meta.publish_calls == []
        (ref,) = g.transit.uploads
        assert g.transit.destroyed == [] and list(g.transit.assets) == [ref]
        assert g.rows(
            "SELECT op_kind, state FROM provider_operations WHERE intent_id = %s", story
        ) == [{"op_kind": "container_create", "state": "succeeded"}]
        assert g.rows(
            "SELECT state, attempts, run_at > now() AS later FROM jobs"
            " WHERE kind = 'publish_pipeline'"
        ) == [{"state": "ready", "attempts": 0, "later": True}]
        reads = (len(g.ig.graph), len(g.ig.cdn))

        # On again, and the wait is over: the retry resumes at the container,
        # stages nothing, creates nothing, reads nothing, and publishes once.
        g.meta.after_create = None
        assert (await g.switch(landed.source, True)).status_code == 200
        g.owner(
            "UPDATE jobs SET run_at = now() WHERE kind = 'publish_pipeline'"
            " AND state = 'ready'"
        )
        await g.run_until(lambda: g.state_of(story) == "posted")
        assert len(g.meta.create_calls) == len(g.meta.publish_calls) == 1
        assert g.transit.uploads == [ref] and g.transit.destroyed == [ref]
        assert (len(g.ig.graph), len(g.ig.cdn)) == reads
        assert g.rows(
            "SELECT op_kind, state, generation FROM provider_operations"
            " WHERE intent_id = %s ORDER BY op_kind",
            story,
        ) == [
            {"op_kind": "container_create", "state": "succeeded", "generation": 1},
            {"op_kind": "publish", "state": "succeeded", "generation": 1},
        ]

        # Each permit carries the epoch it was issued under, and the renewal
        # between them is audited: the container at the old one, the publish
        # at the one the Off and On made.
        assert g.rows(
            "SELECT o.op_kind, p.capture_epoch FROM instagram_operation_provenance p"
            "  JOIN provider_operations o ON o.id = p.operation_id"
            " WHERE p.intent_id = %s ORDER BY o.op_kind",
            story,
        ) == [
            {"op_kind": "container_create", "capture_epoch": epoch},
            {"op_kind": "publish", "capture_epoch": epoch + 2},
        ]
        assert g.rows(
            "SELECT capture_epoch FROM instagram_intent_provenance WHERE intent_id = %s",
            story,
        ) == [{"capture_epoch": epoch + 2}]
        assert g.rows(
            "SELECT detail FROM audit_events WHERE entity_id = %s"
            " AND detail->>'reason' = 'past_posts_authorization_renewed'",
            story,
        ) == [
            {
                "detail": {
                    "reason": "past_posts_authorization_renewed",
                    "capture_epoch": epoch + 2,
                    "policy_revision": 1,
                }
            }
        ]


FULL = {
    "indexed": 5,
    "assets": 7,
    "intent_provenance": 1,
    "operation_provenance": 2,
    "artifacts": 1,
    "live_items": 7,
    "items": 7,
    "tombstones": 0,
}
CLEANED = {
    "indexed": 0,
    "assets": 0,
    "intent_provenance": 0,
    "operation_provenance": 0,
    "artifacts": 0,
    "live_items": 0,
    "items": 7,
    "tombstones": 7,
}


async def test_a_repeated_deletion_episode_cleans_each_generation_once_and_spares_the_rest(
    world, google_configured, instagram_configured, monkeypatch, tmp_path
):
    async with armed_gate(
        world, monkeypatch, tmp_path, drive=(("Folder", False),)
    ) as g:
        past_posts = past_posts_of(REF_A)
        g.publishing()
        g.bind()
        landed = await g.connect(REF_A, "example_handle_a", past_posts)
        await g.index(landed.source, past_posts)
        story = await g.card(landed.account)
        assert (await g.command("approve", intent_id=story)).status_code == 202
        await g.run_until(lambda: g.state_of(story) == "posted")
        # The story is posted and its card's outcome edits are still queued.
        assert g.residue(landed.source) == FULL
        identity = g.identities(landed.source)
        drive = g.rows(
            "SELECT provider_file_ref, state, source_data_generation_id FROM media_items"
            " WHERE provider_file_ref = 'drive-file-folder'"
        )
        subject = oauth.app_scoped_of(REF_A)
        monkeypatch.setattr(settings, "INSTAGRAM_APP_SECRET", SECRET, raising=False)

        # Episode one. The request is a durable receipt that freezes the one
        # generation; it deletes nothing by itself.
        answered, segment, _ = await receipts.request_deletion(g.scene, SECRET, subject)
        assert answered.status_code == 200, answered.text
        code = answered.json()["confirmation_code"]
        first = receipts.receipt_of(g.world, code)
        (g1,) = [x["id"] for x in receipts.generations_of(g.world, g.ws)]
        assert (first["state"], receipts.frozen_of(g.world, first["id"])) == (
            "pending",
            {g1},
        )
        assert (await receipts.status_of(g.scene, code)).json()[
            "status"
        ] == "in_progress"
        assert g.residue(landed.source) == FULL

        # The first pass fences the generation (no read is admitted after it) and
        # stops: a queued card edit still needs its routing, so nothing that
        # names the story is redacted yet and the scope reports cleaning.
        reads = len(g.ig.graph)
        await g.sweep()
        assert g.rows(
            "SELECT lifecycle FROM instagram_past_posts_generations WHERE id = %s", g1
        ) == [{"lifecycle": "deletion_pending"}]
        sync = await g.command("sync_now", source_id=landed.source)
        assert refusal(sync)["facts"]["source_read_reason"] == "generation_unavailable"
        assert [s["state"] for s in receipts.scopes_of(g.world, first["id"])] == [
            "cleaning"
        ]
        assert g.residue(landed.source) == FULL
        assert (await receipts.status_of(g.scene, code)).json()[
            "status"
        ] == "in_progress"

        # The sender delivers the edits; the next pass redacts everything the
        # generation owns, and only then does the receipt complete.
        await g.deliver()
        await g.sweep()
        assert receipts.receipt_of(g.world, code)["state"] == "completed"
        assert (await receipts.status_of(g.scene, code)).json()["status"] == "completed"
        assert [s["state"] for s in receipts.scopes_of(g.world, first["id"])] == [
            "completed"
        ]
        assert g.residue(landed.source) == CLEANED
        assert g.rows(
            "SELECT lifecycle FROM instagram_past_posts_generations WHERE id = %s", g1
        ) == [{"lifecycle": "deleted"}]
        assert g.transit.assets == {} and len(g.transit.lookups) == 1
        assert len(g.ig.graph) == reads, "nothing was read from Instagram to delete"
        # What is not the past-posts source's survives: the posted story and its lock, the
        # item's identity (as a tombstone), Drive, and the account's grant.
        assert g.state_of(story) == "posted"
        assert [r["kind"] for r in g.rows("SELECT kind FROM post_locks")] == ["recent"]
        assert (
            g.rows(
                "SELECT provider_file_ref, state, source_data_generation_id FROM media_items"
                " WHERE provider_file_ref = 'drive-file-folder'"
            )
            == drive
        )

        # An exact retry is the same receipt behind a second capability, already
        # complete, and it starts no cleanup.
        again, _, _ = await receipts.request_deletion(
            g.scene, SECRET, subject, segment=segment
        )
        replay = again.json()["confirmation_code"]
        assert replay != code
        assert receipts.receipt_of(g.world, replay)["id"] == first["id"]
        assert (await receipts.status_of(g.scene, replay)).json()[
            "status"
        ] == "completed"
        assert g.job_counts("meta_deletion_cleanup") == {"succeeded": 2}

        # The person reconnects: a new authorization, a new generation, the saved
        # choice kept. The next authorized read restores each post's item from
        # its tombstone, and the story's repost lock still holds the 2022 post.
        monkeypatch.setattr(settings, "INSTAGRAM_APP_SECRET", "sec", raising=False)
        state = oauth.state_of(await g.scene.start(landed.account, choice=None))
        back = await g.scene.callback(state, REF_A, "example_handle_a")
        outcome = (await g.scene.result(oauth.connection_of(back))).json()
        assert (outcome["outcome"], outcome["history_enabled"]) == (
            "reconnected_preserved",
            True,
        )
        monkeypatch.setattr(settings, "INSTAGRAM_APP_SECRET", SECRET, raising=False)
        await g.index(
            landed.source,
            past_posts,
            ready=lambda: (
                g.summary(landed.source).get("observed") == len(past_posts.posts)
                and "locked" in g.summary(landed.source).get("candidate_exclusions", {})
            ),
        )
        assert g.identities(landed.source) == identity
        assert g.summary(landed.source)["candidate_exclusions"]["locked"] == 1
        (g2,) = [
            x["id"] for x in receipts.generations_of(g.world, g.ws) if x["id"] != g1
        ]
        assert g.residue(landed.source)["indexed"] == 5

        # A callback issued before either grant deletes nothing of the new one:
        # verified ordering excludes a newer authorization from an old request.
        old, _, _ = await receipts.request_deletion(
            g.scene, SECRET, subject, issued_at=int(time.time()) - 3600
        )
        stale = receipts.receipt_of(g.world, old.json()["confirmation_code"])
        assert (stale["state"], receipts.scopes_of(g.world, stale["id"])) == (
            "completed",
            [],
        )
        assert g.residue(landed.source)["indexed"] == 5

        # Episode two: a new request, ordered after the reconnect, is a new
        # receipt over the new generation alone. The door orders a request against
        # a grant by Meta's `issued_at`, in whole seconds, with a 300-second skew:
        # a grant is "before" a request only when it is more than the skew
        # older, and a request cannot claim more than the skew in the future. So
        # the request is issued as far ahead as that admits, once the grant is
        # old enough by the database's own clock for that to be after it.
        await g.until(
            "SELECT now() - authorized_at >= interval '3 seconds' AS ready"
            "  FROM meta_identity_mappings WHERE workspace_id = %s AND is_current",
            g.ws,
        )
        fresh, _, _ = await receipts.request_deletion(
            g.scene, SECRET, subject, issued_at=int(time.time()) + 299
        )
        code2 = fresh.json()["confirmation_code"]
        second = receipts.receipt_of(g.world, code2)
        assert second["id"] != first["id"] and second["state"] == "pending"
        assert receipts.frozen_of(g.world, second["id"]) == {g2}
        assert receipts.frozen_of(g.world, first["id"]) == {g1}
        await g.sweep()
        assert receipts.receipt_of(g.world, code2)["state"] == "completed"
        assert (await receipts.status_of(g.scene, code2)).json()[
            "status"
        ] == "completed"
        assert g.residue(landed.source) == CLEANED
        assert {
            x["id"]: x["lifecycle"]
            for x in g.rows(
                "SELECT id::text AS id, lifecycle FROM instagram_past_posts_generations"
                " WHERE workspace_id = %s",
                g.ws,
            )
        } == {g1: "deleted", g2: "deleted"}
        assert receipts.receipt_of(g.world, code)["state"] == "completed"
        assert g.state_of(story) == "posted"
        assert (
            g.rows(
                "SELECT provider_file_ref, state, source_data_generation_id FROM media_items"
                " WHERE provider_file_ref = 'drive-file-folder'"
            )
            == drive
        )


async def test_two_accounts_and_two_workspaces_never_see_each_others_past_posts(
    world, google_configured, instagram_configured, monkeypatch, tmp_path
):
    async with armed_gate(
        world, monkeypatch, tmp_path, drive=(("Folder", False),)
    ) as g:
        arch_a, arch_b = past_posts_of(REF_A), past_posts_of(REF_B, offset=500_000)
        parents_a = {p.parent["id"] for p in arch_a.posts}
        parents_b = {p.parent["id"] for p in arch_b.posts}
        ids_a, ids_b = (
            {
                asset["id"]
                for post in past_posts.posts
                for asset in (
                    post.children
                    if post.parent["media_type"] == "CAROUSEL_ALBUM"
                    else (post.parent,)
                )
            }
            for past_posts in (arch_a, arch_b)
        )
        assert not (parents_a | ids_a) & (parents_b | ids_b)
        g.publishing()
        g.bind()
        drive_before = g.rows(
            "SELECT m.provider_file_ref, m.state, s.state AS source_state,"
            "       s.source_use_epoch FROM media_items m JOIN media_sources s"
            "    ON s.id = m.source_id WHERE s.provider = 'gdrive'"
        )
        a = await g.connect(REF_A, "example_handle_a", arch_a)
        b = await g.connect(REF_B, "example_handle_b", arch_b)
        assert a.account != b.account and a.source != b.source
        assert {a.source: 0.5, b.source: 0.5} == {
            k: float(v)
            for k, v in g.scene.mix()["ratios"].items()
            if k in (a.source, b.source)
        }
        # Both connects armed their first read, so one tick mints both.
        assert g.read_due(a.source) and g.read_due(b.source)
        minted = await g.tick()
        assert minted["sync_jobs"] == 2, minted
        await g.index(
            a.source,
            arch_a,
            tick=False,
            ready=lambda: g.indexed(a.source, arch_a) and g.indexed(b.source, arch_b),
        )

        # Each account's assets hold its own files and no one else's, and every
        # Graph request is made with the token of the account it names.
        assert set(g.identities(a.source)) == ids_a
        assert set(g.identities(b.source)) == ids_b
        assert {s.account for s in g.ig.graph} == {REF_A, REF_B}
        for seen in g.ig.graph:
            foreign = (
                (parents_b | ids_b) if seen.account == REF_A else (parents_a | ids_a)
            )
            assert not any(i in seen.path for i in foreign), seen
        assert g.ig.cdn == []

        # Both slots arrive: each account is given a story out of its own
        # past-posts source, and the Off Drive folder is drawn by neither.
        story_a, story_b = await g.plan(a.account, b.account)
        for story, mine, post, ref in (
            (story_a, a.source, candidate_of(arch_a), REF_A),
            (story_b, b.source, candidate_of(arch_b), REF_B),
        ):
            assert g.rows(
                "SELECT source_id::text AS source, provider_file_ref AS ref"
                "  FROM media_items WHERE id = %s",
                story["item"],
            ) == [{"source": mine, "ref": post.parent["id"]}]
        await g.sweep_prompts()
        await g.deliver()
        assert {
            fakes.multipart_field(c, "caption").splitlines()[0]
            for c in g.telegram.sent("sendPhoto")
        } == {"\U0001f4f8 @example_handle_a", "\U0001f4f8 @example_handle_b"}
        details = {
            (s.account, s.path)
            for s in g.ig.graph
            if s.path.rsplit("/", 1)[-1]
            in {candidate_of(arch_a).parent["id"], candidate_of(arch_b).parent["id"]}
        }
        assert details == {
            (REF_A, f"/v21.0/{candidate_of(arch_a).parent['id']}"),
            (REF_B, f"/v21.0/{candidate_of(arch_b).parent['id']}"),
        }

        # Approving one account's story posts through that account alone.
        assert (await g.command("approve", intent_id=story_a["id"])).status_code == 202
        await g.run_until(lambda: g.state_of(story_a["id"]) == "posted")
        assert [c["ref"] for c in g.meta.create_calls] == [REF_A]
        assert g.state_of(story_b["id"]) == "awaiting_approval"
        assert [
            r["item"]
            for r in g.rows("SELECT media_item_id::text AS item FROM post_locks")
        ] == [story_a["item"]]

        # Off for B moves B's epoch and fences B's card; A is untouched.
        epochs = lambda: {  # noqa: E731
            r["id"]: r["source_use_epoch"]
            for r in g.rows(
                "SELECT id::text AS id, source_use_epoch FROM media_sources"
                " WHERE provider = 'instagram_past_posts'"
            )
        }
        before = epochs()
        assert (await g.switch(b.source, False)).status_code == 200
        after = epochs()
        assert after[a.source] == before[a.source]
        assert after[b.source] == before[b.source] + 1
        tap = await g.command("approve", intent_id=story_b["id"])
        assert refusal(tap)["facts"] == {"source_reason": "source_off"}
        assert (await g.command("sync_now", source_id=a.source)).status_code == 202
        refused = await g.command("sync_now", source_id=b.source)
        assert refusal(refused)["facts"]["source_read_reason"] == "source_off"

        # Another workspace's person sees none of it: not through the API, and
        # not through the database's own policies as the API's login.
        other, other_user, other_ws = await owned_workspace(
            g.scene.client, monkeypatch, tag=f"{g.scene.tag}-other"
        )
        peek = await g.scene.client.get(
            f"/api/v1/workspaces/{g.ws}/sources", headers=other
        )
        assert peek.status_code in (403, 404), peek.text
        mine = await g.scene.client.get(
            f"/api/v1/workspaces/{other_ws}/sources", headers=other
        )
        assert mine.json() == {"sources": []}

        def census(session):
            return session.execute(
                text(
                    "SELECT (SELECT count(*) FROM instagram_media_index) AS indexed,"
                    " (SELECT count(*) FROM media_items) AS items,"
                    " (SELECT count(*) FROM post_intents) AS intents,"
                    " (SELECT count(*) FROM jobs) AS jobs"
                )
            )

        async def counted(ws, user):
            async def read(session):
                return (await census(session)).mappings().one()

            return dict(await in_tenant(world["ingress"], ws, user, read))

        assert await counted(other_ws, other_user) == {
            "indexed": 0,
            "items": 0,
            "intents": 0,
            "jobs": 0,
        }
        seen = await counted(g.ws, g.scene.user_id)
        assert seen["indexed"] == 10 and seen["intents"] == 2 and seen["jobs"] > 0

        # Drive was never touched by any of it.
        assert (
            g.rows(
                "SELECT m.provider_file_ref, m.state, s.state AS source_state,"
                "       s.source_use_epoch FROM media_items m JOIN media_sources s"
                "    ON s.id = m.source_id WHERE s.provider = 'gdrive'"
            )
            == drive_before
        )


async def test_oauth_callback_interleaved_with_mix_save(
    world, google_configured, instagram_configured, monkeypatch, tmp_path
):
    async with armed_gate(
        world, monkeypatch, tmp_path, drive=(("Folder A", True), ("Folder B", True))
    ) as g:
        scene = g.scene
        past_posts = past_posts_of(REF_A)
        g.ig.add(past_posts)
        drive_a, drive_b = scene.drive["Folder A"], scene.drive["Folder B"]
        admin = await scene.member("admin", "admin")

        # The person is shown the History choice at one revision of the mix and
        # sends the browser to Instagram with it.
        revision = (await scene.preview()).json()["expected_mix_revision"]
        state = oauth.state_of(await scene.start(choice=oauth.choice(True, revision)))

        # While Instagram's page is open, another admin saves a custom mix.
        saved = await scene.client.put(
            scene.url("category-mix"),
            json={
                "protocol_version": 2,
                "action": "save_custom",
                "expected_revision": revision,
                "rows": [
                    {"source_id": drive_a, "ratio": "0.6"},
                    {"source_id": drive_b, "ratio": "0.4"},
                ],
            },
            headers=admin,
        )
        assert saved.status_code == 200, saved.text
        assert scene.mix() == {
            "revision": revision + 1,
            "mode": "custom",
            "ratios": {drive_a: Decimal("0.6"), drive_b: Decimal("0.4")},
        }

        # The callback lands: the connection succeeds, the history does not.
        # The conflict is the result's outcome, the source is Off, the other
        # admin's custom mix is exactly what it was, and not one provider read
        # was made or asked for.
        landed = oauth.connection_of(
            await scene.callback(state, REF_A, "example_handle_a")
        )
        result = (await scene.result(landed)).json()
        source = result["source_id"]
        assert {k: v for k, v in result.items() if k != "created_at"} == {
            "account_id": result["account_id"],
            "source_id": source,
            "outcome": "connected_history_off_mix_conflict",
            "history_enabled": False,
            "mix_revision": revision + 2,
            "mix_reset_applied": False,
            "next_action": "review_mix",
            "current_mix_revision": revision + 2,
        }
        custom = {
            "revision": revision + 2,
            "mode": "custom",
            "ratios": {drive_a: Decimal("0.6"), drive_b: Decimal("0.4"), source: 0},
        }
        assert scene.mix() == custom

        async def no_history_read():
            refused = await g.command("sync_now", source_id=source)
            assert refusal(refused)["facts"] == {
                "source_id": source,
                "source_read_reason": "source_off",
            }
            assert g.ig.requests == [] and g.job_counts() == {}
            assert g.rows(
                "SELECT next_sync_at FROM media_sources WHERE id = %s", source
            ) == [{"next_sync_at": None}]

        await no_history_read()

        # Reconnecting keeps the choice that was saved (Off), resets nothing and
        # reads nothing: the conflict is not a request to try History again.
        state = oauth.state_of(await scene.start(result["account_id"], choice=None))
        again = oauth.connection_of(
            await scene.callback(state, REF_A, "example_handle_a")
        )
        kept = (await scene.result(again)).json()
        assert (
            kept["outcome"],
            kept["history_enabled"],
            kept["mix_reset_applied"],
            kept["next_action"],
            kept["mix_revision"],
        ) == ("reconnected_preserved", False, False, None, revision + 2)
        assert scene.mix() == custom
        await no_history_read()

        # Turning History on later is the explicit choice: the enabled sources
        # share one unit equally, it arms the first read, and the clock's next
        # tick sends that out as the account.
        on = await g.switch(source, True)
        assert on.status_code == 200, on.text
        assert scene.mix()["revision"] == revision + 3
        oauth.assert_equal_split(
            scene.mix()["ratios"], on=[drive_a, drive_b, source], off=[]
        )
        assert g.read_due(source) and g.ig.requests == [] and g.job_counts() == {}
        minted = await g.tick()
        assert minted["sync_jobs"] == 1, minted
        await g.run_until(lambda: len(g.ig.graph) >= 1)
        assert {(s.account, s.path.rsplit("/", 1)[-1]) for s in g.ig.graph} == {
            (REF_A, "media")
        }


def test_the_gate_enters_no_worker_or_api_entrypoint():
    """The composition is assembled and its loops are stepped by the test, so a
    run starts no clock, sleeps through no cadence and posts nothing on its own:
    this file never names the entrypoints (`src.main`, `worker.main`/`run`)."""
    tree = ast.parse(Path(__file__).read_text())
    named = [
        f"line {node.lineno}"
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "worker"
            and node.attr in ("main", "run", "_run")
        )
        or (isinstance(node, ast.ImportFrom) and node.module == "src.main")
        or (
            isinstance(node, ast.Import)
            and any(alias.name == "src.main" for alias in node.names)
        )
    ]
    assert named == []


async def test_post_now_on_the_telegram_card_approves_a_past_posts_story_as_the_web_does(
    world, google_configured, instagram_configured, monkeypatch, tmp_path
):
    """A Post now tap on a past-posts card, end to end: the tap reaches
    `approve` through the real webhook and the dispatcher `create_app` wires,
    which carries the API's past-posts capability, so a past-posts story whose
    media validated is approved from its card exactly as scene 1's web Approve
    approves it, and posts once. Before, the tap ran with no capability and
    answered `media_unsuitable`."""
    async with armed_gate(world, monkeypatch, tmp_path) as g:
        past_posts = past_posts_of(REF_A)
        g.publishing()
        g.bind()
        g.link_telegram()
        landed = await g.connect(REF_A, "example_handle_a", past_posts)
        await g.index(landed.source, past_posts)
        story = await g.card(landed.account)
        # The positive control: the media validated, so the web would approve it.
        assert g.rows(
            "SELECT i.validation_state FROM instagram_media_assets i"
            "  JOIN instagram_intent_provenance p ON p.media_item_id = i.media_item_id"
            " WHERE p.intent_id = %s",
            story,
        ) == [{"validation_state": "supported"}]

        tapped = await g.tap("post", story, message_id=g.card_ref(story))
        assert tapped.status_code == 200, tapped.text
        assert tapped.json() == {"status": "admitted", "outcome": "executed"}
        assert g.state_of(story) == "approved"
        assert g.rows(
            "SELECT actor_kind, actor_user_id::text AS actor, channel"
            "  FROM audit_events WHERE entity_kind = 'post_intent'"
            "   AND entity_id = %s AND to_state = 'approved'",
            story,
        ) == [{"actor_kind": "user", "actor": g.scene.user_id, "channel": "telegram"}]
        assert g.rows(
            "SELECT count(*) AS n FROM jobs WHERE kind = 'publish_pipeline'"
            " AND payload->>'intent_id' = %s",
            story,
        ) == [{"n": 1}]
        assert g.meta.create_calls == [] and g.transit.uploads == []

        # The worker publishes it as it publishes scene 1's.
        await g.drain()
        assert g.state_of(story) == "posted"
        assert len(g.meta.create_calls) == len(g.meta.publish_calls) == 1

        # The card learns both lines, the first naming the tapper.
        await g.deliver()
        edits = [c.json["caption"] for c in g.telegram.sent("editMessageCaption")]
        assert len(edits) == 2
        assert "Approved by Ada" in edits[0] and "Posted" in edits[1]


def ten_slide_past_posts():
    """One old post, ten independently addressable files; its last is a video."""
    base = onboarding.useful(10, int(time.time()), owner_id=REF_A)
    post = candidate_of(base)
    children = tuple(
        {
            "id": str(onboarding.CHILD_ID_BASE + 500 + position),
            "media_type": "VIDEO" if position == 10 else "IMAGE",
            "media_url": f"https://{fakes.CDN_HOST}/slide-{position}.{'mp4' if position == 10 else 'jpg'}",
            **(
                {"thumbnail_url": f"https://{fakes.CDN_HOST}/slide-{position}.jpg"}
                if position == 10
                else {}
            ),
        }
        for position in range(1, 11)
    )
    post = replace(
        post, parent={**post.parent, "media_type": "CAROUSEL_ALBUM"}, children=children
    )
    return replace(base, posts=(post,), n=1)


async def test_ten_slide_composition_keeps_each_identity_lock_and_deletion_tombstone(
    world, google_configured, instagram_configured, monkeypatch, tmp_path
):
    async with armed_gate(world, monkeypatch, tmp_path) as g:
        past_posts = ten_slide_past_posts()
        g.publishing()
        g.bind()
        landed = await g.connect(REF_A, "example_handle_a", past_posts)
        await g.index(landed.source, past_posts, whole=True)
        summary = g.summary(landed.source)
        assert (summary["observed"], summary["represented"], summary["candidates"]) == (
            1,
            1,
            10,
        )
        assert (summary["items"], summary["slides"]) == (10, 10)
        assert g.ig.cdn == [] and g.validated == []
        identities = g.identities(landed.source)
        assert set(identities) == {c["id"] for c in past_posts.posts[0].children}
        assert len(set(identities.values())) == 10
        # Mixed-video evidence remains an activation prerequisite. This scene
        # exercises real image validation; hold that one video explicitly so
        # the clock's UUID ordering can select any of the nine image slides.
        video = identities[past_posts.posts[0].children[-1]["id"]]
        g.owner(
            "INSERT INTO post_locks (workspace_id,media_item_id,kind) VALUES (%s,%s,'hold')",
            g.ws,
            video,
        )
        (first,) = await g.plan(landed.account)
        await g.sweep_prompts()
        await g.deliver()
        (asset,) = g.rows(
            "SELECT asset_media_id,position FROM instagram_media_assets WHERE media_item_id=%s",
            first["item"],
        )
        (photo,) = g.telegram.sent("sendPhoto")
        assert (
            f"Instagram post, slide {asset['position']} of 10"
            in fakes.multipart_field(photo, "caption")
        )
        assert g.rows(
            "SELECT selected_asset_identity FROM instagram_intent_provenance WHERE intent_id=%s",
            first["id"],
        ) == [{"selected_asset_identity": asset["asset_media_id"]}]
        assert {s.path for s in g.ig.cdn} == {f"/slide-{asset['position']}.jpg"}
        assert (await g.command("approve", intent_id=first["id"])).status_code == 202
        await g.drain()
        assert g.state_of(first["id"]) == "posted"
        assert len(g.meta.publish_calls) == 1
        assert g.rows(
            "SELECT media_item_id::text AS item FROM post_locks WHERE kind='recent'"
        ) == [{"item": first["item"]}]
        assert g.rows(
            "SELECT count(*) AS n FROM audit_events WHERE entity_id=%s AND to_state='posted'",
            first["id"],
        ) == [{"n": 1}]
        await g.deliver()

        # A second slot can select another slide of this same post immediately.
        g.owner(
            "UPDATE ig_accounts SET next_slot_at=now()-interval '10 seconds' WHERE id=%s",
            landed.account,
        )
        assert (await g.tick())["slot_jobs"] == 1
        await g.drain()
        (second,) = g.rows(
            "SELECT id::text AS id,media_item_id::text AS item FROM post_intents WHERE ig_account_id=%s AND id<>%s",
            landed.account,
            first["id"],
        )
        assert second["item"] != first["item"] and second["item"] in identities.values()
        await g.sweep_prompts()
        assert g.state_of(second["id"]) == "awaiting_approval"
        # The selected binding exists, but its card is still pending delivery.
        # A complete reindex must prevent that queued card serving another file.
        dropped = next(
            key for key, item in identities.items() if item == second["item"]
        )
        post = past_posts.posts[0]
        smaller = replace(
            past_posts,
            posts=(
                replace(
                    post, children=tuple(c for c in post.children if c["id"] != dropped)
                ),
            ),
        )
        g.ig.add(smaller)
        # Force a fresh local test scan to observe that old parent again.
        g.owner(
            "UPDATE media_sources SET sync_checkpoint=NULL,next_sync_at=now() WHERE id=%s",
            landed.source,
        )
        await g.index(landed.source, smaller, whole=True)
        assert g.rows("SELECT state FROM media_items WHERE id=%s", second["item"]) == [
            {"state": "removed"}
        ]
        assert g.rows(
            "SELECT count(*) AS n FROM instagram_media_assets WHERE source_id=%s",
            landed.source,
        ) == [{"n": 9}]
        await g.sweep_prompts()
        assert g.state_of(second["id"]) == "review_required"
        assert g.rows(
            "SELECT last_error#>>'{error,reason}' AS reason FROM post_intents WHERE id=%s",
            second["id"],
        ) == [{"reason": "past_posts_content_unavailable"}]
        assert len(g.meta.publish_calls) == 1
        await g.deliver()
        # The synthetic owner's explicit give-up resolves the parked story;
        # deletion deliberately waits for this decision on review cards.
        resolved = await g.command(
            "resolve_review", intent_id=second["id"], resolution="cancel"
        )
        assert resolved.status_code == 200, resolved.text
        await g.deliver()

        monkeypatch.setattr(settings, "INSTAGRAM_APP_SECRET", SECRET, raising=False)
        response, _, _ = await receipts.request_deletion(
            g.scene, SECRET, oauth.app_scoped_of(REF_A)
        )
        assert response.status_code == 200
        code = response.json()["confirmation_code"]
        for _ in range(4):
            await g.sweep()
            await g.deliver()
            if receipts.receipt_of(g.world, code)["state"] == "completed":
                break
        assert receipts.receipt_of(g.world, code)["state"] == "completed"
        assert g.residue(landed.source)["assets"] == 0
        assert g.residue(landed.source)["tombstones"] == 10
        assert g.rows(
            "SELECT count(*) AS n FROM media_items WHERE source_id=%s AND NOT source_projection_redacted",
            landed.source,
        ) == [{"n": 0}]

        monkeypatch.setattr(settings, "INSTAGRAM_APP_SECRET", "sec", raising=False)
        state = oauth.state_of(await g.scene.start(landed.account, choice=None))
        back = await g.scene.callback(state, REF_A, "example_handle_a")
        outcome = (await g.scene.result(oauth.connection_of(back))).json()
        assert outcome["outcome"] == "reconnected_preserved"
        await g.index(landed.source, smaller, whole=True)
        assert g.identities(landed.source) == {
            key: item for key, item in identities.items() if key != dropped
        }
        assert g.rows(
            "SELECT state,source_projection_redacted FROM media_items WHERE id=%s",
            second["item"],
        ) == [{"state": "removed", "source_projection_redacted": True}]
        assert g.rows(
            "SELECT media_item_id::text AS item FROM post_locks WHERE kind='recent'"
        ) == [{"item": first["item"]}]
