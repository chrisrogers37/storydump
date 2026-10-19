"""Bounded local validation: no provider, database, or production activation."""

import asyncio
import io
import struct
import sys
from dataclasses import replace

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


def facts_result(**kwargs):
    facts, formats = video_facts(**kwargs)
    return v.inspect_video_facts(
        facts,
        formats,
        v.ContainerEvidence(None, media_extents=((0, 100),)),
        v.ValidationLimits(),
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
