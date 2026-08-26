"""P3 gate — a settings edit BACK TO A PREVIOUS VALUE reaches the database.

F3 locked a client-generated UUID per submit and rejected a content hash
because the hash makes a genuine second edit back to a previous value a
duplicate and silently drops it: the port answers 200, the UI says saved, and
the setting does not move. P2 built the mechanism, but no real caller existed,
so the property could not be exercised against `command_dedup`. P3 is the
first caller, and this is where the property is checked.

**What this adds over the TypeScript test.** `command-client.test.ts` proves
the browser emits three distinct keys for the A -> B -> A sequence. It cannot
prove the port then admits all three, because admission is a row in a real
table with a real unique constraint. This drives that table.

**Why the principal is pinned to ONE session.** `command_dedup`'s key is
`(channel, principal, external_ref)` and the web adapter's principal is the
caller's session id (`v1.py:_dispatch`). A test that varied it would separate
the three calls on the principal instead of the key and pass no matter what
the key scheme was — vacuous, and it would look identical to a real pass. One
session making three edits is also the real scenario.

**The positive control is the load-bearing half.** Asserting that three UUID
keys admit proves nothing on its own: three keys of ANY scheme that happen to
differ would also admit, so the test cannot distinguish a correct client from
an untested table. The control runs the SAME sequence under F3's rejected
content-hash scheme and asserts the third call is refused as a replay. Only
with both does a pass mean the instrument can see the failure it is aimed at.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid

import psycopg2
import pytest

from src.services.target import webhook_ingress as ingress
from src.services.target.webhook_ingress import DeliveryReplayed
from tests.scripts.conftest import (
    _scratch,
    replay_advertised_stream,
    seed_workspace_chain,
    set_test_passwords,
)

pytestmark = pytest.mark.integration

#: The adapter under test. `v1.py::CHANNEL`.
CHANNEL = "web"

#: One signed-in person, making several edits. See the module docstring.
SESSION = "sess-p3-settings"


@pytest.fixture(scope="module")
def settings_db(admin_conn, owner_actor):
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        dsn = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        conn = psycopg2.connect(dsn)
        try:
            seed_workspace_chain(conn, "p3-settings")
        finally:
            conn.close()

        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.pool import NullPool

        engine = create_async_engine(
            dsn.replace("postgresql://", "postgresql+asyncpg://", 1),
            connect_args={"server_settings": {"app.actor_kind": "system"}},
            poolclass=NullPool,
        )
        try:
            yield {"owner": dsn, "engine": engine}
        finally:
            asyncio.run(engine.dispose())
    finally:
        gen.close()


def _call(settings_db, fn):
    async def go():
        async with settings_db["engine"].connect() as conn:
            out = await fn(conn)
            await conn.commit()
            return out

    return asyncio.run(go())


def _admit(settings_db, key: str, payload: dict):
    """One admission, exactly as `v1.py::_dispatch` performs it."""
    return _call(
        settings_db,
        lambda c: ingress.admit(
            c,
            channel=CHANNEL,
            external_ref=key,
            payload=payload,
            principal=SESSION,
        ),
    )


def _body(caption_style: str) -> dict:
    """The body the browser sends — `submitSettingsChange` wraps the map and
    adds the submission id, and the port fingerprints the RAW body."""
    return {
        "settings": {"caption_style": caption_style},
        "submission_id": str(uuid.uuid4()),
    }


def _per_submit_key(body: dict) -> str:
    """`idempotencyKeyFor("settings_change", submission_id)` — the shipped
    derivation, which is `${command}:${identity}`."""
    return f"settings_change:{body['submission_id']}"


def _hashed_body(caption_style: str) -> dict:
    """The body under F3's REJECTED option (b).

    It carries NO `submission_id`, and that is the whole of option (b) rather
    than an incidental difference: the point of a content hash is that no
    per-submission value is needed, so there is none to send. Modelling the
    control with one left in is what the first version of this test did, and
    it made the control fail for the wrong reason — the port fingerprints the
    RAW body, so a fresh id per call made call 3's content differ from call 1's
    and the port raised `AdmissionConflict` instead of `DeliveryReplayed`.

    That wrong control was still informative and the finding is kept: a PARTIAL
    content-hash scheme — hashing the settings while still sending a fresh id —
    fails LOUDLY at 409. It is the pure scheme that is silent, and only the
    pure scheme is what F3 rejected.
    """
    return {"settings": {"caption_style": caption_style}}


def _content_hash_key(body: dict) -> str:
    """The key option (b) would derive: a hash of what is being written."""
    payload = json.dumps(body["settings"], sort_keys=True)
    return f"settings_change:{hashlib.sha256(payload.encode()).hexdigest()[:32]}"


class TestTheEditBackToAPreviousValue:
    """A -> B -> A. The third call is the one that matters: its CONTENT is
    identical to the first, so only the key can separate them."""

    def test_all_three_submissions_are_admitted_under_the_shipped_scheme(
        self, settings_db
    ):
        bodies = [_body("enhanced"), _body("simple"), _body("enhanced")]
        keys = [_per_submit_key(b) for b in bodies]
        assert len(set(keys)) == 3, "the client must mint a fresh id per submit"

        rows = [_admit(settings_db, k, b) for k, b in zip(keys, bodies)]

        # Admitted means it proceeds to execute. A replay would have raised.
        assert [r["external_ref"] for r in rows] == keys
        assert all(r["principal"] == SESSION for r in rows)

    def test_the_first_and_third_bodies_are_genuinely_identical(self, settings_db):
        """The premise of the test above, asserted rather than assumed. If the
        settings maps differed, the third call would be a new write for an
        ordinary reason and the test would prove nothing about the key."""
        first, third = _body("enhanced"), _body("enhanced")
        assert first["settings"] == third["settings"]
        assert first["submission_id"] != third["submission_id"]

    def test_a_content_hash_DROPS_the_third_edit(self, settings_db):
        """THE POSITIVE CONTROL — F3's rejected option (b), run against the
        real table. The third call carries the first's content, so its key
        collides and admission refuses it as a replay: acknowledged at 200 and
        never executed. This is the invisible failure, made visible."""
        bodies = [
            _hashed_body("enhanced"),
            _hashed_body("simple"),
            _hashed_body("enhanced"),
        ]
        keys = [_content_hash_key(b) for b in bodies]
        assert keys[0] == keys[2], "the control must actually collide"
        # Byte-identical, which is what makes the third a REPLAY rather than a
        # conflict. Asserted because it is the precondition the first version
        # of this test violated without noticing.
        assert bodies[0] == bodies[2]

        _admit(settings_db, keys[0], bodies[0])
        _admit(settings_db, keys[1], bodies[1])

        with pytest.raises(DeliveryReplayed):
            _admit(settings_db, keys[2], bodies[2])

    def test_a_reused_key_with_DIFFERENT_content_is_refused_not_replayed(
        self, settings_db
    ):
        """The server's backstop against a client that holds one id across two
        different edits — a `useState` submission id. It is refused (409)
        rather than silently applied, which is why THAT mistake is loud and the
        content-hash one is not."""
        from src.services.target.webhook_ingress import AdmissionConflict

        key = f"settings_change:{uuid.uuid4()}"
        _admit(settings_db, key, _body("enhanced"))

        with pytest.raises(AdmissionConflict):
            _admit(settings_db, key, _body("simple"))
