"""Existing-account reconciliation, as the worker's login (#1644).

The world is production's in miniature: a converted workspace whose accounts
were connected before the archive was live (an `ig_login` credential that
decrypts under the ring, no mapping, no source), the discovery door minting the
job, and `archive_reconciliation.execute` running it as `svc_worker`. Meta's
`/me` is the only fake: it answers per token, records every request, insists on
being asked outside any transaction, and can act while a request is in flight
(a reconnect, a mix save, a second worker). What is asserted is what the run
LEFT in the tables, read as the migration actor, and which outcome each account
was logged with.

Each test gets a fresh lineage database, so the door sees only its own world.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import httpx
import psycopg2
import psycopg2.extras
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from src.config.settings import settings
from src.services.target import (
    archive_lifecycle,
    archive_reconciliation,
    category_mix,
    egress,
    ig_login_oauth,
    oauth_states,
    source_membership,
    versioned_mix,
)
from src.services.target.instagram_sources import ArchiveCapability
from src.services.target.jobs import JobFenced
from src.services.target.unit_of_work import in_transaction, unit_of_work
from tests.scripts import test_instagram_sources_gate as foundation
from tests.scripts import test_meta_receipt_doors_gate as receipts
from tests.scripts import test_versioned_mix_gate as mix_gate
from tests.scripts.conftest import (
    as_user,
    async_url,
    seed_workspace_chain,
    set_test_passwords,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]
db = receipts.db
owner, run, T0 = receipts.owner, receipts.run, receipts.T0

APP_ID = "4242"
APP = f"instagram:{APP_ID}"
RUN_DIGEST = "d" * 64
CLIENT = object()
PROFILE = ig_login_oauth.PROFILE_URL


def ref(n):
    """Account *n*'s own id (`provider_account_ref`)."""
    return f"17841400000{n:05d}"


def scoped(n):
    """The person's app-scoped id: a different value, never derived from `ref`."""
    return f"90000{n:05d}"


def token(n):
    return f"IGQVJ-synthetic-{n}"


class Meta:
    """Instagram's `/me`: what each token says. Every request is recorded and
    must come from outside any transaction; `hooks[token]` runs while that
    request is in flight, as another actor would."""

    def __init__(self):
        self.answers, self.hooks, self.calls = {}, {}, []

    def says(self, n, *, user_id=None):
        """The honest answer for account *n*'s token, or one naming *user_id*."""
        self.answers[token(n)] = (
            200,
            {
                "id": scoped(n),
                "user_id": user_id or ref(n),
                "username": "example_handle",
            },
        )

    async def request(self, client, method, url, *, policy=None, **kwargs):
        assert not in_transaction(), "a provider call inside a transaction"
        assert client is CLIENT
        params = kwargs["params"]
        self.calls.append(
            {"method": method, "url": url, "params": params, "policy": policy}
        )
        hook = self.hooks.get(params["access_token"])
        if hook is not None:
            await hook()
        answer = self.answers[params["access_token"]]
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        return httpx.Response(status, json=body)


@pytest.fixture
async def worker(world_db, admin_conn):
    set_test_passwords(admin_conn)
    engine = create_async_engine(async_url(as_user(world_db, "svc_worker")))
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def meta(monkeypatch):
    fake = Meta()
    monkeypatch.setattr(egress, "request", fake.request)
    monkeypatch.setattr(settings, "INSTAGRAM_APP_ID", APP_ID, raising=False)
    return fake


# --- the world --------------------------------------------------------------


def table(db, sql, params=()):
    conn = psycopg2.connect(db["owner"])
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SET LOCAL app.actor_kind = 'migration'")
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def uow(engine, ws):
    return unit_of_work(engine, ws, actor_kind="system").begin()


async def build(db, worker, meta, tmp_path, accounts=1):
    """A converted workspace (two Drive folders at a custom 70/30, revision 1)
    with *accounts* connected before the archive was live."""
    conn = psycopg2.connect(db["owner"])
    try:
        chain = seed_workspace_chain(conn, f"recon-{uuid.uuid4().hex[:8]}")
    finally:
        conn.close()
    ws = str(chain["ws"])
    found = [{"n": 0, "account": str(chain["iga"])}]
    for n in range(1, accounts):
        ((account,),) = owner(
            db,
            "INSERT INTO ig_accounts (workspace_id, provider_account_ref)"
            " VALUES (%s, %s) RETURNING id",
            (ws, ref(n)),
        )
        found.append({"n": n, "account": str(account)})
    for a in found:
        owner(
            db,
            "UPDATE ig_accounts SET provider_account_ref = %s WHERE id = %s",
            (ref(a["n"]), a["account"]),
        )
        ((credential,),) = owner(
            db,
            "INSERT INTO oauth_credentials (workspace_id, ig_account_id, provider,"
            " encrypted_payload) VALUES (%s, %s, 'ig_login', %s) RETURNING id",
            (ws, a["account"], oauth_states.ring().encrypt(token(a["n"]))),
        )
        a["credential"] = str(credential)
        meta.says(a["n"])
    async with uow(worker, ws) as conn:
        drives = await mix_gate.add(conn, chain, [".7", ".3"])
        await category_mix.convert_workspace(
            conn, workspace_id=ws, evidence=mix_gate.evidence(tmp_path)
        )
    return SimpleNamespace(ws=ws, drives=drives, accounts=found)


def lease(db, ws):
    """Lease the workspace's ready reconciliation job, as the loop's claim does."""
    ((job_id, lease_token, payload),) = owner(
        db,
        "UPDATE jobs SET state = 'leased', lease_token = gen_random_uuid(),"
        " locked_by = 'reconcile-gate', locked_until = now() + interval '5 minutes',"
        " attempts = attempts + 1 WHERE kind = 'reconcile_archive_accounts'"
        " AND state = 'ready' AND workspace_id = %s"
        " RETURNING id, lease_token, payload",
        (ws,),
    )
    return {
        "id": job_id,
        "workspace_id": ws,
        "lease_token": lease_token,
        "payload": payload,
    }


def mint(db, ws, digest=RUN_DIGEST):
    """The lifecycle sweep's reconciliation leg: the real door, as the worker."""
    minted = run(
        db["worker"],
        "SELECT fn_archive_reconcile_jobs(%s, %s, %s)",
        (10, "gate-run", digest),
    )
    assert minted == [(1,)]
    return lease(db, ws)


def relet(db, job):
    """The same job under a new lease: a restarted worker's claim."""
    ((lease_token,),) = owner(
        db,
        "UPDATE jobs SET lease_token = gen_random_uuid(), state = 'leased',"
        " locked_until = now() + interval '5 minutes' WHERE id = %s"
        " RETURNING lease_token",
        (job["id"],),
    )
    return {**job, "lease_token": lease_token}


async def execute(worker, caplog, job):
    deps = SimpleNamespace(
        engine=worker, archive_capability=ArchiveCapability(True), archive_client=CLIENT
    )
    with caplog.at_level(logging.INFO, logger=archive_reconciliation.logger.name):
        return await archive_reconciliation.execute(deps, job)


def outcomes(caplog):
    """[(account id, outcome)] in the order the runs logged them."""
    found = []
    for message in caplog.messages:
        m = re.fullmatch(
            r"reconcile_archive_accounts \S+: account (\S+) -> (\S+)", message
        )
        if m:
            found.append((m.group(1), m.group(2)))
    return found


# --- what the database says -------------------------------------------------


def mappings(db, ws):
    return table(
        db,
        "SELECT ig_account_id::text AS account, credential_id::text AS credential,"
        " authorization_id::text AS authorization, app_identity, subject_ref,"
        " account_ref, method, authorized_at, verified_at, is_current"
        " FROM meta_identity_mappings WHERE workspace_id = %s"
        " ORDER BY created_at, authorization_id",
        (ws,),
    )


def sources(db, ws):
    return table(
        db,
        "SELECT id::text AS id, origin_ig_account_id::text AS origin, state, config,"
        " current_authorization_generation_id::text AS generation, source_use_epoch"
        " FROM media_sources WHERE workspace_id = %s AND provider = 'instagram_archive'"
        " ORDER BY origin_ig_account_id",
        (ws,),
    )


def generations(db, ws):
    return table(
        db,
        "SELECT id::text AS id, source_id::text AS source, credential_id::text AS credential,"
        " authorization_id::text AS authorization, app_id, subject_id, authorized_at,"
        " lifecycle FROM instagram_archive_generations WHERE workspace_id = %s"
        " ORDER BY created_at, id",
        (ws,),
    )


def mix(db, ws):
    """(revision, mode, {source id: ratio}) of the saved mix."""
    (saved,) = table(
        db,
        "SELECT source_mix_revision AS revision, source_mix_mode AS mode"
        " FROM workspaces WHERE id = %s",
        (ws,),
    )
    ratios = table(
        db,
        "SELECT source_id::text AS source, ratio FROM category_post_case_mix"
        " WHERE workspace_id = %s AND effective_to IS NULL",
        (ws,),
    )
    return saved["revision"], saved["mode"], {r["source"]: r["ratio"] for r in ratios}


def job_row(db, job):
    (row,) = table(
        db,
        "SELECT state, attempts, lease_token, payload FROM jobs WHERE id = %s",
        (job["id"],),
    )
    return row


def other_jobs(db, ws):
    return table(
        db,
        "SELECT kind FROM jobs WHERE workspace_id = %s AND kind <> 'reconcile_archive_accounts'",
        (ws,),
    )


# --- the contract -----------------------------------------------------------


async def test_an_existing_grant_gets_a_mapping_and_an_off_source_and_no_feed_is_read(
    db, worker, meta, tmp_path, caplog
):
    w = await build(db, worker, meta, tmp_path)
    (a,) = w.accounts
    job = mint(db, w.ws)
    assert job["payload"] == {"v": 1, "run_id": "gate-run", "run_digest": RUN_DIGEST}

    before = datetime.now(timezone.utc)
    assert await execute(worker, caplog, job) == "done"
    after = datetime.now(timezone.utc)

    # The one identity read, and nothing else: no feed, no retry.
    assert [(c["method"], c["url"], c["params"]) for c in meta.calls] == [
        (
            "GET",
            PROFILE,
            {"fields": "id,user_id,username", "access_token": token(0)},
        )
    ]
    assert meta.calls[0]["policy"].max_attempts == 1
    assert outcomes(caplog) == [(a["account"], "source_created_off")]

    # The mapping: evidence of a read, not of an authorization.
    (mapping,) = mappings(db, w.ws)
    legacy = archive_reconciliation.legacy_authorization_id(w.ws, a["credential"])
    assert legacy == str(
        uuid.uuid5(
            archive_reconciliation.NS_ARCHIVE, f"{w.ws}:{a['credential']}:legacy"
        )
    )
    assert {k: v for k, v in mapping.items() if k != "verified_at"} == {
        "account": a["account"],
        "credential": a["credential"],
        "authorization": legacy,
        "app_identity": APP,
        "subject_ref": scoped(0),
        "account_ref": ref(0),
        "method": "reconciled_read",
        "authorized_at": None,
        "is_current": True,
    }
    assert before <= mapping["verified_at"] <= after

    # The source: Off, with its generation (bound to the mapping's authorization),
    # origin assignment and the current default policy.
    (source,) = sources(db, w.ws)
    (generation,) = generations(db, w.ws)
    assert (source["origin"], source["state"], source["config"]) == (
        a["account"],
        "active",
        {"v": 1},
    )
    assert source["generation"] == generation["id"]
    assert {k: v for k, v in generation.items() if k != "id"} == {
        "source": source["id"],
        "credential": a["credential"],
        "authorization": legacy,
        "app_id": APP_ID,
        "subject_id": ref(0),
        "authorized_at": mapping["verified_at"],
        "lifecycle": "active",
    }
    assert table(
        db,
        "SELECT d.state FROM media_source_destinations d WHERE d.source_id = %s"
        " AND d.ig_account_id = %s",
        (source["id"], a["account"]),
    ) == [{"state": "active"}]
    assert table(
        db,
        "SELECT schema_version, carousel_slides FROM instagram_source_policies WHERE source_id = %s",
        (source["id"],),
    ) == [{"schema_version": 2, "carousel_slides": "all"}]

    # Off, with the custom mix kept and the revision moved by exactly one.
    revision, mode, ratios = mix(db, w.ws)
    assert (revision, mode) == (2, "custom")
    assert ratios == {
        w.drives[0]: Decimal("0.7"),
        w.drives[1]: Decimal("0.3"),
        source["id"]: Decimal(0),
    }

    # Nothing queued, nothing observed.
    assert other_jobs(db, w.ws) == []
    assert table(
        db,
        "SELECT count(*) AS n FROM instagram_media_index WHERE workspace_id = %s",
        (w.ws,),
    ) == [{"n": 0}]


async def test_a_second_worker_racing_the_first_records_nothing_twice(
    db, worker, meta, tmp_path, caplog
):
    w = await build(db, worker, meta, tmp_path)
    (a,) = w.accounts
    job = mint(db, w.ws)

    async def race():
        meta.hooks.clear()
        assert await execute(worker, caplog, job) == "done"

    meta.hooks[token(0)] = race
    assert await execute(worker, caplog, job) == "done"

    # The racer finished first; the first run found its work already done.
    assert outcomes(caplog) == [
        (a["account"], "source_created_off"),
        (a["account"], "source_exists"),
    ]
    assert (
        len(mappings(db, w.ws))
        == len(sources(db, w.ws))
        == len(generations(db, w.ws))
        == 1
    )
    assert mix(db, w.ws)[0] == 2, "the revision moved once"

    # A rerun later has no candidate: no read, no row.
    assert await execute(worker, caplog, relet(db, job)) == "done"
    assert len(meta.calls) == 2
    assert (
        len(mappings(db, w.ws))
        == len(sources(db, w.ws))
        == len(generations(db, w.ws))
        == 1
    )
    assert mix(db, w.ws)[0] == 2


async def test_a_removed_source_is_skipped_but_its_account_is_still_mapped(
    db, worker, meta, tmp_path, caplog
):
    w = await build(db, worker, meta, tmp_path)
    (a,) = w.accounts
    async with uow(worker, w.ws) as conn:
        await source_membership.connect_archive(
            conn,
            workspace_id=w.ws,
            origin_ig_account_id=a["account"],
            authorization=foundation.authorization(),
            had_prior_grant=False,
            choice=source_membership.ArchiveChoice(False, 1),
            allocation_writer=versioned_mix.allocate_membership,
            capability=ArchiveCapability(True),
        )
    async with uow(worker, w.ws) as conn:
        await archive_lifecycle.remove_account_archive(
            conn, workspace_id=w.ws, ig_account_id=a["account"]
        )
    (removed,) = sources(db, w.ws)
    assert removed["config"]["removed"] is True
    held = (mix(db, w.ws), generations(db, w.ws))

    assert await execute(worker, caplog, mint(db, w.ws)) == "done"

    assert outcomes(caplog) == [(a["account"], "source_removed_skipped")]
    (mapping,) = mappings(db, w.ws)
    assert (mapping["method"], mapping["is_current"]) == ("reconciled_read", True)
    # The removed source is exactly as it was: not restored, not re-generated.
    assert sources(db, w.ws) == [removed]
    assert (mix(db, w.ws), generations(db, w.ws)) == held


async def test_an_identity_mismatch_or_an_unreadable_account_writes_nothing_and_never_blocks_a_sibling(
    db, worker, meta, tmp_path, caplog
):
    w = await build(db, worker, meta, tmp_path, accounts=5)
    ok, other, refused, anonymous, lapsed = w.accounts
    # A token that proves another account than the one it is stored on.
    meta.says(1, user_id=ref(99))
    # A refused read, and an answer that carries no app-scoped id.
    meta.answers[token(2)] = (400, {"error": {"message": "synthetic"}})
    meta.answers[token(3)] = (200, {"user_id": ref(3), "username": "example_handle"})
    # Past its expiry but still marked active: the identity is proven, the
    # archive source is not allowed.
    owner(
        db,
        "UPDATE oauth_credentials SET expires_at = now() - interval '1 day' WHERE id = %s",
        (lapsed["credential"],),
    )

    assert await execute(worker, caplog, mint(db, w.ws)) == "done"

    assert dict(outcomes(caplog)) == {
        ok["account"]: "source_created_off",
        other["account"]: "identity_mismatch",
        refused["account"]: "identity_unreadable",
        anonymous["account"]: "identity_unreadable",
        lapsed["account"]: "mapped",
    }
    assert {m["account"] for m in mappings(db, w.ws)} == {
        ok["account"],
        lapsed["account"],
    }
    assert [s["origin"] for s in sources(db, w.ws)] == [ok["account"]]
    assert len(generations(db, w.ws)) == 1
    assert mix(db, w.ws)[0] == 2
    # One request per account: the refused read was not asked again.
    assert sorted(c["params"]["access_token"] for c in meta.calls) == sorted(
        token(n) for n in range(5)
    )


CHANGES = [
    "token_swapped_during_the_read",
    "token_swapped_before_it_was_read",
    "credential_revoked_during_the_read",
    "account_needs_reauth_during_the_read",
    "account_became_manual_during_the_read",
]


@pytest.mark.parametrize("change", CHANGES)
async def test_a_credential_or_account_that_changed_since_it_was_read_refuses(
    change, db, worker, meta, tmp_path, caplog, monkeypatch
):
    w = await build(db, worker, meta, tmp_path)
    (a,) = w.accounts
    job = mint(db, w.ws)
    meta.answers[token(7)] = meta.answers[token(0)]

    def change_it():
        if change.startswith("token_swapped"):
            owner(
                db,
                "UPDATE oauth_credentials SET encrypted_payload = %s WHERE id = %s",
                (oauth_states.ring().encrypt(token(7)), a["credential"]),
            )
        elif change.startswith("credential_revoked"):
            owner(
                db,
                "UPDATE oauth_credentials SET state = 'revoked' WHERE id = %s",
                (a["credential"],),
            )
        elif change.startswith("account_needs_reauth"):
            owner(
                db,
                "UPDATE ig_accounts SET state = 'reauth_required' WHERE id = %s",
                (a["account"],),
            )
        else:
            owner(
                db,
                "UPDATE ig_accounts SET provider_account_ref = 'manual:placeholder'"
                " WHERE id = %s",
                (a["account"],),
            )

    if change == "token_swapped_before_it_was_read":
        # Between the capture (which carries the digest) and the token read: the
        # digest must be the older of the two, or the swap goes unnoticed.
        real, once = ig_login_oauth.load_credential, []

        async def swapped_first(session, **kwargs):
            if not once:
                once.append(True)
                change_it()
            return await real(session, **kwargs)

        monkeypatch.setattr(ig_login_oauth, "load_credential", swapped_first)
    else:

        async def meanwhile():
            change_it()

        meta.hooks[token(0)] = meanwhile
    assert await execute(worker, caplog, job) == "done"

    assert outcomes(caplog) == [(a["account"], "credential_changed")]
    assert mappings(db, w.ws) == [] and sources(db, w.ws) == []
    assert generations(db, w.ws) == []
    assert mix(db, w.ws)[0] == 1

    if change.startswith("token_swapped"):
        # The control: the same account, asked again about the credential that
        # is there now, is reconciled. The refusal was the swap, nothing else.
        meta.hooks.clear()
        assert await execute(worker, caplog, relet(db, job)) == "done"
        assert outcomes(caplog)[-1] == (a["account"], "source_created_off")
        assert [m["method"] for m in mappings(db, w.ws)] == ["reconciled_read"]


@pytest.mark.parametrize(
    "change", ["credential_revoked", "account_needs_reauth", "account_became_manual"]
)
async def test_a_credential_or_account_that_changed_before_the_load_is_never_read(
    change, db, worker, meta, tmp_path, caplog, monkeypatch
):
    """The candidate page said this was an active credential of an active
    account; by the time its turn comes it is not. Step 1 asks again, so nothing
    is decrypted and nothing is sent to Meta: the provider is never shown a token
    this deployment has stopped using, which the re-check at step 3 comes too
    late to prevent."""
    w = await build(db, worker, meta, tmp_path)
    (a,) = w.accounts
    job = mint(db, w.ws)
    real = archive_reconciliation._load

    async def changed_first(engine, ws, account, credential):
        if change == "credential_revoked":
            owner(
                db,
                "UPDATE oauth_credentials SET state = 'revoked' WHERE id = %s",
                (credential,),
            )
        elif change == "account_needs_reauth":
            owner(
                db,
                "UPDATE ig_accounts SET state = 'reauth_required' WHERE id = %s",
                (account,),
            )
        else:
            owner(
                db,
                "UPDATE ig_accounts SET provider_account_ref = 'manual:placeholder'"
                " WHERE id = %s",
                (account,),
            )
        return await real(engine, ws, account, credential)

    monkeypatch.setattr(archive_reconciliation, "_load", changed_first)
    assert await execute(worker, caplog, job) == "done"

    assert outcomes(caplog) == [(a["account"], "credential_changed")]
    assert meta.calls == [], "a token the deployment had stopped using reached Meta"
    assert mappings(db, w.ws) == [] and sources(db, w.ws) == []
    assert mix(db, w.ws)[0] == 1


async def test_an_unreadable_token_flips_through_the_lock_order_and_moves_the_archive(
    db, worker, meta, tmp_path, caplog
):
    """An account whose archive source has no mapping yet is still a candidate,
    and its token can be unreadable. `load_credential` fails closed, and the
    flip is the rejection wrapper's, bound to the capture the load read first:
    the credential and the account flip, the archive's epoch moves once and is
    audited as an expiry, and nothing is read from Meta or recorded."""
    w = await build(db, worker, meta, tmp_path)
    (a,) = w.accounts
    async with uow(worker, w.ws) as conn:
        await source_membership.connect_archive(
            conn,
            workspace_id=w.ws,
            origin_ig_account_id=a["account"],
            authorization=foundation.authorization(),
            had_prior_grant=False,
            choice=source_membership.ArchiveChoice(False, 1),
            allocation_writer=versioned_mix.allocate_membership,
            capability=ArchiveCapability(True),
        )
    (before,) = sources(db, w.ws)
    owner(
        db,
        "UPDATE oauth_credentials SET encrypted_payload = %s WHERE id = %s",
        ("gAAAAABnot-a-real-fernet-token", a["credential"]),
    )

    assert await execute(worker, caplog, mint(db, w.ws)) == "done"

    assert outcomes(caplog) == [(a["account"], "credential_changed")]
    assert meta.calls == [], "an unreadable token is never sent anywhere"
    assert table(
        db,
        "SELECT state FROM oauth_credentials WHERE id = %s",
        (a["credential"],),
    ) == [{"state": "expired"}]
    assert table(
        db, "SELECT state FROM ig_accounts WHERE id = %s", (a["account"],)
    ) == [{"state": "reauth_required"}]
    (after,) = sources(db, w.ws)
    assert after["source_use_epoch"] == before["source_use_epoch"] + 1
    assert [
        r["reason"]
        for r in table(
            db,
            "SELECT detail->>'reason' AS reason FROM audit_events"
            " WHERE entity_kind = 'instagram_source' AND entity_id = %s ORDER BY id",
            (before["id"],),
        )
    ][-1] == "credential_expired"
    assert mappings(db, w.ws) == []


async def test_reconciliation_interleaved_with_mix_save_preserves_custom(
    db, worker, meta, tmp_path, caplog
):
    w = await build(db, worker, meta, tmp_path)
    (a,) = w.accounts
    d0, d1 = w.drives

    async def save_custom():
        async with uow(worker, w.ws) as conn:
            saved = await category_mix.mutate(
                conn,
                workspace_id=w.ws,
                body={
                    "protocol_version": 2,
                    "action": "save_custom",
                    "expected_revision": 1,
                    "rows": [
                        {"source_id": d0, "ratio": "0.6"},
                        {"source_id": d1, "ratio": "0.4"},
                    ],
                },
                by_user_id=None,
            )
        assert (saved["revision"], saved["mode"]) == (2, "custom")

    meta.hooks[token(0)] = save_custom
    assert await execute(worker, caplog, mint(db, w.ws)) == "done"

    assert outcomes(caplog) == [(a["account"], "source_created_off")]
    (source,) = sources(db, w.ws)
    revision, mode, ratios = mix(db, w.ws)
    # The person's save stands; the source joined at zero; one revision later.
    assert (revision, mode) == (3, "custom")
    assert ratios == {d0: Decimal("0.6"), d1: Decimal("0.4"), source["id"]: Decimal(0)}


async def test_a_turn_handles_five_accounts_and_the_job_continues_from_its_cursor(
    db, worker, meta, tmp_path, caplog
):
    w = await build(db, worker, meta, tmp_path, accounts=7)
    by_id = sorted(w.accounts, key=lambda a: a["account"])
    first, rest = by_id[:5], by_id[5:]
    # The first account of the turn proves nothing: it stays a candidate, and
    # only the cursor keeps the second turn from asking about it again.
    stranger = first[0]
    meta.says(stranger["n"], user_id=ref(99))
    job = mint(db, w.ws)

    assert await execute(worker, caplog, job) == "continued"

    assert outcomes(caplog) == [(stranger["account"], "identity_mismatch")] + [
        (a["account"], "source_created_off") for a in first[1:]
    ]
    assert sorted(c["params"]["access_token"] for c in meta.calls) == sorted(
        token(a["n"]) for a in first
    )
    assert mix(db, w.ws)[0] == 1 + 4
    # The job is back, alive: attempt returned, lease cleared, cursor advanced.
    row = job_row(db, job)
    assert (row["state"], row["attempts"], row["lease_token"]) == ("ready", 0, None)
    assert row["payload"] == {**job["payload"], "cursor": first[-1]["account"]}

    assert await execute(worker, caplog, lease(db, w.ws)) == "done"

    assert outcomes(caplog)[5:] == [(a["account"], "source_created_off") for a in rest]
    assert len(meta.calls) == 7, "each account was read once, the refused one too"
    assert len(mappings(db, w.ws)) == len(sources(db, w.ws)) == 6
    assert mix(db, w.ws)[0] == 1 + 6


async def test_a_newer_mapping_is_never_demoted_by_the_older_evidence(
    db, worker, meta, tmp_path, caplog
):
    w = await build(db, worker, meta, tmp_path)
    (a,) = w.accounts
    authorization = str(uuid.uuid4())
    owner(
        db,
        "INSERT INTO meta_identity_mappings (workspace_id, ig_account_id, credential_id,"
        " authorization_id, app_identity, subject_ref, account_ref, method,"
        " authorized_at, verified_at, is_current)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, 'oauth_exchange', %s, %s, true)",
        (
            w.ws,
            a["account"],
            a["credential"],
            authorization,
            APP,
            scoped(0),
            ref(0),
            T0,
            T0,
        ),
    )
    seeded = mappings(db, w.ws)

    assert await execute(worker, caplog, mint(db, w.ws)) == "done"

    assert outcomes(caplog) == [(a["account"], "source_created_off")]
    assert mappings(db, w.ws) == seeded, (
        "no second row, and the OAuth one still current"
    )
    (generation,) = generations(db, w.ws)
    assert (generation["authorization"], generation["authorized_at"]) == (
        authorization,
        T0,
    )
    assert (generation["app_id"], generation["subject_id"]) == (APP_ID, ref(0))


async def test_a_run_that_lost_its_lease_writes_nothing(
    db, worker, meta, tmp_path, caplog
):
    w = await build(db, worker, meta, tmp_path)
    (a,) = w.accounts
    job = mint(db, w.ws)

    async def another_worker_claims_it():
        relet(db, job)

    meta.hooks[token(0)] = another_worker_claims_it
    with pytest.raises(JobFenced):
        await execute(worker, caplog, job)

    assert mappings(db, w.ws) == [] and sources(db, w.ws) == []
    assert mix(db, w.ws)[0] == 1

    # The control: the new owner's run, under the lease that is there now.
    meta.hooks.clear()
    assert await execute(worker, caplog, relet(db, job)) == "done"
    assert outcomes(caplog) == [(a["account"], "source_created_off")]


async def test_a_deployment_that_names_no_instagram_app_reads_and_writes_nothing(
    db, worker, meta, tmp_path, caplog, monkeypatch
):
    w = await build(db, worker, meta, tmp_path)
    (a,) = w.accounts
    monkeypatch.setattr(settings, "INSTAGRAM_APP_ID", "", raising=False)

    assert await execute(worker, caplog, mint(db, w.ws)) == "done"

    assert outcomes(caplog) == [(a["account"], "identity_unreadable")]
    assert meta.calls == []
    assert mappings(db, w.ws) == [] and sources(db, w.ws) == []
