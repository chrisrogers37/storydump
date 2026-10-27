"""Bounded local validation: no provider, database, or production activation."""

import asyncio
import hashlib
import io
import json
import re
import struct
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from src.services.target import media_validation as v
from src.services.target import refusal_evidence
from src.services.target.vocabulary import PUBLISH_MAX_BYTES


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
    *,
    external=False,
    edits=False,
    late=False,
    fragmented=False,
    codecs=(b"avc1",),
    tracks=1,
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
    moov = box(b"moov", track * tracks)
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


@pytest.mark.parametrize("kind,cap", [("image", 8_388_608), ("video", 40_000_000)])
async def test_exact_byte_ceiling_cannot_be_relaxed(kind, cap):
    assert v.SUPPORTED_MEDIA_MATRIX["byte_caps"][kind] == cap
    result = await v.validate_media(request(bytes(cap + 1), kind))
    assert (result.state, result.reason) == ("unsupported", "byte_limit")
    with pytest.raises(ValueError):
        v.ValidationLimits(wall_seconds=31)


def test_the_byte_cap_is_what_the_transit_store_takes():
    """A source is held to the transit store's limits alone: Meta's size limits
    govern the story derivation (`judge_derivation`)."""
    assert v.SUPPORTED_MEDIA_MATRIX["byte_caps"] == PUBLISH_MAX_BYTES


async def test_an_image_over_metas_size_within_the_transit_limit_is_judged():
    """An image over Meta's 8 MB but within the transit store's limit is not
    refused for its size: Meta's limit judges its derivation."""
    result = await v.validate_media(request(bytes(v.PROVIDER_IMAGE_BYTES + 1)))
    assert (result.state, result.reason) == ("unsupported", "malformed_image")


async def test_container_preflight_before_tools(tmp_path):
    result = await v.validate_media(
        request(movie(external=True), "video"),
        limits=v.ValidationLimits(
            ffprobe="/missing/tool", temporary_root=str(tmp_path)
        ),
    )
    assert (result.state, result.reason) == ("unsupported", "external_reference")
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


def probe_facts(
    *,
    duration="10.000000",
    fps="30/1",
    format_duration=None,
    video=True,
    audio_duration=None,
):
    """ffprobe's JSON for one file, in the entries the validator asks for: the
    audio stream runs *duration* unless *audio_duration* says otherwise."""
    streams = [
        {
            "codec_type": "audio",
            "codec_name": "aac",
            "duration": audio_duration or duration,
        }
    ]
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


@pytest.mark.parametrize(
    "facts,key",
    [(probe_facts(duration=None), "duration_seconds"), (probe_facts(fps="0/0"), "fps")],
    ids=["duration", "fps"],
)
def test_a_timing_the_probe_does_not_state_is_not_judged(facts, key):
    """A duration or a frame rate stated nowhere is not judged: the derivation
    is trimmed to the longest story either way."""
    evidence = v.inspect_video_facts(facts)
    assert (evidence[key], evidence["trimmed_to_seconds"]) == ("unstated", None)


@pytest.mark.parametrize(
    "facts,duration,trimmed",
    [
        (probe_facts(duration="59", audio_duration="62"), "62", "60"),
        (probe_facts(duration="2", audio_duration="5"), "5", None),
        (probe_facts(duration=None, format_duration="12.5"), "25/2", None),
    ],
    ids=["longer-audio", "short-video-track", "the-containers-alone"],
)
def test_the_duration_is_the_longest_the_container_or_a_stream_states(
    facts, duration, trimmed
):
    """A 59 s video track with 62 s of audio is a 62 s video, whose derivation
    is trimmed, and a 2 s video track with 5 s of audio is not refused for its
    length."""
    evidence = v.inspect_video_facts(facts)
    assert (evidence["duration_seconds"], evidence["trimmed_to_seconds"]) == (
        duration,
        trimmed,
    )


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


def encoded_video(
    tmp_path,
    *,
    seconds=4,
    rate=30,
    faststart=False,
    fragmented=False,
    audio_tracks=1,
    codec="libx264",
    timecode=False,
    audio="aac",
    cover=False,
):
    """An H.264 + AAC MP4 written with ffmpeg's defaults, which put an edit
    list on each track and `moov` after the media (before it, with
    *faststart*), with *audio_tracks* copies of the audio; *fragmented* writes
    the media in fragments after an empty `moov`, *codec* encodes the video
    with another encoder, *timecode* writes a MOV with a timecode track,
    *audio* encodes the audio with another encoder, and *cover* adds a JPEG
    as an attached picture, a stream and no track."""
    _requires_linux()
    out = tmp_path / ("source.mov" if timecode else "source.mp4")
    picture = tmp_path / "cover.jpg"
    if cover:
        picture.write_bytes(image_bytes())
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
            *(["-i", str(picture)] if cover else []),
            "-t",
            str(seconds),
            "-map",
            "0:v",
            *["-map", "1:a"] * audio_tracks,
            *(["-map", "2:v"] if cover else []),
            "-c:v",
            codec,
            *(["-c:v:1", "copy", "-disposition:v:1", "attached_pic"] if cover else []),
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            audio,
            *(["-movflags", "+faststart"] if faststart else []),
            *(["-movflags", "+empty_moov+frag_keyframe"] if fragmented else []),
            *(["-timecode", "00:00:00:00"] if timecode else []),
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


async def test_a_real_fragmented_video_is_judged_by_the_probe(tmp_path):
    """A fragmented MP4, which the container walker cannot read whole, is
    supported on what the probe reads, and records where the walk stopped."""
    data = encoded_video(tmp_path, fragmented=True)
    assert b"moof" in data, "the stimulus is fragmented"
    result = await v.validate_media(
        request(data, "video"),
        limits=v.ValidationLimits(temporary_root=str(tmp_path)),
    )
    assert (result.state, result.reason) == ("supported", "validated")
    assert result.evidence["container"]["stopped"] == "container_evidence_incomplete"


async def test_a_real_video_with_four_streams_is_probed(tmp_path):
    """One video and three audio streams are within the probe's bound."""
    data = encoded_video(tmp_path, audio_tracks=3)
    result = await v.validate_media(
        request(data, "video"),
        limits=v.ValidationLimits(temporary_root=str(tmp_path)),
    )
    assert (result.state, result.reason) == ("supported", "validated")
    assert "probe_skipped" not in result.evidence


@pytest.mark.parametrize(
    "kwargs,undecoded,skipped",
    [
        ({"codec": "mpeg4"}, ["mp4v"], {"closed_decoders": ["mpeg4"]}),
        ({"timecode": True}, ["tmcd"], None),
    ],
    ids=["mpeg4-part-2-video", "timecode-track"],
)
async def test_a_real_video_with_a_stream_the_probe_does_not_decode_is_supported(
    tmp_path, monkeypatch, kwargs, undecoded, skipped
):
    """A MOV's timecode track has no decoder, so the probe reads the file. An
    MPEG-4 Part 2 video's decoder is closed to the probe, which stops at it,
    so the file is its derivation's to read, and the decoder is named as the
    probe names it (`mpeg4`, where the sample entry is `mp4v`). Either way it
    is supported, and the codecs the probe does not decode are recorded."""
    data = encoded_video(tmp_path, **kwargs)
    probed = []
    run = v.run_bounded

    async def recorded(*args):
        probed.append(await run(*args))
        return probed[-1]

    monkeypatch.setattr(v, "run_bounded", recorded)
    result = await v.validate_media(
        request(data, "video"),
        limits=v.ValidationLimits(temporary_root=str(tmp_path)),
    )
    said = [(output.returncode, output.stderr[-400:]) for output in probed]
    assert (result.state, result.reason) == ("supported", "validated"), said
    assert result.evidence["undecoded_codecs"] == undecoded
    assert result.evidence.get("probe_skipped") == skipped, said


async def probed_video(monkeypatch, tmp_path, data, limits=None):
    """`validate_media` on the real video *data*, and what each run of the
    probe said (its exit code and the end of its stderr), for the message of an
    assertion that fails."""
    probed = []
    run = v.run_bounded

    async def recorded(*args):
        probed.append(await run(*args))
        return probed[-1]

    monkeypatch.setattr(v, "run_bounded", recorded)
    result = await v.validate_media(
        request(data, "video"),
        limits=replace(limits or v.ValidationLimits(), temporary_root=str(tmp_path)),
    )
    return result, [(output.returncode, output.stderr[-400:]) for output in probed]


async def test_a_real_video_with_a_cover_picture_names_its_declined_decoder(
    tmp_path, monkeypatch
):
    """A cover picture is a stream to the probe, whose decoder is closed to
    it, and no track to the walk, which finds no codec it does not decode. The
    probe stops at the decoder, so the file is its derivation's to read, and
    the evidence names the decoder."""
    data = encoded_video(tmp_path, cover=True)
    result, said = await probed_video(monkeypatch, tmp_path, data)
    assert (result.state, result.reason) == ("supported", "validated"), said
    skipped = result.evidence.get("probe_skipped", {})
    assert "mjpeg" in skipped.get("closed_decoders", []), said


async def test_a_real_video_with_mpeg_audio_in_an_aac_entry_names_its_decoder(
    tmp_path, monkeypatch
):
    """MPEG audio in an `mp4a` entry, which the walk takes for AAC's, has no
    decoder open to the probe. The probe stops at it, so the file is its
    derivation's to read, and the evidence names the decoder the probe
    declined, not the entry's name; the walk records nothing as undecoded."""
    data = encoded_video(tmp_path, audio="mp2")
    result, said = await probed_video(monkeypatch, tmp_path, data)
    assert (result.state, result.reason) == ("supported", "validated"), said
    declined = result.evidence.get("probe_skipped", {}).get("closed_decoders")
    assert declined and "mp4a" not in declined, said
    assert "undecoded_codecs" not in result.evidence, said


async def test_a_real_video_whose_cover_passes_the_probes_stream_bound_is_supported(
    tmp_path, monkeypatch
):
    """The walk counts a cover picture as no track and the probe counts it as
    a stream, so a file with as many tracks as the probe's bound passes the
    walk and the probe stops at its own bound. The file is its derivation's to
    read, not left pending, and the evidence says what the walk counted beside
    the bound."""
    data = encoded_video(tmp_path, cover=True)
    tracks = v.inspect_container(data).streams
    assert tracks == 2, "the cover picture is a stream and no track"
    result, said = await probed_video(
        monkeypatch, tmp_path, data, v.ValidationLimits(streams=tracks)
    )
    assert (result.state, result.reason) == ("supported", "validated"), said
    assert result.evidence.get("probe_skipped") == {
        "max_streams": tracks,
        "walked_streams": tracks,
    }, said


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
#: defaults), the reason, and the values it measured.
CONTAINER_REFUSALS = [
    pytest.param(
        movie()[:-1],
        None,
        "malformed_container",
        {"boxes": ["ftyp", "moov"], "box_count": 12, "sample_codecs": ["avc1"]},
        id="malformed_container",
    ),
    pytest.param(
        movie(external=True),
        None,
        "external_reference",
        {"boxes": ["ftyp", "moov"], "box_count": 8, "sample_codecs": []},
        id="external_reference",
    ),
    pytest.param(
        b"".join(box(b"free") for _ in range(40)) + movie()[:-1],
        None,
        "malformed_container",
        {"boxes": ["free"] * 32, "box_count": 52, "sample_codecs": ["avc1"]},
        id="boxes_are_bounded",
    ),
]


@pytest.mark.parametrize("data,limits,reason,measured", CONTAINER_REFUSALS)
def test_container_refusal_carries_the_boxes_it_walked(data, limits, reason, measured):
    """A refused container measures the top-level box kinds walked up to the
    refusal, in file order and at most 32 of them; the number of boxes counted
    at any depth, which the bound on the list does not cap; and the sample
    codecs read so far."""
    checked = v.inspect_container(data, limits)
    assert (checked.reason, checked.stopped) == (reason, None)
    assert checked.measured == measured
    assert_plain(checked.measured)


@pytest.mark.parametrize(
    "data,undecoded",
    [
        (movie(codecs=(b"vp09",)), ("vp09",)),
        (movie(codecs=(b"avc1", b"mp4a", b"tmcd")), ("tmcd",)),
        (movie(fragmented=True, codecs=(b"vp09",)), ("vp09",)),
    ],
    ids=["vp09", "only-the-undecoded-entry", "in-a-walk-that-stopped"],
)
def test_a_codec_the_probe_does_not_decode_is_recorded_not_refused(data, undecoded):
    """The story derivation re-encodes every stream, so a sample entry outside
    the probe's decoders is recorded, whether or not the walk read the
    container whole, and refuses nothing."""
    checked = v.inspect_container(data)
    assert (checked.reason, checked.undecoded) == (None, undecoded)


#: ffmpeg's line for a decoder `-codec_whitelist` keeps closed.
CLOSED = b"[mpeg4 @ 0x1] Codec (mpeg4) not on whitelist 'h264,hevc,aac'\n"
#: Its line for the decoder of an MP3 track in an `mp4a` entry and of a cover
#: picture: streams the walk finds no codec to record for.
MP3 = b"[mp3float @ 0x2] Codec (mp3float) not on whitelist 'h264,hevc,aac'\n"
COVER = b"[mjpeg @ 0x3] Codec (mjpeg) not on whitelist 'h264,hevc,aac'\n"


@pytest.mark.parametrize(
    "returncode,stderr,state,reason,skipped",
    [
        (0, CLOSED, "supported", "validated", None),
        (1, CLOSED, "supported", "validated", {"closed_decoders": ["mpeg4"]}),
        (
            1,
            CLOSED + b"    Last message repeated 1 times\n" + CLOSED,
            "supported",
            "validated",
            {"closed_decoders": ["mpeg4"]},
        ),
        (
            1,
            CLOSED + b"moov atom truncated\n",
            "validation_pending",
            "probe_incomplete",
            None,
        ),
        (
            0,
            b"Protocol 'http' not on whitelist 'file'!\n",
            "validation_pending",
            "probe_incomplete",
            None,
        ),
        (
            1,
            b"[mov,mp4,m4a,3gp,3g2,mj2 @ 0x1] stream 0, error opening alias:"
            b" path='codec (x) not on whitelist'\n",
            "validation_pending",
            "probe_incomplete",
            None,
        ),
        (
            1,
            CLOSED + b"[h264 @ 0x2] corrupt input; last message repeated 1 times\n",
            "unsupported",
            "malformed_video",
            None,
        ),
        (
            0,
            b"skipped: codec (x) not on whitelist, left open\n",
            "validation_pending",
            "probe_incomplete",
            None,
        ),
    ],
    ids=[
        "a-closed-decoder",
        "stopped-at-it",
        "stopped-at-it-as-ffprobe-says-it",
        "and-another-line",
        "a-closed-protocol",
        "a-fault-that-quotes-the-decoder-line",
        "a-fault-that-says-repeated",
        "the-decoder-words-in-a-note",
    ],
)
async def test_a_decoder_the_whitelist_keeps_closed_is_not_a_fault_of_the_file(
    monkeypatch, tmp_path, returncode, stderr, state, reason, skipped
):
    """ffmpeg's line for a decoder outside `-codec_whitelist` is the probe's
    own bound. Alone, the probe ran clean; a probe it stopped gave no facts,
    so the file is its derivation's to read, as one with more streams than the
    probe opens, and its evidence names the decoder, as ffprobe does. Any
    other line still counts, a refused protocol among them: a line is the
    decoder's, or a repeat of it, only when it is that line whole, so a fault
    that merely holds their words is still a fault."""
    result = await validated_video(
        monkeypatch,
        tmp_path,
        probe_facts(),
        movie(codecs=(b"mp4v", b"mp4a")),
        stderr=stderr,
        returncode=returncode,
    )
    assert (result.state, result.reason) == (state, reason)
    assert result.evidence.get("probe_skipped") == skipped


async def test_the_decoders_the_probe_declined_are_named_as_it_names_them(
    monkeypatch, tmp_path
):
    """A stream the walk finds no codec to record for (an MP3 track in an
    `mp4a` entry, a cover picture) is a decoder the probe declined all the
    same: the evidence names each decoder once, in order, as ffprobe does, and
    the walk records nothing as undecoded."""
    result = await validated_video(
        monkeypatch,
        tmp_path,
        probe_facts(),
        movie(codecs=(b"avc1", b"mp4a")),
        stderr=MP3 + b"    Last message repeated 1 times\n" + COVER,
        returncode=1,
    )
    assert (result.state, result.reason) == ("supported", "validated")
    assert result.evidence["probe_skipped"] == {
        "closed_decoders": ["mjpeg", "mp3float"]
    }
    assert "undecoded_codecs" not in result.evidence


#: Every container the walk cannot read whole: the bytes, the limits (None for
#: the defaults), where it stopped, what it walked until then and the tracks it
#: reached.
CONTAINER_STOPS = [
    pytest.param(
        movie(fragmented=True),
        None,
        "container_evidence_incomplete",
        {
            "boxes": ["ftyp", "moov", "mdat", "moof"],
            "box_count": 13,
            "sample_codecs": ["avc1"],
        },
        1,
        id="fragmented",
    ),
    pytest.param(
        movie().replace(b"url ", b"ZZZZ"),
        None,
        "container_evidence_incomplete",
        {
            "boxes": ["ftyp", "moov", "mdat"],
            "box_count": 12,
            "sample_codecs": ["avc1"],
        },
        1,
        id="unknown_reference",
    ),
    pytest.param(
        movie(),
        v.ValidationLimits(boxes=2),
        "container_budget",
        {"boxes": ["ftyp", "moov"], "box_count": 3, "sample_codecs": []},
        0,
        id="box_budget",
    ),
    pytest.param(
        movie(),
        v.ValidationLimits(depth=2),
        "container_budget",
        {"boxes": ["ftyp", "moov"], "box_count": 4, "sample_codecs": []},
        1,
        id="depth_budget",
    ),
]


@pytest.mark.parametrize("data,limits,stopped,measured,streams", CONTAINER_STOPS)
def test_a_container_the_walk_cannot_read_whole_is_not_refused(
    data, limits, stopped, measured, streams
):
    """A walk past its budget, or over what it does not follow (a fragmented
    movie, a reference of a kind it does not know), refuses nothing for it: the
    story derivation re-encodes the file. The walk keeps what it read, as a
    refusal does, and names where it stopped."""
    checked = v.inspect_container(data, limits)
    assert (checked.reason, checked.stopped, checked.streams) == (
        None,
        stopped,
        streams,
    )
    assert checked.measured == measured
    assert_plain(checked.measured)


@pytest.mark.parametrize("data,limits,stopped,measured,streams", CONTAINER_STOPS)
async def test_a_walk_that_stopped_is_evidence_and_the_probe_decides(
    data, limits, stopped, measured, streams, monkeypatch, tmp_path
):
    """A video whose walk stopped short is judged on what the probe reads, and
    its evidence records where the walk stopped and what it read."""
    result = await validated_video(monkeypatch, tmp_path, probe_facts(), data, limits)
    assert (result.state, result.reason) == ("supported", "validated")
    assert result.evidence["container"] == {"stopped": stopped, **measured}


#: What a refusal sentence says for a value its record lacks.
UNREAD = ("another format", "?", "None")


def sentence_of(check, measured):
    """The card's sentence for a refusal by *check* that recorded *measured*."""
    return refusal_evidence.plain(
        refusal_evidence.evidence(
            "local_validation",
            state="unsupported",
            check=check,
            measured=dict(measured),
        )
    )


def assert_reads_with_what_it_measured(check, measured):
    """The card's sentence for a refusal the validator raised is its check's
    own, built from the values the check recorded: never the sentence for a
    check without words, and never a placeholder for a value it did not find.
    A measured value renamed on either side fails here, where the sentence
    table's own cases, built by hand, would not."""
    sentence = sentence_of(check, measured)
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


@pytest.mark.parametrize("data,limits,reason,measured", CONTAINER_REFUSALS)
def test_a_container_refusal_reads_with_what_it_measured(
    data, limits, reason, measured
):
    """Each container the walker refuses reads as its own sentence."""
    checked = v.inspect_container(data, limits)
    assert_reads_with_what_it_measured(checked.reason, checked.measured)


def test_clean_container_measures_nothing():
    """A container the walk reads whole carries an empty `measured`, and the
    tracks it found."""
    checked = v.inspect_container(movie())
    assert (checked.reason, checked.stopped, checked.streams) == (None, None, 1)
    assert checked.measured == {}


@pytest.mark.parametrize("data,limits,reason,measured", CONTAINER_REFUSALS)
async def test_container_refusal_is_the_failed_check_of_the_result(
    data, limits, reason, measured, tmp_path
):
    """What a container refusal measured is the `failed_check` of the result,
    under the refusal's reason."""
    limits = replace(
        limits or v.ValidationLimits(),
        ffprobe="/missing/tool",
        temporary_root=str(tmp_path),
    )
    result = await v.validate_media(request(data, "video"), limits=limits)
    assert result.state == "unsupported"
    assert result.reason == reason
    assert result.failed_check == {"check": reason, "measured": measured}


@pytest.mark.parametrize("kind,cap", [("image", 8_388_608), ("video", 40_000_000)])
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
    """A result that is supported has no `failed_check`, and an image passed
    the one check that holds it to a bound, its size."""
    data = image_bytes()
    result = await v.validate_media(request(data))
    assert result.state == "supported"
    assert result.failed_check is None
    cap = v.SUPPORTED_MEDIA_MATRIX["byte_caps"]["image"]
    assert [dict(check) for check in result.passed_checks] == [
        {"check": "byte_limit", "measured": {"bytes": len(data), "cap": cap}}
    ]


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
    over_the_cap = bytes(v.SUPPORTED_MEDIA_MATRIX["byte_caps"]["image"] + 1)
    for data, kind in ((over_the_cap, "image"), (movie(external=True), "video")):
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


async def validated_video(
    monkeypatch,
    tmp_path,
    facts,
    data=None,
    limits=None,
    argvs=None,
    stderr=b"",
    returncode=0,
):
    """`validate_media` on a video whose probe answers with ffprobe's *facts*,
    *stderr* and *returncode*: the bounded run is faked, and the real
    `_validate_video` reads the answer. Each probe's argv is appended to
    *argvs* when given."""

    async def probe(argv, *args):
        if argvs is not None:
            argvs.append(argv)
        return SimpleNamespace(
            returncode=returncode, stdout=json.dumps(facts).encode(), stderr=stderr
        )

    monkeypatch.setattr(v, "run_bounded", probe)
    return await v.validate_media(
        request(movie() if data is None else data, "video"),
        limits=replace(limits or v.ValidationLimits(), temporary_root=str(tmp_path)),
    )


async def test_a_supported_video_records_each_check_it_passed_with_its_bounds(
    monkeypatch, tmp_path
):
    """A supported file says what it passed: each check that holds it to a
    bound, with the value it measured and the bound the matrix sets."""
    data = movie()
    facts = probe_facts(duration="12.5", fps="30/1")
    result = await validated_video(monkeypatch, tmp_path, facts, data)
    matrix = v.SUPPORTED_MEDIA_MATRIX
    assert (result.state, result.failed_check) == ("supported", None)
    assert [dict(check) for check in result.passed_checks] == [
        {
            "check": "byte_limit",
            "measured": {"bytes": len(data), "cap": matrix["byte_caps"]["video"]},
        },
        {
            "check": "duration",
            "measured": {
                "duration_seconds": "25/2",
                "min_seconds": matrix["duration_seconds"][0],
                "max_seconds": matrix["duration_seconds"][1],
            },
        },
        {
            "check": "fps",
            "measured": {
                "fps": "30",
                "min_fps": matrix["fps"][0],
                "max_fps": matrix["fps"][1],
            },
        },
    ]
    assert (result.evidence["duration_seconds"], result.evidence["fps"]) == (
        "25/2",
        "30",
    )


@pytest.mark.parametrize(
    "facts,passed",
    [
        (probe_facts(fps="0/0"), ["byte_limit", "duration"]),
        (probe_facts(duration=None), ["byte_limit", "fps"]),
    ],
    ids=["fps", "duration"],
)
async def test_a_timing_the_probe_does_not_state_passed_no_check(
    monkeypatch, tmp_path, facts, passed
):
    """A frame rate or a duration the probe does not state is not judged, so
    it is not recorded as passed."""
    result = await validated_video(monkeypatch, tmp_path, facts)
    assert result.state == "supported"
    assert [check["check"] for check in result.passed_checks] == passed


async def test_a_file_with_more_streams_than_the_probe_opens_is_not_probed(
    monkeypatch, tmp_path
):
    """Past the probe's stream bound the probe is not run: the story
    derivation takes the first video and audio stream. The evidence says why,
    and each fact the probe would give is unstated, so no timing check is
    passed."""
    streams = v.ValidationLimits().streams + 1
    result = await validated_video(
        monkeypatch, tmp_path, probe_facts(), movie(tracks=streams)
    )
    assert (result.state, result.reason) == ("supported", "validated")
    assert result.evidence["probe_skipped"] == {
        "streams": streams,
        "max_streams": v.ValidationLimits().streams,
    }
    evidence = result.evidence
    assert (
        evidence["duration_seconds"],
        evidence["fps"],
        evidence["trimmed_to_seconds"],
    ) == ("unstated", "unstated", None)
    assert evidence["derivation"] == v.story_video_encoding()
    assert [check["check"] for check in result.passed_checks] == ["byte_limit"]


#: ffmpeg's line for a file with more streams than `-max_streams`, and the
#: lines it leaves when the stream past the bound is an attached picture.
BOUND = (
    b"[mov,mp4,m4a,3gp,3g2,mj2 @ 0x1] Number of streams exceeds max_streams"
    b" parameter (16), see the documentation if you wish to increase it\n"
)
AFTER_BOUND = (
    b"[mov,mp4,m4a,3gp,3g2,mj2 @ 0x1] Error parsing cover art.\n"
    b"[mov,mp4,m4a,3gp,3g2,mj2 @ 0x1] error reading header\n"
    b"/tmp/media-validation-x/asset: Cannot allocate memory\n"
)


async def test_a_file_the_probe_stops_at_its_own_stream_bound_is_not_probed(
    monkeypatch, tmp_path
):
    """An attached picture is a stream to the probe and no track to the walk,
    so a file the walk counts within the bound can hold more streams than the
    probe opens. The probe stops at its own bound, and the lines after the
    bound's are its consequences: the file is its story derivation's to read,
    and the evidence says what the walk counted beside the bound. Each fact
    the probe would give is unstated, so no timing check is passed."""
    tracks = 2
    result = await validated_video(
        monkeypatch,
        tmp_path,
        probe_facts(),
        movie(tracks=tracks),
        stderr=BOUND + AFTER_BOUND,
        returncode=1,
    )
    assert (result.state, result.reason) == ("supported", "validated")
    assert result.evidence["probe_skipped"] == {
        "max_streams": v.ValidationLimits().streams,
        "walked_streams": tracks,
    }
    evidence = result.evidence
    assert (
        evidence["duration_seconds"],
        evidence["fps"],
        evidence["trimmed_to_seconds"],
    ) == ("unstated", "unstated", None)
    assert [check["check"] for check in result.passed_checks] == ["byte_limit"]


@pytest.mark.parametrize(
    "stderr",
    [b"error opening alias: " + BOUND, BOUND[:-1] + b", ignored\n"],
    ids=["led-by-other-text", "followed-by-other-text"],
)
async def test_the_bounds_words_in_another_shape_are_not_the_probes_bound(
    monkeypatch, tmp_path, stderr
):
    """Only the probe's whole line is its stream bound: the same words inside
    another message are a fault of their own, which leaves the file pending."""
    result = await validated_video(
        monkeypatch, tmp_path, probe_facts(), movie(), stderr=stderr, returncode=1
    )
    assert (result.state, result.reason) == ("validation_pending", "probe_incomplete")
    assert "probe_skipped" not in result.evidence


async def test_the_probe_opens_up_to_its_named_stream_bound(monkeypatch, tmp_path):
    """A file with as many streams as the bound is probed, and the probe is
    told the bound, which is 16 and may only be tightened. It logs errors
    only, the level its stderr is read at, and opens no external reference."""
    bound = v.ValidationLimits().streams
    argvs = []
    result = await validated_video(
        monkeypatch, tmp_path, probe_facts(), movie(tracks=bound), argvs=argvs
    )
    (argv,) = argvs
    assert argv[argv.index("-max_streams") + 1] == str(bound) == "16"
    assert argv[argv.index("-codec_whitelist") + 1] == "h264,hevc,aac"
    assert argv[argv.index("-v") + 1] == "error"
    assert argv[argv.index("-enable_drefs") + 1] == "0"
    assert (result.state, "probe_skipped" in result.evidence) == ("supported", False)
    with pytest.raises(ValueError):
        v.ValidationLimits(streams=bound + 1)


@pytest.mark.parametrize(
    "facts,check,key,low_name,high_name,unit",
    [
        (
            probe_facts(duration="2"),
            "duration",
            "duration_seconds",
            "min_seconds",
            "max_seconds",
            " s",
        ),
        (probe_facts(fps="120/1"), "fps", "fps", "min_fps", "max_fps", ""),
    ],
    ids=["duration", "fps"],
)
async def test_a_refused_timing_records_its_bounds_and_reads_with_them(
    monkeypatch, tmp_path, facts, check, key, low_name, high_name, unit
):
    """A refused duration or frame rate records the bounds it was held to
    beside the value, and its sentence says those bounds: a bound renamed on
    either side fails here."""
    result = await validated_video(monkeypatch, tmp_path, facts)
    low, high = v.SUPPORTED_MEDIA_MATRIX[key]
    measured = result.failed_check["measured"]
    assert (result.reason, measured[low_name], measured[high_name]) == (
        check,
        low,
        high,
    )
    assert result.passed_checks == ()
    sentence = sentence_of(check, measured)
    assert sentence.endswith(f"; stories take {low} to {high}{unit}"), sentence


def test_a_refusal_names_a_check_of_the_closed_set():
    """A refusal carries a check from `UNSUPPORTED_CHECKS`, or the validator
    fails loudly: the refusal sentences cover that set."""
    assert str(v.UnsupportedMedia("duration")) == "duration"
    with pytest.raises(ValueError, match="made_up_check"):
        v.UnsupportedMedia("made_up_check")


def test_the_closed_set_is_every_check_the_validator_names():
    """Every check the validator refuses a file by is in the closed set, and
    the set names no check the validator has dropped."""
    source = Path(v.__file__).read_text(encoding="utf-8")
    named = set(re.findall(r'UnsupportedMedia\(\s*"([a-z_]+)"', source))
    assert named == v.UNSUPPORTED_CHECKS


async def test_a_video_longer_than_a_story_passes_its_duration_check_trimmed(
    monkeypatch, tmp_path
):
    """A video past the longest story is kept and trimmed, and its duration
    check says so beside the value it measured and the bounds it held."""
    result = await validated_video(monkeypatch, tmp_path, probe_facts(duration="72"))
    shortest, longest = v.SUPPORTED_MEDIA_MATRIX["duration_seconds"]
    (duration,) = [c for c in result.passed_checks if c["check"] == "duration"]
    assert result.state == "supported"
    assert dict(duration["measured"]) == {
        "duration_seconds": "72",
        "trimmed_to_seconds": str(longest),
        "min_seconds": shortest,
        "max_seconds": longest,
    }


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
