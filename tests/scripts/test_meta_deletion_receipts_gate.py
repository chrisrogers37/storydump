"""The data-deletion route and its status page, end to end as the production role
(Meta deletion receipt functions).

The OAuth gate's world: the real app on the replayed target schema, connected as
`svc_ingress`. The mapping a deletion request finds is the one the REAL callback
recorded (`scene.add`), so the two halves of the identity work are tied here and
not each proven against a hand-seeded row. Meta's request is a `signed_request`
built the way Meta builds it; nothing leaves the process.

What is asserted is what the tables hold, read as the migration actor:

* a request for a mapped subject is a durable receipt behind a random
  capability, of which only the digest is stored; its scope is frozen;
* an exact retry is the same receipt behind a new capability, and a request
  that differs is a new one; a key rotation never splits an episode and a secret
  rotation never changes which app signed;
* the legacy Facebook secret is held for review, never aliased onto an
  Instagram mapping;
* the status page says `in_progress` or `completed` and nothing finer, and
  says it again through the same capability once the app is rebuilt over the
  same database and key ring (a restart), where a wrong capability reads nothing;
* no raw payload, secret, capability or (outside the mapping itself) subject is
  stored anywhere, or logged.

Without `PAST_POSTS_HMAC_KEYS` the route answers with the logged 16-hex receipt
and stores nothing.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import logging
import time
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.api.app import create_app
from src.config.settings import settings
from src.services.target import past_posts_activation, meta_callbacks
from src.services.target.instagram_catalog import TombstoneKeys
from src.services.target.unit_of_work import asyncpg_url
from tests.scripts import test_oauth_past_posts_choice_gate as oauth_gate
from tests.scripts.conftest import rows_holding
from tests.scripts.test_oauth_past_posts_choice_gate import (
    API,
    app_scoped_of,
    one,
    rows,
)

#: The OAuth gate's world, registered here as fixtures by assignment.
world = oauth_gate.world
fresh_preauth_budget = oauth_gate.fresh_preauth_budget
google_configured = oauth_gate.google_configured
instagram_configured = oauth_gate.instagram_configured
scene = oauth_gate.scene

pytestmark = [pytest.mark.integration, pytest.mark.slow]

APP = "instagram:4242"
#: A secret nothing else in the database could contain by chance.
SECRET = "fixture-ig-" + "b" * 32
LEGACY_SECRET = "fixture-fb-" + "c" * 32
KEYS_V1 = TombstoneKeys(current_version=1, keys={1: bytes(range(32))})
KEYS_V2 = TombstoneKeys(
    current_version=2, keys={1: bytes(range(32)), 2: bytes(range(32, 64))}
)
STATUS = "/webhooks/meta/data-deletion/status"
CODE_SHAPE = meta_callbacks.CAPABILITY_CODE

#: One account per test that maps a person: the doors read across every workspace
#: of the shared database, so a subject another test mapped would answer here too.
REF_RECEIPT = "17841400000000101"
REF_STORED = "17841400000000102"
REF_LOGGED = "17841400000000103"
REF_RETRY = "17841400000000104"
REF_RECONNECT = "17841400000000105"
REF_LEGACY = "17841400000000106"
REF_ROTATION = "17841400000000107"
REF_KEYS = "17841400000000108"
REF_UNMAPPED = "17841400000000109"
REF_KEYLESS = "17841400000000110"
REF_EQUAL = "17841400000000111"
REF_RESTART = "17841400000000113"
#: Never connected by any test: a subject nobody mapped.
REF_RESTART_UNMAPPED = "17841400000000112"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def signed_request(secret, user_id, *, issued_at=None, segment=None):
    """A `signed_request` the way Meta builds one, and its signed segment. A
    *segment* given is signed as it is (Meta re-sending the same bytes)."""
    if segment is None:
        payload = {"algorithm": "HMAC-SHA256", "user_id": user_id}
        if issued_at is not None:
            payload["issued_at"] = issued_at
        segment = _b64url(json.dumps(payload).encode("utf-8"))
    sig = hmac.new(secret.encode("utf-8"), segment.encode("ascii"), hashlib.sha256)
    return f"{_b64url(sig.digest())}.{segment}", segment


def digest(code):
    return hashlib.sha256(code.encode("ascii")).hexdigest()


async def request_deletion(scene, secret, user_id, **kw):
    sent, segment = signed_request(secret, user_id, **kw)
    answered = await scene.client.post(
        "/webhooks/meta/data-deletion", data={"signed_request": sent}
    )
    return answered, segment, sent


async def status_of(scene, code):
    return await scene.client.get(STATUS, params={"code": code})


def receipt_of(world, code):
    """The receipt a capability names, as the migration actor."""
    return one(
        world,
        "SELECT r.id::text AS id, r.state, r.review_reason, r.app_identity,"
        "       r.request_fingerprint AS fingerprint,"
        "       r.fingerprint_key_version AS version, r.issued_at"
        "  FROM meta_deletion_receipt_capabilities c"
        "  JOIN meta_deletion_receipts r ON r.id = c.receipt_id"
        " WHERE c.capability_hash = %s",
        digest(code),
    )


def scopes_of(world, receipt):
    return rows(
        world,
        "SELECT workspace_id::text AS ws, ig_account_id::text AS account,"
        "       credential_id::text AS credential,"
        "       authorization_id::text AS authorization, disposition, state"
        "  FROM meta_deletion_receipt_scopes WHERE receipt_id = %s"
        " ORDER BY disposition, authorization_id",
        receipt,
    )


def frozen_of(world, receipt):
    return {
        r["generation"]
        for r in rows(
            world,
            "SELECT generation_id::text AS generation"
            "  FROM meta_deletion_receipt_generations WHERE receipt_id = %s",
            receipt,
        )
    }


def generations_of(world, ws):
    return rows(
        world,
        "SELECT id::text AS id, authorization_id::text AS authorization"
        "  FROM instagram_past_posts_generations WHERE workspace_id = %s"
        " ORDER BY created_at, id",
        ws,
    )


def receipts_count(world):
    return one(world, "SELECT count(*) AS n FROM meta_deletion_receipts")["n"]


def ring_of(keys):
    """*keys* as a deployment's environment carries a ring: `"1:<b64url>,2:..."`."""
    return ",".join(
        f"{version}:{_b64url(key)}" for version, key in sorted(keys.keys.items())
    )


@contextlib.asynccontextmanager
async def restarted(scene, keys):
    """The deployment after a restart: an app and a pool built afresh over the
    same database, the key ring read from the environment as production reads
    it. Nothing the first app held in memory comes with it."""
    engine = create_async_engine(
        asyncpg_url(scene.world["ingress"]), poolclass=NullPool
    )
    try:
        app = create_app(
            engine=engine, env={past_posts_activation.KEYS_VAR: ring_of(keys)}
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=API
        ) as client:
            yield SimpleNamespace(client=client, app=app)
    finally:
        await engine.dispose()


@pytest.fixture
def armed(monkeypatch):
    """Meta's two candidate secrets, distinctive, and the keys a deployment with
    the past-posts source's key ring has; the legacy secret is the Facebook app's."""

    def arm(scene, keys=KEYS_V2):
        monkeypatch.setattr(settings, "INSTAGRAM_APP_SECRET", SECRET, raising=False)
        monkeypatch.setattr(
            settings, "FACEBOOK_APP_SECRET", LEGACY_SECRET, raising=False
        )
        scene.app.state.past_posts_keys = keys

    return arm


async def mapped(scene, ref, armed, keys=KEYS_V2):
    """The real callback connects *ref* with its history on, and the deployment
    is then armed with the key ring (the callback needs the OAuth secret first)."""
    landed = await scene.add(ref, "example_handle_receipt", history=True)
    armed(scene, keys)
    return landed


async def test_a_mapped_subject_gets_a_durable_receipt_read_back_through_its_capability(
    scene, armed
):
    world, ws = scene.world, scene.ws
    landed = await mapped(scene, REF_RECEIPT, armed)
    subject = app_scoped_of(REF_RECEIPT)

    answered, segment, sent = await request_deletion(scene, SECRET, subject)
    assert answered.status_code == 200, answered.text
    code = answered.json()["confirmation_code"]
    assert CODE_SHAPE.fullmatch(code)
    assert answered.json() == {
        "url": f"{API}{STATUS}?code={code}",
        "confirmation_code": code,
    }

    receipt = receipt_of(world, code)
    expected = meta_callbacks.request_fingerprints(KEYS_V2, APP, segment)[0]
    assert (receipt["state"], receipt["review_reason"]) == ("pending", None)
    assert (receipt["app_identity"], receipt["version"], receipt["issued_at"]) == (
        APP,
        2,
        None,
    )
    assert (receipt["version"], receipt["fingerprint"]) == expected

    # The scope is the mapping the callback recorded, frozen on its generation.
    (generation,) = generations_of(world, ws)
    credential = one(
        world,
        "SELECT credential_id::text AS id FROM meta_identity_mappings"
        " WHERE workspace_id = %s",
        ws,
    )["id"]
    assert scopes_of(world, receipt["id"]) == [
        {
            "ws": ws,
            "account": landed.account,
            "credential": credential,
            "authorization": generation["authorization"],
            "disposition": "in_scope",
            "state": "pending",
        }
    ]
    assert frozen_of(world, receipt["id"]) == {generation["id"]}

    # A capability: only its digest is stored, and the page it opens is no-store.
    assert rows(
        world,
        "SELECT capability_hash FROM meta_deletion_receipt_capabilities"
        " WHERE receipt_id = %s",
        receipt["id"],
    ) == [{"capability_hash": digest(code)}]
    page = await status_of(scene, code)
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert (page.json()["code"], page.json()["status"]) == (code, "in_progress")


async def test_nothing_of_the_request_is_stored_anywhere_but_the_mapping_itself(
    scene, armed
):
    world = scene.world
    await mapped(scene, REF_STORED, armed)
    subject = app_scoped_of(REF_STORED)
    answered, segment, sent = await request_deletion(scene, SECRET, subject)
    code = answered.json()["confirmation_code"]

    # The capability, the signed request and its segment, and the secrets are
    # found in no table at all: only digests and fingerprints are.
    for needle in (code, sent, segment, SECRET, LEGACY_SECRET):
        assert rows_holding(world["stream"], needle) == {}, needle
    # The person's id is held by the mapping and by nothing the receipt wrote.
    holders = rows_holding(world["stream"], subject)
    assert set(holders) == {"public.meta_identity_mappings"}


async def test_nothing_of_the_request_is_logged(scene, armed):
    from src.utils.logger import logger as app_logger

    lines: list[str] = []

    class _Grab(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    handler = _Grab(level=logging.DEBUG)
    previous = app_logger.level
    app_logger.setLevel(logging.DEBUG)
    app_logger.addHandler(handler)
    try:
        await mapped(scene, REF_LOGGED, armed)
        subject = app_scoped_of(REF_LOGGED)
        lines.clear()
        answered, segment, sent = await request_deletion(scene, SECRET, subject)
        await status_of(scene, answered.json()["confirmation_code"])
    finally:
        app_logger.removeHandler(handler)
        app_logger.setLevel(previous)
    joined = "\n".join(lines)
    receipt = receipt_of(scene.world, answered.json()["confirmation_code"])
    assert receipt["id"] in joined, "the receipt id is what an operator needs"
    for needle in (
        subject,
        segment,
        sent,
        SECRET,
        LEGACY_SECRET,
        answered.json()["confirmation_code"],
        receipt["fingerprint"],
    ):
        assert needle not in joined


async def test_an_exact_retry_is_one_receipt_behind_a_new_capability(scene, armed):
    world = scene.world
    await mapped(scene, REF_RETRY, armed)
    subject = app_scoped_of(REF_RETRY)

    first, segment, sent = await request_deletion(scene, SECRET, subject)
    again, _, _ = await request_deletion(scene, SECRET, subject, segment=segment)
    codes = [first.json()["confirmation_code"], again.json()["confirmation_code"]]
    assert codes[0] != codes[1] and all(CODE_SHAPE.fullmatch(c) for c in codes)

    one_receipt = {receipt_of(world, c)["id"] for c in codes}
    assert len(one_receipt) == 1
    (rid,) = one_receipt
    assert len(scopes_of(world, rid)) == 1
    assert (
        one(
            world,
            "SELECT count(*) AS n FROM meta_deletion_receipts"
            " WHERE request_fingerprint = %s",
            receipt_of(world, codes[0])["fingerprint"],
        )["n"]
        == 1
    )
    assert (
        one(
            world,
            "SELECT count(*) AS n FROM meta_deletion_receipt_capabilities"
            " WHERE receipt_id = %s",
            rid,
        )["n"]
        == 2
    )
    for code in codes:
        assert (await status_of(scene, code)).json()["status"] == "in_progress"


async def test_a_request_after_a_reconnect_is_a_new_receipt_and_the_first_scope_stays_frozen(
    scene, armed
):
    world, ws = scene.world, scene.ws
    first = await mapped(scene, REF_RECONNECT, armed)
    subject = app_scoped_of(REF_RECONNECT)
    before, _, _ = await request_deletion(scene, SECRET, subject)
    r1 = receipt_of(world, before.json()["confirmation_code"])
    (old,) = generations_of(world, ws)
    assert r1["state"] == "pending" and frozen_of(world, r1["id"]) == {old["id"]}

    # The person reconnects: a new authorization, a new generation, a new current
    # mapping (the OAuth secret is the callback's, then the route's again).
    scene.monkeypatch.setattr(settings, "INSTAGRAM_APP_SECRET", "sec", raising=False)
    state = oauth_gate.state_of(await scene.start(first.account, choice=None))
    assert (
        await scene.callback(state, REF_RECONNECT, "example_handle_receipt")
    ).status_code == 302
    armed(scene)
    new = [g for g in generations_of(world, ws) if g["id"] != old["id"]]
    assert len(new) == 1

    after, _, _ = await request_deletion(
        scene, SECRET, subject, issued_at=int(time.time())
    )
    r2 = receipt_of(world, after.json()["confirmation_code"])
    assert r2["id"] != r1["id"], "a request that differs is a new episode"
    # The first receipt's scope never grew: the later grant is not in it.
    assert frozen_of(world, r1["id"]) == {old["id"]}
    assert [s["authorization"] for s in scopes_of(world, r1["id"])] == [
        old["authorization"]
    ]
    # The new one froze what existed when it arrived, the later grant included.
    assert new[0]["id"] in frozen_of(world, r2["id"])
    assert {s["authorization"] for s in scopes_of(world, r2["id"])} >= {
        new[0]["authorization"]
    }


async def test_the_legacy_facebook_secret_is_held_for_review_never_aliased_onto_an_instagram_mapping(
    scene, armed
):
    world = scene.world
    await mapped(scene, REF_LEGACY, armed)
    subject = app_scoped_of(REF_LEGACY)

    legacy, _, _ = await request_deletion(scene, LEGACY_SECRET, subject)
    assert legacy.status_code == 200, legacy.text
    code = legacy.json()["confirmation_code"]
    receipt = receipt_of(world, code)
    assert (receipt["app_identity"], receipt["state"], receipt["review_reason"]) == (
        "facebook:legacy",
        "needs_review",
        "app_unrecognized",
    )
    assert scopes_of(world, receipt["id"]) == [], "the same raw subject, another app"
    # A receipt held for review is still just in progress to the person.
    assert (await status_of(scene, code)).json()["status"] == "in_progress"

    # The control: the Instagram app's request for the same subject finds it.
    instagram, _, _ = await request_deletion(scene, SECRET, subject)
    found = receipt_of(world, instagram.json()["confirmation_code"])
    assert (found["app_identity"], found["state"]) == (APP, "pending")
    assert len(scopes_of(world, found["id"])) == 1


async def test_equal_app_secrets_cannot_name_the_app_so_the_request_is_held_for_review(
    scene, armed, monkeypatch
):
    """Both settings holding one secret leave the signature unable to say which
    app signed. It must not be read as the preferred (Instagram) app, whose
    mapping the same raw subject would then reach: the request is held for
    review with no scope, and a warning says why, once."""
    from src.utils.logger import logger as app_logger

    world = scene.world
    await mapped(scene, REF_EQUAL, armed)
    subject = app_scoped_of(REF_EQUAL)
    warnings: list[str] = []

    class _Grab(logging.Handler):
        def emit(self, record):
            warnings.append(record.getMessage())

    handler = _Grab(level=logging.WARNING)
    previous = app_logger.level
    if previous > logging.WARNING:
        app_logger.setLevel(logging.WARNING)
    app_logger.addHandler(handler)
    try:
        monkeypatch.setattr(settings, "FACEBOOK_APP_SECRET", SECRET, raising=False)
        equal, _, _ = await request_deletion(scene, SECRET, subject)
        assert equal.status_code == 200, equal.text
        receipt = receipt_of(world, equal.json()["confirmation_code"])
        assert (
            receipt["app_identity"],
            receipt["state"],
            receipt["review_reason"],
        ) == ("facebook:legacy", "needs_review", "app_unrecognized")
        assert scopes_of(world, receipt["id"]) == [], (
            "the mapped subject is not reached"
        )
        assert len([w for w in warnings if "hold the same app secret" in w]) == 1
        assert SECRET not in "\n".join(warnings)

        # The control: with a secret of its own the legacy app is no ambiguity,
        # and the Instagram app's request for the same subject finds its mapping.
        warnings.clear()
        monkeypatch.setattr(
            settings, "FACEBOOK_APP_SECRET", LEGACY_SECRET, raising=False
        )
        distinct, _, _ = await request_deletion(scene, SECRET, subject)
        found = receipt_of(world, distinct.json()["confirmation_code"])
        assert (found["app_identity"], found["state"]) == (APP, "pending")
        assert len(scopes_of(world, found["id"])) == 1
        assert [w for w in warnings if "hold the same app secret" in w] == []
    finally:
        app_logger.removeHandler(handler)
        app_logger.setLevel(previous)


async def test_a_secret_rotation_is_the_same_app_so_one_episode(
    scene, armed, monkeypatch
):
    world = scene.world
    await mapped(scene, REF_ROTATION, armed)
    subject = app_scoped_of(REF_ROTATION)

    first, segment, _ = await request_deletion(scene, SECRET, subject)
    monkeypatch.setattr(
        settings, "INSTAGRAM_APP_SECRET", "ig-rotated-0e4b", raising=False
    )
    again, _, _ = await request_deletion(
        scene, "ig-rotated-0e4b", subject, segment=segment
    )
    assert again.status_code == 200, again.text
    a, b = (receipt_of(world, r.json()["confirmation_code"]) for r in (first, again))
    assert a["id"] == b["id"] and a["app_identity"] == APP
    assert len(scopes_of(world, a["id"])) == 1


async def test_a_key_rotation_keeps_one_episode_and_stores_the_version_it_first_saw(
    scene, armed
):
    world = scene.world
    await mapped(scene, REF_KEYS, armed, keys=KEYS_V1)
    subject = app_scoped_of(REF_KEYS)

    first, segment, _ = await request_deletion(scene, SECRET, subject)
    r1 = receipt_of(world, first.json()["confirmation_code"])
    assert r1["version"] == 1

    scene.app.state.past_posts_keys = KEYS_V2
    again, _, _ = await request_deletion(scene, SECRET, subject, segment=segment)
    r2 = receipt_of(world, again.json()["confirmation_code"])
    assert r2["id"] == r1["id"], "matched by the older version's fingerprint"
    assert r2["version"] == 1 and r2["fingerprint"] == r1["fingerprint"]

    fresh, _, _ = await request_deletion(
        scene, SECRET, subject, issued_at=int(time.time())
    )
    r3 = receipt_of(world, fresh.json()["confirmation_code"])
    assert r3["id"] != r1["id"] and r3["version"] == 2


async def test_a_subject_nobody_mapped_is_complete_at_once(scene, armed):
    world = scene.world
    armed(scene)
    answered, _, _ = await request_deletion(scene, SECRET, app_scoped_of(REF_UNMAPPED))
    code = answered.json()["confirmation_code"]
    receipt = receipt_of(world, code)
    assert (receipt["state"], scopes_of(world, receipt["id"])) == ("completed", [])
    page = await status_of(scene, code)
    assert (page.status_code, page.json()["status"]) == (200, "completed")
    assert page.headers["cache-control"] == "no-store"


async def test_the_status_page_refuses_what_nothing_issued_and_what_is_no_code(
    scene, armed
):
    armed(scene)
    unknown = await status_of(scene, "0123456789abcdef0123456789abcdef")
    assert (unknown.status_code, unknown.json()) == (
        404,
        {"detail": "unknown confirmation code"},
    )
    assert unknown.headers["cache-control"] == "no-store"
    for bad in (
        "0123456789abcdef0123456789abcde",
        "0123456789ABCDEF0123456789ABCDEF",
        "x",
    ):
        refused = await status_of(scene, bad)
        assert (refused.status_code, refused.json()) == (
            400,
            {"detail": "malformed confirmation code"},
        )


async def test_a_receipt_reads_back_through_its_capability_after_the_app_is_rebuilt(
    scene, armed
):
    """A restart loses the process and keeps the tables: a receipt created
    through the API answers `in_progress` or `completed` to its capability from
    an app built afresh over the same database and key ring, and a capability
    that was never issued reads nothing. The ring read from the environment is
    the one the receipt was made under, so an exact retry after the restart is
    still the same episode."""
    world = scene.world
    await mapped(scene, REF_RESTART, armed)
    subject = app_scoped_of(REF_RESTART)
    owed, segment, _ = await request_deletion(scene, SECRET, subject)
    settled, _, _ = await request_deletion(
        scene, SECRET, app_scoped_of(REF_RESTART_UNMAPPED)
    )
    codes = {
        "in_progress": owed.json()["confirmation_code"],
        "completed": settled.json()["confirmation_code"],
    }
    receipts = {status: receipt_of(world, code)["id"] for status, code in codes.items()}
    assert receipts["in_progress"] != receipts["completed"]
    for status, code in codes.items():
        assert (await status_of(scene, code)).json()["status"] == status
    stored = receipts_count(world)

    async with restarted(scene, KEYS_V2) as rebuilt:
        assert rebuilt.app is not scene.app
        assert rebuilt.app.state.past_posts_keys == KEYS_V2

        for status, code in codes.items():
            page = await status_of(rebuilt, code)
            assert page.status_code == 200, page.text
            assert (page.json()["code"], page.json()["status"]) == (code, status)
            assert page.headers["cache-control"] == "no-store"

        # A capability nobody was given: the right one with a digit changed, and
        # one that never existed. Neither reads a state, nor a hint of one.
        code = codes["in_progress"]
        wrong = (
            code[:-1] + ("0" if code[-1] != "0" else "1"),
            "0123456789abcdef0123456789abcdef",
        )
        for capability in wrong:
            assert capability not in codes.values()
            page = await status_of(rebuilt, capability)
            assert (page.status_code, page.json()) == (
                404,
                {"detail": "unknown confirmation code"},
            )
            assert page.headers["cache-control"] == "no-store"

        # The same ring: the exact retry is the one receipt behind a new capability.
        again, _, _ = await request_deletion(rebuilt, SECRET, subject, segment=segment)
        replay = again.json()["confirmation_code"]
        assert CODE_SHAPE.fullmatch(replay) and replay != codes["in_progress"]
        assert receipt_of(world, replay)["id"] == receipts["in_progress"]
        assert receipts_count(world) == stored
        for capability in (replay, codes["in_progress"]):
            assert (await status_of(rebuilt, capability)).json()["status"] == (
                "in_progress"
            )


async def test_a_verified_subject_that_is_no_id_records_nothing(scene, armed):
    armed(scene)
    before = receipts_count(scene.world)
    refused, _, _ = await request_deletion(scene, SECRET, "not-an-app-scoped-id")
    assert (refused.status_code, refused.json()) == (
        400,
        {"detail": "invalid signed_request"},
    )
    assert receipts_count(scene.world) == before


async def test_without_the_key_ring_the_receipt_is_derived_and_nothing_is_stored(
    scene, armed
):
    world, subject = scene.world, app_scoped_of(REF_KEYLESS)
    await scene.add(REF_KEYLESS, "example_handle_receipt", history=True)
    armed(scene, keys=None)
    before = receipts_count(world)

    answered, _, _ = await request_deletion(scene, SECRET, subject)
    assert answered.status_code == 200, answered.text
    code = answered.json()["confirmation_code"]
    assert code == meta_callbacks.confirmation_code(subject, SECRET)
    assert len(code) == 16
    assert answered.json()["url"] == f"{API}{STATUS}?code={code}"
    # The subject IS mapped, and with no ring nothing consults the mapping.
    assert receipts_count(world) == before

    page = await status_of(scene, code)
    assert (page.status_code, page.json()["status"]) == (200, "received")
