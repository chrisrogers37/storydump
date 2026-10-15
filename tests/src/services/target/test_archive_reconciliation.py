"""The reconciliation executor's edges that need no database (#1644).

What it writes and reads is the gate's (`tests/scripts/test_archive_reconciliation_gate.py`).
Here: the activation it requires before touching anything, the payload it
accepts, the one constant that can never change once a deploy has used it, and
what step 1 reads, in which order, and when it reads nothing.
Every identifier is synthetic.
"""

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from src.services.target import archive_lifecycle
from src.services.target import archive_reconciliation as ar
from src.services.target.instagram_sources import ArchiveCapability, ArchiveRefused

WS = "11111111-1111-4111-8111-111111111111"
CREDENTIAL = "22222222-2222-4222-8222-222222222222"
ACCOUNT = "33333333-3333-4333-8333-333333333333"


def deps(capability):
    """An engine and a client of None: any step past a refusal would fail on them."""
    return SimpleNamespace(
        engine=None, archive_capability=capability, archive_client=None
    )


def job(payload, workspace_id=WS):
    return {"id": "j-rec", "workspace_id": workspace_id, "payload": payload}


def test_the_namespace_and_the_derived_authorization_id_never_change():
    """A deploy that used another namespace would record a second mapping for
    every credential it had already reconciled: the id is the idempotence."""
    assert str(ar.NS_ARCHIVE) == "3844240d-144e-5b98-8f16-ea91fcefd47b"
    assert ar.NS_ARCHIVE == uuid.uuid5(uuid.NAMESPACE_DNS, "archive.storydump")
    legacy = ar.legacy_authorization_id(WS, CREDENTIAL)
    assert legacy == "65db7ab4-d44b-500b-993a-799c56aaa0fd"
    assert legacy == str(uuid.uuid5(ar.NS_ARCHIVE, f"{WS}:{CREDENTIAL}:legacy"))
    # Another spelling of the same ids derives the same authorization...
    assert ar.legacy_authorization_id(WS.upper(), uuid.UUID(CREDENTIAL)) == legacy
    # ...and another credential, or workspace, another one.
    assert ar.legacy_authorization_id(WS, ACCOUNT) != legacy
    assert ar.legacy_authorization_id(ACCOUNT, CREDENTIAL) != legacy


@pytest.mark.parametrize(
    "capability",
    [None, ArchiveCapability(), ArchiveCapability(enabled=False), object()],
    ids=["none", "default", "off", "not a capability"],
)
async def test_an_archive_that_is_not_activated_is_refused_before_anything_is_touched(
    capability,
):
    with pytest.raises(ArchiveRefused) as refused:
        await ar.execute(deps(capability), job({"v": 1}))
    assert refused.value.reason == "archive_not_activated"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"v": 2},
        {"v": "1"},
        {"v": 1, "cursor": "not-an-account-id"},
        {"v": 1, "cursor": 7},
        ["v", 1],
        "v1",
    ],
    ids=[
        "none",
        "empty",
        "other version",
        "version as text",
        "cursor not an id",
        "cursor a number",
        "list",
        "text",
    ],
)
async def test_a_payload_that_is_not_version_1_with_a_workspace_is_refused(payload):
    with pytest.raises(ar.ReconciliationRefused) as refused:
        await ar.execute(deps(ArchiveCapability(enabled=True)), job(payload))
    assert refused.value.reason == "payload_invalid"


@pytest.mark.parametrize("workspace_id", [None, "not-a-workspace"])
async def test_a_job_that_names_no_workspace_is_refused(workspace_id):
    with pytest.raises(ar.ReconciliationRefused) as refused:
        await ar.execute(
            deps(ArchiveCapability(enabled=True)), job({"v": 1}, workspace_id)
        )
    assert refused.value.reason == "payload_invalid"


def test_a_version_1_payload_names_its_workspace_and_an_optional_cursor():
    """The positive control the refusals above need: the door's own payload and
    the one a continuing turn leaves behind both parse."""
    minted = {"v": 1, "run_id": "synthetic-run", "run_digest": "d" * 64}
    assert ar._subject(job(minted)) == (WS, None)
    assert ar._subject(job({**minted, "cursor": ACCOUNT.upper()})) == (WS, ACCOUNT)


CAPTURE = archive_lifecycle.CredentialCapture(
    CREDENTIAL, ACCOUNT, None, None, None, "9" * 64
)


def scripted_step_one(monkeypatch, *, active, captured, token="synthetic-token"):
    """Step 1's three reads, scripted. Returns what each was asked, in order."""
    asked = []

    @asynccontextmanager
    async def factory():
        yield "session"

    async def row(session, sql, **params):
        asked.append(("guard", sql, params))
        return {"active": 1} if active else None

    async def capture(session, *, workspace_id, credential_id):
        asked.append(("capture", workspace_id, credential_id))
        return captured

    async def load(session, *, credential_id, captured):
        asked.append(("load", credential_id, captured))
        return token

    monkeypatch.setattr(ar, "poller_session_factory", lambda engine, ws: factory)
    monkeypatch.setattr(ar.readers, "row", row)
    monkeypatch.setattr(ar.archive_lifecycle, "capture_credential", capture)
    monkeypatch.setattr(ar.ig_login_oauth, "load_credential", load)
    return asked


async def test_step_one_takes_the_digest_from_the_capture_and_reads_the_token_after_it(
    monkeypatch,
):
    asked = scripted_step_one(monkeypatch, active=True, captured=CAPTURE)

    assert await ar._load(None, WS, ACCOUNT, CREDENTIAL) == (
        CAPTURE.digest,
        "synthetic-token",
    )

    assert [step[0] for step in asked] == ["guard", "capture", "load"]
    assert asked[0][2] == {"ws": WS, "cid": CREDENTIAL, "account": ACCOUNT}
    assert asked[2] == ("load", CREDENTIAL, CAPTURE), "bound to the capture just read"


async def test_step_one_reads_nothing_of_a_credential_that_is_no_longer_active(
    monkeypatch,
):
    asked = scripted_step_one(monkeypatch, active=False, captured=CAPTURE)

    assert await ar._load(None, WS, ACCOUNT, CREDENTIAL) is None

    assert [step[0] for step in asked] == ["guard"], "no capture, and no token"


async def test_step_one_reads_no_token_when_the_capture_has_gone(monkeypatch):
    asked = scripted_step_one(monkeypatch, active=True, captured=None)

    assert await ar._load(None, WS, ACCOUNT, CREDENTIAL) is None

    assert [step[0] for step in asked] == ["guard", "capture"], "no token"
