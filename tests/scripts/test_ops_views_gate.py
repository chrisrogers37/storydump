"""Phase 02 of the v2 CLI: the eight read views, through the real app AS THE
PRODUCTION ROLE, with two workspaces seeded so that "only this workspace's
rows" is proven rather than assumed.

Every view is one bounded, tenant-scoped query run under `p_tenant` as
``svc_ingress`` (the role the API runs as); nothing outside row-level
security is read (`rate_counters`, system jobs). Workspace B is seeded with
the same shapes as A and must be absent from every answer for A. The
routes admit tokens (a person-bound token loops its memberships; a service
identity reads its one workspace), refuse a stranger with 404, and answer
the phase-01 envelope.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import uuid

import psycopg2
import pytest
from sqlalchemy import text

from tests.scripts.conftest import (
    _dsn,
    _scratch,
    as_user,
    replay_advertised_stream,
    seed_intent_chain,
    set_test_passwords,
)
from tests.src.api import conftest as api_conftest
from tests.src.api.conftest import api_client, sign_in

google_configured = api_conftest.google_configured

pytestmark = [pytest.mark.integration, pytest.mark.slow]

VIEWS = ("story", "cards", "floating", "account", "jobs", "outbox", "burst")


@pytest.fixture(scope="module")
def world(admin_conn, owner_actor):
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        stream = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        yield {
            "stream": stream,
            "ingress": as_user(db, "svc_ingress"),
            # the maintenance login: a superuser here, BYPASSRLS like the owner
            # role production connects as today — the arm that proves the
            # explicit tenant predicates rather than the policies
            "bypass": _dsn(db.rsplit("/", 1)[-1]),
        }
    finally:
        gen.close()


def _run(coro):
    return asyncio.run(coro)


def _sql(dsn: str, sql: str, params=(), *, actor: bool = True):
    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            if actor:
                cur.execute("SET app.actor_kind = 'migration'")
            cur.execute(sql, params)
            rows = cur.fetchall() if cur.description else None
        conn.commit()
        return rows
    finally:
        conn.close()


def _bearer(secret: str) -> dict:
    return {"Authorization": f"Bearer {secret}"}


def _seed_world(dsn: str, ws: str, tag: str) -> dict:
    """One workspace's ledger, shaped like a real burst: a floating story with
    its job, a refused-then-accepted container, a float wait, a review card,
    a posted story, a card with its binding, a failed notification, an
    account with today's count. Returns the ids."""
    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            floating = seed_intent_chain(cur, ws, f"{tag}-float", state="approved")
            iga = str(floating["iga"])
            cur.execute(
                "UPDATE ig_accounts SET handle = %s, posts_per_day = 5,"
                " tz = 'America/New_York', next_slot_at = now() + interval '1 hour'"
                " WHERE id = %s",
                (f"@{tag}_acct", iga),
            )
            cur.execute(
                "UPDATE post_intents SET publish_step = 'transit_uploaded',"
                " cap_consumed_on = current_date,"
                " attempts_by_step = '{\"fetch\": 1}'::jsonb"
                " WHERE id = %s",
                (str(floating["intent"]),),
            )
            cur.execute(
                "INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at,"
                " state, max_attempts, payload)"
                " VALUES ('publish_pipeline', %s, 'interactive', %s,"
                " now() + interval '60 seconds', 'ready', 5,"
                " jsonb_build_object('v', 1, 'intent_id', %s))",
                (ws, f"publish:{iga}", str(floating["intent"])),
            )
            # an earlier retry of the same story that died: the live job wins
            cur.execute(
                "INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at,"
                " state, attempts, max_attempts, payload)"
                " VALUES ('publish_pipeline', %s, 'interactive', %s,"
                " now() - interval '30 minutes', 'failed', 5, 5,"
                " jsonb_build_object('v', 1, 'intent_id', %s))",
                (ws, f"publish:{iga}:old", str(floating["intent"])),
            )
            # dated a minute ago so the posted story below "posts past" this
            # waiter (the sibling join needs the wait to precede the post)
            cur.execute(
                "INSERT INTO audit_events (workspace_id, entity_kind, entity_id,"
                " from_state, to_state, actor_kind, channel, detail, created_at)"
                " VALUES (%s, 'post_intent', %s, 'approved', 'approved', 'system',"
                " 'system', %s, now() - interval '1 minute')",
                (
                    ws,
                    str(floating["intent"]),
                    json.dumps(
                        {
                            "v": 1,
                            "event": "float_wait",
                            "class": "fetch",
                            "rung": 1,
                            "seconds": 30.0,
                            "next_run_at": (
                                dt.datetime.now(dt.timezone.utc)
                                + dt.timedelta(seconds=60)
                            ).isoformat(),
                            "counters": {"fetch": 1},
                        }
                    ),
                ),
            )
            for generation, state, variant, error in (
                (1, "failed", 0, "9004"),
                (2, "succeeded", 1, None),
            ):
                cur.execute(
                    "INSERT INTO provider_operations (workspace_id, intent_id, provider,"
                    " op_kind, business_key, generation, state, lease_token, response_ref)"
                    " VALUES (%s, %s, 'ig', 'container_create', %s, %s, %s, %s, %s)",
                    (
                        ws,
                        str(floating["intent"]),
                        f"{tag}:container:{generation}",
                        generation,
                        state,
                        str(uuid.uuid4()),
                        json.dumps(
                            {
                                "v": 1,
                                "url_variant": variant,
                                "error": error,
                                "meta": {"subcode": "2207052"} if error else {},
                                "elapsed_ms": 340 if error else 4800,
                            }
                        ),
                    ),
                )
            cur.execute(
                "INSERT INTO channel_bindings (workspace_id, channel, external_ref)"
                " VALUES (%s, 'telegram_group', %s) RETURNING id",
                (ws, f"-100{abs(hash(tag)) % 10**6}"),
            )
            binding = str(cur.fetchone()[0])
            cur.execute(
                "INSERT INTO channel_outbox (workspace_id, binding_id, kind, intent_id,"
                " payload, state, attempts, external_message_ref)"
                " VALUES (%s, %s, 'approval_prompt', %s,"
                " '{\"v\": 1, \"outcome_text\": \"waiting\"}'::jsonb, 'sent', 1, '7587')",
                (ws, binding, str(floating["intent"])),
            )
            cur.execute(
                "INSERT INTO channel_outbox (workspace_id, binding_id, kind, payload,"
                " state, attempts)"
                " VALUES (%s, %s, 'notification', '{\"v\": 1}'::jsonb, 'failed', 3)",
                (ws, binding),
            )
            # a float whose retry job died: approved, debited, the job failed
            dead = seed_intent_chain(cur, ws, f"{tag}-dead", state="approved")
            cur.execute(
                "UPDATE post_intents SET publish_step = 'none',"
                " cap_consumed_on = current_date WHERE id = %s",
                (str(dead["intent"]),),
            )
            cur.execute(
                "INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at,"
                " state, attempts, max_attempts, payload)"
                " VALUES ('publish_pipeline', %s, 'interactive', %s,"
                " now() - interval '10 minutes', 'failed', 5, 5,"
                " jsonb_build_object('v', 1, 'intent_id', %s))",
                (ws, f"publish:{str(dead['iga'])}", str(dead["intent"])),
            )
            review = seed_intent_chain(
                cur, ws, f"{tag}-review", state="review_required"
            )
            cur.execute(
                "UPDATE post_intents SET last_error ="
                ' \'{"v": 1, "reason": "poison", "detail": "9004 x4"}\'::jsonb'
                " WHERE id = %s",
                (str(review["intent"]),),
            )
            cur.execute(
                "INSERT INTO audit_events (workspace_id, entity_kind, entity_id,"
                " from_state, to_state, actor_kind, channel)"
                " VALUES (%s, 'post_intent', %s, 'approved', 'review_required',"
                " 'system', 'system')",
                (ws, str(review["intent"])),
            )
            # a posted row must be complete (`ck_posted_complete`): the manual
            # path — a human confirmed, the cap debited — taken from awaiting
            posted = seed_intent_chain(
                cur, ws, f"{tag}-posted", state="awaiting_approval"
            )
            cur.execute(
                "UPDATE post_intents SET state = 'posted', published_via = 'manual',"
                " cap_consumed_on = current_date, ig_account_id = %s,"
                " schedule_slot_at = now() - interval '1 hour' WHERE id = %s",
                (iga, str(posted["intent"])),
            )
            cur.execute(
                "INSERT INTO daily_post_counts (workspace_id, ig_account_id, local_date,"
                " count, cap_at_write) VALUES (%s, %s, (now() AT TIME ZONE"
                " 'America/New_York')::date, 2, 5)",
                (ws, iga),
            )
            cur.execute(
                "INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at,"
                " state, attempts, max_attempts, payload, created_at, updated_at)"
                " VALUES ('sync_media_source', %s, 'bulk', %s,"
                " now() - interval '2 days', 'failed', 4, 4,"
                " '{\"v\": 1}'::jsonb, now() - interval '2 days', now() - interval '2 days')",
                (ws, f"sync:{tag}:old"),
            )
            # minted two days ago, failed just now: `reschedule_job` reuses a
            # row across retries, so a long float's retry that dies is an OLD
            # row with a NEW failure — the window must see it
            cur.execute(
                "INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at,"
                " state, attempts, max_attempts, payload, created_at)"
                " VALUES ('deliver_outbox', %s, 'bulk', %s,"
                " now() - interval '1 minute', 'failed', 3, 3,"
                " '{\"v\": 1}'::jsonb, now() - interval '2 days')",
                (ws, f"deliver:{tag}:long-float"),
            )
            # a timeline longer than the view's bound: the NEWEST rows must be
            # the ones kept (an operator reads the current state, not the start)
            cur.execute(
                "INSERT INTO audit_events (workspace_id, entity_kind, entity_id,"
                " from_state, to_state, actor_kind, channel, detail, created_at)"
                " SELECT %s, 'post_intent', %s, 'approved', 'approved', 'system',"
                " 'system', jsonb_build_object('v', 1, 'event', 'note', 'n', g),"
                " now() - interval '2 hours' + (g * interval '1 second')"
                " FROM generate_series(1, 520) AS g",
                (ws, str(floating["intent"])),
            )
            open_story = seed_intent_chain(
                cur, ws, f"{tag}-open", state="awaiting_approval"
            )
        conn.commit()
        return {
            "iga": iga,
            "floating": str(floating["intent"]),
            "dead": str(dead["intent"]),
            "review": str(review["intent"]),
            "posted": str(posted["intent"]),
            "open": str(open_story["intent"]),
            "binding": binding,
        }
    finally:
        conn.close()


@pytest.fixture(scope="module")
def seeded(world):
    """Two workspaces created through the API by two people, each seeded the
    same way, plus one system job that belongs to no workspace."""
    state: dict = {}

    async def main():
        async with api_client(world["ingress"]) as (client, engine):
            import pytest as _pytest

            mp = _pytest.MonkeyPatch()
            # the sign-in world `google_configured` builds, at module scope
            for name, value in (
                ("GOOGLE_CLIENT_ID", api_conftest.CLIENT_ID),
                ("GOOGLE_CLIENT_SECRET", "sec"),
                ("OAUTH_REDIRECT_BASE_URL", api_conftest.API),
                ("WEB_APP_URL", api_conftest.FRONT),
                ("SESSION_COOKIE_DOMAIN", api_conftest.COOKIE_DOMAIN),
            ):
                mp.setattr(api_conftest.settings, name, value, raising=False)
            try:
                for who in ("a", "b"):
                    owner = await sign_in(
                        client,
                        mp,
                        sub=f"sub-ops-{who}",
                        email=f"ops-{who}@example.test",
                    )
                    created = await client.post(
                        "/api/v1/workspaces",
                        json={"name": f"Ops {who.upper()}", "tz": "America/New_York"},
                        headers={**owner, "Idempotency-Key": f"create-ops-{who}"},
                    )
                    assert created.status_code == 201, created.text
                    ws = created.json()["workspace_id"]
                    minted = await client.post(
                        "/api/v1/me/tokens",
                        json={"name": f"agent-{who}", "role": "operator"},
                        headers=owner,
                    )
                    readonly = await client.post(
                        "/api/v1/me/tokens",
                        json={"name": f"reader-{who}", "role": "readonly"},
                        headers=owner,
                    )
                    service = await client.post(
                        f"/api/v1/workspaces/{ws}/tokens",
                        json={"name": f"svc-{who}"},
                        headers=owner,
                    )
                    state[who] = {
                        "ws": ws,
                        "session": owner,
                        "token": minted.json()["secret"],
                        "readonly": readonly.json()["secret"],
                        "service": service.json()["secret"],
                        **_seed_world(world["stream"], ws, f"ops{who}"),
                    }
                    # a real command through the token, so the ledger carries a
                    # `cli_command` row and a tap-shaped transition for `burst`
                    skipped = await client.post(
                        f"/api/v1/workspaces/{ws}/commands/skip",
                        json={"intent_id": state[who]["open"]},
                        headers={
                            **_bearer(state[who]["token"]),
                            "Idempotency-Key": f"skip:{state[who]['open']}",
                        },
                    )
                    assert skipped.status_code == 200, skipped.text
            finally:
                mp.undo()

    _run(main())
    _sql(
        world["stream"],
        "INSERT INTO jobs (kind, workspace_id, lane, serialization_key, state,"
        " max_attempts, payload) VALUES ('reap_expired', NULL, 'bulk', 'reap',"
        " 'ready', 1, '{\"v\": 1}'::jsonb)",
    )
    return state


def _view(client, ws: str, view: str, headers: dict, suffix: str = ""):
    return client.get(f"/api/v1/ops/workspaces/{ws}/{view}{suffix}", headers=headers)


def _foreign(other: dict) -> set:
    """Everything of the other workspace that could betray a leaked row —
    its id, its stories, its account, its binding (a leaked burst row
    carries the caller's workspace label, so the label proves nothing)."""
    return {
        other[k]
        for k in (
            "ws",
            "floating",
            "dead",
            "review",
            "posted",
            "open",
            "iga",
            "binding",
        )
    }


def test_every_view_returns_only_this_workspaces_rows_as_svc_ingress(
    world, google_configured, seeded
):
    a, b = seeded["a"], seeded["b"]

    async def main():
        async with api_client(world["ingress"]) as (client, engine):
            async with engine.connect() as conn:
                who = (await conn.execute(text("SELECT current_user"))).scalar()
                assert who == "svc_ingress", who
            token = _bearer(a["token"])

            story = await _view(client, a["ws"], f"story/{a['floating']}", token)
            assert story.status_code == 200, story.text
            body = story.json()
            assert (body["v"], body["kind"], body["error"]) == (1, "story", None)
            assert body["data"]["workspace_id"] == a["ws"]
            (row,) = body["data"]["rows"]
            assert row["workspace_id"] == a["ws"]
            assert row["intent"]["id"] == a["floating"]
            assert row["intent"]["state"] == "approved"
            assert row["intent"]["publish_step"] == "transit_uploaded"
            assert row["intent"]["attempts_by_step"] == {"fetch": 1}
            assert [o["generation"] for o in row["operations"]] == [1, 2]
            assert [o["url_variant"] for o in row["operations"]] == [0, 1]
            assert row["operations"][0]["subcode"] == "2207052"
            assert [c["external_message_ref"] for c in row["cards"]] == ["7587"]
            assert any(e["detail"].get("event") == "float_wait" for e in row["audit"])
            notes = [
                e["detail"]["n"]
                for e in row["audit"]
                if e["detail"].get("event") == "note"
            ]
            assert len(row["audit"]) == 500 and 520 in notes and 1 not in notes, (
                "a timeline past the bound keeps its NEWEST rows"
            )
            assert row["audit"] == sorted(row["audit"], key=lambda e: e["at"]), (
                "and still reads oldest to newest"
            )
            assert row["truncated"] == ["audit"], "and says which list was cut"
            missing = await _view(client, a["ws"], f"story/{b['floating']}", token)
            assert missing.status_code == 200 and missing.json()["data"]["rows"] == []

            cards = await _view(client, a["ws"], f"cards/{a['floating']}", token)
            assert cards.status_code == 200, cards.text
            rows = cards.json()["data"]["rows"]
            assert [c["external_message_ref"] for c in rows] == ["7587"]
            assert rows[0]["channel"] == "telegram_group"
            assert rows[0]["outcome_text"] == "waiting"

            floating = await _view(client, a["ws"], "floating", token)
            rows = {r["id"]: r for r in floating.json()["data"]["rows"]}
            assert set(rows) == {a["floating"], a["dead"]}, rows
            assert rows[a["floating"]]["job_state"] == "ready"
            assert rows[a["floating"]]["last_wait_class"] == "fetch"
            assert rows[a["floating"]]["last_wait_rung"] == 1
            assert rows[a["dead"]]["job_state"] == "failed", (
                "a float whose retry died is the failure the watch exits on"
            )
            assert rows[a["dead"]]["job_attempts"] == 5

            for key in (a["iga"], "@opsa_acct", "opsa_acct", "OPSA_ACCT"):
                account = await _view(client, a["ws"], f"account/{key}", token)
                assert account.status_code == 200, (key, account.text)
                (acct,) = account.json()["data"]["rows"]
                assert acct["id"] == a["iga"] and acct["posts_per_day"] == 5
                assert acct["tz"] == "America/New_York"
                assert acct["today"] == {
                    "local_date": acct["today"]["local_date"],
                    "count": 2,
                    "cap_at_write": 5,
                }
                assert {r["state"] for r in acct["recent"]} >= {"posted", "approved"}
            nobody = await _view(client, a["ws"], "account/@nobody", token)
            assert nobody.status_code == 200 and nobody.json()["data"]["rows"] == []

            jobs = await _view(client, a["ws"], "jobs", token)
            rows = jobs.json()["data"]["rows"]
            groups = {(r["kind"], r["lane"], r["state"]): r for r in rows}
            assert groups[("publish_pipeline", "interactive", "ready")]["count"] == 1
            assert (
                groups[("publish_pipeline", "interactive", "ready")]["samples"] is None
            )
            assert ("reap_expired", "bulk", "ready") not in groups, (
                "a system job belongs to no workspace"
            )
            assert ("sync_media_source", "bulk", "failed") not in groups, (
                "outside the 3-hour window"
            )
            recent = groups[("deliver_outbox", "bulk", "failed")]
            assert recent["count"] == 1 and recent["samples"][0]["attempts"] == 3, (
                "an old row that failed inside the window is inside the window"
            )
            old = await _view(client, a["ws"], "jobs", token, "?since=72h")
            failed = {
                (r["kind"], r["lane"], r["state"]): r
                for r in old.json()["data"]["rows"]
            }[("sync_media_source", "bulk", "failed")]
            assert failed["count"] == 1 and failed["samples"][0]["attempts"] == 4

            outbox = await _view(client, a["ws"], "outbox", token)
            rows = outbox.json()["data"]["rows"]
            assert [(r["kind"], r["state"], r["count"]) for r in rows] == [
                ("notification", "failed", 1)
            ]
            assert rows[0]["external_ref"].startswith("-100")

            burst = await _view(client, a["ws"], "burst", token)
            assert burst.status_code == 200, burst.text
            rows = burst.json()["data"]["rows"]
            sections = {r["section"] for r in rows}
            assert {"tap", "permit", "float_wait", "sibling", "review", "outcome"} <= (
                sections
            )
            taps = [r for r in rows if r["section"] == "tap"]
            seen = [(t["intent_id"], t["to_state"], t["channel"]) for t in taps]
            # the seed's own awaiting → posted transition is a tap too
            assert seen == [
                (a["posted"], "posted", None),
                (a["open"], "skipped", "cli"),
            ]
            permits = [r for r in rows if r["section"] == "permit"]
            assert [
                (p["generation"], p["state"], p["url_variant"]) for p in permits
            ] == [
                (1, "failed", 0),
                (2, "succeeded", 1),
            ]
            (wait,) = [r for r in rows if r["section"] == "float_wait"]
            assert (wait["intent_id"], wait["wait_class"], wait["rung"]) == (
                a["floating"],
                "fetch",
                1,
            )
            assert wait["seconds"] == 30, "the ladder's float reads as its seconds"
            (sibling,) = [r for r in rows if r["section"] == "sibling"]
            assert (
                sibling["posted_id"],
                sibling["waiting_id"],
                sibling["wait_class"],
            ) == (
                a["posted"],
                a["floating"],
                "fetch",
            )
            (review,) = [r for r in rows if r["section"] == "review"]
            assert review["intent_id"] == a["review"]
            assert review["last_error"]["reason"] == "poison"
            outcomes = {
                r["state"]: r["count"] for r in rows if r["section"] == "outcome"
            }
            assert outcomes["skipped"] == 1
            assert all(r["workspace_id"] == a["ws"] for r in rows)

            for view in VIEWS:
                suffix = {
                    "story": f"/{a['floating']}",
                    "cards": f"/{a['floating']}",
                    "account": f"/{a['iga']}",
                }.get(view, "")
                resp = await _view(client, a["ws"], view + suffix, token)
                assert resp.status_code == 200, (view, resp.text)
                for row in resp.json()["data"]["rows"]:
                    assert row["workspace_id"] == a["ws"], (view, row)
                    dumped = json.dumps(row)
                    assert not any(f in dumped for f in _foreign(b)), (view, row)

            posture = await client.get("/api/v1/ops/posture", headers=token)
            assert posture.status_code == 200, posture.text
            data = posture.json()["data"]
            assert data["role"] == {"user": "svc_ingress", "bypassrls": False}
            assert data["ledger"] in ("present", "absent")
            rls = {r["table"]: r for r in data["rls"]}
            assert rls["post_intents"]["enabled"] is True
            assert "fn_reaper_stale_approved" in {d["name"] for d in data["doors"]}

    _run(main())


def test_the_routes_admit_tokens_by_their_scope_and_refuse_strangers(
    world, google_configured, seeded
):
    a, b = seeded["a"], seeded["b"]

    async def main():
        async with api_client(world["ingress"]) as (client, engine):
            anonymous = await _view(client, a["ws"], "floating", {})
            assert anonymous.status_code == 401

            readonly = await _view(client, a["ws"], "floating", _bearer(a["readonly"]))
            assert readonly.status_code == 200, readonly.text

            session = await _view(client, a["ws"], "floating", a["session"])
            assert session.status_code == 200, session.text

            stranger = await _view(client, a["ws"], "floating", _bearer(b["token"]))
            assert stranger.status_code == 404, "not a member reads as not found"

            own = await _view(client, b["ws"], "floating", _bearer(b["service"]))
            assert own.status_code == 200, own.text
            assert {r["id"] for r in own.json()["data"]["rows"]} == {
                b["floating"],
                b["dead"],
            }
            elsewhere = await _view(client, a["ws"], "floating", _bearer(b["service"]))
            assert (elsewhere.status_code, elsewhere.json()["reason"]) == (
                403,
                "wrong_workspace",
            )
            svc_posture = await client.get(
                "/api/v1/ops/posture", headers=_bearer(b["service"])
            )
            assert svc_posture.status_code == 200

            bad_since = await _view(
                client, a["ws"], "jobs", _bearer(a["token"]), "?since=yesterday"
            )
            assert bad_since.status_code == 422

    _run(main())


def test_the_predicates_confine_rows_even_without_row_level_security(
    world, google_configured, seeded
):
    """Production's API connects as the owner role with BYPASSRLS, so every
    view must confine rows by its own WHERE — the policies are the belt,
    the predicate the braces. The same reads through a superuser: only A's
    rows, and B's story is not readable through A's route."""
    a, b = seeded["a"], seeded["b"]

    async def main():
        async with api_client(world["bypass"]) as (client, engine):
            async with engine.connect() as conn:
                bypass = (
                    await conn.execute(
                        text(
                            "SELECT rolbypassrls OR rolsuper FROM pg_roles"
                            " WHERE rolname = current_user"
                        )
                    )
                ).scalar()
                assert bypass, "this arm must not be filtered by the policies"
            token = _bearer(a["token"])
            for view, suffix in (
                ("story", f"/{a['floating']}"),
                ("cards", f"/{a['floating']}"),
                ("floating", ""),
                ("account", f"/{a['iga']}"),
                ("jobs", "?since=72h"),
                ("outbox", ""),
                ("burst", ""),
            ):
                resp = await _view(client, a["ws"], view + suffix, token)
                assert resp.status_code == 200, (view, resp.text)
                rows = resp.json()["data"]["rows"]
                assert rows, view
                for row in rows:
                    assert row["workspace_id"] == a["ws"], (view, row)
                    dumped = json.dumps(row)
                    assert not any(f in dumped for f in _foreign(b)), (view, row)
            crossed = await _view(client, a["ws"], f"story/{b['floating']}", token)
            assert crossed.json()["data"]["rows"] == []
            crossed = await _view(client, a["ws"], f"cards/{b['floating']}", token)
            assert crossed.json()["data"]["rows"] == []
            crossed = await _view(client, a["ws"], f"account/{b['iga']}", token)
            assert crossed.json()["data"]["rows"] == []
            jobs = await _view(client, a["ws"], "jobs", token)
            kinds = {r["kind"] for r in jobs.json()["data"]["rows"]}
            assert "reap_expired" not in kinds
            burst = await _view(client, a["ws"], "burst", token)
            outcomes = {
                r["state"]: r["count"]
                for r in burst.json()["data"]["rows"]
                if r["section"] == "outcome"
            }
            assert outcomes["skipped"] == 1, "B's skip must not be counted into A"

    _run(main())


def test_posture_shows_rls_drift_and_every_ledger_state(
    world, google_configured, seeded
):
    """`posture` exists for two facts: a tenant table whose RLS was dropped,
    and the runner's ledger under the runner's grant (F7). A replayed
    database has no ledger (absent); one created without the grant is
    unreadable, not guessed; with the grant it is present and listed."""
    a = seeded["a"]

    async def main():
        async with api_client(world["ingress"]) as (client, engine):
            token = _bearer(a["token"])

            async def read():
                resp = await client.get("/api/v1/ops/posture", headers=token)
                assert resp.status_code == 200, resp.text
                return resp.json()["data"]

            before = await read()
            assert before["ledger"] == "absent" and before["migrations"] == []
            tables = {r["table"]: r for r in before["rls"]}
            assert tables["post_locks"]["enabled"] is True
            assert "rate_counters" not in tables, (
                "no workspace column, not tenant-plane"
            )

            _sql(world["bypass"], "ALTER TABLE post_locks DISABLE ROW LEVEL SECURITY")
            try:
                drifted = {r["table"]: r for r in (await read())["rls"]}
                assert drifted["post_locks"]["enabled"] is False, (
                    "a table whose RLS was dropped must show, not vanish"
                )
            finally:
                _sql(
                    world["bypass"], "ALTER TABLE post_locks ENABLE ROW LEVEL SECURITY"
                )

            _sql(
                world["bypass"],
                "CREATE SCHEMA runner;"
                " CREATE TABLE runner.schema_migrations (version INT PRIMARY KEY,"
                " checksum TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
                " applied_by TEXT NOT NULL, execution_ms INT, status TEXT);"
                " INSERT INTO runner.schema_migrations (version, checksum, applied_by, status)"
                " VALUES (77, 'a062', 'gate', 'applied')",
            )
            unreadable = await read()
            assert (
                unreadable["ledger"] == "unreadable" and unreadable["migrations"] == []
            )
            _sql(
                world["bypass"],
                "GRANT USAGE ON SCHEMA runner TO svc_ingress;"
                " GRANT SELECT ON runner.schema_migrations TO svc_ingress",
            )
            present = await read()
            assert present["ledger"] == "present"
            assert [m["version"] for m in present["migrations"]] == [77]

    _run(main())
