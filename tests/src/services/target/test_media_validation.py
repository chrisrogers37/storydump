"""Bounded local validation: no provider, database, or production activation."""

import asyncio
import hashlib
import io
import json
import struct
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PIL import Image

from src.services.target import media_validation as v


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


def movie(*, external=False, edits=False, late=False, fragmented=False):
    ref = box(
        b"url ",
        b"\x00\x00\x00" + (b"\x00../private-secret\x00" if external else b"\x01"),
    )
    dref = box(b"dref", bytes(4) + struct.pack(">I", 1) + ref)
    stsd = box(
        b"stsd",
        bytes(4)
        + struct.pack(">I", 1)
        + box(b"avc1", bytes(6) + b"\x00\x01" + bytes(78)),
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
        ({"edits": True}, "edit_list", "unsupported"),
        ({"late": True}, "moov_not_front", "unsupported"),
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


async def test_missing_tools_never_proves_unsupported(tmp_path):
    result = await v.validate_media(
        request(movie(), "video"),
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


def video_facts(
    *,
    count=90,
    fps=30,
    width=320,
    height=568,
    codec="h264",
    sample_rate=48000,
    channels=2,
    audio=False,
):
    stream = {
        "index": 0,
        "codec_type": "video",
        "codec_name": codec,
        "time_base": f"1/{fps}",
        "nb_frames": str(count),
        "duration_ts": count,
        "sample_aspect_ratio": "1:1",
    }
    frames = [
        {
            "stream_index": 0,
            "pts": n,
            "duration": 1,
            "width": width,
            "height": height,
            "pix_fmt": "yuv420p",
            "interlaced_frame": 0,
            "sample_aspect_ratio": "1:1",
        }
        for n in range(count)
    ]
    packets = [
        {"stream_index": 0, "pts": n, "duration": 1, "size": "1", "pos": "10"}
        for n in range(count)
    ]
    streams = [stream]
    if audio:
        streams.append(
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": str(sample_rate),
                "channels": channels,
            }
        )
    return {"streams": streams, "packets": packets, "frames": frames}, {
        "yuv420p": (1, 1)
    }


def facts_result(*, limits=None, mutate=None, **kwargs):
    facts, formats = video_facts(**kwargs)
    if mutate:
        mutate(facts, formats)
    return v.inspect_video_facts(
        facts,
        formats,
        v.ContainerEvidence(None, media_extents=((0, 100),)),
        limits or v.ValidationLimits(),
    )


@pytest.mark.parametrize(
    "count,fps,reason",
    [
        (89, 30, "duration"),
        (90, 30, None),
        (1800, 30, None),
        (1801, 30, "duration"),
        (66, 22, "fps"),
        (69, 23, None),
        (180, 60, None),
        (183, 61, "fps"),
    ],
)
def test_exact_video_duration_and_fps_boundaries(count, fps, reason):
    if reason:
        with pytest.raises(v.UnsupportedMedia, match=f"^{reason}$"):
            facts_result(count=count, fps=fps)
    else:
        assert facts_result(count=count, fps=fps)["video_frames"] == count


@pytest.mark.parametrize(
    "width,height,reason",
    [
        (1920, 1080, None),
        (1921, 1080, "width"),
        (100, 1000, None),
        (99, 1000, "aspect"),
        (1000, 100, None),
        (1001, 100, "aspect"),
    ],
)
def test_exact_video_geometry_boundaries(width, height, reason):
    if reason:
        with pytest.raises(v.UnsupportedMedia, match=f"^{reason}$"):
            facts_result(width=width, height=height)
    else:
        assert facts_result(width=width, height=height)["width"] == width


@pytest.mark.parametrize(
    "sample_rate,channels,reason",
    [
        (48000, 1, None),
        (48000, 2, None),
        (48001, 2, "audio_sample_rate"),
        (48000, 3, "audio_channels"),
    ],
)
def test_audio_configuration_boundaries(sample_rate, channels, reason):
    if reason:
        with pytest.raises(v.UnsupportedMedia, match=f"^{reason}$"):
            facts_result(audio=True, sample_rate=sample_rate, channels=channels)
    else:
        assert (
            facts_result(audio=True, sample_rate=sample_rate, channels=channels)[
                "audio_bitrate"
            ]
            == "unproven"
        )


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("missing_frames", "incomplete_evidence"),
        ("vfr", "timing_evidence_incomplete"),
        ("rotation", "transform_evidence_incomplete"),
        ("extra_track", "stream_topology"),
        ("unknown_pixel", "color_evidence_incomplete"),
    ],
)
def test_unknown_or_incomplete_video_evidence_is_pending(mutation, reason):
    facts, formats = video_facts()
    if mutation == "missing_frames":
        facts["frames"].pop()
    elif mutation == "vfr":
        facts["frames"][40]["pts"] += 1
    elif mutation == "rotation":
        facts["streams"][0]["side_data_list"] = [{"rotation": 90}]
    elif mutation == "extra_track":
        facts["streams"].append(dict(facts["streams"][0]))
    else:
        facts["frames"][-1]["pix_fmt"] = "unrecognized"
    with pytest.raises(v.ValidationPending, match=f"^{reason}$"):
        v.inspect_video_facts(
            facts,
            formats,
            v.ContainerEvidence(None, media_extents=((0, 100),)),
            v.ValidationLimits(),
        )


def test_header_trace_proves_each_gop_start_not_just_first_keyframe():
    prefix = "[trace_headers @ 0x1] "
    valid = (
        prefix
        + "Packet: 10 bytes, key frame, pts 0.\n"
        + prefix
        + "3 nal_unit_type 00101 = 5\n"
        + prefix
        + "9 slice_type 0001 = 7\n"
    )
    valid += (
        prefix
        + "Packet: 10 bytes, pts 1.\n"
        + prefix
        + "3 nal_unit_type 00001 = 1\n"
        + prefix
        + "9 slice_type 001 = 5\n"
    )
    result = v.inspect_headers(valid, "h264", 2)
    assert result["closed_gop"] == "idr_starts_proven"
    bad = (
        valid
        + prefix
        + "Packet: 10 bytes, key frame, pts 2.\n"
        + prefix
        + "3 nal_unit_type 00001 = 1\n"
        + prefix
        + "9 slice_type 0001 = 7\n"
    )
    with pytest.raises(v.ValidationPending, match="closed_gop_unproven"):
        v.inspect_headers(bad, "h264", 3)
    with pytest.raises(v.ValidationPending, match="header_evidence_incomplete"):
        v.inspect_headers(valid, "h264", 3)


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
    executable.write_text(
        "#!"
        + sys.executable
        + '\nimport sys\nif "-version" in sys.argv: print("ffprobe version 7.1")\nelse: print("{")\n'
    )
    executable.chmod(0o700)
    result = await v.validate_media(
        request(movie(), "video"),
        limits=v.ValidationLimits(
            ffprobe=str(executable),
            ffmpeg=str(executable),
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


def refusal_of(error, **kwargs):
    """The `error` that `inspect_video_facts` raises for the facts
    `facts_result` builds from `kwargs`."""
    with pytest.raises(error) as raised:
        facts_result(**kwargs)
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


def interlace_first_frame(facts, formats):
    facts["frames"][0]["interlaced_frame"] = 1


def subsample_first_frame_444(facts, formats):
    facts["frames"][0]["pix_fmt"] = "yuv444p"
    formats["yuv444p"] = (0, 0)


def stretch_every_frame(facts, formats):
    for frame in facts["frames"]:
        frame["sample_aspect_ratio"] = "1:2"


def stray_fourth_packet(facts, formats):
    facts["packets"][3]["pos"] = "500"


def mp3_audio(facts, formats):
    facts["streams"][1]["codec_name"] = "mp3"


def second_audio_stream(facts, formats):
    facts["streams"].append({**facts["streams"][1], "index": 2})


def unnamed_video_codec(facts, formats):
    del facts["streams"][0]["codec_name"]


#: Every refusal `inspect_video_facts` makes after reading values: the error it
#: raises, the check it names, how its facts are built, and the values it measured.
VIDEO_REFUSALS = [
    pytest.param(
        v.UnsupportedMedia,
        "video_codec",
        {"codec": "vp9"},
        {"video_codec": "vp9"},
        id="video_codec",
    ),
    pytest.param(
        v.UnsupportedMedia,
        "video_codec",
        {"mutate": unnamed_video_codec},
        {"video_codec": None},
        id="video_codec_unnamed",
    ),
    pytest.param(
        v.UnsupportedMedia,
        "video_codec",
        {"codec": "x" * 300},
        {"video_codec": "x" * 64},
        id="video_codec_text_bound",
    ),
    pytest.param(
        v.UnsupportedMedia,
        "width",
        {"width": 1921, "height": 1080},
        {"width": 1921, "height": 1080},
        id="width",
    ),
    pytest.param(
        v.UnsupportedMedia,
        "aspect",
        {"width": 1001, "height": 100},
        {
            "width": 1001,
            "height": 100,
            "sample_aspect_ratio": "1",
            "aspect": "1001/100",
        },
        id="aspect",
    ),
    pytest.param(
        v.UnsupportedMedia,
        "aspect",
        {"width": 100, "height": 1000, "mutate": stretch_every_frame},
        {
            "width": 100,
            "height": 1000,
            "sample_aspect_ratio": "1/2",
            "aspect": "1/20",
        },
        id="aspect_with_sample_aspect_ratio",
    ),
    pytest.param(
        v.UnsupportedMedia,
        "interlaced",
        {"mutate": interlace_first_frame},
        {"width": 320, "height": 568},
        id="interlaced",
    ),
    pytest.param(
        v.UnsupportedMedia,
        "chroma_subsampling",
        {"mutate": subsample_first_frame_444},
        {"pix_fmt": "yuv444p"},
        id="chroma_subsampling",
    ),
    pytest.param(
        v.UnsupportedMedia,
        "duration",
        {"count": 89, "fps": 30},
        {"duration_seconds": "89/30", "fps": "30", "video_frames": 89},
        id="duration",
    ),
    pytest.param(
        v.UnsupportedMedia,
        "fps",
        {"count": 671, "fps": 22},
        {"duration_seconds": "61/2", "fps": "22", "video_frames": 671},
        id="fps",
    ),
    pytest.param(
        v.UnsupportedMedia,
        "packet_outside_media",
        {"mutate": stray_fourth_packet},
        {"packet_pos": 500, "packet_size": 1},
        id="packet_outside_media",
    ),
    pytest.param(
        v.UnsupportedMedia,
        "audio_codec",
        {"audio": True, "mutate": mp3_audio},
        {"audio_codec": "mp3"},
        id="audio_codec",
    ),
    pytest.param(
        v.UnsupportedMedia,
        "audio_sample_rate",
        {"audio": True, "sample_rate": 48001},
        {"sample_rate": 48001},
        id="audio_sample_rate",
    ),
    pytest.param(
        v.UnsupportedMedia,
        "audio_channels",
        {"audio": True, "channels": 3},
        {"channels": 3},
        id="audio_channels",
    ),
    pytest.param(
        v.ValidationPending,
        "stream_topology",
        {"audio": True, "mutate": second_audio_stream},
        {"streams": 3, "video_streams": 1, "audio_streams": 2},
        id="stream_topology",
    ),
    pytest.param(
        v.ValidationPending,
        "pixel_budget",
        {"limits": v.ValidationLimits(pixels=100_000)},
        {"width": 320, "height": 568, "pixel_limit": 100_000},
        id="pixel_budget",
    ),
]


@pytest.mark.parametrize("error,check,build,measured", VIDEO_REFUSALS)
def test_video_check_carries_the_values_it_read(error, check, build, measured):
    """A video check that read values refuses with exactly those values as
    `exc.measured`: whole numbers stay numbers, a ratio is its exact text, any
    other value is text cut to 64 characters, and a field the probe left out
    stays None."""
    raised = refusal_of(error, **build)
    assert str(raised) == check
    assert raised.measured == measured
    assert_plain(raised.measured)


#: Every refusal `inspect_container` makes: the bytes, the limits (None for the
#: defaults), the reason, whether it is pending, and the values it measured.
CONTAINER_REFUSALS = [
    pytest.param(
        movie(edits=True),
        None,
        "edit_list",
        False,
        {"boxes": ["ftyp", "moov"], "box_count": 13, "sample_codecs": ["avc1"]},
        id="edit_list",
    ),
    pytest.param(
        movie(late=True),
        None,
        "moov_not_front",
        False,
        {
            "boxes": ["ftyp", "mdat", "moov"],
            "box_count": 12,
            "sample_codecs": ["avc1"],
        },
        id="moov_not_front",
    ),
    pytest.param(
        movie().replace(b"avc1", b"vp09"),
        None,
        "video_codec",
        False,
        {
            "boxes": ["ftyp", "moov", "mdat"],
            "box_count": 12,
            "sample_codecs": ["vp09"],
        },
        id="video_codec",
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
        b"".join(box(b"free") for _ in range(40)) + movie(late=True),
        None,
        "moov_not_front",
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
    same."""
    checked = v.inspect_container(data, limits)
    assert (checked.reason, checked.pending) == (reason, pending)
    assert checked.measured == measured
    assert_plain(checked.measured)


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
    for data, kind in ((bytes(8_000_001), "image"), (movie(edits=True), "video")):
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
            "edit_list",
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
