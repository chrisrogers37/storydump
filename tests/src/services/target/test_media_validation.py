"""Bounded local validation: no provider, database, or production activation."""

import asyncio
import hashlib
import io
import json
import struct
import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PIL import Image

from src.services.target import media_validation as v
from src.services.target import refusal_evidence


def image_bytes(fmt="JPEG", size=(32, 48)):
    out = io.BytesIO()
    Image.new("RGB", size, (10, 30, 50)).save(out, format=fmt)
    return out.getvalue()


def request(data, kind="image", **changes):
    return v.ValidationInput(
        data, kind, "generation-one", 7, "selected-child", **changes
    )


def box(kind, payload=b"", *, extended=False):
    if extended:
        return struct.pack(">I4sQ", 1, kind, 16 + len(payload)) + payload
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def movie(
    *, external=False, edits=False, late=False, fragmented=False, codecs=(b"avc1",)
):
    ref = box(
        b"url ",
        b"\x00\x00\x00" + (b"\x00../private-secret\x00" if external else b"\x01"),
    )
    dref = box(b"dref", bytes(4) + struct.pack(">I", 1) + ref)
    stsd = box(
        b"stsd",
        bytes(4)
        + struct.pack(">I", len(codecs))
        + b"".join(box(codec, bytes(6) + b"\x00\x01" + bytes(78)) for codec in codecs),
    )
    track = box(
        b"trak",
        box(b"mdia", box(b"minf", box(b"dinf", dref) + box(b"stbl", stsd)))
        + (box(b"edts", box(b"elst", bytes(8))) if edits else b""),
    )
    moov = box(b"moov", track)
    mdat = box(b"mdat", bytes(8))
    return (
        box(b"ftyp", b"isom" + bytes(4))
        + (mdat + moov if late else moov + mdat)
        + (box(b"moof") if fragmented else b"")
    )


async def test_real_jpeg_decodes_with_generation_bound_evidence():
    source = request(image_bytes())
    result = await v.validate_media(source)
    assert result.state == "supported"
    assert result.evidence["width"] == 32
    assert result.evidence["height"] == 48
    assert result.source_generation == source.source_generation
    assert result.observation_revision == 7
    assert result.selected_asset_id == "selected-child"
    assert (
        result.fingerprint
        != (await v.validate_media(replace(source, observation_revision=8))).fingerprint
    )
    assert result.evidence["color"] == "provider_conversion_allowed"


@pytest.mark.parametrize(
    "data,reason",
    [
        (b"garbage", "malformed_image"),
        (image_bytes("PNG"), "image_format"),
        (image_bytes()[:-100], "malformed_image"),
    ],
)
async def test_actual_format_and_complete_pixels_required(data, reason):
    result = await v.validate_media(request(data))
    assert (result.state, result.reason) == ("unsupported", reason)


async def test_image_pixel_budget_is_pending_and_cleanup_is_owned(tmp_path):
    limits = v.ValidationLimits(pixels=100, temporary_root=str(tmp_path))
    result = await v.validate_media(request(image_bytes(size=(32, 48))), limits=limits)
    assert (result.state, result.reason) == ("validation_pending", "pixel_budget")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("kind,cap", [("image", 8_000_000), ("video", 40_000_000)])
async def test_exact_byte_ceiling_cannot_be_relaxed(kind, cap):
    assert v.SUPPORTED_MEDIA_MATRIX["byte_caps"][kind] == cap
    result = await v.validate_media(request(bytes(cap + 1), kind))
    assert (result.state, result.reason) == ("unsupported", "byte_limit")
    with pytest.raises(ValueError):
        v.ValidationLimits(wall_seconds=31)


@pytest.mark.parametrize(
    "kwargs,reason,state",
    [
        ({"external": True}, "external_reference", "unsupported"),
        ({"fragmented": True}, "container_evidence_incomplete", "validation_pending"),
    ],
)
async def test_container_preflight_before_tools(kwargs, reason, state, tmp_path):
    result = await v.validate_media(
        request(movie(**kwargs), "video"),
        limits=v.ValidationLimits(
            ffprobe="/missing/tool", temporary_root=str(tmp_path)
        ),
    )
    assert (result.state, result.reason) == (state, reason)
    assert list(tmp_path.iterdir()) == []
    assert "private-secret" not in repr(result)


@pytest.mark.parametrize("kwargs", [{}, {"edits": True}, {"late": True}])
async def test_missing_tools_never_proves_unsupported(kwargs, tmp_path):
    """The source's edit list and `moov` position are the story derivation's,
    not judged: such a container passes the walker and the probe decides."""
    assert v.inspect_container(movie(**kwargs)).reason is None
    result = await v.validate_media(
        request(movie(**kwargs), "video"),
        limits=v.ValidationLimits(
            ffprobe="/missing/tool", temporary_root=str(tmp_path)
        ),
    )
    assert (result.state, result.reason) == ("validation_pending", "tool_unavailable")
    assert "/missing/tool" not in repr(result)
    assert list(tmp_path.iterdir()) == []


def test_container_parser_uses_atom_bounds_not_payload_search():
    checked = v.inspect_container(movie() + box(b"free", b"elst moov"))
    assert checked.reason is None
    assert v.inspect_container(movie()[:-1]).reason == "malformed_container"
    assert (
        v.inspect_container(box(b"free", b"payload", extended=True) + movie()).reason
        is None
    )
    assert (
        v.inspect_container(struct.pack(">I4sQ", 1, b"moov", 2**64 - 1)).reason
        == "malformed_container"
    )


@pytest.mark.parametrize(
    "code,reason",
    [
        ("import time;time.sleep(10)", "deadline"),
        ('import sys;sys.stdout.write("x"*10000)', "output_budget"),
    ],
)
async def test_owned_process_deadline_and_running_output_cap(code, reason, tmp_path):
    limits = v.ValidationLimits(
        wall_seconds=0.1, output_bytes=1000, temporary_root=str(tmp_path)
    )
    with pytest.raises(v.ValidationPending, match=reason):
        await v.run_bounded(
            [sys.executable, "-c", code],
            limits,
            str(tmp_path),
            asyncio.get_running_loop().time() + limits.wall_seconds,
        )


async def test_cancellation_kills_reaps_owned_process_and_removes_temp(tmp_path):
    # A real sleeping executable, injected only at the tool seam.
    executable = tmp_path / "sleep-tool"
    executable.write_text("#!/bin/sh\nexec sleep 30\n")
    executable.chmod(0o700)
    task = asyncio.create_task(
        v.validate_media(
            request(movie(), "video"),
            limits=v.ValidationLimits(
                ffprobe=str(executable), temporary_root=str(tmp_path)
            ),
        )
    )
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(tmp_path.iterdir()) == [executable]


def probe_facts(*, duration="10.000000", fps="30/1", format_duration=None, video=True):
    """ffprobe's JSON for one file, in the entries the validator asks for."""
    streams = [{"codec_type": "audio", "codec_name": "aac", "duration": duration}]
    if video:
        streams.insert(
            0,
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 720,
                "height": 1280,
                "avg_frame_rate": fps,
                "duration": duration,
            },
        )
    return {
        "streams": streams,
        "format": {} if format_duration is None else {"duration": format_duration},
    }


@pytest.mark.parametrize(
    "duration,text,trimmed",
    [
        ("2.999", None, None),
        ("3", "3", None),
        ("60", "60", None),
        ("60.001", "60001/1000", "60"),
    ],
)
def test_exact_video_duration_boundaries(duration, text, trimmed):
    """Under the shortest story is refused; over the longest is kept, and its
    derivation trims it."""
    if text is None:
        with pytest.raises(v.UnsupportedMedia, match="^duration$"):
            v.inspect_video_facts(probe_facts(duration=duration))
    else:
        facts = v.inspect_video_facts(probe_facts(duration=duration))
        assert (facts["duration_seconds"], facts["trimmed_to_seconds"]) == (
            text,
            trimmed,
        )


def test_a_video_records_the_encoding_its_derivation_is_sent():
    assert v.inspect_video_facts(probe_facts())["derivation"] == {
        "video_codec": ["h264", "high", "4.2"],
        "audio_codec": "aac",
        "audio_hz": 48_000,
        "fps": [23, 60],
        "video_bits_per_second": 13_000_000,
        "longest_seconds": 60,
    }


def test_the_bit_rate_cap_honours_both_of_metas_limits():
    """Meta's VBR maximum, and its file size at the longest story with the
    audio's share."""
    matrix = v.SUPPORTED_MEDIA_MATRIX
    cap = v.story_video_encoding()["video_bits_per_second"]
    longest = matrix["duration_seconds"][1]
    audio = matrix["provider_audio_bits_per_second"]
    assert cap <= matrix["provider_video_bits_per_second"]
    assert (cap + audio) * longest <= matrix["provider_video_bytes"] * 8


@pytest.mark.parametrize(
    "fps,judged",
    [
        ("22/1", None),
        ("23/1", "23"),
        ("30000/1001", "30000/1001"),
        ("60/1", "60"),
        ("61/1", None),
    ],
)
def test_a_stated_frame_rate_outside_the_matrix_is_refused(fps, judged):
    if judged is None:
        with pytest.raises(v.UnsupportedMedia, match="^fps$"):
            v.inspect_video_facts(probe_facts(fps=fps))
    else:
        assert v.inspect_video_facts(probe_facts(fps=fps))["fps"] == judged


def test_a_frame_rate_the_probe_does_not_state_is_not_judged():
    assert v.inspect_video_facts(probe_facts(fps="0/0"))["fps"] == "unstated"


def test_the_duration_falls_back_to_the_containers_and_none_is_pending():
    facts = probe_facts(duration=None, format_duration="12.5")
    assert v.inspect_video_facts(facts)["duration_seconds"] == "25/2"
    with pytest.raises(v.ValidationPending, match="^timing_evidence_incomplete$"):
        v.inspect_video_facts(probe_facts(duration=None))


def test_a_file_with_no_video_stream_is_refused():
    with pytest.raises(v.UnsupportedMedia, match="^stream_topology$"):
        v.inspect_video_facts(probe_facts(video=False))


def test_what_the_story_derivation_sets_is_not_judged():
    """The source's codec, frame size and further tracks are the derivation's to
    settle: the first video stream is judged on its duration and rate only."""
    facts = probe_facts()
    facts["streams"][0].update(codec_name="hevc", width=3840, height=2160)
    facts["streams"].append(
        {"codec_type": "video", "avg_frame_rate": "120/1", "duration": "10"}
    )
    evidence = v.inspect_video_facts(facts)
    assert (evidence["codec"], evidence["width"], evidence["fps"]) == (
        "hevc",
        3840,
        "30",
    )
    assert evidence["audio"] is True


def encoded_video(tmp_path, *, seconds=4, rate=30, faststart=False):
    """An H.264 + AAC MP4 written with ffmpeg's defaults, which put an edit
    list on each track and `moov` after the media (before it, with
    *faststart*)."""
    _requires_linux()
    out = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=64x64:rate={rate}",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            str(seconds),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            *(["-movflags", "+faststart"] if faststart else []),
            str(out),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    return out.read_bytes()


async def test_a_real_video_with_edit_lists_and_a_late_moov_is_supported(tmp_path):
    data = encoded_video(tmp_path)
    assert b"elst" in data and data.find(b"mdat") < data.find(b"moov"), (
        "the stimulus carries an edit list and a late moov"
    )
    result = await v.validate_media(
        request(data, "video"),
        limits=v.ValidationLimits(temporary_root=str(tmp_path)),
    )
    assert (result.state, result.reason) == ("supported", "validated")
    assert (result.evidence["codec"], result.evidence["fps"]) == ("h264", "30")


@pytest.mark.parametrize("seconds,rate,reason", [(2, 30, "duration"), (4, 120, "fps")])
async def test_a_real_video_outside_the_matrix_is_refused(
    tmp_path, seconds, rate, reason
):
    result = await v.validate_media(
        request(encoded_video(tmp_path, seconds=seconds, rate=rate), "video"),
        limits=v.ValidationLimits(temporary_root=str(tmp_path)),
    )
    assert (result.state, result.reason) == ("unsupported", reason)


def test_linux_probe_tools_are_required_in_ci():
    import shutil

    if sys.platform == "linux":
        assert shutil.which("ffprobe"), (
            "ffprobe must be installed; do not skip validation coverage"
        )
        assert shutil.which("ffmpeg"), (
            "ffmpeg must be installed; do not skip validation coverage"
        )


async def test_evidence_is_immutable_and_raw_bytes_are_not_retained():
    result = await v.validate_media(request(image_bytes()))
    with pytest.raises(TypeError):
        result.evidence["width"] = 99
    assert "data=" not in repr(result)
    assert "media-validation-" not in repr(result)
    assert "sha256" in result.evidence


def _requires_linux():
    """Off Linux, `run_bounded` refuses before it creates a process, so a test
    that waits for its process to be created would wait forever."""
    if sys.platform != "linux":
        pytest.fail(
            "the selected-media validator runs only on Linux"
            " (`media_validation.run_bounded`): run this test on the Linux lane",
            pytrace=False,
        )


async def test_cancellation_during_process_creation_still_reaps_group(
    monkeypatch, tmp_path
):
    import os
    import signal

    _requires_linux()
    original = asyncio.create_subprocess_exec
    spawned = []
    ready = asyncio.Event()

    async def delayed(*args, **kwargs):
        process = await original(*args, **kwargs)
        spawned.append(process)
        ready.set()
        await asyncio.sleep(0.1)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed)
    limits = v.ValidationLimits()
    task = asyncio.create_task(
        v.run_bounded(
            [sys.executable, "-c", "import time;time.sleep(30)"],
            limits,
            str(tmp_path),
            asyncio.get_running_loop().time() + 30,
        )
    )
    await ready.wait()
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
        assert spawned[0].returncode is not None, (
            "creation cancellation leaked the owned group"
        )
    finally:
        if spawned[0].returncode is None:
            os.killpg(spawned[0].pid, signal.SIGKILL)
            await spawned[0].wait()


async def test_malformed_tool_json_and_timeout_clean_owned_temporary_files(tmp_path):
    executable = tmp_path / "fixture-tool"
    executable.write_text("#!" + sys.executable + '\nprint("{")\n')
    executable.chmod(0o700)
    result = await v.validate_media(
        request(movie(), "video"),
        limits=v.ValidationLimits(
            ffprobe=str(executable),
            temporary_root=str(tmp_path),
        ),
    )
    assert (result.state, result.reason) == (
        "validation_pending",
        "incomplete_evidence",
    )
    assert list(tmp_path.iterdir()) == [executable]
    executable.write_text("#!/bin/sh\nexec sleep 30\n")
    result = await v.validate_media(
        request(movie(), "video"),
        limits=v.ValidationLimits(
            ffprobe=str(executable), temporary_root=str(tmp_path), wall_seconds=0.1
        ),
    )
    assert (result.state, result.reason) == ("validation_pending", "deadline")
    assert list(tmp_path.iterdir()) == [executable]


async def test_unexpected_validator_fault_removes_owned_temporary_file(
    monkeypatch, tmp_path
):

    async def fault(*args):
        raise RuntimeError("synthetic implementation fault")

    monkeypatch.setattr(v, "run_bounded", fault)
    with pytest.raises(RuntimeError, match="synthetic implementation fault"):
        await v.validate_media(
            request(image_bytes()),
            limits=v.ValidationLimits(temporary_root=str(tmp_path)),
        )
    assert list(tmp_path.iterdir()) == []


def test_container_depth_count_and_unknown_reference_are_pending():
    assert (
        v.inspect_container(movie(), v.ValidationLimits(boxes=2)).reason
        == "container_budget"
    )
    assert (
        v.inspect_container(movie(), v.ValidationLimits(depth=2)).reason
        == "container_budget"
    )
    altered = movie().replace(b"url ", b"ZZZZ")
    assert v.inspect_container(altered).reason == "container_evidence_incomplete"


@pytest.mark.parametrize(
    "field", ["source_generation", "observation_revision", "selected_asset_id"]
)
async def test_evidence_cannot_relabel_another_captured_identity(field):
    source = request(image_bytes())
    change = {
        "source_generation": "generation-two",
        "observation_revision": 8,
        "selected_asset_id": "another-child",
    }
    one = await v.validate_media(source)
    two = await v.validate_media(replace(source, **{field: change[field]}))
    assert getattr(one, field) != getattr(two, field)
    assert one.fingerprint != two.fingerprint


@pytest.mark.parametrize(
    "changes",
    [
        {"observation_revision": True},
        {"observation_revision": 1.5},
        {"source_generation": "https://provider.example/secret"},
        {"selected_asset_id": "https://provider.example/signed-secret"},
    ],
)
def test_captured_identity_requires_bounded_ids_and_integer_revision(changes):
    with pytest.raises(ValueError, match="captured observation identity required"):
        replace(request(image_bytes()), **changes)


async def test_repeated_creation_cancellation_reaps_before_temp_cleanup(
    monkeypatch, tmp_path
):
    import os
    import signal

    _requires_linux()
    original = asyncio.create_subprocess_exec
    ready = asyncio.Event()
    spawned = []
    creation_finished = []
    executable = tmp_path / "fixture-sleep-tool"
    executable.write_text("#!/bin/sh\nexec sleep 30\n")
    executable.chmod(0o700)

    async def delayed(*args, **kwargs):
        process = await original(*args, **kwargs)
        spawned.append(process)
        ready.set()
        await asyncio.sleep(0.2)
        assert __import__("pathlib").Path(kwargs["cwd"]).exists()
        creation_finished.append(True)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed)
    cancellation_seen = []

    async def operation():
        try:
            await v.validate_media(
                request(movie(), "video"),
                limits=v.ValidationLimits(
                    ffprobe=str(executable), temporary_root=str(tmp_path)
                ),
            )
        except asyncio.CancelledError as exc:
            cancellation_seen.append(exc.args)
            raise

    task = asyncio.create_task(operation())
    await ready.wait()
    task.cancel("first-cancellation")
    await asyncio.sleep(0.02)
    task.cancel("second-cancellation")
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
        assert creation_finished == [True], "repeated cancellation abandoned creation"
        assert spawned[0].returncode is not None, (
            "repeated cancellation leaked the owned child"
        )
        with pytest.raises(ProcessLookupError):
            os.killpg(spawned[0].pid, 0)
        assert list(tmp_path.iterdir()) == [executable]
        assert cancellation_seen == [("first-cancellation",)]
    finally:
        if spawned[0].returncode is None:
            os.killpg(spawned[0].pid, signal.SIGKILL)
            await spawned[0].communicate()


async def test_repeated_cancellation_during_finally_finishes_reaping(
    monkeypatch, tmp_path
):
    _requires_linux()
    original = asyncio.create_subprocess_exec
    ready = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_finished = []
    spawned = []

    async def capture(*args, **kwargs):
        process = await original(*args, **kwargs)
        spawned.append(process)
        wait = process.wait
        calls = 0

        async def delayed_wait():
            nonlocal calls
            calls += 1
            if calls >= 2:
                cleanup_started.set()
                await asyncio.sleep(0.2)
                assert __import__("pathlib").Path(kwargs["cwd"]).exists()
                cleanup_finished.append(True)
            return await wait()

        process.wait = delayed_wait
        ready.set()
        return process

    executable = tmp_path / "fixture-sleep-tool"
    executable.write_text("#!/bin/sh\nexec sleep 30\n")
    executable.chmod(0o700)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    cancellation_seen = []

    async def operation():
        try:
            await v.validate_media(
                request(movie(), "video"),
                limits=v.ValidationLimits(
                    ffprobe=str(executable), temporary_root=str(tmp_path)
                ),
            )
        except asyncio.CancelledError as exc:
            cancellation_seen.append(exc.args)
            raise

    task = asyncio.create_task(operation())
    await ready.wait()
    await asyncio.sleep(0.02)
    task.cancel("first-cancellation")
    await cleanup_started.wait()
    task.cancel("second-cancellation")
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleanup_finished == [True], "repeated cancellation interrupted final reaping"
    assert spawned[0].returncode is not None
    assert list(tmp_path.iterdir()) == [executable]
    assert cancellation_seen == [("first-cancellation",)]


def refusal_of(error, facts):
    """The `error` that `inspect_video_facts` raises for *facts*."""
    with pytest.raises(error) as raised:
        v.inspect_video_facts(facts)
    return raised.value


def assert_plain(value):
    """`value` is what the JSON record keeps: None, a boolean, a whole number,
    text of at most 64 characters, a list of at most 32 of those, or a mapping
    of those keyed by text. Bytes are none of them."""
    if isinstance(value, dict):
        for key, item in value.items():
            assert isinstance(key, str), key
            assert_plain(item)
    elif isinstance(value, list):
        assert len(value) <= 32
        for item in value:
            assert_plain(item)
    elif isinstance(value, str):
        assert len(value) <= 64
    else:
        assert value is None or type(value) in (bool, int), repr(value)


#: Every refusal `inspect_video_facts` makes: the error, the check, the
#: probe's facts, and the values it measured.
VIDEO_REFUSALS = [
    pytest.param(
        v.UnsupportedMedia,
        "stream_topology",
        probe_facts(video=False),
        {"streams": 1, "video_streams": 0},
        id="stream_topology",
    ),
    pytest.param(
        v.UnsupportedMedia,
        "duration",
        probe_facts(duration="2.5"),
        {"duration_seconds": "5/2", "fps": "30"},
        id="duration",
    ),
    pytest.param(
        v.UnsupportedMedia,
        "fps",
        probe_facts(fps="120/1"),
        {"duration_seconds": "10", "fps": "120"},
        id="fps",
    ),
]


@pytest.mark.parametrize("error,check,facts,measured", VIDEO_REFUSALS)
def test_video_check_carries_the_values_it_read(error, check, facts, measured):
    """A video check refuses with the values it read as `exc.measured`: whole
    numbers stay numbers and a duration or a rate is its exact text."""
    raised = refusal_of(error, facts)
    assert str(raised) == check
    assert raised.measured == measured
    assert_plain(raised.measured)


#: Every refusal `inspect_container` makes: the bytes, the limits (None for the
#: defaults), the reason, whether it is pending, and the values it measured.
CONTAINER_REFUSALS = [
    pytest.param(
        movie(codecs=(b"vp09",)),
        None,
        "video_codec",
        False,
        {
            "boxes": ["ftyp", "moov", "mdat"],
            "box_count": 12,
            "sample_codecs": ["vp09"],
            "refused_codecs": ["vp09"],
        },
        id="video_codec",
    ),
    pytest.param(
        movie(codecs=(b"avc1", b"ac-3")),
        None,
        "video_codec",
        False,
        {
            "boxes": ["ftyp", "moov", "mdat"],
            "box_count": 13,
            "sample_codecs": ["avc1", "ac-3"],
            "refused_codecs": ["ac-3"],
        },
        id="video_codec-names-only-the-refused-stream",
    ),
    pytest.param(
        movie()[:-1],
        None,
        "malformed_container",
        False,
        {"boxes": ["ftyp", "moov"], "box_count": 12, "sample_codecs": ["avc1"]},
        id="malformed_container",
    ),
    pytest.param(
        movie(external=True),
        None,
        "external_reference",
        False,
        {"boxes": ["ftyp", "moov"], "box_count": 8, "sample_codecs": []},
        id="external_reference",
    ),
    pytest.param(
        movie(fragmented=True),
        None,
        "container_evidence_incomplete",
        True,
        {
            "boxes": ["ftyp", "moov", "mdat", "moof"],
            "box_count": 13,
            "sample_codecs": ["avc1"],
        },
        id="container_evidence_incomplete",
    ),
    pytest.param(
        movie(),
        v.ValidationLimits(boxes=2),
        "container_budget",
        True,
        {"boxes": ["ftyp", "moov"], "box_count": 3, "sample_codecs": []},
        id="container_budget",
    ),
    pytest.param(
        b"".join(box(b"free") for _ in range(40)) + movie()[:-1],
        None,
        "malformed_container",
        False,
        {"boxes": ["free"] * 32, "box_count": 52, "sample_codecs": ["avc1"]},
        id="boxes_are_bounded",
    ),
]


@pytest.mark.parametrize("data,limits,reason,pending,measured", CONTAINER_REFUSALS)
def test_container_refusal_carries_the_boxes_it_walked(
    data, limits, reason, pending, measured
):
    """A refused container measures the top-level box kinds walked up to the
    refusal, in file order and at most 32 of them; the number of boxes counted
    at any depth, which the bound on the list does not cap; and the sample
    codecs read so far. An unsupported refusal and a pending one carry the
    same. A codec refusal also names the sample codecs it refused, and only
    those."""
    checked = v.inspect_container(data, limits)
    assert (checked.reason, checked.pending) == (reason, pending)
    assert checked.measured == measured
    assert_plain(checked.measured)


#: What a refusal sentence says for a value its record lacks.
UNREAD = ("a stream", "another format", "?", "None")


def assert_reads_with_what_it_measured(check, measured):
    """The card's sentence for a refusal the validator raised is its check's
    own, built from the values the check recorded: never the sentence for a
    check without words, and never a placeholder for a value it did not find.
    A measured value renamed on either side fails here, where the sentence
    table's own cases, built by hand, would not."""
    sentence = refusal_evidence.plain(
        refusal_evidence.evidence(
            "local_validation",
            state="unsupported",
            check=check,
            measured=dict(measured),
        )
    )
    assert f"the {check} check" not in sentence, sentence
    assert not any(word in sentence for word in UNREAD), sentence


@pytest.mark.parametrize(
    "error,check,facts,measured",
    [case for case in VIDEO_REFUSALS if case.values[0] is v.UnsupportedMedia],
)
def test_a_video_refusal_reads_with_what_it_measured(error, check, facts, measured):
    """Each video refusal the probe's facts raise reads as its own sentence."""
    raised = refusal_of(error, facts)
    assert_reads_with_what_it_measured(str(raised), raised.measured)


@pytest.mark.parametrize(
    "data,limits,reason,pending,measured",
    [case for case in CONTAINER_REFUSALS if not case.values[3]],
)
def test_a_container_refusal_reads_with_what_it_measured(
    data, limits, reason, pending, measured
):
    """Each unsupported container the walker refuses reads as its own
    sentence."""
    checked = v.inspect_container(data, limits)
    assert_reads_with_what_it_measured(checked.reason, checked.measured)


def test_clean_container_measures_nothing():
    """A container that passes carries an empty `measured`."""
    checked = v.inspect_container(movie())
    assert checked.reason is None
    assert checked.measured == {}


@pytest.mark.parametrize("data,limits,reason,pending,measured", CONTAINER_REFUSALS)
async def test_container_refusal_is_the_failed_check_of_the_result(
    data, limits, reason, pending, measured, tmp_path
):
    """What a container refusal measured is the `failed_check` of the result,
    under the refusal's reason, for an unsupported result and a pending one."""
    limits = replace(
        limits or v.ValidationLimits(),
        ffprobe="/missing/tool",
        temporary_root=str(tmp_path),
    )
    result = await v.validate_media(request(data, "video"), limits=limits)
    assert result.state == ("validation_pending" if pending else "unsupported")
    assert result.reason == reason
    assert result.failed_check == {"check": reason, "measured": measured}


@pytest.mark.parametrize("kind,cap", [("image", 8_000_000), ("video", 40_000_000)])
async def test_byte_limit_measures_the_bytes_and_the_cap(kind, cap):
    """Bytes over the cap are refused with their count and the cap as
    `measured`."""
    result = await v.validate_media(request(bytes(cap + 1), kind))
    assert result.failed_check == {
        "check": "byte_limit",
        "measured": {"bytes": cap + 1, "cap": cap},
    }


@pytest.mark.parametrize("fmt", ["PNG", "BMP"])
async def test_image_format_measures_the_detected_format(fmt):
    """An image that is not a JPEG is refused with the format found in its
    bytes as `measured`."""
    result = await v.validate_media(request(image_bytes(fmt)))
    assert result.failed_check == {
        "check": "image_format",
        "measured": {"format": fmt},
    }


@pytest.mark.parametrize(
    "data,kind,reason",
    [(b"garbage", "image", "malformed_image"), (movie(), "video", "tool_unavailable")],
    ids=["malformed_image", "tool_unavailable"],
)
async def test_a_check_that_read_no_value_carries_an_empty_measured(
    data, kind, reason, tmp_path
):
    """A check that read no value still names itself: its `failed_check` has
    the check and an empty `measured`."""
    limits = v.ValidationLimits(ffprobe="/missing/tool", temporary_root=str(tmp_path))
    result = await v.validate_media(request(data, kind), limits=limits)
    assert result.failed_check == {"check": reason, "measured": {}}


async def test_a_supported_result_has_no_failed_check():
    """A result that is supported has no `failed_check`."""
    result = await v.validate_media(request(image_bytes()))
    assert result.state == "supported"
    assert result.failed_check is None


@pytest.mark.parametrize(
    "reported,measured",
    [
        (
            {"format": "F" * 300, "widths": list(range(40))},
            {"format": "F" * 64, "widths": list(range(32))},
        ),
        ("not a mapping", {}),
    ],
    ids=["bounded", "not_a_mapping"],
)
async def test_worker_measured_values_are_bounded_again(
    reported, measured, monkeypatch, tmp_path
):
    """The values the image worker reports are kept as any measured value is,
    text cut to 64 characters and lists to 32 items, and a `measured` that is
    not a mapping is dropped: the worker decoded hostile bytes, so its report
    is not trusted to be bounded."""

    async def worker(*args):
        document = json.dumps({"unsupported": "image_format", "measured": reported})
        return SimpleNamespace(returncode=0, stdout=document.encode(), stderr=b"")

    monkeypatch.setattr(v, "run_bounded", worker)
    result = await v.validate_media(
        request(image_bytes()), limits=v.ValidationLimits(temporary_root=str(tmp_path))
    )
    assert result.failed_check == {"check": "image_format", "measured": measured}


async def test_failed_check_is_plain_json(tmp_path):
    """A result's `failed_check`, taken as a plain mapping, survives
    `json.dumps` and reads back equal, whether the values it measured are
    numbers or lists of text. (The result holds it as a read-only mapping;
    `dict(...)` is what the record takes.)"""
    limits = v.ValidationLimits(ffprobe="/missing/tool", temporary_root=str(tmp_path))
    for data, kind in ((bytes(8_000_001), "image"), (movie(external=True), "video")):
        result = await v.validate_media(request(data, kind), limits=limits)
        failed = dict(result.failed_check)
        assert failed["measured"], "a check that measured nothing proves nothing here"
        assert json.loads(json.dumps(failed)) == failed
        assert_plain(failed)


@pytest.mark.parametrize(
    "data", [image_bytes(), b"garbage"], ids=["supported", "refused"]
)
async def test_fingerprint_is_the_documented_digest_with_or_without_a_failed_check(
    data,
):
    """A result's fingerprint is the SHA-256 of the sorted, compact JSON of its
    captured identity, the matrix and evidence versions, its state, its reason
    and its evidence, and of nothing else: a result that carries a
    `failed_check` is fingerprinted exactly as one that does not."""
    source = request(data)
    result = await v.validate_media(source)
    assert (result.failed_check is None) == (result.state == "supported")
    documented = {
        "kind": source.kind,
        "generation": source.source_generation,
        "revision": source.observation_revision,
        "asset": source.selected_asset_id,
        "matrix": v.MATRIX_VERSION,
        "evidence_version": v.EVIDENCE_VERSION,
        "state": result.state,
        "reason": result.reason,
        "evidence": dict(result.evidence),
    }
    digest = hashlib.sha256(
        json.dumps(documented, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert result.fingerprint == digest


def refused_container(boxes):
    """An `inspect_container` that refuses every container, measuring `boxes`."""

    def inspect(data, limits=None):
        return v.ContainerEvidence(
            "malformed_container",
            measured={"boxes": boxes, "box_count": len(boxes), "sample_codecs": []},
        )

    return inspect


async def test_fingerprint_does_not_move_with_what_the_check_measured(monkeypatch):
    """Two results for the same bytes whose checks measured different values
    carry different `failed_check`s and the same fingerprint."""
    source = request(movie(), "video")
    results = []
    for boxes in (["ftyp"], ["ftyp", "moov", "mdat"]):
        monkeypatch.setattr(v, "inspect_container", refused_container(boxes))
        results.append(await v.validate_media(source))
    first, second = results
    assert dict(first.failed_check) != dict(second.failed_check)
    assert first.fingerprint == second.fingerprint


# -- the story derivation ------------------------------------------------------


def esds(channels, frequency_index=3):
    """An `esds` whose AudioSpecificConfig is AAC LC (object type 2) at
    *frequency_index* (3 is 48 kHz, 0 is 96 kHz) with *channels* as its
    channelConfiguration, inside its decoder config (objectTypeIndication
    0x40) and its ES descriptor."""
    config = struct.pack(">H", 2 << 11 | frequency_index << 7 | channels << 3)
    specific = bytes([0x05, len(config)]) + config
    decoder = bytes([0x04, 13 + len(specific), 0x40, 0x15]) + bytes(11) + specific
    es = bytes([0x03, 3 + len(decoder)]) + b"\x00\x01\x00" + decoder
    return box(b"esds", bytes(4) + es)


def audio_entry(codec=b"mp4a", channels=2, frequency_index=3, *, hz=48_000):
    """An AudioSampleEntry: reserved bytes and the data reference index, two
    reserved words, the channel count as its template 2 (as ffmpeg writes an
    MP4's), the sample size, two zeroes and *hz* as the 16.16 rate, then for
    AAC the `esds` that configures *channels* at *frequency_index*."""
    return box(
        codec,
        bytes(6)
        + b"\x00\x01"
        + bytes(8)
        + struct.pack(">HHHHI", 2, 16, 0, 0, hz << 16)
        + (esds(channels, frequency_index) if codec == b"mp4a" else b""),
    )


def track(handler, entry, *, edits=False):
    """A `trak` whose `mdia` names its *handler* and whose `stsd` holds
    *entry*, with an edit list when *edits*."""
    hdlr = box(b"hdlr", bytes(8) + handler + bytes(12) + b"\x00")
    stsd = box(b"stsd", bytes(4) + struct.pack(">I", 1) + entry)
    return box(
        b"trak",
        (box(b"edts", box(b"elst", bytes(8))) if edits else b"")
        + box(b"mdia", hdlr + box(b"minf", box(b"stbl", stsd))),
    )


def derivation(*, late=False, edits=False, audio=audio_entry(), pad=0):
    """A story derivation's bytes: `ftyp`, then `moov` (a video track and,
    with *audio*, a sound track, grown by *pad* bytes of `free`) before
    `mdat`, or after it when *late*."""
    video = track(
        b"vide", box(b"avc1", bytes(6) + b"\x00\x01" + bytes(70)), edits=edits
    )
    sound = track(b"soun", audio, edits=edits) if audio else b""
    moov = box(
        b"moov",
        box(b"mvhd", bytes(100))
        + video
        + sound
        + (box(b"free", bytes(pad)) if pad else b""),
    )
    mdat = box(b"mdat", bytes(64))
    return box(b"ftyp", b"isom" + bytes(4)) + (mdat + moov if late else moov + mdat)


def moov_end(data):
    """Where `moov` ends in a derivation built with `moov` first."""
    return data.index(b"mdat") - 4


def test_a_derivation_with_moov_first_says_what_its_moov_holds():
    data = derivation()
    assert v.derivation_container(data) == {
        "boxes": ["ftyp", "moov"],
        "moov_first": True,
        "moov_end": moov_end(data),
        "edit_lists": 0,
        "sample_codecs": ["avc1", "mp4a"],
        "audio": [{"codec": "mp4a", "channels": 2, "hz": 48_000}],
    }


def test_a_head_that_stops_inside_moov_says_only_where_moov_ends():
    """The probe's first bytes: enough to find `moov` and its size, not its
    tracks. The rest is read once `ready` knows how far to read."""
    data = derivation(pad=4096)
    facts = v.derivation_container(data[:1024])
    assert (facts["moov_first"], facts["moov_end"]) == (True, moov_end(data))
    assert facts["edit_lists"] is None
    assert facts["sample_codecs"] is None and facts["audio"] is None


def test_a_derivation_with_its_media_first_is_not_walked():
    facts = v.derivation_container(derivation(late=True))
    assert facts["boxes"] == ["ftyp", "mdat"]
    assert (facts["moov_first"], facts["moov_end"]) == (False, None)
    assert facts["edit_lists"] is None and facts["audio"] is None


def test_every_edit_list_and_every_audio_entry_is_read():
    facts = v.derivation_container(
        derivation(edits=True, audio=audio_entry(b"ac-3", hz=64_000))
    )
    assert facts["edit_lists"] == 2
    assert facts["sample_codecs"] == ["avc1", "ac-3"]
    assert facts["audio"] == [{"codec": "ac-3", "channels": None, "hz": 64_000}]


@pytest.mark.parametrize("channels", [1, 2, 6])
def test_the_channels_are_the_aac_configurations_not_the_entrys_template(channels):
    """Every entry here says 2 in its own channel field; the AAC
    configuration in its `esds` names what the stream carries."""
    facts = v.derivation_container(derivation(audio=audio_entry(channels=channels)))
    assert facts["audio"] == [{"codec": "mp4a", "channels": channels, "hz": 48_000}]


def test_the_rate_is_the_aac_configurations_past_what_the_entry_can_hold():
    """A 16.16 rate stops at 65,535 Hz, so the entry here says 48 kHz; its AAC
    configuration says 96 kHz, which is the rate the stream carries."""
    facts = v.derivation_container(derivation(audio=audio_entry(frequency_index=0)))
    assert facts["audio"] == [{"codec": "mp4a", "channels": 2, "hz": 96_000}]
    with pytest.raises(v.UnsupportedMedia, match="derivation_audio_rate") as rate:
        v.judge_derivation({"length": None, "container": facts}, "video")
    assert rate.value.measured == {"hz": 96_000, "limit": 48_000}


def test_an_aac_entry_without_a_configuration_reads_no_channels():
    """Its rate is the entry's own; its channel count is not read."""
    entry = box(b"mp4a", bytes(6) + b"\x00\x01" + bytes(20))
    facts = v.derivation_container(derivation(audio=entry))
    assert facts["audio"] == [{"codec": "mp4a", "channels": None, "hz": 0}]


def test_a_moov_whose_boxes_do_not_nest_is_left_unread():
    data = bytearray(derivation())
    trak = data.index(b"trak") - 4
    data[trak : trak + 4] = struct.pack(">I", 1 << 20)  # past the end of `moov`
    facts = v.derivation_container(bytes(data))
    assert facts["moov_first"] is True and facts["unread"] == "moov_unwalkable"
    assert facts["edit_lists"] is None and facts["audio"] is None


def test_a_real_encoded_video_is_read_as_its_muxer_wrote_it(tmp_path):
    """ffmpeg's defaults put `moov` after the media and an edit list on the
    tracks; `+faststart` moves `moov` to the front. The sine source is mono
    AAC at 48 kHz."""
    late = v.derivation_container(encoded_video(tmp_path))
    assert (late["boxes"][-1], late["moov_first"]) == ("mdat", False)
    early = v.derivation_container(encoded_video(tmp_path, faststart=True))
    assert early["moov_first"] is True and early["edit_lists"] >= 1
    assert early["sample_codecs"] == ["avc1", "mp4a"]
    assert early["audio"] == [{"codec": "mp4a", "channels": 1, "hz": 48_000}]


def observed(length=None, **container):
    """What `ready` records of a frame, as `judge_derivation` reads it."""
    return {"status": 206, "length": length, "container": container}


@pytest.mark.parametrize("kind,limit", [("video", 100_000_000), ("image", 8_000_000)])
def test_a_derivation_over_metas_size_is_refused_with_both_numbers(kind, limit):
    v.judge_derivation(observed(limit), kind)
    with pytest.raises(v.UnsupportedMedia, match="derivation_bytes") as refused:
        v.judge_derivation(observed(limit + 1), kind)
    assert refused.value.measured == {"bytes": limit + 1, "limit": limit}
    assert_reads_with_what_it_measured("derivation_bytes", refused.value.measured)


def test_a_derivation_whose_audio_is_not_aac_or_is_over_48_khz_is_refused():
    fits = {"codec": "mp4a", "channels": 2, "hz": 48_000}
    v.judge_derivation(observed(audio=[fits]), "video")
    with pytest.raises(v.UnsupportedMedia, match="derivation_audio_codec") as codec:
        v.judge_derivation(observed(audio=[{**fits, "codec": "ac-3"}]), "video")
    assert codec.value.measured == {"codec": "ac-3"}
    assert_reads_with_what_it_measured("derivation_audio_codec", codec.value.measured)
    with pytest.raises(v.UnsupportedMedia, match="derivation_audio_rate") as rate:
        v.judge_derivation(observed(audio=[{**fits, "hz": 64_000}]), "video")
    assert rate.value.measured == {"hz": 64_000, "limit": 48_000}
    assert_reads_with_what_it_measured("derivation_audio_rate", rate.value.measured)


#: The layout and encoding facts that are recorded and left to Meta's answer:
#: each one's switch, a container that breaks its line, the check its switch
#: refuses with, and what that refusal measures.
RECORDED_LINES = [
    pytest.param(
        "REFUSE_MOOV_POSITION",
        {"moov_first": False, "boxes": ["ftyp", "mdat"]},
        "derivation_moov_position",
        {"boxes": ["ftyp", "mdat"]},
        id="moov-position",
    ),
    pytest.param(
        "REFUSE_EDIT_LISTS",
        {"moov_first": True, "edit_lists": 2},
        "derivation_edit_list",
        {"edit_lists": 2},
        id="edit-lists",
    ),
    pytest.param(
        "REFUSE_AUDIO_CHANNELS",
        {"audio": [{"codec": "mp4a", "channels": 6, "hz": 48_000}]},
        "derivation_audio_channels",
        {"channels": 6, "limit": 2},
        id="audio-channels",
    ),
]


@pytest.mark.parametrize("switch,container,check,measured", RECORDED_LINES)
def test_a_recorded_line_refuses_only_with_its_switch(
    monkeypatch, switch, container, check, measured
):
    assert getattr(v, switch) is False, "recorded, and left to Meta's answer"
    v.judge_derivation(observed(**container), "video")
    monkeypatch.setattr(v, switch, True)
    with pytest.raises(v.UnsupportedMedia, match=check) as refused:
        v.judge_derivation(observed(**container), "video")
    assert refused.value.measured == measured
    assert_reads_with_what_it_measured(check, refused.value.measured)


@pytest.mark.parametrize(
    "observation",
    [
        None,
        {},
        observed(),
        observed(moov_first=None, edit_lists=None, audio=None),
        observed(audio=[{"codec": "mp4a", "channels": None, "hz": None}]),
    ],
    ids=["no-probe", "empty", "no-length", "nothing-read", "entry-cut-short"],
)
def test_what_the_probe_did_not_read_is_not_judged(observation):
    v.judge_derivation(observation, "video")
