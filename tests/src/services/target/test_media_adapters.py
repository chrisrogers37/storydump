"""The registry dispatches reads by source, never by a default provider."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
import traceback

from src import worker
from src.channels.telegram_transport import MediaTransient, MediaUnavailable
from src.services.target import media_adapters, unit_of_work
from src.services.target.drive_adapter import (
    DriveLostResponse,
    DriveRetryableError,
    DriveTerminalError,
)


class BytesAdapter:
    def __init__(self, *, before_fetch=None, error=None):
        self.calls = []
        self.before_fetch = before_fetch
        self.error = error

    async def fetch_bytes(self, config, file_ref, **kwargs):
        if self.before_fetch:
            self.before_fetch()
        self.calls.append((config, file_ref, kwargs))
        if self.error:
            raise self.error
        return b"archive", "story.jpg", "image/jpeg"


def _source_factory(monkeypatch, row):
    """Make holding a tenant session across a provider call observable."""
    observed = {"open": False, "tenants": [], "statements": []}

    class Session:
        async def execute(self, statement, values):
            observed["statements"].append((str(statement), values))
            return SimpleNamespace(mappings=lambda: SimpleNamespace(first=lambda: row))

    @asynccontextmanager
    async def session():
        observed["open"] = True
        try:
            yield Session()
        finally:
            observed["open"] = False

    def factory(engine, workspace_id):
        observed["tenants"].append(workspace_id)
        return session

    monkeypatch.setattr(unit_of_work, "poller_session_factory", factory)
    return observed


def _card():
    # The exact old queued-card shape: no provider or config copied to outbox.
    return {
        "workspace_id": "ws-a",
        "source_id": "src-a",
        "ref": "opaque:42",
        "kind": "image",
    }


def test_an_unknown_provider_never_falls_back_to_drive():
    with pytest.raises(media_adapters.ProviderNotWired) as error:
        media_adapters.get_adapter({"gdrive": object()}, "not-installed")
    assert error.value.provider == "not-installed"
    assert isinstance(error.value, media_adapters.MediaRetryableError)


@pytest.mark.parametrize(
    "drive_type, neutral_type",
    [
        (DriveTerminalError, media_adapters.MediaTerminalError),
        (DriveRetryableError, media_adapters.MediaRetryableError),
        (DriveLostResponse, media_adapters.MediaLostResponse),
    ],
)
def test_existing_drive_error_types_keep_their_neutral_routing(
    drive_type, neutral_type
):
    assert isinstance(drive_type("fault"), neutral_type)


async def test_both_byte_paths_select_the_second_provider_and_pass_its_config(
    monkeypatch,
):
    config = {"v": 9, "bucket": "archive-a"}
    observed = _source_factory(
        monkeypatch, {"provider": "fake_archive", "config": config}
    )

    def outside_transaction():
        assert not observed["open"]

    adapter = BytesAdapter(before_fetch=outside_transaction)
    adapters = {"gdrive": object(), "fake_archive": adapter}
    fetch = worker._publish_media_fetch(adapters)
    assert (
        await fetch(
            {
                "provider": "fake_archive",
                "source_config": config,
                "source_state": "active",
                "source_id": "src-a",
                "workspace_id": "ws-a",
                "provider_file_ref": "opaque:42",
                "media_kind": "video",
            }
        )
        == b"archive"
    )
    assert await worker._card_media_fetch(object(), adapters)(_card()) == (
        b"archive",
        "story.jpg",
        "image/jpeg",
    )
    assert [call[:2] for call in adapter.calls] == [(config, "opaque:42")] * 2
    assert [call[2]["max_bytes"] for call in adapter.calls] == [
        worker.PUBLISH_MAX_BYTES["video"],
        10 * 1024 * 1024,
    ]
    assert observed["tenants"] == ["ws-a"]
    sql, params = observed["statements"][0]
    assert "id = :source AND workspace_id = :workspace" in sql
    assert "state = 'active'" in sql
    assert params == {"source": "src-a", "workspace": "ws-a"}


@pytest.mark.parametrize("source", [None, {"provider": "not-wired", "config": {}}])
async def test_a_missing_or_unwired_card_source_is_unavailable(monkeypatch, source):
    _source_factory(monkeypatch, source)
    with pytest.raises(MediaUnavailable):
        await worker._card_media_fetch(object(), {"gdrive": object()})(_card())


@pytest.mark.parametrize(
    "state, provider",
    [
        (None, "gdrive"),
        (None, "fake_archive"),
        ("paused", "fake_archive"),
        ("error", "fake_archive"),
    ],
)
async def test_publish_refuses_missing_or_inactive_source(state, provider):
    adapters = {"gdrive": BytesAdapter(), "fake_archive": BytesAdapter()}
    with pytest.raises(media_adapters.MediaTerminalError):
        await worker._publish_media_fetch(adapters)(
            {"source_state": state, "provider": provider}
        )
    assert [a.calls for a in adapters.values()] == [[], []]


@pytest.mark.parametrize("state", ["paused", "error"])
async def test_an_inactive_drive_folder_still_reaches_its_credential_door(state):
    """A disconnected folder is paused, and its approved story's fetch still
    goes to the Drive adapter, whose credential door decides: a dead grant
    retries and parks for review, never a terminal failure here (#1477)."""
    drive = BytesAdapter()
    fetch = worker._publish_media_fetch({"gdrive": drive})
    intent = {
        "provider": "gdrive",
        "source_state": state,
        "source_config": {"folder_ref": "folder"},
        "source_id": "src-d",
        "workspace_id": "ws-d",
        "provider_file_ref": "file",
        "media_kind": "image",
    }
    assert await fetch(intent) == b"archive"
    assert [call[:2] for call in drive.calls] == [({"folder_ref": "folder"}, "file")]


async def test_an_unwired_publish_provider_is_terminal():
    with pytest.raises(media_adapters.MediaTerminalError):
        await worker._publish_media_fetch({"gdrive": object()})(
            {"source_state": "active", "provider": "not-wired"}
        )


@pytest.mark.parametrize(
    "fault, expected",
    [
        (media_adapters.MediaTerminalError("gone"), MediaUnavailable),
        (media_adapters.MediaRetryableError("busy"), MediaTransient),
        (media_adapters.MediaLostResponse("lost"), MediaTransient),
    ],
)
async def test_card_errors_use_provider_neutral_types(monkeypatch, fault, expected):
    _source_factory(monkeypatch, {"provider": "fake_archive", "config": {"v": 1}})
    with pytest.raises(expected):
        await worker._card_media_fetch(
            object(), {"fake_archive": BytesAdapter(error=fault)}
        )(_card())


@pytest.mark.parametrize(
    "error_type, expected",
    [
        (media_adapters.MediaTerminalError, MediaUnavailable),
        (media_adapters.MediaRetryableError, MediaTransient),
        (media_adapters.MediaLostResponse, MediaTransient),
    ],
)
async def test_card_error_wrappers_do_not_expose_provider_exception_text(
    monkeypatch, error_type, expected
):
    _source_factory(monkeypatch, {"provider": "fake_archive", "config": {"v": 1}})
    secret = "https://provider.invalid/media?access_token=private-provider-secret"
    fetch = worker._card_media_fetch(
        object(),
        {
            "fake_archive": BytesAdapter(error=error_type(secret)),
        },
    )
    with pytest.raises(expected) as caught:
        await fetch(_card())
    assert "private-provider-secret" not in str(caught.value)
    assert "access_token" not in str(caught.value)
    assert "provider.invalid" not in str(caught.value)
    assert caught.value.__suppress_context__


@pytest.mark.parametrize(
    "error_type", [media_adapters.MediaCredentialDead, media_adapters.MediaSourceGone]
)
async def test_card_connection_failures_stay_counted_without_provider_text(
    monkeypatch, caplog, error_type
):
    from src.channels.telegram_transport import TelegramTransport

    _source_factory(monkeypatch, {"provider": "fake_archive", "config": {"v": 1}})
    secret = "https://provider.invalid/media?access_token=private-provider-secret"
    fetch = worker._card_media_fetch(
        object(), {"fake_archive": BytesAdapter(error=error_type(secret))}
    )
    transport = TelegramTransport(
        "test-token", client=SimpleNamespace(), media_fetch=fetch
    )
    assert await transport._fetch_media({"id": "queued-card"}, _card()) is None
    assert transport.media_fetch_failures == 1
    assert error_type.__name__ in caplog.text
    assert "private-provider-secret" not in caplog.text
    assert "provider.invalid" not in caplog.text


@pytest.mark.parametrize(
    "error_type",
    [
        media_adapters.MediaTerminalError,
        media_adapters.MediaRetryableError,
        media_adapters.MediaLostResponse,
        media_adapters.MediaCredentialDead,
        media_adapters.MediaSourceGone,
    ],
)
def test_publish_error_record_keeps_only_media_error_class(error_type):
    from src.services.target.publish_pipeline import _error_of

    fault = error_type(
        "https://provider.invalid/media?access_token=private-provider-secret"
    )
    fault.code = "private-provider-secret"
    fault.detail = {"fbtrace_id": "private-provider-secret"}
    assert _error_of(fault) == {"v": 1, "error": {"type": error_type.__name__}}


@pytest.mark.parametrize(
    "error_type",
    [
        media_adapters.MediaTerminalError,
        media_adapters.MediaRetryableError,
        media_adapters.MediaLostResponse,
        media_adapters.MediaCredentialDead,
        media_adapters.MediaSourceGone,
        media_adapters.MediaError,
    ],
)
async def test_publish_fetch_boundary_hides_secrets_from_later_sql_failure(error_type):
    secret = "https://provider.invalid/media?access_token=private-provider-secret"
    fetch = worker._publish_media_fetch(
        {"fake_archive": BytesAdapter(error=error_type(secret))}
    )
    intent = {
        "source_state": "active",
        "provider": "fake_archive",
        "source_config": {"v": 1},
        "source_id": "source-a",
        "workspace_id": "workspace-a",
        "provider_file_ref": "file-a",
    }
    with pytest.raises(RuntimeError) as nested:
        try:
            await fetch(intent)
        except error_type:
            raise RuntimeError("database unavailable")
    rendered = "".join(traceback.format_exception(nested.value))
    assert error_type.__name__ in rendered
    assert "database unavailable" in rendered
    assert "private-provider-secret" not in rendered
    assert "provider.invalid" not in rendered


async def test_archive_card_fetch_uses_server_intent_bound_preparation(monkeypatch):
    from types import SimpleNamespace
    from src.services.target import source_policy
    from src.services.target.outbox import MediaAdmissionNotSent

    _source_factory(monkeypatch, {"provider": "instagram_archive", "config": {"v": 1}})
    calls = []

    async def selected(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(content=b"verified JPEG", media_kind="image")

    media = {**_card(), "intent_id": "server-intent"}
    result = await worker._card_media_fetch(object(), {}, fetch_selected=selected)(
        media
    )
    assert result == (b"verified JPEG", "story.jpg", "image/jpeg")
    assert calls[0]["intent_id"] == "server-intent"

    async def denied(**kwargs):
        raise source_policy.MediaAdmissionPending("selected_asset_changed")

    with pytest.raises(MediaAdmissionNotSent):
        await worker._card_media_fetch(object(), {}, fetch_selected=denied)(media)
    with pytest.raises(MediaAdmissionNotSent):
        await worker._card_media_fetch(object(), {}, fetch_selected=selected)(_card())


@pytest.mark.parametrize(
    "failure",
    [
        media_adapters.MediaCredentialDead,
        media_adapters.MediaSourceGone,
        media_adapters.MediaLostResponse,
    ],
)
async def test_archive_source_failure_never_uses_drive_text_degradation(
    monkeypatch, failure
):
    from src.services.target.outbox import MediaAdmissionNotSent

    _source_factory(monkeypatch, {"provider": "instagram_archive", "config": {"v": 1}})

    async def selected(**kwargs):
        raise failure("synthetic provider failure")

    with pytest.raises(MediaAdmissionNotSent):
        await worker._card_media_fetch(object(), {}, fetch_selected=selected)(
            {**_card(), "intent_id": "server-intent"}
        )


async def test_missing_archive_source_is_a_no_send_refusal(monkeypatch):
    from src.services.target.outbox import MediaAdmissionNotSent

    _source_factory(monkeypatch, None)
    with pytest.raises(MediaAdmissionNotSent):
        await worker._card_media_fetch(object(), {})(
            {**_card(), "intent_id": "server-intent"}
        )
