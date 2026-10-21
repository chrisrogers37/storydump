"""Verified Meta identity mappings through the real Instagram callback, as the
production role (`meta_identity_mappings`).

The OAuth gate's world: the real app on the replayed target schema, connected
as `svc_ingress`, Instagram the only stub (`exchange_code` hands back the grant
a scene names, with the app-scoped id the identity read proved). What is
asserted is what the callback LEFT in the tables, read as the migration actor,
and what the tenant-free door finds as the API's own login with no tenant:

* where the past-posts source is live, one mapping per (credential, authorization),
  current, joined to the generation on the SAME authorization id and time; a
  reconnect lands a new one and the previous falls;
* an exchange that proved no app-scoped id refuses the whole landing;
* where the past-posts source is not live nothing is recorded;
* the door finds the mapping by (app, the person's id) and by nothing else: not
  by the account's own id, and not under another app;
* deauthorize runs the account-reference lookup where no mapping names the
  subject, and only the past-posts-aware revocation where one does (nothing is
  revoked twice).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import psycopg2
import pytest

from tests.scripts import test_oauth_past_posts_choice_gate as oauth_gate
from tests.scripts.test_oauth_past_posts_choice_gate import (
    APP_ID,
    LIVE,
    REF_NEW,
    REF_X,
    Scene,
    app_scoped_of,
    choice,
    count,
    error_page,
    one,
    rows,
    state_of,
)

#: The OAuth gate's world, registered here as fixtures by assignment.
world = oauth_gate.world
fresh_preauth_budget = oauth_gate.fresh_preauth_budget
google_configured = oauth_gate.google_configured
instagram_configured = oauth_gate.instagram_configured
scene = oauth_gate.scene

pytestmark = [pytest.mark.integration, pytest.mark.slow]

APP = f"instagram:{APP_ID}"
#: Accounts only the door tests use: the door reads across every workspace of
#: the shared database, so a subject another test mapped would answer here too.
REF_DOOR = "17841400000000041"
REF_DOOR_RECONNECT = "17841400000000042"
MAPPING_COLUMNS = (
    "SELECT ig_account_id::text AS account, credential_id::text AS credential,"
    "       authorization_id::text AS authorization, app_identity, subject_ref,"
    "       account_ref, method, authorized_at, verified_at, is_current"
    "  FROM meta_identity_mappings WHERE workspace_id = %s"
    " ORDER BY created_at, authorization_id"
)


def mappings(world, ws):
    return rows(world, MAPPING_COLUMNS, ws)


def credential_of(world, ws, account):
    return one(
        world,
        "SELECT id::text AS id FROM oauth_credentials"
        " WHERE workspace_id = %s AND ig_account_id = %s AND provider = 'ig_login'",
        ws,
        account,
    )["id"]


def generations(world, ws):
    return rows(
        world,
        "SELECT authorization_id::text AS authorization, authorized_at, subject_id"
        "  FROM instagram_past_posts_generations WHERE workspace_id = %s"
        " ORDER BY created_at, id",
        ws,
    )


def door(world, app, subject):
    """`fn_meta_subject_mappings` as the API's own login, claiming no tenant."""
    conn = psycopg2.connect(world["ingress"])
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT current_user, current_setting('app.tenant_id', true)")
            who, tenant = cur.fetchone()
            assert (who, tenant in (None, "")) == ("svc_ingress", True)
            cur.execute(
                "SELECT o_workspace_id::text, o_ig_account_id::text,"
                "       o_credential_id::text, o_authorization_id::text, o_is_current"
                "  FROM fn_meta_subject_mappings(%s, %s)",
                (app, subject),
            )
            return cur.fetchall()
    finally:
        conn.close()


async def test_the_callback_records_one_current_mapping_joined_to_the_past_posts_generation(
    scene,
):
    world, ws = scene.world, scene.ws
    landed = await scene.add(REF_NEW, "example_handle_new", history=True)

    (mapping,) = mappings(world, ws)
    credential = credential_of(world, ws, landed.account)
    assert (
        mapping["account"],
        mapping["credential"],
        mapping["app_identity"],
        mapping["subject_ref"],
        mapping["account_ref"],
        mapping["method"],
        mapping["is_current"],
    ) == (
        landed.account,
        credential,
        APP,
        app_scoped_of(REF_NEW),
        REF_NEW,
        "oauth_exchange",
        True,
    )
    # The person's id is not the account's: the mapping carries both.
    assert mapping["subject_ref"] != mapping["account_ref"]

    # One authorization id and one time for the generation and the identity it
    # was verified under; the generation keeps the account's own id as subject.
    (generation,) = generations(world, ws)
    assert mapping["authorization"] == generation["authorization"]
    assert mapping["authorized_at"] == mapping["verified_at"]
    assert mapping["authorized_at"] == generation["authorized_at"]
    assert generation["subject_id"] == REF_NEW


async def test_a_reconnect_lands_a_new_current_mapping_and_the_previous_one_falls(
    scene,
):
    world, ws = scene.world, scene.ws
    first = await scene.add(REF_X, "example_handle_x", history=True)
    (before,) = mappings(world, ws)

    state = state_of(await scene.start(first.account, choice=None))
    landed = await scene.callback(state, REF_X, "example_handle_x")
    assert landed.status_code == 302, landed.text

    old, new = mappings(world, ws)
    assert (old["authorization"], old["is_current"]) == (before["authorization"], False)
    assert new["authorization"] != before["authorization"]
    assert new["is_current"] is True
    # Same credential row (replaced in place), same account, same person.
    assert new["credential"] == old["credential"] == before["credential"]
    assert (new["account"], new["subject_ref"]) == (old["account"], old["subject_ref"])
    # The earlier row is evidence: nothing but its flag moved.
    assert {k: v for k, v in old.items() if k != "is_current"} == {
        k: v for k, v in before.items() if k != "is_current"
    }
    assert [m["is_current"] for m in mappings(world, ws)].count(True) == 1
    # The new authorization is the generation's too.
    assert new["authorization"] in {g["authorization"] for g in generations(world, ws)}


async def test_an_exchange_that_proved_no_app_scoped_id_refuses_the_whole_landing(
    scene,
):
    """Nothing can be mapped, and without the mapping a deauthorize or a
    deletion could not find this person: the landing is refused whole (no
    destination, credential, source, generation, result, mix change or
    mapping), and the control beside it lands with its mapping."""
    world, ws = scene.world, scene.ws
    before = scene.landing()
    state = state_of(await scene.start(choice=choice(True, 1)))
    failed = await scene.callback(state, REF_NEW, "example_handle_new", app_scoped=None)
    assert failed.status_code == 302, failed.text
    assert failed.headers["location"] == error_page("history_setup_failed")
    assert scene.landing() == before
    assert mappings(world, ws) == []

    landed = await scene.add(REF_NEW, "example_handle_new", history=True)
    assert landed.result["outcome"] == "connected_history_on"
    assert len(mappings(world, ws)) == 1


@pytest.mark.parametrize(
    "capability,converted",
    [(None, True), (LIVE, False)],
    ids=["no-capability", "capability-but-unconverted-mix"],
)
async def test_where_the_past_posts_is_not_live_no_mapping_is_recorded(
    world,
    google_configured,
    instagram_configured,
    monkeypatch,
    tmp_path,
    capability,
    converted,
):
    """Production changes nothing before activation. The exchange proved an
    id, but the callback lands only the destination and its credential: not
    one mapping row is written."""
    async with Scene.open(
        world, monkeypatch, tmp_path, capability=capability, converted=converted
    ) as scene:
        state = state_of(await scene.start())
        landed = await scene.callback(state, REF_NEW, "example_handle_new")
        assert landed.status_code == 302, landed.text
        after = scene.landing()
        assert [a["provider_account_ref"] for a in after["accounts"]] == [REF_NEW]
        assert [c["state"] for c in after["credentials"]] == ["active"]
        assert after["sources"] == [] and after["generations"] == 0
        assert count(world, "meta_identity_mappings", scene.ws) == 0


async def test_the_door_finds_the_person_by_their_app_scoped_id_under_their_app_only(
    scene,
):
    world, ws = scene.world, scene.ws
    landed = await scene.add(REF_DOOR, "example_handle_door", history=True)
    (mapping,) = mappings(world, ws)

    # As the API's login with no tenant: the person's id under this app.
    assert door(world, APP, app_scoped_of(REF_DOOR)) == [
        (ws, landed.account, mapping["credential"], mapping["authorization"], True)
    ]
    # Identical raw subjects under another app never alias.
    assert door(world, "instagram:4343", app_scoped_of(REF_DOOR)) == []
    assert door(world, "facebook:legacy", app_scoped_of(REF_DOOR)) == []
    # The account's own id is not the person's: it finds nothing.
    assert door(world, APP, REF_DOOR) == []


async def test_a_reconnect_leaves_the_door_two_rows_one_of_them_current(scene):
    world, ws = scene.world, scene.ws
    first = await scene.add(REF_DOOR_RECONNECT, "example_handle_door", history=True)
    state = state_of(await scene.start(first.account, choice=None))
    landed = await scene.callback(state, REF_DOOR_RECONNECT, "example_handle_door")
    assert landed.status_code == 302, landed.text

    found = door(world, APP, app_scoped_of(REF_DOOR_RECONNECT))
    assert len(found) == 2
    assert sorted(row[4] for row in found) == [False, True]
    assert {row[2] for row in found} == {credential_of(world, ws, first.account)}


# --- deauthorize: by account reference, or the past-posts-aware path, never both ------

#: Accounts only the deauthorize tests use (and the legacy lookup's key: the
#: account's own id, which a callback's subject equals only when Meta says so).
REF_LEGACY_DEAUTH = "17841400000000051"
REF_MAPPED_DEAUTH = "17841400000000052"


def deauthorize_request(subject, *, issued_at=None):
    payload = {"algorithm": "HMAC-SHA256", "user_id": subject}
    if issued_at is not None:
        payload["issued_at"] = issued_at
    segment = (
        base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    )
    sig = hmac.new(b"sec", segment.encode("ascii"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode().rstrip("=") + "." + segment


async def deauthorize(scene, subject, **kw):
    """Meta's deauthorize through the real route as the API's own login, signed
    by the secret the OAuth gate's deployment holds."""
    return await scene.client.post(
        "/webhooks/meta/deauthorize",
        data={"signed_request": deauthorize_request(subject, **kw)},
    )


def credential_state(world, ws):
    return one(
        world,
        "SELECT state FROM oauth_credentials"
        " WHERE workspace_id = %s AND provider = 'ig_login'",
        ws,
    )["state"]


async def test_deauthorize_with_no_mapping_runs_the_account_reference_lookup(
    world, google_configured, instagram_configured, monkeypatch, tmp_path
):
    """While the past-posts source is not live the callback records no mapping, so the
    subject is matched by the account's own id and its credential revoked."""
    async with Scene.open(
        world, monkeypatch, tmp_path, capability=None, converted=True
    ) as scene:
        state = state_of(await scene.start())
        landed = await scene.callback(state, REF_LEGACY_DEAUTH, "example_handle_legacy")
        assert landed.status_code == 302, landed.text
        assert count(world, "meta_identity_mappings", scene.ws) == 0
        assert credential_state(world, scene.ws) == "active"

        answered = await deauthorize(scene, REF_LEGACY_DEAUTH)
        assert (answered.status_code, answered.json()) == (200, {"status": "ok"})
        assert credential_state(world, scene.ws) == "revoked"
        # A disconnect deletes and disables nothing.
        assert scene.landing()["accounts"][0]["state"] == "active"
        # And a second one is the completed no-op Meta is promised.
        again = await deauthorize(scene, REF_LEGACY_DEAUTH)
        assert (again.status_code, again.json()) == (200, {"status": "ok"})


async def test_deauthorize_with_a_mapping_never_also_runs_the_legacy_lookup(scene):
    """The subject names this account by BOTH keys: the verified mapping (Meta
    says the app-scoped id is the account's own) and `provider_account_ref`. The
    request predates the grant, so the ordering rule leaves the grant alone, and
    because a mapping names the subject the legacy lookup must not run behind
    it: the credential stays. The control removes the mapping, and the same
    request then revokes through the legacy lookup, which proves it matched."""
    world, ws = scene.world, scene.ws
    shown = (await scene.preview()).json()
    state = state_of(
        await scene.start(choice=choice(True, shown["expected_mix_revision"]))
    )
    landed = await scene.callback(
        state, REF_MAPPED_DEAUTH, "example_handle_mapped", app_scoped=REF_MAPPED_DEAUTH
    )
    assert landed.status_code == 302, landed.text
    assert door(world, APP, REF_MAPPED_DEAUTH) != []
    before = scene.landing()

    an_hour_before_the_grant = int(time.time()) - 3600
    answered = await deauthorize(
        scene, REF_MAPPED_DEAUTH, issued_at=an_hour_before_the_grant
    )
    assert (answered.status_code, answered.json()) == (200, {"status": "ok"})
    assert credential_state(world, ws) == "active"
    assert scene.landing() == before, "a newer grant is left entirely alone"

    # The control: with no mapping the very same request runs the
    # account-reference lookup.
    from tests.scripts.conftest import execute

    execute(
        world["stream"],
        "SET app.actor_kind = 'migration';"
        " DELETE FROM meta_identity_mappings WHERE workspace_id = %s",
        (ws,),
    )
    assert door(world, APP, REF_MAPPED_DEAUTH) == []
    await deauthorize(scene, REF_MAPPED_DEAUTH, issued_at=an_hour_before_the_grant)
    assert credential_state(world, ws) == "revoked"
