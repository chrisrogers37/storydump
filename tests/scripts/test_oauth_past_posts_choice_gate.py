"""The OAuth history choice, from the preview to the applied result, AS THE
PRODUCTION ROLE (OAuth past-posts choice).

The OAuth choice and its applied result form a matrix of
entry points against account states. Every row is driven here through the REAL
app on the replayed target schema, connected as `svc_ingress` — the role the API
runs as; a gate run as the owner would prove the SQL, not the deployment:

    sign in → create a workspace → a converted mix (three Drive folders at
    70 / 30 / Off) → connect-preview → the start, which records the choice in
    the one-shot state → the callback → the applied result's read

The past-posts capability is injected on `app.state`, as the trusted composition
root (`past_posts_composition.build_api`) injects it where the past-posts source is armed;
by default none is injected, and the first test shows that connect leg offers
no choice and writes no past-posts source, generation or result.
Instagram is the only stub:
`ig_login_oauth.exchange_code` hands back the grant a scene names, so no
request leaves the process. Everything else is real — `oauth_states` and its
CHECKs, `connect_past_posts`, `versioned_mix`'s allocation, the audit triggers, the
policies on `oauth_connection_results`.

Outcomes are read from the tables, as the migration actor, and not from the
API's own answer alone: the result resource says what the callback RECORDED,
and the tables say what it DID. Each Off outcome also shows no historical read
was asked for: the admission authority refuses the source by name, and no job
was queued.
"""

from __future__ import annotations

import contextlib
import itertools
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from urllib.parse import parse_qs, urlsplit

import httpx
import psycopg2
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.api.app import create_app
from src.api.principal import COOKIE
from src.config.settings import settings
from src.services.target import (
    category_mix,
    connect_choice,
    ig_login_oauth,
    sessions,
    source_membership,
    source_policy,
    versioned_mix,
)
from src.services.target.instagram_sources import PastPostsCapability, PastPostsRefused
from src.services.target.source_read_admission import (
    SourceReadRefused,
    capture_source_read,
)
from src.services.target.unit_of_work import asyncpg_url, unit_of_work
from tests.scripts.conftest import (
    as_made_before_conversion,
    _scratch,
    as_user,
    execute,
    fetch_all,
    in_tenant,
    build_world,
    set_test_passwords,
)
from tests.scripts.test_versioned_mix_gate import evidence as conversion_evidence
from tests.src.api import conftest as api_conftest
from tests.src.api.conftest import API, FRONT, owned_workspace, sign_in

#: The configured sign-in world, registered here as a fixture by assignment.
google_configured = api_conftest.google_configured

pytestmark = [pytest.mark.integration, pytest.mark.slow]

LIVE = PastPostsCapability(enabled=True)
#: A Meta app id, as Meta's callbacks name an app: digits.
APP_ID = "4242"
#: Stands for "the exchange proved the id the grant says"; any other value
#: (`None` included) is what the identity read proved instead.
PROVEN = object()


def app_scoped_of(ref):
    """The person's app-scoped id the fake exchange proves for account *ref*: a
    different number from the account's own id, as Meta's are."""
    return f"99{ref}"


#: Instagram's ids for the accounts a scene connects, and the handles they carry.
REF_NEW = "17841400000000011"
REF_X = "17841400000000012"
REF_OLD = "17841400000000013"
REF_PIN = "17841400000000014"
REF_OTHER = "17841400000000015"
REF_ROW = "17841400000000016"

#: The Drive folders every scene's converted mix starts from: 70 / 30 / Off.
FOLDERS = (("Folder A", "0.7"), ("Folder B", "0.3"), ("Folder C", "0"))

#: A start that sends no body at all, as against `choice=None`,
#: which sends `{"past_posts_choice": null}`, the reconnect's.
NO_BODY = object()

_TAGS = itertools.count(1)


# --- the world ---------------------------------------------------------------


@pytest.fixture(scope="module")
def world(admin_conn, owner_actor):
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        stream = build_world(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        yield {"stream": stream, "ingress": as_user(db, "svc_ingress")}
    finally:
        gen.close()


@pytest.fixture(autouse=True)
def fresh_preauth_budget(world):
    """Sign-in and every callback spend the 30-a-minute pre-auth admission of
    one client address (`principal.preauth_guard`), and this module shares a
    database. A scene starts with the budget whole; the limit itself is
    `test_signup_gate`'s and `test_web_router_x2_gate`'s business."""
    execute(world["stream"], "DELETE FROM rate_counters WHERE scope = 'preauth_ip'")


@pytest.fixture
def instagram_configured(monkeypatch):
    monkeypatch.setattr(settings, "INSTAGRAM_APP_ID", APP_ID, raising=False)
    monkeypatch.setattr(settings, "INSTAGRAM_APP_SECRET", "sec", raising=False)


# --- readers: the tables, as the migration actor ------------------------------


def rows(world, query, *params):
    return fetch_all(world["stream"], query, params or None)


def one(world, query, *params):
    found = rows(world, query, *params)
    assert len(found) == 1, found
    return found[0]


def count(world, table, ws, where=""):
    return one(
        world,
        f"SELECT count(*) AS n FROM {table} WHERE workspace_id = %s {where}",
        ws,
    )["n"]


def mix_of(world, ws):
    saved = one(
        world,
        "SELECT source_mix_revision AS revision, source_mix_mode AS mode"
        "  FROM workspaces WHERE id = %s",
        ws,
    )
    current = rows(
        world,
        "SELECT source_id::text AS source_id, ratio FROM category_post_case_mix"
        " WHERE workspace_id = %s AND effective_to IS NULL",
        ws,
    )
    return {**saved, "ratios": {r["source_id"]: r["ratio"] for r in current}}


def landing_of(world, ws):
    """What a connect leaves in the workspace, as counts and facts — the
    snapshot a refused or replayed callback must leave exactly as it found it."""
    return {
        "accounts": rows(
            world,
            "SELECT id::text AS id, provider_account_ref, state FROM ig_accounts"
            " WHERE workspace_id = %s ORDER BY id",
            ws,
        ),
        "credentials": rows(
            world,
            "SELECT id::text AS id, ig_account_id::text AS account, state"
            "  FROM oauth_credentials"
            " WHERE workspace_id = %s AND provider = 'ig_login' ORDER BY id",
            ws,
        ),
        "sources": rows(
            world,
            "SELECT id::text AS id, state, source_use_epoch FROM media_sources"
            " WHERE workspace_id = %s AND provider = 'instagram_past_posts' ORDER BY id",
            ws,
        ),
        "generations": count(world, "instagram_past_posts_generations", ws),
        "results": count(world, "oauth_connection_results", ws),
        "mix": mix_of(world, ws),
    }


def jobs_of(world, ws):
    return count(world, "jobs", ws)


def state_row(world, state):
    return one(
        world,
        "SELECT purpose, reconnect_target::text AS target, past_posts_entry,"
        "       past_posts_use_history AS use_history,"
        "       past_posts_expected_mix_revision AS revision,"
        "       past_posts_preview_version AS version, consumed_at IS NOT NULL AS used"
        "  FROM oauth_states WHERE state = %s",
        state,
    )


def add_member(world, ws, user_id, role):
    """A seat in the workspace, as the migration actor (the suite's one
    spelling: `seed_workspace_chain`'s `app.actor_kind`)."""
    conn = psycopg2.connect(world["stream"])
    try:
        with conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            cur.execute(
                "INSERT INTO workspace_members (workspace_id, user_id, role)"
                " VALUES (%s, %s, %s)",
                (ws, user_id, role),
            )
        conn.commit()
    finally:
        conn.close()


# --- the web's requests, as plain functions ------------------------------------


def choice(use_history, revision, version=1):
    return {
        "use_history": use_history,
        "expected_mix_revision": revision,
        "preview_version": version,
    }


def token_of(headers):
    """The opaque session value a bearer header carries."""
    return headers["Authorization"].removeprefix("Bearer ")


def cookie_of(headers):
    """The returning browser: the session the bearer names, as the cookie a
    top-level navigation back from Instagram carries."""
    return {"Cookie": f"{COOKIE}={token_of(headers)}"}


def state_of(started):
    assert started.status_code == 200, started.text
    query = urlsplit(started.json()["authorization_url"]).query
    return parse_qs(query)["state"][0]


def connection_of(landed):
    """The applied result's id a landed callback redirects with, off the
    Settings screen's accounts tab and nothing else."""
    assert landed.status_code == 302, landed.text
    location = urlsplit(landed.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == (
        f"{FRONT}/dashboard/settings"
    )
    query = parse_qs(location.query)
    assert set(query) == {"tab", "connection"} and query["tab"] == ["accounts"]
    return query["connection"][0]


def error_page(reason):
    return f"{FRONT}/auth/error?reason={reason}&flow=instagram"


def assert_equal_split(ratios, on, off):
    """The equal reset the preview promised: the enabled sources share one
    unit to 48 places, and what was Off stays Off."""
    shares = [ratios[source] for source in on]
    with localcontext() as context:
        context.prec = 50
        assert sum(shares) == 1
        assert max(shares) - min(shares) < Decimal("1e-48")
        assert abs(shares[0] - Decimal(1) / len(shares)) < Decimal("1e-48")
    for source in off:
        assert ratios[source] == 0


class Provider:
    """Instagram's three provider calls as one fake exchange: whichever grant
    the scene names, and every code it was handed. Never a network call."""

    def __init__(self):
        self.grant = None
        self.codes = []

    def give(self, ref, handle, *, app_scoped=PROVEN):
        self.grant = ig_login_oauth.IgGrant(
            access_token=f"token-for-{ref}",
            expires_at=None,
            ig_user_id=ref,
            username=handle,
            app_scoped_id=app_scoped_of(ref) if app_scoped is PROVEN else app_scoped,
        )

    async def exchange_code(
        self, client, *, code, redirect_uri, client_id, client_secret
    ):
        assert (client_id, client_secret) == (APP_ID, "sec")
        assert redirect_uri == f"{API}/auth/instagram-login/callback"
        self.codes.append(code)
        return self.grant


@dataclass
class Landed:
    """One connect that landed: the state it was started with, the result's id
    its callback redirected with, and what the result resource says."""

    state: str
    connection: str
    result: dict

    @property
    def account(self):
        return self.result["account_id"]

    @property
    def source(self):
        return self.result["source_id"]


class Scene:
    """One workspace under the real app: its owner, three Drive folders behind a
    converted mix, and the requests a connect is made of. Ids are strings."""

    def __init__(self, *, world, client, app, engine, provider, monkeypatch, tag):
        self.world, self.client, self.app = world, client, app
        self.engine, self.provider, self.monkeypatch = engine, provider, monkeypatch
        self.tag = tag
        self.owner = self.user_id = self.ws = None
        self.drive = {}

    # -- building the world ---------------------------------------------------

    @classmethod
    @contextlib.asynccontextmanager
    async def open(
        cls, world, monkeypatch, tmp_path, *, capability, converted, folders=FOLDERS
    ):
        tag = f"oauth-choice-{next(_TAGS)}"
        engine = create_async_engine(asyncpg_url(world["ingress"]), poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                who = (await conn.execute(text("SELECT current_user"))).scalar()
                assert who == "svc_ingress", who
            app = create_app(engine=engine)
            app.state.email = None
            app.state.past_posts_capability = capability
            provider = Provider()
            monkeypatch.setattr(ig_login_oauth, "exchange_code", provider.exchange_code)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url=API) as client:
                scene = cls(
                    world=world,
                    client=client,
                    app=app,
                    engine=engine,
                    provider=provider,
                    monkeypatch=monkeypatch,
                    tag=tag,
                )
                scene.owner, scene.user_id, scene.ws = await owned_workspace(
                    client, monkeypatch, tag=tag
                )
                # A workspace made today starts converted; these scenes are of
                # one made before, as every existing workspace is until its
                # conversion job runs.
                as_made_before_conversion(world["stream"], scene.ws)
                if converted:
                    await scene.convert(tmp_path, folders)
                yield scene
        finally:
            await engine.dispose()

    async def convert(self, tmp_path, folders):
        """The Drive *folders* (three, at 70 / 30 / Off, unless a scene says
        otherwise), then the conversion job's conversion to the versioned mix
        at revision 1."""
        async with unit_of_work(
            self.engine, self.ws, actor_kind="system"
        ).begin() as conn:
            for letter, (name, ratio) in zip("abc", folders):
                source = (
                    await conn.execute(
                        text(
                            "INSERT INTO media_sources (workspace_id, provider, config)"
                            " VALUES (:ws, 'gdrive', jsonb_build_object("
                            "  'folder_ref', CAST(:ref AS text),"
                            "  'folder_name', CAST(:name AS text))) RETURNING id"
                        ),
                        {"ws": self.ws, "ref": f"folder-{letter}", "name": name},
                    )
                ).scalar_one()
                await conn.execute(
                    text(
                        "INSERT INTO category_post_case_mix"
                        " (workspace_id, source_id, category, ratio)"
                        " VALUES (:ws, :sid, 'synthetic', :ratio)"
                    ),
                    {"ws": self.ws, "sid": str(source), "ratio": Decimal(ratio)},
                )
                self.drive[letter] = str(source)
            await category_mix.convert_workspace(
                conn, workspace_id=self.ws, evidence=conversion_evidence()
            )
        assert self.mix() == {
            "revision": 1,
            "mode": "custom" if folders else "equal",
            "ratios": {
                self.drive[letter]: Decimal(ratio)
                for letter, (_, ratio) in zip("abc", folders)
            },
        }

    async def member(self, suffix, role):
        """Another signed-in person with a seat here: their bearer."""
        headers = await sign_in(
            self.client,
            self.monkeypatch,
            sub=f"sub-{self.tag}-{suffix}",
            email=f"{self.tag}-{suffix}@example.test",
        )
        me = await self.client.get("/api/v1/me", headers=headers)
        add_member(self.world, self.ws, me.json()["user"]["id"], role)
        return headers

    async def another_session(self):
        """The owner, signed in again: a second session of the same person."""
        return await sign_in(
            self.client,
            self.monkeypatch,
            sub=f"sub-{self.tag}",
            email=f"{self.tag}@example.test",
        )

    # -- the web's requests ---------------------------------------------------

    def url(self, path):
        return f"/api/v1/workspaces/{self.ws}/{path}"

    async def preview(self, account_id=None, *, who=None):
        params = {} if account_id is None else {"account_id": account_id}
        return await self.client.get(
            self.url("accounts/connect-preview"),
            params=params,
            headers=who or self.owner,
        )

    async def start(self, account_id=None, *, choice=NO_BODY, who=None):
        path = (
            "accounts/connect"
            if account_id is None
            else f"accounts/{account_id}/connect"
        )
        kwargs = {} if choice is NO_BODY else {"json": {"past_posts_choice": choice}}
        return await self.client.post(
            self.url(path), headers=who or self.owner, **kwargs
        )

    async def callback(self, state, ref, handle, *, returning=None, app_scoped=PROVEN):
        """Instagram's redirect back, the exchange faked: the browser carries the
        session cookie of *returning* (the owner's, unless named)."""
        self.provider.give(ref, handle, app_scoped=app_scoped)
        return await self.client.get(
            f"/auth/instagram-login/callback?state={state}&code=c0de",
            headers=cookie_of(returning or self.owner),
            follow_redirects=False,
        )

    async def result(self, connection, *, who=None, ws=None):
        return await self.client.get(
            f"/api/v1/workspaces/{ws or self.ws}/connection-results/{connection}",
            headers=who or self.owner,
        )

    async def add(self, ref, handle, *, history):
        """The header Add, the web's way: the preview, the start carrying the
        choice it showed (none when *history* is None), the callback, the
        result's read."""
        shown = (await self.preview()).json()
        started = await self.start(
            choice=NO_BODY
            if history is None
            else choice(history, shown["expected_mix_revision"])
        )
        state = state_of(started)
        landed = await self.callback(state, ref, handle)
        connection = connection_of(landed)
        read = await self.result(connection)
        assert read.status_code == 200, read.text
        return Landed(state, connection, read.json())

    # -- facts ----------------------------------------------------------------

    def mix(self):
        return mix_of(self.world, self.ws)

    def landing(self):
        return landing_of(self.world, self.ws)

    def jobs(self):
        return jobs_of(self.world, self.ws)

    async def in_tenant(self, fn):
        """*fn(session)* in the workspace, as `svc_ingress`, as its owner."""
        return await in_tenant(self.world["ingress"], self.ws, self.user_id, fn)

    async def assert_no_history_read(self, source, jobs_before, *, next_sync_at=None):
        """An Off source asks for no historical read: the admission authority
        refuses it by name, nothing was queued for it, and its read clock is
        what it was. A source that was never On was never armed (None); one that
        was On keeps the due time that made it readable, which an Off, a removal
        and a restore leave alone (*next_sync_at*)."""
        with pytest.raises(SourceReadRefused) as refused:
            await self.in_tenant(
                lambda s: capture_source_read(
                    s, workspace_id=self.ws, source_id=source, capability=LIVE
                )
            )
        assert refused.value.reason == "source_off"
        assert self.jobs() == jobs_before
        assert (
            one(
                self.world,
                "SELECT next_sync_at FROM media_sources WHERE id = %s",
                source,
            )["next_sync_at"]
            == next_sync_at
        )


@pytest.fixture
async def scene(world, google_configured, instagram_configured, monkeypatch, tmp_path):
    """A workspace with the past-posts source available: the capability injected and the
    mix converted."""
    async with Scene.open(
        world, monkeypatch, tmp_path, capability=LIVE, converted=True
    ) as opened:
        yield opened


# --- the default connect: no capability, or no converted mix ----------------------


@pytest.mark.parametrize(
    "capability,converted",
    [(None, True), (LIVE, False)],
    ids=["no-capability", "capability-but-unconverted-mix"],
)
async def test_where_the_past_posts_is_not_live_the_connect_makes_no_past_posts_write(
    world,
    google_configured,
    instagram_configured,
    monkeypatch,
    tmp_path,
    capability,
    converted,
):
    """By default no capability is injected and no converted mix is served; the
    connect leg there is a preview that offers no choice, a start that refuses
    one by name and records nothing, and a callback that lands the destination
    and its credential and returns `?connected=instagram` with no past-posts source, no
    generation and no result."""
    async with Scene.open(
        world, monkeypatch, tmp_path, capability=capability, converted=converted
    ) as scene:
        before = scene.mix()
        unavailable = {
            "past_posts_available": False,
            "entry": "add",
            "account_id": None,
            "handle": None,
            "history": {"editable": False, "default": False, "saved_enabled": None},
            "expected_mix_revision": None,
            "preview_version": 1,
            "reset": None,
        }
        assert (await scene.preview()).json() == unavailable

        refused = await scene.start(choice=choice(True, 1))
        assert refused.status_code == 503, refused.text
        assert refused.json()["reason"] == "past_posts_not_activated"
        assert count(world, "oauth_states", scene.ws) == 0

        state = state_of(await scene.start())
        assert state_row(world, state) == {
            "purpose": "connect",
            "target": None,
            "past_posts_entry": None,
            "use_history": None,
            "revision": None,
            "version": None,
            "used": False,
        }
        landed = await scene.callback(state, REF_NEW, "example_handle_new")
        assert landed.status_code == 302, landed.text
        assert landed.headers["location"] == (
            f"{FRONT}/dashboard/settings?connected=instagram"
        )

        after = scene.landing()
        assert [a["provider_account_ref"] for a in after["accounts"]] == [REF_NEW]
        assert [c["state"] for c in after["credentials"]] == ["active"]
        assert after["sources"] == []
        assert (after["generations"], after["results"]) == (0, 0)
        assert after["mix"] == before
        assert state_row(world, state)["used"] is True
        assert len(scene.provider.codes) == 1


# --- the one versioned default ----------------------------------------------------


async def test_a_changed_history_default_moves_the_switch_and_nothing_saved(
    scene, monkeypatch
):
    """The versioned creation default is where the switch starts for a NEW
    connection, read at each preview. A person's own choice made against it is
    what lands, and what lands is saved: the account's reconnect names it and
    carries no default, so a later default changes nothing that was chosen."""
    real = source_policy.policy_defaults
    monkeypatch.setattr(
        source_policy,
        "policy_defaults",
        lambda: {**real(), "new_connection_use_history": False},
    )

    shown = (await scene.preview()).json()
    assert shown["history"] == {
        "editable": True,
        "default": False,
        "saved_enabled": None,
    }

    landed = await scene.add(REF_NEW, "example_handle_new", history=True)
    assert landed.result["outcome"] == "connected_history_on"
    assert landed.result["history_enabled"] is True

    kept = (await scene.preview(landed.account)).json()
    assert kept["history"] == {
        "editable": False,
        "default": None,
        "saved_enabled": True,
    }


# --- header Add → a previously unknown account ------------------------------------


async def test_header_add_of_an_unknown_account_with_history_on_lands_on(scene):
    world, ws = scene.world, scene.ws
    a, b, c = (scene.drive[k] for k in "abc")

    shown = (await scene.preview()).json()
    reset = shown.pop("reset")
    assert shown == {
        "past_posts_available": True,
        "entry": "add",
        "account_id": None,
        "handle": None,
        "history": {"editable": True, "default": True, "saved_enabled": None},
        "expected_mix_revision": 1,
        "preview_version": 1,
    }
    assert (reset["applies"], reset["mode_after"]) == (True, "equal")
    promised = {r["source_id"]: r for r in reset["rows_after"]}
    assert set(promised) == {None, a, b, c}
    assert promised[None]["provider"] == "instagram_past_posts"
    assert {k: (promised[k]["provider"], promised[k]["name"]) for k in (a, b, c)} == {
        a: ("gdrive", "Folder A"),
        b: ("gdrive", "Folder B"),
        c: ("gdrive", "Folder C"),
    }

    state = state_of(await scene.start(choice=choice(True, 1)))
    assert state_row(world, state) == {
        "purpose": "connect",
        "target": None,
        "past_posts_entry": "add",
        "use_history": True,
        "revision": 1,
        "version": 1,
        "used": False,
    }

    started = datetime.now(timezone.utc)
    landed = await scene.callback(state, REF_NEW, "example_handle_new")
    finished = datetime.now(timezone.utc)
    connection = connection_of(landed)

    # The destination, its credential and the past-posts source landed together.
    (account,) = rows(
        world,
        "SELECT id::text AS id, provider_account_ref, handle, state"
        "  FROM ig_accounts WHERE workspace_id = %s",
        ws,
    )
    assert (account["provider_account_ref"], account["handle"]) == (
        REF_NEW,
        "example_handle_new",
    )
    assert account["state"] == "active"
    credential = one(
        world,
        "SELECT id::text AS id, state FROM oauth_credentials"
        " WHERE workspace_id = %s AND ig_account_id = %s AND provider = 'ig_login'",
        ws,
        account["id"],
    )
    assert credential["state"] == "active"
    source = one(
        world,
        "SELECT id::text AS id, state FROM media_sources"
        " WHERE workspace_id = %s AND provider = 'instagram_past_posts'"
        "   AND origin_ig_account_id = %s",
        ws,
        account["id"],
    )
    assert source["state"] == "active"

    # The authorization the callback built: a fresh id, the configured app, the
    # grant's own account, and the callback's time.
    generation = one(
        world,
        "SELECT credential_id::text AS credential, authorization_id::text AS auth,"
        "       app_id, subject_id, authorized_at, lifecycle"
        "  FROM instagram_past_posts_generations WHERE workspace_id = %s",
        ws,
    )
    assert generation["credential"] == credential["id"]
    assert (generation["app_id"], generation["subject_id"]) == (APP_ID, REF_NEW)
    assert generation["lifecycle"] == "active"
    assert uuid.UUID(generation["auth"]).version == 4
    assert started <= generation["authorized_at"] <= finished

    # Lands On, and the mix reset equal: the enabled folders and the new source
    # share one unit, the Off folder stays Off, exactly as the preview promised.
    after = scene.mix()
    assert (after["revision"], after["mode"]) == (2, "equal")
    assert_equal_split(after["ratios"], on=[a, b, source["id"]], off=[c])
    # The preview and the applied mix agree on every share, to the 48th place
    # and not the last digit: the unit's rounding remainder goes to whichever
    # equal share sorts first, and the new source has no id yet at preview time.
    applied = {
        None: after["ratios"][source["id"]],
        **{k: after["ratios"][k] for k in (a, b, c)},
    }
    with localcontext() as context:
        context.prec = 50
        for key, share in applied.items():
            assert abs(share - Decimal(promised[key]["target_share"])) < Decimal(
                "1e-48"
            )

    # The result row: bound to the person and the returning session, fifteen
    # minutes, and the redirect carries its id.
    session = one(
        world,
        "SELECT id::text AS id FROM session_tokens WHERE token_hash = %s",
        sessions.token_hash(token_of(scene.owner)),
    )
    result = one(
        world,
        "SELECT id::text AS id, workspace_id::text AS ws, user_id::text AS uid,"
        "       session_id::text AS sid, ig_account_id::text AS account,"
        "       source_id::text AS source, outcome, history_enabled, mix_revision,"
        "       mix_reset_applied, next_action, created_at, expires_at"
        "  FROM oauth_connection_results WHERE workspace_id = %s",
        ws,
    )
    assert result["id"] == connection
    assert (result["ws"], result["uid"], result["sid"]) == (
        ws,
        scene.user_id,
        session["id"],
    )
    assert (result["account"], result["source"]) == (account["id"], source["id"])
    assert (
        result["outcome"],
        result["history_enabled"],
        result["mix_revision"],
        result["mix_reset_applied"],
        result["next_action"],
    ) == ("connected_history_on", True, 2, True, None)
    assert result["expires_at"] - result["created_at"] == timedelta(minutes=15)

    # The resource says what the tables hold, beside the mix's current revision.
    read = await scene.result(connection)
    assert read.status_code == 200, read.text
    body = read.json()
    assert datetime.fromisoformat(body.pop("created_at")) == result["created_at"]
    assert body == {
        "account_id": account["id"],
        "source_id": source["id"],
        "outcome": "connected_history_on",
        "history_enabled": True,
        "mix_revision": 2,
        "mix_reset_applied": True,
        "next_action": None,
        "current_mix_revision": 2,
    }

    # On is a source the admission authority admits: the control for the Off
    # outcomes below, which it refuses by name.
    admitted = await scene.in_tenant(
        lambda s: capture_source_read(
            s, workspace_id=ws, source_id=source["id"], capability=LIVE
        )
    )
    assert (admitted.source_id, admitted.provider) == (
        source["id"],
        "instagram_past_posts",
    )


async def test_header_add_of_an_unknown_account_with_history_off_lands_off(scene):
    world, ws = scene.world, scene.ws
    a, b, c = (scene.drive[k] for k in "abc")
    custom = scene.mix()["ratios"]
    jobs = scene.jobs()

    shown = (await scene.preview()).json()
    state = state_of(
        await scene.start(choice=choice(False, shown["expected_mix_revision"]))
    )
    assert state_row(world, state)["use_history"] is False
    landed = await scene.callback(state, REF_NEW, "example_handle_new")
    connection = connection_of(landed)

    read = (await scene.result(connection)).json()
    assert (
        read["outcome"],
        read["history_enabled"],
        read["mix_revision"],
        read["mix_reset_applied"],
        read["next_action"],
        read["current_mix_revision"],
    ) == ("connected_history_off", False, 2, False, None, 2)

    # Adding the Off row is a source-set change: the revision moves, and no
    # ratio does. The mode stays what the workspace had.
    after = scene.mix()
    assert (after["revision"], after["mode"]) == (2, "custom")
    assert after["ratios"] == {**custom, read["source_id"]: Decimal(0)}
    assert {k: after["ratios"][k] for k in (a, b, c)} == {
        a: Decimal("0.7"),
        b: Decimal("0.3"),
        c: Decimal(0),
    }
    (account,) = rows(
        world, "SELECT id::text AS id FROM ig_accounts WHERE workspace_id = %s", ws
    )
    assert read["account_id"] == account["id"]
    assert count(world, "oauth_credentials", ws, "AND state = 'active'") == 1
    await scene.assert_no_history_read(read["source_id"], jobs)


async def test_the_first_source_of_a_workspace_with_no_drive_is_the_whole_mix(
    world, google_configured, instagram_configured, monkeypatch, tmp_path
):
    """One enabled Instagram source needs no Drive setup: the preview offers it
    the whole mix, and an On landing makes it exactly that."""
    async with Scene.open(
        world, monkeypatch, tmp_path, capability=LIVE, converted=True, folders=()
    ) as scene:
        shown = (await scene.preview()).json()
        assert shown["expected_mix_revision"] == 1
        assert shown["reset"] == {
            "applies": True,
            "mode_after": "equal",
            "rows_after": [
                {
                    "source_id": None,
                    "provider": "instagram_past_posts",
                    "name": None,
                    "target_share": "1",
                }
            ],
        }
        landed = await scene.add(REF_NEW, "example_handle_new", history=True)
        assert landed.result["outcome"] == "connected_history_on"
        assert landed.result["mix_reset_applied"] is True
        assert scene.mix() == {
            "revision": 2,
            "mode": "equal",
            "ratios": {landed.source: Decimal(1)},
        }


# --- row first-connect of a handle-only account ------------------------------------


@pytest.mark.parametrize("history", [True, False], ids=["on", "off"])
async def test_first_connect_of_a_handle_only_account_applies_the_history_choice(
    scene, history
):
    world, ws = scene.world, scene.ws
    a, b, c = (scene.drive[k] for k in "abc")
    custom = scene.mix()["ratios"]
    typed = await scene.client.post(
        scene.url("accounts"),
        json={"handle": "example_handle_row"},
        headers=scene.owner,
    )
    assert typed.status_code == 201, typed.text
    account = typed.json()["account_id"]
    assert one(
        world,
        "SELECT provider_account_ref FROM ig_accounts WHERE id = %s",
        account,
    ) == {"provider_account_ref": "manual:example_handle_row"}
    jobs = scene.jobs()

    shown = (await scene.preview(account)).json()
    assert (shown["entry"], shown["account_id"], shown["handle"]) == (
        "first_connect",
        account,
        "example_handle_row",
    )
    assert shown["history"] == {
        "editable": True,
        "default": True,
        "saved_enabled": None,
    }
    named = [r for r in shown["reset"]["rows_after"] if r["source_id"] is None]
    assert [r["name"] for r in named] == ["example_handle_row"]

    state = state_of(
        await scene.start(
            account, choice=choice(history, shown["expected_mix_revision"])
        )
    )
    assert state_row(world, state) == {
        "purpose": "connect",
        "target": account,
        "past_posts_entry": "first_connect",
        "use_history": history,
        "revision": 1,
        "version": 1,
        "used": False,
    }
    connection = connection_of(
        await scene.callback(state, REF_ROW, "example_handle_row")
    )

    # It lands ON the row it pinned, and flips its provisional reference.
    assert rows(
        world,
        "SELECT id::text AS id, provider_account_ref FROM ig_accounts"
        " WHERE workspace_id = %s",
        ws,
    ) == [{"id": account, "provider_account_ref": REF_ROW}]
    read = (await scene.result(connection)).json()
    assert read["account_id"] == account
    after = scene.mix()
    if history:
        assert (read["outcome"], read["history_enabled"]) == (
            "connected_history_on",
            True,
        )
        assert (read["mix_revision"], read["mix_reset_applied"]) == (2, True)
        assert (after["revision"], after["mode"]) == (2, "equal")
        assert_equal_split(after["ratios"], on=[a, b, read["source_id"]], off=[c])
    else:
        assert (read["outcome"], read["history_enabled"]) == (
            "connected_history_off",
            False,
        )
        assert (read["mix_revision"], read["mix_reset_applied"]) == (2, False)
        assert (after["revision"], after["mode"]) == (2, "custom")
        assert after["ratios"] == {**custom, read["source_id"]: Decimal(0)}
        await scene.assert_no_history_read(read["source_id"], jobs)


async def test_a_first_connect_must_come_back_as_the_account_it_pinned(scene):
    world = scene.world
    typed = await scene.client.post(
        scene.url("accounts"),
        json={"handle": "example_handle_pin"},
        headers=scene.owner,
    )
    account = typed.json()["account_id"]
    before = scene.landing()

    state = state_of(await scene.start(account, choice=choice(True, 1)))
    wrong = await scene.callback(state, REF_OTHER, "example_handle_other")
    assert wrong.status_code == 302, wrong.text
    assert wrong.headers["location"] == error_page("wrong_account")
    # Nothing of it landed: no credential, source, generation or result, and
    # the mix and the provisional reference are as they were.
    assert scene.landing() == before
    assert one(
        world, "SELECT provider_account_ref FROM ig_accounts WHERE id = %s", account
    ) == {"provider_account_ref": "manual:example_handle_pin"}
    assert state_row(world, state)["used"] is True

    # The same flow, coming back as the right account, lands: the refusal above
    # was the identity's and nothing else's.
    state = state_of(await scene.start(account, choice=choice(True, 1)))
    landed = await scene.callback(state, REF_PIN, "example_handle_pin")
    assert (await scene.result(connection_of(landed))).json()["account_id"] == account
    assert scene.mix()["revision"] == 2


# --- header Add and row reconnect on an account that already has a saved choice ----


@pytest.mark.parametrize("saved", [True, False], ids=["existing-on", "existing-off"])
async def test_header_add_landing_on_an_existing_account_keeps_what_it_saved(
    scene, saved
):
    world, ws = scene.world, scene.ws
    first = await scene.add(REF_X, "example_handle_x", history=saved)
    assert first.result["history_enabled"] is saved
    before = scene.landing()
    credential = before["credentials"][0]["id"]
    jobs = scene.jobs()

    # The Add start names no account, so it still offers a choice — and this one
    # picks the opposite of what the account saved.
    shown = (await scene.preview()).json()
    assert (shown["entry"], shown["history"]["editable"]) == ("add", True)
    state = state_of(
        await scene.start(choice=choice(not saved, shown["expected_mix_revision"]))
    )
    assert state_row(world, state)["use_history"] is (not saved)
    connection = connection_of(await scene.callback(state, REF_X, "example_handle_x"))

    read = (await scene.result(connection)).json()
    assert (
        read["outcome"],
        read["history_enabled"],
        read["mix_reset_applied"],
        read["next_action"],
    ) == ("reconnected_preserved", saved, False, None)
    assert (read["account_id"], read["source_id"]) == (first.account, first.source)
    assert (
        read["mix_revision"]
        == read["current_mix_revision"]
        == before["mix"]["revision"]
    )

    # The saved choice, the mix and the revision are untouched; the account is
    # not duplicated, and its credential was replaced in place.
    after = scene.landing()
    assert after["mix"] == before["mix"]
    assert after["accounts"] == before["accounts"]
    assert [c["id"] for c in after["credentials"]] == [credential]
    assert after["results"] == 2
    # Two authorizations, two generations: the first superseded, a fresh id each.
    generations = rows(
        world,
        "SELECT authorization_id::text AS auth, lifecycle"
        "  FROM instagram_past_posts_generations WHERE workspace_id = %s ORDER BY created_at",
        ws,
    )
    assert sorted(g["lifecycle"] for g in generations) == ["active", "superseded"]
    assert len({g["auth"] for g in generations}) == 2
    if not saved:
        await scene.assert_no_history_read(first.source, jobs)


@pytest.mark.parametrize("saved", [True, False], ids=["existing-on", "existing-off"])
async def test_row_reconnect_keeps_the_saved_choice_and_resets_nothing(scene, saved):
    world, ws = scene.world, scene.ws
    first = await scene.add(REF_X, "example_handle_x", history=saved)
    before = scene.landing()
    jobs = scene.jobs()

    shown = (await scene.preview(first.account)).json()
    assert shown["entry"] == "reconnect"
    assert shown["history"] == {
        "editable": False,
        "default": None,
        "saved_enabled": saved,
    }
    assert shown["reset"] is None

    # A reconnect takes no choice: refused by name, and no state was issued.
    refused = await scene.start(
        first.account, choice=choice(not saved, shown["expected_mix_revision"])
    )
    assert refused.status_code == 400, refused.text
    assert refused.json()["reason"] == "invalid_past_posts_choice"
    assert count(world, "oauth_states", ws) == 1

    state = state_of(await scene.start(first.account, choice=None))
    assert state_row(world, state) == {
        "purpose": "reconnect",
        "target": first.account,
        "past_posts_entry": "reconnect",
        "use_history": None,
        "revision": None,
        "version": None,
        "used": False,
    }
    connection = connection_of(await scene.callback(state, REF_X, "example_handle_x"))

    read = (await scene.result(connection)).json()
    assert (
        read["outcome"],
        read["history_enabled"],
        read["mix_reset_applied"],
        read["next_action"],
    ) == ("reconnected_preserved", saved, False, None)
    assert read["mix_revision"] == before["mix"]["revision"]
    after = scene.landing()
    assert after["mix"] == before["mix"]
    assert after["accounts"] == before["accounts"]
    assert [c["id"] for c in after["credentials"]] == [
        c["id"] for c in before["credentials"]
    ]
    if not saved:
        await scene.assert_no_history_read(first.source, jobs)


# --- a grant from before the past-posts source, and a source that was removed ----------------


@pytest.mark.parametrize(
    "entry", ["row-reconnect", "header-add-choosing-on"], ids=lambda e: e
)
async def test_a_pre_rollout_grant_without_a_past_posts_row_lands_off(scene, entry):
    world, ws = scene.world, scene.ws

    # The grant is from before the past-posts source: connected with no capability, which
    # is the default connect — the credential and destination, nothing else.
    scene.app.state.past_posts_capability = None
    state = state_of(await scene.start())
    landed = await scene.callback(state, REF_OLD, "example_handle_old")
    assert landed.headers["location"] == (
        f"{FRONT}/dashboard/settings?connected=instagram"
    )
    (account,) = rows(
        world, "SELECT id::text AS id FROM ig_accounts WHERE workspace_id = %s", ws
    )
    assert count(world, "oauth_credentials", ws, "AND provider = 'ig_login'") == 1
    assert (
        count(world, "media_sources", ws, "AND provider = 'instagram_past_posts'") == 0
    )
    assert count(world, "oauth_connection_results", ws) == 0

    # The past-posts source arrives. The account has a grant and no past-posts row.
    scene.app.state.past_posts_capability = LIVE
    custom = scene.mix()
    jobs = scene.jobs()
    if entry == "row-reconnect":
        shown = (await scene.preview(account["id"])).json()
        assert shown["entry"] == "reconnect"
        # Past posts stay Off until enabled in the Posting mix.
        assert shown["history"] == {
            "editable": False,
            "default": None,
            "saved_enabled": False,
        }
        state = state_of(await scene.start(account["id"], choice=None))
    else:
        shown = (await scene.preview()).json()
        state = state_of(
            await scene.start(choice=choice(True, shown["expected_mix_revision"]))
        )
        assert state_row(world, state)["use_history"] is True
    connection = connection_of(
        await scene.callback(state, REF_OLD, "example_handle_old")
    )

    # Off, whatever the start recorded: a grant that predates the past-posts source is an
    # existing connection, and an existing connection never enables by landing.
    read = (await scene.result(connection)).json()
    assert read["account_id"] == account["id"]
    assert (
        read["outcome"],
        read["history_enabled"],
        read["mix_reset_applied"],
        read["next_action"],
        read["mix_revision"],
    ) == ("connected_history_off", False, False, None, 2)
    after = scene.mix()
    assert (after["revision"], after["mode"]) == (2, "custom")
    assert after["ratios"] == {**custom["ratios"], read["source_id"]: Decimal(0)}
    await scene.assert_no_history_read(read["source_id"], jobs)


async def test_a_removed_source_restored_by_oauth_stays_off(scene):
    world, ws = scene.world, scene.ws
    a, b, c = (scene.drive[k] for k in "abc")
    first = await scene.add(REF_X, "example_handle_x", history=True)
    assert scene.mix()["revision"] == 2
    # Connecting with History On armed its first read: the due time is set.
    armed = one(
        world, "SELECT next_sync_at FROM media_sources WHERE id = %s", first.source
    )["next_sync_at"]
    assert armed is not None

    # The past-posts source is removed: the lifecycle verb, called here as
    # the system, with the real allocation. It forces the source Off.
    async with unit_of_work(scene.engine, ws, actor_kind="system").begin() as conn:
        retired = await source_membership.retire_past_posts(
            conn,
            workspace_id=ws,
            source_id=first.source,
            expected_mix_revision=2,
            allocation_writer=versioned_mix.allocate_membership,
            capability=LIVE,
        )
    assert retired.mix_revision == 3
    assert one(
        world,
        "SELECT s.state, COALESCE((s.config->>'removed')::boolean, false) AS removed,"
        "       d.state AS destination"
        "  FROM media_sources s JOIN media_source_destinations d"
        "    ON d.workspace_id = s.workspace_id AND d.source_id = s.id"
        " WHERE s.id = %s",
        first.source,
    ) == {"state": "paused", "removed": True, "destination": "retired"}
    removed = scene.mix()
    assert (removed["revision"], removed["mode"]) == (3, "equal")
    assert_equal_split(removed["ratios"], on=[a, b], off=[c, first.source])

    # Reconnecting restores it, and what the person is told first is that it
    # stays Off.
    shown = (await scene.preview(first.account)).json()
    assert shown["entry"] == "reconnect"
    assert shown["history"]["saved_enabled"] is False and shown["reset"] is None
    jobs = scene.jobs()
    state = state_of(await scene.start(first.account, choice=None))
    connection = connection_of(await scene.callback(state, REF_X, "example_handle_x"))

    read = (await scene.result(connection)).json()
    assert read["source_id"] == first.source
    assert (
        read["outcome"],
        read["history_enabled"],
        read["mix_reset_applied"],
        read["next_action"],
        read["mix_revision"],
    ) == ("reconnected_preserved", False, False, None, 4)
    assert one(
        world,
        "SELECT s.state, COALESCE((s.config->>'removed')::boolean, false) AS removed,"
        "       d.state AS destination"
        "  FROM media_sources s JOIN media_source_destinations d"
        "    ON d.workspace_id = s.workspace_id AND d.source_id = s.id"
        " WHERE s.id = %s",
        first.source,
    ) == {"state": "active", "removed": False, "destination": "active"}
    restored = scene.mix()
    assert (restored["revision"], restored["mode"]) == (4, "equal")
    assert restored["ratios"] == removed["ratios"]
    # Off again, the removal and the restore left the old due time as it was.
    await scene.assert_no_history_read(first.source, jobs, next_sync_at=armed)


# --- a custom save lands between the start and the callback ------------------------


async def test_a_custom_save_between_start_and_callback_lands_off_with_a_conflict(
    scene,
):
    world, ws = scene.world, scene.ws
    a, b, c = (scene.drive[k] for k in "abc")
    admin = await scene.member("admin", "admin")

    shown = (await scene.preview()).json()
    assert shown["expected_mix_revision"] == 1
    state = state_of(await scene.start(choice=choice(True, 1)))

    # While Instagram's page is open, another admin saves a custom mix.
    saved = await scene.client.put(
        scene.url("category-mix"),
        json={
            "protocol_version": 2,
            "action": "save_custom",
            "expected_revision": 1,
            "rows": [
                {"source_id": a, "ratio": "0.6"},
                {"source_id": b, "ratio": "0.4"},
                {"source_id": c, "ratio": "0"},
            ],
        },
        headers=admin,
    )
    assert saved.status_code == 200, saved.text
    custom = scene.mix()
    assert custom == {
        "revision": 2,
        "mode": "custom",
        "ratios": {a: Decimal("0.6"), b: Decimal("0.4"), c: Decimal(0)},
    }
    jobs = scene.jobs()

    connection = connection_of(
        await scene.callback(state, REF_NEW, "example_handle_new")
    )

    # The connection succeeded, the history did not: the source landed Off, the
    # result names the conflict, and the revision it was previewed against is
    # not the one it landed on.
    read = (await scene.result(connection)).json()
    created_at = read.pop("created_at")
    assert datetime.fromisoformat(created_at).tzinfo is not None
    assert read == {
        "account_id": read["account_id"],
        "source_id": read["source_id"],
        "outcome": "connected_history_off_mix_conflict",
        "history_enabled": False,
        "mix_revision": 3,
        "mix_reset_applied": False,
        "next_action": "review_mix",
        "current_mix_revision": 3,
    }
    (account,) = rows(
        world, "SELECT id::text AS id FROM ig_accounts WHERE workspace_id = %s", ws
    )
    assert account["id"] == read["account_id"]
    assert count(world, "oauth_credentials", ws, "AND state = 'active'") == 1
    assert (
        count(world, "media_sources", ws, "AND provider = 'instagram_past_posts'") == 1
    )

    # The other admin's custom mix is untouched: same mode, same ratios, and the
    # new Off row is the revision's only change.
    after = scene.mix()
    assert (after["revision"], after["mode"]) == (3, "custom")
    assert {k: v for k, v in after["ratios"].items() if k != read["source_id"]} == (
        custom["ratios"]
    )
    assert after["ratios"][read["source_id"]] == 0
    await scene.assert_no_history_read(read["source_id"], jobs)

    # A later change moves the mix on: the result keeps what the callback did
    # and reports the current revision beside it.
    reset = await scene.client.put(
        scene.url("category-mix"),
        json={"protocol_version": 2, "action": "reset_equal", "expected_revision": 3},
        headers=admin,
    )
    assert reset.status_code == 200, reset.text
    later = (await scene.result(connection)).json()
    assert (later["mix_revision"], later["current_mix_revision"]) == (3, 4)
    assert later["outcome"] == "connected_history_off_mix_conflict"
    assert later["next_action"] == "review_mix"


async def test_a_stale_preview_is_a_mix_conflict_before_any_redirect(scene):
    """The same race one step earlier: the custom save lands before the person
    presses Connect. An On start against the revision it was shown is refused
    with the current one and issues no state; an Off start is not stale, since
    an Off add resets nothing."""
    world, ws = scene.world, scene.ws
    a, b, c = (scene.drive[k] for k in "abc")
    admin = await scene.member("admin", "admin")
    shown = (await scene.preview()).json()
    assert shown["expected_mix_revision"] == 1

    saved = await scene.client.put(
        scene.url("category-mix"),
        json={
            "protocol_version": 2,
            "action": "save_custom",
            "expected_revision": 1,
            "rows": [
                {"source_id": a, "ratio": "0.6"},
                {"source_id": b, "ratio": "0.4"},
                {"source_id": c, "ratio": "0"},
            ],
        },
        headers=admin,
    )
    assert saved.status_code == 200, saved.text

    stale = await scene.start(choice=choice(True, 1))
    assert stale.status_code == 409, stale.text
    assert stale.json() == {"reason": "mix_conflict", "current_revision": 2}
    assert count(world, "oauth_states", ws) == 0

    # Off against the same old revision starts: the state records what was shown.
    off = state_of(await scene.start(choice=choice(False, 1)))
    assert state_row(world, off)["revision"] == 1
    # And a fresh preview's On start is the control: it is issued.
    fresh = (await scene.preview()).json()
    assert fresh["expected_mix_revision"] == 2
    on = state_of(await scene.start(choice=choice(True, 2)))
    assert state_row(world, on)["use_history"] is True


# --- replay, and who may read the result -------------------------------------------


async def test_a_replayed_callback_is_refused_whole(scene):
    first = await scene.add(REF_X, "example_handle_x", history=True)
    before = scene.landing()
    exchanges = len(scene.provider.codes)
    jobs = scene.jobs()

    replayed = await scene.callback(first.state, REF_X, "example_handle_x")
    assert replayed.status_code == 302, replayed.text
    assert replayed.headers["location"] == error_page("state_refused")
    # No second exchange, no duplicate account, credential, source or
    # generation, no second result and no second reset.
    assert len(scene.provider.codes) == exchanges == 1
    assert scene.landing() == before
    assert before["results"] == 1 and len(before["sources"]) == 1
    assert scene.jobs() == jobs

    # Refreshing the authorized result view is harmless.
    again = await scene.result(first.connection)
    assert again.status_code == 200 and again.json() == first.result


async def test_the_result_is_read_by_the_person_and_session_it_is_bound_to(scene):
    world, ws = scene.world, scene.ws
    first = await scene.add(REF_X, "example_handle_x", history=True)
    house_404 = {"detail": "not found"}

    # The person who started it, in the session that returned: readable.
    assert (await scene.result(first.connection)).status_code == 200

    # Another person with a seat in the workspace: the house 404. The seat is
    # real, so the 404 is the result's binding and not the member floor's.
    teammate = await scene.member("teammate", "admin")
    seat = await scene.client.get(scene.url("category-mix"), headers=teammate)
    assert seat.status_code == 200, seat.text
    other = await scene.result(first.connection, who=teammate)
    assert (other.status_code, other.json()) == (404, house_404)

    # Another session of the same person: 404 too.
    elsewhere = await scene.another_session()
    assert cookie_of(elsewhere) != cookie_of(scene.owner)
    other = await scene.result(first.connection, who=elsewhere)
    assert (other.status_code, other.json()) == (404, house_404)

    # The same person and session under another workspace of theirs: 404.
    made = await scene.client.post(
        "/api/v1/workspaces",
        json={"name": "Second", "tz": "America/New_York"},
        headers={**scene.owner, "Idempotency-Key": f"second-{scene.tag}"},
    )
    assert made.status_code == 201, made.text
    second = made.json()["workspace_id"]
    own = await scene.client.get(f"/api/v1/workspaces/{second}", headers=scene.owner)
    assert own.status_code == 200, own.text
    other = await scene.result(first.connection, ws=second)
    assert (other.status_code, other.json()) == (404, house_404)

    # An id nobody was given.
    missing = await scene.result(str(uuid.uuid4()))
    assert (missing.status_code, missing.json()) == (404, house_404)

    # Expiry is the whole of the next refusal: move it into the past and the
    # bound person and session read the house 404; move it back and they read it.
    execute(
        world["stream"],
        "UPDATE oauth_connection_results SET expires_at = now() - interval '1 second'"
        " WHERE id = %s",
        (first.connection,),
    )
    expired = await scene.result(first.connection)
    assert (expired.status_code, expired.json()) == (404, house_404)
    execute(
        world["stream"],
        "UPDATE oauth_connection_results SET expires_at = now() + interval '5 minutes'"
        " WHERE id = %s",
        (first.connection,),
    )
    assert (await scene.result(first.connection)).json() == first.result
    assert count(world, "oauth_connection_results", ws) == 1


async def test_the_result_is_bound_to_the_session_that_returned(scene):
    """The state pins the PERSON who started the flow, and the returning
    browser may carry another session of theirs: the result is that one's."""
    elsewhere = await scene.another_session()
    state = state_of(await scene.start(choice=choice(True, 1)))
    landed = await scene.callback(
        state, REF_NEW, "example_handle_new", returning=elsewhere
    )
    connection = connection_of(landed)

    returned = await scene.result(connection, who=elsewhere)
    assert returned.status_code == 200, returned.text
    assert returned.json()["outcome"] == "connected_history_on"
    started = await scene.result(connection)
    assert (started.status_code, started.json()) == (404, {"detail": "not found"})


# --- atomicity ---------------------------------------------------------------------


async def test_a_refused_history_setup_rolls_the_whole_landing_back(scene):
    """`connect_past_posts` landing its writes and THEN refusing — the worst case
    for atomicity — leaves nothing of the callback: no destination, credential,
    source, generation, result or mix change. The person lands on the error
    page, and the control beside it lands."""
    world = scene.world
    before = scene.landing()
    real = connect_choice.connect_past_posts

    async def refused_after_landing(executor, **kwargs):
        await real(executor, **kwargs)
        raise PastPostsRefused("past_posts_allocation_contract_failed")

    state = state_of(await scene.start(choice=choice(True, 1)))
    with scene.monkeypatch.context() as patch:
        patch.setattr(connect_choice, "connect_past_posts", refused_after_landing)
        failed = await scene.callback(state, REF_NEW, "example_handle_new")
    assert failed.status_code == 302, failed.text
    assert failed.headers["location"] == error_page("history_setup_failed")
    assert scene.landing() == before
    assert state_row(world, state)["used"] is True
    assert len(scene.provider.codes) == 1

    # The control: the same flow without the refusal lands the lot.
    landed = await scene.add(REF_NEW, "example_handle_new", history=True)
    assert landed.result["outcome"] == "connected_history_on"
    after = scene.landing()
    assert [a["provider_account_ref"] for a in after["accounts"]] == [REF_NEW]
    assert (len(after["sources"]), after["generations"], after["results"]) == (1, 1, 1)
