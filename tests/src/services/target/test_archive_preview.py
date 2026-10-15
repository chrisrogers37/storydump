"""The archive preview's bounded local decode, without a database or a provider.

Admission and the fence are PostgreSQL's to refuse, so they are proven under
the production roles in `tests/scripts/test_archive_preview_gate.py`; this pins
the bytes a preview may answer with.
"""

from __future__ import annotations

import asyncio
import io
import logging
import threading
import time
from types import SimpleNamespace

import pytest
from asyncpg import exceptions as pg_errors
from PIL import Image
from sqlalchemy.exc import DBAPIError

from src.services.target import archive_preview, egress


def test_preview_subject_selects_the_items_own_asset():
    sql = archive_preview._SUBJECT
    assert "ma.asset_media_id AS selected_asset_id" in sql
    assert "mi.parent_media_id" in sql
    assert "ma.media_item_id IS NOT NULL" in sql
    assert "first_child_id" not in sql


def jpeg(size=(64, 32), **options) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (10, 30, 50)).save(out, format="JPEG", **options)
    return out.getvalue()


def png(size=(8, 8)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size).save(out, format="PNG")
    return out.getvalue()


def with_comment(data: bytes, payload: bytes) -> bytes:
    """*data* with a COM segment after its SOI: a payload the decoder skips."""
    return (
        data[:2]
        + b"\xff\xfe"
        + (len(payload) + 2).to_bytes(2, "big")
        + payload
        + data[2:]
    )


class TestTheBoundedDecode:
    def test_a_complete_jpeg_is_a_preview(self):
        assert archive_preview.preview_jpeg(jpeg()) is True

    @pytest.mark.parametrize(
        "size,admitted",
        [((2048, 8), True), ((2049, 8), False), ((8, 2048), True), ((8, 2049), False)],
    )
    def test_the_longest_edge_is_at_most_2048(self, size, admitted):
        assert archive_preview.PREVIEW_MAX_EDGE == 2048
        assert archive_preview.preview_jpeg(jpeg(size)) is admitted

    @pytest.mark.parametrize("over,admitted", [(0, True), (1, False)])
    def test_the_size_is_at_most_two_mebibytes(self, over, admitted):
        """Bytes after the end-of-image marker are inert to the decoder, so the
        padded file is a complete JPEG of exactly the cap, then one byte over."""
        data = jpeg()
        padded = data + bytes(archive_preview.PREVIEW_MAX_BYTES - len(data) + over)
        assert len(padded) == 2 * 1024 * 1024 + over
        assert archive_preview.preview_jpeg(padded) is admitted

    @pytest.mark.parametrize(
        "data",
        [b"", b"garbage", png(), jpeg()[:-40], b"\xff\xd8\xff" + bytes(64)],
        ids=["empty", "garbage", "png", "truncated", "header-only"],
    )
    def test_anything_but_a_complete_jpeg_refuses(self, data):
        assert archive_preview.preview_jpeg(data) is False

    def test_a_header_claiming_a_huge_picture_refuses_before_decoding(self):
        data = bytearray(jpeg())
        sof = data.find(b"\xff\xc0")
        data[sof + 5 : sof + 9] = (60000).to_bytes(2, "big") * 2
        started = time.monotonic()
        assert archive_preview.preview_jpeg(bytes(data)) is False
        assert time.monotonic() - started < 1, "refused from the header alone"

    def test_a_progressive_jpeg_is_a_preview_and_its_scans_are_bounded(self):
        progressive = jpeg((256, 256), progressive=True)
        assert progressive.count(b"\xff\xda") > 1
        assert archive_preview.preview_jpeg(progressive) is True
        # Scan markers past the bound refuse before any decode, even where the
        # count over-states the scans (here they sit in a skipped comment).
        limit = archive_preview.PREVIEW_MAX_SCANS
        at_limit = with_comment(jpeg(), b"\xff\xda" * (limit - 1))
        assert at_limit.count(b"\xff\xda") == limit
        assert archive_preview.preview_jpeg(at_limit) is True
        over = with_comment(jpeg(), b"\xff\xda" * limit)
        assert archive_preview.preview_jpeg(over) is False

    async def test_a_decode_past_its_deadline_is_no_preview(self, monkeypatch):
        def slow(data):
            time.sleep(0.3)
            return True

        monkeypatch.setattr(archive_preview, "preview_jpeg", slow)
        monkeypatch.setattr(archive_preview, "PREVIEW_DECODE_SECONDS", 0.05)
        assert await archive_preview._decodes(jpeg()) is False
        monkeypatch.setattr(archive_preview, "PREVIEW_DECODE_SECONDS", 5)
        assert await archive_preview._decodes(jpeg()) is True

    async def test_the_decode_runs_off_the_event_loop(self, monkeypatch):
        def off_the_loop(data):
            return threading.current_thread() is not threading.main_thread()

        monkeypatch.setattr(archive_preview, "preview_jpeg", off_the_loop)
        assert await archive_preview._decodes(jpeg()) is True


def a_preview(serve):
    """An `ArchivePreview` whose served part (admission, provider, fence) is
    *serve*, so the cap in front of it is all that runs."""
    port = archive_preview.ArchivePreview(ports=None)
    port._serve = serve
    return port


async def ask(port, *, ws="ws-a", media="m-1"):
    return await port.preview(engine=None, workspace_id=ws, media_id=media, version="v")


async def refused_at_once(port, **kw):
    """What the port answers a request it must refuse. A cap that did not hold
    would let the request in, to wait where the others do, so it is bounded:
    it fails the test instead of hanging it."""
    return await asyncio.wait_for(ask(port, **kw), timeout=5)


class _Held:
    """A served part that waits to be released, and counts who started."""

    def __init__(self, expect):
        self.expect, self.started = expect, []
        self.every_one_started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, engine, ws, mid, version):
        self.started.append((ws, mid))
        if len(self.started) >= self.expect:
            self.every_one_started.set()
        await self.release.wait()
        return b"jpeg"

    async def in_flight(self, port, *requests):
        """Start a preview for each of *requests* (`(ws, media)`) and wait until
        all of them are being served."""
        tasks = [
            asyncio.create_task(ask(port, ws=ws, media=media)) for ws, media in requests
        ]
        await asyncio.wait_for(self.every_one_started.wait(), timeout=5)
        return tasks

    async def finish(self, *tasks):
        self.release.set()
        return await asyncio.gather(*tasks, return_exceptions=True)


class TestTheInFlightCap:
    async def test_a_workspace_holds_two_and_the_third_is_refused_unserved(
        self, caplog
    ):
        held = _Held(expect=2)
        port = a_preview(held)
        tasks = await held.in_flight(port, ("ws-a", "m-1"), ("ws-a", "m-2"))
        try:
            assert port._in_flight == {"ws-a": 2}

            with caplog.at_level(logging.INFO, logger=archive_preview.__name__):
                third = await refused_at_once(port, media="m-3")

            assert third is None
            assert held.started == [("ws-a", "m-1"), ("ws-a", "m-2")], "never served"
            assert [r.getMessage() for r in caplog.records] == [
                "archive preview of media m-3 not served: preview_slots_busy"
            ]
        finally:
            done = await held.finish(*tasks)
        assert done == [b"jpeg", b"jpeg"]

    async def test_the_limit_is_the_constant_and_no_other_number(self, monkeypatch):
        assert archive_preview.PREVIEW_SLOTS_PER_WORKSPACE == 2
        monkeypatch.setattr(archive_preview, "PREVIEW_SLOTS_PER_WORKSPACE", 1)
        held = _Held(expect=1)
        port = a_preview(held)
        tasks = await held.in_flight(port, ("ws-a", "m-1"))
        try:
            assert await refused_at_once(port, media="m-2") is None
        finally:
            done = await held.finish(*tasks)
        assert done == [b"jpeg"]

    async def test_a_slot_is_free_again_once_a_preview_finishes(self):
        held = _Held(expect=2)
        port = a_preview(held)
        tasks = await held.in_flight(port, ("ws-a", "m-1"), ("ws-a", "m-2"))
        try:
            assert await refused_at_once(port, media="m-3") is None
        finally:
            await held.finish(*tasks)

        assert port._in_flight == {}
        # The control: with both finished, the same request is served.
        assert await ask(port, media="m-3") == b"jpeg"

    async def test_another_workspace_has_its_own_slots(self):
        held = _Held(expect=3)
        port = a_preview(held)
        tasks = await held.in_flight(
            port, ("ws-a", "m-1"), ("ws-a", "m-2"), ("ws-b", "m-4")
        )
        try:
            assert port._in_flight == {"ws-a": 2, "ws-b": 1}
            assert await refused_at_once(port, ws="ws-a", media="m-3") is None
            assert ("ws-b", "m-4") in held.started, "not queued behind ws-a"
        finally:
            done = await held.finish(*tasks)
        assert done == [b"jpeg"] * 3
        assert port._in_flight == {}

    async def test_a_preview_that_raises_gives_its_slot_back(self):
        async def broken(engine, ws, mid, version):
            raise RuntimeError("a bug, not a refusal")

        port = a_preview(broken)
        for _ in range(archive_preview.PREVIEW_SLOTS_PER_WORKSPACE + 1):
            with pytest.raises(RuntimeError):
                await refused_at_once(port)
        assert port._in_flight == {}

    async def test_a_cancelled_preview_gives_its_slot_back(self):
        held = _Held(expect=1)
        port = a_preview(held)
        [task] = await held.in_flight(port, ("ws-a", "m-1"))
        try:
            assert port._in_flight == {"ws-a": 1}
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            held.release.set()
        assert port._in_flight == {}


JPEG = jpeg()


def a_port_whose_provider(fetch_preview):
    """An `ArchivePreview` whose admission and fence pass and whose adapter's
    `fetch_preview` is *fetch_preview*."""

    async def prepare(capture, admission_provider):
        return SimpleNamespace(fetch_preview=fetch_preview)

    async def admit(engine, ws, mid, version):
        capture = SimpleNamespace(source_id="source", workspace_id=ws)
        return capture, {"parent_media_id": "1", "selected_asset_id": "1"}

    async def fence(engine, capture, mid, version):
        return None

    port = archive_preview.ArchivePreview(
        archive_preview.ArchivePreviewPorts(capability=None, prepare_adapter=prepare)
    )
    port._admit, port._fence = admit, fence
    return port


def raising(exc):
    async def fetch_preview(*args, **kwargs):
        raise exc

    return fetch_preview


class TestWhatAPreviewAnswersAsThePlaceholder:
    @pytest.mark.parametrize(
        "failure",
        [
            DBAPIError("SELECT 1", {}, pg_errors.LockNotAvailableError("timeout")),
            DBAPIError("SELECT 1", {}, pg_errors.DeadlockDetectedError("deadlock")),
            egress.ResponseTooLarge("over the cap"),
        ],
        ids=["a_lock_timeout", "a_deadlock", "a_reply_over_the_cap"],
    )
    async def test_these_failures_are_no_preview_not_an_error(self, failure):
        assert await ask(a_port_whose_provider(raising(failure))) is None

    async def test_the_control_a_provider_that_answers_is_served(self):
        async def answers(*args, **kwargs):
            return JPEG

        assert await ask(a_port_whose_provider(answers)) == JPEG

    @pytest.mark.parametrize(
        "bug",
        [RuntimeError("a bug"), KeyError("k"), egress.TransactionDisciplineError("x")],
    )
    async def test_an_unexpected_failure_still_fails_loudly(self, bug):
        """The catch is the closed set the page can show a placeholder for, not a
        blanket: a defect is still a 500, and still takes its slot back."""
        port = a_port_whose_provider(raising(bug))
        with pytest.raises(type(bug)):
            await ask(port)
        assert port._in_flight == {}


class TestTheLoggedReason:
    def test_a_closed_reason_is_its_own(self):
        assert archive_preview._reason(archive_preview._Refused("stale_version")) == (
            "stale_version"
        )

    @pytest.mark.parametrize(
        "driver,reason",
        [
            (pg_errors.LockNotAvailableError("timeout"), "database_55P03"),
            (pg_errors.DeadlockDetectedError("deadlock"), "database_40P01"),
            (ConnectionResetError("reset"), "database_error"),
        ],
        ids=["lock_not_available", "deadlock", "no_sqlstate"],
    )
    def test_a_database_failure_is_its_sqlstate_never_its_message(self, driver, reason):
        failure = DBAPIError("SELECT secret_column", {"p": "secret-bind"}, driver)

        named = archive_preview._reason(failure)

        assert named == reason
        assert "secret" not in named

    def test_the_sqlstate_is_found_one_level_below_the_wrapper(self):
        """SQLAlchemy's asyncpg dialect wraps the driver error one level deeper
        than `orig` (`_dbapi.driver_candidates`)."""
        wrapper = Exception("wrapped")
        wrapper.__cause__ = pg_errors.LockNotAvailableError("timeout")

        assert archive_preview._reason(DBAPIError("SELECT 1", {}, wrapper)) == (
            "database_55P03"
        )

    def test_any_other_failure_is_its_class(self):
        assert archive_preview._reason(egress.ResponseTooLarge("x")) == (
            "ResponseTooLarge"
        )
